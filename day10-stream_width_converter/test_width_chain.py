"""Check cascade policies, independently observed histories, and strict input/output artifacts."""

from dataclasses import replace
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
import unittest

from width_chain import (
    Audit, ChainError, Dimensions, InputEdge, Observation, PacketWatch,
    Plan, Stage, _hex_row, _parse_sample, _split, compare_routes, distribution,
    make_schedule, read_samples, report_markdown,
    sample_plan, verify, write_schedule,
)
from width_model import Beat, Report, Status
from width_recipe import Packet, Window


def packet(payload, width, *, tag=9, start=0, gap=0, pad=255, edits=()):
    """Build a bounded timed fixture in terms of bytes and explicit bus faults."""
    return Packet.parse({
        "tag": tag,
        "payload": payload.hex(),
        "at": start,
        "interbeat": gap,
        "pad": pad,
        "edits": list(edits),
    }, width)


def capture(plan):
    """Build a full reference observation sequence solely for software audit tests."""
    generated = tuple(make_schedule(plan))
    return tuple(item[0] for item in generated), tuple(item[1] for item in generated)


def lengths(summary):
    """Return packet and byte totals per observation boundary."""
    return [(stage["packets_out"], stage["bytes_out"]) for stage in summary["stages"]]


class GeometryAndPlans(unittest.TestCase):
    def test_all_port_widths_and_modes_map_to_exact_parameters(self):
        """Match the host topology to each named elaboration parameter."""
        geometry = Dimensions.create((3, 7, 2, 9), ("streaming", "validated", "streaming"),
                                     (11, 551, 15))
        self.assertEqual(geometry.widths, (3, 7, 2, 9))
        self.assertEqual(geometry.parameters(), {
            "W0": 3, "W1": 7, "W2": 2, "W3": 9,
            "D0": 11, "D1": 551, "D2": 15,
            "M0": 1, "M1": 0, "M2": 1,
        })

    def test_bad_dimensions_are_rejected_before_elaboration(self):
        """Reject missing stages and illegal counts before opening a tool."""
        valid = Stage(3, 5, 32, "validated")
        with self.assertRaisesRegex(ChainError, "three conversion"):
            Dimensions((valid,))
        with self.assertRaisesRegex(ChainError, "successor"):
            Dimensions((valid, valid, valid))
        with self.assertRaisesRegex(ChainError, "four widths"):
            Dimensions.create((3, 3), ("validated",), (45,))
        for width in (0, 17, True):
            with self.subTest(width=width), self.assertRaises(ValueError):
                Stage(width, 3, 80, "validated")
        with self.assertRaisesRegex(ChainError, "accommodate both"):
            Stage(8, 8, 15, "streaming")
        with self.assertRaisesRegex(ChainError, "unknown conversion"):
            Stage(8, 8, 16, "buffered")

    def test_plan_validation_applies_trusted_input_contract(self):
        """Reject a sparse or tag-changing source before a cut-through stage."""
        dims = Dimensions.create((4, 5, 2, 4), ("streaming",) * 3, (9, 8, 8))
        sparse = packet(bytes(range(7)), 4, edits=({"beat": 1, "keep": 5},))
        tag = packet(bytes(range(7)), 4, edits=({"beat": 1, "tag": 1},))
        for broken in (sparse, tag):
            with self.subTest(broken=broken), self.assertRaisesRegex(ChainError, "malformed"):
                Plan(dims, (broken,)).check()
        good = packet(bytes(range(12)), 4)
        self.assertEqual(Plan(dims, (good,)).check().packets, (good,))

    def test_json_shape_and_nested_failure_context(self):
        """Read names consistently and localize a bad packet or report schedule."""
        case = {
            "widths": [4, 8, 2, 7],
            "modes": ["validated", "streaming", "validated"],
            "depths": [128, 14, 20],
            "packets": [{"tag": 13, "payload": "ab 40 c1", "at": 9}],
            "sink_stalls": [[2, 6], [12, 13]],
            "report_stalls": [[], [], [[1, 140]]],
        }
        resolved = Plan.parse(case)
        self.assertEqual(resolved.geometry.input_width, 4)
        self.assertEqual(resolved.packets[0].bytes_in_order, bytes((171, 64, 193)))
        self.assertEqual(resolved.report_stalls[2], (Window(1, 140),))
        with self.assertRaisesRegex(ValueError, "packet 0"):
            Plan.parse(dict(case, packets=[{"tag": 0, "payload": "<not hex>"}]))
        with self.assertRaisesRegex(ValueError, "one entry"):
            Plan.parse(dict(case, report_stalls=[[]]))
        with self.assertRaisesRegex(ValueError, "overlap"):
            Plan.parse(dict(case, sink_stalls=[[1, 6], [5, 8]]))
        with self.assertRaisesRegex(ValueError, "unrecognized fields"):
            Plan.parse(dict(case, out_stall=[[1, 2]]))

    def test_source_arrivals_and_limit_have_clear_boundaries(self):
        """Require monotonic source dates and drain within the caller's edge budget."""
        dims = Dimensions.create((4, 8, 2, 7), ("streaming",) * 3, (12, 10, 11))
        first = packet(b"alpha", 4, start=100)
        earlier = packet(b"lower", 4, start=80)
        with self.assertRaisesRegex(ChainError, "sorted"):
            Plan(dims, (first, earlier)).check()
        planned = Plan(dims, (first,), sink_stalls=(Window(100, 240),))
        with self.assertRaisesRegex(ChainError, "did not drain"):
            tuple(make_schedule(planned, limit=200))
        with self.assertRaisesRegex(ChainError, "edge limit"):
            tuple(make_schedule(planned, limit=None))
        observed = verify(planned, (view for _, view in make_schedule(planned, limit=300)))
        self.assertEqual(observed["input_packets"], 1)
        self.assertGreaterEqual(observed["cycles"], 241)

    def test_empty_plan_ends_after_the_initial_reset(self):
        """Record one reset when no finite packet work was requested."""
        dims = Dimensions.create((3, 3, 3, 3), ("streaming",) * 3, (6, 6, 6))
        plan = Plan(dims, ())
        only = tuple(make_schedule(plan))
        self.assertEqual(len(only), 2)
        self.assertTrue(only[0][0].reset)
        self.assertEqual(verify(plan, (row[1] for row in only))["input_packets"], 0)

    def test_two_types_of_stalls_are_independently_configured(self):
        """Observe report stalls at the validating boundary without conflating sink traffic."""
        dims = Dimensions.create((4, 8, 2, 7), ("validated", "streaming", "validated"),
                                 (80, 10, 60))
        packets = (packet(b"tiny", 4), packet(b"followup", 4, tag=6))
        plan = Plan(dims, packets, report_stalls=((Window(1, 30),), (), ()))
        report = verify(plan, (row[1] for row in make_schedule(plan)))
        self.assertEqual(report["stages"][0]["report_stall_cycles"], 28)
        self.assertEqual(report["stages"][1]["report_stall_cycles"], 0)
        self.assertEqual(lengths(report), [(2, 12), (2, 12), (2, 12)])

    def test_pathological_padded_inputs_are_cleaned_at_each_stage(self):
        """Check rounding and byte order across sixteen, one, seven, and nine lanes."""
        dims = Dimensions.create((16, 1, 7, 9), ("validated", "streaming", "validated"),
                                 (68, 12, 68))
        payload = bytes(range(65))
        plan = Plan(dims, (packet(payload, 16, pad=0x99),))
        report = verify(plan, (row[1] for row in make_schedule(plan)))
        self.assertEqual(lengths(report), [(1, 65)] * 3)
        self.assertTrue(all(stage["latency"][0]["bytes"] == 65 for stage in report["stages"]))


