"""Generate wire and ledger comparisons for the self-checking SV testbench."""

from __future__ import annotations

import argparse
import itertools
import random
from dataclasses import dataclass, replace
from enum import IntEnum
from pathlib import Path

from ouch_model import (
    SNAPSHOT_WIDTHS,
    Command,
    CommandKind as Cmd,
    Notice,
    NoticeKind,
    Pending,
    Session,
    SessionState,
    pack_login,
)
from ouch_protocol import (
    BLANK_SESSION,
    MAX_PRICE,
    MAX_SEQUENCE,
    MAX_SHARES,
    Envelope,
    Login,
    Status,
    VenueEvent,
    control,
    login_accepted,
    unframe,
)


class Act(IntEnum):
    RESET = 0
    LINK = 1
    LOGIN = 2
    COMMAND = 3
    BYTES = 4
    TICK = 5
    KILL = 6
    LOGOUT = 7
    KEEP = 8
    BARRIER = 9
    TX_LIMIT = 10
    TX_WAIT = 11
    QUERY_HEARTBEAT = 12


SESSION = b"   SIM1234"
OTHER_SESSION = b"    OTHER1"
USER = b"DEMO  "
PASSWORD = b"wiretest  "
DEFAULT_LOGIN = Login(USER, PASSWORD)
SYMBOLS = (b"TST1    ", b"TEST.A  ", b"SIM     ", b"MOCK    ")
MAX_FRAGMENT = 250


@dataclass(frozen=True)
class Stimulus:
    action: Act
    arg_a: int = 0
    arg_b: int = 0
    payload: int = 0

    def line(self) -> str:
        """Encode this stimulus as one text row consumed by the simulation driver."""
        return f"{int(self.action)} {self.arg_a} {self.arg_b} {self.payload:x}"


