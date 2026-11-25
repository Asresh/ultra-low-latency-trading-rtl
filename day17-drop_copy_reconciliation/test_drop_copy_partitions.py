"""Exercise isolation and recovery in the partition scheduler independently of RTL."""

from __future__ import annotations

import unittest

from check_drop_copy import REFERENCE
from check_partitions import Scenario, bad_addresses, incident
from drop_copy_model import Key, Status
from drop_copy_partition_model import BAD_NAMESPACE, Input, Observation, PartitionModel, Restart


class PartitionTests(unittest.TestCase):
    def setUp(self):
        self.key = Key(0, 3, 10, 77)
        self.model = PartitionModel(3, 2, wait=6, history=19)

    def observation(self, namespace: int) -> Observation:
        return Observation(namespace, (self.key, REFERENCE))

    def test_both_lanes_observe_identical_keys_without_matching_across_namespaces(self):
        staged = self.model.advance(Input(primary=self.observation(1), copy=self.observation(2)))
        self.assertEqual(staged[:2], (True, True))
        self.assertIsNone(self.model.ledgers[0].report)
        self.assertEqual(self.model.ledgers[1].pending(), 1)
        self.assertEqual(self.model.ledgers[2].pending(), 1)
        out_a = self.model.advance(Input(copy=self.observation(1)))
        out_b = self.model.advance(Input(primary=self.observation(2)))
        self.assertEqual(out_a[4:6], (1, Status.STAGED))
        self.assertEqual(out_b[4:6], (2, Status.STAGED))
        self.assertEqual([row.counts["matched"] for row in self.model.ledgers], [0, 1, 1])

    def test_arbiter_does_not_switch_a_stalled_report_to_a_new_lower_namespace(self):
        self.model.advance(Input(copy=self.observation(2), ready=False))
        first = self.model.advance(Input(primary=self.observation(0), ready=False))
        next_edge = self.model.advance(Input(primary=self.observation(1), ready=False))
        accepted = self.model.advance(Input())
        self.assertEqual(first[4:6], (2, Status.STAGED))
        self.assertEqual(first[3:13], next_edge[3:13])
        self.assertEqual(first[3:13], accepted[3:13])
        remaining = [self.model.advance(Input())[4] for _ in range(2)]
        self.assertEqual(remaining, [0, 1])

    def test_missing_then_exact_late_mate_retains_incident_until_targeted_restart(self):
        self.model.advance(Input(primary=self.observation(1)))
        for _ in range(7):
            self.model.advance(Input())
        self.assertEqual(self.model.ledgers[1].counts["missing"], 1)
        self.model.advance(Input(copy=self.observation(1)))
        self.assertEqual(self.model.ledgers[1].counts["late"], 1)
        before = self.model.advance(Input(restart=Restart(2, 1)))
        after = self.model.advance(Input())
        self.assertEqual((before[-2], after[-2]), (2, 2))
        self.model.advance(Input(restart=Restart(1, 1)))
        self.assertEqual(self.model.advance(Input())[-2], 0)

    def test_invalid_management_report_does_not_abort_pending_event(self):
        self.model.advance(Input(primary=self.observation(1)))
        self.model.advance(Input(restart=Restart(3, 99)))
        sample = self.model.advance(Input())
        self.assertEqual((sample[4], sample[5]), (3, BAD_NAMESPACE))
        self.assertEqual(self.model.ledgers[1].epoch, 0)
        self.assertEqual(self.model.ledgers[1].pending(), 1)
        self.assertEqual(self.model.bad_count, 1)

    def test_reset_discards_all_partitions_and_held_diagnostic(self):
        self.model.advance(Input(primary=self.observation(2), ready=False))
        self.model.advance(Input(restart=Restart(1, 4), ready=False))
        before = self.model.advance(Input(reset=True, ready=False))
        after = self.model.advance(Input())
        self.assertEqual(before[:4], (False, False, False, 0))
        self.assertEqual(after[3:15], (0,) * 12)
        self.assertEqual(after[-2:], (0, 0))

    def test_directed_scenarios_have_declared_report_coverage(self):
        for n in (1, 3, 5):
            s = Scenario(n, sets=4)
            incident(s)
            bad_addresses(s)
            self.assertEqual(s.coverage["status_BAD_NAMESPACE"], 3)
            self.assertEqual(s.coverage["status_PAIR_CONFLICT"], 1)
            self.assertEqual(s.coverage["status_STALE_EPOCH"], 1)
            if n > 1:
                self.assertEqual(s.coverage["status_MATCH"], 1)
            if n > 2:
                self.assertEqual(s.coverage["parallel_lanes_and_restart"], 1)


if __name__ == "__main__":
    unittest.main()
