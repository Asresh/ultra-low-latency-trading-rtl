"""Check exact replay semantics and the corresponding offline fault report."""

from __future__ import annotations

import unittest
from dataclasses import replace

from ouch_audit import TranscriptError, audit_json_lines
from ouch_model import NoticeKind, Session
from ouch_protocol import Envelope, Login, Status, VenueEvent, control, login_accepted, unframe
from test_ouch_audit import Capture, SECRET, SESSION_ID, USERNAME, accept, enter


class SessionReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.initial = Login(USERNAME, SECRET, sequence=1)
        self.session = Session(replay_depth=3)
        self.session.link(True)
        self.assertEqual(self.session.request_login(self.initial), (None, self.initial.encode()))
        self.apply(login_accepted(SESSION_ID, 1))
        self.frames = [
            VenueEvent("S", timestamp, reason=ord("S")).frame() for timestamp in range(1, 6)
        ]
        for frame in self.frames:
            self.apply(frame)
        self.assertEqual(self.session.oldest_verifiable, 3)
        self.assertEqual(self.session.checkpoint, 6)

    def apply(self, frame: bytes):
        return self.session.frame(unframe(frame))

    def reopen(self, start: int, expected: Status = Status.OK) -> None:
        self.session.link(False)
        self.session.link(True)
        request = replace(self.initial, session=SESSION_ID, sequence=start)
        notice, wire = self.session.request_login(request)
        if expected == Status.OK:
            self.assertIsNone(notice)
            self.assertEqual(wire, request.encode())
        else:
            self.assertEqual((notice.kind, notice.status, notice.seq, wire),
                             (NoticeKind.LOGIN_REJECT, expected, start, b""))

    def test_cut_after_request_and_regressed_valid_ack(self):
        self.reopen(4)
        self.reopen(6, Status.REPLAY_FENCE)
        self.reopen(4)
        self.assertEqual(self.apply(login_accepted(SESSION_ID, 3)).seq, 3)
        self.assertEqual(self.session.unverified, 3)
        self.assertEqual(self.apply(self.frames[2]).kind, NoticeKind.REPLAY)
        self.assertEqual(self.session.unverified, 4)
        self.reopen(5, Status.REPLAY_FENCE)
        self.reopen(4)
        self.apply(login_accepted(SESSION_ID, 4))
        self.apply(self.frames[3])
        self.apply(self.frames[4])
        self.assertIsNone(self.session.unverified)
        self.assertEqual(self.session.checkpoint, 6)

    def test_malformed_replay_precedes_byte_comparison(self):
        self.reopen(4)
        self.apply(login_accepted(SESSION_ID, 4))
        error = self.apply(Envelope("S", b"S").frame())
        self.assertEqual(error.status, Status.OUCH_LENGTH)
        self.assertEqual(self.session.unverified, 4)
        self.assertEqual(self.session.metrics.replayed, 0)
        self.reopen(2, Status.REPLAY_WINDOW)
        self.reopen(6, Status.REPLAY_FENCE)

    def test_rejected_live_event_cannot_evict_committed_history(self):
        reject = VenueEvent("S", 120, reason=0).frame()
        self.assertEqual(self.apply(reject).status, Status.BAD_FIELDS)
        self.assertEqual((self.session.checkpoint, self.session.oldest_verifiable), (6, 3))
        self.reopen(3)
        self.apply(login_accepted(SESSION_ID, 3))
        for frame in self.frames[2:]:
            self.assertEqual(self.apply(frame).status, Status.OK)
        self.assertEqual(self.session.metrics.decoded_events, 5)

    def test_reset_discards_fence_and_previous_session(self):
        self.reopen(3)
        self.apply(login_accepted(SESSION_ID, 3))
        self.apply(self.frames[3])  # valid frame at the wrong position
        self.assertIsNotNone(self.session.unverified)
        self.session.reset()
        self.assertEqual(self.session.oldest_verifiable, 1)
        self.assertIsNone(self.session.unverified)
        self.assertEqual(self.session.request_login(self.initial), (None, self.initial.encode()))
        self.apply(login_accepted(b"   SESSION", 1))
        self.assertEqual(self.apply(self.frames[0]).kind, NoticeKind.SYSTEM)


class ReplayCaptureTests(unittest.TestCase):
    def divergent_capture(self) -> Capture:
        fixture = Capture()
        fixture.connect()
        fixture.market()
        fixture.send("tx", enter(22).encode(), split=17)
        fixture.send("rx", accept(22), split=34)
        fixture.disconnect()
        fixture.connect(requested=2, accepted=2, initial=False)
        fixture.send("rx", control("H"))
        # The structure and order identity still decode, but quantity changed.
        fixture.send("rx", accept(22, quantity=11), split=31)
        return fixture

    def test_external_report_detects_divergence_at_wire_offset(self):
        fixture = self.divergent_capture()
        audit = audit_json_lines(fixture.records, replay_depth=2)
        summary = audit.summary()
        fault = audit.rows[-1]
        self.assertEqual((fault.epoch, fault.offset, fault.seq, fault.status),
                         (2, 36, 2, "REPLAY_DIVERGED"))
        self.assertEqual(summary["phase"], "FAULT")
        self.assertEqual(summary["remaining_refs"], [22])
        self.assertEqual(summary["next_sequence"], 3)
        self.assertEqual(summary["replay_verification"], {
            "capacity": 2, "first_covered_sequence": 1, "first_unverified_sequence": 2,
        })

    def test_position_mode_can_inspect_the_same_legacy_capture(self):
        audit = audit_json_lines(self.divergent_capture().records)
        self.assertEqual(audit.rows[-1].operation, "replay")
        self.assertNotIn("replay_verification", audit.summary())

    def test_following_socket_must_repeat_unverified_position(self):
        fixture = self.divergent_capture()
        fixture.disconnect()
        fixture.connect(requested=3, accepted=3, initial=False)
        with self.assertRaisesRegex(TranscriptError, "transmitted login rejected by model: REPLAY_FENCE"):
            audit_json_lines(fixture.records, replay_depth=2)

    def test_following_socket_can_supply_original_bytes(self):
        fixture = self.divergent_capture()
        fixture.disconnect()
        fixture.connect(requested=2, accepted=2, initial=False)
        fixture.send("rx", accept(22), split=14)
        audit = audit_json_lines(fixture.records, replay_depth=2)
        summary = audit.summary()
        self.assertEqual(audit.rows[-1].status, "OK")
        self.assertEqual(audit.rows[-1].operation, "replay")
        self.assertIsNone(summary["replay_verification"]["first_unverified_sequence"])
        self.assertEqual(summary["counts"]["error:REPLAY_DIVERGED"], 1)
        self.assertEqual(summary["next_sequence"], 3)


if __name__ == "__main__":
    unittest.main()