class Scenario:
    def __init__(self, slots: int):
        """Initialize the session oracle, board state and reproducible output trails."""
        self.device = Session(slots=slots)
        self.actions: list[Stimulus] = []
        self.notices: list[Notice] = []
        self.packets: list[bytes] = []
        self.history: list[bytes] = []
        self.aborted: list[bytes] = []
        self.next_tag = itertools.count(1)
        self.clock = itertools.count(98_765_432_101, 170)
        self.match_id = itertools.count(0xABC0050_0000_0001)
        self.venue_id = itertools.count(0x1870_F001_A046_0001)
        self.hwm = 0

    def _action(self, action: Act, a: int = 0, b: int = 0, payload: int = 0) -> None:
        """Append one typed control or data stimulus to the simulation sequence."""
        self.actions.append(Stimulus(action, a, b, payload))

    def _check(self, reply: Notice | None, expected: Status | None = None) -> None:
        """Store a generated reply and verify any case-specific expected status."""
        if reply is not None:
            self.notices.append(reply)
        if expected is not None and (reply is None or reply.status != expected):
            raise AssertionError(f"expected {expected.name}, observed {reply}")

    def _tx(self, frame: bytes) -> None:
        """Queue one expected transmission for byte-exact comparison."""
        if frame:
            self.packets.append(frame)

    def barrier(self, slot: int = 0) -> None:
        """Record the current expected state before any later events are applied."""
        width = sum(SNAPSHOT_WIDTHS)
        self._action(Act.BARRIER, len(self.packets), len(self.notices),
                     (slot << width) | self.device.snapshot(slot))

    def reset(self) -> None:
        """Clear volatile state to the documented power-on defaults."""
        self._action(Act.RESET)
        self.device.reset()
        self.history.clear()
        self.hwm = 0

    def link(self, up: bool) -> None:
        """Change connection state at an explicit transport boundary."""
        self._action(Act.LINK, int(up))
        self.device.link(up)

    def reconnect(self, begin: int | None = None, ack: int | None = None) -> None:
        """Model transport loss, reconnection, login and the requested replay cursor."""
        self.barrier()
        self.link(False)
        self.link(True)
        if begin is None:
            begin = self.device.checkpoint
        if ack is None:
            ack = begin
        self.login(replace(DEFAULT_LOGIN, session=SESSION, sequence=begin))
        self.rx(login_accepted(SESSION, ack))
        self.barrier()

    def login(self, request: Login = DEFAULT_LOGIN, expected: Status | None = None) -> None:
        """Drive a login request and verify its response against the model."""
        self._action(Act.LOGIN, payload=pack_login(request))
        notice, sent = self.device.request_login(request)
        self._check(notice, expected)
        self._tx(sent)

    def cmd(
        self, kind: Cmd | int, slot: int, ref: int = 0, prev: int = 0,
        qty: int = 0, price: int = 0, symbol: bytes = b"", side: str = "",
        tif: str = "", display: str = "", client_id: bytes = b"",
        expected: Status | None = None,
    ) -> Notice:
        """Submit an order command and record its modeled response and wire output."""
        cmd = Command(
            kind, slot, next(self.next_tag), ref, prev, qty,
            price, symbol, side, tif, display, client_id,
        )
        self._action(Act.COMMAND, payload=cmd.pack())
        notice, frame = self.device.command(cmd)
        self._check(notice, expected)
        self._tx(frame)
        return notice

    @staticmethod
    def ident(ref: int) -> bytes:
        """Derive a deterministic client identifier from the order reference."""
        return f"J{ref:012X} ".encode("ascii")

    def allocate_ref(self) -> int:
        """Return the next unused order reference for a generated command."""
        self.hwm = max(self.hwm, self.device.store.high_water) + 1
        return self.hwm

    def enter(
        self, slot: int, qty: int = 30, price: int = 125_001, side: str = "B",
        ref: int | None = None, symbol: bytes = SYMBOLS[0], tif: str = "0",
        display: str = "Y", expected: Status = Status.OK,
    ) -> int:
        """Construct and drive a new-order request with the specified fixture fields."""
        if ref is None:
            ref = self.allocate_ref()
        if not self.hwm or ref > self.hwm:
            self.hwm = ref
        self.cmd(Cmd.ENTER, slot, ref, qty=qty, price=price, symbol=symbol, side=side,
                 tif=tif, display=display, client_id=self.ident(ref), expected=expected)
        return ref

    def replace(
        self, slot: int, qty: int, price: int, ref: int | None = None,
        prev: int | None = None, expected: Status = Status.OK,
    ) -> int:
        """Construct and drive a replace request keyed by old and new references."""
        if ref is None:
            ref = self.allocate_ref()
        if prev is None:
            prev = self.device.store.view(slot).ref
        self.cmd(Cmd.REPLACE, slot, ref, prev=prev, qty=qty, price=price, tif="3", display="N",
                 client_id=self.ident(ref), expected=expected)
        return ref

    def rx(
        self, *frames: bytes, max_keep: int = 8, expected: Status | None = None,
        remember: bool = True,
    ) -> None:
        """Send received data through the same protocol parser exercised by the simulator."""
        raw = b"".join(frames)
        if not 0 < len(raw) <= MAX_FRAGMENT:
            raise ValueError(f"unsupported RX vector group: {len(raw)} bytes")
        self._action(Act.BYTES, len(raw), max_keep, int.from_bytes(raw, "little"))
        for i, frame in enumerate(frames):
            envelope = unframe(frame)
            notice = self.device.frame(envelope)
            self._check(notice, expected if i == len(frames) - 1 else None)
            if remember and notice and envelope.kind == "S" and notice.status == Status.OK \
                    and notice.kind != NoticeKind.REPLAY:
                if notice.seq != len(self.history) + 1:
                    raise AssertionError("reference history and committed sequence disagree")
                self.history.append(frame)
        self.barrier()

    def partial(self, fragment: bytes) -> None:
        """Record a fragment without falsely treating it as a complete packet."""
        if not fragment or len(fragment) > MAX_FRAGMENT:
            raise ValueError("fragment length is invalid")
        self._action(Act.BYTES, len(fragment), 4, int.from_bytes(fragment, "little"))

    def malformed_framing(self, raw: bytes, status: Status, keep: int | None = None) -> None:
        """Supply an invalid frame or keep mask and check the parser fault."""
        if keep is None:
            self._action(Act.BYTES, len(raw), 8, int.from_bytes(raw, "little"))
        else:
            self._action(Act.KEEP, keep, payload=int.from_bytes(raw, "little"))
        self._check(self.device.parser_fault(status), status)

    def tick(self, count: int) -> None:
        """Advance one timebase edge or the requested number of synthetic ticks."""
        self._action(Act.TICK, count)
        self._expect_ticks(count)

    def _expect_ticks(self, count: int) -> None:
        """Advance the model timer and queue any resulting notices or keepalives."""
        for _ in range(count):
            notice, packet = self.device.tick()
            self._check(notice)
            self._tx(packet)

    def query_during_heartbeat(self) -> None:
        """Schedule a query while a heartbeat is due on the wire."""
        command = Command(Cmd.QUERY, 0, next(self.next_tag))
        self._action(Act.QUERY_HEARTBEAT, self.device.tx_ticks, payload=command.pack())
        self._check(self.device.command(command)[0], Status.OK)
        self._expect_ticks(self.device.tx_ticks)
        self._check(self.device.command(command)[0], Status.OK)
        self.barrier()

    def kill(self) -> None:
        """Apply the session's one-shot kill behavior."""
        self.device.kill()
        self._action(Act.KILL)

    def logout(self) -> None:
        """Request orderly logout and record the expected control response."""
        self._action(Act.LOGOUT)
        self._tx(self.device.logout())

    def hold_tx_after(self, beats: int) -> None:
        """Apply egress backpressure after a selected number of transferred beats."""
        self._action(Act.TX_LIMIT, beats)

    def cut_tcp_while_transmitting(self, prefix_bytes: int) -> None:
        """Disconnect after exactly the specified prefix of a wire frame."""
        frame = self.packets.pop()
        if not 0 < prefix_bytes < len(frame) or prefix_bytes % 8:
            raise ValueError("partial TCP test must end at a nonterminal beat")
        self.aborted.append(frame[:prefix_bytes])
        self._action(Act.TX_WAIT)
        self.link(False)
        self.hold_tx_after(-1)
        self.link(True)
        self.login(replace(DEFAULT_LOGIN, session=SESSION, sequence=self.device.checkpoint))
        self.rx(login_accepted(SESSION, self.device.checkpoint))

    def system(self, value: str = "S", expected: Status = Status.OK) -> None:
        """Drive an exchange system event and compare its resulting state."""
        self.rx(VenueEvent("S", next(self.clock), reason=ord(value)).frame(), expected=expected)

    def up(self) -> None:
        """Set the transport link active in the scenario."""
        self.reset()
        self.link(True)
        self.login()
        self.rx(control("+", b"synthetic"), login_accepted(SESSION, 1), max_keep=3)
        self.system()
        self.barrier()

    def ack(
        self, slot: int, *, quantity: int | None = None, price: int | None = None,
        state: str = "L", symbol: bytes | None = None, expected: Status = Status.OK,
    ) -> None:
        """Acknowledge the selected outstanding command with its current reference."""
        order = self.device.store.view(slot)
        cmd = order.outstanding
        if cmd is None or cmd.kind not in (Cmd.ENTER, Cmd.REPLACE):
            raise ValueError("no enter or replace pending")
        replacement = cmd.kind == Cmd.REPLACE
        quantity = cmd.quantity - order.total_filled if quantity is None else quantity
        price = cmd.price if price is None else price
        self.rx(VenueEvent(
            "U" if replacement else "A", next(self.clock), cmd.ref,
            cmd.prev if replacement else 0, quantity, price,
            order.symbol if symbol is None else symbol, cmd.client_id,
            order.side, state, next(self.venue_id),
        ).frame(), expected=expected)

    def venue_reject(self, slot: int, *, explicit: bool = False) -> None:
        """Drive a reject for the selected slot and expected reference."""
        cmd = self.device.store.view(slot).outstanding
        if cmd is None:
            raise ValueError("no command pending")
        self.rx(VenueEvent("J", next(self.clock), cmd.ref, client_id=cmd.client_id,
                           reason=0x030B, explicit_zero_appendage=explicit).frame(),
                expected=Status.OK)

    def exec(self, slot: int, qty: int, px: int, match: int | None = None,
             expected: Status = Status.OK) -> None:
        """Generate an execution report for the current order and requested price and size."""
        if match is None:
            match = next(self.match_id)
        order = self.device.store.view(slot)
        self.rx(VenueEvent("E", next(self.clock), order.ref, quantity=qty, price=px,
                           venue=match, reason=ord("A")).frame(), expected=expected)

    def event_cancel(self, slot: int, qty: int, explicit: bool = False,
                     expected: Status = Status.OK) -> None:
        """Drive a venue cancellation with optional explicit reference checking."""
        order = self.device.store.view(slot)
        self.rx(VenueEvent("C", next(self.clock), order.ref, quantity=qty,
                           reason=ord("U"), explicit_zero_appendage=explicit).frame(), expected=expected)

    def status(self, slot: int, kind: str, explicit: bool = False,
               expected: Status = Status.OK) -> None:
        """Drive an order-status report and verify the expected session notice."""
        order = self.device.store.view(slot)
        self.rx(VenueEvent(kind, next(self.clock), order.ref,
                           explicit_zero_appendage=explicit).frame(), expected=expected)

    def write(self, out: Path) -> None:
        """Write the stimulus and expected notices, frames, and checkpoints to output files."""
        out.mkdir(parents=True, exist_ok=True)
        with (out / "stimulus.txt").open("w", encoding="ascii") as stream:
            for command in self.actions:
                print(command.line(), file=stream)
        with (out / "tx.txt").open("w", encoding="ascii") as stream:
            for frame in self.packets:
                print(len(frame), f"{int.from_bytes(frame, 'little'):x}", file=stream)
        with (out / "notice.txt").open("w", encoding="ascii") as stream:
            for notice in self.notices:
                print(f"{notice.pack():x}", file=stream)
        with (out / "aborted.txt").open("w", encoding="ascii") as stream:
            for frame in self.aborted:
                print(len(frame), f"{int.from_bytes(frame, 'little'):x}", file=stream)


