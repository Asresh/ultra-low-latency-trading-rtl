"""Exercise the public terminal workflow from source capture to checked replay export."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from replay_audit import PinSample
from replay_cli import main
from replay_format import load_packet_csv, read_tape, write_tape
from replay_model import ReplayModel
from replay_program import Packet, Tape
from replay_vectors import read_cyclestream
from test_replay_experiments import observation_row
from test_replay_formats import ethernet, frame, header


class TerminalWorkflowTests(unittest.TestCase):
    """Validate public output files, program validation, and exit status contracts."""

    def setUp(self):
        """Build inputs and output targets inside an isolated temporary directory."""
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.capture = self.directory / "input capture.csv"
        self.program = self.directory / "program.trp"
        self.script = self.directory / "control.txt"
        self.stimulus = self.directory / "input.stim"
        self.transcript = self.directory / "output.trace"

    def run_cli(self, *arguments):
        """Return decoded structured output and any exit code from the public CLI."""
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                status = int(main([str(value) for value in arguments]))
            except SystemExit as exc:
                status = int(exc.code)
        return status, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def okay(self, *arguments):
        """Run a command and require JSON output with no diagnostics."""
        status, data, errors = self.run_cli(*arguments)
        self.assertEqual((status, errors), (0, ""))
        self.assertIsInstance(data, dict)
        return data

    def rejected(self, arguments, explanation):
        """Require a rejection before any JSON output and compare its explanation."""
        status, data, lines = self.run_cli(*arguments)
        self.assertEqual(status, 2)
        self.assertIsNone(data)
        self.assertIn(explanation, lines)
        self.assertNotIn("Traceback", lines)

    def write_input(self):
        """Create two packets sharing an arrival tick and one later packet."""
        self.capture.write_text(
            "time_ns,tag,payload\n"
            "1000.125,5,4142434445464748696a6b\n"
            "1000.125,6,fe03\n"
            "1050.125,7,78\n",
            encoding="utf-8",
        )

    def synthetic_transcript(self, depth):
        """Generate expected capture text from already-compiled configuration pins."""
        model = ReplayModel(depth)
        with self.transcript.open("w", encoding="ascii") as output:
            for index, cycle in enumerate(read_cyclestream(self.stimulus)):
                sample = PinSample.expected(index, model.step(cycle).observation)
                output.write(observation_row(sample)+"\n")

    def test_conversion_and_time_rebasing(self):
        """Compile a finite capture, then crop and scale without editing packet bytes."""
        self.write_input()
        digest = self.okay("compile-csv", self.capture, self.program, "--depth", 8,
                           "--lead", 2, "--beat-spacing", 3, "--packet-gap", 2, "--verbose")
        self.assertEqual(digest["origin_ns"], "8001/8")
        self.assertEqual(digest["packets"], 3)
        self.assertEqual(digest["beats"], 4)
        self.assertEqual(digest["bytes"], 14)
        self.assertEqual(digest["worst_shift_ticks"], 5)
        self.assertEqual([p["actual_tick"] for p in digest["placements"]], [2,7,9])
        inspection = self.okay("inspect", self.program, "--repeats", 2, "--gap", 3, "--verbose")
        self.assertEqual(inspection["prediction"]["emitted_beats"], 8)
        self.assertEqual(inspection["prediction"]["emitted_packets"], 6)
        self.assertEqual(inspection["prediction"]["last_transfer_tick"], 24)
        self.assertEqual(inspection["packet_details"][1]["tag"], 6)
        result_csv = self.directory / "transformed.csv"
        transformed = self.okay("transform", self.program, result_csv, "--start", 7,
                                "--end", 9, "--rebase", 2, "--scale", "3.5", "--rounding", "nearest")
        self.assertEqual(transformed["beats"], 2)
        self.assertEqual(result_csv.read_text(), "tick,tag,last,payload\n7,6,1,fe03\n14,7,1,78\n")

    def test_replay_and_export_known_program(self):
        """Run scripted pin capture, audit timing and export the exact accepted packets."""
        self.write_input()
        self.okay("compile-csv", self.capture, self.program, "--depth", 8)
        self.script.write_text(
            'load "program.trp"\nstart 2 4 -> OK\nawait_done 100\n',
            encoding="utf-8",
        )
        cfg = self.okay("script", self.script, self.stimulus, "--depth", 8)
        self.assertEqual(len(cfg["requests"]), 6)
        self.assertTrue(all(row["status"] == "OK" for row in cfg["requests"]))
        self.synthetic_transcript(8)
        audit = self.okay("audit", self.stimulus, self.transcript, "--depth", 8)
        self.assertEqual(audit["events"]["beats"], 8)
        self.assertEqual(audit["events"]["packets"], 6)
        metrics = self.okay("metrics", self.stimulus, self.transcript, "--verbose")
        self.assertEqual(metrics["runs"][0]["beats_per_tag"], {"5": 4, "6": 2, "7": 2})
        check = self.okay("compare", self.program, self.stimulus, self.transcript,
                          "--repeats", 2, "--gap", 4)
        self.assertEqual(check["missing_beats"], 0)
        dest = self.directory / "accepted.csv"
        report = self.okay("export", self.stimulus, self.transcript, dest, "--reference", "due", "--pass", 2)
        self.assertEqual(report, {"packets": 3, "bytes": 14})
        programmed = read_tape(self.program)
        with dest.open(newline="", encoding="utf-8") as output:
            packets = tuple(load_packet_csv(output))
        self.assertEqual([item.payload for item in packets], [item.payload for item in programmed.packets()])
        self.assertEqual([item.time_ns for item in packets], [0, 20, 50])

    def test_replay_metrics_violations_return_one_and_json(self):
        """Differentiate a checked timing failure from malformed input or tool errors."""
        self.script.write_text(
            "append 8 1 1 41\nstart\nawait_done 20\n", encoding="utf-8",
        )
        self.okay("script", self.script, self.stimulus)
        self.synthetic_transcript(16)
        status, document, output = self.run_cli(
            "metrics", self.stimulus, self.transcript, "--maximum-first", 0
        )
        self.assertEqual((status, output), (1, ""))
        self.assertEqual(document["violations"], ["run 0: first transfer 9 exceeds 0 cycles"])

    def test_pcap_pipeline_filters_tracks_and_exports_timing(self):
        """Keep one selected VLAN packet and preserve every captured byte."""
        pcap = self.directory / "select.pcap"
        skipped = ethernet(0x0800, (7,), b"skip")
        chosen = ethernet(0x0800, (8,7), b"keep")
        raw = header(">", True)
        raw += frame(">", 1, 7, skipped)
        raw += frame(">", 1, 12, chosen)
        raw += frame(">", 2, 10, ethernet(0x86DD, (8,7), b"bypass"))
        pcap.write_bytes(raw)
        output = self.okay("compile-pcap", pcap, self.program, "--vlan", 8,
                           "--ethertype", "0x0800", "--tag", "outer-vlan", "--lead", 9)
        self.assertEqual(output["origin_ns"], "1000000012")
        self.assertEqual(output["packets"], 1)
        saved, = read_tape(self.program).packets()
        self.assertEqual(saved.payload, chosen)
        self.assertEqual(saved.tag, 8)
        self.assertEqual(saved.start, 9)

    def test_destination_aliases_are_rejected_before_any_input_changes(self):
        """Refuse identical, symbolic-link and hard-link output aliases."""
        self.write_input()
        old = self.capture.read_bytes()
        link = self.directory / "alias.csv"
        link.symlink_to(self.capture)
        hard = self.directory / "same data.csv"
        hard.hardlink_to(self.capture) if hasattr(hard, "hardlink_to") else None
        if not hard.exists():
            import os
            os.link(self.capture, hard)
        for output in (self.capture, link, hard):
            with self.subTest(output=output):
                self.rejected(("compile-csv", self.capture, output), "aliases")
                self.assertEqual(self.capture.read_bytes(), old)
        write_tape(Tape.from_packets((Packet.paced(1,b"same",3),)), self.program)
        self.rejected(("transform", self.program, self.program), "aliases")

    def test_capacity_and_keyword_errors_leave_old_output_untouched(self):
        """Reject invalid compile options before replacing an existing tape."""
        self.write_input()
        old = Tape.from_packets((Packet.paced(7,b"previous",0),))
        write_tape(old, self.program)
        for options, message in [
            (("--depth",1), "replay entries"),
            (("--collision","fail"), "available tick"),
            (("--speed","0"), "positive"),
            (("--clock-hz","Infinity"), "finite"),
            (("--origin-ns","1200"), "predecessor"),
        ]:
            with self.subTest(options=options):
                self.rejected(("compile-csv", self.capture, self.program, *options), message)
                self.assertEqual(read_tape(self.program), old)

    def test_bad_script_and_wrong_fixture_depth_are_distinct(self):
        """Report compile locations and capture mismatches with readable errors."""
        self.script.write_text("append 0 1 1 aa\nstart 0 -> OK\n")
        self.rejected(("script", self.script, self.stimulus), "line 2")
        self.assertFalse(self.stimulus.exists())
        self.script.write_text("append 0 1 1 aa\nappend 0 2 1 bb\nstart\nawait_done 12\n")
        self.okay("script", self.script, self.stimulus, "--depth", 2)
        self.synthetic_transcript(2)
        self.rejected(("audit", self.stimulus, self.transcript, "--depth", 1), "response")
        self.assertEqual(self.okay("audit", self.stimulus, self.transcript, "--depth", 2)["events"]["packets"], 2)


if __name__ == "__main__":
    unittest.main()
