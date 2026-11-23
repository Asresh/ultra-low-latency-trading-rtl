"""Check scheduled arrivals, exact captures, and fault diagnostics."""

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from limiter_capture import CaptureAudit, CaptureMismatch, Transfer, audit_directory, prepare
from limiter_metrics import (
    TIMELINE_FIELDS, Interval, filter_transfers, histogram_record, nearest_rank,
    summarize_keys, write_timeline,
)
from limiter_model import CycleModel
from limiter_protocol import Command, Drive, Manifest, Observation, Operation, PROFILES, Status, write_rows
from limiter_scenarios import boundary_cases, parallel_burst, random_traffic
from limiter_workload import (
    Arrival, SinkSchedule, Stall, compile_program, read_arrivals, read_stalls, sample_program,
    write_inputs,
)


class WorkloadFilesTest(unittest.TestCase):
    """Load portable schedules and explain malformed input at the correct location."""

    def setUp(self):
        """Allocate a fresh test directory and a compact hardware geometry."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.geo = PROFILES["small"]
        self.requests = self.folder / "workload.csv"
        self.stalls = self.folder / "stalls.csv"

    def test_fixture_round_trips_every_profile(self):
        """Read back the layout exported by the documented sample command."""
        for name, geometry in PROFILES.items():
            with self.subTest(profile=name):
                arrivals, stalls = sample_program(geometry)
                write_inputs(self.folder, arrivals, stalls)
                self.assertEqual(read_arrivals(self.requests, geometry), arrivals)
                self.assertEqual(read_stalls(self.stalls), stalls)
                self.assertEqual(read_stalls(None), [])

    def test_explicit_decimal_defaults_leave_hardware_validation_available(self):
        """Accept a zero-cost offer to exercise the DUT's own field validation."""
        self.requests.write_text(
            "cycle,action,key,tick,amount,rate,enable,tag\n2,offer,0,7,,,,8\n"
        )
        arrivals = read_arrivals(self.requests, self.geo)
        self.assertEqual(arrivals, [Arrival(2, Command.offer(0, 7, 0, 8))])
        program = compile_program(self.geo, arrivals, [])
        model = CycleModel(self.geo)
        responses = [model.step(drive).response for drive in program.inputs]
        self.assertIn(Status.BAD_FIELDS, [item.status for item in responses if item])

    def test_row_errors_point_to_bad_text_field(self):
        """Reject malformed decimal, command, and control data before simulation."""
        rows = (
            ("1,offer,0,0,1,0,0,", "tag"),
            ("1,offer,-1,0,1,,,", "key"),
            ("1,offer,0,0xff,1,,,", "tick"),
            ("1,interrogate,0,0,,,,", "unknown action"),
            ("1,offer,4,0,1,,,", "key"),
            ("1,reset,0,0,,,,", "empty command fields"),
            ("1,inspect,0,0,,,2,", "enable"),
            ('1,"offer,0,0,1,,,', "unexpected end"),
        )
        for row, expected in rows:
            # An omitted tag is a valid default for a compact source file.
            if expected == "tag":
                row = row + "-1"
            self.requests.write_text("cycle,action,key,tick,amount,rate,enable,tag\n" + row + "\n")
            with self.subTest(row=row):
                with self.assertRaisesRegex(ValueError, f"workload.csv:2:.*{expected}"):
                    read_arrivals(self.requests, self.geo)

    def test_parser_rejects_ambiguous_and_extra_columns(self):
        """Fail early on a wrong header or a row not matching the documented width."""
        cases = (
            ("cycle,action,key,tick,amount,rate,tag,enable\n", "header"),
            ("cycle,action,key,tick,amount,rate,enable,tag\n1,offer,0\n", "columns"),
        )
        for content, expected in cases:
            self.requests.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, expected):
                read_arrivals(self.requests, self.geo)

    def test_sink_schedule_half_open_and_sequential(self):
        """The stop cycle opens the sink immediately and intervals may abut."""
        schedule = SinkSchedule([Stall(2, 4), Stall(4, 6), Stall(9, 10)])
        got = [schedule.ready(cycle) for cycle in range(11)]
        self.assertEqual(got, [1, 1, 0, 0, 0, 0, 1, 1, 1, 0, 1])
        with self.assertRaisesRegex(ValueError, "rewind"):
            schedule.ready(3)

    def test_bad_stall_inputs_are_rejected_without_silent_merging(self):
        """An overlapping or malformed interval fails explicitly."""
        cases = (
            ("start,stop\n9,10\n2,4\n", "overlap"),
            ("start,stop\n1,3\n2,4\n", "overlap"),
            ("start,stop\n1,1\n", "start < stop"),
            ("start,stop\n1,-2\n", "stop"),
            ("begin,end\n1,3\n", "header"),
        )
        for data, pattern in cases:
            self.stalls.write_text(data)
            with self.assertRaisesRegex(ValueError, pattern):
                read_stalls(self.stalls)


