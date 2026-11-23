"""Validate accepted stream reconstruction, comparison, and run-level timing."""

from dataclasses import replace
from fractions import Fraction
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from replay_audit import CapturedOffer, PinSample, first_difference, read_transcript, verify
from replay_compare import ComparisonOptions, check_run, export_packets, select_run
from replay_control import ScriptCompiler, parse_script
from replay_format import StampedPacket
from replay_metrics import (
    Budget, Delivery, Distribution, PacketAssembler, PacketDelivery, ReplayRun, extract_runs,
)
from replay_model import ReplayModel
from replay_program import Command, Cycle, Packet, ReplayError, Statistics, Status, Tape
from replay_vectors import decode_cycle, encode_cycle, read_cyclestream, write_cyclestream


def delivery(cycle, *, number=1, data=b"ab", tag=1, last=True, due=None, offered=None):
    """Create a typed output transfer with optional distinct virtual times."""
    due = cycle-2 if due is None else due
    offered = cycle-1 if offered is None else offered
    return Delivery(cycle, number, tag, data, last, due, offered, cycle)


def timeline(instructions, depth=5):
    """Execute a source script and build matching current-edge pin observations."""
    script = ScriptCompiler(depth, ".")
    cycles = script.compile(parse_script(instructions))
    reference = ReplayModel(depth)
    observed = tuple(
        PinSample.expected(index, reference.step(cycle).observation)
        for index, cycle in enumerate(cycles)
    )
    return cycles, observed


def repeat_fixture():
    """Return a two-pass run with equal-tick records and a known repeat gap."""
    tape = Tape.from_packets([
        Packet.paced(8, b"abcdefghrst", 0, 0),
        Packet.paced(9, b"z", 4),
    ])
    cycles, samples = timeline([
        "append 0 8 0 6162636465666768",
        "append 0 8 1 727374",
        "append 4 9 1 7a",
        "start 2 2",
        "await_done 20",
    ])
    return tape, cycles, samples, extract_runs(cycles, samples)[0]


