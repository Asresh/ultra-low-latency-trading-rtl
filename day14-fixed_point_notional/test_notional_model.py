"""Verify exact fixed-point and fee semantics independent of the hardware arithmetic."""

from dataclasses import replace
from fractions import Fraction
from math import ceil, floor
import random
import unittest

from notional_model import Kind, Reply, Request, Rounding, U64, calculate, rounded_divide
from notional_workload import boundary_orders, cases, model_latency


QUANTIZERS = {Rounding.FLOOR: floor, Rounding.CEIL: ceil, Rounding.NEAREST_EVEN: round}


def rational_amounts(request, fraction_bits):
    """Use Fraction and standard quantizers as an independent three-field oracle."""
    quantize = QUANTIZERS[request.rounding]
    ideal_gross = Fraction(request.price * request.quantity, 1 << fraction_bits)
    gross = quantize(ideal_gross)
    ideal_fee = Fraction(gross * request.fee_bps, 10000)
    fee = quantize(ideal_fee)
    cash = gross - fee if request.sell else gross + fee
    inexact = ideal_gross.denominator != 1 or ideal_fee.denominator != 1
    return gross, fee, cash, inexact


class RoundingRules(unittest.TestCase):
    def test_small_fraction_grid(self):
        """Compare all small residues and both tie parities against Fraction."""
        for denom in range(1, 38):
            for numerator in range(denom * 8):
                value = Fraction(numerator, denom)
                for mode, quantizer in QUANTIZERS.items():
                    observed = rounded_divide(numerator, denom, mode)
                    self.assertEqual(observed.amount, quantizer(value))
                    self.assertEqual(observed.inexact, value.denominator != 1)

    def test_half_rounds_to_even_quotient(self):
        """Check that the low quotient bit decides a tie."""
        for numerator, want in ((1, 0), (3, 2), (5, 2), (7, 4), (9, 4)):
            result = rounded_divide(numerator, 2, Rounding.NEAREST_EVEN)
            self.assertEqual(result, (want, True))

    def test_fee_midpoint_has_two_parities(self):
        """Gross of 3 or 5 produces fees of 1.5 or 2.5 with the same rate."""
        base = Request(1, 1, 1, 5000, Rounding.NEAREST_EVEN)
        for qty in (3, 5):
            with self.subTest(qty=qty):
                buy = calculate(replace(base, quantity=qty), 0)
                sell = calculate(replace(base, quantity=qty, sell=True), 0)
                self.assertEqual(buy.fee, 2)
                self.assertEqual(sell.fee, 2)
                self.assertEqual(buy.cash + sell.cash, 2 * qty)
                self.assertTrue(buy.inexact)

    def test_extreme_denominators_and_exact_cases(self):
        """Integer division and unsigned 80-bit values retain all significant bits."""
        for num, denom in ((0, 1), (1 << 80, 1), ((1 << 80) - 1, 9999), (73 << 31, 1 << 31)):
            for mode, quantizer in QUANTIZERS.items():
                ideal = Fraction(num, denom)
                self.assertEqual(rounded_divide(num, denom, mode).amount, quantizer(ideal))
        for num, denom in ((-1, 2), (1, 0), (1, -2)):
            with self.assertRaisesRegex(ValueError, "nonnegative"):
                rounded_divide(num, denom, Rounding.FLOOR)