def directed_lifecycle(s: Scenario) -> None:
    """Exercise enter, replace, cancel, execution and terminal transitions in sequence."""
    s.reset()
    s.cmd(Cmd.QUERY, 0, expected=Status.OK)
    s.cmd(Cmd.ENTER, 0, expected=Status.NOT_ACTIVE)
    s.link(True)
    s.login(replace(DEFAULT_LOGIN, username=b" D    "), Status.LOGIN_TEXT)
    s.login(replace(DEFAULT_LOGIN, timeout=b"00000"), Status.LOGIN_TEXT)
    s.login(replace(DEFAULT_LOGIN, sequence=0), Status.LOGIN_TEXT)
    s.login()
    s.rx(control("+", b"debug wins no cursor"), login_accepted(SESSION, 1), max_keep=1)
    s.cmd(Cmd.ENTER, 0, expected=Status.MARKET_CLOSED)
    s.system()
    s.barrier()

    s.cmd(7, 0, expected=Status.UNSUPPORTED_CMD)
    s.cmd(Cmd.QUERY, 255, expected=Status.BAD_SLOT)
    s.cmd(Cmd.RETRY, 0, expected=Status.NO_RETRY)
    s.enter(0, ref=0, expected=Status.STALE_REFERENCE)
    s.enter(0, symbol=b" BAD    ", expected=Status.BAD_FIELDS)
    s.enter(0, qty=0, expected=Status.BAD_FIELDS)
    s.enter(0, qty=MAX_SHARES + 1, expected=Status.BAD_FIELDS)
    s.enter(0, price=MAX_PRICE + 1, expected=Status.BAD_FIELDS)
    s.enter(0, side="T", expected=Status.BAD_FIELDS)
    s.enter(0, tif="6", expected=Status.BAD_FIELDS)
    first = s.enter(0, qty=120, price=130_000)
    second = s.enter(1, qty=40, price=140_000, side="S")
    s.enter(1, expected=Status.SLOT_BUSY)
    s.cmd(Cmd.REPLACE, 2, expected=Status.NO_ORDER)
    s.cmd(Cmd.RETRY, 0, ref=first + 7, expected=Status.STALE_REFERENCE)
    s.cmd(Cmd.RETRY, 0, ref=first, expected=Status.OK)
    s.ack(1, state="D", price=140_100)
    s.ack(0, price=129_000)
    s.barrier(0)
    assert s.device.store.view(1).leaves == 0
    assert second > first
    s.exec(0, 23, 128_999)
    s.replace(0, 22, 132_000, expected=Status.BAD_FIELDS)
    s.cmd(Cmd.CANCEL, 0, ref=first - 1, expected=Status.STALE_REFERENCE)
    s.cmd(Cmd.CANCEL, 0, ref=first, qty=120, expected=Status.BAD_FIELDS)
    s.cmd(Cmd.CANCEL, 0, ref=first, qty=22, expected=Status.BAD_FIELDS)
    new_ref = s.replace(0, 145, 135_001)
    s.exec(0, 18, 129_500)
    s.ack(0, quantity=97)
    s.barrier()
    updated = s.device.store.view(0)
    assert (updated.ref, updated.total_filled, updated.leaves) == (new_ref, 41, 97)

    rejected = s.replace(0, 177, 146_000)
    s.venue_reject(0, explicit=True)
    s.enter(1, ref=rejected, expected=Status.STALE_REFERENCE)
    s.cmd(Cmd.CANCEL, 0, ref=new_ref, qty=60, expected=Status.OK)
    s.status(0, "P")
    s.cmd(Cmd.RETRY, 0, ref=new_ref, expected=Status.OK)
    s.status(0, "I", explicit=True)
    s.cmd(Cmd.CANCEL, 0, ref=new_ref, qty=60, expected=Status.OK)
    s.event_cancel(0, 78, explicit=True)
    s.exec(0, 19, 130_000)
    s.barrier(0)
    assert not s.device.store.view(0).live
    s.cmd(Cmd.RETRY, 0, ref=new_ref, expected=Status.NO_RETRY)

    rejected_enter = s.enter(0, side="S", display="N")
    s.venue_reject(0)
    s.enter(0, ref=rejected_enter, expected=Status.STALE_REFERENCE)
    current = s.enter(0)
    s.ack(0)
    s.replace(0, 50, 130_001)
    s.event_cancel(0, 30)
    s.cmd(Cmd.RETRY, 0, ref=current, expected=Status.NO_RETRY)
    s.barrier()