class CaptureTimelineTests(unittest.TestCase):
    """Test exact physical and virtual timing across each form of interruption."""

    def test_ready_not_valid_controls_delivery_and_stall_duration(self):
        """Count a valid held beat once and preserve the total physical hold count."""
        cycles, samples = timeline([
            "append 0 3 1 11",
            "sink off",
            "start",
            "await_offer 10",
            "idle 6",
            "sink on",
            "await_done 5",
        ])
        run, = extract_runs(cycles, samples)
        self.assertEqual(run.output_hold_cycles, 6)
        self.assertEqual(run.cycles_to_first, 7)
        self.assertEqual(run.deliveries[0].held, 7)
        self.assertEqual(run.packets.complete[0].payload, b"\x11")
        self.assertIsNone(first_difference(samples[0], samples[0]))
        audit = verify(cycles, samples, 5)
        self.assertEqual(audit.events["beats"], 1)
        self.assertEqual(audit.max_output_hold, 6)

    def test_pause_retains_wall_stall_even_with_constant_virtual_time(self):
        """Distinguish virtual scheduling time from wall-cycle backpressure."""
        cycles, observed = timeline([
            "append 0 1 1 2122",
            "sink off",
            "start",
            "await_offer 4",
            "pause",
            "idle 7",
            "sink on",
            "await_done 4",
        ])
        run, = extract_runs(cycles, observed)
        self.assertEqual(run.paused_cycles, 8)
        self.assertEqual(run.output_hold_cycles, 8)
        self.assertEqual(run.deliveries[0].held, 1)
        self.assertEqual(run.wall_cycles, 10)
        self.assertEqual(run.end_reason, "done")
        self.assertEqual(run.record()["bytes"], 2)

    def test_reset_preserves_history_without_exporting_a_fragment_as_packet(self):
        """An interrupted first beat is counted but never appears as a full payload."""
        cycles, observed = timeline([
            "append 0 1 0 2122232425262728",
            "append 30 1 1 30",
            "start",
            "idle 3",
            "reset",
            "append 0 9 1 45",
            "start",
            "await_done 6",
        ])
        first, second = extract_runs(cycles, observed)
        self.assertEqual(first.end_reason, "reset")
        self.assertEqual(len(first.deliveries), 1)
        self.assertEqual(len(first.packets.complete), 0)
        self.assertEqual(first.packets.cut[0][0].payload, b"!\"#$%&'(")
        self.assertEqual(second.end_reason, "done")
        self.assertEqual(second.packets.complete[0].payload, b"E")
        self.assertEqual(second.record(detailed=True)["beats_per_pass"], {1: 1})

    def test_incomplete_capture_is_explicit(self):
        """Report reasonable snapshots of a run only on an opt-in path."""
        cycles, samples = timeline([
            "append 100 5 1 aa",
            "start",
            "idle 2",
        ])
        with self.assertRaisesRegex(ReplayError, "ended during"):
            extract_runs(cycles, samples)
        run, = extract_runs(cycles, samples, allow_incomplete=True)
        self.assertEqual(run.end_reason, "capture_end")
        self.assertIsNone(run.cycles_to_first)
        self.assertEqual(run.wall_cycles, 3)
        self.assertEqual(run.record()["late_virtual_ticks"]["count"], 0)

    def test_capture_flags_malformed_control_state(self):
        """Check physical row indexes and active-to-idle handshake rules."""
        _, cycles, samples, run = repeat_fixture()
        sample_list = list(samples)
        sample_list[run.ended] = replace(sample_list[run.ended], done=False)
        with self.assertRaisesRegex(ReplayError, "active fell"):
            extract_runs(cycles, sample_list)
        bad_position = (replace(samples[0], index=99),) + samples[1:]
        with self.assertRaisesRegex(ReplayError, "wrong sample index"):
            extract_runs(cycles, bad_position)
        with self.assertRaisesRegex(ReplayError, "length mismatch"):
            extract_runs(cycles[:-1], samples)
        with self.assertRaisesRegex(ReplayError, "empty"):
            extract_runs([], [])

    def test_metric_distribution_min_median_tail_and_exact_average(self):
        """Report finite nearest-rank statistics without rounding the arithmetic mean."""
        stats = Distribution.from_samples([5, 0, 8, 1, 4, 3])
        self.assertEqual(stats.record(), {
            "count": 6, "min": 0, "max": 8, "mean": "7/2",
            "median": 3, "p95": 8, "p99": 8,
        })
        self.assertIsNone(Distribution.from_samples([]).average)
        full = Distribution.from_samples(range(200))
        self.assertEqual((full.at_p95, full.at_p99), (189, 197))
        self.assertEqual(full.average, Fraction(199, 2))

    def test_budget_diagnostics_expose_run_and_measured_field(self):
        """Associate failures only with a measured overrun or a requested framing limit."""
        _, _, _, run = repeat_fixture()
        self.assertEqual(tuple(Budget(4, 8).failures([run])), ())
        failed = tuple(Budget(0, 0).failures([run]))
        self.assertEqual(failed, (
            "run 0: lateness 1 exceeds 0 virtual ticks",
            "run 0: first transfer 1 exceeds 0 cycles",
        ))
        extra = PacketAssembler()
        extra.step(delivery(2, data=b" " * 8, last=False))
        extra.end_run()
        run.packets = extra
        self.assertEqual(tuple(Budget(allow_fragments=False).failures([run])),
                         ("run 0: output ended with a partial packet",))

    def test_fragment_identity_and_order_are_checked(self):
        """Require stable tag/pass throughout one packet and increasing transfer cycles."""
        prefix = delivery(3, data=b"A"*8, last=False)
        tail = delivery(6, data=b"BC")
        result = PacketDelivery((prefix, tail))
        self.assertEqual(result.payload, b"A"*8+b"BC")
        self.assertEqual(result.tag, 1)
        self.assertEqual(result.first.cycle, 3)
        self.assertEqual(result.last.cycle, 6)
        self.assertEqual(result.pass_number, 1)
        for broken in ((prefix,), (prefix, replace(tail, tag=7)),
                       (prefix, replace(tail, pass_number=2)), (tail, tail),
                       (prefix, replace(tail, cycle=3))):
            with self.subTest(broken=broken):
                with self.assertRaises(ReplayError):
                    PacketDelivery(broken)
        assembly = PacketAssembler()
        assembly.step(prefix)
        with self.assertRaisesRegex(ReplayError, "tag"):
            assembly.step(replace(tail, tag=2))

    def test_decode_field_mask_and_noncanonical_invalid_lanes(self):
        """A malformed offer cannot be interpreted as delivered bytes."""
        base = PinSample(1, True, None, CapturedOffer(0xAA, 1, True, 1, 0, 0, 1),
                         True, False, False, 1, Statistics())
        self.assertEqual(Delivery.capture(base).payload, b"\xaa")
        for changed, failure in (
            (replace(base.offer, data=0xABAA), "inactive"),
            (replace(base.offer, keep=0), "contiguous"),
            (replace(base.offer, keep=0x81), "contiguous"),
            (replace(base.offer, last=False), "framing"),
            (replace(base.offer, pass_number=0), "pass"),
            (replace(base.offer, due=1), "precedes"),
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ReplayError, failure):
                    Delivery.capture(replace(base, offer=changed))
        with self.assertRaisesRegex(ReplayError, "valid"):
            Delivery.capture(replace(base, offer=None))


