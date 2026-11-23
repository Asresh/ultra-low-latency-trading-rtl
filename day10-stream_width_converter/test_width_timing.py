"""Check the analytical clock comparison and reproducible campaign configuration."""

from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from width_campaign import (
    CampaignError, Profile, campaign, defaults, parse_profiles, run_tool, sha256, write_summary,
)
from width_timing import Arrival, Ports, Ready, TimingError, compare, load, summarize


class Clocks(unittest.TestCase):
    def test_single_short_packet(self):
        """Both models publish a two-byte tail one edge after the input."""
        options = Ports(4, 8, 64, 12)
        results = compare([Arrival(2, 0)], options, Ready(1, 0))
        for path in ("store_and_forward", "cut_through"):
            report = results[path]
            self.assertEqual(report["cycles"], 2)
            self.assertEqual(report["first_after_commit_p50"], 1)
            self.assertEqual(report["complete_after_input_start_p95"], 1)
            self.assertEqual(report["payload_per_cycle"], "1")
        self.assertEqual(results["payload_storage_bytes"], {
            "store_and_forward": 128, "cut_through": 12,
        })
        self.assertEqual(results["cut_through_metadata_bits"], 204)

    def test_multi_beat_lead_and_complete_pacing(self):
        """A validated stream can emit well before the last input byte."""
        options = Ports(4, 8, 64, 16)
        ready = Ready(1, 0)
        result = compare((Arrival(30, 0),), options, ready)
        packet = result["store_and_forward"]
        stream = result["cut_through"]
        self.assertEqual(packet["first_after_commit_p50"], 1)
        self.assertEqual(stream["first_after_commit_p50"], -5)
        self.assertEqual(packet["complete_after_input_start_p95"], 11)
        self.assertEqual(stream["complete_after_input_start_p95"], 8)
        self.assertEqual((packet["outputs_started_before_commit"], stream["outputs_started_before_commit"]),
                         (0, 1))
        self.assertEqual(result["cycles_saved_by_cut_through"], 3)

    def test_backpressure_conserves_distinct_frames(self):
        """Exercise partial masks and source stalls under a periodic ready blackout."""
        packets = [Arrival(size, index % 4) for index, size in enumerate(range(1, 80))]
        ports = Ports(7, 3, 80, 11)
        ready = Ready(9, 7)
        result = compare(packets, ports, ready)
        for mode in ("store_and_forward", "cut_through"):
            current = result[mode]
            self.assertEqual(current["payload_bytes"], sum(range(1, 80)))
            self.assertEqual(current["packets"], 79)
            self.assertGreater(current["source_stall_edges"], 0)
            self.assertGreater(current["sink_stall_edges"], 0)
            self.assertGreater(current["simultaneous_edges"], 0)
        self.assertGreater(result["store_and_forward"]["max_packets_in_flight"], 1)

    def test_optional_input_spacing_and_tiny_horizon(self):
        """Arrival clocks count from the same origin even when the bus is initially idle."""
        packets = [Arrival(1, 20), Arrival(2, 3)]
        ports = Ports(1, 2, 8, 3)
        enabled = Ready(1, 0)
        report = summarize(packets, ports, enabled, "packet")
        self.assertEqual(report["cycles"], 26)
        self.assertEqual(report["payload_per_cycle"], "3/26")
        with self.assertRaisesRegex(TimingError, "elapsed-clock"):
            summarize(packets, ports, enabled, "packet", cycle_limit=10)
        with self.assertRaisesRegex(TimingError, "unknown"):
            summarize(packets, ports, enabled, "bad")

    def test_impossible_geometries_and_empty_requests(self):
        """Require one legal geometry and packets that can fit both study variants."""
        valid = Ports(4, 8, 12, 12)
        ready = Ready(8, 4)
        conditions = (
            (replace(valid, input_width=0), [Arrival(1, 0)], "physical width"),
            (replace(valid, output_width=17), [Arrival(1, 0)], "physical width"),
            (replace(valid, byte_capacity=4), [Arrival(1, 0)], "byte FIFO"),
            (replace(valid, packet_limit=0), [Arrival(1, 0)], "packet bank"),
            (valid, [], "both implementations"),
            (valid, [Arrival(13, 0)], "both implementations"),
        )
        for geometry, requests, reason in conditions:
            with self.subTest(geometry=geometry), self.assertRaisesRegex(TimingError, reason):
                compare(requests, geometry, ready)
        for period, busy in ((0,0), (3,3), (3,-1), (4097,2), (True,0)):
            with self.subTest(period=period, busy=busy), self.assertRaises(TimingError):
                Ready(period,busy)

    def test_canonical_csv_schema(self):
        """Whitespace, aliases, and missing fields don't silently change arrival times."""
        with TemporaryDirectory() as name:
            path = Path(name) / "lengths.csv"
            path.write_text("length,gap\n2,0\n37,8\n11,1\n", encoding="utf-8")
            self.assertEqual(load(path), [Arrival(2,0), Arrival(37,8), Arrival(11,1)])
            for raw, pattern in (("gap,length\n1,2\n", "expected length,gap"),
                                 ("length,gap\n01,8\n", "row 2"),
                                 ("length,gap\n1,\n", "row 2"),
                                 ("length,gap\n1,2,3\n", "row 2"),
                                 ("length,gap\n0,1\n", "row 2")):
                path.write_text(raw, encoding="utf-8")
                with self.subTest(raw=raw), self.assertRaisesRegex(TimingError, pattern):
                    load(path)