def replay_and_stops(s: Scenario) -> None:
    """Exercise replay, sequence gaps and deliberate session termination."""
    s.up()
    first = s.enter(0, qty=18)
    s.ack(0)
    s.exec(0, 3, 124_900)
    pending = s.enter(1, qty=MAX_SHARES, price=MAX_PRICE, side="S")
    s.partial(b"\x00\x41S\x41\x01\x02")
    s.reconnect(begin=2)
    s.cmd(Cmd.RETRY, 1, ref=pending, expected=Status.NOT_ACTIVE)
    s.rx(*s.history[1:3], max_keep=8, remember=False)
    s.barrier(1)
    assert not s.device.replaying
    s.cmd(Cmd.RETRY, 1, ref=pending, expected=Status.OK)
    s.ack(1)
    s.kill()
    s.cmd(Cmd.ENTER, 2, expected=Status.KILLED)
    s.replace(0, 17, 130_000, expected=Status.KILLED)
    s.cmd(Cmd.CANCEL, 0, ref=first, expected=Status.OK)
    s.reconnect()
    s.cmd(Cmd.RETRY, 0, ref=first, expected=Status.OK)
    s.event_cancel(0, 15)
    s.exec(1, 1, MAX_PRICE)
    s.system("E")
    s.logout()
    s.barrier(1)
    s.reconnect()
    s.rx(control("Z"))
    s.barrier(1)
    s.link(False)
    s.link(True)
    s.cmd(Cmd.CANCEL, 1, ref=pending, expected=Status.NOT_ACTIVE)
    s.barrier(1)


