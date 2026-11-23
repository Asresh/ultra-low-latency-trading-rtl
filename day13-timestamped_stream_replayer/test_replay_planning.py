"""Test numeric clock conversion and offline source-script semantics."""

from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
import unittest

from replay_control import Instruction, ScriptCompiler, compile_script, parse_script, request
from replay_format import StampedPacket, write_tape
from replay_model import ReplayModel, load_commands
from replay_planner import (
    CompileOptions, compile_packets, predict, predict_report, rational, rebase_tape, retime_tape,
    round_ticks,
)
from replay_program import Command, Cycle, Operation, Packet, ReplayError, Status, Tape, U32


class ClockTests(unittest.TestCase):
    """Check exact arithmetic and deterministic serialization delays."""

    def test_rounding_at_negative_and_positive_ties(self):
        """Nearest chooses even and explicit floor or ceiling use signed rational order."""
        cases = (
            (Fraction(-5, 2), (-3, -2, -2)),
            (Fraction(-3, 2), (-2, -1, -2)),
            (Fraction(-1, 5), (-1, 0, 0)),
            (Fraction(1, 5), (0, 1, 0)),
            (Fraction(1, 2), (0, 1, 0)),
            (Fraction(3, 2), (1, 2, 2)),
            (Fraction(5, 2), (2, 3, 2)),
            (Fraction(51, 20), (2, 3, 3)),
            (Fraction(100), (100, 100, 100)),
        )
        for number, expected in cases:
            with self.subTest(number=number):
                self.assertEqual(tuple(round_ticks(number, mode)
                                       for mode in ("floor", "ceil", "nearest")), expected)
        with self.assertRaisesRegex(ReplayError, "rounding"):
            round_ticks(4, "unknown")
        for bad in ("-Infinity", "NaN", object(), "1/0"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ReplayError, "finite"):
                    rational(bad, "test")

    def test_clock_perfect_exact_period_and_speed(self):
        """Use one rational conversion from input interval to tick, even with rate scaling."""
        opts = CompileOptions(Fraction(156_250_000), lead_ticks=4, speed=Fraction(5, 2))
        self.assertEqual(opts.period_ns, Fraction(32, 5))
        self.assertEqual(opts.desired_tick(1080, 1000), 9)
        self.assertEqual(opts.desired_tick(Fraction(2133, 2), 1000), 9)
        self.assertEqual(opts.desired_tick(1000, 1000), 4)
        with self.assertRaisesRegex(ReplayError, "origin"):
            opts.desired_tick(999, 1000)

    def test_option_validation_exposes_the_failed_dimension(self):
        """Reject clocks, delays, capacities and policies that firmware cannot encode."""
        source = CompileOptions(100_000_000)
        for values, message in (
            ({"clock_hz": 0}, "positive"),
            ({"speed": -1}, "positive"),
            ({"lead_ticks": U32+1}, "lead"),
            ({"lead_ticks": 9, "limit_ticks": 8}, "maximum"),
            ({"beat_spacing": -1}, "beat spacing"),
            ({"packet_gap": -1}, "packet gap"),
            ({"rounding": "banked"}, "rounding"),
            ({"collision": "truncate"}, "policy"),
            ({"maximum_beats": -1}, "capacity"),
        ):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ReplayError, message):
                    replace(source, **values)

    def test_collision_is_late_without_shuffling_payloads(self):
        """Serialize packets in file order with an explicit placement report."""
        source = (
            StampedPacket(2000, 3, b"abcdefghxyz"),
            StampedPacket(2001, 4, b"hello"),
            StampedPacket(2060, 5, b"last"),
        )
        opts = CompileOptions(100_000_000, lead_ticks=2, beat_spacing=3, packet_gap=2)
        compiled = compile_packets(source, opts)
        self.assertEqual(compiled.origin, 2000)
        self.assertEqual([item.ticks for item in compiled.tape.packets()], [(2, 5), (7,), (9,)])
        self.assertEqual([item.payload for item in compiled.tape.packets()], [p.payload for p in source])
        self.assertEqual(compiled.worst_shift, 4)
        self.assertEqual(compiled.shifted, 2)
        self.assertEqual(compiled.report(verbose=True)["placements"][1], {
            "position": 1,
            "tag": 4,
            "relative_ns": "1",
            "desired_tick": 3,
            "actual_tick": 7,
            "last_tick": 7,
            "delay_ticks": 4,
            "bytes": 5,
            "beats": 1,
        })
        with self.assertRaisesRegex(ReplayError, "before available tick"):
            compile_packets(source, replace(opts, collision="fail"))

    def test_equal_timestamps_and_zero_gaps_remain_explicit(self):
        """Allow ties and expose that the realized bus has only one offer slot."""
        source = (StampedPacket(12, 8, b"abcdefghx"), StampedPacket(12, 9, b"Y"))
        opts = CompileOptions(10, beat_spacing=0, packet_gap=0)
        result = compile_packets(source, opts)
        self.assertEqual([beat.tick for beat in result.tape.events], [0, 0, 0])
        predictions = list(predict(result.tape))
        self.assertEqual([item.accepted for item in predictions], [1, 2, 3])
        self.assertEqual([item.late for item in predictions], [0, 1, 2])
        self.assertEqual(predict_report(result.tape)["maximum_lateness_ticks"], 2)

    def test_capacity_and_time_limits_are_checked_before_tail(self):
        """Reject the whole compilation rather than returning a truncated packet."""
        inputs = (StampedPacket(2, 3, b"first"), StampedPacket(10, 4, b"abcdefghQ"))
        with self.assertRaisesRegex(ReplayError, "entries"):
            compile_packets(inputs, CompileOptions(100_000_000, maximum_beats=2))
        with self.assertRaisesRegex(ReplayError, "maximum offset"):
            compile_packets(inputs, CompileOptions(100_000_000, limit_ticks=1))
        with self.assertRaisesRegex(ReplayError, "timestamp"):
            compile_packets(reversed(inputs), CompileOptions(100_000_000))
        with self.assertRaisesRegex(ReplayError, "predecessor"):
            compile_packets(inputs, CompileOptions(100_000_000), origin=3)
        empty = compile_packets((), CompileOptions(18), origin=19)
        self.assertEqual(empty.report(), {
            "origin_ns": "19", "packets": 0, "beats": 0, "bytes": 0,
            "duration_ticks": 0, "shifted_packets": 0, "worst_shift_ticks": 0,
        })

    def test_transforms_preserve_bytes_markers_and_direction(self):
        """Shift and scale only due times, preserving complete packet boundaries."""
        a = Packet.paced(1, b"abcDEFghi", 19, 3)
        b = Packet.paced(2, b"k", 72)
        tape = Tape.from_packets((a, b))
        moved = rebase_tape(tape, 2)
        self.assertEqual([i.tick for i in moved.events], [2, 5, 55])
        scaled = retime_tape(moved, Fraction(1, 2), "nearest")
        self.assertEqual([i.tick for i in scaled.events], [1, 2, 28])
        self.assertEqual([p.payload for p in scaled.packets()], [a.payload, b.payload])
        self.assertEqual([p.tag for p in scaled.packets()], [1, 2])
        with self.assertRaisesRegex(ReplayError, "tick"):
            rebase_tape(tape, U32)
        with self.assertRaisesRegex(ReplayError, "positive"):
            retime_tape(tape, 0)

    def test_predict_rejects_over_budget_before_work(self):
        """Avoid simulation expansion when the requested repetitive image is too large."""
        tape = Tape.from_packets((Packet.paced(0, b"A", 9),))
        for args, message in (
            ((tape, 0), "nonempty"),
            ((tape, -1), "repeats"),
            ((tape, 2, -1), "gap"),
            ((Tape(()),), "nonempty"),
        ):
            with self.subTest(args=args):
                with self.assertRaisesRegex(ReplayError, message):
                    tuple(predict(*args))
        with self.assertRaisesRegex(ReplayError, "event limit"):
            tuple(predict(tape, 2, max_events=1))

    def test_independent_analytic_prediction_matches_cycle_oracle(self):
        """Check dense, sparse, bursty and repeated programs without backpressure."""
        rng = Random(132246)
        for _ in range(120):
            gap, repeats = rng.randrange(12), rng.randrange(1, 5)
            tick = rng.randrange(4)
            packets = []
            for tag in range(rng.randrange(1, 8)):
                packet = Packet.paced(tag, b"x"*rng.randrange(1, 40), tick, rng.randrange(4))
                packets.append(packet)
                tick = packet.end + rng.randrange(7)
            tape = Tape.from_packets(packets)
            model = ReplayModel(len(tape.events))
            for entry in load_commands(tape):
                model.step(Cycle(cfg=entry))
            self.assertEqual(model.step(Cycle(cfg=Command.start(repeats, gap))).request.op, Operation.START)
            actual = []
            for _ in range(2500):
                if not model.active:
                    break
                step = model.step(Cycle())
                if step.output:
                    actual.append((step.output.event, step.output.due, step.output.offered, step.observation.now))
            else:
                self.fail("independent timeline exceeded idle watchdog")
            analytic = [(row.event, row.due, row.offered, row.accepted) for row in predict(tape, repeats, gap)]
            self.assertEqual(actual, analytic)


    def test_consecutive_passes_recover_payload_independent_of_absolute_origin(self):
        """Constant source timestamp shifts cannot change the compiled long-gap schedule."""
        rng = Random(71340)
        options = CompileOptions(Fraction(312500000), lead_ticks=5,
                                 beat_spacing=3, packet_gap=4, rounding="nearest")
        for fixture in range(35):
            start = Fraction(rng.randrange(2 ** 48), 7)
            current = start
            captured = []
            for tag in range(7):
                current += Fraction(rng.randrange(1,1000), rng.randrange(1,17))
                payload = bytes(rng.getrandbits(8) for _ in range(rng.randrange(1,38)))
                captured.append(StampedPacket(current, tag, payload))
            ref = compile_packets(captured, options)
            offset = Fraction(1 << 54, 19)
            shifted = [replace(item, time_ns=item.time_ns+offset) for item in captured]
            again = compile_packets(shifted, options)
            self.assertEqual(ref.tape, again.tape)
            self.assertEqual(again.origin-ref.origin, offset)
            self.assertEqual(ref.placements, again.placements)
            self.assertEqual(predict_report(ref.tape, 3, 7), predict_report(again.tape, 3, 7))

    def test_packet_gap_scales_only_after_last_beat(self):
        """Ensure dense burst timestamps don't move the previous packet's last beat."""
        packets = (
            StampedPacket(18, 0, b"A" * 23),
            StampedPacket(18, 1, b"B" * 13),
            StampedPacket(1000, 2, b"C"),
        )
        options = CompileOptions(100000000, packet_gap=2, beat_spacing=4)
        base = compile_packets(packets, options)
        large_gap = compile_packets(packets, replace(options, packet_gap=6))
        self.assertEqual(tuple(base.tape.packets())[0], tuple(large_gap.tape.packets())[0])
        self.assertEqual(base.placements[1].delay_ticks, 10)
        self.assertEqual(large_gap.placements[1].delay_ticks, 14)
        self.assertEqual(base.placements[-1].actual_tick, large_gap.placements[-1].actual_tick)
        self.assertEqual(base.tape.payload_bytes, large_gap.tape.payload_bytes)

