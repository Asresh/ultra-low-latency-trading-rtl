"""Executable order-session oracle and packed trace schema.

The oracle splits transports from session and order objects. It addresses order
chains with dictionaries, while the RTL uses a bounded slot CAM and packed
state. Its outbound bytes come from Python struct formats.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import astuple, dataclass, replace
from enum import IntEnum
from typing import Iterable

from ouch_protocol import (
    BLANK_SESSION,
    MAX_PRICE,
    MAX_SEQUENCE,
    MAX_SHARES,
    ClientOrder,
    DecodeError,
    Envelope,
    Login,
    Status,
    VenueEvent,
    control,
    decode_peer,
    parse_decimal,
    valid_client,
    valid_session,
    valid_symbol,
)


class CommandKind(IntEnum):
    ENTER = 0
    REPLACE = 1
    CANCEL = 2
    RETRY = 3
    QUERY = 4


class NoticeKind(IntEnum):
    COMMAND = 1
    LOGIN = 2
    LOGIN_REJECT = 3
    REPLAY = 4
    END = 5
    FAULT = 6
    ACCEPT = 7
    REPLACE = 8
    CANCEL = 9
    EXECUTE = 10
    REJECT = 11
    CANCEL_PENDING = 12
    CANCEL_REJECT = 13
    SYSTEM = 14


class SessionState(IntEnum):
    OFFLINE = 0
    LOGIN_WAIT = 3
    ACTIVE = 5
    ENDED = 6
    FAULT = 7


class Pending(IntEnum):
    NONE = 0
    ENTER = 1
    REPLACE = 2
    CANCEL = 3


NO_SLOT = 255
MAX_U32 = (1 << 32) - 1
ORDER_EVENT_KIND = {
    "A": NoticeKind.ACCEPT,
    "U": NoticeKind.REPLACE,
    "C": NoticeKind.CANCEL,
    "E": NoticeKind.EXECUTE,
    "J": NoticeKind.REJECT,
    "P": NoticeKind.CANCEL_PENDING,
    "I": NoticeKind.CANCEL_REJECT,
}

COMMAND_WIDTHS = (3, 8, 16, 32, 32, 32, 64, 64, 8, 8, 8, 112)
LOGIN_WIDTHS = (48, 80, 80, 64, 40)
NOTICE_WIDTHS = (5, 8, 8, 16, 64, 32, 32, 32, 32, 32, 64, 64, 64, 16)
SLOT_WIDTHS = (1, 2, 32, 32, 32, 32, 64, 64, 64, 64, 112, 8, 16)
METRIC_WIDTHS = (32,) * 9
SNAPSHOT_WIDTHS = (
    3, 8, 80, 64, 1, 1, 1, 1, 32, 8, 8,
    *SLOT_WIDTHS, *METRIC_WIDTHS,
)


def integer(field: bytes | str | int) -> int:
    """Convert a protocol field to the unsigned integer used by the fixture."""
    if isinstance(field, bytes):
        return int.from_bytes(field, "big")
    if isinstance(field, str):
        if len(field) > 1:
            raise ValueError(f"expected a one-byte character, got {field!r}")
        return ord(field) if field else 0
    return int(field)


def pack(values: Iterable[bytes | str | int], widths: tuple[int, ...]) -> int:
    """Serialize the record fields in their declared bit positions."""
    fields = list(values)
    if len(fields) != len(widths):
        raise ValueError(f"packed {len(fields)} values for a {len(widths)}-field record")
    result = 0
    for offset, (raw, size) in enumerate(zip(fields, widths)):
        value = integer(raw)
        if not 0 <= value < (1 << size):
            raise ValueError(f"field {offset} value {value} does not fit {size} bits")
        result = (result << size) | value
    return result


def unpack(value: int, widths: tuple[int, ...]) -> list[int]:
    """Extract packed fields in their declared width and order."""
    if value < 0 or value.bit_length() > sum(widths):
        raise ValueError("packed value exceeds the schema width")
    parts = []
    for width in reversed(widths):
        parts.append(value & ((1 << width) - 1))
        value >>= width
    return parts[::-1]


@dataclass(frozen=True)
class Command:
    kind: int
    slot: int
    tag: int
    ref: int = 0
    prev: int = 0
    quantity: int = 0
    price: int = 0
    symbol: bytes = b""
    side: str = ""
    tif: str = ""
    display: str = ""
    client_id: bytes = b""

    def client_message(self) -> ClientOrder:
        """Return the normalized client order represented by this command."""
        if self.kind == CommandKind.ENTER:
            kind = "O"
        elif self.kind == CommandKind.REPLACE:
            kind = "U"
        elif self.kind == CommandKind.CANCEL:
            kind = "X"
        else:
            raise ValueError(f"command {self.kind} has no wire message")
        return ClientOrder(kind, self.ref, self.quantity, self.price, self.prev, self.side,
                           self.symbol, self.tif, self.display, self.client_id)

    def pack(self) -> int:
        """Serialize the record fields in their declared bit positions."""
        return pack(astuple(self), COMMAND_WIDTHS)


@dataclass(frozen=True)
class Notice:
    kind: int
    status: int = Status.OK
    slot: int = NO_SLOT
    tag: int = 0
    seq: int = 0
    ref: int = 0
    prev: int = 0
    quantity: int = 0
    leaves: int = 0
    filled: int = 0
    price: int = 0
    time: int = 0
    venue: int = 0
    reason: int = 0

    def pack(self) -> int:
        """Serialize the record fields in their declared bit positions."""
        return pack(astuple(self), NOTICE_WIDTHS)


@dataclass(frozen=True)
class Order:
    live: bool = False
    ref: int = 0
    leaves: int = 0
    total_filled: int = 0
    limit: int = 0
    symbol: bytes = b""
    venue: int = 0
    last_match: int | None = None
    ident: bytes = b""
    side: str = ""
    tag: int = 0
    outstanding: Command | None = None

    @property
    def pending(self) -> Pending:
        """Return whether an in-flight request still owns this order slot."""
        if self.outstanding is None:
            return Pending.NONE
        return Pending(self.outstanding.kind + 1)

    @property
    def next_ref(self) -> int:
        """Choose the proposed replacement reference when one is pending."""
        if self.pending == Pending.REPLACE and self.outstanding is not None:
            return self.outstanding.ref
        return 0

    def close(self) -> Order:
        """Mark the order terminal and discard pending operation metadata."""
        return replace(self, live=False, leaves=0, outstanding=None)

    def slot_fields(self) -> tuple:
        """Expose a slot's lifecycle and order fields for checkpoint comparison."""
        return (
            self.live, self.pending, self.ref, self.next_ref, self.leaves, self.total_filled,
            self.limit, self.symbol, self.venue, self.last_match or 0, self.ident, self.side, self.tag,
        )


