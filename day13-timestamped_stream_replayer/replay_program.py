"""Protocol records and immutable images for the timestamped stream replayer."""

from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Iterable, Optional

LANES = 8
U16 = (1 << 16) - 1
U32 = (1 << 32) - 1
U48 = (1 << 48) - 1
U64 = (1 << 64) - 1


class ReplayError(ValueError):
    """A trace, command or tape violates its documented format."""


class Operation(IntEnum):
    """Numeric opcodes on the configuration request channel."""

    APPEND = 0
    CLEAR = 1
    START = 2
    PAUSE = 3
    RESUME = 4
    STOP = 5


class Status(IntEnum):
    """Response codes shared with replay_codes.svh."""

    OK = 0
    BAD_OP = 1
    BUSY = 2
    FULL = 3
    BAD_TIME = 4
    BAD_KEEP = 5
    BAD_TAG = 6
    EMPTY = 7
    OPEN_PACKET = 8
    BAD_REPEAT = 9
    BAD_STATE = 10


def unsigned(value, maximum, name):
    """Validate a bus-sized unsigned integer and return it unchanged."""
    if type(value) not in (int, Operation, Status) or not 0 <= value <= maximum:
        raise ReplayError(f"{name} must be an integer from 0 through {maximum}")
    return int(value)


def boolean(value, name):
    """Validate a single-bit Python field without accepting arbitrary truthy values."""
    if type(value) is not bool:
        raise ReplayError(f"{name} must be Boolean")
    return value


def prefix_keep(count):
    """Return the canonical mask for a beat with one to eight low lanes."""
    if type(count) is not int or not 1 <= count <= LANES:
        raise ReplayError("a replay beat needs one to eight bytes")
    return (1 << count) - 1


def lanes_from_keep(keep):
    """Decode a contiguous low-lane mask and reject zero or gaps."""
    keep = unsigned(keep, 0xFF, "keep")
    count = bin(keep).count("1")
    if not count or keep != (1 << count) - 1:
        raise ReplayError(f"keep 0x{keep:02x} does not select contiguous low lanes")
    return count


def mask_data(data, keep):
    """Zero all unselected lanes of a little-endian 64-bit payload word."""
    data = unsigned(data, U64, "data")
    keep = unsigned(keep, 0xFF, "keep")
    raw = data.to_bytes(LANES, "little")
    return int.from_bytes(bytes(byte if keep >> lane & 1 else 0
                                for lane, byte in enumerate(raw)), "little")


@dataclass(frozen=True)
class Beat:
    """One normalized event staged in the replay tape."""

    tick: int
    data: int
    keep: int
    last: bool
    tag: int

    def __post_init__(self):
        """Reject malformed geometry and normalize the inactive byte lanes."""
        unsigned(self.tick, U32, "tick")
        unsigned(self.tag, U16, "tag")
        boolean(self.last, "last")
        lanes_from_keep(self.keep)
        if not self.last and self.keep != 0xFF:
            raise ReplayError("a non-final beat must use all eight lanes")
        object.__setattr__(self, "data", mask_data(self.data, self.keep))

    @classmethod
    def from_payload(cls, tick, payload, tag, last):
        """Pack a bytes object into low lanes and retain the caller's frame boundary."""
        if type(payload) is not bytes:
            raise ReplayError("beat payload must be bytes")
        keep = prefix_keep(len(payload))
        return cls(tick, int.from_bytes(payload, "little"), keep, last, tag)

    @property
    def payload(self):
        """Expose only the valid little-endian bytes."""
        return self.data.to_bytes(LANES, "little")[:lanes_from_keep(self.keep)]

    def at(self, tick):
        """Copy this event with a new offset, rechecking the 32-bit bound."""
        return replace(self, tick=tick)


@dataclass(frozen=True)
class Packet:
    """A complete packet and the offset of each constituent beat."""

    tag: int
    payload: bytes
    ticks: tuple

    def __post_init__(self):
        """Validate packet length, per-beat tick count, and chronological order."""
        unsigned(self.tag, U16, "tag")
        if type(self.payload) is not bytes or not self.payload:
            raise ReplayError("a packet needs a nonempty bytes payload")
        if type(self.ticks) is not tuple:
            raise ReplayError("packet ticks must be a tuple")
        if len(self.ticks) != (len(self.payload) + LANES - 1) // LANES:
            raise ReplayError("packet has the wrong number of beat timestamps")
        for tick in self.ticks:
            unsigned(tick, U32, "tick")
        if any(current < prior for prior, current in zip(self.ticks, self.ticks[1:])):
            raise ReplayError("packet timestamps must be chronological")

    @classmethod
    def paced(cls, tag, payload, start, distance=1):
        """Schedule a packet's beats with a fixed number of ticks between offers."""
        unsigned(start, U32, "packet start")
        unsigned(distance, U32, "inter-beat distance")
        if type(payload) is not bytes:
            raise ReplayError("packet payload must be bytes")
        beats = (len(payload) + LANES - 1) // LANES
        ticks = tuple(start + index * distance for index in range(beats))
        return cls(tag, payload, ticks)

    def beats(self):
        """Packetize payload bytes and mark exactly the final beat."""
        return tuple(
            Beat.from_payload(tick, self.payload[index*LANES:(index+1)*LANES],
                              self.tag, index == len(self.ticks)-1)
            for index, tick in enumerate(self.ticks)
        )

    @property
    def start(self):
        """Return the offset of the first beat."""
        return self.ticks[0]

    @property
    def end(self):
        """Return the offset of the final beat."""
        return self.ticks[-1]