class CompareTests(unittest.TestCase):
    """Verify a captured run against the original program independent of state stepping."""

    def test_check_replayed_bytes_and_due_ticks_in_both_passes(self):
        """Rebuild the repeat epoch from the actual final accepted tick."""
        tape, _, _, run = repeat_fixture()
        expected = ComparisonOptions(repeats=2, repeat_gap=2, maximum_lateness=1)
        results = check_run(tape, run, expected)
        self.assertEqual(results.expected_beats, 6)
        self.assertEqual(results.accepted_beats, 6)
        self.assertEqual(results.missing, 0)
        self.assertEqual(results.source_packets, 2)
        self.assertEqual(results.record(), {
            "expected_beats": 6, "accepted_beats": 6, "missing_beats": 0,
            "source_packets_per_pass": 2, "complete_passes": 2,
            "passes": [
                {"pass": 1, "base_tick": 0, "expected_beats": 3, "accepted_beats": 3,
                 "packets": 2, "accepted_bytes": 12, "last_accept_tick": 5,
                 "max_offer_delay_ticks": 1, "complete": True},
                {"pass": 2, "base_tick": 8, "expected_beats": 3, "accepted_beats": 3,
                 "packets": 2, "accepted_bytes": 12, "last_accept_tick": 13,
                 "max_offer_delay_ticks": 1, "complete": True},
            ],
        })

    def test_compare_checks_data_independent_of_metrics(self):
        """Mutations in bytes, end marker, tag, pass, or time give specific errors."""
        tape, _, _, run = repeat_fixture()
        standard = ComparisonOptions(2, 2)
        original = run.deliveries[1]
        for record, message in (
            (replace(original, payload=b"bad"), "payload"),
            (replace(original, payload=b"rstxxxxx", last=False), "payload"),
            (replace(original, tag=255), "captured tag"),
            (replace(original, pass_number=2), "captured pass"),
            (replace(original, due=1), "scheduled tick"),
        ):
            with self.subTest(record=record):
                run.deliveries[1] = record
                with self.assertRaisesRegex(ReplayError, message):
                    check_run(tape, run, standard)
        run.deliveries[1] = original
        with self.assertRaisesRegex(ReplayError, "delay budget"):
            check_run(tape, run, ComparisonOptions(2, 2, maximum_lateness=0))
        with self.assertRaisesRegex(ReplayError, "more beats"):
            check_run(tape, run, ComparisonOptions(1, 2))
        with self.assertRaisesRegex(ReplayError, "scheduled tick"):
            check_run(tape, run, ComparisonOptions(2, 4))

    def test_compare_allows_only_a_prefix_cut(self):
        """Report missed suffix without hiding corruption inside the captured prefix."""
        tape, _, _, run = repeat_fixture()
        run.deliveries = run.deliveries[:4]
        with self.assertRaisesRegex(ReplayError, "only 4 of 6"):
            check_run(tape, run, ComparisonOptions(2, 2))
        cutoff = check_run(tape, run, ComparisonOptions(2, 2, allow_cut=True))
        self.assertEqual(cutoff.missing, 2)
        self.assertEqual([p.complete for p in cutoff.passes], [True, False])
        with self.assertRaisesRegex(ReplayError, "nonempty"):
            check_run(Tape(()), run)

    def test_export_uses_time_basis_and_packet_selection(self):
        """Preserve payload order and honor exact nanosecond time for each pass."""
        _, _, _, run = repeat_fixture()
        virtual = tuple(export_packets(run, 100_000_000, reference="due", selected_pass=2))
        self.assertEqual(virtual, (
            StampedPacket(0, 8, b"abcdefghrst"),
            StampedPacket(40, 9, b"z"),
        ))
        absolute = tuple(export_packets(run, 100_000_000, reference="accepted", rebase=False))
        self.assertEqual([p.time_ns for p in absolute], [10, 50, 90, 130])
        physical = tuple(export_packets(run, 100_000_000, reference="physical"))
        self.assertEqual([p.time_ns for p in physical], [0, 40, 80, 120])
        with self.assertRaises(ReplayError):
            tuple(export_packets(run, 0))
        with self.assertRaisesRegex(ReplayError, "time base"):
            tuple(export_packets(run, 10, reference="wall"))

    def test_run_selection_requires_unambiguous_input(self):
        """Select a run explicitly if an input capture contains more than one."""
        a, b = ReplayRun(0, 1), ReplayRun(1, 10)
        self.assertIs(select_run([a]), a)
        self.assertIs(select_run((a, b), 1), b)
        with self.assertRaisesRegex(ReplayError, "multiple"):
            select_run((a, b))
        with self.assertRaisesRegex(ReplayError, "outside"):
            select_run((a, b), -1)
        with self.assertRaisesRegex(ReplayError, "no active"):
            select_run(())


