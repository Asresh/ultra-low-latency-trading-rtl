"""Check exact cash effects of merging quantity and selecting an input grid."""

from fractions import Fraction
from io import StringIO
import random
import unittest

from notional_aggregation import AggregationStudy, GroupError, GroupKey, largest_feasible, partition
from notional_batch import Order, pack_price, parse_decimal
from notional_model import Kind, Request, Rounding, U64, calculate
from notional_quantizer import GridError, build_candidate, choose, point, read_prices


class Merging(unittest.TestCase):
    def test_partition_conserves_unsigned_requests(self):
        """The final chunk takes exactly the nonzero remainder."""
        self.assertEqual(list(partition(27, 10)), [10, 10, 7])
        self.assertEqual(list(partition(20, 10)), [10, 10])
        self.assertEqual(list(partition(1, 10)), [1])
        for quantity, limit in ((0, 5), (1, 0), (1, 1 << 32), (-3, 4)):
            with self.subTest(quantity=quantity, limit=limit), self.assertRaises(GroupError):
                list(partition(quantity, limit))

    def test_fractional_quantity_aggregation_changes_rounding(self):
        """Two floor-rounded gross amounts can lose a unit that merging retains."""
        study = AggregationStudy(1)
        req = Request(1, 3, 1, 0, Rounding.FLOOR)
        for _ in range(2):
            study.add(Order("DEMO", req))
        group, = study.inspect()["groups"]
        self.assertEqual(group["separate"]["gross"], 2)
        self.assertEqual(group["aggregated"]["gross"], 3)
        self.assertEqual(group["delta_aggregated_minus_separate"], {"cash": 1, "gross": 1, "fee": 0})
        self.assertEqual(group["chunks"], [2])

    def test_exclusions_and_different_sides_are_isolated(self):
        """Only successful orders with matching quantity-independent config are batched."""
        study = AggregationStudy(0)
        for instrument, request in (
            ("X", Request(1, 20, 3, 2500)),
            ("X", Request(2, 20, 2, 2500, sell=True)),
            ("X", Request(3, 20, 4, 2500, limit=1)),
            ("X", Request(4, 20, 0)),
            ("Y", Request(4, 20, 2, 2500)),
        ):
            study.add(Order(instrument, request))
        actual = study.inspect()
        self.assertEqual(actual["input_rows"], 5)
        self.assertEqual(actual["excluded_by_status"], {"BAD_QUANTITY": 1, "LIMIT": 1})
        self.assertEqual(len(actual["groups"]), 3)
        self.assertEqual([item["separate"]["cash"] for item in actual["groups"]], [75, 30, 50])
        self.assertEqual([item["group"]["sell"] for item in actual["groups"]], [False, True, False])

    def test_overflow_splits_at_the_correct_quantity_boundary(self):
        """Two allowed requests can merge only up to the representable uint64 total."""
        price = (1 << 32) - 1
        key = GroupKey("X", price, 10000, Rounding.FLOOR, False)
        self.assertEqual(largest_feasible(key, 0, price), U64 // (price * 2))
        qty = U64 // (price * 2)
        study = AggregationStudy(0)
        for item in (qty, 10):
            study.add(Order("X", key.order(item)))
        group, = study.inspect()["groups"]
        self.assertEqual(group["chunks"], [qty, 10])
        self.assertEqual(group["aggregated"]["cash"], 2 * price * (qty + 10))

    def test_quantity_partition_oracle_for_random_cases(self):
        """Compare total fields with separate scalar oracle evaluations per chunk."""
        rng = random.Random(31114)
        for _ in range(160):
            mode = rng.randrange(3)
            sell = bool(rng.getrandbits(1))
            maximum = rng.randint(1, 22)
            price = rng.randint(1, 150)
            fee = rng.randrange(10001)
            policy = Request(38, price, 1, fee, mode, sell)
            tasks = tuple(rng.randint(1, 50) for _ in range(10))
            planner = AggregationStudy(4, maximum)
            for size in tasks:
                order = Request(policy.tag, price, size, fee, mode, sell)
                planner.add(Order("W", order))
            report, = planner.inspect(sample_chunks=900)["groups"]
            self.assertEqual(sum(report["chunks"]), sum(tasks))
            self.assertTrue(all(chunk <= maximum for chunk in report["chunks"]))
            for key, sizes in (("separate", tasks), ("aggregated", report["chunks"])):
                rows = [calculate(Request(0, price, amount, fee, mode, sell), 4) for amount in sizes]
                self.assertEqual(report[key]["fee"], sum(record.fee for record in rows))
                self.assertEqual(report[key]["gross"], sum(record.gross for record in rows))
                self.assertEqual(report[key]["cash"], sum(record.cash for record in rows))


class PriceGrid(unittest.TestCase):
    def test_exact_binary_grid_for_common_fractional_ticks(self):
        """A price ending in 0.125 requires at least three fractional bits."""
        candidates = [Fraction(129, 8), Fraction(13, 4), Fraction(6, 1)]
        plan = choose(candidates, Fraction(0), Rounding.NEAREST_EVEN)
        self.assertEqual(plan.selected.fraction_bits, 3)
        self.assertEqual(plan.selected.exact_codes, 3)
        for original in candidates:
            sample = point(original, 3, Rounding.NEAREST_EVEN)
            self.assertEqual(sample.raw, pack_price(original, 3))
            self.assertEqual(sample.represented, original)

    def test_collapsed_prices_block_an_ambiguous_grid(self):
        """Two close prices may round to one code even while their error is small."""
        values = [Fraction(49, 16), Fraction(50, 16)]
        normalized = choose(values, Fraction(1, 4), Rounding.FLOOR)
        compact = choose(values, Fraction(1, 4), Rounding.FLOOR, preserve_levels=False)
        self.assertGreater(normalized.selected.fraction_bits, compact.selected.fraction_bits)
        self.assertEqual(normalized.selected.collisions, 0)
        self.assertEqual(compact.selected.collisions, 1)

    def test_inexact_decimal_cannot_be_represented_exactly(self):
        """A base-10 fraction with denominator 5 has no exact binary layout."""
        prices = [parse_decimal("0.1"), parse_decimal("0.2")]
        self.assertIsNone(choose(prices, Fraction(), Rounding.NEAREST_EVEN).selected)
        approximate = choose(prices, Fraction(1, 1000), Rounding.NEAREST_EVEN)
        self.assertIsNotNone(approximate.selected)
        self.assertLessEqual(approximate.selected.maximum_error, Fraction(1,1000))

    def test_exact_grid_with_bit_and_overflow_limits(self):
        """Watch a formerly exact price exceed 32 bits when fraction bits increase."""
        largest = Fraction((1 << 32) - 1)
        first = build_candidate([largest], 0, Rounding.FLOOR)
        second = build_candidate([largest], 1, Rounding.FLOOR)
        self.assertEqual((first.overflow, second.overflow), (0, 1))
        self.assertEqual(second.maximum_error, Fraction())
        self.assertIsNone(choose([largest], Fraction(), Rounding.FLOOR, floor=1).selected)

    def test_price_file_deduplication_and_validation(self):
        """One repeated valid price preserves distinct-level counts and error weighting."""
        data = StringIO("# Study fixtures\n1.125\n1.1250\n4.50\n\n")
        prices = read_prices(data)
        self.assertEqual(prices, (Fraction(9,8), Fraction(9,2)))
        summary = choose(prices, Fraction(), Rounding.FLOOR).values(include_all=True)
        self.assertEqual(len(summary["alternatives"]), 32)
        self.assertEqual(summary["selected"]["positive_levels"], 2)
        with self.assertRaisesRegex(GridError, "line 2"):
            read_prices(StringIO("100\n0\n"))
        for prices, budget in (([], Fraction()), ([Fraction(1)], Fraction(-1,2))):
            with self.assertRaises(GridError):
                choose(prices, budget, Rounding.FLOOR)
