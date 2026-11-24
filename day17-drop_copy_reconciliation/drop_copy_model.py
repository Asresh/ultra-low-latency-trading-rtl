"""Reference model and portable trace format for the drop-copy reconciler."""

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, Optional, Tuple


WORD32 = (1 << 32) - 1
RESET_TICK = 0xFFFFFFF0
COUNTERS = (
    "observed", "matched", "conflict", "duplicate", "missing",
    "late", "full", "rejected", "flushed",
)
PIN_NAMES = (
    "primary_ready", "copy_ready", "epoch_ready", "report_valid",
    "status", "source", "key", "expected", "observed_payload", "diff", "age",
    "flushed_in_report", "active_epoch", "pending", "tick", *COUNTERS,
)


class Status(IntEnum):
    """The status codes carried by the report channel."""

    STAGED = 0
    MATCH = 1
    PAIR_CONFLICT = 2
    DUPLICATE = 3
    DUP_CONFLICT = 4
    MISSING_COPY = 5
    MISSING_PRIMARY = 6
    LATE = 7
    FULL = 8
    STALE_EPOCH = 9
    BAD_EVENT = 10
    RESTART = 11
    BAD_RESTART = 12


class Lane(IntEnum):
    """Report provenance and the two physical observation lanes."""

    PRIMARY = 0
    COPY = 1
    SWEEP = 2
    EPOCH = 3


@dataclass(frozen=True)
class Key:
    """Normalized event identity, including the original source session."""

    epoch: int
    venue: int
    session: int
    execution: int
    kind: int = 0

    def packed(self) -> int:
        """Encode original identity from the most to the least significant field."""
        return (self.epoch << 58 | self.venue << 50 | self.session << 34
                | self.execution << 2 | self.kind)

    def bucket(self, sets: int) -> int:
        """Return the set number by XORing set-width digits of the packed key."""
        if sets == 1:
            return 0
        bits = sets.bit_length() - 1
        remaining = self.packed()
        folded = 0
        while remaining:
            folded ^= remaining & (sets - 1)
            remaining >>= bits
        return folded


@dataclass(frozen=True)
class Payload:
    """Fields compared between the primary path and drop-copy observation."""

    order: int
    quantity: int
    price: int
    side: int

    def packed(self) -> int:
        """Encode the four comparison fields into the payload word."""
        return self.order << 57 | self.quantity << 33 | self.price << 1 | self.side

    def diff(self, other: "Payload") -> int:
        """Set one bit per field that disagrees with the other payload."""
        return (8 * (self.order != other.order) + 4 * (self.quantity != other.quantity)
                + 2 * (self.price != other.price) + (self.side != other.side))


Event = Tuple[Key, Payload]


@dataclass(frozen=True)
class Inputs:
    """Input values held from a falling edge until the next rising edge."""

    primary: Optional[Event] = None
    copy: Optional[Event] = None
    epoch: Optional[int] = None
    ready: bool = True
    reset: bool = False

    def row(self) -> tuple:
        """Flatten this cycle in the order the SystemVerilog bench reads."""
        lanes = []
        for event in (self.primary, self.copy):
            if event is None:
                lanes.extend((0, 0, 0))
            else:
                key, payload = event
                lanes.extend((1, key.packed(), payload.packed()))
        return (self.reset, *lanes, self.epoch is not None, self.epoch or 0, self.ready)


@dataclass(frozen=True)
class Report:
    """A registered diagnostic with prior/current values for offline investigation."""

    status: Status
    source: Lane
    key: Key
    expected: Optional[Payload] = None
    observed: Optional[Payload] = None
    diff: int = 0
    age: int = 0
    flushed: int = 0

    def row(self) -> tuple:
        """Flatten metrics and normalize absent payloads to the wire value zero."""
        return (1, self.status, self.source, self.key.packed(),
                self.expected.packed() if self.expected else 0,
                self.observed.packed() if self.observed else 0,
                self.diff, self.age, self.flushed)


@dataclass
class Entry:
    """An event record with the accepted payloads and timing of its latest transition."""

    slot: int
    first: Lane
    payloads: Dict[Lane, Payload]
    since: int
    closed: bool = False


