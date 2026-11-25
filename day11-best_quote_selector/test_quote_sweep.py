"""Known-answer and exhaustive small-capacity checks for sweep economics."""

from __future__ import annotations

import itertools
import random
import unittest
from dataclasses import replace

from quote_model import QuoteBook
from quote_schema import Command, Profile
from quote_sweep_model import Allocation, FeePolicy, SweepCode, SweepQuery, plan
from quote_sweep_vectors import SweepMachine, demo


class SweepEconomicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = Profile(1, 4, 8, 5, 7, 6, 8)
        self.book = QuoteBook(self.profile)
        self.rules = [FeePolicy(1, 31, 0)] * 4
        self.buy = SweepQuery(0, 1, 1, 5, 106, 8, 15)
        self.sell = replace(self.buy, buy=0, limit=1)

    def quote(self, venue: int, bid: tuple, ask: tuple, tick: int = 10) -> None:
        sequence = self.book.last_sequences.get((0, venue), 0) + 1
        self.book.apply(Command.replace(0, venue, sequence, bid, ask), tick)

    def route(self, request: SweepQuery, tick: int = 11):
        return plan(self.book, self.rules, 17, request, tick)

    def test_fee_can_beat_better_displayed_price_and_respects_raw_limit(self):
        self.quote(0, (50, 20), (101, 20))
        self.quote(1, (50, 20), (103, 20))
        self.rules[1] = FeePolicy(1, 20, -10)
        result = self.route(self.buy)
        self.assertEqual(result.children, (Allocation(1, 103, 5, -10, 1, 1),))
        self.assertEqual(result.value, 465)
        limited = self.route(replace(self.buy, limit=101))
        self.assertEqual(limited.children, (Allocation(0, 101, 5, 0, 1, 1),))
        self.assertEqual(limited.value, 505)

    def test_signed_sell_proceeds_and_negative_buy_cost(self):
        self.quote(0, (5, 20), (10, 20))
        self.rules[0] = FeePolicy(1, 20, 20)
        sold = self.route(self.sell)
        self.assertEqual((sold.quantity, sold.value), (5, -75))
        self.rules[0] = FeePolicy(1, 20, -15)
        paid = self.route(self.buy)
        self.assertEqual((paid.quantity, paid.value), (5, -25))

    def test_cap_and_caller_mask_are_independent_of_quote_size(self):
        for venue in range(4):
            self.quote(venue, (20, 9), (50, 15))
        self.rules[0] = FeePolicy(1, 0, -20)
        self.rules[1] = FeePolicy(0, 1, -20)
        self.rules[2] = FeePolicy(1, 3, 0)
        self.rules[3] = FeePolicy(1, 15, 0)
        allowed = replace(self.buy, venues=0b0111)
        partial = self.route(allowed)
        self.assertEqual((partial.status, partial.eligible, partial.quantity, partial.value),
                         (SweepCode.PARTIAL, 4, 3, 150))
        full_required = self.route(replace(allowed, all_or_none=1))
        self.assertEqual((full_required.status, full_required.eligible,
                          full_required.quantity, full_required.value, full_required.children),
                         (SweepCode.AON_UNFILLED, 4, 0, 0, ()))

    def test_wrapping_time_outside_half_range_and_side_absence(self):
        self.quote(0, (44, 9), (50, 15), tick=126)  # at t=1, age is 3
        self.quote(1, (0, 0), (70, 7), tick=65)    # age is exactly half range
        self.quote(2, (52, 9), (0, 0), tick=64)    # quote has no ask
        self.quote(3, (53, 6), (55, 6), tick=120)  # age is 9
        result = self.route(self.buy, tick=1)
        self.assertEqual((result.eligible, result.stale), (0b0001, 0b1010))
        self.assertEqual(result.children[0].age, 3)

    def test_error_precedence_canonical_payload_and_epoch(self):
        cases = (
            (replace(self.buy, instrument=1, quantity=0, limit=0, max_age=64), SweepCode.BAD_INST),
            (replace(self.buy, quantity=0, limit=0, max_age=64), SweepCode.BAD_AGE),
            (replace(self.buy, quantity=0, limit=0), SweepCode.BAD_QTY),
            (replace(self.buy, limit=0), SweepCode.BAD_LIMIT),
        )
        for request, outcome in cases:
            with self.subTest(outcome=outcome):
                result = self.route(request)
                self.assertEqual((result.status, result.epoch), (outcome, 17))
                self.assertEqual(
                    (result.eligible, result.stale, result.quantity, result.value, result.children),
                    (0, 0, 0, 0, ()),
                )

    def test_greedy_value_matches_exhaustive_feasible_allocations(self):
        rng = random.Random(0x0FEED11)
        for _ in range(128):
            self.book.reset()
            quotes = []
            for venue in range(4):
                price = rng.randint(1, 40)
                cap = rng.randint(0, 4)
                fee = rng.randint(-50, 50)
                qty = rng.randint(1, 4)
                self.quote(venue, (price, qty), (price, qty))
                self.rules[venue] = FeePolicy(1, cap, fee)
                quotes.append((price, min(cap, qty), fee))
            target = rng.randint(1, 10)
            options = [range(row[1]+1) for row in quotes]
            filled = min(target, sum(row[1] for row in quotes))
            allocations = [
                row for row in itertools.product(*options) if sum(row) == filled
            ]
            def value(alloc, buy):
                direction = 1 if buy else -1
                return sum(amount * (price + direction*fee)
                           for amount, (price, cap, fee) in zip(alloc, quotes))

            for buy in (0, 1):
                req = replace(self.buy, quantity=target, buy=buy, limit=106 if buy else 1)
                result = self.route(req)
                optimal = min if buy else max
                self.assertEqual(result.quantity, filled)
                self.assertEqual(result.value, optimal(value(row, buy) for row in allocations))
                routes = [child.venue for child in result.children]
                bound = {route: quotes[route][1] for route in routes}
                self.assertEqual(len(routes), len(set(routes)))
                self.assertTrue(all(0 < child.quantity <= bound[child.venue]
                                    for child in result.children))

    def test_cycle_demo_independent_known_answers(self):
        machine = SweepMachine(Profile(), fee_width=12)
        demo(machine)
        self.assertEqual(machine.checked["policy_race"], 1)
        self.assertEqual(machine.checked["data_race"], 1)
        self.assertEqual(machine.checked["result_stall"], 3)
        self.assertEqual(machine.checked["result"], 9)


if __name__ == "__main__":
    unittest.main()
