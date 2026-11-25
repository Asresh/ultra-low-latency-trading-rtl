"""Independent snapshot transaction and per-stream delta model."""

from collections import Counter, deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional, Tuple
from zlib import crc32


U32 = (1 << 32) - 1
U64 = (1 << 64) - 1


class Phase(IntEnum):
    IDLE = 0
    UPLOAD = 1
    HEADER = 2
    ROWS = 3
    PREPARE = 4
    FENCE = 5
    CONFIGURE = 6
    WAIT_ACK = 7
    FINALIZE = 8
    DRAIN = 9


class Status(IntEnum):
    OK = 0
    BAD_SLOT = 1
    BAD_ID = 2
    BAD_EPOCH = 3
    BAD_COUNT = 4
    BAD_CRC = 5
    BOOK_REJECTED = 6
    RECEIVER_REJECTED = 7
    DELTA_GAP = 8


class RecordType(IntEnum):
    HEADER = 0
    ROW = 1
    COMMIT = 2
    ABORT = 3


class Traffic(IntEnum):
    DIRECT = 0
    BUFFER = 1
    STALE = 2
    GAP = 3
    BLOCK = 4
    INVALID = 5


@dataclass(frozen=True)
class Geometry:
    slots: int = 3
    epoch_width: int = 3
    wait_width: int = 4
    rows: int = 5
    deltas: int = 3

    @property
    def slot_width(self):
        return self.slots.bit_length()

    @property
    def count_width(self):
        return (self.rows + 1).bit_length()

    @property
    def delta_width(self):
        return self.deltas.bit_length()


@dataclass(frozen=True)
class Header:
    identity: int = 0
    slot: int = 0
    epoch: int = 0
    session: int = 0
    sequence: int = 0
    count: int = 0
    crc: int = 0
    force: bool = False
    spacing: int = 0


@dataclass(frozen=True)
class Row:
    identity: int = 0
    word: int = 0
    last: bool = False


@dataclass(frozen=True)
class Prepared:
    identity: int = 0
    ok: bool = False


@dataclass(frozen=True)
class ReceiverAck:
    status: int = 0
    slot: int = 0
    epoch: int = 0
    active: bool = False


@dataclass(frozen=True)
class Event:
    slot: int = 0
    epoch: int = 0
    data: int = 0

    @property
    def sequence(self):
        return (self.data >> 520) & U64


@dataclass(frozen=True)
class Restore:
    kind: RecordType
    header: Header
    index: int = 0
    word: int = 0


@dataclass(frozen=True)
class Done:
    identity: int
    slot: int
    status: Status
    epoch: int
    receiver_installed: bool
    book_committed: bool
    buffered: int
    crc: int


@dataclass(frozen=True)
class Inputs:
    begin: Optional[Header] = None
    row: Optional[Row] = None
    prepared: Optional[Prepared] = None
    ack: Optional[ReceiverAck] = None
    event: Optional[Event] = None
    reset: bool = False
    restore_ready: bool = True
    config_ready: bool = True
    m_ready: bool = True
    done_ready: bool = True
    monitor: int = 0


@dataclass
class Context:
    known: bool = False
    quarantined: bool = False
    epoch: int = 0
    sequence: int = 0
    telemetry: Counter = field(default_factory=Counter)


@dataclass(frozen=True)
class Monitor:
    known: bool
    quarantined: bool
    epoch: int
    sequence: int
    counts: Tuple[int, ...]


@dataclass(frozen=True)
class Pins:
    begin_ready: bool
    row_ready: bool
    prepared_ready: bool
    ack_ready: bool
    s_ready: bool
    restore: Optional[Restore]
    config: Optional[Header]
    output: Optional[Event]
    done: Optional[Done]
    monitor: Optional[Monitor]
    phase: Phase
    gap: bool
    occupancy: int
    candidate: int
    counts: Tuple[int, ...]


def bump(counters, key):
    counters[key] = min(U32, counters[key] + 1)


