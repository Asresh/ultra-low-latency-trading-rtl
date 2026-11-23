"""Verify CLI transfers, file safety, and replay-to-analysis integration."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from quote_model import Machine
from quote_replay import Event, write_events
from quote_schema import Command, Drive, PROFILES, Query, row_text
from quote_tool import main, reject_aliases
from quote_verify import read_drives, write_drives
from quote_workload import benchmark_tape


class QuoteCLI(unittest.TestCase):
    def setUp(self):
        """Allocate an isolated filesystem and a representative named profile."""
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.profile = PROFILES["default"]

    def call(self, *args):
        """Run one command without hiding its console status or diagnostics."""
        standard = StringIO()
        errors = StringIO()
        items = [str(arg) for arg in args]
        with redirect_stdout(standard), redirect_stderr(errors):
            status = main(items)
        return status, standard.getvalue(), errors.getvalue()

    def trace_file(self, filename, drives=None):
        """Make a representative trace for testing the CLI separately from RTL simulation."""
        if drives is None:
            drives = benchmark_tape(self.profile, 20).drives
        machine = Machine(self.profile)
        filename.write_text("".join(
            row_text((edge, *machine.step(item).encode())) + "\n"
            for edge, item in enumerate(drives)
        ), encoding="ascii")
        return filename, drives

    def test_parameters_for_every_profile(self):
        """Map all configured profile values to exact Icarus named override flags."""
        for profile, model in PROFILES.items():
            status, text, errors = self.call("parameters", "--profile", profile)
            self.assertEqual(status, 0, errors)
            words = text.split()
            self.assertEqual(len(words), len(model.sv_parameters()))
            expected = {
                f"-Ptb_best_quote_selector.{key}={value}"
                for key, value in model.sv_parameters().items()
            }
            self.assertEqual(set(words), expected)

    def test_raw_audit_creates_reports_and_export(self):
        """Check the nominal workflow yields structured metrics and a human-readable dump."""
        driver = self.folder / "driver.txt"
        trace, inputs = self.trace_file(self.folder / "trace.txt")
        write_drives(driver, inputs, self.profile)
        destination = self.folder / "results"
        outputs = (destination / "raw.json", destination / "observed.jsonl", destination / "report.md")
        status, standard, errors = self.call(
            "audit", driver, trace, "--json", outputs[0], "--capture", outputs[1],
            "--markdown", outputs[2],
        )
        self.assertEqual(status, 0, errors)
        self.assertIn("PASS 25 matched cycles", standard)
        report = json.loads(outputs[0].read_text())
        self.assertEqual(report["timing"]["query_latency"]["p95"], 2)
        self.assertEqual(report["market"]["command_codes"], {"OK": 20})
        self.assertTrue(report["market"]["instruments"])
        self.assertIn("Best quote capture report", outputs[2].read_text())
        records = [json.loads(line) for line in outputs[1].read_text().splitlines()]
        self.assertEqual(len(records), 42)
        self.assertEqual(records[0]["kind"], "reset")
        self.assertEqual(sum(record["kind"] == "query" for record in records), 20)

    def test_replay_target_feeds_audit_without_field_loss(self):
        """Exercise symbolic input, source queue delays, and extracted result semantics."""
        source = self.folder / "events.jsonl"
        driver = self.folder / "build" / "drive.txt"
        report = self.folder / "build" / "schedule.json"
        events = [
            Event(0, reset=True),
            Event(1, command=Command.replace(0, 2, 1, (100, 2), (106, 3))),
            Event(1, query=Query(0, 10, 15)),
            Event(2, query=Query(0, 10, 16)),
            Event(3, query=Query(0, 10, 17)),
        ]
        write_events(source, events, self.profile)
        status, normal, errors = self.call(
            "replay", source, driver, "--report", report, "--window", "2:8:result",
        )
        self.assertEqual(status, 0, errors)
        self.assertIn("Scheduled 4 transfers", normal)
        observed = json.loads(report.read_text())
        self.assertEqual([row["actual"] for row in observed["accepted"]], [1, 1, 2, 8])
        trace, _ = self.trace_file(
            self.folder / "playback.txt", tuple(read_drives(driver, self.profile))
        )
        output = self.folder / "observed.jsonl"
        status, _, errors = self.call("audit", driver, trace, "--capture", output)
        self.assertEqual(status, 0, errors)
        results = [json.loads(row) for row in output.read_text().splitlines()]
        queries = [event for event in results if event["kind"] == "query"]
        self.assertEqual([row["accepted"] for row in queries], [1, 2, 8])
        self.assertEqual([row["result"]["state"] for row in queries], ["EMPTY", "OPEN", "OPEN"])
        self.assertEqual([row["result"]["spread"] for row in queries], [None, 6, 6])
        self.assertEqual(queries[2]["result"]["bid"]["venue"], 2)

    def test_report_rejects_malformed_capture_before_overwrite(self):
        """An ill-typed data record must not replace an existing report."""
        source = self.folder / "bad.jsonl"
        target = self.folder / "report.json"
        source.write_text('{"kind":"bad"}\n')
        target.write_text("keep")
        status, _, errors = self.call("report", source, "--json", target)
        self.assertEqual(status, 2)
        self.assertIn("bad.jsonl:1", errors)
        self.assertEqual(target.read_text(), "keep")

    def test_reject_aliases_with_identical_and_linked_paths(self):
        """Protect input records even when output is a hard link or a symlink."""
        source = self.folder / "source.txt"
        source.write_text("untouched")
        symlink = self.folder / "reference.txt"
        symlink.symlink_to(source)
        from os import link
        hardlink = self.folder / "same_inode.txt"
        link(source, hardlink)
        for target in (source, symlink, hardlink, self.folder / "." / "source.txt"):
            with self.subTest(path=target), self.assertRaisesRegex(ValueError, "aliases"):
                reject_aliases([source], [target])
        self.assertEqual(source.read_text(), "untouched")

    def test_duplicate_outputs_are_detected_before_creation(self):
        """Catch distinct arguments resolving to the same not-yet-created artifact."""
        source, target = self.folder / "source", self.folder / "target"
        with self.assertRaisesRegex(ValueError, "aliases"):
            reject_aliases([source], [target, target])
        self.assertFalse(target.exists())

    def test_malformed_driver_never_starts_an_external_tool(self):
        """Fail on invalid input even when the supplied simulator executable cannot run."""
        driver = self.folder / "bad-driver.txt"
        driver.write_text("1 2 3\n")
        trace = self.folder / "output.txt"
        status, _, errors = self.call(
            "simulate", driver, trace, "--image", "missing-image", "--vvp", "missing-program"
        )
        self.assertEqual(status, 2)
        self.assertIn("expected 18 columns", errors)
        self.assertFalse(trace.exists())

    def test_empty_driver_lacks_a_reset_epoch(self):
        """Require nonempty input before launching a simulation binary."""
        driver = self.folder / "empty.txt"
        driver.write_text("")
        status, _, errors = self.call(
            "simulate", driver, self.folder / "trace.txt", "--image", "unused"
        )
        self.assertEqual(status, 2)
        self.assertIn("needs a nonempty driver", errors)

    def test_external_exit_propagates_meaningful_error(self):
        """Pass the actual child process status through the CLI error boundary."""
        driver = self.folder / "driver.txt"
        write_drives(driver, (Drive(reset=1),), self.profile)
        status, _, errors = self.call(
            "simulate", driver, self.folder / "trace.txt", "--image", "unused", "--vvp", "/usr/bin/false"
        )
        self.assertEqual(status, 2)
        self.assertIn("RTL simulation exited 1", errors)

    def test_regression_arguments_are_bounded_and_unique(self):
        """Refuse too few cycles for coverage and duplicate output seed directories."""
        cases = (
            (("--cycles", 30), "at least 800"),
            (("--seed", 17, "--seed", 17), "must be distinct"),
        )
        for tail, message in cases:
            with self.subTest(message=message):
                status, _, errors = self.call("regress", "--image", "unused", *tail)
                self.assertEqual(status, 2)
                self.assertIn(message, errors)


if __name__ == "__main__":
    unittest.main()