class IndependentConservation(unittest.TestCase):
    def setUp(self):
        """Choose a topology that can reject packets at two separate boundaries."""
        self.dimensions = Dimensions.create(
            (4, 8, 3, 4), ("validated", "streaming", "validated"), (36, 18, 21)
        )

    def plan(self):
        """Exercise malformed input, downstream length rejection, and retained duplicate tags."""
        rows = (
            packet(bytes(range(16)), 4, tag=4),
            packet(bytes(range(8)), 4, tag=4, edits=({"beat": 1, "tag": 5},)),
            packet(bytes(range(30)), 4, tag=4),
            packet(bytes(range(48)), 4, tag=4),
            packet(bytes(range(6)), 4, tag=4),
        )
        return Plan(self.dimensions, rows, (Window(1, 65),), ((), (), (Window(6, 36),)))

    def test_each_validation_boundary_drops_only_its_bad_packets(self):
        """Check an explicit independently calculated survivor count at every link."""
        plan = self.plan()
        _, actual = capture(plan)
        report = verify(plan, actual)
        self.assertEqual(report["input_packets"], 5)
        self.assertEqual(lengths(report), [(3, 52), (3, 52), (2, 22)])
        first, middle, last = report["stages"]
        self.assertEqual(first["reports"], {"OK": 3, "BAD_TAG": 1, "TOO_LONG": 1})
        self.assertEqual(middle["reports"], {})
        self.assertEqual(last["reports"], {"OK": 2, "TOO_LONG": 1})
        self.assertEqual([row["packet"] for row in last["latency"]], [0, 4])
        self.assertEqual([row["tag"] for row in last["latency"]], [4, 4])
        self.assertGreater(middle["byte_occupancy_peak"], 0)

    def test_pure_audit_detects_exact_first_byte_mutation(self):
        """Catch semantic corruption without relying on model-generated edge equivalence."""
        plan = self.plan()
        traces, samples = capture(plan)
        audit = Audit(plan.dimensions)
        with self.assertRaisesRegex(ChainError, "packet 0 changed"):
            for drive, row in zip(traces, samples):
                offered = row[-1].output
                if offered and offered.last and drive.sink_ready:
                    row = (*row[:-1], replace(row[-1], output=replace(offered, data=offered.data ^ 1)))
                audit.step(drive, row)

    def test_pure_audit_detects_report_before_ingress(self):
        """Reject a diagnostic that appeared without the corresponding frame."""
        audit = Audit(self.dimensions)
        blank = Observation(False, None, None, 0)
        audit.step(InputEdge(reset=True), (blank,) * 3)
        extra = replace(blank, report=Report(8, 12, Status.OK))
        with self.assertRaisesRegex(ChainError, "stage 0: extra report"):
            audit.step(InputEdge(), (extra, blank, blank))

    def test_pure_audit_detects_unaccounted_streaming_occupancy(self):
        """Check bytes in flight using only accepted and observed physical beats."""
        plan = self.plan()
        traces, samples = capture(plan)
        audit = Audit(plan.dimensions)
        corrupted = False
        with self.assertRaisesRegex(ChainError, "occupancy differs"):
            for cycle, (drive, row) in enumerate(zip(traces, samples)):
                if row[1].occupancy > 0 and cycle > 1 and not corrupted:
                    row = (row[0], replace(row[1], occupancy=row[1].occupancy - 1), row[2])
                    corrupted = True
                audit.step(drive, row)
        self.assertTrue(corrupted)

    def test_plateau_stalls_keep_the_final_beat_exact(self):
        """Detect a changed or withdrawn output that the sink already observed stalled."""
        plan = self.plan()
        traces, samples = capture(plan)
        audit = Audit(plan.dimensions)
        for index, (drive, row) in enumerate(zip(traces, samples)):
            audit.step(drive, row)
            if row[-1].output is not None and not drive.sink_ready:
                flush = (*samples[index+1][:-1], replace(samples[index+1][-1], output=None))
                with self.assertRaisesRegex(ChainError, "stage 2: held output changed"):
                    audit.step(traces[index+1], flush)
                break
        else:
            self.fail("fixture did not stall a visible output")

    def test_stage_observations_cannot_hide_extra_or_premature_records(self):
        """Compare exact cycle count and width-specific interface state."""
        plan = self.plan()
        _, samples = capture(plan)
        with self.assertRaisesRegex(ChainError, "trace and schedule lengths differ"):
            verify(plan, samples[:-1])
        shorter = (samples[0][:-1], *samples[1:])
        with self.assertRaisesRegex(ChainError, "stage samples"):
            verify(plan, shorter)
        fake_edge = (replace(samples[0][0], source_ready=True), *samples[0][1:])
        with self.assertRaisesRegex(ChainError, "edge 0 stage 0"):
            verify(plan, (fake_edge, *samples[1:]))

    def test_reconstruction_of_canonical_words_is_explicit(self):
        """Check exact representation of narrow, irregular, and full final beats."""
        source = bytes((9, 11, 29, 55, 222))
        expected = (
            Beat(9 | (11 << 8) | (29 << 16), 7, False, 17),
            Beat(55 | (222 << 8), 3, True, 17),
        )
        self.assertEqual(tuple(_split(source, 3, 17)), expected)
        parser = PacketWatch(3)
        self.assertIsNone(parser.accept(expected[0], 4))
        summary, actual = parser.accept(expected[1], 9)
        self.assertEqual(actual, expected)
        self.assertEqual(summary.payload, source)
        self.assertEqual((summary.first, summary.last, summary.active_cycles), (4, 9, 6))

    def test_randomized_geometry_and_capacity_rules(self):
        """Check that arbitrary valid geometry preserves every surviving packet's bytes."""
        rng = Random(31674)
        for trial in range(12):
            widths = tuple(rng.randint(1, 16) for _ in range(4))
            modes = tuple(rng.choice(("validated", "streaming")) for _ in range(3))
            depths = [
                rng.randint(5, 39) if mode == "validated" else widths[index] + widths[index+1] + 1
                for index, mode in enumerate(modes)
            ]
            dims = Dimensions.create(widths, modes, depths)
            payloads = [bytes(rng.randrange(256) for _ in range(rng.randint(1, 40))) for _ in range(12)]
            plan = Plan(dims, tuple(packet(value, widths[0], tag=index % 2)
                                    for index, value in enumerate(payloads)),
                        (Window(8, 26),))
            report = verify(plan, (view for drive, view in make_schedule(plan)))
            survivors = payloads
            expected = []
            for mode, depth in zip(modes, depths):
                if mode == "validated":
                    survivors = [value for value in survivors if len(value) <= depth]
                expected.append((len(survivors), sum(map(len, survivors))))
            self.assertEqual(lengths(report), expected, trial)