@dataclass
class Metrics:
    socket_frames: int = 0
    decoded_events: int = 0
    replayed: int = 0
    sent_commands: int = 0
    retries: int = 0
    local_rejects: int = 0
    fault_count: int = 0
    peer_heartbeats: int = 0
    client_heartbeats: int = 0

    def add(self, name: str) -> None:
        """Increment the named saturating diagnostic counter."""
        prior = getattr(self, name)
        setattr(self, name, min(MAX_U32, prior + 1))


class OrderStore:
    def __init__(self, slots: int):
        """Initialize a bounded slot table with empty order and reference state."""
        if not 1 <= slots <= 255:
            raise ValueError("slot capacity is outside 1..255")
        self.capacity = slots
        self.orders: dict[int, Order] = {}
        self.high_water = 0

    def view(self, slot: int) -> Order:
        """Return a stable inspection of the selected order slot."""
        return self.orders.get(slot, Order())

    @property
    def live_count(self) -> int:
        """Count order slots currently eligible for live-reference lookup."""
        return sum(order.live for order in self.orders.values())

    @property
    def pending_count(self) -> int:
        """Count operations still waiting for a terminal venue response."""
        return sum(order.pending != Pending.NONE for order in self.orders.values())

    @staticmethod
    def _positive_limit(price: int) -> bool:
        """Check that a limit price is in the nonzero supported range."""
        return 0 < price <= MAX_PRICE

    @staticmethod
    def _valid_order(order: Command) -> bool:
        """Check the required price, quantity and protocol fields for a new order."""
        return (
            0 < order.price <= MAX_PRICE
            and 0 < order.quantity <= MAX_SHARES
            and order.tif in {"0", "3", "5"}
            and order.display in {"Y", "N"}
            and valid_client(order.client_id)
        )

    def _open(self, order: Order, cmd: Command) -> tuple[Status, Order]:
        """Admit an enter command while enforcing slot, reference and field checks."""
        if order.live or order.pending != Pending.NONE:
            return Status.SLOT_BUSY, order
        if cmd.ref <= self.high_water:
            return Status.STALE_REFERENCE, order
        if not self._valid_order(cmd) or not valid_symbol(cmd.symbol) or cmd.side not in {"B", "S"} or cmd.prev:
            return Status.BAD_FIELDS, order
        return Status.OK, Order(
            ref=cmd.ref, limit=cmd.price, symbol=cmd.symbol, ident=cmd.client_id,
            side=cmd.side, tag=cmd.tag, outstanding=cmd,
        )

    def _modify(self, order: Order, cmd: Command) -> tuple[Status, Order]:
        """Validate a cancel or replacement against the live order and current pending state."""
        if not order.live:
            return Status.NO_ORDER, order
        if order.pending != Pending.NONE:
            return Status.SLOT_BUSY, order
        if cmd.kind == CommandKind.REPLACE:
            if cmd.ref <= self.high_water or cmd.prev != order.ref:
                return Status.STALE_REFERENCE, order
            if not self._valid_order(cmd) or cmd.quantity <= order.total_filled:
                return Status.BAD_FIELDS, order
        elif cmd.ref != order.ref:
            return Status.STALE_REFERENCE, order
        elif cmd.quantity and not order.total_filled <= cmd.quantity < order.total_filled + order.leaves:
            return Status.BAD_FIELDS, order
        return Status.OK, replace(order, outstanding=cmd)

    @staticmethod
    def _retry(order: Order, cmd: Command, admission: Status) -> tuple[Status, bytes]:
        """Distinguish exact pending retransmissions from conflicting command retries."""
        pending = order.outstanding
        if pending is None:
            return Status.NO_RETRY, b""
        if cmd.ref != pending.ref:
            return Status.STALE_REFERENCE, b""
        if pending.kind != CommandKind.CANCEL and admission != Status.OK:
            return admission, b""
        return Status.OK, pending.client_message().encode()

    def command(
        self, cmd: Command, session_status: Status, admission_status: Status,
    ) -> tuple[Notice, bytes]:
        """Apply a local order command with the current admission and session policy."""
        order = self.view(cmd.slot)
        next_order = order
        status = session_status
        outbound = b""
        note = Notice(
            NoticeKind.COMMAND, slot=cmd.slot, tag=cmd.tag, ref=cmd.ref, prev=cmd.prev,
            quantity=cmd.quantity, price=cmd.price,
        )
        if status == Status.OK and not 0 <= cmd.slot < self.capacity:
            status = Status.BAD_SLOT
        if status == Status.OK:
            if cmd.kind == CommandKind.ENTER:
                status, next_order = self._open(order, cmd)
            elif cmd.kind in (CommandKind.REPLACE, CommandKind.CANCEL):
                status, next_order = self._modify(order, cmd)
            elif cmd.kind == CommandKind.RETRY:
                status, outbound = self._retry(order, cmd, admission_status)
            elif cmd.kind == CommandKind.QUERY:
                note = replace(
                    note, ref=order.ref, prev=order.next_ref,
                    quantity=order.outstanding.quantity if order.outstanding else 0,
                    price=order.limit, reason=(int(order.live) << 2) | order.pending,
                )
            else:
                status = Status.UNSUPPORTED_CMD
        if status == Status.OK and cmd.kind in (CommandKind.ENTER, CommandKind.REPLACE, CommandKind.CANCEL):
            self.orders[cmd.slot] = next_order
            outbound = cmd.client_message().encode()
            if cmd.kind != CommandKind.CANCEL:
                self.high_water = cmd.ref
        reply = replace(
            note, status=status, leaves=next_order.leaves, filled=next_order.total_filled,
            venue=next_order.venue,
        )
        return reply, outbound

    def _live_index(self) -> dict[int, tuple[int, Order]]:
        """Build the lookup from current venue references to order slots."""
        return {
            order.ref: (slot, order) for slot, order in self.orders.items()
            if order.live or order.pending == Pending.ENTER
        }

    def _replacement_index(self) -> dict[int, tuple[int, Order]]:
        """Build the lookup from pending replacement references to slots."""
        return {
            order.next_ref: (slot, order) for slot, order in self.orders.items()
            if order.pending == Pending.REPLACE
        }

    def _ack(self, event: VenueEvent, order: Order) -> tuple[Status, Order]:
        """Commit or reject a venue acknowledgment in the current order state."""
        pending = order.outstanding
        expected = Pending.ENTER if event.kind == "A" else Pending.REPLACE
        if order.pending != expected or pending is None or event.ref != pending.ref:
            return Status.LIFECYCLE, order
        price_ok = self._positive_limit(event.price) and (
            (order.side == "B" and event.price <= pending.price)
            or (order.side == "S" and event.price >= pending.price)
        )
        live_answer = event.state == "L" and event.quantity > 0
        dead_answer = event.state == "D"
        liability = order.total_filled + event.quantity
        if (
            not (live_answer or dead_answer)
            or event.symbol != order.symbol
            or event.side != order.side
            or event.client_id != pending.client_id
            or not price_ok
            or liability > pending.quantity
        ):
            return Status.ACK_ECHO, order
        return Status.OK, replace(
            order, live=live_answer, ref=event.ref,
            leaves=event.quantity if live_answer else 0,
            limit=event.price, venue=event.venue, ident=pending.client_id,
            tag=pending.tag, outstanding=None,
        )

    def _decrement(self, event: VenueEvent, order: Order) -> tuple[Status, Order]:
        """Apply a venue-side quantity decrement without exceeding live shares."""
        if not order.live:
            return Status.LIFECYCLE, order
        if not 0 < event.quantity <= order.leaves:
            return Status.BAD_DELTA, order
        if event.kind == "E":
            if not self._positive_limit(event.price):
                return Status.BAD_DELTA, order
            if event.venue == order.last_match:
                return Status.DUP_MATCH, order
            next_order = replace(
                order, total_filled=order.total_filled + event.quantity, last_match=event.venue,
            )
        elif order.pending == Pending.CANCEL:
            next_order = replace(order, outstanding=None)
        else:
            next_order = order
        leaves = next_order.leaves - event.quantity
        return Status.OK, replace(next_order, leaves=leaves) if leaves else next_order.close()

    @staticmethod
    def _venue_reject(event: VenueEvent, order: Order) -> tuple[Status, Order]:
        """Retire the matching in-flight order operation and preserve valid live state."""
        pending = order.outstanding
        if order.pending not in {Pending.ENTER, Pending.REPLACE} or pending is None:
            return Status.LIFECYCLE, order
        if (event.ref, event.client_id) != (pending.ref, pending.client_id):
            return Status.ACK_ECHO, order
        if order.pending == Pending.ENTER:
            return Status.OK, order.close()
        return Status.OK, replace(order, outstanding=None)

    @staticmethod
    def _cancel_status(event: VenueEvent, order: Order) -> tuple[Status, Order]:
        """Interpret whether a venue status terminates or leaves the order live."""
        if not order.live or order.pending != Pending.CANCEL:
            return Status.LIFECYCLE, order
        return Status.OK, order if event.kind == "P" else replace(order, outstanding=None)

    def event(self, event: VenueEvent, seq: int) -> Notice:
        """Apply a sequenced venue event to the order with the referenced identity."""
        if event.kind == "U":
            hit = self._live_index().get(event.prev)
        elif event.kind == "J":
            hit = self._replacement_index().get(event.ref) or self._live_index().get(event.ref)
        else:
            hit = self._live_index().get(event.ref)

        if hit is None:
            return Notice(
                NoticeKind.FAULT, Status.UNKNOWN_REFERENCE, seq=seq, ref=event.ref,
                prev=event.prev, quantity=event.quantity, price=event.price, time=event.time,
                venue=event.venue, reason=event.reason,
            )
        slot, order = hit
        pending = order.outstanding
        tag = order.tag
        if event.kind in {"U", "J", "P", "I"}:
            tag = pending.tag if pending is not None else 0
        if event.kind in {"A", "U"}:
            status, updated = self._ack(event, order)
        elif event.kind in {"C", "E"}:
            status, updated = self._decrement(event, order)
        elif event.kind == "J":
            status, updated = self._venue_reject(event, order)
        elif event.kind in {"P", "I"}:
            status, updated = self._cancel_status(event, order)
        else:
            raise ValueError(f"non-lifecycle event {event.kind}")
        if status == Status.OK:
            self.orders[slot] = updated
        return Notice(
            ORDER_EVENT_KIND[event.kind] if status == Status.OK else NoticeKind.FAULT,
            status, slot, tag, seq, event.ref, event.prev, event.quantity, updated.leaves,
            updated.total_filled, event.price, event.time, event.venue, event.reason,
        )


