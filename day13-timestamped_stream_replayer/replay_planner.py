"""Translate wall time into finite FPGA offsets and estimate offered-cycle timing."""

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

from replay_format import StampedPacket
from replay_program import LANES, Packet, ReplayError, Tape, U16, U32, U48, unsigned


def rational(value, label):
    """Normalize clocks and rates to finite rational numbers."""
    try:
        return Fraction(value)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError) as exc:
        raise ReplayError(f"{label} must be a finite rational number") from exc


def round_ticks(value, mode):
    """Apply floor, ceiling or ties-to-even exactly once at quantization."""
    ticks = rational(value, "ticks")
    lower, remainder = divmod(ticks.numerator, ticks.denominator)
    if mode == "floor":
        return lower
    if mode == "ceil":
        return lower + bool(remainder)
    if mode == "nearest":
        twice = remainder * 2
        if twice == ticks.denominator:
            return lower + (lower % 2)
        return lower + (twice > ticks.denominator)
    raise ReplayError(f"unknown tick rounding mode {mode!r}")


@dataclass(frozen=True)
class CompileOptions:
    """Virtual-clock and packet serialization policy for a finite tape."""

    clock_hz: Fraction
    lead_ticks: int = 0
    beat_spacing: int = 1
    packet_gap: int = 1
    rounding: str = "ceil"
    collision: str = "delay"
    limit_ticks: int = U32
    maximum_beats: Optional[int] = None
    speed: Fraction = Fraction(1)

    def __post_init__(self):
        """Validate timing knobs, bounded offsets, accepted policies and optional depth."""
        rate = rational(self.clock_hz, "clock")
        speed = rational(self.speed, "speed")
        if rate <= 0 or speed <= 0:
            raise ReplayError("clock and speed must be positive")
        unsigned(self.lead_ticks, U32, "lead ticks")
        unsigned(self.beat_spacing, U32, "beat spacing")
        unsigned(self.packet_gap, U32, "packet gap")
        unsigned(self.limit_ticks, U32, "maximum tick")
        if self.maximum_beats is not None:
            unsigned(self.maximum_beats, U32, "capacity")
        if self.lead_ticks > self.limit_ticks:
            raise ReplayError("lead time already exceeds the maximum tape tick")
        round_ticks(0, self.rounding)
        if self.collision not in ("delay", "fail"):
            raise ReplayError("collision policy must be delay or fail")
        object.__setattr__(self, "clock_hz", rate)
        object.__setattr__(self, "speed", speed)

    @property
    def period_ns(self):
        """Return the exact nanoseconds represented by one replay tick."""
        return Fraction(10**9, 1) / self.clock_hz

    def desired_tick(self, time_ns, epoch_ns):
        """Convert a capture timestamp relative to its origin, including speed scaling."""
        relative = rational(time_ns, "time") - rational(epoch_ns, "time origin")
        if relative < 0:
            raise ReplayError("packet timestamp precedes the selected origin")
        adjusted = relative / (self.period_ns * self.speed)
        return self.lead_ticks + round_ticks(adjusted, self.rounding)


@dataclass(frozen=True)
class PacketPlacement:
    """Map a capture packet to the requested and finally placed tape offsets."""

    position: int
    tag: int
    relative_ns: Fraction
    desired_tick: int
    actual_tick: int
    end_tick: int
    length: int

    @property
    def delay_ticks(self):
        """Return additional delay inserted to serialize packet records."""
        return self.actual_tick - self.desired_tick

    @property
    def beats(self):
        """Count the number of FPGA records occupied by the packet."""
        return (self.length + LANES - 1) // LANES

    def record(self):
        """Return scheduling diagnostics in a JSON-compatible mapping."""
        return {
            "position": self.position,
            "tag": self.tag,
            "relative_ns": str(self.relative_ns),
            "desired_tick": self.desired_tick,
            "actual_tick": self.actual_tick,
            "last_tick": self.end_tick,
            "delay_ticks": self.delay_ticks,
            "bytes": self.length,
            "beats": self.beats,
        }


@dataclass(frozen=True)
class CompileResult:
    """The checked tape plus all quantization and serialization decisions."""

    tape: Tape
    origin: Fraction
    placements: tuple

    @property
    def shifted(self):
        """Count capture packets that required a serialization delay."""
        return sum(p.delay_ticks > 0 for p in self.placements)

    @property
    def worst_shift(self):
        """Return the largest insertion delay, or zero for an empty capture."""
        return max((p.delay_ticks for p in self.placements), default=0)

    def report(self, *, verbose=False):
        """Render compile totals and optional per-packet placement details."""
        info = {
            "origin_ns": str(self.origin),
            "packets": len(self.placements),
            "beats": len(self.tape.events),
            "bytes": self.tape.payload_bytes,
            "duration_ticks": self.tape.duration,
            "shifted_packets": self.shifted,
            "worst_shift_ticks": self.worst_shift,
        }
        if verbose:
            info["placements"] = [packet.record() for packet in self.placements]
        return info