class TimelineReports(unittest.TestCase):
    def setUp(self):
        """Build repeated tags, an ordinary delivery, and one downstream capacity drop."""
        self.data = [
            packet(bytes(range(7)), 4, tag=11),
            packet(bytes(range(22)), 4, tag=11),
            packet(bytes(range(6)), 4, tag=11),
        ]
        self.geometry = Dimensions.create((4, 8, 5, 9), ("streaming", "validated", "streaming"),
                                          (12, 12, 14))

    def run_plan(self, dimensions, stalls=()):
        """Capture a nonoverlapping source timeline under a particular physical layout."""
        plan = Plan(dimensions, tuple(self.data), stalls)
        return verify(plan, (view for drive, view in make_schedule(plan)))

    def test_repeated_tags_keep_distinct_ordered_fates(self):
        """Show CRCs and physical beats on every surviving branch with one dropped route."""
        measured = self.run_plan(self.geometry)
        entries = measured["packet_routes"]
        self.assertEqual([entry["packet"] for entry in entries], [0, 1, 2])
        self.assertEqual([entry["tag"] for entry in entries], [11, 11, 11])
        self.assertEqual([entry["delivered"] for entry in entries], [True, False, True])
        self.assertEqual(
            [node["disposition"] for node in entries[1]["route"]], ["OK", "TOO_LONG", "UNREACHED"]
        )
        last = entries[0]["route"][-1]["output"]
        self.assertEqual(last["crc32"], entries[0]["source"]["crc32"])
        self.assertEqual(last["bytes"], 7)
        self.assertEqual(last["beats"], 1)
        self.assertEqual(entries[0]["source"]["beats"], 2)
        self.assertIsNone(entries[1]["route"][0]["report_edge"])
        self.assertGreater(
            entries[1]["route"][1]["report_edge"], entries[1]["route"][0]["output"]["last"]
        )
        self.assertEqual(measured["end_to_end"]["count"], 2)

    def test_latency_and_markdown_have_known_empty_and_nonempty_shape(self):
        """Distinguish absent measurements and include a bounded sample of actual packets."""
        self.assertEqual(distribution([8, 1, 2]), {"count": 3, "min": 1, "p50": 2, "p95": 8, "max": 8})
        self.assertEqual(distribution([]), {"count": 0, "min": None, "p50": None, "p95": None, "max": None})
        measured = self.run_plan(self.geometry)
        rendered = report_markdown(measured, rows=2)
        self.assertIn("Delivered 2 end-to-end packets", rendered)
        self.assertIn("| 1 | validated | 8 → 5 | 2 | 13 |", rendered)
        self.assertIn("| 1 | 11 | 22 | OK | TOO_LONG | UNREACHED | rejected |", rendered)
        self.assertIn("other 1 histories", rendered)
        self.assertNotIn("Packet examples", report_markdown(measured, rows=0))
        with self.assertRaisesRegex(ChainError, "nonnegative"):
            report_markdown(measured, rows=-1)

    def test_compare_same_fates_across_incommensurate_layouts(self):
        """Compare content independently of changed internal widths and finite sink stalls."""
        other = Dimensions.create((4, 5, 2, 3), ("validated", "streaming", "streaming"),
                                  (12, 9, 6))
        baseline = self.run_plan(self.geometry)
        candidate = self.run_plan(other, (Window(2, 60),))
        parity = compare_routes(baseline, candidate)
        self.assertTrue(parity["equivalent"])
        self.assertEqual(parity["differences"], [])
        self.assertEqual(parity["paired_latency_delta"]["count"], 2)
        self.assertGreater(parity["paired_latency_delta"]["max"], 0)
        self.assertNotEqual(baseline["packet_routes"][0]["route"][0]["output"]["beats"],
                            candidate["packet_routes"][0]["route"][0]["output"]["beats"])

    def test_finish_rejects_report_at_the_ingress_edge(self):
        """Require validating reports to originate after terminal ingress, even if tag is correct."""
        source = packet(b"small", 4)
        dimensions = Dimensions.create(
            (4, 8, 5, 9), ("validated", "streaming", "streaming"), (32, 13, 14)
        )
        schedule = Plan(dimensions, (source,))
        drives, observations = capture(schedule)
        early_cycle = next(
            cycle for cycle, (drive, visible) in enumerate(zip(drives, observations))
            if drive.offered is not None and drive.offered.last and visible[0].source_ready
        )
        next_cycle = early_cycle + 1
        actual = observations[next_cycle][0].report
        self.assertEqual(actual, Report(9, 5, Status.OK))
        monitor = Audit(dimensions)
        for index, (drive, row) in enumerate(zip(drives, observations)):
            if index == early_cycle:
                row = (replace(row[0], report=actual), *row[1:])
            elif index == next_cycle:
                row = (replace(row[0], report=None), *row[1:])
            monitor.step(drive, row)
        with self.assertRaisesRegex(ChainError, "noncausal report at stage 0"):
            monitor.finish()

    def test_hold_violation_on_report_has_its_own_diagnostic(self):
        """Changing a blocked diagnostic is illegal independently of output data movement."""
        schedule = Plan(
            self.geometry, tuple(self.data),
            report_stalls=((), (Window(2, 65),), ()),
        )
        drives, seen = capture(schedule)
        first = next(
            index for index, (drive, row) in enumerate(zip(drives, seen))
            if row[1].report is not None and not drive.report_ready[1]
        )
        watcher = Audit(self.geometry)
        for drive, observed in zip(drives[:first+1], seen[:first+1]):
            watcher.step(drive, observed)
        later = seen[first+1]
        changed = replace(later[1].report, length=later[1].report.length + 1)
        altered = (later[0], replace(later[1], report=changed), later[2])
        with self.assertRaisesRegex(ChainError, "held report changed"):
            watcher.step(drives[first+1], altered)

    def test_zero_delivery_metrics_remain_empty(self):
        """Distinguish no surviving packet from an impossible zero-cycle delivery."""
        strict = Dimensions.create((4, 8, 5, 9), ("validated", "streaming", "streaming"),
                                   (3, 13, 14))
        report = self.run_plan(strict)
        self.assertEqual(report["input_packets"], 3)
        self.assertEqual(lengths(report), [(0, 0)] * 3)
        self.assertEqual(report["end_to_end"],
                         {"count": 0, "min": None, "p50": None, "p95": None, "max": None})
        self.assertIn("p95 last-byte delay — cycles", report_markdown(report))
        extra = compare_routes(report, report)
        self.assertTrue(extra["equivalent"])
        self.assertIsNone(extra["paired_latency_delta"]["p50"])

    def test_compare_detects_different_survival_and_different_input(self):
        """Separate a known capacity-policy change from a different test input."""
        all_stream = Dimensions.create((4, 5, 2, 3), ("streaming",) * 3, (9, 7, 5))
        baseline = self.run_plan(self.geometry)
        candidate = self.run_plan(all_stream)
        difference = compare_routes(baseline, candidate)
        self.assertFalse(difference["equivalent"])
        row = difference["differences"][0]
        self.assertEqual(row["packet"], 1)
        self.assertEqual(row["baseline"], ("rejected", "TOO_LONG"))
        self.assertEqual(row["candidate"][0], "delivered")
        self.assertEqual(row["candidate"][1][0], 22)
        original = self.data[0]
        self.data[0] = packet(b"changed", 4, tag=11)
        with self.assertRaisesRegex(ChainError, "different input packet 0"):
            compare_routes(baseline, self.run_plan(all_stream))
        self.data[0] = original
        del self.data[-1]
        with self.assertRaisesRegex(ChainError, "different numbers"):
            compare_routes(candidate, self.run_plan(all_stream))


