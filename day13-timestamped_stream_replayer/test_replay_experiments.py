"""Cross-check independent periodic schedules, fixture files, and capture experiments."""

from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
import unittest

from replay_audit import PinSample
from replay_experiment import (
    Experiment, PROFILES, PeriodicReady, benchmark_fixture, capture_fixture,
    check_experiment, packet_lengths, save_experiment,
)
from replay_format import read_tape
from replay_metrics import extract_runs
from replay_model import ReplayModel
from replay_program import Packet, ReplayError, Tape, U48
from replay_vectors import read_cyclestream


def observation_row(sample):
    """Encode a simulated pin image using the physical transcript column order."""
    wire = sample.offer
    config = sample.response
    counters = sample.statistics
    fields = (
        int(sample.cfg_ready),
        int(config is not None),
        int(config.status) if config else 0,
        config.length if config else 0,
        int(wire is not None),
        wire.data if wire else 0,
        wire.keep if wire else 0,
        int(wire.last) if wire else 0,
        wire.tag if wire else 0,
        wire.due if wire else 0,
        wire.offered if wire else 0,
        wire.pass_number if wire else 0,
        int(sample.active),
        int(sample.paused),
        int(sample.done),
        sample.now,
        counters.beats,
        counters.packets,
        counters.late_beats,
        counters.passes,
    )
    return str(sample.index) + " " + " ".join(f"{value:x}" for value in fields)


def scheduled_model(experiment, capacity=None):
    """Generate expected wire observations from the independent edge model."""
    depth = len(experiment.tape.events) if capacity is None else capacity
    reference = ReplayModel(depth)
    cycles = experiment.pin_cycles()
    samples = tuple(
        PinSample.expected(index, reference.step(cycle).observation)
        for index, cycle in enumerate(cycles)
    )
    return cycles, samples


def save_synthetic_pins(folder, experiment):
    """Write independently generated pin samples for exercising file-level audit paths."""
    _, samples = scheduled_model(experiment)
    rows = "\n".join(observation_row(sample) for sample in samples)
    (folder/"output.trace").write_text(rows+"\n", encoding="ascii")