def pack_login(login: Login) -> int:
    """Pack a login request using the simulation interface's field widths."""
    return pack(astuple(login), LOGIN_WIDTHS)


class Session:
    """Retain acknowledged sequence, market state, and pending commands across sockets."""

    def __init__(
        self, slots: int = 8, tx_ticks: int = 4, rx_ticks: int = 7, replay_depth: int = 0,
    ):
        """Initialize the order ledger and independent link, timer and replay state."""
        if tx_ticks < 0 or rx_ticks < 0:
            raise ValueError("timer intervals must be nonnegative")
        if replay_depth < 0:
            raise ValueError("replay depth must be nonnegative")
        self.capacity = slots
        self.tx_ticks = tx_ticks
        self.rx_ticks = rx_ticks
        self.replay_depth = replay_depth
        self.reset()

    def reset(self) -> None:
        """Clear volatile state to the documented power-on defaults."""
        old_up = getattr(self, "up", False)
        self.store = OrderStore(self.capacity)
        self.metrics = Metrics()
        self.state = SessionState.OFFLINE
        self.last_fault = Status.OK
        self.checkpoint = 1
        self.socket_cursor = 1
        self.replay_history: OrderedDict[int, bytes] = OrderedDict()
        self.unverified: int | None = None
        self.login_seen = False
        self.session = BLANK_SESSION
        self.market = False
        self.killed = False
        self.up = old_up
        self.request: Login | None = None
        self.logged_out = False
        self.tx_idle = 0
        self.rx_idle = 0

    @property
    def oldest_verifiable(self) -> int:
        """Earliest retained sequence, or the next live position if empty."""
        return next(iter(self.replay_history), self.checkpoint)

    def _replay_bounds(self, sequence: int, ceiling: int) -> Status:
        """Check the exact history and any verification obligation across cuts."""
        if self.replay_depth:
            if sequence < self.oldest_verifiable:
                return Status.REPLAY_WINDOW
            if sequence > ceiling or (self.unverified is not None and sequence > self.unverified):
                return Status.REPLAY_FENCE
        return Status.OK

    @property
    def replaying(self) -> bool:
        """Report whether the transport cursor still trails the committed session cursor."""
        return self.state == SessionState.ACTIVE and not self.logged_out and self.socket_cursor != self.checkpoint

    @property
    def close_socket(self) -> bool:
        """Close the network-facing state and reset link-level timers and parsing."""
        return self.up and (self.state in (SessionState.ENDED, SessionState.FAULT) or self.logged_out)

    @property
    def admission(self) -> Status:
        """Return the highest-priority reason an order is currently blocked."""
        if self.killed:
            return Status.KILLED
        return Status.OK if self.market else Status.MARKET_CLOSED

    def link(self, up: bool) -> None:
        """Change connection state at an explicit transport boundary."""
        self.up = bool(up)
        if not self.up:
            if self.state != SessionState.ENDED:
                self.state = SessionState.OFFLINE
            self.logged_out = False
            self.request = None
            self.tx_idle = 0
            self.rx_idle = 0

    def kill(self) -> None:
        """Apply the session's one-shot kill behavior."""
        self.killed = True

    def request_login(self, request: Login) -> tuple[Notice | None, bytes]:
        """Validate the login request and stage the corresponding wire frame."""
        if not self.up or self.state != SessionState.OFFLINE:
            raise RuntimeError("login transaction would be held: socket not ready")
        try:
            request.validate()
            admissible = (
                request.session == self.session and 0 < request.sequence <= self.checkpoint
                if self.login_seen else request.sequence == 1
            )
            if not admissible:
                raise DecodeError(Status.LOGIN_TEXT, "invalid login replay window")
        except DecodeError:
            return Notice(NoticeKind.LOGIN_REJECT, Status.LOGIN_TEXT, seq=self.checkpoint), b""
        bounds = self._replay_bounds(request.sequence, self.checkpoint)
        if bounds != Status.OK:
            return Notice(NoticeKind.LOGIN_REJECT, bounds, seq=request.sequence), b""
        if self.replay_depth and request.sequence < self.checkpoint:
            self.unverified = request.sequence
        self.request = request
        self.state = SessionState.LOGIN_WAIT
        return None, request.encode()

    def command(self, cmd: Command) -> tuple[Notice, bytes]:
        """Apply a local order command with the current admission and session policy."""
        status = Status.OK
        if cmd.kind != CommandKind.QUERY:
            if not self.up or self.state != SessionState.ACTIVE or self.logged_out or self.replaying:
                status = Status.NOT_ACTIVE
            elif cmd.kind in (CommandKind.ENTER, CommandKind.REPLACE):
                status = self.admission
        notice, emitted = self.store.command(cmd, status, self.admission)
        if notice.status != Status.OK:
            self.metrics.add("local_rejects")
        elif cmd.kind == CommandKind.RETRY:
            self.metrics.add("retries")
        elif emitted:
            self.metrics.add("sent_commands")
        if emitted:
            self.tx_idle = 0
        return notice, emitted

    def logout(self) -> bytes:
        """Request orderly logout and record the expected control response."""
        if self.state != SessionState.ACTIVE or not self.up or self.logged_out:
            raise RuntimeError("logout has no active socket")
        self.logged_out = True
        self.tx_idle = 0
        return control("O")

    def _fail(self, fault: Notice) -> Notice:
        """Latch the session fault and drop the active logical connection."""
        self.state = SessionState.FAULT
        self.last_fault = Status(fault.status)
        self.metrics.add("fault_count")
        return fault

    def parser_fault(self, status: Status) -> Notice:
        """Translate an RX framing error into the session's terminal fault handling."""
        return self._fail(Notice(NoticeKind.FAULT, status, seq=self.checkpoint))

    def _login_ack(self, payload: bytes) -> Notice:
        """Validate login acknowledgment fields and select the replay starting point."""
        if self.request is None:
            raise RuntimeError("login ack without a pending request")
        session, sequence = payload[:10], payload[10:]
        if not valid_session(session) or self.request.session not in (BLANK_SESSION, session):
            return self._fail(Notice(NoticeKind.FAULT, Status.SESSION_ID, seq=self.checkpoint))
        try:
            start = parse_decimal(sequence)
            if not start:
                raise DecodeError(Status.LOGIN_TEXT, "zero login accepted sequence")
        except DecodeError:
            return self._fail(Notice(NoticeKind.FAULT, Status.LOGIN_TEXT, seq=self.checkpoint))
        if (self.login_seen and start > self.checkpoint) or (not self.login_seen and start != self.request.sequence):
            return self._fail(Notice(NoticeKind.FAULT, Status.SEQUENCE_GAP, seq=start))
        bounds = self._replay_bounds(start, self.request.sequence)
        if bounds != Status.OK:
            return self._fail(Notice(NoticeKind.FAULT, bounds, seq=start))
        if self.replay_depth:
            self.unverified = start if start < self.checkpoint else None
        if not self.login_seen:
            self.checkpoint = start
        self.login_seen = True
        self.session = session
        self.socket_cursor = start
        self.state = SessionState.ACTIVE
        self.last_fault = Status.OK
        return Notice(NoticeKind.LOGIN, seq=start)

    def _ordered_event(self, event: VenueEvent, raw: bytes) -> Notice:
        """Suppress replayed events and require the next uncommitted sequence ID."""
        cursor = self.socket_cursor
        if cursor > self.checkpoint:
            return self._fail(Notice(NoticeKind.FAULT, Status.SEQUENCE_GAP, seq=cursor))
        if cursor == MAX_SEQUENCE:
            return self._fail(Notice(NoticeKind.FAULT, Status.SEQUENCE_OVERFLOW, seq=cursor))
        if cursor < self.checkpoint:
            if self.replay_depth and self.replay_history.get(cursor) != raw:
                return self._fail(Notice(NoticeKind.FAULT, Status.REPLAY_DIVERGED, seq=cursor))
            self.socket_cursor += 1
            if self.replay_depth:
                self.unverified = self.socket_cursor if self.socket_cursor < self.checkpoint else None
            self.metrics.add("replayed")
            return Notice(NoticeKind.REPLAY, seq=cursor)
        if event.kind == "S":
            valid = event.reason in {ord("S"), ord("E")}
            notice = Notice(
                NoticeKind.SYSTEM if valid else NoticeKind.FAULT,
                Status.OK if valid else Status.BAD_FIELDS,
                seq=cursor, time=event.time, reason=event.reason,
            )
            if valid:
                self.market = event.reason == ord("S")
        else:
            notice = self.store.event(event, cursor)
        if notice.status != Status.OK:
            return self._fail(notice)
        if self.replay_depth:
            self.replay_history[cursor] = raw
            if len(self.replay_history) > self.replay_depth:
                self.replay_history.popitem(last=False)
        self.socket_cursor += 1
        self.checkpoint += 1
        self.metrics.add("decoded_events")
        return notice

    def frame(self, envelope: Envelope) -> Notice | None:
        """Apply a received SoupBinTCP envelope and return the resulting notice."""
        if not self.up or self.state not in (SessionState.ACTIVE, SessionState.LOGIN_WAIT) or self.logged_out:
            raise RuntimeError(f"frame is outside receive phase {self.state.name}")
        self.metrics.add("socket_frames")
        self.rx_idle = 0
        try:
            kind, payload = decode_peer(envelope)
        except DecodeError as exc:
            return self._fail(Notice(NoticeKind.FAULT, exc.status, seq=self.socket_cursor))
        if kind == "+":
            return None
        if self.state == SessionState.LOGIN_WAIT:
            if kind == "A":
                assert isinstance(payload, bytes)
                return self._login_ack(payload)
            if kind == "J":
                assert isinstance(payload, bytes)
                return self._fail(
                    Notice(NoticeKind.LOGIN_REJECT, Status.LOGIN_REJECTED,
                           seq=self.checkpoint, reason=payload[0])
                )
            return self._fail(Notice(NoticeKind.FAULT, Status.LOGIN_PHASE, seq=self.checkpoint))
        if kind == "H":
            self.metrics.add("peer_heartbeats")
            return None
        if kind == "Z":
            self.state = SessionState.ENDED
            self.market = False
            return Notice(NoticeKind.END, seq=self.checkpoint)
        if kind == "S":
            assert isinstance(payload, VenueEvent)
            return self._ordered_event(payload, envelope.frame())
        return self._fail(Notice(NoticeKind.FAULT, Status.LOGIN_PHASE, seq=self.checkpoint))

    def tick(self) -> tuple[Notice | None, bytes]:
        """Advance one timebase edge or the requested number of synthetic ticks."""
        if self.state != SessionState.ACTIVE or self.logged_out:
            return None, b""
        self.rx_idle += 1
        self.tx_idle += 1
        if self.rx_ticks and self.rx_idle >= self.rx_ticks:
            return self.parser_fault(Status.RX_TIMEOUT), b""
        if self.tx_ticks and self.tx_idle >= self.tx_ticks:
            self.metrics.add("client_heartbeats")
            self.tx_idle = 0
            return None, control("R")
        return None, b""

    def snapshot(self, slot: int) -> int:
        """Return visible session, sequence and slot fields for a checkpoint."""
        values = (
            self.state, self.last_fault, self.session, self.checkpoint, self.replaying,
            self.market, self.killed, self.close_socket, self.store.high_water,
            self.store.live_count, self.store.pending_count,
            *self.store.view(slot).slot_fields(), *astuple(self.metrics),
        )
        return pack(values, SNAPSHOT_WIDTHS)
