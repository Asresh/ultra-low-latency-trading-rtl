"""Check exact decimal parsing, wire tape compilation, and group totals."""

import csv
import os
from fractions import Fraction
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from notional_batch import (
    BatchError, ORDER_FIELDS, compile_orders, ensure_distinct, natural,
    pack_price, parse_decimal, read_orders, summarize,
)
from notional_model import Rounding


HEADER = ",".join(ORDER_FIELDS) + "\n"


class DecimalConversion(unittest.TestCase):
    def test_exact_and_quantized_fractions(self):
        """Represent the decimal input without implicit binary-float rounding."""
        self.assertEqual(parse_decimal("100.125"), Fraction(801, 8))
        self.assertEqual(parse_decimal("0.000000000000000000000001"), Fraction(1, 10**24))
        self.assertEqual(pack_price(parse_decimal("100.125"), 8), 0x6420)
        with self.assertRaisesRegex(BatchError, "off the fixed-point grid"):
            pack_price(parse_decimal("0.1"), 8)
        self.assertEqual(pack_price(parse_decimal("0.1"), 8, Rounding.FLOOR), 25)
        self.assertEqual(pack_price(parse_decimal("0.1"), 8, Rounding.CEIL), 26)
        self.assertEqual(pack_price(parse_decimal("0.1"), 8, Rounding.NEAREST_EVEN), 26)

    def test_wire_half_even_at_32_bit_boundary(self):
        """Quantizing a value upward past the physical maximum must fail."""
        largest = parse_decimal("16777215.99609375")
        self.assertEqual(pack_price(largest, 8), 0xFFFFFFFF)
        off_grid = parse_decimal("16777215.998046875")
        self.assertEqual(pack_price(off_grid, 8, Rounding.FLOOR), 0xFFFFFFFF)
        for mode in (Rounding.CEIL, Rounding.NEAREST_EVEN):
            with self.subTest(mode=mode), self.assertRaisesRegex(BatchError, "32 bits"):
                pack_price(off_grid, 8, mode)

    def test_rejects_ambiguous_decimal_spellings(self):
        """Require finite canonical decimal text without alternate notations."""
        for raw in ("NaN", "Inf", "01", "0x10", "+1", "-0", "2e3", ".5", "0.", "1 0", 1.5):
            with self.subTest(raw=raw), self.assertRaises(BatchError):
                parse_decimal(raw)
        for invalid in ("", "-1", "01", "1.2", " 1", "1 "):
            with self.subTest(invalid=invalid), self.assertRaises(BatchError):
                natural(invalid, "order tag", 16)

    def test_lexical_unsigned_ranges(self):
        """The optional limit permits the full unsigned 64-bit range."""
        self.assertEqual(natural("18446744073709551615", "limit", 64), (1 << 64)-1)
        for width, raw in ((16, "65536"), (32, "4294967296"), (64, "18446744073709551616")):
            with self.subTest(width=width), self.assertRaisesRegex(BatchError, "bits"):
                natural(raw, "field", width)


class BatchCompilation(unittest.TestCase):
    def setUp(self):
        """Use isolated files for intentional invalid input and alias tests."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.orders = root / "synthetic.csv"
        self.report = root / "report.csv"
        self.tape = root / "tape.txt"

    def input(self, rows):
        """Write the exact public header and arbitrary synthetic data lines."""
        self.orders.write_text(HEADER + "".join(row + "\n" for row in rows), encoding="utf-8")

    def test_compiles_buy_sell_and_reject_to_expected_wire_words(self):
        """Compare text and tape values from one batch with by-hand cash arithmetic."""
        self.input((
            "9,ABC,buy,12.5,2,400,nearest_even,26",
            "10,ABC,sell,12.5,2,400,floor,24",
            "11,DEF,sell,1,1,0,ceil,2",
        ))
        summary = compile_orders(self.orders, self.report, self.tape, 8)
        self.assertEqual(summary["records"], 3)
        actual = self.tape.read_text().splitlines()
        self.assertEqual(actual[0], "9 c80 2 190 2 0 1 1a 9 0 0 19 1 1a 0 55")
        self.assertEqual(actual[1], "a c80 2 190 0 1 1 18 a 0 1 19 1 18 0 55")
        self.assertEqual(actual[2], "b 100 1 0 1 1 1 2 b 6 1 1 0 1 0 55")
        self.assertEqual(len(summary["source_sha256"]), 64)
        self.assertEqual(len(summary["tape_sha256"]), 64)
        abc = summary["total_by_instrument"]["ABC"]
        self.assertEqual(abc, dict(requests=2, accepted=2, inexact=0, gross=50,
                                   fee=2, buy_debits=26, sell_proceeds=24, net_cash_change=-2))
        other = summary["total_by_instrument"]["DEF"]
        self.assertEqual((other["requests"], other["accepted"], other["net_cash_change"]), (1, 0, 0))
        with self.report.open(encoding="UTF-8") as file:
            report = list(csv.DictReader(file))
        self.assertEqual([row["status"] for row in report], ["OK", "OK", "LIMIT"])
        self.assertEqual(summarize(report), summary["total_by_instrument"])

    def test_input_row_failure_keeps_outputs(self):
        """Parse the entire input before touching an existing report."""
        self.input(("5,AA,buy,2.5,1,0,floor,", "6,AA,sell,2.1,5,0,floor,"))
        self.report.write_text("old report", encoding="UTF-8")
        self.tape.write_text("old tape", encoding="UTF-8")
        with self.assertRaisesRegex(BatchError, "line 3"):
            compile_orders(self.orders, self.report, self.tape, 8)
        self.assertEqual(self.report.read_text(), "old report")
        self.assertEqual(self.tape.read_text(), "old tape")

    def test_aliases_and_missing_columns_are_rejected(self):
        """An existing input can never be selected as one of the generated artifacts."""
        self.input(("1,AB,buy,2,1,0,floor,",))
        os.link(self.orders, self.report)
        with self.assertRaisesRegex(BatchError, "same file"):
            compile_orders(self.orders, self.report, self.tape, 8)
        with self.assertRaisesRegex(BatchError, "same path"):
            ensure_distinct((self.tape, self.tape))
        for line in ("7,ab,sell,2,4,0,floor,", "8,AA,hat,2,4,0,floor,",
                     "9,AB,buy,1,5,2,invalid,", "10,AB,buy,1,5,2,floor",
                     "11,AB,buy,1,5,2,floor,,7"):
            with self.subTest(line=line), self.assertRaises(BatchError):
                list(read_orders(StringIO(HEADER + line + "\n"), 8))

    def test_empty_csv_requires_at_least_one_order(self):
        """Keep accidental empty tapes from looking like a passing regression."""
        self.input(())
        with self.assertRaisesRegex(BatchError, "at least one"):
            compile_orders(self.orders, self.report, self.tape, 8)
        self.assertFalse(self.report.exists())
        self.assertFalse(self.tape.exists())