class ProgramSchedulingTest(unittest.TestCase):
    """Exercise queue admission, backpressure, reset, and completion semantics."""

    def setUp(self):
        """Choose a 3-key geometry with modest field widths."""
        self.geo = PROFILES["small"]

    def serve(self, program):
        """Compare the schedule to the cycle oracle and return delivered commands."""
        audit = CaptureAudit(self.geo)
        oracle = CycleModel(self.geo)
        for drive in program.inputs:
            audit.check_cycle(drive, oracle.step(drive))
        return audit.finish(), audit.delivered

    def test_same_cycle_arrivals_keep_source_file_order(self):
        """Drain co-arrivals in recorded priority order while ready starts low."""
        arrivals = [
            Arrival(0, Command.configure(0, 0, 7, 1, 1)),
            Arrival(0, Command.offer(0, 0, 7, 2)),
            Arrival(0, Command.offer(0, 0, 1, 3)),
        ]
        program = compile_program(self.geo, arrivals, [Stall(0, 3)])
        report, delivered = self.serve(program)
        self.assertEqual(program.submitted, 3)
        self.assertEqual(program.peak_queue, 3)
        self.assertEqual(program.accepted, 3)
        self.assertEqual(report.input_stalled, 2)
        self.assertEqual([item.command.tag for item in delivered], [1, 2, 3])
        self.assertEqual([item.response.status for item in delivered], [Status.OK, Status.PASS, Status.LIMITED])
        self.assertEqual([item.accepted for item in delivered], [0, 3, 4])
        self.assertEqual([item.delivered for item in delivered], [3, 4, 5])

    def test_reset_cancels_source_pending_and_hardware_pending_separately(self):
        """Count which requests never handshook and which accepted responses were cancelled."""
        arrivals, stalls = sample_program(self.geo)
        program = compile_program(self.geo, arrivals, stalls)
        report, delivered = self.serve(program)
        self.assertEqual(program.submitted, 9)
        self.assertEqual(program.accepted, 7)
        self.assertEqual(report.discarded, 1)
        self.assertEqual(len(program.dropped), 2)
        self.assertEqual([dropped.reset for dropped in program.dropped], [10, 10])
        self.assertEqual([dropped.queued for dropped in program.dropped], [5, 6])
        self.assertEqual(report.transfers + report.discarded, program.accepted)
        self.assertEqual(len(delivered), 6)
        self.assertEqual(program.to_record()["cycles"], len(program.inputs))

    def test_cycle_and_queue_limits_are_checked_before_large_replays(self):
        """Reject oversize backlogs and unbounded runs without partial output."""
        requests = [Arrival(0, Command.inspect(0, 0, tag)) for tag in range(4)]
        errors = (
            (requests, [], {"max_queue": 3}, "queue exceeded"),
            ([Arrival(6)], [], {"max_cycles": 5}, "last arrival"),
            (requests, [Stall(1, 9)], {"max_cycles": 4}, "sink stall"),
            (requests, [], {"max_queue": 0}, "positive integer"),
            (requests, [], {"max_cycles": 1}, "reset and drain"),
            (requests, [Stall(0, 6)], {"max_cycles": 6}, "did not drain"),
        )
        for program, schedule, settings, error in errors:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    compile_program(self.geo, program, schedule, **settings)

    def test_ordering_and_reset_conflicts_are_explicit(self):
        """Avoid silently rewinding producer time or choosing a reset priority."""
        command = Command.inspect(0, 0)
        cases = (
            ([Arrival(4), Arrival(3)], "nondecreasing"),
            ([Arrival(4, command), Arrival(4)], "only arrival"),
            ([Arrival(4), Arrival(4, command)], "only arrival"),
            ([Arrival(-1)], "nonnegative"),
        )
        for workload, diagnostic in cases:
            with self.assertRaisesRegex(ValueError, diagnostic):
                compile_program(self.geo, workload, [])

    def test_empty_workload_and_isolated_reset_terminate(self):
        """Every plan ends with a known empty response slot."""
        empty = compile_program(self.geo, [], [])
        self.assertEqual(len(empty.inputs), 1)
        self.assertEqual(empty.accepted, 0)
        plan = compile_program(self.geo, [Arrival(2)], [])
        self.assertEqual(len(plan.inputs), 4)
        self.assertEqual([index for index, edge in enumerate(plan.inputs) if edge.reset], [2])
        self.assertEqual(self.serve(plan)[0].transfers, 0)


