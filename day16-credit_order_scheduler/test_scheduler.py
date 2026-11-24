"""Small independent examples that pin down the executable scheduler contract."""

import unittest

from scheduler_model import Control, Drive, Geometry, Op, Order, Scheduler, Status


class SchedulerContract(unittest.TestCase):
    """Assert outcomes directly for short, explanatory management sequences."""

    def setUp(self) -> None:
        """Start each example with two lanes and three-bit balances."""
        self.scheduler = Scheduler(Geometry(2, 2, 3))

    def configure(self, key: int, capacity: int, weight: int = 1) -> None:
        """Configure one lane and check that the command has succeeded."""
        pins = self.scheduler.advance(Drive(control=Control(Op.CONFIGURE, key, capacity, weight)))
        self.assertTrue(pins.control_ready)
        self.assertEqual(self.scheduler.report.status, Status.OK)

    def inspect(self, key: int, sink_ready: bool = False):
        """Capture state after the inspect edge, including any concurrent handoff."""
        pins = self.scheduler.advance(Drive(control=Control(Op.INSPECT, key), sink_ready=sink_ready))
        self.assertTrue(pins.control_ready)
        report = self.scheduler.report
        return report.capacity, report.available, report.inflight, report.queued

    def test_offer_is_stable_when_higher_priority_appears(self) -> None:
        """Lock in a low-priority head before an earlier lane becomes eligible."""
        self.configure(0, 2)
        self.configure(1, 2)
        first = Order(1, 1, 80, 40)
        priority = Order(0, 1, 81, 41)
        self.scheduler.advance(Drive(order=first))
        pins = self.scheduler.advance(Drive(order=priority))
        self.assertEqual(pins.offer, first)
        self.assertEqual(self.scheduler.balances(), {0: (2, 2, 0), 1: (2, 2, 0)})
        for _ in range(4):
            self.assertEqual(self.scheduler.advance(Drive()).offer, first)
        self.assertEqual(self.scheduler.advance(Drive(sink_ready=True)).offer, first)
        self.assertEqual(self.scheduler.advance(Drive(sink_ready=True)).offer, priority)

    def test_oversize_head_is_lane_local(self) -> None:
        """Block younger entries behind an expensive head and drain another lane."""
        self.configure(0, 2)
        self.configure(1, 2)
        oversize, behind = Order(0, 3, 70, 51), Order(0, 1, 71, 52)
        other = Order(1, 1, 72, 53)
        for order in (oversize, behind, other):
            self.assertTrue(self.scheduler.advance(Drive(order=order)).order_ready)
        self.assertEqual(self.scheduler.advance(Drive(sink_ready=True)).offer, other)
        denied = self.scheduler.advance(Drive(order=Order(0, 1, 73, 54), sink_ready=True))
        self.assertFalse(denied.order_ready)
        self.assertIsNone(denied.offer)
        self.configure(0, 4)
        self.assertEqual(self.scheduler.advance(Drive(sink_ready=True)).offer, oversize)
        self.assertEqual(self.scheduler.advance(Drive(sink_ready=True)).offer, behind)

    def test_refund_does_not_consume_same_edge_debit(self) -> None:
        """Reject premature credit return even while a valid order is sent."""
        self.configure(0, 3)
        order = Order(0, 3, 11, 123)
        self.scheduler.advance(Drive(order=order))
        refund = Control(Op.REFUND, 0, 3)
        old = self.scheduler.advance(Drive(control=refund, sink_ready=True))
        self.assertEqual(old.offer, order)
        self.assertEqual(self.scheduler.report.status, Status.OVER_REFUND)
        self.assertEqual(self.scheduler.report.inflight, 3)
        self.assertEqual(self.scheduler.report.available, 0)
        self.scheduler.advance(Drive(control=refund))
        self.assertEqual(self.scheduler.report.status, Status.OK)
        self.assertEqual(self.scheduler.balances()[0], (3, 3, 0))
        self.scheduler.advance(Drive(control=refund))
        self.assertEqual(self.scheduler.report.status, Status.OVER_REFUND)
        self.assertEqual(self.scheduler.balances()[0], (3, 3, 0))

    def test_report_wait_does_not_gate_egress(self) -> None:
        """Keep inspecting an old snapshot while fresh traffic proceeds."""
        self.configure(0, 3)
        first = Order(0, 1, 1, 1)
        second = Order(0, 2, 2, 2)
        self.scheduler.advance(Drive(order=first, control=Control(Op.INSPECT, 0)))
        stale = self.scheduler.report
        pins = self.scheduler.advance(Drive(order=second, sink_ready=True, report_ready=False,
                                            control=Control(Op.INSPECT, 0)))
        self.assertFalse(pins.control_ready)
        self.assertEqual(pins.offer, first)
        self.assertEqual(pins.report, stale)
        pins = self.scheduler.advance(Drive(sink_ready=True, report_ready=True,
                                            control=Control(Op.INSPECT, 0)))
        self.assertTrue(pins.control_ready)
        self.assertEqual(pins.offer, second)
        self.assertEqual(pins.report, stale)
        self.assertEqual(self.scheduler.report.fields(), (2, 0, 0, 3, 0, 3, 0, 1))

    def test_debit_and_refund_on_maximum_balance(self) -> None:
        """Spend and refund concurrently without arithmetic wrap at maximum capacity."""
        self.configure(0, 7)
        early, late = Order(0, 4, 1, 1), Order(0, 3, 2, 2)
        self.scheduler.advance(Drive(order=early))
        self.scheduler.advance(Drive(order=late, sink_ready=True))
        pins = self.scheduler.advance(Drive(sink_ready=True, control=Control(Op.REFUND, 0, 4)))
        self.assertEqual(pins.offer, late)
        self.assertEqual(self.scheduler.report.fields(), (1, 0, 0, 7, 4, 3, 0, 1))
        self.scheduler.advance(Drive(control=Control(Op.REFUND, 0, 3)))
        self.assertEqual(self.inspect(0), (7, 7, 0, 0))

    def test_reset_drops_pending_report_and_order(self) -> None:
        """Gate transfers on reset and require reconfiguration after the edge."""
        self.configure(0, 3)
        self.scheduler.advance(Drive(order=Order(0, 2, 0, 5), report_ready=False))
        self.scheduler.advance(Drive(report_ready=False))
        pins = self.scheduler.advance(Drive(reset=True, sink_ready=True))
        self.assertEqual(pins.fields(), (0,) * 17)
        after = self.scheduler.advance(Drive(sink_ready=True))
        self.assertIsNone(after.offer)
        self.assertIsNone(after.report)
        self.assertEqual(self.inspect(0), (0, 0, 0, 0))

    def test_configuration_barrier_at_visible_offer(self) -> None:
        """Preserve a live offer and deny resize while credit is outstanding."""
        self.configure(0, 2)
        self.scheduler.advance(Drive(order=Order(0, 2, 12, 8)))
        resize = Control(Op.CONFIGURE, 0, 1, 6)
        self.scheduler.advance(Drive(control=resize, sink_ready=True))
        self.assertEqual(self.scheduler.report.status, Status.BUSY)
        self.assertEqual(self.scheduler.balances()[0], (2, 0, 2))
        self.scheduler.advance(Drive(control=resize))
        self.assertEqual(self.scheduler.report.status, Status.BUSY)
        self.scheduler.advance(Drive(control=Control(Op.REFUND, 0, 2)))
        self.configure(0, 1, 6)
        self.assertEqual(self.scheduler.report.weight, 6)


if __name__ == "__main__":
    unittest.main()
