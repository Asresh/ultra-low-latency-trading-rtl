"""Verify normalized named-venue imports and exact integer clock quantization."""

import csv
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from quote_csv import Catalog, COLUMNS, CsvImportError, ceil_cycles, read_catalog, read_csv
from quote_csv_tool import main
from quote_replay import Event, read_events, schedule
from quote_schema import Command, PROFILES, Query


def stream_for(*records):
    """Serialize explicit CSV fixtures with a stable column order and blank optional fields."""
    file = StringIO()
    writer = csv.DictWriter(file, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(records)
    file.seek(0)
    return file


class CsvTime(unittest.TestCase):
    def test_integer_round_up_across_noninteger_periods(self):
        """Check 156.25 MHz slots use ceiling, including an exact boundary."""
        freq = 156_250_000
        for delay, edge in ((0, 0), (1, 1), (6, 1), (7, 2), (31, 5), (32, 5), (33, 6)):
            with self.subTest(delay=delay):
                self.assertEqual(ceil_cycles(delay, freq), edge)

    def test_long_intervals_avoid_binary_float_roundoff(self):
        """Keep exact clock rounding for timestamps beyond a binary float's integer range."""
        origin = 1 << 60
        self.assertEqual(ceil_cycles(origin, 1_000_000_000), origin)
        self.assertEqual(ceil_cycles(origin + 1, 1_000_000_000), origin + 1)
        self.assertEqual(ceil_cycles(origin + 1, 500_000_000), origin // 2 + 1)
        for delta, frequency in ((-1, 1), (1, 0), (1, 1.3), (True, 1)):
            with self.assertRaises(CsvImportError):
                ceil_cycles(delta, frequency)


class NamedConversion(unittest.TestCase):
    def setUp(self):
        """Provide bounded synthetic aliases and an ordinary two-sided update."""
        self.profile = PROFILES["default"]
        self.aliases = {"instruments": {"ALPHA": 0, "BETA": 2}, "venues": {"EAST": 0, "WEST": 3}}
        self.catalog = Catalog.from_object(self.aliases, self.profile)
        self.update = {
            "time_ns": 500, "kind": "replace", "instrument": "ALPHA", "venue": "WEST",
            "sequence": 9, "bid_price": 32, "bid_qty": 5, "ask_price": 42, "ask_qty": 4,
            "tag": 22,
        }

    def test_injects_reset_and_orders_collocated_records(self):
        """Retain source order when two times round to the same cycle."""
        find = {"time_ns": 501, "kind": "query", "instrument": "ALPHA", "max_age": 4, "tag": 23}
        query_b = dict(find, time_ns=505, instrument="BETA", tag=24)
        source = stream_for(self.update, find, query_b)
        events, report = read_csv(source, self.catalog, self.profile)
        self.assertEqual(
            events,
            (
                Event(0, reset=True),
                Event(1, command=Command.replace(0, 3, 9, (32, 5), (42, 4), 22)),
                Event(2, query=Query(0, 4, 23)),
                Event(2, query=Query(2, 4, 24)),
            ),
        )
        self.assertEqual(asdict(report), {
            "origin_ns": 500, "clock_hz": 100_000_000, "rows": 3,
            "first_cycle": 1, "last_cycle": 2, "injected_reset": True,
            "collocated_data_rows": 1,
        })
        replay = schedule(self.profile, events)
        self.assertEqual([item.planned for item in replay.accepted], [1, 2, 2])
        self.assertEqual([item.actual for item in replay.accepted], [1, 2, 3])

    def test_explicit_reset_and_venue_controls(self):
        """Honor a real reset record and preserve control plus withdrawal ordering."""
        records = (
            {"time_ns": 490, "kind": "reset"},
            self.update,
            {"time_ns": 510, "kind": "withdraw", "instrument": "ALPHA",
             "venue": "WEST", "sequence": 10},
            {"time_ns": 520, "kind": "disable", "instrument": "ALPHA", "venue": "WEST"},
            {"time_ns": 530, "kind": "enable", "instrument": "ALPHA", "venue": "WEST"},
        )
        events, report = read_csv(stream_for(*records), self.catalog, self.profile)
        self.assertFalse(report.injected_reset)
        self.assertEqual([event.cycle for event in events], list(range(5)))
        self.assertEqual(events[2].command, Command.withdraw(0, 3, 10))
        self.assertEqual(events[3].command, Command.venue_enabled(0, 3, False))
        self.assertEqual(events[4].command, Command.venue_enabled(0, 3, True))

    def test_invalid_payloads_fail_with_line_information(self):
        """Reject ambiguous columns, unknown aliases, and integers too wide for the bus."""
        checks = (
            (dict(self.update, venue="NORTH"), "unknown venue"),
            (dict(self.update, instrument="OMEGA"), "unknown instrument"),
            (dict(self.update, bid_price="10.5"), "nonnegative integer"),
            (dict(self.update, sequence=""), "missing sequence"),
            (dict(self.update, sequence=0x1000), "does not fit"),
            (dict(self.update, max_age=3), "unexpected fields"),
            (dict(self.update, kind="execute"), "unknown row kind"),
        )
        for row, error in checks:
            with self.subTest(error=error):
                with self.assertRaisesRegex(CsvImportError, "CSV line 2: .*" + error):
                    read_csv(stream_for(row), self.catalog, self.profile)

    def test_bad_header_and_missing_record_rejected(self):
        """Reject missing and duplicate headers without guessing at transport fields."""
        malformed_headers = (
            "",
            "time_ns,kind,venue\n",
            ",".join(COLUMNS + ("time_ns",)) + "\n",
        )
        for header in malformed_headers:
            with self.subTest(header=header):
                with self.assertRaisesRegex(CsvImportError, "CSV columns"):
                    read_csv(StringIO(header), self.catalog, self.profile)
        with self.assertRaisesRegex(CsvImportError, "no records"):
            read_csv(stream_for(), self.catalog, self.profile)

    def test_descending_time_and_reset_collision_rejected(self):
        """Stop when ordering or a rounded reset cannot be represented faithfully."""
        records = (
            ((self.update, dict(self.update, time_ns=400)), "nondecreasing"),
            ((self.update, {"time_ns": 500, "kind": "reset"}), "same edge"),
        )
        for pair, detail in records:
            with self.subTest(detail=detail):
                with self.assertRaisesRegex(CsvImportError, detail):
                    read_csv(stream_for(*pair), self.catalog, self.profile)

    def test_catalog_validation_and_freezing(self):
        """Reject ambiguous maps and isolate a published mapping from caller edits."""
        values = (
            ({"instruments": {}, "venues": {"EAST": 0}}, "nonempty"),
            ({"instruments": {"ALPHA": 0, "BETA": 0}, "venues": {"EAST": 0}}, "duplicate"),
            ({"instruments": {" ALPHA": 1}, "venues": {"EAST": 0}}, "invalid"),
            ({"instruments": {"ALPHA": True}, "venues": {"EAST": 0}}, "outside profile"),
            ({"instruments": {"ALPHA": 1}, "venues": {"EAST": 5}}, "outside profile"),
            ({"instruments": {"ALPHA": 1}}, "needs instruments and venues"),
        )
        for obj, reason in values:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(CsvImportError, reason):
                    Catalog.from_object(obj, self.profile)
        self.aliases["instruments"]["ALPHA"] = 3
        self.assertEqual(self.catalog.instrument("ALPHA"), 0)

    def test_catalog_duplicate_key_and_cli_artifacts(self):
        """Read a catalog once, then verify the CLI emits playable events and a report."""
        with TemporaryDirectory() as folder:
            base = Path(folder)
            catalog = base / "catalog.json"
            catalog.write_text('{"instruments":{"ALPHA":0,"ALPHA":1},"venues":{"WEST":3}}')
            with self.assertRaisesRegex(CsvImportError, "duplicate catalog key"):
                read_catalog(catalog, self.profile)
            catalog.write_text(json.dumps(self.aliases))
            source = base / "quotes.csv"
            source.write_text(stream_for(self.update).read())
            output = base / "out" / "events.json"
            report = base / "reports" / "origin.json"
            self.assertEqual(main([str(source), str(catalog), str(output), "--report", str(report)]), 0)
            self.assertEqual(len(tuple(read_events(output, self.profile))), 2)
            self.assertEqual(json.loads(report.read_text())["origin_ns"], 500)


if __name__ == "__main__":
    unittest.main()