class AvailabilityTests(unittest.TestCase):
    """Check exact periodic opportunity arithmetic and range errors."""

    def test_roundtrip_and_wrap(self):
        """Treat tick zero as the first character, repeating only after a full period."""
        availability = PeriodicReady.parse("10011")
        self.assertEqual(availability.word(), "10011")
        self.assertEqual([availability.ready(i) for i in range(10)], [
            True, False, False, True, True, True, False, False, True, True,
        ])
        self.assertEqual([availability.next_ready(i) for i in range(10)], [0,3,3,3,4,5,8,8,8,9])

    def test_malformed_patterns_reject_non_bits_and_zero_service(self):
        """A periodic sink model must predict eventual acceptance."""
        for text, message in [
            ("101a", "characters"), ("1 0", "characters"), ("", "4096"),
            ("000", "never"), ("1"*4097, "4096"), (None, "characters"),
        ]:
            with self.subTest(text=text):
                with self.assertRaisesRegex(ReplayError, message):
                    PeriodicReady.parse(text)
        with self.assertRaisesRegex(ReplayError, "Boolean"):
            PeriodicReady((True,2))
        with self.assertRaisesRegex(ReplayError, "range"):
            PeriodicReady.parse("10").next_ready(U48)

    def test_analytic_ready_period_matches_bounded_forward_search(self):
        """Compare the closed time window against an independent Boolean sequence search."""
        rng = Random(4990)
        for _ in range(250):
            length = rng.randrange(1, 70)
            values = [rng.randrange(5) == 0 for _ in range(length)]
            values[rng.randrange(length)] = True
            mask = PeriodicReady(tuple(values))
            tick = rng.randrange(5000)
            extended = values * ((tick+length)//length+1)
            desired = extended.index(True, tick)
            self.assertEqual(mask.next_ready(tick), desired)


class ExperimentTests(unittest.TestCase):
    """Stress replay predictability under periodic but arbitrary backpressure."""

    def test_exact_dense_periodic_schedule(self):
        """Verify same-tick serialization and the next-pass gap anchored to acceptance."""
        tape = Tape.from_packets((
            Packet.paced(2, b"12345678Q", 0, 0),
            Packet.paced(3, b"R", 3),
        ))
        setup = Experiment(tape, PeriodicReady.parse("101"), 2, 4)
        self.assertEqual(
            [(row.pass_number,row.due,row.offered,row.accepted) for row in setup.predict()],
            [(1,0,0,2),(1,0,2,3),(1,3,3,5),(2,10,10,11),(2,10,11,12),(2,13,13,14)],
        )
        controls, observed = scheduled_model(setup)
        run, = extract_runs(controls, observed)
        report = setup.verify(run)
        self.assertEqual(report["complete_passes"], 2)
        self.assertEqual(report["expected_beats"], 6)
        self.assertEqual(report["physical_hold_cycles"], 2)
        self.assertEqual(report["expected_first_offset"], 2)
        self.assertEqual(report["measured_first_offset"], 2)

    def test_no_service_before_deadline_fails_with_cycle_budget(self):
        """Reject impossible budgets before creating a partial test stimulus file."""
        tape = Tape.from_packets((Packet.paced(2,b"one byte",99),))
        setup = Experiment(tape, PeriodicReady.parse("10"), cycle_limit=100)
        with self.assertRaisesRegex(ReplayError, "cycle budget"):
            tuple(setup.predict())
        with self.assertRaisesRegex(ReplayError, "cycle budget"):
            tuple(replace(setup, cycle_limit=2).predict())
        with self.assertRaisesRegex(ReplayError, "positive"):
            replace(setup, repeats=0)

    def test_tape_file_and_manifest_are_lossless(self):
        """Regenerate exactly the same experiment from its public build files."""
        case = benchmark_fixture("mixed", 4135)
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            save_experiment(folder, case)
            tape = read_tape(folder/"image.trp")
            self.assertEqual(tape, case.tape)
            self.assertEqual(tuple(read_cyclestream(folder/"input.stim")), case.pin_cycles())
            self.assertEqual(json.loads((folder/"analytical.json").read_text()),
                             [list(row) for row in case.expected_words()])
            with (folder/"settings.json").open() as cfg:
                recovered = Experiment.read(cfg, tape)
            self.assertEqual(recovered, case)
            save_synthetic_pins(folder, case)
            report = check_experiment(folder)
            self.assertEqual(report["accepted_beats"], len(case.tape.events) * case.repeats)
            self.assertEqual(report, json.loads((folder/"result.json").read_text()))

    def test_only_versioned_manifest_accepts_finite_options(self):
        """Reject schema or version drift rather than guessing the intended timing."""
        case = benchmark_fixture("sparse", 182)
        expected = case.manifest()
        options = ({**expected, "format": 2}, {**expected, "unknown": 7},
                   {**expected, "ready": "000"}, {**expected, "cycle_limit": 0}, [])
        for bad in options:
            with self.subTest(bad=bad):
                with self.assertRaises(ReplayError):
                    Experiment.read(StringIO(json.dumps(bad)), case.tape)

    def test_controlled_fixture_shapes_have_exact_payload_counts(self):
        """Each named pattern deliberately exercises different traffic and mask geometry."""
        self.assertEqual(len(set(packet_lengths(p) for p in PROFILES)), 3)
        for p in PROFILES:
            with self.subTest(profile=p):
                stamped = tuple(capture_fixture(p, 70823))
                native = benchmark_fixture(p, 70823)
                original = [packet.payload for packet in stamped]
                self.assertEqual([packet.payload for packet in native.tape.packets()], original)
                self.assertEqual(tuple(len(p.payload) for p in stamped), packet_lengths(p))
                self.assertEqual(native, benchmark_fixture(p, 70823))
                self.assertNotEqual(native, benchmark_fixture(p, 70824))
        with self.assertRaisesRegex(ReplayError, "unsupported"):
            packet_lengths("n/a")
        with self.assertRaisesRegex(ReplayError, "entries"):
            benchmark_fixture("dense", 1, depth=1)

    def test_random_periods_crosscheck_every_handshake(self):
        """Exercise hundreds of availability and tape shapes through two independent schedulers."""
        rng = Random(1230916)
        for experiment_id in range(180):
            packet_count = rng.randrange(1, 10)
            start = rng.randrange(12)
            packets = []
            for tag in range(packet_count):
                size = rng.randrange(1, 45)
                data = bytes(rng.randrange(256) for _ in range(size))
                placed = Packet.paced(tag, data, start, rng.randrange(6))
                packets.append(placed)
                start = placed.end + rng.randrange(4)
            mask = [rng.randrange(3) == 1 for _ in range(rng.randrange(1, 16))]
            mask[rng.randrange(len(mask))] = True
            tape = Tape.from_packets(packets)
            experiment = Experiment(tape, PeriodicReady(tuple(mask)), rng.randrange(1,5), rng.randrange(9))
            with self.subTest(sample=experiment_id):
                controls, expected = scheduled_model(experiment)
                run, = extract_runs(controls, expected)
                check = experiment.verify(run)
                self.assertEqual(check["complete_passes"], experiment.repeats)

    def test_timing_audit_distinguishes_lateness_from_edited_transcript(self):
        """A bus transfer can have correct bytes and due tick but wrong offer timing."""
        case = benchmark_fixture("dense", 19)
        controls, samples = scheduled_model(case)
        run, = extract_runs(controls, samples)
        index = next(i for i,item in enumerate(run.deliveries) if item.offered < item.accepted)
        row = run.deliveries[index]
        run.deliveries[index] = replace(row, offered=row.offered+1)
        with self.assertRaisesRegex(ReplayError, "analytic check"):
            case.verify(run)


if __name__ == "__main__":
    unittest.main()