@dataclass(frozen=True)
class Tape:
    """An immutable sequence of complete, chronologically ordered packets."""

    events: tuple

    def __post_init__(self):
        """Verify monotonicity and stable tags from packet start to final beat."""
        if type(self.events) is not tuple:
            raise ReplayError("tape events must be stored in an immutable tuple")
        expected_tag = None
        previous_tick = 0
        for event in self.events:
            if not isinstance(event, Beat):
                raise ReplayError("tape events must be Beat objects")
            if event.tick < previous_tick:
                raise ReplayError("tape timestamps are not chronological")
            if expected_tag is not None and event.tag != expected_tag:
                raise ReplayError("tag changed before a packet's final beat")
            expected_tag = None if event.last else event.tag
            previous_tick = event.tick
        if expected_tag is not None:
            raise ReplayError("tape ends with a partial packet")

    @classmethod
    def from_packets(cls, packets: Iterable[Packet]):
        """Join packets in caller-supplied order and validate the combined tape."""
        return cls(tuple(event for packet in packets for event in packet.beats()))

    @property
    def duration(self):
        """Return the final offsets in ticks, with zero for an empty tape."""
        return self.events[-1].tick if self.events else 0

    @property
    def payload_bytes(self):
        """Count programmed payload bytes across all packets."""
        return sum(len(event.payload) for event in self.events)

    def packets(self):
        """Decode validated beats back into packet payloads and tick tuples."""
        payload, ticks = bytearray(), []
        for event in self.events:
            payload.extend(event.payload)
            ticks.append(event.tick)
            if event.last:
                yield Packet(event.tag, bytes(payload), tuple(ticks))
                payload.clear()
                ticks.clear()

    def between(self, start=0, end=U32):
        """Keep only complete packets whose first and last ticks are in range."""
        unsigned(start, U32, "range start")
        unsigned(end, U32, "range end")
        if end < start:
            raise ReplayError("range end precedes its start")
        return Tape.from_packets(
            packet for packet in self.packets() if start <= packet.start and packet.end <= end
        )

    def checked_depth(self, depth):
        """Return this tape or raise a useful capacity error before touching the device."""
        unsigned(depth, U32, "depth")
        if len(self.events) > depth:
            raise ReplayError(f"tape contains {len(self.events)} beats but device holds {depth}")
        return self


@dataclass(frozen=True)
class Command:
    """One raw configuration request, which can intentionally carry invalid fields."""

    op: int
    tick: int = 0
    data: int = 0
    keep: int = 0
    last: bool = False
    tag: int = 0
    repeats: int = 0
    gap: int = 0

    def __post_init__(self):
        """Reject fields that cannot be driven on the pins while preserving bad opcodes."""
        unsigned(self.op, 7, "opcode")
        unsigned(self.tick, U32, "offset")
        unsigned(self.data, U64, "data")
        unsigned(self.keep, 0xFF, "keep")
        boolean(self.last, "last")
        unsigned(self.tag, U16, "tag")
        unsigned(self.repeats, U16, "repeat count")
        unsigned(self.gap, U32, "repeat gap")

    @classmethod
    def append(cls, event: Beat):
        """Convert a normalized tape beat into a write request."""
        return cls(Operation.APPEND, event.tick, event.data, event.keep, event.last, event.tag)

    @classmethod
    def start(cls, repeats=1, gap=0):
        """Build a replay request with an explicit count and pass separation."""
        return cls(Operation.START, repeats=repeats, gap=gap)

    def event(self):
        """Decode raw append fields into a checked tape beat."""
        return Beat(self.tick, self.data, self.keep, self.last, self.tag)


@dataclass(frozen=True)
class Response:
    """Status and image length sampled together from the response slot."""

    status: Status
    length: int


@dataclass(frozen=True)
class Offer:
    """A stable output beat and the timestamps captured when it was presented."""

    event: Beat
    due: int
    offered: int
    pass_number: int

    @property
    def late_by(self):
        """Report scheduler lateness in virtual ticks at offer creation."""
        return self.offered - self.due


@dataclass(frozen=True)
class Cycle:
    """Inputs driven before one positive edge of the replay core."""

    reset: bool = False
    cfg: Optional[Command] = None
    rsp_ready: bool = True
    ready: bool = True

    def __post_init__(self):
        """Guarantee bit-exact controls in cycle logs and stimulus files."""
        boolean(self.reset, "reset")
        boolean(self.rsp_ready, "response ready")
        boolean(self.ready, "stream ready")
        if self.cfg is not None and not isinstance(self.cfg, Command):
            raise ReplayError("cycle configuration must be a Command")


@dataclass(frozen=True)
class Statistics:
    """Counters count accepted output beats, their packet ends and their pass ends."""

    beats: int = 0
    packets: int = 0
    late_beats: int = 0
    passes: int = 0

    def receive(self, offer, end_pass):
        """Return counters after a transfer, saturating each independent field."""
        return Statistics(
            min(U32, self.beats + 1),
            min(U32, self.packets + int(offer.event.last)),
            min(U32, self.late_beats + int(offer.late_by > 0)),
            min(U32, self.passes + int(end_pass)),
        )