def reference_and_market_boundaries(s: Scenario) -> None:
    """Exercise reference reuse and market-eligibility boundary conditions."""
    s.up()
    threshold = (1 << 32) - 1
    slot0 = s.enter(0, ref=threshold - 2, qty=15)
    s.ack(0)
    pending = s.replace(0, 19, 125_555, ref=threshold - 1)
    assert s.device.store.view(0).ref == slot0
    s.kill()
    s.cmd(Cmd.RETRY, 0, ref=pending, expected=Status.KILLED)
    s.ack(0, state="D")
    s.cmd(Cmd.QUERY, 0, expected=Status.OK)
    s.barrier()

    s.up()
    live = s.enter(0, ref=threshold, qty=28, price=23_001)
    s.ack(0)
    s.enter(1, ref=threshold, expected=Status.STALE_REFERENCE)
    s.enter(1, ref=1, expected=Status.STALE_REFERENCE)
    s.system("E")
    s.enter(1, ref=threshold - 1, expected=Status.MARKET_CLOSED)
    s.cmd(Cmd.CANCEL, 0, ref=live, qty=13, expected=Status.OK)
    s.exec(0, 2, 23_001)
    s.event_cancel(0, 13)
    s.exec(0, 13, 23_100)
    s.barrier()


def protocol_faults(s: Scenario) -> None:
    """Drive invalid packet inputs and verify the decoded fault reason."""
    for malformed, code in (
        (b"\x00\x00", Status.FRAME_LENGTH),
        (b"\x00\x49", Status.FRAME_LENGTH),
        (b"\xff\xff", Status.FRAME_LENGTH),
    ):
        s.up()
        s.malformed_framing(malformed, code)
        s.barrier()
    for keep in (0, 0x5, 0x80, 0x7F ^ 0x8):
        s.up()
        s.malformed_framing(b"\x00\x01H\xbe\xef\x01\x02\x03", Status.BAD_KEEP, keep)
        s.barrier()
    invalid_frames = (
        (Envelope("H", b"X").frame(), Status.FRAME_LENGTH),
        (Envelope("W", b"").frame(), Status.SOUP_TYPE),
        (Envelope("S", b"").frame(), Status.OUCH_LENGTH),
        (Envelope("S", b"B" + b"\0" * 35).frame(), Status.OUCH_TYPE),
        (Envelope("S", VenueEvent("C", 5, quantity=1).payload() + b"\0").frame(), Status.OUCH_LENGTH),
        (Envelope("S", VenueEvent("E", 6, quantity=1, price=234).payload()[:-1] +
                  b"\x03").frame(), Status.OUCH_APPENDAGE),
        (login_accepted(SESSION, 1), Status.LOGIN_PHASE),
    )
    for raw, code in invalid_frames:
        s.up()
        s.rx(raw, expected=code)
        s.barrier()

    s.up()
    s.system("X", expected=Status.BAD_FIELDS)
    s.barrier()


