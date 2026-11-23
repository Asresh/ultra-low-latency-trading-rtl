"""Independent packet and cycle references for the store-and-forward gearbox."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import IntEnum


class Status(IntEnum):
    OK = 0
    BAD_KEEP = 1
    BAD_TAG = 2
    TOO_LONG = 3


def require_unsigned(value, bits, label):
    """Check unsigned fields before packing them for the simulator."""
    if not isinstance(value, int) or not 0 <= value < (1 << bits):
        raise ValueError(f"{label} must fit in {bits} bits")
    return value


def full(width):
    """Return the lane mask for an unpadded beat."""
    return (1 << width) - 1


@dataclass(frozen=True)
class Beat:
    data: int = 0
    keep: int = 0
    last: bool = False
    tag: int = 0

    def validate(self, width):
        """Reject field values that the physical port cannot represent."""
        require_unsigned(self.data, width * 8, "beat data")
        require_unsigned(self.keep, width, "lane mask")
        require_unsigned(self.tag, 16, "packet tag")
        if self.last not in (True, False):
            raise ValueError("last must be a single bit")

    def bytes(self, width):
        """Extract physically selected lanes in increasing byte order."""
        self.validate(width)
        lanes = self.data.to_bytes(width, "little")
        return bytes(byte for lane, byte in enumerate(lanes) if self.keep & (1 << lane))


@dataclass(frozen=True)
class Report:
    tag: int = 0
    length: int = 0
    status: Status = Status.OK


@dataclass(frozen=True)
class Drive:
    valid: bool = False
    beat: Beat = Beat()
    ready: bool = True
    report_ready: bool = True
    reset: bool = False


@dataclass(frozen=True)
class Sample:
    input_ready: bool
    output: Beat | None
    report: Report | None
    accepted: int
    rejected: int
    emitted: int
    output_bytes: int

    @property
    def counters(self):
        """Keep the four observable statistics in hardware port order."""
        return self.accepted, self.rejected, self.emitted, self.output_bytes


def to_beats(payload, width, tag, fill=0):
    """Split a nonempty packet into lane-order beats with explicit padding."""
    if not payload:
        raise ValueError("packets must have at least one payload byte")
    if not 1 <= width <= 16:
        raise ValueError("lane width must be between 1 and 16")
    require_unsigned(tag, 16, "packet tag")
    require_unsigned(fill, 8, "padding lane")
    for offset in range(0, len(payload), width):
        fragment = payload[offset:offset + width]
        word = fragment.ljust(width, bytes([fill]))
        yield Beat(
            int.from_bytes(word, "little"),
            full(len(fragment)),
            offset + width >= len(payload),
            tag,
        )


def validate_packet(beats, width, capacity):
    """Classify a whole packet using first-fault priority in input order."""
    if not beats or any(beat.last for beat in beats[:-1]) or not beats[-1].last:
        raise ValueError("model expects exactly one complete frame")
    payload = bytearray()
    original_tag = beats[0].tag
    status = Status.OK
    for beat in beats:
        fragment = beat.bytes(width)
        payload.extend(fragment)
        if status != Status.OK:
            continue
        if beat.keep != full(len(fragment)) or not fragment or (
            not beat.last and len(fragment) != width
        ):
            status = Status.BAD_KEEP
        elif beat.tag != original_tag:
            status = Status.BAD_TAG
        elif len(payload) > capacity:
            status = Status.TOO_LONG
    report = Report(original_tag, min(len(payload), 0xFFFF), status)
    return report, bytes(payload) if status == Status.OK else None


class Gearbox:
    """Model packet occupancy with a deque, independent of physical RAM banks."""

    def __init__(self, input_width, output_width, capacity):
        """Configure the port widths and maximum bytes per packet."""
        if min(input_width, output_width, capacity) < 1:
            raise ValueError("widths and capacity must be positive")
        if max(input_width, output_width) > 16 or capacity > 0xFFFF:
            raise ValueError("geometry exceeds the host fixture encoding")
        self.input_width = input_width
        self.output_width = output_width
        self.capacity = capacity
        self.reset()

    def reset(self):
        """Invalidate pending packet work and zero all public statistics."""
        self.pending = deque()
        self.input_packet = []
        self.report = None
        self.out = None
        self.accepted = 0
        self.rejected = 0
        self.emitted = 0
        self.output_bytes = 0

    def observe(self, drive):
        """Compute pre-edge signals without mutating the queues."""
        available = len(self.pending) < 2 and (
            self.input_packet or self.report is None or drive.report_ready
        )
        return Sample(
            bool(not drive.reset and available),
            None if drive.reset else self.out,
            self.report,
            self.accepted,
            self.rejected,
            self.emitted,
            self.output_bytes,
        )

    def advance(self, drive):
        """Sample one clock edge and apply independent input and output transfers."""
        before = self.observe(drive)
        if drive.reset:
            self.reset()
            return before
        if drive.report_ready:
            self.report = None
        if drive.ready and before.output is not None:
            self._retire_output()
        if before.input_ready and drive.valid:
            self._capture(drive.beat)
        self.out = self.pending[0][0] if self.pending else None
        return before

    def _capture(self, beat):
        """Accumulate accepted beats until the packet can be validated as a whole."""
        beat.validate(self.input_width)
        self.input_packet.append(beat)
        if not beat.last:
            return
        self.report, payload = validate_packet(self.input_packet, self.input_width, self.capacity)
        self.input_packet.clear()
        if payload is None:
            self.rejected = min(self.rejected + 1, 0xFFFFFFFF)
        else:
            self.pending.append(deque(to_beats(payload, self.output_width, self.report.tag)))
            self.accepted = min(self.accepted + 1, 0xFFFFFFFF)

    def _retire_output(self):
        """Release one physical output beat and a bank only at its packet boundary."""
        beat = self.pending[0].popleft()
        self.output_bytes = min(self.output_bytes + len(beat.bytes(self.output_width)), 0xFFFFFFFF)
        if beat.last:
            self.pending.popleft()
            self.emitted = min(self.emitted + 1, 0xFFFFFFFF)