class OrderSemantics(unittest.TestCase):
    def test_validation_precedence(self):
        """Invalid input priority remains deterministic when several fields fail."""
        base = Request(7, 0, 0, 10001, 3, True, 15)
        faults = (
            (base, Kind.BAD_QUANTITY),
            (replace(base, quantity=1), Kind.BAD_PRICE),
            (replace(base, quantity=1, price=1), Kind.BAD_FEE),
            (replace(base, quantity=1, price=1, fee_bps=1), Kind.BAD_MODE),
        )
        for order, fault in faults:
            self.assertEqual(calculate(order, 8), Reply(7, fault, True))

    def test_fixed_point_price_rounds_before_fee(self):
        """The second quantizer applies to an already rounded transaction amount."""
        # Price of 1.5, gross rounds to 2 and 75% of that rounds to 2.
        buy = Request(3, 384, 1, 7500, Rounding.NEAREST_EVEN)
        sell = replace(buy, sell=True)
        self.assertEqual(calculate(buy, 8), Reply(3, Kind.OK, False, 2, 2, 4, True))
        self.assertEqual(calculate(sell, 8), Reply(3, Kind.OK, True, 2, 2, 0, True))

    def test_sides_have_opposite_limit_interpretation(self):
        """A cash limit sets a maximum debit or minimum proceeds."""
        order = Request(13, 40, 3, 2500, Rounding.CEIL)
        for limit, want in ((149, Kind.LIMIT), (150, Kind.OK), (151, Kind.OK)):
            self.assertEqual(calculate(replace(order, limit=limit), 0).status, want)
        for limit, want in ((89, Kind.OK), (90, Kind.OK), (91, Kind.LIMIT)):
            self.assertEqual(calculate(replace(order, limit=limit, sell=True), 0).status, want)

    def test_buy_overflow_saturates_and_precedes_limit(self):
        """A 65-bit cash sum saturates with an explicit diagnostic."""
        maximum = (1 << 32) - 1
        buy = Request(0, maximum, maximum, 10000, 2, False, 1)
        result = calculate(buy, 0)
        self.assertEqual(result, Reply(0, Kind.OVERFLOW, False, maximum**2, maximum**2, U64))
        self.assertEqual(calculate(replace(buy, sell=True, limit=0), 0),
                         Reply(0, Kind.OK, True, maximum**2, maximum**2, 0))

    def test_very_small_price_can_quantize_to_zero_gross(self):
        """A positive raw price can produce exact zero at the integer output only after rounding."""
        floor_case = calculate(Request(1, 1, 1, 0, Rounding.FLOOR, False, 0), 31)
        ceil_case = calculate(Request(1, 1, 1, 10000, Rounding.CEIL, False, 0), 31)
        self.assertEqual(floor_case, Reply(1, Kind.OK, False, 0, 0, 0, True))
        self.assertEqual(ceil_case, Reply(1, Kind.LIMIT, False, 1, 1, 2, True))

    def test_random_full_width_matches_rational(self):
        """Check full unsigned multiplication and arbitrary fractions at all sample scales."""
        rng = random.Random(112341)
        for frac in (0, 1, 4, 8, 23, 31):
            for index in range(350):
                price = rng.randint(1, 0xFFFFFFFF)
                qty = rng.randint(1, 0xFFFFFFFF)
                req = Request(index, price, qty, rng.randint(0, 10000),
                              rng.choice(tuple(Rounding)), bool(rng.getrandbits(1)))
                gross, fee, cash, inexact = rational_amounts(req, frac)
                reply = calculate(req, frac)
                self.assertEqual(reply.tag, req.tag)
                self.assertEqual(reply.sell, req.sell)
                self.assertEqual((reply.gross, reply.fee, reply.cash, reply.inexact),
                                 (gross, fee, min(U64, cash), inexact))
                self.assertEqual(reply.status, Kind.OVERFLOW if cash > U64 else Kind.OK)
                self.assertGreaterEqual(reply.gross, reply.fee)

    def test_request_wire_uses_explicit_limit_enable(self):
        """Distinguish an optional limit of zero from an absent limit."""
        base = Request(16, 123, 400, 4, 2, False)
        self.assertEqual(base.wire(), (16, 123, 400, 4, 2, 0, 0, 0))
        self.assertEqual(replace(base, limit=0).wire(), (16, 123, 400, 4, 2, 0, 1, 0))

    def test_field_validation_rejects_python_bool_as_uint(self):
        """Catch accidental truth values and signed port fields early."""
        base = Request(1, 100, 51)
        for key, value in (("tag", True), ("price", -1), ("quantity", 1 << 32),
                           ("fee_bps", 1 << 16), ("rounding", 4), ("sell", 1), ("limit", -1)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                replace(base, **{key: value})
        for fraction_bits in (-1, 32):
            with self.assertRaisesRegex(ValueError, "fractional"):
                calculate(base, fraction_bits)


class StimulusCoverage(unittest.TestCase):
    def test_boundary_tape_contains_all_statuses(self):
        """Integer-price geometry exercises overflow plus every validation code."""
        request_list = list(cases(0, 12, 128))
        observed = {calculate(req, 0).status for req in request_list}
        self.assertEqual(observed, set(Kind))
        self.assertEqual(len({req.tag for req in request_list}), 97)
        self.assertIn(Kind.OVERFLOW, {calculate(req, 0).status for req in boundary_orders(0)})

    def test_validation_shortcut_observable(self):
        """The timing contract distinguishes field errors from full calculations."""
        early = {status for status in Kind if model_latency(status) == 1}
        self.assertEqual(early, {Kind.BAD_MODE, Kind.BAD_QUANTITY, Kind.BAD_PRICE, Kind.BAD_FEE})
        self.assertEqual({model_latency(status) for status in Kind if status not in early}, {85})
