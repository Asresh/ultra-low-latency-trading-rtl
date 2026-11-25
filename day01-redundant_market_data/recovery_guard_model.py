"""Independent cycle model for a MoldUDP64 repair admission boundary."""

from collections import Counter, deque
from dataclasses import dataclass, field
from enum import IntEnum
from struct import Struct
from typing import Optional, Tuple


HEADER = Struct("!10sQH")
MODULO = 1 << 64
MAX32 = (1 << 32) - 1


class Operation(IntEnum):
    SYNC = 0
    ARM = 1
    CANCEL = 2
    INSPECT = 3


class ReplyCode(IntEnum):
    OK = 0
    BAD_SLOT = 1
    BAD_EPOCH = 2
    DISABLED = 3
    BAD_REQ = 4
    BUSY = 5
    SAME_REQ = 6
    BAD_TOKEN = 7
    EXHAUSTED = 8
    NO_LEASE = 9


class Phase(IntEnum):
    IDLE = 0
    LIVE = 1
    COMPLETE = 2
    TIMED_OUT = 3
    REVOKED = 4


class Verdict(IntEnum):
    ACCEPT = 0
    FRAME = 1
    LIMIT = 2
    EMPTY = 3
    SLOT = 4
    CONTEXT = 5
    EPOCH = 6
    SESSION = 7
    UNARMED = 8
    TOKEN = 9
    EXPIRED = 10
    RANGE = 11
    DUPLICATE = 12
    OVERLAP = 13
    CANCELLED = 14


@dataclass(frozen=True)
class Geometry:
    slots: int = 3
    packet_bytes: int = 72
    message_bytes: int = 24
    blocks: int = 4
    window: int = 7
    epoch_width: int = 4
    token_width: int = 3

    @property
    def key_width(self):
        return self.slots.bit_length()


@dataclass(frozen=True)
class Route:
    slot: int = 0
    epoch: int = 0
    token: int = 0


@dataclass(frozen=True)
class Request:
    session: bytes = bytes(10)
    sequence: int = 0
    count: int = 0

    def wire(self):
        return HEADER.pack(self.session, self.sequence, self.count)


@dataclass(frozen=True)
class Control:
    op: Operation
    route: Route = Route()
    enabled: bool = True
    request: Request = Request()
    ttl: int = 0


@dataclass(frozen=True)
class Snapshot:
    phase: Phase = Phase.IDLE
    route: Route = Route()
    enabled: bool = False
    request: Request = Request()
    coverage: int = 0
    ttl: int = 0


@dataclass(frozen=True)
class Reply:
    op: Operation
    status: ReplyCode
    snapshot: Snapshot


@dataclass(frozen=True)
class Beat:
    data: int
    keep: int
    first: bool
    last: bool
    route: Route

    def masked_bytes(self):
        lanes = self.data.to_bytes(8, "little")
        return bytes(byte if self.keep & (1 << lane) else 0 for lane, byte in enumerate(lanes))


@dataclass(frozen=True)
class Decoded:
    header: Request
    route: Route
    length: int
    shape: Verdict
    validation_edges: int
    beats: Tuple[Beat, ...]


@dataclass(frozen=True)
class Audit:
    route: Route
    status: Verdict
    sequence: int
    count: int
    length: int
    coverage: int
    complete: bool


@dataclass(frozen=True)
class Inputs:
    command: Optional[Control] = None
    beat: Optional[Beat] = None
    reset: bool = False
    reply_ready: bool = True
    replay_ready: bool = True
    audit_ready: bool = True
    stat_slot: int = 0
    stat_index: int = 0


@dataclass(frozen=True)
class Pins:
    command_ready: bool
    rx_ready: bool
    reply: Optional[Reply]
    replay: Optional[Beat]
    audit: Optional[Audit]
    stat_valid: bool
    stat_value: int
    totals: Tuple[int, int, int]


def prefix_size(mask):
    """Recognize one contiguous prefix without treating a sparse tail as bytes."""
    amount = mask.bit_count() if hasattr(mask, "bit_count") else bin(mask).count("1")
    return amount if mask == (1 << amount) - 1 else 0


