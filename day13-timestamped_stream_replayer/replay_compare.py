"""Compare captured replay traffic with its programmed tape and export accepted packets."""

from dataclasses import dataclass
from fractions import Fraction

from replay_format import StampedPacket
from replay_planner import rational
from replay_program import ReplayError, U16, U32, U48, unsigned


@dataclass(frozen=True)
class ComparisonOptions:
    """Describe the expected replay run and the allowed end-of-capture conditions."""

    repeats: int = 1
    repeat_gap: int = 0
    allow_cut: bool = False
    maximum_lateness: object = None

    def __post_init__(self):
        """Validate firmware field bounds before using policy on a captured run."""
        unsigned(self.repeats, U16, "repeat count")
        unsigned(self.repeat_gap, U32, "repeat gap")
        if self.repeats == 0:
            raise ReplayError("comparison requires a positive number of passes")
        if type(self.allow_cut) is not bool:
            raise ReplayError("cut policy must be Boolean")
        if self.maximum_lateness is not None:
            unsigned(self.maximum_lateness, U48, "maximum lateness")


@dataclass(frozen=True)
class PassReport:
    """Comparison totals for one hardware pass, complete or interrupted."""

    pass_number: int
    base: int
    beats_expected: int
    beats_seen: int
    packet_ends: int
    payload_bytes: int
    latest_accept: int
    latest_lateness: int

    @property
    def complete(self):
        """Return whether the hardware delivered the entire pass."""
        return self.beats_expected == self.beats_seen

    def record(self):
        """Render pass geometry and measured output counts as JSON-compatible fields."""
        return {
            "pass": self.pass_number,
            "base_tick": self.base,
            "expected_beats": self.beats_expected,
            "accepted_beats": self.beats_seen,
            "packets": self.packet_ends,
            "accepted_bytes": self.payload_bytes,
            "last_accept_tick": self.latest_accept,
            "max_offer_delay_ticks": self.latest_lateness,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class Comparison:
    """One successful known-program comparison and its measured pass details."""

    expected_beats: int
    accepted_beats: int
    source_packets: int
    passes: tuple

    @property
    def missing(self):
        """Return the suffix length that was not observed at a legal cut."""
        return self.expected_beats - self.accepted_beats

    def record(self):
        """Render totals without claiming interrupted passes were successful."""
        return {
            "expected_beats": self.expected_beats,
            "accepted_beats": self.accepted_beats,
            "missing_beats": self.missing,
            "source_packets_per_pass": self.source_packets,
            "complete_passes": sum(report.complete for report in self.passes),
            "passes": [report.record() for report in self.passes],
        }


def valid_delivery(event, captured, *, due, index, pass_number, maximum_lateness=None):
    """Check the payload, frame, label, and exact deadline for one accepted beat."""
    position = f"pass {pass_number} beat {index}"
    if captured.pass_number != pass_number:
        raise ReplayError(f"{position}: captured pass is {captured.pass_number}")
    if captured.payload != event.payload:
        raise ReplayError(f"{position}: mismatched payload bytes")
    if captured.last != event.last:
        raise ReplayError(f"{position}: mismatched packet boundary")
    if captured.tag != event.tag:
        raise ReplayError(f"{position}: captured tag {captured.tag}, expected {event.tag}")
    if captured.due != due:
        raise ReplayError(f"{position}: scheduled tick {captured.due}, expected {due}")
    if maximum_lateness is not None and captured.lateness > maximum_lateness:
        raise ReplayError(f"{position}: offer exceeds the configured delay budget")


def check_run(tape, run, options=ComparisonOptions()):
    """Check an observed full run or prefix against the immutable programmed event order."""
    width = len(tape.events)
    if not width:
        raise ReplayError("comparison needs a nonempty programmed tape")
    source_packets = sum(int(event.last) for event in tape.events)
    expected = width * options.repeats
    transfers = run.deliveries
    if len(transfers) > expected:
        raise ReplayError("captured run delivered more beats than the programmed tape")
    if not options.allow_cut and len(transfers) < expected:
        raise ReplayError(f"captured only {len(transfers)} of {expected} expected beats")

    base, pointer = 0, 0
    summaries = []
    for pass_number in range(1, options.repeats+1):
        packet_ends = 0
        bytes_seen = 0
        length = min(width, len(transfers)-pointer)
        if length == 0:
            break
        final_time = None
        max_late = 0
        for index, (item, event) in enumerate(zip(transfers[pointer:pointer+length], tape.events)):
            due = min(U48, base + event.tick)
            valid_delivery(event, item, due=due, index=index, pass_number=pass_number,
                           maximum_lateness=options.maximum_lateness)
            final_time = item.accepted
            bytes_seen += len(item.payload)
            packet_ends += item.last
            max_late = max(max_late, item.lateness)
        summaries.append(PassReport(
            pass_number, base, width, length, packet_ends, bytes_seen, final_time, max_late,
        ))
        pointer += length
        base = min(U48, final_time + options.repeat_gap + 1)

    return Comparison(expected, len(transfers), source_packets, tuple(summaries))


def select_run(runs, index=None):
    """Resolve an explicit run index or a trace containing exactly one run."""
    if not runs:
        raise ReplayError("trace contains no active runs")
    if index is None:
        if len(runs) != 1:
            raise ReplayError("trace has multiple runs; choose one with --run")
        return runs[0]
    if type(index) is not int or not 0 <= index < len(runs):
        raise ReplayError(f"run index {index} falls outside 0..{len(runs)-1}")
    return runs[index]


def packet_timestamp(packet, reference):
    """Read one supported event timestamp from the first beat of a complete packet."""
    if reference == "physical":
        return packet.first.cycle
    if reference == "accepted":
        return packet.first.accepted
    if reference == "offered":
        return packet.first.offered
    if reference == "due":
        return packet.first.due
    raise ReplayError(f"invalid export time base {reference!r}")


def export_packets(run, clock_hz, *, reference="physical", rebase=True, selected_pass=None):
    """Export accepted complete packets as exact stamped traffic for a second replay."""
    rate = rational(clock_hz, "export clock")
    if rate <= 0:
        raise ReplayError("export clock must be positive")
    period = Fraction(10**9, 1) / rate
    if selected_pass is not None:
        unsigned(selected_pass, U16, "export pass")
        if selected_pass == 0:
            raise ReplayError("export pass must be positive")
    first_tick = None
    prior_tick = None
    for packet in run.packets.complete:
        if selected_pass is not None and packet.pass_number != selected_pass:
            continue
        since = packet_timestamp(packet, reference)
        if prior_tick is not None and since < prior_tick:
            raise ReplayError("selected packet stamps are not chronological")
        if first_tick is None:
            first_tick = since if rebase else 0
        prior_tick = since
        yield StampedPacket((since-first_tick)*period, packet.tag, packet.payload)
