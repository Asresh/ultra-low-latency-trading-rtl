"""Check cash-limit search against independent exhaustive and rational cases."""

from dataclasses import replace
from fractions import Fraction
import random
import unittest

from notional_limits import Boundary, LimitError, quantity_plan, recheck
from notional_model import Kind, Request, Rounding, U64, calculate
from notional_study import RoundingStudy, describe, exact_amounts, fraction_text
from test_notional_model import rational_amounts


class LimitSearch(unittest.TestCase):
    def test_buy_boundary_returns_adjacent_failure(self):
        """For a five-unit price and zero fees, budget 22 allows four units."""
        request = Request(1, 5, 1, limit=22)
        plan = quantity_plan(request, 0, 45)
        self.assertEqual(plan.accepted, Boundary(4, 20, 20, 0, Kind.OK))
        self.assertEqual(plan.adjacent_reject, Boundary(5, 25, 25, 0, Kind.LIMIT))
        self.assertTrue(0 < plan.trials <= 8)
        recheck(plan, request, 0)

    def test_sell_boundary_is_minimum_feasible_quantity(self):
        """The lower proceeds limit is inclusive on the first quantized success."""
        req = Request(3, 5, 1, 2000, Rounding.NEAREST_EVEN, True, 17)
        plan = quantity_plan(req, 0, 33)
        self.assertEqual(plan.accepted, Boundary(5, 20, 25, 5, Kind.OK))
        self.assertEqual(plan.adjacent_reject, Boundary(4, 16, 20, 4, Kind.LIMIT))
        recheck(plan, req, 0)

    def test_boundary_plateau_can_reach_the_maximum(self):
        """Positive subunit prices can be affordable at a zero cash budget."""
        req = Request(1, 1, 1, 1, Rounding.FLOOR, False, 0)
        plan = quantity_plan(req, 8, 50)
        self.assertEqual(plan.accepted, Boundary(50, 0, 0, 0, Kind.OK))
        self.assertIsNone(plan.adjacent_reject)
        recheck(plan, req, 8)

    def test_no_feasible_buy_or_sell(self):
        """Retain a diagnostic boundary even when no quantity passes."""
        buy = Request(1, 10, 1, limit=5)
        sell = Request(2, 11, 1, 10000, Rounding.CEIL, True, 1)
        small_buy = quantity_plan(buy, 0, 20)
        unprofitable_sell = quantity_plan(sell, 0, 31)
        self.assertIsNone(small_buy.accepted)
        self.assertEqual(small_buy.adjacent_reject, Boundary(1, 10, 10, 0, Kind.LIMIT))
        self.assertIsNone(unprofitable_sell.accepted)
        self.assertEqual(unprofitable_sell.adjacent_reject, Boundary(31, 0, 341, 341, Kind.LIMIT))
        recheck(small_buy, buy, 0)
        recheck(unprofitable_sell, sell, 0)

    def test_full_range_buy_prevents_addition_overflow(self):
        """Use an explicit overflow status as a first-failing witness in the search."""
        maximum = (1 << 32) - 1
        req = Request(11, maximum, 1, 10000, Rounding.FLOOR, False, U64)
        plan = quantity_plan(req, 0, maximum)
        self.assertEqual(plan.accepted.quantity, U64 // (2 * maximum))
        self.assertEqual(plan.adjacent_reject.code, Kind.OVERFLOW)
        self.assertEqual(plan.adjacent_reject.cash, U64)
        self.assertLessEqual(plan.trials, 34)
        recheck(plan, req, 0)

    def test_seeded_monotonic_search_agrees_with_brute_force(self):
        """Compare both sides and every rounding mode over hundreds of complete scans."""
        rng = random.Random(9911)
        for frac in (0, 1, 8, 12):
            for sample in range(75):
                price = rng.randint(1, 2**frac * 6)
                fee = rng.randint(0, 10000)
                maximum = rng.randint(1, 50)
                sell = bool(sample & 1)
                rounding = rng.randrange(3)
                req = Request(sample, price, 1, fee, rounding, sell, rng.randint(0, 220))
                good = []
                for quantity in range(1, maximum+1):
                    gross, fee, cash, inexact = rational_amounts(replace(req, quantity=quantity), frac)
                    acceptable = cash >= req.limit if sell else cash <= req.limit
                    if acceptable:
                        good.append(quantity)
                plan = quantity_plan(req, frac, maximum)
                expected = (min(good) if sell else max(good)) if good else None
                actual = plan.accepted.quantity if plan.accepted else None
                self.assertEqual(actual, expected)
                recheck(plan, req, frac)

    def test_invalid_options_reject_early(self):
        """A cash-limit witness is meaningful only for legal pricing fields and limits."""
        req = Request(0, 12, 1, limit=5)
        for candidate in (replace(req, limit=None), replace(req, price=0),
                          replace(req, fee_bps=10001), replace(req, rounding=3)):
            with self.subTest(candidate=candidate), self.assertRaises(LimitError):
                quantity_plan(candidate, 0, 6)
        for maximum in (False, 0, 1 << 32, 5.5):
            with self.subTest(maximum=maximum), self.assertRaises(LimitError):
                quantity_plan(req, 0, maximum)

    def test_recheck_catches_tampered_boundary(self):
        """A serialized witness must still match its exact calculation."""
        order = Request(123, 11, 1, limit=17)
        plan = quantity_plan(order, 0, 4)
        changed = replace(plan, accepted=replace(plan.accepted, cash=12))
        with self.assertRaisesRegex(LimitError, "accepted witness"):
            recheck(changed, order, 0)
        changed = replace(plan, adjacent_reject=replace(plan.adjacent_reject, code=Kind.OK))
        with self.assertRaisesRegex(LimitError, "adjacent witness"):
            recheck(changed, order, 0)


class StudyReports(unittest.TestCase):
    def test_exact_fee_is_rational_before_price_quantization(self):
        """Raw-price multiplication is a separate comparison point from RTL fee rounding."""
        request = Request(1, 3, 1, 1250)
        gross, fee, cash = exact_amounts(request, 1)
        self.assertEqual((gross, fee, cash), (Fraction(3, 2), Fraction(3, 16), Fraction(27, 16)))
        sold = exact_amounts(replace(request, sell=True), 1)
        self.assertEqual(sold[2], Fraction(21, 16))

    def test_describe_keeps_negative_and_fractional_errors_exact(self):
        """The report never estimates small rounding errors with float arithmetic."""
        stats = describe([Fraction(-1, 3), Fraction(2, 5), Fraction(0, 1), Fraction(1, 3)])
        self.assertEqual(stats["min"], "-1/3")
        self.assertEqual(stats["median"], "1/6")
        self.assertEqual(stats["mean"], "1/10")
        self.assertEqual(stats["zero_error"], 1)
        self.assertEqual(describe([])["mean"], "0")
        self.assertEqual(fraction_text(7), "7")

    def test_rounding_can_flip_a_cash_limit(self):
        """A buy at exactly a floor value can fail when rounded up."""
        study = RoundingStudy(1)
        req = Request(1, 3, 1, 0, 0, limit=1)
        study.check_order("FOO", req)
        report = study.summary(1)
        self.assertEqual(report["rounding_sensitive_count"], 1)
        example, = report["rounding_sensitive_examples"]
        self.assertEqual(example["includes"], ["FLOOR"])
        self.assertEqual(example["excludes"], ["CEIL", "NEAREST_EVEN"])
        self.assertEqual((example["floor_cash"], example["ceil_cash"], example["even_cash"]), (1, 2, 2))
        self.assertEqual(report["modes"]["FLOOR"]["error"]["gross"]["mean"], "-1/2")
        self.assertEqual(report["modes"]["CEIL"]["error"]["gross"]["mean"], "1/2")
        self.assertEqual(study.summary(0)["rounding_sensitive_examples"], [])

    def test_overflows_and_invalid_rows_are_separately_counted(self):
        """Avoid treating saturated values as ordinary small rounding errors."""
        study = RoundingStudy(0)
        max32 = (1 << 32) - 1
        study.check_order("X", Request(1, max32, max32, 10000))
        study.check_order("X", Request(2, 7, 0))
        result = study.summary(2)
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["invalid_fields"], {"BAD_QUANTITY": 1})
        for stats in result["modes"].values():
            self.assertEqual(stats["outcomes"], {"OVERFLOW": 1})
            self.assertEqual(stats["saturated_results"], 1)
            self.assertEqual(stats["error"]["cash"]["samples"], 0)
        with self.assertRaises(ValueError):
            study.summary(-1)
