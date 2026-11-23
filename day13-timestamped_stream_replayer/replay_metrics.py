"""Summarize accepted traffic, real-clock stalls, and virtual-time scheduling error."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import zip_longest
from pathlib import Path
from typing import Optional

from replay_audit import read_transcript
from replay_program import LANES, ReplayError, lanes_from_keep, mask_data, unsigned, U16, U48
from replay_vectors import read_cyclestream


@dataclass(frozen=True)
class Delivery:
    """A valid replay offer sampled on a completed physical handshake."""

    cycle: int
    pass_number: int
    tag: int
    payload: bytes
    last: bool
    due: int
    offered: int
    accepted: int

    def __post_init__(self):
        """Validate identity, payload geometry, and chronological timing fields."""
        if self.cycle < 0:
            raise ReplayError("physical cycle cannot be negative")
        unsigned(self.pass_number, U16, "pass")
        unsigned(self.tag, U16, "tag")
        if self.pass_number < 1:
            raise ReplayError("delivered beat must belong to a replay pass")
        if type(self.payload) is not bytes or not 1 <= len(self.payload) <= LANES:
            raise ReplayError("delivered payload length must fit a beat")
        if type(self.last) is not bool or (not self.last and len(self.payload) != LANES):
            raise ReplayError("delivery framing disagrees with its lane count")
        for tick in (self.due, self.offered, self.accepted):
            unsigned(tick, U48, "delivery tick")
        if self.due > self.offered or self.offered > self.accepted:
            raise ReplayError("delivery time precedes its due or offered time")

    @classmethod
    def capture(cls, sample):
        """Decode the offered payload and keep only canonical lanes."""
        offer = sample.offer
        if offer is None:
            raise ReplayError("delivery needs valid stream pins")
        count = lanes_from_keep(offer.keep)
        if mask_data(offer.data, offer.keep) != offer.data:
            raise ReplayError("delivered data has nonzero inactive byte lanes")
        value = offer.data.to_bytes(LANES, "little")[:count]
        return cls(sample.index, offer.pass_number, offer.tag, value, offer.last,
                   offer.due, offer.offered, sample.now)

    @property
    def lateness(self):
        """Return timing error caused by serialization or previous backpressure."""
        return self.offered - self.due

    @property
    def held(self):
        """Return how far virtual time advanced between offer and acceptance."""
        return self.accepted - self.offered


@dataclass(frozen=True)
class PacketDelivery:
    """One emitted packet with its tag, pass, and timing of every beat."""

    deliveries: tuple

    def __post_init__(self):
        """Require all beats to share a pass and tag and end at exactly one last."""
        if not self.deliveries or not self.deliveries[-1].last:
            raise ReplayError("captured packet has no final beat")
        reference = self.deliveries[0]
        for index, record in enumerate(self.deliveries):
            if (record.tag, record.pass_number) != (reference.tag, reference.pass_number):
                raise ReplayError("captured packet changed tag or pass")
            if record.last and index != len(self.deliveries)-1:
                raise ReplayError("captured packet contains more than one ending")
        if any(a.cycle >= b.cycle for a, b in zip(self.deliveries, self.deliveries[1:])):
            raise ReplayError("captured packet beat order is not chronological")

    @property
    def tag(self):
        """Return the stable packet identity."""
        return self.deliveries[0].tag

    @property
    def pass_number(self):
        """Return the pass that emitted this complete packet."""
        return self.deliveries[0].pass_number

    @property
    def payload(self):
        """Join active byte lanes across all delivered beats."""
        return b"".join(beat.payload for beat in self.deliveries)

    @property
    def first(self):
        """Return the first accepted beat and its arrival metadata."""
        return self.deliveries[0]

    @property
    def last(self):
        """Return the terminal accepted beat and its arrival metadata."""
        return self.deliveries[-1]


class PacketAssembler:
    """Assemble output beats, preserving partial data when stop or reset cuts a packet."""

    def __init__(self):
        """Start with no open packet and no complete or interrupted payloads."""
        self.pending = []
        self.complete = []
        self.cut = []

    def step(self, record):
        """Validate an accepted beat and commit a packet only on its final marker."""
        if self.pending:
            head = self.pending[0]
            if (record.pass_number, record.tag) != (head.pass_number, head.tag):
                raise ReplayError("new pass or tag arrived before previous packet ended")
        self.pending.append(record)
        if record.last:
            self.complete.append(PacketDelivery(tuple(self.pending)))
            self.pending.clear()

    def end_run(self):
        """Move an unfinished fragment to the cut list without claiming a packet."""
        if self.pending:
            self.cut.append(tuple(self.pending))
            self.pending.clear()


@dataclass(frozen=True)
class Distribution:
    """Compact exact discrete count and nearest-rank statistics."""

    count: int
    minimum: int
    maximum: int
    total: int
    at_median: int
    at_p95: int
    at_p99: int

    @classmethod
    def from_samples(cls, samples):
        """Sort finite samples and calculate exact rank statistics for nonempty inputs."""
        data = sorted(samples)
        if not data:
            return cls(0, 0, 0, 0, 0, 0, 0)
        count = len(data)
        return cls(count, data[0], data[-1], sum(data),
                   data[(count+1)//2-1], data[(95*count+99)//100-1],
                   data[(99*count+99)//100-1])

    @property
    def average(self):
        """Return the exact arithmetic mean from measured counts."""
        return Fraction(self.total, self.count) if self.count else None

    def record(self):
        """Record ticks without rounding away the exact mean."""
        return {
            "count": self.count,
            "min": self.minimum,
            "max": self.maximum,
            "mean": str(self.average) if self.average is not None else None,
            "median": self.at_median,
            "p95": self.at_p95,
            "p99": self.at_p99,
        }


@dataclass
class ReplayRun:
    """Physical activity and accepted beats between activation and termination."""

    index: int
    began: int
    ended: Optional[int] = None
    end_reason: Optional[str] = None
    paused_cycles: int = 0
    output_hold_cycles: int = 0
    response_hold_cycles: int = 0
    deliveries: list = field(default_factory=list)
    packets: PacketAssembler = field(default_factory=PacketAssembler)

    def add(self, record):
        """Track untampered accepted order and assemble completed payloads."""
        if self.deliveries:
            prior = self.deliveries[-1]
            if record.cycle <= prior.cycle:
                raise ReplayError("run repeats or reverses a physical transfer cycle")
            if not prior.pass_number <= record.pass_number <= prior.pass_number+1:
                raise ReplayError("run skips or reverses pass order")
        elif record.pass_number != 1:
            raise ReplayError("run must start at pass 1")
        self.packets.step(record)
        self.deliveries.append(record)

    def finish(self, cycle, reason):
        """Finalize counters and record any fragment cut by stop or reset."""
        if self.ended is not None:
            raise ReplayError("a replay run cannot end twice")
        if cycle < self.began:
            raise ReplayError("run end predates activation")
        self.ended = cycle
        self.end_reason = reason
        self.packets.end_run()

    @property
    def wall_cycles(self):
        """Return active physical clock slots, including time lost to pause or stalls."""
        return self.ended - self.began if self.ended is not None else None

    @property
    def cycles_to_first(self):
        """Measure physical edges from active state to first accepted beat."""
        return self.deliveries[0].cycle-self.began if self.deliveries else None

    def group(self, field_name):
        """Group delivered beats by the requested tag or pass number."""
        if field_name not in ("tag", "pass_number"):
            raise ReplayError("group must be tag or pass_number")
        result = defaultdict(list)
        for delivery in self.deliveries:
            result[getattr(delivery, field_name)].append(delivery)
        return dict(result)

    def record(self, *, detailed=False):
        """Create a stable report separating physical stalls from virtual timing."""
        result = {
            "run": self.index,
            "start_index": self.began,
            "end_index": self.ended,
            "end_reason": self.end_reason,
            "wall_cycles": self.wall_cycles,
            "paused_cycles": self.paused_cycles,
            "stream_hold_cycles": self.output_hold_cycles,
            "response_hold_cycles": self.response_hold_cycles,
            "cycles_to_first": self.cycles_to_first,
            "beats": len(self.deliveries),
            "bytes": sum(len(record.payload) for record in self.deliveries),
            "packets": len(self.packets.complete),
            "cut_partial_packets": len(self.packets.cut),
            "total_passes_seen": len(self.group("pass_number")),
            "late_virtual_ticks": Distribution.from_samples(beat.lateness for beat in self.deliveries).record(),
            "hold_virtual_ticks": Distribution.from_samples(beat.held for beat in self.deliveries).record(),
        }
        if detailed:
            result["beats_per_tag"] = {tag: len(beats) for tag, beats in sorted(self.group("tag").items())}
            result["beats_per_pass"] = {
                number: len(beats) for number, beats in sorted(self.group("pass_number").items())
            }
            result["packet_lengths"] = dict(sorted(Counter(
                len(packet.payload) for packet in self.packets.complete
            ).items()))
        return result


def extract_runs(cycles, samples, *, allow_incomplete=False):
    """Pair a stimulus and pin capture to reconstruct runs and accepted payloads."""
    current = None
    result = []
    missing = object()
    last_cycle = None
    for index, (controls, sample) in enumerate(zip_longest(cycles, samples, fillvalue=missing)):
        if controls is missing or sample is missing:
            raise ReplayError(f"timeline length mismatch at cycle {index}")
        if sample.index != index:
            raise ReplayError(f"timeline has wrong sample index at cycle {index}")
        last_cycle = index
        if controls.reset:
            if current is not None:
                current.finish(index, "reset")
                current = None
            continue
        if sample.active:
            if current is None:
                current = ReplayRun(len(result), index)
                result.append(current)
            current.paused_cycles += sample.paused
            current.response_hold_cycles += sample.response is not None and not controls.rsp_ready
            if sample.offer is not None:
                if controls.ready:
                    current.add(Delivery.capture(sample))
                else:
                    current.output_hold_cycles += 1
        elif current is not None:
            if not sample.done:
                raise ReplayError(f"active fell without done or reset at cycle {index}")
            current.finish(index, "done")
            current = None
    if current is not None:
        if not allow_incomplete:
            raise ReplayError("capture ended during an active replay run")
        current.finish(last_cycle+1, "capture_end")
    if last_cycle is None:
        raise ReplayError("replay trace is empty")
    return tuple(result)


def load_runs(stimulus, transcript, *, allow_incomplete=False):
    """Read canonical fixture and transcript files before extracting run measurements."""
    return extract_runs(read_cyclestream(Path(stimulus)), read_transcript(Path(transcript)),
                        allow_incomplete=allow_incomplete)


@dataclass(frozen=True)
class Budget:
    """Independent performance constraints applied only when the user requests them."""

    maximum_lateness: Optional[int] = None
    maximum_first_transfer: Optional[int] = None
    allow_fragments: bool = True

    def __post_init__(self):
        """Check optional bound units and the cut-packet policy."""
        for limit in (self.maximum_lateness, self.maximum_first_transfer):
            if limit is not None and (type(limit) is not int or limit < 0):
                raise ReplayError("performance budgets must be nonnegative integers")
        if type(self.allow_fragments) is not bool:
            raise ReplayError("fragment policy must be Boolean")

    def failures(self, runs):
        """List each violated constraint with a run number and measured value."""
        for item in runs:
            late = max((event.lateness for event in item.deliveries), default=0)
            first = item.cycles_to_first
            if self.maximum_lateness is not None and late > self.maximum_lateness:
                yield f"run {item.index}: lateness {late} exceeds {self.maximum_lateness} virtual ticks"
            if (self.maximum_first_transfer is not None and first is not None
                    and first > self.maximum_first_transfer):
                yield f"run {item.index}: first transfer {first} exceeds {self.maximum_first_transfer} cycles"
            if not self.allow_fragments and item.packets.cut:
                yield f"run {item.index}: output ended with a partial packet"
