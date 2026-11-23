"""Check that reported coverage corresponds to decisions actually delivered."""

import unittest
from collections import Counter
from dataclasses import replace

from limiter_capture import CaptureAudit, CaptureMismatch
from limiter_coverage import DecisionCoverage, required_for_standard_matrix
from limiter_model import Limiter
from limiter_protocol import Command, Drive, Observation, PROFILES, Status
from limiter_scenarios import boundary_cases, parallel_burst, saturated_counters
from run_limiter_matrix import check_coverage, command_words


def audit_schedule(schedule):
    """Check a deterministic cycle schedule and return the coverage summary."""
    checker = CaptureAudit(schedule.geometry)
    for drive, sample in zip(schedule.inputs, schedule.expected):
        checker.check_cycle(drive, sample)
    return checker.finish()


class BucketCoverageTest(unittest.TestCase):
    """Check modular time, capacity, and saturated counter transition categories."""

    def setUp(self):
        """Use a small intentional profile to reach wrap and saturation quickly."""
        self.geometry = PROFILES["small"]
        self.policy = Limiter(self.geometry)
        self.coverage = DecisionCoverage(self.geometry)

    def command(self, item):
        """Capture one accepted request and delivered response in order."""
        result = self.policy.decide(item)
        self.coverage.transfer(item, result)
        return result

    def test_ticks_are_measured_from_the_last_delivered_response_per_key(self):
        """Interleaving keys cannot make a long absolute gap look like a wrap."""
        self.command(Command.configure(0, 61, 6, 1))
        self.command(Command.configure(1, 37, 6, 0))
        self.command(Command.offer(0, 61, 6))
        self.command(Command.offer(1, 40, 1))
        self.command(Command.inspect(0, 1))
        self.command(Command.offer(0, 33, 4))
        self.command(Command.offer(0, 0, 1))
        self.command(Command.inspect(0, 32))
        got = self.coverage.export()
        self.assertEqual(got["time_forward_wrap"], 1)
        self.assertEqual(got["time_half_range"], 1)
        self.assertEqual(got["time_backwards"], 1)
        self.assertEqual(got["time_max_forward"], 1)
        self.assertEqual(got["time_zero_rate"], 1)
        self.assertEqual(got["refill_clipped"], 1)
        self.assertEqual(got["same_tick"], 1)
        self.assertGreater(got["key_changed"], 2)

    def test_bad_fields_do_not_count_successful_elapsed_time(self):
        """Configure and failed commands leave time coverage separate."""
        self.command(Command.configure(0, 62, 4, 4))
        before = self.coverage.export()
        response = self.command(Command.offer(0, 3, 0))
        self.assertEqual(response.status, Status.BAD_FIELDS)
        after = self.coverage.export()
        self.assertEqual(after["status_bad_fields"], 1)
        self.assertEqual(after["operation_offer"], 1)
        for category in ("refill_clipped", "time_forward_wrap", "time_zero_rate"):
            self.assertEqual(before.get(category, 0), after.get(category, 0))

    def test_bucket_use_distinguishes_exact_charge_from_insufficient_amount(self):
        """Count ambiguity-free first limits and partial credit observations."""
        self.command(Command.configure(0, 0, 6, 2))
        self.command(Command.offer(0, 0, 6))
        self.command(Command.offer(0, 0, 1))
        self.command(Command.offer(0, 1, 4))
        self.command(Command.offer(0, 1, 1))
        self.command(Command.offer(0, 1, 7))
        got = self.coverage.export()
        self.assertEqual(got["spent_exactly_available"], 1)
        self.assertEqual(got["limited_empty"], 1)
        self.assertEqual(got["limited_by_one"], 1)
        self.assertEqual(got["limited_larger_than_capacity"], 1)
        self.assertEqual(got["balance_retained"], 1)

    def test_saturation_transitions_are_sampled_not_inferred_from_width(self):
        """Count only first overflow, subsequent sticky values, and actual clearing."""
        exact = self.geometry.count_max
        self.command(Command.configure(0, 0, exact, exact))
        first = self.command(Command.offer(0, 0, exact))
        self.assertEqual(first.saturated, 0)
        self.assertNotIn("saturation_first_charged", self.coverage.export())
        overflow = self.command(Command.offer(0, 1, 1))
        self.assertEqual(overflow.saturated, 4)
        self.assertEqual(self.coverage.export()["saturation_first_charged"], 1)
        self.command(Command.inspect(0, 2))
        self.assertEqual(self.coverage.export()["saturation_kept_charged"], 1)
        self.command(Command.clear(0, 2))
        self.assertEqual(self.coverage.export()["saturation_cleared"], 1)

    def test_invalid_key_does_not_replace_valid_previous_anchor(self):
        """Track an unused selector without recording it as an implemented key."""
        self.command(Command.configure(0, 0, 6, 2))
        self.command(Command.offer(3, 2, 1))
        self.command(Command.offer(0, 1, 1))
        self.assertEqual(self.coverage.export()["unused_key_encoding"], 1)
        self.assertEqual(set(self.coverage.latest), {0})
        self.assertEqual(self.coverage.latest[0].anchor, 1)

    def test_reset_drops_per_key_history_but_keeps_campaign_accounting(self):
        """The next run must not measure time from a previous reset epoch."""
        self.command(Command.configure(0, 60, 3, 1))
        self.coverage.reset([1])
        self.policy.reset()
        self.command(Command.inspect(0, 1))
        self.coverage.reset([])
        got = self.coverage.export()
        self.assertEqual(got["reset_pending"], 1)
        self.assertEqual(got["reset_idle"], 1)
        self.assertNotIn("time_forward_wrap", got)
        self.assertEqual(got["configuration_enabled"], 1)
        self.assertEqual(self.coverage.latest, {})


