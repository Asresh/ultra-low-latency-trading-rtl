"""Known-answer tests for atomic cohort transitions and cutoff ordering."""

from collections import Counter
from dataclasses import replace
import unittest

from auction_model import Command, Geometry, Kind, Op
from banked_auction_model import Control, ControlOp, ControlStatus, Detail, PROFILES, Profile, Wrapper
from banked_auction_verify import Fixture, open_auction, order, pressure, worked


PROFILE = Profile(Geometry(4,4,5,3,4,10),3,2,2,6,9)


class CohortSemantics(unittest.TestCase):
    def setUp(self):
        self.run = Fixture(PROFILE)
        self.run.edge(reset=True)
        for bank in range(PROFILE.banks):
            open_auction(self.run,bank)
        self.run.idle(8)

    def test_roll_back_an_arm_when_one_epoch_is_wrong(self):
        self.run.manage(Control(ControlOp.ARM,3,(1,2,1),self.run.model.clock+9,27))
        self.assertEqual(self.run.model.report.status,ControlStatus.ROLLBACK)
        self.assertEqual(self.run.model.report.detail,Detail.EPOCH<<3)
        self.assertEqual(self.run.model.report.armed,0)
        self.assertTrue(all(lane.schedule is None for lane in self.run.model.lanes))

    def test_successful_arm_serializes_conflicting_same_edge_input_only(self):
        cfg = Control(ControlOp.ARM,3,(1,1,0),self.run.model.clock+10,24)
        command = order(1,0,0,3,2,99)
        pins = self.run.edge(control=cfg,command=command,bank=0)
        self.assertFalse(pins.source_ready)
        self.assertTrue(pins.control_ready)
        self.run.command(0,command)
        self.run.command(2,order(1,0,0,3,2,100))
        self.assertEqual(self.run.model.report,None)
        self.run.drain()
        statuses = {event.result.tag:event.wrapper for _,event in self.run.events}
        self.assertEqual((statuses[99],statuses[100]),(Wrapper.ADMITTED,Wrapper.ADMITTED))

    def test_cancellation_rolls_back_for_mixed_armed_and_unarmed_selection(self):
        deadline = self.run.model.clock+20
        self.run.manage(Control(ControlOp.ARM,3,(1,1,0),deadline,28))
        self.run.manage(Control(ControlOp.CANCEL,7,(1,1,1),tag=29))
        reply = self.run.model.report
        self.assertEqual((reply.status,reply.detail,reply.armed),
                         (ControlStatus.ROLLBACK,Detail.UNARMED<<6,3))
        self.assertEqual(reply.cutoff,deadline+(deadline<<PROFILE.clock_bits))
        self.run.manage(Control(ControlOp.CANCEL,3,(1,1,1),tag=30))
        self.run.drain()
        self.assertFalse(any(event.scheduled for _,event in self.run.events))

    def test_independent_cutoff_cohorts_keep_their_own_deadlines(self):
        t0 = self.run.model.clock+10
        t1 = t0+8
        self.run.manage(Control(ControlOp.ARM,1,(1,0,0),t0,15))
        self.run.manage(Control(ControlOp.ARM,2,(0,1,0),t1,16))
        self.run.manage(Control(ControlOp.QUERY,7,tag=17))
        self.assertEqual(self.run.model.report.armed,3)
        self.assertEqual(self.run.model.report.cutoff,t0+(t1<<PROFILE.clock_bits))
        due_at = {}
        for _ in range(30):
            snapshot = self.run.edge()
            for key in range(2):
                if snapshot.due>>key&1:
                    due_at.setdefault(key,snapshot.clock)
        self.assertEqual(due_at,{0:t0,1:t1})
        terminal = Counter((event.bank,event.result.tag) for _,event in self.run.events
                           if event.scheduled and event.result.last)
        self.assertEqual(terminal,{(0,15):1,(1,16):1})

    def test_clock_saturates_and_manual_close_still_works(self):
        tiny = replace(PROFILE,clock_bits=5)
        run = Fixture(tiny)
        run.edge(reset=True)
        open_auction(run,0)
        run.idle(40)
        self.assertEqual(run.model.clock,31)
        run.manage(Control(ControlOp.ARM,1,(1,0,0),31,29))
        self.assertEqual(run.model.report.detail,Detail.PAST)
        run.command(0,Command(Op.CLOSE,1,30))
        run.drain()
        [result] = (event for _,event in run.events if event.result.tag==30 and event.result.last)
        self.assertEqual((result.result.kind,result.scheduled),(Kind.DONE,False))

    def test_external_abort_is_locked_when_scheduled_then_cancellation_releases_lock(self):
        self.run.manage(Control(ControlOp.ARM,1,(1,0,0),self.run.model.clock+12,19))
        self.run.command(0,Command(Op.ABORT,1,21))
        self.run.manage(Control(ControlOp.CANCEL,1,(1,0,0),tag=20))
        self.run.command(0,Command(Op.ABORT,1,22))
        self.run.drain()
        statuses = {event.result.tag:(event.wrapper,event.result.kind) for _,event in self.run.events}
        self.assertEqual(statuses[21],(Wrapper.LOCKED,Kind.REJECT))
        self.assertEqual(statuses[22],(Wrapper.ADMITTED,Kind.ABORTED))

    def test_output_and_management_holds_are_independent(self):
        self.run.command(0,order(1,0,0,2,7,31),output_ready=False)
        self.run.manage(Control(ControlOp.QUERY,1,tag=32))
        hold = None
        for _ in range(8):
            sample = self.run.edge(output_ready=False,control_ready=False)
            if hold is not None:
                self.assertEqual(hold,sample.report)
            hold = sample.report
        self.assertIsNotNone(hold)
        self.assertEqual((hold.control.tag,hold.status),(32,ControlStatus.OK))

    def test_worked_volume_and_cutoff_rejection_boundary(self):
        fixture = worked(PROFILES["default"])
        self.assertEqual(fixture.metrics["scheduled_terminals"],2)
        self.assertEqual(fixture.metrics["CUTOFF"],2)
        self.assertEqual(fixture.metrics["LOCKED"],1)
        self.assertGreater(fixture.metrics["cohort_edges"],0)

    def test_tight_fixture_holds_a_predeadline_offer_until_after_cutoff(self):
        fixture = pressure(Profile(PROFILES["solo"].auction,2,1,1,4,8))
        self.assertEqual(fixture.metrics["late_inputs"],1)
        self.assertEqual(fixture.metrics["CUTOFF"],1)
        self.assertGreater(fixture.metrics["blocked_inputs"],10)


if __name__ == "__main__":
    unittest.main()