class TraceFiles(unittest.TestCase):
    def setUp(self):
        """Use a compact trusted cascade for deliberate capture corruption."""
        self.plan = sample_plan("trusted", packets=4)
        self.drives, self.samples = capture(self.plan)

    def test_file_roundtrip_keeps_all_interfaces_and_edges(self):
        """Read stage captures exactly as written by the tape generator."""
        with TemporaryDirectory() as folder:
            path = Path(folder)
            count = write_schedule(path, zip(self.drives, self.samples))
            rows = tuple(read_samples(path / "expected.txt", self.plan.dimensions))
            self.assertEqual(count, len(self.drives))
            self.assertEqual(rows, self.samples)
            self.assertEqual(verify(self.plan, rows)["input_packets"], 4)
            stimulus = (path / "drive.txt").read_text().splitlines()
            self.assertEqual(stimulus[0], _hex_row(self.drives[0].words()))
            self.assertTrue(all(len(line.split()) == 10 for line in stimulus))

    def test_reader_bad_row_has_exact_line_and_cause(self):
        """Keep malformed columns and skipped cycles distinguishable."""
        blank = _hex_row((0, *([0] * 33)))
        cases = (
            (blank + "\n0\n", "field count"),
            (blank + "\n" + blank + "\n", "expected edge 1"),
            (blank + "\nx\n", "invalid literal"),
        )
        for text, detail in cases:
            with self.subTest(detail=detail), TemporaryDirectory() as folder:
                path = Path(folder) / "bad.txt"
                path.write_text(text)
                with self.assertRaisesRegex(ChainError, f"bad.txt:2: .*{detail}"):
                    tuple(read_samples(path, self.plan.dimensions))

    def test_reader_checks_invalid_payloads_and_occupancy(self):
        """Reject traces inconsistent with valid bits or configured physical space."""
        inactive = [0] * 34
        for index, value, detail in (
            (4, 1, "invalid output"),
            (8, 1, "invalid report"),
            (11, 12, "occupancy"),
            (2, 2, "Boolean"),
        ):
            corrupt = list(inactive)
            corrupt[index] = value
            with self.subTest(detail=detail), self.assertRaisesRegex(ValueError, detail):
                _parse_sample(corrupt, self.plan.dimensions)


if __name__ == "__main__":
    unittest.main()