class CaptureAuditTest(unittest.TestCase):
    """Reconstruct the right decision queue and report exact corrupted cycles."""

    def setUp(self):
        """Write a trusted capture from the reference for controlled corruption."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.geo = PROFILES["small"]
        self.plan = boundary_cases(self.geo)
        self.manifest = Manifest("small", "boundary", 917, len(self.plan.inputs), self.geo)
        self.capture = self.path / "capture.hex"
        prepare(self.path, self.manifest, self.plan.inputs)
        self.write_samples(self.plan.expected)

    def write_samples(self, samples):
        """Replace the pinned capture without changing its accepted input schedule."""
        write_rows(self.capture, (sample.words() for sample in samples))

    def test_complete_reference_capture_reports_all_statuses(self):
        """Correlate restored request identities with all modeled dispositions."""
        meta, report, transfers = audit_directory(self.path)
        self.assertEqual(meta, self.manifest)
        self.assertEqual(report.cycles, len(self.plan.inputs))
        self.assertEqual(set(report.statuses), set(Status))
        self.assertGreater(report.output_stalled, 0)
        self.assertGreater(report.input_stalled, 0)
        self.assertGreater(report.replaced, 0)
        self.assertEqual(report.discarded, 1)
        self.assertEqual(len(transfers), report.transfers)
        self.assertTrue(all(item.latency >= 1 for item in transfers))
        self.assertEqual(report.to_record()["requests"], report.transfers + 1)

    def test_capture_length_mismatch_labeled_as_missing_or_extra(self):
        """Explain off-by-one files before consuming an unpaired result."""
        variants = ((self.plan.expected[:-1], "missing capture row"),
                    ([*self.plan.expected, self.plan.expected[-1]], "unexpected capture row"))
        for rows, diagnostic in variants:
            with self.subTest(diagnostic=diagnostic):
                self.write_samples(rows)
                with self.assertRaisesRegex(CaptureMismatch, diagnostic):
                    audit_directory(self.path)

    def test_corrupted_metadata_in_result_is_reported_by_name(self):
        """A wrong tag cannot be mistaken for another command's decision."""
        samples = list(self.plan.expected)
        index = next(i for i, sample in enumerate(samples) if sample.output_valid)
        reference = samples[index].response
        samples[index] = replace(samples[index], response=replace(reference, tag=reference.tag ^ 1))
        self.write_samples(samples)
        with self.assertRaisesRegex(CaptureMismatch, f"cycle {index}: tag expected"):
            audit_directory(self.path)

    def test_bad_input_ready_is_detected_before_payload_matching(self):
        """Pin timing is part of the contract even on idle cycles."""
        samples = list(self.plan.expected)
        samples[0] = replace(samples[0], input_ready=1)
        self.write_samples(samples)
        with self.assertRaisesRegex(CaptureMismatch, "expected s_ready"):
            audit_directory(self.path)

    def test_stalled_source_changes_are_checked_independently(self):
        """The audit can identify a broken stimulus vector, not just a wrong RTL output."""
        changed = list(self.plan.inputs)
        index = next(
            i for i, (drive, sample) in enumerate(zip(changed, self.plan.expected))
            if drive.valid and not sample.input_ready
        )
        original = changed[index + 1].command
        changed[index + 1] = replace(changed[index + 1], command=replace(original, tag=original.tag ^ 1))
        prepare(self.path, self.manifest, changed)
        with self.assertRaisesRegex(CaptureMismatch, "source changed a stalled request"):
            audit_directory(self.path)

    def test_wrong_cycle_count_and_nonzero_invalid_payload(self):
        """Reject trace-level inconsistencies even if no actual transaction changed."""
        replace(self.manifest, cycles=self.manifest.cycles + 1).write(self.path / "manifest.json")
        with self.assertRaisesRegex(CaptureMismatch, "manifest expected"):
            audit_directory(self.path)
        self.manifest.write(self.path / "manifest.json")
        samples = [list(sample.words()) for sample in self.plan.expected]
        samples[0][-1] = 1
        write_rows(self.capture, samples)
        with self.assertRaisesRegex(CaptureMismatch, "zero payload"):
            audit_directory(self.path)


