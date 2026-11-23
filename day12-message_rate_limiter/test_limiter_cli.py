"""Verify the documented offline CLI and its source-file safety rules."""

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from limiter_capture import prepare
from limiter_cli import existing_alias, integer_set, named_status, protect_sources
from limiter_protocol import Manifest, PROFILES, Status, write_rows
from limiter_scenarios import boundary_cases


class CliToolsTest(unittest.TestCase):
    """Drive the real entry point and inspect its portable output files."""

    def setUp(self):
        """Use temporary files for all traffic generation and error fixtures."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        self.driver = Path(__file__).with_name("limiter_cli.py")

    def run_cli(self, *args):
        """Run the offline tool with the same interpreter as the unit tests."""
        return subprocess.run(
            [sys.executable, "-B", str(self.driver), *(str(arg) for arg in args)],
            text=True,
            capture_output=True,
        )

    def require_json(self, *args):
        """Require CLI success and decode its standard JSON report."""
        execution = self.run_cli(*args)
        self.assertEqual(execution.returncode, 0, execution.stderr)
        return json.loads(execution.stdout)

    def demand(self):
        """Write a small trace with same-tick contention for both policy tools."""
        path = self.workspace / "demands.csv"
        path.write_text("tick,cost\n0,2\n0,3\n2,4\n3,2\n", encoding="utf-8")
        return path

    def capture_folder(self):
        """Create an auditable boundary fixture without depending on Icarus."""
        path = self.workspace / "capture"
        geometry = PROFILES["small"]
        plan = boundary_cases(geometry)
        manifest = Manifest("small", "boundary", 0, len(plan.inputs), geometry)
        prepare(path, manifest, plan.inputs)
        write_rows(path / "capture.hex", (sample.words() for sample in plan.expected))
        return path

    def test_sample_and_prepare_without_tool_path(self):
        """Generate portable files and compiles them into a reusable pin workload."""
        sample = self.workspace / "sample"
        generated = self.run_cli("sample", sample, "--profile", "small")
        self.assertEqual(generated.returncode, 0, generated.stderr)
        output = self.workspace / "output"
        result = self.require_json(
            "prepare", sample / "workload.csv", output, "--stalls", sample / "stalls.csv",
            "--profile", "small", "--iverilog", "not-used-in-offline-mode",
        )
        self.assertEqual(result["source"]["submitted"], 9)
        self.assertEqual(result["source"]["accepted"], 7)
        self.assertEqual(result["source"]["peak_queue"], 2)
        self.assertEqual(len(result["source"]["reset_discarded_at_source"]), 2)
        self.assertTrue((output / "inputs.hex").exists())
        self.assertFalse((output / "capture.hex").exists())
        self.assertEqual(Manifest.read(output / "manifest.json").profile, "small")
        self.assertEqual(json.loads((output / "source.json").read_text()), result)

    def test_audit_selected_status_window_and_timeline(self):
        """Allow a reader to inspect only the failed attempts for one key."""
        reads = self.capture_folder()
        timeline = self.workspace / "timeline.csv"
        report = self.require_json(
            "audit", reads, "--key", 0, "--status", "bad_tick", "--timeline", timeline,
        )
        self.assertEqual(report["window_count"], 2)
        self.assertEqual(report["window_keys"][0]["key"], 0)
        self.assertEqual(report["window_keys"][0]["statuses"], {"BAD_TICK": 2})
        self.assertEqual(report["pins"]["discarded"], 1)
        with timeline.open(newline="") as file:
            parsed = list(csv.DictReader(file))
        self.assertEqual(len(parsed), 2)
        self.assertTrue(all(row["status"] == "BAD_TICK" for row in parsed))
        self.assertTrue(all(int(row["latency"]) >= 1 for row in parsed))

    def test_audit_empty_window_includes_full_pin_metrics_and_json_output(self):
        """Filtering the timeline must leave re-verification and totals intact."""
        reads = self.capture_folder()
        report = self.workspace / "audit.json"
        empty = self.workspace / "empty.csv"
        result = self.run_cli(
            "audit", reads, "--first", "99", "--last", "100", "--by-issue",
            "--json", report, "--timeline", empty,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(report.read_text())
        self.assertEqual(record["window_count"], 0)
        self.assertEqual(record["window_keys"], [])
        self.assertGreater(record["latency"]["samples"], 0)
        self.assertEqual(len(empty.read_text().splitlines()), 1)
        self.assertEqual(result.stdout, "")

    def test_tune_surfaces_same_tick_capacity_bound_and_candidate_grid(self):
        """Verify frontiers and the minimum refill report through the public CLI."""
        source = self.demand()
        report = self.require_json(
            "tune", source, "--capacities", "4-6", "--rates", "0,1,2",
            "--profile", "small", "--full-pass", "--all-scores",
        )
        self.assertEqual(report["offers"], 4)
        self.assertEqual(report["candidates"], 9)
        self.assertEqual(report["minimum_full_pass"]["largest_same_tick_cost"], 5)
        self.assertEqual(report["minimum_full_pass"]["policies"][0], {
            "capacity": 4, "minimum_refill": None,
        })
        self.assertEqual(len(report["scores"]), 9)
        self.assertTrue(report["frontier"])

    def test_envelope_reports_witness_and_peak_traffic_without_a_replay(self):
        """A capacity recommendation should expose the interval that requires it."""
        record = self.require_json(
            "envelope", self.demand(), "--rates", "0,2", "--windows", "1-2",
        )
        self.assertEqual(record["offers"], 4)
        self.assertEqual(record["refill_requirements"][0]["minimum_configured_capacity"], 11)
        self.assertEqual(record["refill_requirements"][1]["minimum_configured_capacity"], 5)
        peaks = [row["maximum_cost"] for row in record["periods"]]
        self.assertEqual(peaks, [5, 6])
        witness = record["refill_requirements"][1]["witness"]
        self.assertEqual(witness["required_capacity"], 5)

    def test_clock_bridge_exports_wrap_map_and_compatible_arrivals(self):
        """Check interoperability between the two offline schedule commands."""
        demands = self.workspace / "absolute.csv"
        demands.write_text("tick,key,cost\n0,0,2\n72,0,1\n")
        policies = self.workspace / "keys.json"
        policies.write_text('[{"key":0,"capacity":5,"rate":1}]\n')
        output = self.workspace / "wrapped"
        bridge = self.run_cli("bridge", demands, policies, output, "--profile", "small")
        self.assertEqual(bridge.returncode, 0, bridge.stderr)
        clock_map = json.loads((output / "clock-map.json").read_text())
        self.assertEqual(clock_map["inspections"], 2)
        self.assertEqual(clock_map["rows"][-1]["absolute_tick"], 72)
        self.assertEqual(clock_map["rows"][-1]["encoded_tick"], 8)
        run = self.require_json("prepare", output / "workload.csv", self.workspace / "plan", "--profile", "small")
        self.assertEqual(run["source"]["submitted"], 5)
        self.assertEqual(run["source"]["accepted"], 5)

    def test_cli_reports_resource_and_invalid_enum_errors(self):
        """Guard against inadmissible candidate scans and malformed filters."""
        source = self.demand()
        folder = self.capture_folder()
        cases = (
            (("tune", source, "--capacities", "1-30", "--rates", "0-30", "--max-candidates", 200), "maximum 200"),
            (("tune", source, "--capacities", "9999", "--rates", "0", "--profile", "small"), "fit small"),
            (("envelope", source, "--rates", "0-100", "--max-scans", 12), "maximum 12"),
            (("audit", folder, "--status", "MAYBE"), "expected one of"),
            (("audit", folder, "--first", "8", "--last", "4"), "last cycle"),
            (("bridge", self.workspace / "missing", source, self.workspace / "output"), "No such file"),
        )
        for args, error in cases:
            with self.subTest(error=error):
                result = self.run_cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_output_paths_do_not_destroy_sources_or_each_other(self):
        """Reject ordinary paths, symbolic aliases, and hardlinked files."""
        source = self.demand()
        original = source.read_text()
        alias = self.workspace / "alias.csv"
        alias.symlink_to(source)
        hardlink = self.workspace / "hardlink.csv"
        os.link(source, hardlink)
        paths = (source, alias, hardlink)
        for item in paths:
            with self.subTest(path=item.name):
                self.assertTrue(existing_alias(source, item))
                result = self.run_cli("tune", source, "--capacities", 5, "--rates", 1, "--json", item)
                self.assertIn("would overwrite", result.stderr)
                self.assertEqual(source.read_text(), original)
        destination = self.workspace / "same.csv"
        with self.assertRaisesRegex(ValueError, "would overwrite"):
            protect_sources([], [destination, destination])

    def test_alias_of_capture_file_cannot_be_replaced_by_audit(self):
        """A report path must stay separate from the trace proving it."""
        capture = self.capture_folder()
        source = capture / "capture.hex"
        saved = source.read_text()
        result = self.run_cli("audit", capture, "--timeline", source)
        self.assertIn("would overwrite", result.stderr)
        self.assertEqual(source.read_text(), saved)

    def test_argument_helpers_keep_range_labels_and_numeric_meaning(self):
        """Policy ranges are ascending, bounded, and deduplicated."""
        self.assertEqual(integer_set("2,4-7,5,0"), [0, 2, 4, 5, 6, 7])
        self.assertEqual(named_status("bad_tick"), Status.BAD_TICK)
        for text in ("", "ab", "1,,2", "4-2", "0-10000", "-1", "1-2-3"):
            with self.assertRaises(argparse.ArgumentTypeError):
                integer_set(text)


if __name__ == "__main__":
    unittest.main()
