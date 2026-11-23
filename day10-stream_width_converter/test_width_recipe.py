"""Test human-authored traffic schedules and exact pcap replay conversion."""

from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
from struct import pack
from tempfile import TemporaryDirectory
import unittest

from width_capture import CaptureError, Geometry
from width_model import Status
from width_pcap import ImportErrorDetail, compile_pcap, ensure_pace, interface_key, select_rows, shared_capture_module
from width_recipe import Edit, Packet, Recipe, RecipeError, Source, Window, compile_recipe, simulate


def small_recipe(packets=None):
    """Use simple timing and explicit field edits for handwritten fixtures."""
    return {
        "input_width": 2,
        "output_width": 4,
        "capacity": 8,
        "packets": packets if packets is not None else [
            {"tag": 1, "payload": "11 22 33", "at": 3, "interbeat": 2},
            {"tag": 2, "payload": "44 55", "at": 8, "edits": [{"beat": 0, "keep": 0}]},
            {"tag": 3, "payload": "aa bb cc dd", "at": 9},
        ],
        "output_stalls": [[8, 12]],
        "report_stalls": [[7, 10]],
    }


class RecipeValidation(unittest.TestCase):
    def test_timestamps_stalls_and_faults(self):
        """Packet, beat gap, and report windows change actual handshake clocks."""
        plan = Recipe.parse(small_recipe())
        incoming = []
        outputs = []
        reports = []
        for cycle, (drive, observed) in enumerate(simulate(plan)):
            if drive.valid and observed.input_ready:
                incoming.append((cycle, drive.beat.tag, drive.beat.last))
            if drive.ready and observed.output is not None:
                outputs.append((cycle, observed.output.tag, observed.output.bytes(4)))
            if drive.report_ready and observed.report is not None:
                reports.append((cycle, observed.report.tag, observed.report.status))
        self.assertEqual(incoming[:2], [(3, 1, False), (6, 1, True)])
        self.assertEqual(outputs, [(7, 1, b"\x11\x22\x33"), (13, 3, bytes.fromhex("aabbccdd"))])
        self.assertEqual(reports, [(10, 1, Status.OK), (11, 2, Status.BAD_KEEP), (13, 3, Status.OK)])

    def test_held_beat_keeps_edits_and_timing(self):
        """A blocked source offers the identical bytes until a real transfer."""
        spec = Recipe.parse(small_recipe([{"tag": 5, "payload": "01 02 03", "at": 8,
                                          "interbeat": 1, "edits": [{"beat": 1, "tag": 4}]}]))
        sender = Source(spec)
        self.assertIsNone(sender.offer(2))
        first = sender.offer(8)
        self.assertEqual(first, sender.offer(10))
        sender.retire(10)
        self.assertIsNone(sender.offer(11))
        second = sender.offer(12)
        self.assertTrue(second.last)
        self.assertEqual(second.tag, 4)
        sender.retire(12)
        self.assertIsNone(sender.offer(13))
        self.assertTrue(sender.done)
        with self.assertRaisesRegex(RuntimeError, "offered beat"):
            sender.retire(13)

    def test_schema_fields_are_checked_before_scheduling(self):
        """Unknown, missing, reordered, and overlapping directives are reported."""
        invalid = [
            ({**small_recipe(), "input_width": True}, "input width"),
            ({**small_recipe(), "capacity": 0}, "capacity"),
            ({**small_recipe(), "output_stalls": [[2,5],[3,4]]}, "overlap"),
            ({**small_recipe(), "output_stalls": [[4,4]]}, "stall end"),
            ({**small_recipe(), "report_stalls": ["3:8"]}, "stall window"),
            ({**small_recipe(), "stale": True}, "unrecognized"),
            (small_recipe([]), "at least one"),
            (small_recipe([{"tag": 0, "payload": "11", "at": 9},
                           {"tag": 1, "payload": "22", "at": 8}]), "ordered"),
            (small_recipe([{"tag": 0, "payload": "1"}]), "hexadecimal"),
            (small_recipe([{"tag": 0, "payload": ""}]), "at least one byte"),
        ]
        for case, message in invalid:
            with self.subTest(case=case), self.assertRaisesRegex(RecipeError, message):
                Recipe.parse(case)

    def test_exact_edit_validation(self):
        """An edit must target one valid lane word and cannot remove the last marker."""
        for bad in ({"beat": 0}, {"beat": -1, "data": 0},
                    {"beat": 1, "data": 0xABCD0}, {"beat": 3, "keep": 0},
                    {"beat": 0, "last": True}):
            with self.subTest(bad=bad), self.assertRaises(RecipeError):
                Edit.parse(bad, 2, 2)
        spec = {"tag": 6, "payload": "010203", "edits": [{"beat": 0, "keep": 1},
                                                         {"beat": 0, "data": 0}]}
        with self.assertRaisesRegex(RecipeError, "one edit"):
            Packet.parse(spec, 2)
        edit = Edit.parse({"beat": 1, "data": 0xFCC5, "keep": 1}, 2, 2)
        self.assertEqual(edit, Edit(1, 1, None, 0xFCC5))

    def test_watchdog_and_empty_window(self):
        """A long blocked burst hits the bounded recipe timeout deterministically."""
        plan = Recipe.parse(small_recipe([{"tag": 1, "payload": "11", "at": 99}]))
        with self.assertRaisesRegex(RecipeError, "did not drain"):
            list(simulate(plan, max_cycles=30))
        self.assertFalse(Window(2, 7).covers(1))
        self.assertTrue(Window(2, 7).covers(2))
        self.assertFalse(Window(2, 7).covers(7))

    def test_output_path_is_kept_on_malformed_input(self):
        """A parse error or an alias must never overwrite the recipe or an existing tape."""
        with TemporaryDirectory() as name:
            root = Path(name)
            source = root / "input.json"
            tape = root / "compiled.txt"
            source.write_text("{}", encoding="utf-8")
            tape.write_text("prior", encoding="ascii")
            with self.assertRaises(RecipeError):
                compile_recipe(source, tape)
            self.assertEqual(tape.read_text(), "prior")
            source.write_text(json.dumps(small_recipe()), encoding="utf-8")
            with self.assertRaises(CaptureError):
                compile_recipe(source, source)
            count = compile_recipe(source, tape)
            self.assertEqual(count, len(tape.read_text().splitlines()))
            self.assertTrue(all(len(line.split()) == 22 for line in tape.read_text().splitlines()))