def socket_tx_boundary(s: Scenario) -> None:
    """Cut a partially transmitted frame and check the transport state boundary."""
    s.up()
    s.hold_tx_after(1)
    first = s.enter(0, qty=69, price=678_900, symbol=SYMBOLS[1])
    s.cut_tcp_while_transmitting(8)
    assert s.device.store.view(0).pending == Pending.ENTER
    s.cmd(Cmd.RETRY, 0, ref=first, expected=Status.OK)
    s.ack(0)
    s.exec(0, 12, 600_000)
    s.barrier()

    s.hold_tx_after(2)
    second = s.replace(0, 19, 722_000)
    s.cut_tcp_while_transmitting(16)
    s.cmd(Cmd.RETRY, 0, ref=second, expected=Status.OK)
    s.exec(0, 3, 650_000)
    s.venue_reject(0)
    s.cmd(Cmd.RETRY, 0, ref=second, expected=Status.NO_RETRY)
    s.cmd(Cmd.CANCEL, 0, ref=first, expected=Status.OK)
    s.event_cancel(0, 54)
    s.barrier(0)


def ledger_faults(s: Scenario) -> None:
    """Drive invalid order updates and check that the ledger remains consistent."""
    s.up()
    s.rx(VenueEvent("C", next(s.clock), 99, quantity=1).frame(), expected=Status.UNKNOWN_REFERENCE)
    s.barrier()
    s.up()
    s.enter(0)
    s.exec(0, 1, 129_000, expected=Status.LIFECYCLE)
    s.barrier()
    s.up()
    s.enter(0)
    s.ack(0, symbol=SYMBOLS[2], expected=Status.ACK_ECHO)
    s.barrier()
    s.up()
    s.enter(0)
    s.ack(0)
    s.exec(0, 31, 125_000, expected=Status.BAD_DELTA)
    s.barrier()
    s.up()
    s.enter(0)
    s.ack(0)
    s.exec(0, 1, 125_000, match=0x3F)
    s.exec(0, 1, 125_000, match=0x3F, expected=Status.DUP_MATCH)
    s.barrier()


