"""Hand-worked marginal fee cases for the independent test oracle."""
from dataclasses import replace
import unittest

from fee_tier_vectors import Control, Fill, Profile, expected_control, expected_fill
from notional_model import U64


class MarginalFeeTest(unittest.TestCase):
    def test_alternating_sign_rates_round_once(self):
        profile = Profile(1, 1, 2, True, (3, 8, 12),
                          (10000, -2000, 3000, -100), (-10000, 10000, -9000, 8000))
        order = Fill(0, 1, 1, 6, 2, True, False, 2)
        result, settled = expected_fill([profile], order, 0, 15)
        self.assertEqual(result, (0, 12, 1, 13, 14, 4, True))
        self.assertEqual(settled.next_sequence, 2)
        self.assertEqual(settled.volume, 14)

    def test_negative_exact_ties_round_to_even(self):
        profile = Profile(1, 2, 0, True, (), (-5000,), (5000,))
        buy = Fill(0, 1, 2, 3, 1, True, False, 2)
        sell = replace(buy, maker=False, sell=True)
        self.assertEqual(expected_fill([profile], buy, 0, 31)[0][:4], (0, 3, -2, 1))
        self.assertEqual(expected_fill([profile], sell, 0, 31)[0][:4], (0, 3, 2, 1))
        one = replace(buy, price=1)
        self.assertEqual(expected_fill([profile], one, 0, 31)[0][:4], (0, 1, 0, 1))

    def test_turnover_overflow_leaves_committed_state(self):
        previous = Profile(1, 3, U64, True, (), (1,), (1,))
        fill = Fill(0, 1, 3, 1, 1, True, False, 0)
        failed, exposed = expected_fill([previous], fill, 0, 31)
        self.assertEqual(failed, (9, 0, 0, 0, U64, 0, 0))
        self.assertIs(exposed, previous)

    def test_bad_install_does_not_change_venue(self):
        original = Profile(1, 2, 77, True, (4, 5), (0, 2, -2), (1, 1, 1))
        change = Control(0, 0, 1, 1, 0, (5, 5), (0, 2, -2), (1, 1, 1))
        self.assertEqual(expected_control([original], change, 15), (3, original))
        self.assertEqual(expected_control([original], replace(change, revision=0), 15), (2, original))

    def test_zero_gross_can_advance_last_sequence_without_a_fee(self):
        profile = Profile(1, 7, 0, True, (), (10000,), (-10000,))
        order = Fill(0, 1, 7, 1, 1, True, False, 0)
        result, settled = expected_fill([profile], order, 8, 7)
        self.assertEqual(result, (0, 0, 0, 0, 0, 0, True))
        self.assertFalse(settled.armed)
        self.assertEqual(settled.next_sequence, 7)