class CaptureImport(unittest.TestCase):
    def make_packet(self, index, payload, **adjustments):
        """Construct a portable capture row with explicit source metadata."""
        capture = shared_capture_module()
        row = capture.CapturePacket(index, 0, 0, "ethernet", 1, index * 13, len(payload), payload)
        return replace(row, **adjustments)

    def test_exact_time_round_up_and_skips(self):
        """Select just complete Ethernet payloads and retain relative arrival times."""
        records = (
            self.make_packet(0, b"11", interface=1),
            self.make_packet(1, b"22"),
            self.make_packet(2, b"33", wire_length=3),
            self.make_packet(3, b"44", link_type=228),
            self.make_packet(4, b"abcde1234", fcs_bytes=4),
        )
        recipe, stats = select_rows(records, Geometry(2, 3, 4), Fraction(8, 1), (0, 0))
        self.assertEqual([p["at"] for p in recipe["packets"]], [0, 5])
        self.assertEqual([p["payload"] for p in recipe["packets"]], ["3232", "6162636465"])
        self.assertEqual(stats.values(), {"imported": 2, "skipped_interface": 1,
                                         "skipped_truncated": 1, "skipped_link_type": 1,
                                         "rejected_oversize": 1, "removed_fcs": 4})

    def test_negative_clock_alias_and_excess_time(self):
        """Reject a nonmonotonic capture or unusable clock before scheduling."""
        valid = self.make_packet(1, b"x")
        reversed_record = self.make_packet(0, b"y")
        geometry = Geometry(2, 4, 8)
        with self.assertRaisesRegex(ImportErrorDetail, "decreasing"):
            select_rows((valid, reversed_record), geometry, Fraction(2))
        with self.assertRaisesRegex(ImportErrorDetail, "horizon"):
            select_rows((valid, replace(valid, capture_time_ns=1000013)), geometry, Fraction(1))
        with self.assertRaisesRegex(ImportErrorDetail, "no complete"):
            select_rows((valid,), geometry, Fraction(2), (72, 0))
        with self.assertRaisesRegex(ImportErrorDetail, "FCS"):
            select_rows((replace(valid, fcs_bytes=2),), geometry, Fraction(2))
        for pace in ("0", "9/0", "invalid", "-1"):
            with self.subTest(pace=pace), self.assertRaises(ImportErrorDetail):
                ensure_pace(pace)
        for value in ("-1:0", "0", "0:0:0", "a:1"):
            with self.subTest(value=value), self.assertRaises(ImportErrorDetail):
                interface_key(value)

    def test_binary_classic_capture_to_tape(self):
        """Exercise the existing pcap decoder through the independent gearbox fixture path."""
        with TemporaryDirectory() as name:
            root = Path(name)
            source = root / "traffic.pcap"
            target = root / "recipe.json"
            tape = root / "source.txt"
            first, second = b"sample-01", b"sample-02"
            raw = pack("<IHHIIII", 0xA1B23C4D, 2, 4, 0, 0, 128, 1)
            raw += pack("<IIII", 8, 900, len(first), len(first)) + first
            raw += pack("<IIII", 8, 926, len(second), len(second)) + second
            source.write_bytes(raw)
            summary = compile_pcap(source, target, Geometry(4, 8, 128), "25/2", tape=tape)
            self.assertEqual(summary.imported, 2)
            plan = json.loads(target.read_text())
            self.assertEqual([item["at"] for item in plan["packets"]], [0, 3])
            self.assertEqual([bytes.fromhex(item["payload"]) for item in plan["packets"]], [first, second])
            self.assertTrue(tape.read_text().startswith("1 "))
            with self.assertRaises(ImportErrorDetail):
                compile_pcap(source, target, Geometry(4, 8, 128), 8, tape=target)
