"""Verify normalized comparison separates semantics, resets, and timing changes."""

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from quote_capture import CommandCapture, Flush, QueryCapture
from quote_compare import compare_captures, main, split_epochs
from quote_schema import Ack, Code, Command, Query, Result
from quote_tool import write_capture


class CompareCaptures(unittest.TestCase):
    def setUp(self):
        """Prepare a reproducible command and lookup in one initial reset epoch."""
        self.ack = CommandCapture(1, 2, 18, Command.withdraw(0, 0, 8, 79), Ack(Code.OK, 79))
        self.query = QueryCapture(1, 3, 18, Query(0, 3, 81), Result(tag=81, tick=18))
        self.first = Flush(0, (), ())
        self.reference = (self.first, self.ack, self.query)

    def test_compare_equal_captures(self):
        """Report complete parity and zero latency delta for an identical run."""
        report = compare_captures(self.reference, self.reference)
        self.assertTrue(report["ok"])
        self.assertEqual(report["semantic_differences"], 0)
        self.assertEqual(report["channels"]["command"],
                         {"matched": 1, "baseline_only": 0, "candidate_only": 0, "mismatched": 0})
        self.assertEqual(report["timing"]["query"]["paired_delta"]["p95"], 0)
        self.assertFalse(report["timing"]["query"]["p95_limit_exceeded"])

    def test_interleaving_change_only_moves_latency(self):
        """Allow separate output streams to complete in a different combined order."""
        candidate = (self.first, self.query, replace(self.ack, delivered=6))
        report = compare_captures(self.reference, candidate)
        self.assertTrue(report["ok"])
        self.assertEqual(report["details"], [])
        self.assertEqual(report["timing"]["command"]["paired_delta"]["max"], 4)
        self.assertFalse(compare_captures(self.reference, candidate, maximum_p95=3)["ok"])
        self.assertTrue(compare_captures(self.reference, candidate, maximum_p95=4)["ok"])

    def test_sampled_tick_and_result_detail_are_semantic(self):
        """Flag a changed sampling point even if request tag and delay stay the same."""
        changed = replace(self.query, tick=19, result=replace(self.query.result, tick=19))
        report = compare_captures(self.reference, (self.first, self.ack, changed))
        self.assertFalse(report["ok"])
        self.assertEqual(report["semantic_differences"], 1)
        self.assertEqual(report["details"][0]["location"], "epoch 1 query 0")
        self.assertIn("tick=19 expected=18", report["details"][0]["detail"])
        self.assertIn("result.tick=19 expected=18", report["details"][0]["detail"])
        self.assertIsNone(report["timing"]["query"]["candidate"]["p95"])

    def test_missing_and_extra_records_count_by_channel(self):
        """Track absent and unexpected logical transfers independently."""
        candidate = (self.first, self.query, replace(self.query, delivered=8))
        report = compare_captures(self.reference, candidate, details=1)
        self.assertEqual(report["semantic_differences"], 2)
        self.assertEqual(len(report["details"]), 1)
        self.assertIn("missing tag", report["details"][0]["detail"])
        self.assertEqual(report["channels"]["command"]["baseline_only"], 1)
        self.assertEqual(report["channels"]["query"]["candidate_only"], 1)

    def test_reset_tag_accounting_ignores_absolute_edge(self):
        """Distinguish a timestamp-only reset shift from losing a queued transaction."""
        reset = Flush(8, (9,), (7,))
        candidate = (*self.reference, replace(reset, cycle=9))
        report = compare_captures((*self.reference, reset), candidate)
        self.assertTrue(report["ok"])
        candidate = (*self.reference, replace(reset, queries=(5,)))
        report = compare_captures((*self.reference, reset), candidate)
        self.assertFalse(report["ok"])
        self.assertIn("flushed", report["details"][0]["detail"])

    def test_missing_reset_is_not_aligned_across_epochs(self):
        """Refuse to hide a missing reset in otherwise identical data."""
        report = compare_captures((*self.reference, Flush(8, (), ())), self.reference)
        self.assertEqual(report["semantic_differences"], 2)
        self.assertIn("reset boundary", report["details"][0]["detail"])
        self.assertIn("ends before", report["details"][1]["detail"])

    def test_control_configuration_and_chronology_validation(self):
        """Reject backward trace order and malformed numeric comparison boundaries."""
        with self.assertRaisesRegex(ValueError, "backwards"):
            list(split_epochs((self.query, self.ack)))
        with self.assertRaisesRegex(ValueError, "difference limit"):
            compare_captures([], [], details=-1)
        with self.assertRaisesRegex(ValueError, "maximum p95"):
            compare_captures([], [], maximum_p95=-2)

    def test_cli_status_and_report_file(self):
        """Write a stable comparison result and return failing status on timing regression."""
        with TemporaryDirectory() as folder:
            base = Path(folder)
            reference = base / "base.jsonl"
            candidate = base / "candidate.jsonl"
            report = base / "summary.json"
            write_capture(reference, self.reference)
            write_capture(candidate, self.reference[:2] + (replace(self.query, delivered=8),))
            self.assertEqual(main([str(reference), str(candidate), "--maximum-p95", "1",
                                   "--output", str(report)]), 1)
            rendered = json.loads(report.read_text())
            self.assertFalse(rendered["ok"])
            self.assertTrue(rendered["timing"]["query"]["p95_limit_exceeded"])
            self.assertEqual(main([str(reference), str(candidate)]), 0)


if __name__ == "__main__":
    unittest.main()