def login_faults_and_ticks(s: Scenario) -> None:
    """Exercise malformed login outcomes and heartbeat tick limits."""
    for response, code in (
        (login_accepted(SESSION, 2), Status.SEQUENCE_GAP),
        (login_accepted(SESSION, 0), Status.LOGIN_TEXT),
        (login_accepted(SESSION, b"18446744073709551616"), Status.LOGIN_TEXT),
        (login_accepted(SESSION, b"                  1 "), Status.LOGIN_TEXT),
        (login_accepted(SESSION, b" " * 20), Status.LOGIN_TEXT),
        (login_accepted(SESSION, b"                 1X0"), Status.LOGIN_TEXT),
        (login_accepted(SESSION, MAX_SEQUENCE), Status.SEQUENCE_GAP),
        (login_accepted(BLANK_SESSION, 1), Status.SESSION_ID),
        (control("J", b"A"), Status.LOGIN_REJECTED),
        (control("H"), Status.LOGIN_PHASE),
    ):
        s.reset()
        s.link(True)
        s.login()
        s.rx(response, max_keep=2, expected=code)
        s.barrier()
    s.up()
    s.barrier()
    s.link(False)
    s.link(True)
    s.login(replace(DEFAULT_LOGIN, session=OTHER_SESSION, sequence=1), Status.LOGIN_TEXT)
    s.login(replace(DEFAULT_LOGIN, session=SESSION, sequence=1))
    s.rx(login_accepted(OTHER_SESSION, 1), expected=Status.SESSION_ID)
    s.barrier()
    s.up()
    s.barrier()
    s.link(False)
    s.link(True)
    s.login(replace(DEFAULT_LOGIN, session=SESSION, sequence=2))
    s.rx(login_accepted(SESSION, 5), expected=Status.SEQUENCE_GAP)
    s.barrier()

    s.up()
    s.rx(control("H"), control("H"), control("+", b"beating"), max_keep=8)
    s.barrier()
    s.query_during_heartbeat()
    s.rx(control("H"))
    s.barrier()
    s.tick(7)
    s.barrier()
    assert s.device.last_fault == Status.RX_TIMEOUT


def random_entry(s: Scenario, rng: random.Random, slot: int) -> None:
    """Generate one reproducible new-order attempt for the chosen slot."""
    qty = rng.randint(25, 10_000)
    price = rng.randint(10_000, 990_000_000)
    side = rng.choice(("B", "S"))
    s.enter(slot, qty, price, side, symbol=rng.choice(SYMBOLS),
            tif=rng.choice(("0", "3", "5")), display=rng.choice(("N", "Y")))


