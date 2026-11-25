"""Inspect a synthetic bidirectional Soup/OUCH transcript without a network connection."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from math import ceil
from pathlib import Path
from typing import Iterable, Iterator

from ouch_model import Command, CommandKind, Notice, NoticeKind, Pending, Session
from ouch_protocol import (
    Direction,
    ClientOrder,
    Envelope,
    Login,
    SoupStream,
    Status,
    StreamDecodeError,
    decode_client,
    unframe,
)


@dataclass(frozen=True)
class CaptureEntry:
    time_ns: int
    kind: str
    fragment: bytes = b""
    up: bool | None = None
    label: str = ""

    @classmethod
    def load(cls, row: object) -> CaptureEntry:
        """Validate external capture fields and build a normalized typed record."""
        if not isinstance(row, dict):
            raise ValueError("expect a JSON object")
        t = row.get("t")
        kind = row.get("kind")
        if type(t) is not int or not 0 <= t < (1 << 64):
            raise ValueError("t must be an unsigned 64-bit nanosecond timestamp")
        if kind in ("rx", "tx"):
            if set(row) != {"t", "kind", "hex"} or not isinstance(row.get("hex"), str):
                raise ValueError("rx/tx records require exactly t, kind and hex")
            binary = bytes.fromhex(row["hex"])
            if not binary:
                raise ValueError("an empty fragment has no transcript meaning")
            return cls(t, kind, fragment=binary)
        if kind == "link":
            if set(row) != {"t", "kind", "up"} or type(row.get("up")) is not bool:
                raise ValueError("link records require exactly t, kind and boolean up")
            return cls(t, kind, up=row["up"])
        if kind == "mark":
            if set(row) != {"t", "kind", "label"} or not isinstance(row.get("label"), str):
                raise ValueError("mark records require exactly t, kind and label")
            if not row["label"]:
                raise ValueError("marker label must be nonempty")
            return cls(t, kind, label=row["label"])
        raise ValueError(f"unknown transcript entry kind {kind!r}")


def load_json_lines(path: Path) -> Iterator[CaptureEntry]:
    """Yield parsed capture records and report malformed rows with line context."""
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                yield CaptureEntry.load(json.loads(line))
            except (ValueError, TypeError) as error:
                raise ValueError(f"{path}:{number}: {error}") from error


@dataclass(frozen=True)
class AuditRow:
    time_ns: int
    epoch: int
    direction: str
    offset: int
    operation: str
    ref: int = 0
    seq: int = 0
    status: str = "OK"
    note: str = ""


@dataclass
class RoundTrip:
    first_submit_ns: int
    last_submit_ns: int
    attempts: int = 1


@dataclass
class Mark:
    time_ns: int
    label: str
    sequence: int
    live: int
    pending: int
    phase: str


class TranscriptError(RuntimeError):
    pass


class TranscriptAuditor:
    """Track externally observable sequencing and order state across captures."""

    def __init__(self, slots: int = 8, replay_depth: int = 0):
        """Create independent client/venue framers, a session oracle and audit logs."""
        self.session = Session(slots=slots, tx_ticks=0, rx_ticks=0, replay_depth=replay_depth)
        self.parsers = {direction: SoupStream() for direction in Direction}
        self.rows: list[AuditRow] = []
        self.marks: list[Mark] = []
        self.counts: Counter[str] = Counter()
        self.epoch = 0
        self.last_time = 0
        self.tx_tag = 0
        self.unanswered: dict[int, RoundTrip] = {}
        self.admission_ns: list[int] = []
        self.last_attempt_ns: list[int] = []
        self.shutdown_required = False

    @staticmethod
    def _direction_name(direction: Direction) -> str:
        """Convert the capture direction code to its displayed protocol endpoint."""
        return "tx" if direction == Direction.HOST_TO_PEER else "rx"

    def _record(
        self,
        t: int,
        direction: Direction,
        offset: int,
        op: str,
        ref: int = 0,
        seq: int = 0,
        status: Status = Status.OK,
        note: str = "",
    ) -> None:
        """Append a timestamped diagnosis with offsets and protocol identity."""
        self.rows.append(AuditRow(t, self.epoch, self._direction_name(direction), offset,
                                  op, ref, seq, Status(status).name, note))
        self.counts[f"{self._direction_name(direction)}:{op}"] += 1
        if status != Status.OK:
            self.counts[f"error:{Status(status).name}"] += 1

    def _link(self, up: bool, time_ns: int) -> None:
        """Update transport framing and model state at the captured transition."""
        if up == self.session.up:
            raise TranscriptError(f"duplicate link {'up' if up else 'down'} at {time_ns} ns")
        if not up:
            for direction, parser in self.parsers.items():
                if parser.partial:
                    self._record(time_ns, direction, parser.offset, "socket_cut",
                                 note=f"{len(parser.partial)} unfinished frame bytes discarded")
                    self.counts["partial_bytes_discarded"] += len(parser.partial)
        self.session.link(up)
        self.shutdown_required = False
        if up:
            self.epoch += 1
            self.parsers = {direction: SoupStream() for direction in Direction}

    def _choose_slot(self, order: ClientOrder) -> int:
        """Find the effective order slot for the captured operation."""
        target = order.prev if order.kind == "U" else order.ref
        if order.kind != "O":
            for slot, state in self.session.store.orders.items():
                if state.live and state.ref == target:
                    return slot
            raise TranscriptError(f"{order.kind}: {target} is not a live order")
        for slot in range(self.session.capacity):
            state = self.session.store.view(slot)
            if not state.live and state.pending == Pending.NONE:
                return slot
        raise TranscriptError("serialized entry exceeds configured capture order-slot capacity")

    def _existing_retry(self, order: ClientOrder, frame: bytes) -> int | None:
        """Recognize an exact pending request retransmission without creating a new identity."""
        for slot, state in self.session.store.orders.items():
            pending = state.outstanding
            if pending is None or pending.ref != order.ref:
                continue
            if pending.client_message().encode() == frame:
                return slot
            # A live order's cancel uses the old reference, so distinguish a
            # pending cancel from an unrelated enter or replacement encoding.
            pending_kind = {
                Pending.ENTER: "O",
                Pending.REPLACE: "U",
                Pending.CANCEL: "X",
            }[state.pending]
            if pending_kind == order.kind:
                raise TranscriptError(f"changed bytes for pending {order.kind} reference {order.ref}")
        return None

    def _outbound_order(self, t: int, offset: int, wire: ClientOrder, raw_frame: bytes) -> None:
        """Decode a captured client command and reconcile it with the expected order ledger."""
        self.tx_tag = (self.tx_tag + 1) & 0xFFFF
        retry_slot = self._existing_retry(wire, raw_frame)
        if retry_slot is not None:
            cmd = Command(CommandKind.RETRY, retry_slot, self.tx_tag, wire.ref)
        else:
            slot = self._choose_slot(wire)
            operations = {"O": CommandKind.ENTER, "U": CommandKind.REPLACE, "X": CommandKind.CANCEL}
            cmd = Command(
                operations[wire.kind], slot, self.tx_tag, wire.ref, wire.prev,
                wire.quantity, wire.price, wire.symbol, wire.side, wire.tif, wire.display, wire.client_id,
            )
        notice, expected = self.session.command(cmd)
        if notice.status != Status.OK:
            raise TranscriptError(f"unexpected transmitted {wire.kind} ref={wire.ref}: {Status(notice.status).name}")
        if expected != raw_frame:
            raise TranscriptError(f"{wire.kind} ref={wire.ref} uses encoding outside the FPGA subset")
        if wire.kind != "X":
            if retry_slot is not None:
                pending = self.unanswered.get(wire.ref)
                if pending is None:
                    raise TranscriptError(f"attempt bookkeeping lost retry reference {wire.ref}")
                pending.last_submit_ns = t
                pending.attempts += 1
            else:
                self.unanswered[wire.ref] = RoundTrip(t, t)
        self._record(t, Direction.HOST_TO_PEER, offset, wire.kind, wire.ref,
                     note="journal retry" if retry_slot is not None else "command")

    def _from_client(self, frame: Envelope, t: int, offset: int) -> None:
        """Apply a full client frame to the audit session."""
        kind, decoded = decode_client(frame)
        if kind == "L":
            assert isinstance(decoded, Login)
            notice, encoded = self.session.request_login(decoded)
            if notice is not None:
                raise TranscriptError(f"transmitted login rejected by model: {Status(notice.status).name}")
            if encoded != frame.frame():
                raise TranscriptError("login request uses encoding outside the FPGA subset")
            self._record(t, Direction.HOST_TO_PEER, offset, kind, seq=decoded.sequence,
                         note="credentials omitted from report")
        elif kind == "U":
            assert isinstance(decoded, ClientOrder)
            self._outbound_order(t, offset, decoded, frame.frame())
        elif kind == "O":
            self.session.logout()
            self._record(t, Direction.HOST_TO_PEER, offset, kind)
        else:
            if not self.session.up or self.session.request is None or self.session.logged_out:
                raise TranscriptError("client heartbeat outside an open login")
            self.session.metrics.add("client_heartbeats")
            self._record(t, Direction.HOST_TO_PEER, offset, kind)

    def _completed(self, notice: Notice, time_ns: int) -> None:
        """Collect completion latency and reference information for an accepted notice."""
        if notice.kind not in {NoticeKind.ACCEPT, NoticeKind.REPLACE, NoticeKind.REJECT}:
            return
        started = self.unanswered.pop(notice.ref, None)
        if started is None:
            return
        self.admission_ns.append(time_ns - started.first_submit_ns)
        self.last_attempt_ns.append(time_ns - started.last_submit_ns)
        self.counts["completed_entry_attempts"] += started.attempts

    def _prune_terminal_journals(self) -> None:
        """Remove retry bookkeeping that no longer owns a live order."""
        wanted = {
            order.outstanding.ref for order in self.session.store.orders.values()
            if order.outstanding is not None and order.outstanding.kind != CommandKind.CANCEL
        }
        for ref in sorted(self.unanswered.keys() - wanted):
            self.counts["retired_without_explicit_ack"] += 1
            del self.unanswered[ref]

    def _from_peer(self, frame: Envelope, t: int, offset: int) -> None:
        """Apply a complete venue frame and preserve its sequenced disposition."""
        last = self.session.frame(frame)
        if last is None:
            self._record(t, Direction.PEER_TO_HOST, offset, frame.kind)
            return
        kind = NoticeKind(last.kind)
        status = Status(last.status)
        if status == Status.OK:
            self._completed(last, t)
            self._prune_terminal_journals()
        if kind in {NoticeKind.FAULT, NoticeKind.LOGIN_REJECT, NoticeKind.END}:
            self.shutdown_required = True
        name = kind.name.lower()
        note = f"leaves={last.leaves} filled={last.filled}" if last.ref else ""
        self._record(t, Direction.PEER_TO_HOST, offset, name, last.ref, last.seq, status, note)

    def _fragment(self, entry: CaptureEntry) -> None:
        """Decode newly captured bytes and preserve any valid frames preceding a fault."""
        if not self.session.up:
            raise TranscriptError(f"byte fragment while transport down at {entry.time_ns}")
        if self.shutdown_required:
            raise TranscriptError("byte fragment continued after fail-closed boundary")
        direction = Direction.HOST_TO_PEER if entry.kind == "tx" else Direction.PEER_TO_HOST
        try:
            completed = self.parsers[direction].ingest(entry.fragment)
            framing_error = None
        except StreamDecodeError as error:
            completed, framing_error = error.completed, error
        for offset, frame in completed:
            if self.shutdown_required:
                raise TranscriptError("frame coalesced after fail-closed boundary")
            if direction == Direction.HOST_TO_PEER:
                self._from_client(frame, entry.time_ns, offset)
            else:
                self._from_peer(frame, entry.time_ns, offset)
        if framing_error is not None and not self.shutdown_required:
            self._record(entry.time_ns, direction, framing_error.offset or 0, "framing_fault",
                         status=framing_error.status)
            if direction == Direction.PEER_TO_HOST:
                self.session.parser_fault(framing_error.status)
            self.shutdown_required = True

    def apply(self, entry: CaptureEntry) -> None:
        """Apply one checked capture entry to the transcript state."""
        if entry.time_ns < self.last_time:
            raise TranscriptError(f"timestamp moved backward at {entry.time_ns}")
        self.last_time = entry.time_ns
        if entry.kind == "link":
            assert entry.up is not None
            self._link(entry.up, entry.time_ns)
        elif entry.kind == "mark":
            self.marks.append(Mark(entry.time_ns, entry.label, self.session.checkpoint,
                                   self.session.store.live_count, self.session.store.pending_count,
                                   self.session.state.name))
        else:
            self._fragment(entry)

    @staticmethod
    def _latency(samples: list[int]) -> dict[str, int | None]:
        """Summarize the measured distribution of sample latencies."""
        if not samples:
            return {"samples": 0, "min_ns": None, "p50_ns": None, "p95_ns": None, "max_ns": None}
        ordered = sorted(samples)
        return {
            "samples": len(ordered),
            "min_ns": ordered[0],
            "p50_ns": ordered[ceil(len(ordered) * 0.50) - 1],
            "p95_ns": ordered[ceil(len(ordered) * 0.95) - 1],
            "max_ns": ordered[-1],
        }

    def summary(self) -> dict[str, object]:
        """Return run counters, diagnostics and measured distributions as structured fields."""
        result = {
            "connections": self.epoch,
            "open_socket": self.session.up,
            "session": self.session.session.decode("ascii").strip(),
            "phase": self.session.state.name,
            "next_sequence": self.session.checkpoint,
            "high_water": self.session.store.high_water,
            "remaining_refs": sorted(
                state.ref for state in self.session.store.orders.values() if state.live
            ),
            "unanswered_refs": sorted(self.unanswered),
            "first_attempt": self._latency(self.admission_ns),
            "last_attempt": self._latency(self.last_attempt_ns),
            "counts": dict(sorted(self.counts.items())),
            "counters": asdict(self.session.metrics),
            "incomplete_capture_bytes": sum(len(p.partial) for p in self.parsers.values())
                                        if self.session.up else 0,
            "markers": [asdict(marker) for marker in self.marks],
        }
        if self.session.replay_depth:
            result["replay_verification"] = {
                "capacity": self.session.replay_depth,
                "first_covered_sequence": self.session.oldest_verifiable,
                "first_unverified_sequence": self.session.unverified,
            }
        return result


def audit_json_lines(
    entries: Iterable[CaptureEntry], slots: int = 8, replay_depth: int = 0,
) -> TranscriptAuditor:
    """Consume a complete JSONL transcript into the independent session auditor."""
    audit = TranscriptAuditor(slots, replay_depth)
    for record_index, entry in enumerate(entries, 1):
        try:
            audit.apply(entry)
        except (ValueError, RuntimeError) as error:
            raise TranscriptError(f"record {record_index}, t={entry.time_ns}: {error}") from error
    return audit


def load_word_frames(path: Path) -> Iterator[bytes]:
    """Read simulator wide-word captures as canonical framed bytes."""
    with path.open(encoding="ascii") as stream:
        for number, line in enumerate(stream, 1):
            try:
                size, text = line.split()
                size_value = int(size)
                word = int(text, 16)
                if not 1 <= size_value <= 74:
                    raise ValueError("frame length outside 1..74")
                if not 0 <= word < 1 << (size_value * 8):
                    raise ValueError("nonzero data outside frame width")
                yield word.to_bytes(size_value, "little")
            except ValueError as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc


def check_tx_capture(hardware: Path, expected: Path) -> Counter[str]:
    """Compare the hardware transmission capture with expected complete frames."""
    from itertools import zip_longest

    counts: Counter[str] = Counter()
    for index, (observed, reference) in enumerate(
        zip_longest(load_word_frames(hardware), load_word_frames(expected)), 1
    ):
        if observed is None or reference is None:
            raise TranscriptError(f"different TX packet count at record {index}")
        if observed != reference:
            raise TranscriptError(f"byte mismatch in TX packet {index}")
        envelope = unframe(observed)
        kind, decoded = decode_client(envelope)
        name = decoded.kind if isinstance(decoded, ClientOrder) else kind
        counts[name] += 1
    return counts


def parser() -> argparse.ArgumentParser:
    """Configure CLI argument handling for capture or simulator comparison."""
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(required=True, dest="command")
    dump = commands.add_parser("audit", help="compare a local JSONL capture to the session model")
    dump.add_argument("input", type=Path)
    dump.add_argument("--slots", type=int, default=8)
    dump.add_argument("--replay-depth", type=int, default=0, help="match the RTL replay history depth")
    dump.add_argument("--details", action="store_true")
    dump.add_argument("--fail-on-fault", action="store_true")
    check = commands.add_parser("check-tx", help="validate and compare word captures from simulation")
    check.add_argument("observed", type=Path)
    check.add_argument("expected", type=Path)
    return root


def main() -> int:
    """Run the requested transcript audit or wire-capture comparison."""
    args = parser().parse_args()
    try:
        if args.command == "audit":
            audit = audit_json_lines(load_json_lines(args.input), args.slots, args.replay_depth)
            result = audit.summary()
            if args.details:
                result["records"] = [asdict(row) for row in audit.rows]
            print(json.dumps(result, indent=2))
            if args.fail_on_fault and any(key.startswith("error:") for key in audit.counts):
                return 1
        else:
            counts = check_tx_capture(args.observed, args.expected)
            print(f"PASS decoded {sum(counts.values())} exact TX frames: {dict(sorted(counts.items()))}")
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"audit: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