class Model:
    """Use byte-folded CRC, a captured image list and FIFO-ordered events."""

    def __init__(self, geometry):
        self.g = geometry
        self.reset()

    def reset(self):
        self.phase = Phase.IDLE
        self.highest = 0
        self.header = Header()
        self.image = []
        self.received = 0
        self.crc = 0
        self.delivered_rows = 0
        self.receiver_epoch = 0
        self.installed = False
        self.final = RecordType.ABORT
        self.status = Status.OK
        self.fenced = False
        self.draining = False
        self.gap = False
        self.candidate = 0
        self.deltas = deque()
        self.output = None
        self.delta_priority = False
        self.completion = None
        self.contexts = [Context() for _ in range(self.g.slots)]
        self.stats = Counter()

    def disposition(self, event):
        if event.slot >= self.g.slots:
            return Traffic.INVALID
        if event.slot == self.header.slot:
            if self.fenced:
                admitted = self.phase == Phase.WAIT_ACK or (
                    self.phase == Phase.FINALIZE and self.final == RecordType.COMMIT
                )
                if not admitted or event.epoch != self.header.epoch + 1:
                    return Traffic.STALE
                return Traffic.BUFFER if not self.gap and event.sequence == self.candidate else Traffic.GAP
            if self.draining:
                return Traffic.BLOCK
        ctx = self.contexts[event.slot]
        if ctx.quarantined:
            return Traffic.STALE
        if not ctx.known:
            return Traffic.DIRECT
        if ctx.epoch != event.epoch:
            return Traffic.STALE
        return Traffic.DIRECT if ctx.sequence == event.sequence else Traffic.GAP

    def wants_delta(self, port, action):
        return bool(self.deltas and self.draining and
                    (self.output is None or port.m_ready) and (
                       self.delta_priority or port.event is None or action != Traffic.DIRECT
                    ))

    def restore(self):
        if self.phase == Phase.HEADER:
            return Restore(RecordType.HEADER, self.header)
        if self.phase == Phase.ROWS:
            return Restore(RecordType.ROW, self.header, self.delivered_rows, self.image[self.delivered_rows])
        if self.phase == Phase.FINALIZE:
            return Restore(self.final, self.header)
        return None

    def monitor(self, selected):
        if selected >= self.g.slots:
            return None
        ctx = self.contexts[selected]
        return Monitor(ctx.known, ctx.quarantined, ctx.epoch if ctx.known else 0,
                       ctx.sequence if ctx.known else 0,
                       tuple(ctx.telemetry[key] for key in ("stale", "gap", "delivered", "buffered")))

    def observe(self, port):
        action = self.disposition(port.event or Event())
        space = self.output is None or port.m_ready
        input_ready = {
            Traffic.DIRECT: space and not self.wants_delta(port, action),
            Traffic.BUFFER: len(self.deltas) < self.g.deltas,
            Traffic.BLOCK: False,
        }.get(action, True)
        enabled = not port.reset
        return Pins(
            enabled and self.phase == Phase.IDLE and (self.completion is None or port.done_ready),
            enabled and self.phase == Phase.UPLOAD,
            enabled and self.phase == Phase.PREPARE,
            enabled and self.phase == Phase.WAIT_ACK,
            enabled and input_ready,
            self.restore() if enabled else None,
            self.header if enabled and self.phase == Phase.CONFIGURE else None,
            self.output if enabled else None,
            self.completion if enabled else None,
            self.monitor(port.monitor) if enabled else None,
            self.phase, self.gap, len(self.deltas), self.candidate,
            tuple(self.stats[key] for key in ("invalid", "foreign_rows", "foreign_prepares", "books", "failed")),
        )

    def start(self, header):
        result = Status.OK
        if header.slot >= self.g.slots:
            result = Status.BAD_SLOT
        elif header.identity <= self.highest:
            result = Status.BAD_ID
        elif header.epoch == (1 << self.g.epoch_width) - 1:
            result = Status.BAD_EPOCH
        elif header.count > self.g.rows:
            result = Status.BAD_COUNT
        elif header.count == 0 and header.crc:
            result = Status.BAD_CRC
        if result != Status.OK:
            bump(self.stats, "failed")
            self.completion = Done(header.identity, header.slot, result, header.epoch, False, False, 0, 0)
            return
        self.header = header
        self.highest = header.identity
        self.phase = Phase.UPLOAD if header.count else Phase.HEADER
        self.image.clear()
        self.received = 0
        self.crc = 0
        self.receiver_epoch = header.epoch
        self.installed = False
        self.final = RecordType.ABORT
        self.status = Status.OK

    def upload(self, row):
        if row.identity != self.header.identity:
            bump(self.stats, "foreign_rows")
            return
        if self.received < self.g.rows:
            self.image.append(row.word)
        self.received = min(self.g.rows+1, self.received+1)
        self.crc = crc32(row.word.to_bytes(16, "little"), self.crc)
        if not row.last:
            return
        if self.received != self.header.count:
            code = Status.BAD_COUNT
        elif self.crc != self.header.crc:
            code = Status.BAD_CRC
        else:
            self.phase = Phase.HEADER
            return
        self.phase = Phase.IDLE
        bump(self.stats, "failed")
        self.completion = Done(self.header.identity, self.header.slot, code,
                               self.receiver_epoch, False, False, 0, self.crc)

    def prepare(self, prepared):
        if prepared.identity != self.header.identity:
            bump(self.stats, "foreign_prepares")
            return
        if not prepared.ok:
            self.status = Status.BOOK_REJECTED
            self.phase = Phase.FINALIZE
            return
        self.fenced = True
        self.phase = Phase.FENCE
        self.deltas.clear()
        self.candidate = self.header.sequence
        self.gap = False
        self.draining = False

    def acknowledge(self, ack):
        ctx = self.contexts[self.header.slot]
        if ack.slot == self.header.slot:
            ctx.known = True
            ctx.epoch = ack.epoch
            self.receiver_epoch = ack.epoch
        self.installed = (ack.slot, ack.epoch, ack.status, ack.active) == (
            self.header.slot, self.header.epoch + 1, 0, True
        )
        self.status = Status.RECEIVER_REJECTED if not self.installed else (
            Status.DELTA_GAP if self.gap else Status.OK
        )
        self.final = RecordType.COMMIT if self.status == Status.OK else RecordType.ABORT
        self.phase = Phase.FINALIZE

    def finalize(self):
        book = self.final == RecordType.COMMIT
        success = book and not self.gap
        final_status = Status.DELTA_GAP if book and self.gap else self.status
        self.completion = Done(self.header.identity, self.header.slot, final_status,
                               self.receiver_epoch, self.installed, book, len(self.deltas), self.crc)
        if book:
            bump(self.stats, "books")
        if not success:
            bump(self.stats, "failed")
        if self.fenced:
            ctx = self.contexts[self.header.slot]
            ctx.quarantined = not success
            if success:
                ctx.sequence = self.candidate
            else:
                self.deltas.clear()
            self.draining = success and bool(self.deltas)
        self.fenced = False
        self.phase = Phase.DRAIN if success else Phase.IDLE

    def traffic(self, port, pins):
        action = self.disposition(port.event or Event())
        take_delta = self.wants_delta(port, action)
        space = self.output is None or port.m_ready
        if self.output is not None and port.m_ready:
            bump(self.contexts[self.output.slot].telemetry, "delivered")
        if take_delta:
            self.output = self.deltas.popleft()
            self.delta_priority = False
        elif space:
            self.output = None
        if self.draining and not self.deltas:
            self.draining = False
        if port.event is None or not pins.s_ready:
            return
        event = port.event
        if action == Traffic.INVALID:
            bump(self.stats, "invalid")
            return
        ctx = self.contexts[event.slot]
        if action == Traffic.DIRECT:
            self.output = event
            self.delta_priority = True
            if ctx.known:
                ctx.sequence = (ctx.sequence+1) & U64
        elif action == Traffic.BUFFER:
            self.deltas.append(event)
            self.candidate = (self.candidate+1) & U64
            bump(ctx.telemetry, "buffered")
        elif action == Traffic.STALE:
            bump(ctx.telemetry, "stale")
        elif action == Traffic.GAP:
            bump(ctx.telemetry, "gap")
            if self.fenced and event.slot == self.header.slot:
                self.gap = True
            else:
                ctx.quarantined = True

    def advance(self, port):
        pins = self.observe(port)
        if port.reset:
            self.reset()
            return pins
        phase = self.phase
        hold_old = self.output is not None and self.output.slot == self.header.slot
        was_draining = self.draining
        if port.done_ready:
            self.completion = None
        self.traffic(port, pins)
        if phase == Phase.IDLE and port.begin and pins.begin_ready:
            self.start(port.begin)
        elif phase == Phase.UPLOAD and port.row:
            self.upload(port.row)
        elif phase == Phase.HEADER and port.restore_ready:
            self.delivered_rows = 0
            self.phase = Phase.ROWS if self.header.count else Phase.PREPARE
        elif phase == Phase.ROWS and port.restore_ready:
            self.delivered_rows += 1
            if self.delivered_rows == self.header.count:
                self.phase = Phase.PREPARE
        elif phase == Phase.PREPARE and port.prepared:
            self.prepare(port.prepared)
        elif phase == Phase.FENCE and not hold_old:
            self.phase = Phase.CONFIGURE
        elif phase == Phase.CONFIGURE and port.config_ready:
            self.phase = Phase.WAIT_ACK
        elif phase == Phase.WAIT_ACK and port.ack:
            self.acknowledge(port.ack)
        elif phase == Phase.FINALIZE and port.restore_ready:
            self.finalize()
        elif phase == Phase.DRAIN and not was_draining:
            self.phase = Phase.IDLE
        return pins