def frame_decode(beats, geometry):
    """Parse the bytes as a packet and count the decisions needed to validate it."""
    route = beats[0].route
    prefix = b"".join(beat.masked_bytes() for beat in beats[:3]).ljust(24, b"\0")
    header = Request(*HEADER.unpack_from(prefix))
    fragments = []
    frame_error = False
    for index, beat in enumerate(beats):
        amount = prefix_size(beat.keep)
        frame_error |= beat.first != (index == 0) or amount == 0 or (not beat.last and amount != 8)
        fragments.append(beat.masked_bytes()[:amount])
    wire = b"".join(fragments)
    size = min(65535, len(wire))
    steps = 1
    status = Verdict.ACCEPT
    if frame_error or size < HEADER.size:
        status = Verdict.FRAME
    elif size > geometry.packet_bytes:
        status = Verdict.LIMIT
    elif header.count in (0, 65535):
        status = Verdict.EMPTY if size == HEADER.size else Verdict.FRAME
    elif header.count > geometry.blocks:
        status = Verdict.LIMIT
    else:
        remaining = memoryview(wire)[HEADER.size:]
        for index in range(header.count):
            steps += 2
            if len(remaining) < 2:
                status = Verdict.FRAME
                break
            size_field = int.from_bytes(remaining[:2], "big")
            remaining = remaining[2:]
            if size_field > len(remaining) or (index == header.count - 1 and size_field != len(remaining)):
                status = Verdict.FRAME
                break
            if size_field > geometry.message_bytes:
                status = Verdict.LIMIT
                break
            remaining = remaining[size_field:]
    normalized = tuple(
        Beat(int.from_bytes(beat.masked_bytes(), "little"), beat.keep, beat.first, beat.last, route)
        for beat in beats
    )
    return Decoded(header, route, size, status, steps, normalized)


def count(counter, key, amount=1):
    counter[key] = min(MAX32, counter[key] + amount)


@dataclass
class Slot:
    epoch: int = 0
    enabled: bool = False
    token: int = 0
    phase: Phase = Phase.IDLE
    request: Request = Request()
    sequence_map: dict = field(default_factory=dict)
    received: set = field(default_factory=set)
    deadline: int = 0
    counters: Counter = field(default_factory=Counter)

    def coverage(self):
        return sum(1 << self.sequence_map[seq] for seq in self.received)

    def inspect(self, index, tick):
        return Snapshot(self.phase, Route(index, self.epoch, self.token), self.enabled,
                        self.request, self.coverage(), max(0, self.deadline - tick))

    def install(self, control):
        self.epoch = control.route.epoch
        self.enabled = control.enabled
        self.token = 0
        self.phase = Phase.IDLE
        self.request = Request(control.request.session)
        self.sequence_map.clear()
        self.received.clear()
        self.deadline = 0
        count(self.counters, 20)

    def arm(self, control, now):
        self.token += 1
        self.phase = Phase.LIVE
        self.request = control.request
        self.sequence_map = {(self.request.sequence + i) % MODULO: i for i in range(self.request.count)}
        self.received.clear()
        self.deadline = now + 1 + control.ttl
        count(self.counters, 16)

    def cancel(self):
        self.phase = Phase.REVOKED
        self.deadline = 0
        count(self.counters, 17)

    def advance(self, now):
        if self.phase == Phase.LIVE and self.deadline <= now:
            self.phase = Phase.TIMED_OUT
            self.deadline = 0
            count(self.counters, 18)


