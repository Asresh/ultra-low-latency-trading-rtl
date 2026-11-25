"""Known-answer and bounded allocation checks for reservation semantics."""

from __future__ import annotations

import unittest

from quote_reservation_model import Action, GrantCode, ReservationCode as RC, ReservingMachine
from quote_reservation_vectors import (
    Scenario,
    dispatch_race,
    exhaustion,
    expiry_and_wrap,
    idempotent_enable_preserves_consumption,
    validation_races,
)
from quote_schema import Command, PROFILES
from quote_sweep_model import SweepCode


class ReservationTests(unittest.TestCase):
    def scenario(self, profile="default", slots=3):
        return Scenario(ReservingMachine(PROFILES[profile],slots,4,3))

    def test_atomic_venue_grant_never_partially_debits(self):
        s = self.scenario()
        dispatch_race(s)
        self.assertEqual(s.machine.counters["revoked"],1)
        self.assertEqual(s.machine.counters["dispatched"],1)
        self.assertEqual(s.machine.used,{(0,0,1):9,(0,1,1):5})

    def test_global_policy_fence_and_selected_row_races(self):
        s = self.scenario()
        validation_races(s)
        self.assertEqual(s.machine.coverage["QUOTE_CHANGED"],2)
        self.assertEqual(s.machine.coverage["POLICY_CHANGED"],1)
        self.assertTrue(s.machine.coverage["BAD_TTL"])

    def test_modular_deadline_excludes_token_on_its_expiry_edge(self):
        for profile,slots in (("default",3),("solo",1)):
            with self.subTest(profile=profile):
                s = self.scenario(profile,slots)
                expiry_and_wrap(s)
                self.assertEqual(s.machine.counters["dispatched"],0)
                self.assertEqual(s.machine.counters["revoked"],1)

    def test_allocator_and_quote_generations_fail_closed_at_exhaustion(self):
        s = self.scenario("solo",1)
        exhaustion(s)
        self.assertEqual(s.machine.poisoned,{(0,0)})
        self.assertGreater(s.machine.coverage["NO_TOKEN"],s.machine.token_max)

    def test_other_instrument_quote_and_invalid_same_row_update_preserve_held_capability(self):
        s = self.scenario()
        s.reset()
        s.show(quantity=11)
        booked = s.reserve(9,all_or_none=1)
        s.offer(booked.token)
        other = Command.replace(1,0,1,(60,20),(61,20),s.next_tag())
        self.assertTrue(s.edge(command=other,grant_ready=0).applicable)
        stale = Command.replace(0,0,1,(98,5),(100,5),s.next_tag())
        self.assertTrue(s.edge(command=stale,grant_ready=1).applicable)
        self.assertEqual(s.machine.used,{(0,0,1):9})
        self.assertEqual(s.reserve(7).result.quantity,2)

    def test_held_result_is_historical_after_its_token_has_expired(self):
        s = self.scenario()
        s.reset()
        s.show(quantity=11)
        req = s.request(5)
        s.edge(query=req,ttl=3,result_ready=0)
        snapshot = s.wait_result(RC.HELD,result_ready=0)
        an_expiry = snapshot.token.birth + snapshot.token.ttl
        same = s.edge(tick=an_expiry,result_ready=0)
        self.assertEqual(same.reservation,snapshot)
        self.assertEqual(same.counts[0],0)
        self.assertFalse(same.query_ready)
        s.edge()
        status = s.offer(snapshot.token,grant_ready=1,expected=GrantCode.NOT_HELD)
        self.assertFalse(status.applicable)
        self.assertEqual(s.reserve(11).result.status,SweepCode.FILLED)

    def test_enabling_an_already_enabled_row_does_not_replenish_it(self):
        s = self.scenario()
        idempotent_enable_preserves_consumption(s)
        self.assertEqual(s.machine.used,{(0,0,1):11})
        self.assertEqual(s.machine.revisions,{(0,0):1})

    def test_offer_on_expiry_returns_revoked_without_outbound_children(self):
        s = self.scenario()
        s.reset()
        s.show()
        held = s.reserve(4,ttl=5)
        token = held.token
        action = Action(1,19,token.slot,token.generation)
        edge = s.edge(tick=token.birth+token.ttl,action=action,grant_ready=0)
        self.assertEqual(edge.counts[0],0)
        denied = s.edge(grant_ready=1)
        self.assertEqual(denied.grant.status,GrantCode.REVOKED)
        self.assertFalse(denied.applicable)
        self.assertIsNone(denied.grant.token)


if __name__ == "__main__":
    unittest.main()
