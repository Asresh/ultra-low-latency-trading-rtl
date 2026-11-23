"""Validate policy search and burst envelopes against independent small enumerations."""

import io
import random
import unittest
from fractions import Fraction

from limiter_envelope import capacity_grid, find_capacity, max_cost_in_period, period_grid
from limiter_tuning import (
    Demand, Policy, accepts_all, burst_demand, candidate_policies, dominant, evaluate,
    frontier, minimum_full_pass_grid, minimum_refill, parse_demand_rows, sweep,
)


def full_interval_bound(traffic, rate):
    """Enumerate every contiguous interval and compute its refill deficit."""
    greatest = 0
    for first, start in enumerate(traffic):
        amount = 0
        for end in traffic[first:]:
            amount += end.cost
            residual = amount - rate * (end.tick - start.tick)
            greatest = max(greatest, residual)
    return greatest


def brute_period(traffic, duration):
    """Collect all interval costs without a sliding pointer."""
    maximum = 0
    for first, start in enumerate(traffic):
        amount = sum(row.cost for row in traffic[first:] if row.tick - start.tick < duration)
        maximum = max(maximum, amount)
    return maximum


def generated_demands(seed, length):
    """Make a repeatable absolute-time trace with many same-tick collisions."""
    rng = random.Random(seed)
    tick = 0
    demands = []
    for _ in range(length):
        tick += rng.randrange(4)
        demands.append(Demand(tick, rng.randint(1, 8)))
    return demands


class DemandValidationTest(unittest.TestCase):
    """Keep CSV input shape and chronological validation independent of replay."""

    def decode(self, text):
        """Read a synthetic demand file with a stable name for diagnostics."""
        return tuple(parse_demand_rows(io.StringIO(text), "demands.csv"))

    def test_valid_file_order_keeps_same_tick_bursts(self):
        """Retain costs in source order when multiple messages share a tick."""
        expected = (Demand(0, 3), Demand(5, 7), Demand(5, 1), Demand(8, 4))
        self.assertEqual(self.decode("tick,cost\n0,3\n5,7\n5,1\n8,4\n"), expected)
        self.assertEqual(self.decode("tick,cost\n"), ())
        self.assertEqual(burst_demand(expected), [(0, 3), (5, 8), (8, 4)])

    def test_invalid_csv_preserves_context(self):
        """Refuse negative, empty, or out-of-order arrivals with field context."""
        cases = (
            ("time,cost\n", "tick,cost header"),
            ("tick,cost\n1\n", "demands.csv:2: expected two"),
            ("tick,cost\n-1,2\n", "demands.csv:2: tick"),
            ("tick,cost\n1,0\n", "cost must be positive"),
            ("tick,cost\n1,\n", "cost must be an unsigned"),
            ("tick,cost\n5,1\n4,2\n", "demands.csv:3: ticks"),
            ('tick,cost\n"2,3\n', "unexpected end"),
        )
        for source, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ValueError, expected):
                    self.decode(source)

    def test_value_types_and_policy_limits(self):
        """Reject types or magnitudes unusable for integer-credit diagnostics."""
        for tick, cost in ((-1, 1), (1, -1), (True, 1), (1, 0), (1, 2.5)):
            with self.assertRaises(ValueError):
                Demand(tick, cost)
        for cap, rate in ((0, 0), (3, 4), (1, -1), (True, 0), (3, 0.5)):
            with self.assertRaises(ValueError):
                Policy(cap, rate)
        self.assertEqual(Policy(1, 0).rate, 0)
        self.assertEqual(Policy(1, 1).capacity, 1)