class Model:
    """Compare external handshakes with a byte parser and per-slot sequence sets."""

    def __init__(self, geometry):
        self.geometry = geometry
        self.reset()

    def reset(self):
        self.slots = [Slot() for _ in range(self.geometry.slots)]
        self.captured = []
        self.candidate = None
        self.processing = 0
        self.replay = deque()
        self.reply = None
        self.audit = None
        self.totals = Counter()
        self.tick = 0

    def can_decide(self, port):
        return not port.reset and self.candidate is not None and self.processing == 0 and (
            self.audit is None or port.audit_ready
        )

    def fenced(self, control):
        if control is None or control.op != Operation.SYNC or not self.replay:
            return False
        slot = control.route.slot
        return slot == self.replay[0].route.slot and control.route.epoch > self.slots[slot].epoch

    def observe(self, port):
        stat_ok = 0 <= port.stat_slot < len(self.slots) and 0 <= port.stat_index <= 20 and not port.reset
        return Pins(
            not port.reset and not self.can_decide(port) and not self.fenced(port.command) and
            (self.reply is None or port.reply_ready),
            not port.reset and self.candidate is None and not self.replay,
            self.reply if not port.reset else None,
            self.replay[0] if self.replay and not port.reset else None,
            self.audit if not port.reset else None,
            stat_ok, self.slots[port.stat_slot].counters[port.stat_index] if stat_ok else 0,
            tuple(self.totals[key] for key in ("frames", "rejected", "unknown")),
        )

    def command(self, control):
        route = control.route
        if route.slot >= len(self.slots):
            return Reply(control.op, ReplyCode.BAD_SLOT, Snapshot(route=Route(route.slot)))
        slot = self.slots[route.slot]
        status = ReplyCode.OK
        snapshot_tick = self.tick
        if control.op == Operation.SYNC:
            if route.epoch > slot.epoch:
                slot.install(control)
            elif route.epoch == 0 or (route.epoch, control.enabled, control.request.session) != (
                slot.epoch, slot.enabled, slot.request.session
            ):
                status = ReplyCode.BAD_EPOCH
        elif control.op == Operation.ARM:
            if route.epoch != slot.epoch:
                status = ReplyCode.BAD_EPOCH
            elif not slot.enabled:
                status = ReplyCode.DISABLED
            elif (control.request.session != slot.request.session or
                  not 1 <= control.request.count <= self.geometry.window or control.ttl == 0):
                status = ReplyCode.BAD_REQ
            elif slot.phase == Phase.LIVE:
                if control.request == slot.request:
                    status = ReplyCode.SAME_REQ
                    count(slot.counters, 19)
                else:
                    status = ReplyCode.BUSY
            elif slot.token == (1 << self.geometry.token_width) - 1:
                status = ReplyCode.EXHAUSTED
            else:
                slot.arm(control, self.tick)
                snapshot_tick += 1
        elif control.op == Operation.CANCEL:
            if route.epoch != slot.epoch:
                status = ReplyCode.BAD_EPOCH
            elif slot.phase != Phase.LIVE:
                status = ReplyCode.NO_LEASE
            elif route.token != slot.token:
                status = ReplyCode.BAD_TOKEN
            else:
                slot.cancel()
        return Reply(control.op, status, slot.inspect(route.slot, snapshot_tick))

    def qualify(self, packet):
        route = packet.route
        slot = self.slots[route.slot] if route.slot < len(self.slots) else None
        if packet.shape != Verdict.ACCEPT:
            return packet.shape, set()
        if slot is None:
            return Verdict.SLOT, set()
        if not slot.enabled:
            return Verdict.CONTEXT, set()
        if route.epoch != slot.epoch:
            return Verdict.EPOCH, set()
        if packet.header.session != slot.request.session:
            return Verdict.SESSION, set()
        if slot.phase == Phase.IDLE:
            return Verdict.UNARMED, set()
        if route.token != slot.token:
            return Verdict.TOKEN, set()
        if slot.phase == Phase.TIMED_OUT:
            return Verdict.EXPIRED, set()
        if slot.phase == Phase.REVOKED:
            return Verdict.CANCELLED, set()
        sequence_set = {(packet.header.sequence + i) % MODULO for i in range(packet.header.count)}
        if not sequence_set.issubset(slot.sequence_map):
            return Verdict.RANGE, set()
        duplicates = sequence_set.intersection(slot.received)
        if duplicates:
            return (Verdict.DUPLICATE if duplicates == sequence_set else Verdict.OVERLAP), set()
        return Verdict.ACCEPT, sequence_set

    def decide(self, packet):
        verdict, admitted = self.qualify(packet)
        slot = self.slots[packet.route.slot] if packet.route.slot < len(self.slots) else None
        coverage = 0
        complete = False
        count(self.totals, "frames")
        if slot is None:
            count(self.totals, "unknown")
        else:
            count(slot.counters, verdict)
        if verdict == Verdict.ACCEPT:
            slot.received.update(admitted)
            count(slot.counters, 15, len(admitted))
            coverage = slot.coverage()
            complete = len(slot.received) == slot.request.count
            if complete:
                slot.phase = Phase.COMPLETE
                slot.deadline = 0
            self.replay.extend(packet.beats)
        else:
            count(self.totals, "rejected")
        return Audit(packet.route, verdict, packet.header.sequence, packet.header.count,
                     packet.length, coverage, complete)

    def advance(self, port):
        pins = self.observe(port)
        if port.reset:
            self.reset()
            return pins
        can_decide = self.can_decide(port)
        if port.replay_ready and self.replay:
            self.replay.popleft()
        if port.reply_ready:
            self.reply = None
        if port.audit_ready:
            self.audit = None
        if port.command and pins.command_ready:
            self.reply = self.command(port.command)
        if can_decide:
            self.audit = self.decide(self.candidate)
            self.candidate = None
        elif self.processing:
            self.processing -= 1
        if port.beat and pins.rx_ready:
            self.captured.append(port.beat)
            if port.beat.last:
                self.candidate = frame_decode(tuple(self.captured), self.geometry)
                self.processing = self.candidate.validation_edges
                self.captured.clear()
        self.tick += 1
        for slot in self.slots:
            slot.advance(self.tick)
        return pins
