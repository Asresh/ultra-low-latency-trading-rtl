"""Adversarial offline capture, retry, and outcome accounting tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ouch_audit import (
    CaptureEntry,
    TranscriptAuditor,
    TranscriptError,
    audit_json_lines,
    check_tx_capture,
    load_json_lines,
    load_word_frames,
)
from ouch_model import (
    COMMAND_WIDTHS,
    NOTICE_WIDTHS,
    Command,
    CommandKind,
    NoticeKind,
    Session,
    pack,
    unpack,
)
from ouch_protocol import (
    ClientOrder,
    DecodeError,
    Envelope,
    Login,
    Status,
    VenueEvent,
    control,
    login_accepted,
    unframe,
)


SESSION_ID = b"   AUDIT95"
USERNAME = b"ACCT  "
SECRET = b"synthkey  "
SYMBOL = b"TEST    "


class Capture:
    def __init__(self):
        self.clock = 0
        self.records: list[CaptureEntry] = []

    def event(self, kind: str, delta: int = 10, **kwargs) -> int:
        self.clock += delta
        self.records.append(CaptureEntry(self.clock, kind, **kwargs))
        return self.clock

    def send(self, kind: str, frame: bytes, split: int | None = None) -> int:
        if split is None:
            return self.event(kind, fragment=frame)
        self.assert_positive_fragment(frame, split)
        self.event(kind, fragment=frame[:split])
        return self.event(kind, fragment=frame[split:])

    @staticmethod
    def assert_positive_fragment(frame: bytes, split: int) -> None:
        if not 0 < split < len(frame):
            raise ValueError("split must be interior to the frame")

    def connect(self, requested: int = 1, accepted: int = 1, initial: bool = True) -> None:
        self.event("link", up=True)
        login = Login(USERNAME, SECRET, sequence=requested)
        if not initial:
            login = replace(login, session=SESSION_ID)
        self.send("tx", login.encode(), split=2)
        self.send("rx", login_accepted(SESSION_ID, accepted), split=11)

    def disconnect(self) -> None:
        self.event("link", up=False)

    def market(self, reason: str = "S") -> bytes:
        frame = VenueEvent("S", 125000, reason=ord(reason)).frame()
        self.send("rx", frame)
        return frame


def enter(ref: int, quantity: int = 12, price: int = 7_100) -> ClientOrder:
    return ClientOrder("O", ref, quantity, price, side="B", symbol=SYMBOL, tif="0",
                       display="Y", client_id=f"N{ref:012x} ".encode("ascii"))


def accept(ref: int, quantity: int = 12, price: int = 7_100) -> bytes:
    order = enter(ref, quantity, price)
    return VenueEvent("A", 132223, order.ref, quantity=order.quantity,
                      price=order.price, symbol=order.symbol, side=order.side,
                      client_id=order.client_id, venue=0x12399F, state="L").frame()


class CaptureTests(unittest.TestCase):
    def test_split_login_replayed_prefix_and_exact_journal_retry(self):
        script = Capture()
        script.connect()
        opening = script.market()
        initial = enter(0xAA)
        first = script.send("tx", initial.encode(), split=1)
        script.event("mark", label="after first order")
        script.disconnect()
        script.connect(requested=1, accepted=1, initial=False)
        script.send("rx", opening)
        latest = script.send("tx", initial.encode())
        seen = script.send("rx", accept(initial.ref), split=18)
        changed = ClientOrder(
            "U", 0xBB, 11, 7_250, prev=initial.ref, tif="0", display="Y",
            client_id=b"R000000000187 ",
        )
        submitted = script.send("tx", changed.encode())
        rejected = script.send("rx", VenueEvent(
            "J", seen, changed.ref, client_id=changed.client_id, reason=0x24A,
        ).frame())
        script.send("rx", VenueEvent("E", 99999, initial.ref, quantity=2, price=7123,
                                     venue=13, reason=ord("A")).frame())
        script.send("tx", ClientOrder("X", initial.ref, 0).encode())
        script.send("rx", VenueEvent("C", 999999, initial.ref, quantity=10, reason=ord("U")).frame())
        script.event("mark", label="terminal")
        audit = audit_json_lines(script.records)
        summary = audit.summary()
        self.assertEqual(summary["connections"], 2)
        self.assertEqual(summary["next_sequence"], 6)
        self.assertEqual(summary["remaining_refs"], [])
        self.assertEqual(summary["unanswered_refs"], [])
        self.assertEqual(summary["counts"]["rx:replay"], 1)
        self.assertEqual(summary["counts"]["tx:O"], 2)
        self.assertEqual(summary["counts"]["completed_entry_attempts"], 3)
        self.assertEqual(summary["counters"]["retries"], 1)
        self.assertEqual(summary["counters"]["decoded_events"], 5)
        self.assertEqual(summary["first_attempt"]["max_ns"], seen - first)
        self.assertEqual(summary["first_attempt"]["min_ns"], rejected - submitted)
        self.assertEqual(summary["last_attempt"]["max_ns"], max(seen - latest, rejected - submitted))
        self.assertEqual([m.pending for m in audit.marks], [1, 0])
        serialized = json.dumps(summary) + str(audit.rows)
        self.assertNotIn(SECRET.decode("ascii").strip(), serialized)

    def test_disconnect_discards_partial_both_directions(self):
        script = Capture()
        script.connect()
        script.market()
        partial = enter(1).encode()
        script.send("tx", partial[:16])
        script.send("rx", b"\0\x1fA")
        script.disconnect()
        script.connect(requested=2, accepted=2, initial=False)
        script.send("tx", partial)
        script.send("rx", accept(1))
        audit = audit_json_lines(script.records)
        snap = audit.summary()
        self.assertEqual(snap["remaining_refs"], [1])
        self.assertEqual(snap["counts"]["partial_bytes_discarded"], 19)
        cuts = [p for p in audit.rows if p.operation == "socket_cut"]
        self.assertEqual([p.direction for p in cuts], ["tx", "rx"])
        self.assertTrue(all(p.epoch == 1 for p in cuts))

    def test_pending_identity_cannot_be_rewritten_under_same_reference(self):
        script = Capture()
        script.connect()
        script.market()
        original = enter(1)
        script.send("tx", original.encode())
        script.send("tx", replace(original, quantity=original.quantity + 1).encode())
        with self.assertRaisesRegex(TranscriptError, "changed bytes"):
            audit_json_lines(script.records)

    def test_forward_gap_rejected_while_committed_cursor_stays_anchored(self):
        script = Capture()
        script.connect()
        script.market()
        script.disconnect()
        script.connect(requested=2, accepted=3, initial=False)
        audit = audit_json_lines(script.records)
        self.assertEqual(audit.summary()["phase"], "FAULT")
        self.assertEqual(audit.summary()["next_sequence"], 2)
        self.assertTrue(audit.shutdown_required)
        self.assertEqual(audit.rows[-1].status, "SEQUENCE_GAP")
        script.send("rx", control("H"))
        with self.assertRaisesRegex(TranscriptError, "after fail-closed"):
            audit_json_lines(script.records)

    def test_late_framing_fault_in_same_fragment_keeps_earlier_deliveries(self):
        script = Capture()
        script.connect()
        script.market()
        script.send("rx", control("H") + control("+", b"info") + b"\0\0")
        audit = audit_json_lines(script.records)
        self.assertEqual(audit.session.metrics.peer_heartbeats, 1)
        self.assertEqual(audit.session.checkpoint, 2)
        self.assertEqual(audit.rows[-1].operation, "framing_fault")
        self.assertEqual(audit.rows[-1].offset, 56)
        self.assertTrue(audit.shutdown_required)

    def test_required_post_end_socket_transition(self):
        script = Capture()
        script.connect()
        script.market()
        script.send("rx", control("Z"))
        script.event("mark", label="venue ended")
        script.disconnect()
        audit = audit_json_lines(script.records)
        self.assertEqual(audit.marks[-1].phase, "ENDED")
        self.assertEqual(audit.summary()["phase"], "ENDED")
        self.assertFalse(audit.summary()["open_socket"])

    def test_snapshot_query_before_and_after_order_terminal(self):
        session = Session(slots=4)
        session.link(True)
        session.request_login(Login(USERNAME, SECRET))
        session.frame(unframe(login_accepted(SESSION_ID, 1)))
        session.frame(unframe(VenueEvent("S", 9, reason=ord("S")).frame()))
        order = enter(244)
        cmd = Command(CommandKind.ENTER, 3, 13, order.ref, 0, order.quantity,
                      order.price, order.symbol, order.side, order.tif, order.display, order.client_id)
        reply, wire = session.command(cmd)
        self.assertEqual(reply.status, Status.OK)
        self.assertEqual(wire, order.encode())
        query, _ = session.command(Command(CommandKind.QUERY, 3, 15))
        self.assertEqual(query.reason, 1)
        self.assertEqual(query.quantity, 12)
        self.assertEqual(query.ref, 244)
        session.frame(unframe(accept(244)))
        query, _ = session.command(Command(CommandKind.QUERY, 3, 16))
        self.assertEqual(query.reason, 4)
        session.frame(unframe(VenueEvent("C", 77, 244, quantity=12).frame()))
        query, _ = session.command(Command(CommandKind.QUERY, 3, 17))
        self.assertEqual(query.reason, 0)
        self.assertEqual(query.ref, 244)
        self.assertEqual(query.leaves, 0)
        self.assertEqual(query.kind, NoticeKind.COMMAND)

    def test_saturation_metrics_preserve_no_wrap(self):
        audit = TranscriptAuditor()
        audit.session.metrics.socket_frames = (1 << 32) - 1
        script = Capture()
        script.connect()
        script.market()
        for entry in script.records:
            audit.apply(entry)
        self.assertEqual(audit.summary()["counters"]["socket_frames"], (1 << 32) - 1)

    def test_moved_back_time_empty_data_and_transport_failures(self):
        invalid_sets = (
            ([CaptureEntry(1, "tx", b"1")], "transport down"),
            ([CaptureEntry(9, "link", up=True), CaptureEntry(8, "mark", label="x")],
             "backward"),
            ([CaptureEntry(0, "link", up=False)], "duplicate link"),
        )
        for rows, error in invalid_sets:
            with self.subTest(error=error):
                with self.assertRaisesRegex(TranscriptError, error):
                    audit_json_lines(rows)
        invalid_rows = [
            {"kind": "tx", "t": 1, "hex": ""},
            {"kind": "tx", "t": 1, "hex": "g0"},
            {"kind": "tx", "t": True, "hex": "00"},
            {"kind": "tx", "t": -2, "hex": "00"},
            {"kind": "rx", "t": 12, "hex": "00", "extra": 1},
            {"kind": "link", "t": 0, "up": 1},
            {"kind": "mark", "t": 0, "label": ""},
        ]
        for row in invalid_rows:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    CaptureEntry.load(row)

    def test_unanswered_capture_tombstone_and_eof_tail(self):
        script = Capture()
        script.connect()
        script.market()
        script.send("tx", enter(3).encode())
        script.send("rx", accept(3)[:14])
        result = audit_json_lines(script.records).summary()
        self.assertEqual(result["unanswered_refs"], [3])
        self.assertEqual(result["incomplete_capture_bytes"], 14)
        self.assertEqual(result["first_attempt"]["samples"], 0)

    def test_prunes_inflight_replace_when_old_reference_cancels(self):
        script = Capture()
        script.connect()
        script.market()
        script.send("tx", enter(1).encode())
        script.send("rx", accept(1))
        script.send("tx", ClientOrder("U", 2, 13, 7891, prev=1, tif="0", display="N",
                                      client_id=b"R000000000002 ").encode())
        script.send("rx", VenueEvent("C", 1256, 1, quantity=12, reason=ord("U")).frame())
        result = audit_json_lines(script.records).summary()
        self.assertEqual(result["counts"]["retired_without_explicit_ack"], 1)
        self.assertEqual(result["remaining_refs"], [])
        self.assertEqual(result["unanswered_refs"], [])


class SchemaTests(unittest.TestCase):
    def test_dynamic_capture_path_line_numbers_and_invalid_units(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trace.jsonl"
            path.write_text('\n{"t": 0, "kind": "link", "up": true}\n{"broken": true}\n',
                            encoding="utf-8")
            items = load_json_lines(path)
            self.assertEqual(next(items), CaptureEntry(0, "link", up=True))
            with self.assertRaisesRegex(ValueError, "trace.jsonl:3:"):
                next(items)

    def test_simulation_word_file_canonical_bytes(self):
        def lines(frames: list[bytes]) -> str:
            return "".join(f"{len(frame)} {int.from_bytes(frame, 'little'):x}\n" for frame in frames)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            expected = root / "expected"
            observed = root / "actual"
            frames = [Login(USERNAME, SECRET).encode(), enter(123).encode(), control("R")]
            expected.write_text(lines(frames), encoding="ascii")
            observed.write_text(lines(frames), encoding="ascii")
            self.assertEqual(check_tx_capture(observed, expected), {"L": 1, "O": 1, "R": 1})
            observed.write_text(lines(frames[:-1]), encoding="ascii")
            with self.assertRaisesRegex(TranscriptError, "different TX packet count"):
                check_tx_capture(observed, expected)
            observed.write_text(lines([*frames[:2], control("O")]), encoding="ascii")
            with self.assertRaisesRegex(TranscriptError, "byte mismatch"):
                check_tx_capture(observed, expected)
            observed.write_text("1 fff\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "outside frame"):
                list(load_word_frames(observed))
            observed.write_text(lines([Envelope("U", b"Y").frame()]), encoding="ascii")
            expected.write_text(observed.read_text(), encoding="ascii")
            with self.assertRaises(DecodeError):
                check_tx_capture(observed, expected)

    def test_packed_record_width_overflow_is_rejected(self):
        instances = [COMMAND_WIDTHS, NOTICE_WIDTHS]
        for widths in instances:
            values = [(1 << width) - 1 for width in widths]
            self.assertEqual(unpack(pack(values, widths), widths), values)
            self.assertEqual(unpack(0, widths), [0] * len(widths))
            with self.assertRaisesRegex(ValueError, "over|exceed"):
                unpack(1 << sum(widths), widths)
            for i, width in enumerate(widths):
                invalid = values.copy()
                invalid[i] = 1 << width
                with self.assertRaisesRegex(ValueError, "does not fit"):
                    pack(invalid, widths)


if __name__ == "__main__":
    unittest.main()
