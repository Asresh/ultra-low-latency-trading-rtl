"""Hand-worked currency-rate lookup and exact rounding cases."""
from dataclasses import replace
import unittest

from fx_cash_vectors import Quote, Request, convert


class FxCashTest(unittest.TestCase):
    def setUp(self):
        self.request = Request(7,0,2,True,1,15,5,2)
        self.book = {
            0: Quote(0,1,0,2,8,8,60),
            1: Quote(1,1,0,1,8,12,86),
            2: Quote(2,1,1,2,4,6,85),
        }

    def convert(self, request):
        return convert(self.book, 100, request, 3, 3, 3)[1]

    def test_use_pivot_and_round_once(self):
        # Ask product (12/8)*(6/8)=9/8. 5*9/8 rounds to 6.
        self.assertEqual(self.convert(self.request), (0,6,2,1,2,15,True))
        # Bid product (8/8)*(4/8)=1/2. -5/2 rounds to -2.
        self.assertEqual(self.convert(replace(self.request, amount=-5)), (0,-2,2,1,2,15,True))

    def test_future_quote_is_stale(self):
        self.book[0] = replace(self.book[0], observed=101)
        self.assertEqual(self.convert(replace(self.request, via=False))[0], 3)
        self.book[0] = replace(self.book[0], observed=100)
        self.assertEqual(self.convert(self.request), (0,5,1,0,7,0,False))

    def test_extreme_signed_amount_stays_exact_on_identity(self):
        raw = -(1 << 64)
        self.assertEqual(self.convert(replace(self.request,dst=0,amount=raw))[1],raw)