class ReplayScoringTest(unittest.TestCase):
    """Calculate precise passes, notional credits, and the longest gap in service."""

    def test_a_fatter_bucket_may_admit_fewer_messages_due_to_earlier_cost(self):
        """Show why pass-count tuning cannot safely binary-search a rate threshold."""
        # Rate 0 rejects the large second offer and preserves small capacity.
        # Rate 1 admits it, then rejects the next two no-refill offers.
        demand = (
            Demand(0, 2), Demand(1, 4), Demand(1, 1), Demand(1, 1), Demand(1, 1),
        )
        slow = evaluate(Policy(5, 0), demand)
        fast = evaluate(Policy(5, 1), demand)
        self.assertEqual((slow.passed, slow.charged), (4, 5))
        self.assertEqual((fast.passed, fast.charged), (2, 6))
        self.assertEqual(slow.worst_streak, 1)
        self.assertEqual(fast.worst_streak, 3)
        self.assertFalse(dominant(slow, fast))
        self.assertFalse(dominant(fast, slow))

    def test_exact_ratios_and_denial_classes(self):
        """Count capacity failures separately from lack of current credit."""
        demand = (Demand(0, 6), Demand(0, 2), Demand(1, 11), Demand(2, 5), Demand(3, 2))
        run = evaluate(Policy(7, 2), demand)
        self.assertEqual(run.offered, 5)
        self.assertEqual(run.passed, 3)
        self.assertEqual(run.limited, 2)
        self.assertEqual(run.oversize, 1)
        self.assertEqual(run.insufficient, 1)
        self.assertEqual(run.worst_streak, 2)
        self.assertEqual(run.end_balance, 0)
        self.assertEqual(run.pass_ratio, Fraction(3, 5))
        self.assertEqual(run.charge_ratio, Fraction(13, 26))
        self.assertEqual(run.to_record()["pass_ratio"], "3/5")

    def test_empty_demand_is_fully_satisfied_before_any_clock(self):
        """Use defined ratios and preserved starting capacity for no traffic."""
        config = Policy(11, 3)
        measure = evaluate(config, iter(()))
        self.assertEqual((measure.passed, measure.charged, measure.end_balance), (0, 0, 11))
        self.assertEqual(measure.pass_ratio, Fraction(1))
        self.assertEqual(measure.charge_ratio, Fraction(1))
        self.assertEqual(minimum_refill(11, ()), 0)
        self.assertTrue(accepts_all(config, ()))

    def test_same_tick_burst_can_rule_out_capacity_before_search(self):
        """Even maximum refill cannot cover more than cap at one tick."""
        demands = (Demand(0, 3), Demand(0, 4), Demand(4, 5))
        grid = minimum_full_pass_grid(demands, [6, 7, 8, 12])
        self.assertEqual(grid["largest_same_tick_cost"], 7)
        self.assertEqual(grid["policies"], [
            {"capacity": 6, "minimum_refill": None},
            {"capacity": 7, "minimum_refill": 2},
            {"capacity": 8, "minimum_refill": 1},
            {"capacity": 12, "minimum_refill": 0},
        ])

    def test_sweep_filters_invalid_and_duplicate_configurations(self):
        """A deterministic candidate grid lists each legal pair exactly once."""
        demand = (Demand(0, 3), Demand(1, 3))
        candidates = candidate_policies([4, 2, 2], [0, 2, 2, 4, 5])
        self.assertEqual(
            candidates, (Policy(2, 0), Policy(2, 2), Policy(4, 0), Policy(4, 2), Policy(4, 4))
        )
        results = sweep(iter(demand), [4, 2], (0, 2, 4, 5))
        self.assertEqual(tuple(score.policy for score in results), candidates)
        self.assertEqual(len(results), len(candidates))
        self.assertEqual(frontier([]), [])
        for caps, slopes in (([], [0]), ([3], []), ([0], [0]), ([3], [-1]), ([3], [4])):
            with self.assertRaises(ValueError):
                candidate_policies(caps, slopes)

    def test_policy_frontier_keeps_only_nondominated_alternatives(self):
        """A stronger cap or rate that buys no benefit is omitted."""
        traffic = (Demand(0, 2), Demand(1, 2))
        scores = sweep(traffic, [2, 3, 4, 5], [0, 1, 2, 3])
        best = frontier(scores)
        for point in scores:
            self.assertEqual(point in best, not any(dominant(other, point) for other in scores))
        labels = [score.policy for score in best if score.limited == 0]
        self.assertEqual(labels, [Policy(2, 2), Policy(3, 1), Policy(4, 0)])