@dataclass
class Oracle:
    """Scoreboard keyed by complete identities, with a bounded placement policy."""

    sets: int
    wait: int
    history: int
    reset_tick: int = RESET_TICK
    tick: int = field(init=False)
    epoch: int = field(init=False)
    cursor: int = field(init=False)
    preferred: Lane = field(init=False)
    entries: Dict[Key, Entry] = field(init=False)
    counts: Dict[str, int] = field(init=False)
    report: Optional[Report] = field(init=False)

    def __post_init__(self) -> None:
        """Validate model parameters and initialize from a completed reset."""
        if self.sets < 1 or self.sets & (self.sets - 1):
            raise ValueError("sets must be a positive power of two")
        if self.wait < 1 or self.history < 1:
            raise ValueError("wait and history must be positive")
        self.reset()

    def reset(self) -> None:
        """Clear the scoreboard, output and counters to their power-on state."""
        self.tick = self.reset_tick
        self.epoch = 0
        self.cursor = 0
        self.preferred = Lane.PRIMARY
        self.entries = {}
        self.counts = dict.fromkeys(COUNTERS, 0)
        self.report = None

    def age(self, entry: Entry) -> int:
        """Measure elapsed cycles modulo the width of the hardware counter."""
        return (self.tick - entry.since) & WORD32

    def pending(self) -> int:
        """Count outstanding events whose other lane has yet to arrive."""
        return sum(not entry.closed for entry in self.entries.values())

    def add(self, name: str, amount: int = 1) -> None:
        """Increment a counter by amount and clamp at its 32-bit ceiling."""
        self.counts[name] = min(WORD32, self.counts[name] + amount)

    def due(self) -> Optional[Tuple[Key, Entry]]:
        """Find the due event nearest the sweep cursor, wrapping physical slots."""
        overdue = ((key, record) for key, record in self.entries.items()
                   if not record.closed and self.age(record) >= self.wait)
        return min(overdue, key=lambda item: (item[1].slot - self.cursor) % (2 * self.sets),
                   default=None)

    def sample(self, inputs: Inputs) -> tuple:
        """Read the complete pins before the next rising edge changes state."""
        room = not inputs.reset and (self.report is None or inputs.ready)
        enabled = room and inputs.epoch is None and self.due() is None
        choose_copy = inputs.copy is not None and (
            inputs.primary is None or self.preferred == Lane.COPY
        )
        output = self.report.row() if self.report is not None else (0,) * 9
        return (enabled and not choose_copy, enabled and choose_copy, room, *output,
                self.epoch, self.pending(), self.tick, *self.counts.values())

    def advance(self, inputs: Inputs) -> tuple:
        """Sample pins and commit at most one diagnostic-producing operation."""
        pins = self.sample(inputs)
        if inputs.reset:
            self.reset()
            return pins
        self.retire()
        if pins[2]:
            self.report = None
            if inputs.epoch is not None:
                self.report = self.restart(inputs.epoch)
            else:
                expired = self.due()
                if expired is not None:
                    self.report = self.expire(*expired)
                elif inputs.primary is not None and pins[0]:
                    self.report = self.accept(Lane.PRIMARY, *inputs.primary)
                elif inputs.copy is not None and pins[1]:
                    self.report = self.accept(Lane.COPY, *inputs.copy)
        self.tick = (self.tick + 1) & WORD32
        return pins

    def restart(self, requested: int) -> Report:
        """Report abort count and clear all retained records on an increasing epoch."""
        key = Key(requested, 0, 0, 0)
        if requested <= self.epoch:
            self.add("rejected")
            return Report(Status.BAD_RESTART, Lane.EPOCH, key)
        waiting = self.pending()
        self.add("flushed", waiting)
        self.entries.clear()
        self.epoch = requested
        self.cursor = 0
        self.preferred = Lane.PRIMARY
        return Report(Status.RESTART, Lane.EPOCH, key, flushed=waiting)

    def expire(self, key: Key, entry: Entry) -> Report:
        """Preserve a missing event as a tombstone and move sweep priority."""
        status = Status.MISSING_COPY if entry.first == Lane.PRIMARY else Status.MISSING_PRIMARY
        result = Report(status, Lane.SWEEP, key, expected=entry.payloads[entry.first],
                        age=self.age(entry))
        entry.closed = True
        entry.since = self.tick
        self.cursor = (entry.slot + 1) % (2 * self.sets)
        self.add("missing")
        return result

    def retire(self) -> None:
        """Remove closed records by age every cycle, including during output stalls."""
        retired = [old for old, entry in self.entries.items()
                   if entry.closed and self.age(entry) >= self.history]
        for old in retired:
            del self.entries[old]

    def placement(self, key: Key) -> Optional[int]:
        """Return the first remaining vacant slot in the key's set."""
        occupied = {entry.slot for entry in self.entries.values()}
        start = 2 * key.bucket(self.sets)
        return next((slot for slot in (start, start + 1) if slot not in occupied), None)

    def compare(self, key: Key, entry: Entry, lane: Lane, payload: Payload) -> Report:
        """Classify follow-up traffic against the stored value from its relevant lane."""
        same_lane_already_seen = lane in entry.payloads
        prior = entry.payloads.get(lane, entry.payloads[entry.first])
        elapsed = self.age(entry)
        difference = prior.diff(payload)
        if same_lane_already_seen:
            if difference:
                status = Status.DUP_CONFLICT
                self.add("conflict")
            else:
                status = Status.DUPLICATE
                self.add("duplicate")
        elif entry.closed:
            status = Status.LATE
            self.add("late")
        else:
            entry.payloads[lane] = payload
            entry.closed = True
            entry.since = self.tick
            if difference:
                status = Status.PAIR_CONFLICT
                self.add("conflict")
            else:
                status = Status.MATCH
                self.add("matched")
        return Report(status, lane, key, prior, payload, difference, elapsed)

    def accept(self, lane: Lane, key: Key, payload: Payload) -> Report:
        """Validate and reconcile a single handshake without replacing prior observations."""
        self.preferred = Lane.COPY if lane == Lane.PRIMARY else Lane.PRIMARY
        self.add("observed")
        if key.epoch != self.epoch:
            status = Status.STALE_EPOCH
            self.add("rejected")
        elif key.execution == 0 or key.kind > 1 or payload.quantity == 0:
            status = Status.BAD_EVENT
            self.add("rejected")
        else:
            slot = self.placement(key)
            if key in self.entries:
                return self.compare(key, self.entries[key], lane, payload)
            if slot is None:
                status = Status.FULL
                self.add("full")
            else:
                self.entries[key] = Entry(slot, lane, {lane: payload}, self.tick)
                status = Status.STAGED
        return Report(status, lane, key, observed=payload)
