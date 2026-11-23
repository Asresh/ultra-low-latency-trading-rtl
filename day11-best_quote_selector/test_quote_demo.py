"""Protect the self-contained beginner walkthrough and its known results."""

from dataclasses import replace
import unittest

from quote_capture import QueryCapture, join_cycles
from quote_demo import EXPECTED, _check_named_queries, _schedule_report, example_inputs
from quote_model import Machine
from quote_verify import TraceError


class Walkthrough(unittest.TestCase):
    def fixture(self):
        """Build the default example and produce independent expected transfers."""
        profile, aliases, normalized, events, replay = example_inputs()
        model = Machine(profile)
        records = tuple(join_cycles(replay.drives, (model.step(row) for row in replay.drives)))
        return profile, aliases, normalized, events, replay, records

    def test_sample_metadata(self):
        """Check catalog names, reserve origin, and exact date conversion."""
        profile, aliases, normalized, events, replay, _ = self.fixture()
        self.assertEqual(aliases["venues"], {"EAST": 0, "WEST": 1})
        self.assertEqual(normalized.origin_ns, 1000)
        self.assertEqual(normalized.first_cycle, 0)
        self.assertEqual(normalized.last_cycle, 10)
        self.assertFalse(normalized.injected_reset)
        self.assertEqual(normalized.collocated_data_rows, 3)
        self.assertEqual(len(events), 14)
        report = _schedule_report(profile, normalized, replay)
        self.assertEqual(len(report["accepted"]), 13)
        self.assertTrue(all(entry["delay"] == 0 for entry in report["accepted"]))

    def test_fixed_queries_are_independent_of_predicted_bus(self):
        """Keep all eight static expectations matched to the sample CSV."""
        *_, observed = self.fixture()
        self.assertEqual(_check_named_queries(observed), EXPECTED)

    def test_changed_bid_in_example_fails_readable_check(self):
        """Check that a capture mismatch reports a precise failing tag."""
        *_, observed = self.fixture()
        altered = list(observed)
        index = next(
            i for i, row in enumerate(altered)
            if isinstance(row, QueryCapture) and row.result.tag == 14
        )
        crossed = altered[index].result
        new_bid = replace(crossed.bid, price=crossed.bid.price - 1)
        altered[index] = replace(altered[index], result=replace(crossed, bid=new_bid))
        with self.assertRaisesRegex(TraceError, "named example changed: .*14"):
            _check_named_queries(altered)

    def test_duplicate_observation_can_never_be_hidden_by_a_dictionary(self):
        """Detect duplicate query results before comparing by unique example tags."""
        *_, observed = self.fixture()
        with self.assertRaisesRegex(TraceError, "duplicate example tag 18"):
            _check_named_queries((*observed, observed[-1]))


if __name__ == "__main__":
    unittest.main()
