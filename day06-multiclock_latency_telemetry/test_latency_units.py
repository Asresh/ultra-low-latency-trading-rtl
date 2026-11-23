"""Check exact period integration across a change of rate and a wrap."""

import random
from fractions import Fraction
import unittest

from latency_reference import LIMIT64, Observation, Report, Status
from latency_units import Q32, RateEpoch, RateError, RateMap, render


class PiecewiseClock(unittest.TestCase):
    def test_three_rates_do_not_confuse_ns_with_raw_ticks(self):
        c = RateMap(
            [RateEpoch(100, 5, Q32 // 2), RateEpoch(104, 9, 0), RateEpoch(106, 4, Q32 // 4)]
        )
        self.assertEqual(c.elapsed(102, 108), Fraction("37.5"))
        e = (
            Observation(102, 3, 0, 1, False), Observation(108, 3, 0, 0, True)
        )
        converted = render(Report.pair(*e, 111), c)
        self.assertEqual(converted["duration"], 6)
        self.assertEqual(converted["duration_ns_rounded"], 38)
        self.assertEqual(converted["duration_q32"], 375 * Q32 // 10)
        self.assertEqual(c.elapsed(103, 104), Fraction("5.5"))
        self.assertEqual(c.elapsed(104, 105), 9)

    def test_wrap_origin_ordering(self):
        c = RateMap([RateEpoch(LIMIT64 - 4, 3, 0), RateEpoch(2, 10, 0)])
        self.assertEqual(c.elapsed(LIMIT64 - 2, 4), 35)
        self.assertEqual(c.elapsed(2, 2), 0)
        with self.assertRaisesRegex(RateError, "earliest"):
            c.elapsed(LIMIT64 - 6, 4)
        with self.assertRaisesRegex(RateError, "negative"):
            c.elapsed(4, 2)

    def test_invalid_epoch_shape_and_repeated_stamps(self):
        with self.assertRaisesRegex(RateError, "list"):
            RateMap.parse({})
        with self.assertRaisesRegex(RateError, "at least one"):
            RateMap.parse([])
        with self.assertRaisesRegex(RateError, "fields"):
            RateMap.parse([{"tick": 0}])
        with self.assertRaisesRegex(RateError, "integer"):
            RateMap.parse([{"tick": False, "integer_ns": 6, "fraction_ns": 0}])
        with self.assertRaisesRegex(RateError, "strictly"):
            RateMap([RateEpoch(10, 5, 0), RateEpoch(10, 4, 0)])
        with self.assertRaisesRegex(RateError, "strictly"):
            RateMap([RateEpoch(10, 5, 0), RateEpoch(9, 4, 0)])
        with self.assertRaisesRegex(RateError, "domains"):
            RateMap([RateEpoch(10, 0, 0)])
        with self.assertRaisesRegex(RateError, "between"):
            RateMap.parse([{"tick": "0x10", "integer_ns": 3, "fraction_ns": -1}])

    def test_random_intervals_against_an_expanded_schedule(self):
        rng = random.Random(323948)
        epochs = []
        expanded = []
        for _ in range(71):
            seconds = rng.randrange(1, 14)
            int_ns = rng.randrange(2, 9)
            frac_ns = rng.getrandbits(32)
            epochs.append(RateEpoch(len(expanded), int_ns, frac_ns))
            expanded.extend([Fraction(int_ns) + Fraction(frac_ns, Q32)] * seconds)
        c = RateMap(epochs)
        for _ in range(450):
            start = rng.randrange(len(expanded))
            end = rng.randrange(start, len(expanded) + 1)
            self.assertEqual(c.elapsed(start, end), sum(expanded[start:end], Fraction()))

    def test_no_delay_attached_to_nonmatch_status(self):
        c = RateMap([RateEpoch(512, 8, 0)])
        e = Report.single(Status.DUPLICATE, Observation(100, 4, 0, 0, False), 300)
        self.assertIsNone(render(e, c)["duration_ns"])


if __name__ == "__main__":
    unittest.main()