class MatrixCoverageTest(unittest.TestCase):
    """Ensure standard test artifacts retain evidence of boundary behavior."""

    def results(self):
        """Create the same reduced summary that the aggregate checker consumes."""
        profile = "small"
        geometry = PROFILES[profile]
        return {
            profile: {
                "boundary": audit_schedule(boundary_cases(geometry)).to_record(),
                "saturation": audit_schedule(saturated_counters(geometry)).to_record(),
                "burst": audit_schedule(parallel_burst(geometry)).to_record(),
            }
        }

    def test_counter_clip_cases_are_required_for_small_profiles(self):
        """A coverage report cannot claim saturation without known clipped events."""
        self.assertIn("saturation_first_charged", required_for_standard_matrix(["small"]))
        self.assertNotIn("saturation_first_charged", required_for_standard_matrix(["default"]))
        results = self.results()
        combined = check_coverage(results, ["small"])
        self.assertEqual(set(combined["statuses"]), {item.name for item in Status})
        observed = combined["coverage"]
        required = required_for_standard_matrix(["small"])
        self.assertEqual(required - observed.keys(), set())
        for category in required:
            self.assertGreater(observed[category], 0)

    def test_checker_distinguishes_status_and_category_coverage_regressions(self):
        """Removing a category fails even if the same decision codes were seen."""
        results = self.results()
        for report in results["small"].values():
            report["coverage"].pop("time_half_range", None)
        with self.assertRaisesRegex(AssertionError, "missing boundary categories"):
            check_coverage(results, ["small"])
        results = self.results()
        for report in results["small"].values():
            report["statuses"].pop("DISABLED", None)
        with self.assertRaisesRegex(AssertionError, "missing status coverage"):
            check_coverage(results, ["small"])

    def test_handshake_evidence_is_required_separately(self):
        """Even a complete decision matrix needs real output stalls and replacements."""
        results = self.results()
        for report in results["small"].values():
            report["output_stalled"] = 0
        with self.assertRaisesRegex(AssertionError, "incomplete handshake"):
            check_coverage(results, ["small"])

    def test_tool_override_parser_preserves_shell_independent_arguments(self):
        """Wrappers can pass arguments without interpreting filesystem names as shell code."""
        self.assertEqual(command_words('"/opt/tool path/iverilog" -B dep'), [
            "/opt/tool path/iverilog", "-B", "dep",
        ])
        self.assertEqual(command_words("wrapper 'shell; no'"), ["wrapper", "shell; no"])
        with self.assertRaisesRegex(ValueError, "may not be empty"):
            command_words("  ")


class AuditBoundaryTest(unittest.TestCase):
    """Require a complete drain and immutable response across backpressure."""

    def test_missing_end_of_trace_leaves_one_pending_decision(self):
        """Do not publish statistics for an incomplete logical transaction stream."""
        geometry = PROFILES["small"]
        checker = CaptureAudit(geometry)
        drive = Drive(valid=1, command=Command.inspect(0, 0))
        checker.check_cycle(drive, Observation(1, 0, None))
        with self.assertRaisesRegex(CaptureMismatch, "without draining"):
            checker.finish()

    def test_a_mutated_held_snapshot_reports_a_simulator_protocol_defect(self):
        """Identify stability regressions earlier than output value mismatches."""
        schedule = boundary_cases(PROFILES["small"])
        checker = CaptureAudit(schedule.geometry)
        for index, (drive, sample) in enumerate(zip(schedule.inputs, schedule.expected)):
            if checker.held_response is not None and not drive.reset:
                changed = replace(sample, response=replace(sample.response, balance=sample.response.balance ^ 1))
                with self.assertRaisesRegex(CaptureMismatch, f"cycle {index}: sink saw"):
                    checker.check_cycle(drive, changed)
                break
            checker.check_cycle(drive, sample)
        else:
            self.fail("boundary campaign contained no held result")


if __name__ == "__main__":
    unittest.main()
