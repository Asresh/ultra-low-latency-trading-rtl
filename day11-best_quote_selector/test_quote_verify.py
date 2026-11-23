"""Check that the scoreboard rejects malformed and undercovered captures."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from quote_model import Machine
from quote_schema import Code, Drive, PROFILES, Result, Sample, State, Winner, row_text
from quote_verify import (
    Scoreboard, TraceError, compare_fields, compare_trace, latency_summary, payload_defects,
    percentile, read_drives, read_samples, save_report, write_drives,
)
from quote_workload import benchmark_tape, price_extremes_tape, stall_tape, state_tape


class TraceFaults(unittest.TestCase):
    def setUp(self):
        """Create a measured scenario with a complete matched output trace."""
        self.profile = PROFILES["default"]
        self.workload = state_tape(self.profile)
        self.drives = self.workload.drives
        machine = Machine(self.profile)
        self.samples = tuple(machine.step(row) for row in self.drives)

    def find_result(self, predicate):
        """Locate the first actual result edge satisfying a fixture predicate."""
        return next(
            index for index, item in enumerate(self.samples)
            if item.result is not None and predicate(item.result)
        )

    def alter_result(self, index, **changes):
        """Yield a trace differing at one observed bus field."""
        result = self.samples[index].result
        edited = replace(self.samples[index], result=replace(result, **changes))
        return self.samples[:index] + (edited,) + self.samples[index + 1:]

    def test_subset_and_full_trace_length(self):
        """Fail if one sequence ends early, regardless of which channel dropped data."""
        for drives, samples in (
            (self.drives[:-1], self.samples), (self.drives, self.samples[:-1]),
        ):
            with self.assertRaisesRegex(TraceError, "drive/trace length mismatch"):
                compare_trace(drives, samples, self.profile)
        with self.assertRaisesRegex(TraceError, "empty trace"):
            compare_trace([], [], self.profile)

    def test_payload_field_differences_are_precise(self):
        """Identify an altered price and sequence within nested candidate records."""
        index = self.find_result(lambda result: result.bid.valid)
        before = self.samples[index].result.bid
        changed = replace(before, price=99, sequence=91)
        actual = self.alter_result(index, bid=changed)
        with self.assertRaisesRegex(TraceError, "result.bid.price=99.*result.bid.sequence=91"):
            compare_trace(self.drives, actual, self.profile)

    def test_missing_output_valid_reported_at_first_edge(self):
        """Detect absent response even if later records appear otherwise correct."""
        index = self.find_result(lambda result: True)
        edited = replace(self.samples[index], result=None)
        trace = self.samples[:index] + (edited,) + self.samples[index + 1:]
        with self.assertRaisesRegex(TraceError, f"cycle {index}: result=None"):
            compare_trace(self.drives, trace, self.profile)

    def test_ready_bit_is_checked_even_when_source_idle(self):
        """Unexpected ready drops are protocol differences without requiring an offer."""
        index = next(index for index, drive in enumerate(self.drives)
                     if not drive.reset and drive.command is None)
        edited = replace(self.samples[index], cmd_ready=0)
        trace = self.samples[:index] + (edited,) + self.samples[index + 1:]
        with self.assertRaisesRegex(TraceError, "cmd_ready=0 expected=1"):
            compare_trace(self.drives, trace, self.profile)

    def test_pending_consumption_cannot_be_hidden_by_trimming_both_files(self):
        """Reject a synchronized truncation that still leaves one accepted query pending."""
        edge = next(index for index, item in enumerate(self.drives) if item.query is not None)
        with self.assertRaisesRegex(TraceError, "undrained trace"):
            compare_trace(self.drives[:edge + 1], self.samples[:edge + 1], self.profile)

    def test_monitor_flags_producer_stall_violations(self):
        """Mutating a stalled command is detected even if a model tolerates it."""
        stalled = stall_tape(self.profile)
        machine = Machine(self.profile)
        raw = tuple(machine.step(row) for row in stalled.drives)
        for index, drive in enumerate(stalled.drives[:-1]):
            if drive.command is not None and not raw[index].cmd_ready:
                break
        next_drive = stalled.drives[index + 1]
        changed = replace(next_drive, command=replace(next_drive.command, tag=499))
        modified = stalled.drives[:index + 1] + (changed,) + stalled.drives[index + 2:]
        new_reference = Machine(self.profile)
        altered = (new_reference.step(step) for step in modified)
        with self.assertRaisesRegex(TraceError, "stalled command payload changed"):
            compare_trace(modified, altered, self.profile)

    def test_coverage_requirements_are_enforced_separately(self):
        """Check exact hardware equality can still fail an unmet coverage obligation."""
        for update, wanted in (
            ({"required_codes": frozenset((Code.BAD_PRICE,))}, "missing error code"),
            ({"minimum_cmd_stalls": 999}, "insufficient command stalls"),
            ({"minimum_query_stalls": 999}, "insufficient query stalls"),
            ({"minimum_parallel": 999}, "insufficient simultaneous"),
            ({"exact_query_latency": 3}, "unexpected latency"),
        ):
            requirement = replace(self.workload, **update)
            with self.subTest(wanted=wanted), self.assertRaisesRegex(TraceError, wanted):
                compare_trace(self.drives, self.samples, self.profile, workload=requirement)

    def test_report_counts_and_allowable_latency(self):
        """Check a fully pipelined scenario accepts both channels on every planned edge."""
        scenario = benchmark_tape(self.profile, 30)
        machine = Machine(self.profile)
        report = compare_trace(
            scenario.drives, (machine.step(row) for row in scenario.drives),
            self.profile, workload=scenario,
        )
        self.assertEqual(report["edges"]["parallel"], 30)
        self.assertEqual(report["command_latency"], {"count": 30, "min": 1, "p50": 1, "p95": 1, "max": 1})
        self.assertEqual(report["query_latency"], {"count": 30, "min": 2, "p50": 2, "p95": 2, "max": 2})


    def test_extremes_include_exact_price_width_and_opposite_sign(self):
        """Check all geometry-specific price extrema with a signed subtraction known answer."""
        for name, geometry in PROFILES.items():
            story = price_extremes_tape(geometry)
            model = Machine(geometry)
            sampled = tuple(model.step(row) for row in story.drives)
            report = compare_trace(story.drives, sampled, geometry, name, story)
            answers = {
                row.result.tag: row.result for row in sampled if row.result is not None
            }
            max_spread = geometry.price_max - 1
            self.assertEqual(answers[0].spread, max_spread)
            self.assertEqual(answers[0].bid.qty, geometry.qty_max)
            self.assertEqual(answers[0].ask.qty, geometry.qty_max)
            self.assertEqual(answers[1].spread, 0)
            self.assertEqual(answers[1].state, State.LOCKED)
            if geometry.venues > 1:
                self.assertEqual(answers[2].spread, -max_spread)
                self.assertEqual(answers[2].bid.price, geometry.price_max)
                self.assertEqual(answers[2].ask.price, 1)
                self.assertEqual(answers[2].state, State.CROSSED)
                self.assertIn("CROSSED", report["market_states"])
            else:
                self.assertNotIn("CROSSED", report["market_states"])

    def test_cycle_number_gaps_have_original_line_diagnostic(self):
        """Independently validate the physical trace edge numbering."""
        with TemporaryDirectory() as folder:
            path = Path(folder) / "error.txt"
            observations = (0, 2)
            path.write_text("".join(
                row_text((sample, *self.samples[index].encode())) + "\n"
                for index, sample in enumerate(observations)
            ))
            with self.assertRaisesRegex(TraceError, "error.txt:2: expected edge 1, received 2"):
                tuple(read_samples(path, self.profile))


class StructuralGuards(unittest.TestCase):
    def setUp(self):
        """Use a legal synthetic quote result as a baseline for injected faults."""
        self.profile = PROFILES["default"]
        self.valid = Result(
            code=Code.OK, state=State.OPEN, active=3, stale=4,
            bid=Winner(1, 0, 12, 1, 4, 2), ask=Winner(1, 1, 21, 2, 1, 3), spread=9,
        )

    def test_reference_payload_and_multiple_independent_faults(self):
        """Check independent observable metadata consistency rather than only exact equality."""
        self.assertEqual(payload_defects(self.valid, self.profile), [])
        invalid = replace(
            self.valid, code=Code.BAD_OP, active=12, stale=20,
            bid=replace(self.valid.bid, price=0, age=self.profile.time_half),
            state=State.CROSSED, spread=1,
        )
        descriptions = payload_defects(invalid, self.profile)
        expectations = (
            "both active and stale", "outside this profile", "reserved for updates",
            "invalid query exposed", "not an active venue", "zero field", "ambiguous age",
            "spread does not equal", "state does not match",
        )
        for fragment in expectations:
            self.assertTrue(any(fragment in issue for issue in descriptions), fragment)

    def test_invalid_side_payload_has_canonical_values(self):
        """Require disabled fields to be zero even if the opposite side remains valid."""
        result = replace(self.valid, bid=Winner(price=9), state=State.ASK_ONLY, spread=0)
        self.assertEqual(payload_defects(result, self.profile), ["absent bid is not all zero"])

    def test_difference_keeps_type_when_both_fields_nonempty(self):
        """Show complete context when a structural comparison meets a missing record."""
        self.assertEqual(compare_fields(None, State.OPEN), ["value=None expected=<State.OPEN: 3>"])
        self.assertEqual(compare_fields(4, 4), [])

    def test_percentile_uses_observed_nearest_ranks_ignoring_order(self):
        """Reject arbitrary rank boundaries and preserve integer measurements."""
        values = [27, 1, 22, 2, 5]
        self.assertEqual([percentile(values, rank) for rank in (0, 20, 50, 90, 100)],
                         [1, 1, 5, 27, 27])
        self.assertIsNone(percentile([], 95))
        self.assertIsNone(latency_summary([])["p95"])
        with self.assertRaises(ValueError):
            percentile(values, -1)

    def test_garbage_decimal_driver_is_rejected_with_line(self):
        """Report inability to recover an input edge without hiding its origin."""
        with TemporaryDirectory() as folder:
            path = Path(folder) / "input.txt"
            write_drives(path, (Drive(reset=1),), self.profile)
            with path.open("a") as output:
                output.write("x \n")
            with self.assertRaisesRegex(TraceError, "input.txt:2: expected 18 columns"):
                tuple(read_drives(path, self.profile))
            report = Path(folder) / "report.json"
            save_report(report, {"count": 2})
            self.assertEqual(report.read_text(), '{\n  "count": 2\n}\n')


if __name__ == "__main__":
    unittest.main()
