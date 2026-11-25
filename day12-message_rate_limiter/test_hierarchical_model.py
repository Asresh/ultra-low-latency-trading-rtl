"""Policy invariants checked independently of RTL layout and simulation timing."""

from dataclasses import replace
import unittest

from hierarchical_model import Command, Geometry, Hierarchy, Op, Status
from run_hierarchical_limiter import setup


class HierarchicalPolicies(unittest.TestCase):
    def setUp(self):
        self.geometry = Geometry(3, 6, 4, 5, 2, 10)
        self.book = Hierarchy(self.geometry)
        for policy in setup(self.geometry, 14):
            self.assertEqual(self.book.apply(policy).status, Status.OK)

    def offer(self, amount, session=0, tick=14):
        return self.book.apply(Command(Op.OFFER, session=session, tick=tick, amount=amount))

    def test_rejection_is_atomic_and_critical_uses_group_reserve(self):
        first = self.offer(11)
        self.assertEqual(first.status, Status.PASS)
        self.assertEqual(tuple(row.balance for row in first.after), (14, 6, 3))
        limited = self.offer(1)
        self.assertEqual((limited.status, limited.detail), (Status.LIMITED, 2))
        self.assertEqual(tuple(row.balance for row in limited.after), limited.before)
        self.assertEqual([row.denied for row in limited.after], [0, 1, 0])
        critical = self.offer(6, session=1)
        self.assertTrue(critical.critical)
        self.assertEqual(critical.status, Status.PASS)
        self.assertEqual(tuple(row.balance for row in critical.after), (8, 0, 8))
        empty = self.offer(1, session=1)
        self.assertEqual((empty.status, empty.detail), (Status.LIMITED, 2))

    def test_priority_field_cannot_be_forged_by_an_offer(self):
        required = Command(Op.OFFER, amount=1, tick=14)
        denied = self.book.apply(replace(required, critical=True))
        self.assertEqual(denied.status, Status.BAD_FIELDS)
        self.assertFalse(denied.critical)
        self.assertEqual(self.book.apply(required).status, Status.PASS)

    def test_different_groups_share_only_port_capacity(self):
        self.assertEqual(self.offer(11).status, Status.PASS)
        self.assertEqual(self.offer(5, session=2).status, Status.PASS)
        outcome = self.offer(1, session=2)
        self.assertEqual(outcome.detail, 1)
        self.assertEqual(tuple(row.balance for row in outcome.after), (9, 12, 9))

    def test_disabled_group_blocks_without_spending_other_buckets(self):
        self.book.apply(Command(Op.CONFIG_GROUP, group=0, tick=14))
        unavailable = self.offer(6, session=1)
        self.assertEqual((unavailable.status, unavailable.detail), (Status.DISABLED, 2))
        self.assertEqual(tuple(row.balance for row in unavailable.after), (25, 0, 14))
        self.assertEqual(self.offer(6, session=3).status, Status.PASS)

    def test_stale_group_fails_before_refill_and_forward_wrap_is_valid(self):
        self.assertEqual(self.offer(7, tick=1).status, Status.PASS)
        self.book.apply(Command(Op.INSPECT_GROUP, group=0, tick=2))
        stale = self.offer(1, tick=1)
        self.assertEqual((stale.status, stale.detail), (Status.BAD_TICK, 2))
        self.assertEqual(tuple(row.anchor for row in stale.after), (1, 2, 1))
        same = self.offer(1, tick=2)
        self.assertEqual(same.status, Status.PASS)
        self.assertEqual(tuple(row.anchor for row in same.after), (2, 2, 2))
        self.assertEqual(self.offer(1, tick=10).detail, 7)

    def test_full_width_token_reserve_addition_blocks_on_overflow(self):
        self.book.apply(Command(Op.CONFIG_PORT, amount=31, rate=0, reserve=31, enable=True, tick=14))
        ordinary = self.offer(1)
        self.assertEqual((ordinary.status, ordinary.detail), (Status.LIMITED, 1))
        self.assertEqual(self.offer(1, session=1).status, Status.PASS)

    def test_session_route_move_and_configuration_counter_replacement(self):
        self.offer(1, session=1)
        moved = self.book.apply(
            Command(Op.CONFIG_SESSION, session=1, group=2, amount=9, enable=True, tick=14)
        )
        self.assertEqual(moved.before[2], 13)
        self.assertFalse(moved.critical)
        self.assertEqual(moved.after[2].admitted, 0)
        applied = self.offer(3, session=1)
        self.assertEqual((applied.group, applied.status), (2, Status.PASS))
        self.assertEqual(applied.after[1].balance, 14)
        previous_group = self.book.apply(Command(Op.INSPECT_GROUP, group=0, tick=14))
        self.assertEqual(previous_group.after[1].balance, 16)

    def test_saturation_flags_indicate_lost_counter_increments(self):
        for _ in range(3):
            result = self.offer(1)
        self.assertEqual(result.after[2].overflow, 0)
        exceeded = self.offer(1)
        self.assertEqual(exceeded.after[2].admitted, 3)
        self.assertEqual(exceeded.after[2].overflow, 1)
        for _ in range(4):
            denied = self.offer(31)
        self.assertEqual([row.denied for row in denied.after], [3, 3, 3])
        self.assertEqual(denied.after[2].overflow, 3)
        self.assertEqual(denied.detail, 7)

    def test_invalid_time_is_checked_after_fields_and_disabled_path(self):
        fields = self.book.apply(Command(Op.OFFER, session=6, tick=6))
        self.assertEqual(fields.status, Status.BAD_SESSION)
        bad_fields = self.book.apply(Command(Op.OFFER, tick=6))
        self.assertEqual(bad_fields.status, Status.BAD_FIELDS)
        invalid_group = self.book.apply(Command(Op.CONFIG_SESSION, group=3, enable=True))
        self.assertEqual(invalid_group.status, Status.BAD_GROUP)
        self.book.apply(Command(Op.CONFIG_PORT, tick=14))
        self.assertEqual(self.offer(1, tick=6).status, Status.DISABLED)

    def test_inspection_only_updates_target_and_reports_rollover(self):
        self.offer(6, session=2)
        inspected = self.book.apply(Command(Op.INSPECT_GROUP, group=1, tick=0))
        self.assertEqual(inspected.scope, 2)
        self.assertEqual(inspected.before, (0, 15, 0))
        self.assertEqual(inspected.after[1].anchor, 0)
        parent = self.book.apply(Command(Op.INSPECT_PORT, tick=14))
        self.assertEqual(parent.before[0], 19)
        self.assertEqual(parent.scope, 1)


if __name__ == "__main__":
    unittest.main()