def compile_packets(stamped, options, *, origin=None):
    """Quantize ordered complete packets and enforce depth before programming the FPGA."""
    start_point = rational(origin, "time origin") if origin is not None else None
    if start_point is not None and start_point < 0:
        raise ReplayError("time origin must be nonnegative")
    placements = []
    packets = []
    bytes_used = 0
    beats_used = 0
    prior_time = start_point
    earliest = 0
    for position, captured in enumerate(stamped):
        if not isinstance(captured, StampedPacket):
            raise ReplayError(f"packet {position} is not a timed capture record")
        if prior_time is not None and captured.time_ns < prior_time:
            raise ReplayError(f"packet {position} timestamp is earlier than its predecessor or origin")
        if start_point is None:
            start_point = captured.time_ns
        prior_time = captured.time_ns
        due = options.desired_tick(captured.time_ns, start_point)
        if due < earliest and options.collision == "fail":
            raise ReplayError(f"packet {position} requests tick {due}, before available tick {earliest}")
        actual = max(due, earliest)
        length = len(captured.payload)
        beats = (length + LANES - 1) // LANES
        end = actual + (beats - 1) * options.beat_spacing
        if end > options.limit_ticks:
            raise ReplayError(f"packet {position} would exceed maximum offset at tick {end}")
        if options.maximum_beats is not None and beats_used + beats > options.maximum_beats:
            raise ReplayError(f"packet {position} would exceed {options.maximum_beats} replay entries")
        packet = Packet.paced(captured.tag, captured.payload, actual, options.beat_spacing)
        packets.append(packet)
        placements.append(PacketPlacement(
            position, captured.tag, captured.time_ns-start_point, due, actual, end, length,
        ))
        beats_used += beats
        bytes_used += length
        earliest = end + options.packet_gap
    tape = Tape.from_packets(packets)
    if tape.payload_bytes != bytes_used:
        raise AssertionError("tape byte count does not match compiled capture")
    return CompileResult(tape, start_point or Fraction(0), tuple(placements))


def rebase_tape(tape, offset=0):
    """Shift all beats so the first one lands at the given tick, preserving deltas."""
    unsigned(offset, U32, "initial offset")
    if not tape.events:
        return tape
    delta = offset - tape.events[0].tick
    return Tape(tuple(event.at(event.tick+delta) for event in tape.events))


def retime_tape(tape, multiplier, mode="ceil"):
    """Scale time intervals using exact arithmetic before bounded tick conversion."""
    factor = rational(multiplier, "retiming factor")
    if factor <= 0:
        raise ReplayError("retiming factor must be positive")
    round_ticks(0, mode)
    return Tape(tuple(event.at(round_ticks(event.tick*factor, mode)) for event in tape.events))


@dataclass(frozen=True)
class PredictedBeat:
    """A no-stall timing prediction for one event in one replay pass."""

    pass_number: int
    tape_index: int
    event: object
    due: int
    offered: int
    accepted: int

    @property
    def late(self):
        """Return cycle slipping due to equal deadlines or an earlier packed beat."""
        return self.offered - self.due


def predict(tape, repeats=1, gap=0, *, max_events=100000):
    """Yield exact no-stall offer and transfer ticks without stepping empty clock edges."""
    unsigned(repeats, U16, "repeats")
    unsigned(gap, U32, "repeat gap")
    unsigned(max_events, U32, "prediction limit")
    if not repeats or not tape.events:
        raise ReplayError("prediction requires a nonempty tape and a positive repeat count")
    if repeats * len(tape.events) > max_events:
        raise ReplayError("prediction would exceed the configured event limit")
    base = 0
    for number in range(1, repeats+1):
        earliest_offer = base
        accepted = 0
        for tape_index, event in enumerate(tape.events):
            due = min(U48, base + event.tick)
            offered = max(due, earliest_offer)
            accepted = min(U48, offered+1)
            yield PredictedBeat(number, tape_index, event, due, offered, accepted)
            earliest_offer = accepted
        base = min(U48, accepted + gap + 1)


def predict_report(tape, repeats=1, gap=0, *, max_events=100000):
    """Summarize the estimated schedule, including intrinsic queue lateness."""
    count = 0
    late = 0
    maximum = 0
    packets = 0
    last = None
    for beat in predict(tape, repeats, gap, max_events=max_events):
        count += 1
        late += beat.late > 0
        maximum = max(maximum, beat.late)
        packets += beat.event.last
        last = beat.accepted
    return {
        "emitted_beats": count,
        "emitted_packets": packets,
        "late_beats": late,
        "maximum_lateness_ticks": maximum,
        "last_transfer_tick": last,
        "passes": repeats,
    }
