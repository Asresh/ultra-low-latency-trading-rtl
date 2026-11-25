"""Known-answer queue, fairness, expiry and accounting checks for the oracle."""

from dataclasses import replace
import unittest

from deferred_model import Control, DeferredScheduler, Geometry, Offer, Op, Status
from run_deferred_scheduler import example


G = Geometry(2,3,5,6,6,11,10)


def provision(model):
    model.step(admin=Control(Op.SET_PORT,capacity=31,rate=12,enable=True))
    for group in range(G.groups):
        model.step(admin=Control(Op.SET_GROUP,group=group,capacity=31,rate=12,enable=True))
    for session in range(G.sessions):
        model.step(admin=Control(Op.SET_SESSION,session=session,group=session%G.groups,
                                 capacity=31,rate=12,quantum=2*(session+1),enable=True,
                                 critical=session==1))
    model.step()


def retire_all(model,count):
    events = []
    for _ in range(count):
        pins = model.step()
        if pins.result is not None and not pins.result.admin:
            events.append(pins.result)
    return events


class DeferredFlow(unittest.TestCase):
    def setUp(self):
        self.book = DeferredScheduler(G)
        provision(self.book)

    def hold_result(self):
        self.book.step(admin=Control(Op.GET_PORT,tag=1023))
        self.assertIsNotNone(self.book.pending)

    def test_packet_age_counts_through_long_held_result_without_wrap(self):
        self.hold_result()
        accepted = self.book.step(ready=False,offer=Offer(0,3,2,11,15))
        self.assertTrue(accepted.source_ready)
        for _ in range((1 << G.ttl_bits)+4):
            self.assertEqual(self.book.step(ready=False).result.tag,1023)
        [message] = retire_all(self.book,10)
        self.assertEqual((message.status,message.tag,message.ttl,message.cost),(Status.EXPIRED,11,0,3))
        self.assertEqual(message.balances,(31,31,31))

    def test_mid_visit_retains_deficit_for_following_head(self):
        self.hold_result()
        for tag in (20,21,22,23,24):
            self.assertTrue(self.book.step(ready=False,offer=Offer(0,1,63,tag)).source_ready)
        for tag in (90,91,92,93,94):
            self.assertTrue(self.book.step(ready=False,offer=Offer(1,1,63,tag)).source_ready)
        messages = retire_all(self.book,40)
        self.assertEqual([m.tag for m in messages if m.session==0],[20,21,22,23,24])
        self.assertEqual([m.tag for m in messages if m.session==1],[90,91,92,93,94])
        self.assertEqual([m.session for m in messages if m.session==1][-1],1)
        visit_counts = []
        for message in messages:
            if not visit_counts or visit_counts[-1][0] != message.session:
                visit_counts.append([message.session,1])
            else:
                visit_counts[-1][1] += 1
        self.assertIn([0,2],visit_counts)
        self.assertIn([1,4],visit_counts)
        self.assertTrue(all(m.status==Status.ADMITTED for m in messages))

    def test_source_full_blocks_only_its_own_queue(self):
        self.hold_result()
        for count in range(G.depth):
            self.assertTrue(self.book.step(ready=False,offer=Offer(0,1,63,count)).source_ready)
        stuck = Offer(0,1,63,32)
        self.assertFalse(self.book.step(ready=False,offer=stuck).source_ready)
        self.assertTrue(self.book.step(ready=False,offer=Offer(1,1,63,40)).source_ready)
        for _ in range(4):
            hit = self.book.step(offer=stuck)
            if hit.source_ready:
                break
        self.assertTrue(hit.source_ready)
        self.assertEqual(hit.occupancy & ((1<<G.occupancy_bits)-1),G.depth)
        self.assertEqual(len(self.book.tenant[0].queue),G.depth)

    def test_disabled_and_permanently_unaffordable_paths_expose_scope_masks(self):
        changed = Control(Op.SET_GROUP,group=0,capacity=2,reserve=2,enable=True)
        self.book.step(admin=changed)
        self.book.step(offer=Offer(0,1,63,4))
        [late] = retire_all(self.book,8)
        self.assertEqual((late.status,late.detail),(Status.IMPOSSIBLE,2))
        self.book.step(admin=Control(Op.SET_PORT))
        self.book.step(admin=Control(Op.SET_GROUP,group=1))
        self.book.step(offer=Offer(1,1,63,6))
        [off] = retire_all(self.book,8)
        self.assertEqual((off.status,off.detail),(Status.DISABLED,3))

    def test_critical_session_keeps_its_own_hard_bucket(self):
        self.book.step(admin=Control(Op.SET_PORT,capacity=9,rate=0,reserve=9,enable=True))
        self.book.step(admin=Control(Op.SET_SESSION,session=1,group=1,capacity=4,
                                     quantum=6,enable=True,critical=True))
        self.book.step(offer=Offer(0,1,63,0x20))
        self.book.step(offer=Offer(1,5,63,0x21))
        self.book.step(offer=Offer(1,4,63,0x22))
        completed = {m.tag:m for m in retire_all(self.book,16)}
        self.assertEqual((completed[0x20].status,completed[0x20].detail),(Status.IMPOSSIBLE,1))
        self.assertEqual((completed[0x21].status,completed[0x21].detail),(Status.IMPOSSIBLE,4))
        self.assertEqual((completed[0x22].status,completed[0x22].balances[0]),(Status.ADMITTED,5))

    def test_live_session_remap_rejected_until_fifo_drains(self):
        first = self.book.step(offer=Offer(0,1,55,5), admin=Control(Op.GET_SESSION,session=0))
        self.assertTrue(first.source_ready)
        move = Control(Op.SET_SESSION,session=0,group=1,enable=True,capacity=7,quantum=2)
        self.book.step(admin=move)
        self.assertEqual(self.book.pending.status,Status.BUSY)
        [data] = retire_all(self.book,10)
        self.assertEqual(data.group,0)
        self.book.step(admin=move)
        self.assertEqual((self.book.pending.status,self.book.pending.group),(Status.OK,1))

    def test_zero_ttl_cost_and_invalid_selector_do_not_touch_data_fifo(self):
        good = Offer(0,1,8,99)
        for illegal in (replace(good,ttl=0),replace(good,cost=0),replace(good,session=G.sessions)):
            pins = self.book.step(offer=illegal)
            self.assertTrue(pins.source_ready)
            self.assertTrue(pins.rejected)
            self.assertFalse(self.book.occupancy)

    def test_example_preserves_unrelated_service_while_group_budget_exhausted(self):
        from deferred_model import PROFILES

        workload = example(PROFILES["default"])
        events = [event for _,event in workload.results if not event.admin]
        self.assertEqual([x.tag for x in events],[0xb0,0xc0,0xc1,0xb1])
        self.assertEqual([x.status for x in events],
                         [Status.ADMITTED,Status.ADMITTED,Status.ADMITTED,Status.EXPIRED])
        self.assertGreater(workload.metrics["stalled_egress"],0)

    def test_signaled_reset_cancels_both_buffered_and_held_items(self):
        self.hold_result()
        self.book.step(ready=False,offer=Offer(0,3,63,7))
        pins = self.book.step(reset=True,ready=False)
        self.assertEqual((pins.admin_ready,pins.source_ready,pins.occupancy),(False,False,1))
        self.assertIsNone(pins.result)
        self.assertFalse(self.book.occupancy)
        self.assertIsNone(self.book.pending)


if __name__ == "__main__":
    unittest.main()