class Campaign(unittest.TestCase):
    def example(self):
        """Return one validated profile dictionary with no executable options."""
        return {"name": "roomy", "input": 4, "output": 8, "capacity": 128, "seed": 9}

    def test_profile_validates_compile_time_geometry(self):
        """Compile parameters come only from controlled numeric fields."""
        example = Profile.parse(self.example())
        self.assertEqual(example.parameters(), [
            "-Ptb_packet_gearbox.IN_BYTES=4",
            "-Ptb_packet_gearbox.OUT_BYTES=8",
            "-Ptb_packet_gearbox.MAX_FRAME=128",
        ])
        self.assertEqual(example.values(), {
            "name": "roomy", "input_width": 4, "output_width": 8,
            "capacity": 128, "seed": 9, "packets": 180,
        })
        for key, value in (("name", "../out"), ("input", False), ("output", 17),
                           ("seed", -1), ("packets", 39), ("capacity", 0), ("tool", "false")):
            with self.subTest(key=key), self.assertRaises(CampaignError):
                Profile.parse({**self.example(), key: value})

    def test_campaign_checks_bounds_and_duplicate_outputs(self):
        """No two parallel jobs may write the same result directory."""
        one = self.example()
        for rows in (None, (), [], [one, one], [one] * 25):
            with self.subTest(rows=rows), self.assertRaises(CampaignError):
                parse_profiles(rows)
        with self.assertRaisesRegex(CampaignError, "edge budget"):
            Profile.parse({**one, "input": 1, "capacity": 384, "packets": 500})
        self.assertEqual(len(defaults()), 4)
        self.assertEqual(len({row.name for row in defaults()}), 4)

    def test_local_process_failures_are_reported(self):
        """Capture return codes and timeouts without interpreting config as shell code."""
        with TemporaryDirectory() as name:
            log = Path(name) / "output.log"
            run_tool([sys.executable, "-c", "print('proof')"], log)
            self.assertEqual(log.read_text().strip(), "proof")
            with self.assertRaisesRegex(CampaignError, "returned 3"):
                run_tool([sys.executable, "-c", "raise SystemExit(3)"], log)
            with self.assertRaisesRegex(CampaignError, "timed out"):
                run_tool([sys.executable, "-c", "import time; time.sleep(3)"], log, timeout=0.01)
            self.assertEqual(len(sha256(log)), 64)

    def test_worker_reports_failed_row_and_retains_its_profile(self):
        """A missing simulator produces an explicit failed campaign and keeps source evidence."""
        with TemporaryDirectory() as name:
            root = Path(name)
            profiles = [Profile.parse({**self.example(), "packets": 41})]
            report = campaign(root, profiles, str(root / "absent"), "vvp")
            self.assertEqual(report["passed"], 0)
            self.assertEqual(report["profiles"], 1)
            self.assertEqual(report["results"][0]["profile"]["name"], "roomy")
            self.assertIn("unable to start", report["results"][0]["error"])
            self.assertTrue((root / "roomy" / "stimulus.txt").is_file())
            self.assertTrue((root / "campaign.json").is_file())
            with self.assertRaisesRegex(CampaignError, "worker count"):
                campaign(root, profiles, "iverilog", "vvp", jobs=0)

    def test_summary_keeps_partial_failure_and_metrics(self):
        """Packet totals reflect only completed profiles in an ordered report."""
        with TemporaryDirectory() as name:
            root = Path(name)
            rows = [{"stimulus_cycles": 31, "traffic": {"packets": 2}}, {"error": "malformed"}]
            report = write_summary(root, rows)
            self.assertEqual(report["passed"], 1)
            self.assertEqual(report["total_cycles"], 31)
            self.assertEqual(report["total_output_packets"], 2)
            self.assertEqual(report["results"], rows)
            self.assertFalse((root / "campaign.tmp").exists())