class PinFormatTests(unittest.TestCase):
    """Validate the raw cycle fixture format and strict one-to-one transcript matching."""

    def test_roundtrip_controls_config_hex_values_and_reset(self):
        """Preserve all raw encoded fields, even intentionally invalid requests."""
        request = Command(7, 0x01234567, 0xABCD6789EF127525, 255, True, 32768, 801, 7777)
        test_inputs = [Cycle(), Cycle(reset=True), Cycle(cfg=request, ready=False, rsp_ready=False)]
        for cycle in test_inputs:
            with self.subTest(cycle=cycle):
                self.assertEqual(decode_cycle(encode_cycle(cycle)), cycle)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "scenario.stim"
            write_cyclestream(path, test_inputs)
            self.assertEqual(tuple(read_cyclestream(path)), tuple(test_inputs))
            for blob, message in [
                ("", "count"),
                ("0\n", "count"),
                ("2\n"+encode_cycle(Cycle())+"\n", "short"),
                ("1\n"+encode_cycle(Cycle())+"\n"+encode_cycle(Cycle())+"\n", "more"),
                ("1\nnot hex\n", "twelve hexadecimal"),
            ]:
                with self.subTest(blob=blob):
                    path.write_text(blob)
                    with self.assertRaisesRegex(ValueError, message):
                        tuple(read_cyclestream(path))

    def test_noncanonical_cycle_controls_are_rejected(self):
        """Do not let numeric controls truncate silently at the simulator pin."""
        with self.assertRaisesRegex(ValueError, "single"):
            decode_cycle("0 1 0 0 0 0 2 0 0 0 0 0")
        with self.assertRaisesRegex(ValueError, "idle"):
            decode_cycle("0 0 3 0 0 0 0 0 0 0 0 0")
        with self.assertRaisesRegex(ValueError, "opcode"):
            decode_cycle("0 1 9 0 0 0 0 0 0 0 0 0")

    def test_pin_parser_rejects_pin_width_or_missing_cycle(self):
        """Decode a legal sample and diagnose corrupted wire fields and sequence numbers."""
        good = "4 1 1 0 2 1 ab 1 1 2 3 4 1 1 0 0 5 3 2 1 0"
        parsed = PinSample.decode(good)
        self.assertEqual(parsed.index, 4)
        self.assertEqual(parsed.response.status, Status.OK)
        self.assertEqual(parsed.offer.tag, 2)
        self.assertEqual(parsed.statistics, Statistics(3, 2, 1, 0))
        with self.assertRaisesRegex(ReplayError, "field"):
            PinSample.decode("1 2 3")
        with self.assertRaisesRegex(ReplayError, "integer"):
            PinSample.decode(good.replace(" ab ", " xx "))
        with self.assertRaisesRegex(ReplayError, "pin width"):
            PinSample.decode(good.replace(" ab ", " -1 "))
        with self.assertRaisesRegex(ReplayError, "status"):
            PinSample.decode(good.replace(" 1 0 2 ", " 1 f 2 "))
        with TemporaryDirectory() as temporary:
            path = Path(temporary)/"sample.trace"
            path.write_text(good+"\n")
            with self.assertRaisesRegex(ReplayError, "expected 0"):
                tuple(read_transcript(path))

    def test_audit_reports_first_mismatched_cycle_and_field(self):
        """Preserve the exact pin name to make waveform diagnosis straightforward."""
        _, cycles, samples, _ = repeat_fixture()
        changed = list(samples)
        changed[5] = replace(changed[5], now=10)
        with self.assertRaisesRegex(ReplayError, "cycle 5: now"):
            verify(cycles, changed, 5)
        with self.assertRaisesRegex(ReplayError, "lengths differ"):
            verify(cycles[:-1], samples, 5)
        with self.assertRaisesRegex(ReplayError, "empty"):
            verify([], [], 5)
        with self.assertRaisesRegex(ReplayError, "coverage missing"):
            verify(cycles, samples, 5, directed=True)


if __name__ == "__main__":
    unittest.main()