class EnvelopePropertiesTest(unittest.TestCase):
    """Cross-check efficient scans and binary search with slower direct methods."""

    def test_linear_envelope_matches_every_contiguous_subinterval(self):
        """The returned witness must reproduce the worst required capacity."""
        for seed in range(32):
            traffic = generated_demands(seed, 24)
            for rate in range(10):
                witness = find_capacity(traffic, rate)
                self.assertEqual(witness.required_capacity, full_interval_bound(traffic, rate))
                chosen = traffic[witness.first_index:witness.last_index + 1]
                self.assertEqual(witness.cost, sum(item.cost for item in chosen))
                self.assertEqual(witness.requests, len(chosen))
                self.assertEqual(witness.first_tick, chosen[0].tick)
                self.assertEqual(witness.last_tick, chosen[-1].tick)
                self.assertEqual(witness.refill, rate * (witness.last_tick - witness.first_tick))
                self.assertEqual(witness.required_capacity, witness.cost - witness.refill)

    def test_minimum_rate_and_capacity_agree_with_exhaustive_replay(self):
        """Probe both independent full-pass algorithms around their exact cutoffs."""
        for seed in range(25):
            traffic = generated_demands(seed, 17)
            for cap in range(1, 25):
                passing = [rate for rate in range(cap + 1) if accepts_all(Policy(cap, rate), traffic)]
                expected = passing[0] if passing else None
                self.assertEqual(minimum_refill(cap, traffic), expected)
                if expected is not None:
                    self.assertLessEqual(find_capacity(traffic, expected).required_capacity, cap)
                    if expected > 0:
                        self.assertGreater(find_capacity(traffic, expected - 1).required_capacity, cap)
            for rate in range(8):
                bound = find_capacity(traffic, rate).required_capacity
                capacity = max(1, rate, bound)
                self.assertTrue(accepts_all(Policy(capacity, rate), traffic))
                if capacity > max(rate, 1):
                    self.assertFalse(accepts_all(Policy(capacity - 1, rate), traffic))

    def test_peak_window_matches_quadratic_reference(self):
        """Check windows at a single tick and after all bursts fit inside."""
        for seed in range(24):
            traffic = generated_demands(seed, 30)
            for window in (1, 2, 3, 8, 20, 120):
                witness = max_cost_in_period(traffic, window)
                self.assertEqual(witness.cost, brute_period(traffic, window))
                self.assertLess(witness.last_tick - witness.first_tick, window)

    def test_grid_exposes_witnesses_and_bounds_in_exact_order(self):
        """Keep geometry fit independent from the mathematically required capacity."""
        demand = (Demand(0, 3), Demand(0, 1), Demand(1, 3), Demand(5, 1))
        result = capacity_grid(demand, [5, 0, 3, 5], max_capacity=4)
        self.assertEqual([row["rate"] for row in result], [0, 3, 5])
        self.assertEqual([row["minimum_configured_capacity"] for row in result], [8, 4, 5])
        self.assertEqual([row["fits_token_width"] for row in result], [False, True, False])
        self.assertEqual(result[1]["witness"]["required_capacity"], 4)
        peaks = period_grid(demand, [1, 2, 6])
        self.assertEqual([row["maximum_cost"] for row in peaks], [4, 7, 8])

    def test_empty_invalid_and_backwards_envelope_inputs(self):
        """Reject invalid parameters even when there is no traffic."""
        self.assertIsNone(find_capacity([], 0))
        self.assertIsNone(max_cost_in_period([], 1))
        self.assertIsNone(capacity_grid([], [0])[0]["witness"])
        self.assertEqual(period_grid([], [5])[0]["maximum_cost"], 0)
        with self.assertRaisesRegex(ValueError, "duration"):
            max_cost_in_period([], 0)
        with self.assertRaisesRegex(ValueError, "rate"):
            find_capacity([], -1)
        with self.assertRaisesRegex(ValueError, "nondecreasing"):
            find_capacity([Demand(2, 1), Demand(1, 1)], 1)
        with self.assertRaisesRegex(ValueError, "max_capacity"):
            capacity_grid([], [0], max_capacity=-1)
        with self.assertRaisesRegex(ValueError, "at least one"):
            capacity_grid([], [])


if __name__ == "__main__":
    unittest.main()