class TimelineMetricsTest(unittest.TestCase):
    """Measure audited results rather than inferred throughput or source arrivals."""

    def record(self, schedule):
        """Convert a schedule into its checked historical transfer sequence."""
        capture = CaptureAudit(schedule.geometry)
        for drive, sample in zip(schedule.inputs, schedule.expected):
            capture.check_cycle(drive, sample)
        return capture.finish(), capture.delivered

    def test_latency_bins_and_percentiles_use_integer_nearest_rank(self):
        """Check ties, empty histograms, unsorted bins, and exact percentile bounds."""
        counter = {5: 9, 1: 4, 2: 5, 100: 0, 9: 2}
        self.assertEqual(nearest_rank(counter, 1, 2), 5)
        self.assertEqual(nearest_rank(counter, 9, 20), 2)
        self.assertEqual(nearest_rank(counter, 19, 20), 9)
        self.assertEqual(histogram_record(counter)["max"], 9)
        self.assertEqual(histogram_record({})["samples"], 0)
        self.assertIsNone(nearest_rank({}, 1, 2))
        with self.assertRaisesRegex(ValueError, "percentile"):
            nearest_rank(counter, 0, 1)
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            nearest_rank({1: -1}, 1, 2)

    def test_cycle_filter_switches_delivered_and_issue_intervals(self):
        """A stalled request may issue inside one window and transfer outside it."""
        _, transfers = self.record(boundary_cases(PROFILES["small"]))
        delayed = next(record for record in transfers if record.latency > 1)
        interval = Interval(delayed.accepted, delayed.accepted + 1)
        issues = list(filter_transfers(transfers, interval=interval, by_issue=True))
        results = list(filter_transfers(transfers, interval=interval))
        self.assertIn(delayed, issues)
        self.assertNotIn(delayed, results)
        selected = list(filter_transfers(transfers, status=Status.LIMITED, key=0))
        self.assertTrue(selected)
        self.assertTrue(all(row.response.status == Status.LIMITED for row in selected))
        self.assertTrue(all(row.command.key == 0 for row in selected))
        self.assertEqual(list(filter_transfers(transfers, interval=Interval(2, 2))), [])

    def test_key_totals_equal_exact_pass_and_limit_costs(self):
        """Do not use clipped wire counters to sum a complete audit tape."""
        _, transfers = self.record(random_traffic(PROFILES["small"], 34, 300))
        reports = summarize_keys(transfers)
        self.assertTrue(reports)
        for record in reports:
            related = [item for item in transfers if item.command.key == record["key"]]
            passed = [item for item in related if item.response.status == Status.PASS]
            limited = [item for item in related if item.response.status == Status.LIMITED]
            self.assertEqual(record["admitted_cost"], sum(item.command.amount for item in passed))
            self.assertEqual(
                record["requested_cost"], sum(item.command.amount for item in (*passed, *limited))
            )
            self.assertEqual(record["latency"]["samples"], len(related))
            self.assertEqual(record["first_issue"], related[0].accepted)
            self.assertEqual(record["last_delivery"], related[-1].delivered)

    def test_csv_timeline_keeps_stable_header_and_data_for_empty_filters(self):
        """Both populated and empty windows remain useful to downstream CSV readers."""
        _, records = self.record(parallel_burst(PROFILES["default"], 16))
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "timeline.csv"
            chosen = records[:3]
            write_timeline(file, chosen)
            with file.open(newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(tuple(reader.fieldnames), TIMELINE_FIELDS)
                rows = list(reader)
            self.assertEqual([int(row["tag"]) for row in rows], [item.command.tag for item in chosen])
            self.assertEqual([int(row["latency"]) for row in rows], [1, 1, 1])
            write_timeline(file, [])
            self.assertEqual(file.read_text(), ",".join(TIMELINE_FIELDS) + "\n")


if __name__ == "__main__":
    unittest.main()