class ScriptTests(unittest.TestCase):
    """Run documented source-level scripting paths without simulator internals."""

    def test_parser_preserves_quotes_comments_and_expectation(self):
        """Resolve paths with spaces while ignoring comments and formatting."""
        statements = list(parse_script([
            "# Empty program",
            'LOAD "tape with spaces.csv" # keep unchanged',
            "",
            "start 0x03 0 -> ok",
            "raw 7 0 0 0 0 0 0 0 -> BAD_OP",
        ]))
        self.assertEqual(statements, [
            Instruction(2, "load", ("tape with spaces.csv",)),
            Instruction(4, "start", ("0x03", "0"), Status.OK),
            Instruction(5, "raw", ("7","0","0","0","0","0","0","0"), Status.BAD_OP),
        ])
        self.assertEqual(request(statements[1]), Command.start(3))
        self.assertEqual(request(statements[2]), Command(7))

    def test_parser_rejects_illformed_and_illegal_status(self):
        """Give the caller the script line number and offending parser rule."""
        for text, message in (
            ("", None),
            ("load 'undelimited", "quotation"),
            ("mystery", "unknown statement"),
            ("idle 4 -> OK", "only supported"),
            ("stop -> BAD_UNKNOWN", "unknown response"),
            ("start ->", "single"),
            ("start -> OK -> BUSY", "single"),
        ):
            with self.subTest(text=text):
                if message is None:
                    self.assertEqual(tuple(parse_script([text])), ())
                else:
                    with self.assertRaisesRegex(ReplayError, f"line 1: .*{message}"):
                        list(parse_script([text]))

    def test_config_commands_preserve_invalid_status_outcomes(self):
        """Drive negative bus inputs and observe no mutation of the stored image."""
        lines = [
            "start -> EMPTY",
            "append 0 0x1234 1 aabb",
            "start 0 -> BAD_REPEAT",
            "raw 0 0 1 0 1 2 0 0 -> BAD_KEEP",
            "raw 6 0 0 0 0 0 0 0 -> BAD_OP",
            "start",
            "await_done 8",
        ]
        comp = ScriptCompiler(2, ".")
        tape = comp.compile(parse_script(lines))
        self.assertGreater(len(tape), 7)
        self.assertEqual([applied.status for applied in comp.applied],
                         [Status.EMPTY, Status.OK, Status.BAD_REPEAT, Status.BAD_KEEP, Status.BAD_OP, Status.OK])
        self.assertEqual([applied.length for applied in comp.applied], [0, 1, 1, 1, 1, 1])

    def test_status_mismatch_and_watchdogs_are_actionable(self):
        """Stop fixture generation promptly when hardware would wait forever."""
        cases = (
            (["start -> OK"], "expected OK, got EMPTY"),
            (["append 40 9 1 ff", "start", "await_offer 2"], "await_offer"),
            (["responses off", "clear", "start"], "request stayed blocked"),
            (["idle 10000"], "edge watchdog"),
            (["sink orange"], "accepts on or off"),
            (["clear extra"], "arguments"),
        )
        for source, problem in cases:
            with self.subTest(source=source):
                compiler = ScriptCompiler(4, ".", max_cycles=52, request_wait=8)
                with self.assertRaisesRegex(ReplayError, problem):
                    compiler.compile(parse_script(source))

    def test_script_handles_pause_stops_and_absolute_context(self):
        """Load from the script directory and preserve held traffic during a stop."""
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            tape = Tape.from_packets([Packet.paced(6, b"test data", 3)])
            source = folder / "data file.trp"
            write_tape(tape, source)
            script = folder / "sequence.txt"
            script.write_text(
                'load "data file.trp"\nsink off\nstart 2 4\nawait_offer 6\npause\n'
                "idle 3\nstop\nsink on\nawait_done 16\nreset\nstart -> EMPTY\n"
            )
            cycles, applied = compile_script(script, 4)
            self.assertEqual([entry.line for entry in applied[:3]], [1, 1, 1])
            self.assertEqual(applied[-1].status, Status.EMPTY)
            model = ReplayModel(4)
            outputs = [edge.output.event.payload for edge in model.play(cycles) if edge.output]
            self.assertEqual(outputs, [b"test dat"])

    def test_requested_depth_applies_to_loaded_files(self):
        """Reject an oversized image before any configuration request is emitted."""
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            tape = Tape.from_packets([Packet.paced(2, b"long packet 01234567", 3)])
            write_tape(tape, folder / "big.trp")
            compiler = ScriptCompiler(2, folder)
            with self.assertRaisesRegex(ReplayError, "line 1 .*capacity"):
                compiler.compile(parse_script(["load big.trp"]))
            self.assertEqual(compiler.applied, [])
            self.assertEqual(len(compiler.cycles), 1)


if __name__ == "__main__":
    unittest.main()