def random_live(s: Scenario, rng: random.Random, slot: int) -> None:
    """Choose an operation against a live order with reproducible fields."""
    order = s.device.store.view(slot)
    move = rng.randrange(100)
    if move < 37:
        take = rng.randint(1, order.leaves)
        s.exec(slot, take, rng.randint(10_000, MAX_PRICE))
    elif move < 58:
        if rng.random() < 0.62:
            intended = order.total_filled
        else:
            intended = order.total_filled + rng.randrange(order.leaves)
        s.cmd(Cmd.CANCEL, slot, ref=order.ref, qty=intended, expected=Status.OK)
    elif move < 85 and order.total_filled < MAX_SHARES:
        limit = min(MAX_SHARES, order.total_filled + rng.randint(1, 6500))
        s.replace(slot, limit, rng.randrange(10_000, MAX_PRICE))
    elif move < 95:
        s.cmd(Cmd.QUERY, slot, expected=Status.OK)
    else:
        s.event_cancel(slot, rng.randint(1, order.leaves), explicit=bool(rng.randrange(2)))


def random_pending(s: Scenario, rng: random.Random, slot: int) -> None:
    """Choose an acknowledgment, reject or other event for a pending command."""
    order = s.device.store.view(slot)
    cmd = order.outstanding
    assert cmd is not None
    choice = rng.random()
    if choice < 0.14:
        s.cmd(Cmd.RETRY, slot, ref=cmd.ref, expected=Status.OK)
        return
    if order.pending in {Pending.ENTER, Pending.REPLACE}:
        if choice < 0.27:
            s.venue_reject(slot, explicit=bool(rng.randrange(2)))
        elif choice < 0.4 and order.pending == Pending.REPLACE and order.leaves > 1:
            s.exec(slot, rng.randint(1, order.leaves - 1), max(1, order.limit - 1))
        else:
            maximum = cmd.quantity - order.total_filled
            if maximum < 0:
                s.venue_reject(slot)
                return
            state = "D" if rng.random() < 0.08 or maximum <= 0 else "L"
            qty = rng.randint(1, maximum) if state == "L" else max(0, maximum)
            s.ack(slot, state=state, quantity=qty)
    elif choice < 0.33:
        s.status(slot, "I", explicit=bool(rng.randrange(2)))
    elif choice < 0.44:
        s.status(slot, "P", explicit=bool(rng.randrange(2)))
    else:
        s.event_cancel(slot, rng.randint(1, order.leaves), explicit=bool(rng.randrange(2)))


def randomized(s: Scenario, seed: int, operations: int) -> None:
    """Generate a repeatable sequence of mixed legal and exceptional transactions."""
    rng = random.Random(seed)
    s.up()
    for pos in range(operations):
        if pos % 71 == 70 and len(s.history) >= 2:
            lookback = min(len(s.history), rng.randint(0, 3))
            begin = s.device.checkpoint - lookback
            s.reconnect(begin)
            for frame in s.history[begin - 1:]:
                s.rx(frame, remember=False)
        elif pos % 37 == 18:
            s.rx(control("H"), control("+", b"fragmented stream"), control("H"), max_keep=8)
        slot = rng.randrange(s.device.capacity)
        order = s.device.store.view(slot)
        if order.pending != Pending.NONE:
            random_pending(s, rng, slot)
        elif order.live:
            random_live(s, rng, slot)
        else:
            random_entry(s, rng, slot)
        if pos % 31 == 12:
            s.barrier(slot)
    for slot in range(s.device.capacity):
        s.barrier(slot)


def main() -> None:
    """Generate reproducible OUCH stimulus files for the chosen slot geometry."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=lambda raw: int(raw, 0), default=0x0AC405)
    parser.add_argument("--operations", type=int, default=900)
    parser.add_argument("--slots", type=int, choices=(4, 8, 12), default=8)
    args = parser.parse_args()
    if not 0 <= args.operations <= 40000:
        parser.error("--operations outside deterministic tag capacity")
    s = Scenario(args.slots)
    directed_lifecycle(s)
    replay_and_stops(s)
    reference_and_market_boundaries(s)
    socket_tx_boundary(s)
    protocol_faults(s)
    ledger_faults(s)
    login_faults_and_ticks(s)
    randomized(s, args.seed, args.operations)
    s.write(args.output)
    print(f"{len(s.actions)} stimuli, {len(s.packets)} exact TX frames, {len(s.notices)} expected notices")


if __name__ == "__main__":
    main()
