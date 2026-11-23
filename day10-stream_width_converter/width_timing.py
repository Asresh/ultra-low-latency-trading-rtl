"""Compare model clock budgets for two implementations on the same legal traffic."""

import argparse
import csv
from collections import Counter, deque
from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path

from width_model import Beat, Drive, Gearbox, to_beats
from width_profile import quantile
from width_recipe import fields, integer
from width_stream import Edge, QueueOracle


class TimingError(ValueError):
    """A workload is too large or cannot fit in one of its comparison profiles."""


@dataclass(frozen=True)
class Arrival:
    length: int
    gap: int

    @classmethod
    def parse(cls, raw):
        """Validate a CSV row and preserve a zero gap as adjacent arrivals."""
        try:
            fields(raw, {"length", "gap"})
            size = int(raw["length"])
            spacing = int(raw["gap"])
            if str(size) != raw["length"] or str(spacing) != raw["gap"]:
                raise ValueError("fields must use canonical unsigned integers")
            return cls(integer(size, "length", 65535, 1), integer(spacing, "gap", 100000))
        except (TypeError, ValueError) as exc:
            raise TimingError(str(exc)) from exc


@dataclass(frozen=True)
class Ports:
    input_width: int
    output_width: int
    packet_limit: int
    byte_capacity: int

    def check(self):
        """Distinguish eligible packet storage from the streaming buffer's pin geometry."""
        for width in (self.input_width, self.output_width):
            if type(width) is not int or not 1 <= width <= 16:
                raise TimingError("physical width must be 1–16 bytes")
        if not 1 <= self.packet_limit <= 65535:
            raise TimingError("packet bank must hold 1–65535 bytes")
        if not self.input_width + self.output_width <= self.byte_capacity <= 65535:
            raise TimingError("byte FIFO must hold one beat from both ports")


@dataclass(frozen=True)
class Ready:
    period: int
    busy_cycles: int

    def __post_init__(self):
        """A repeating pause always has at least one available cycle."""
        if type(self.period) is not int or not 1 <= self.period <= 4096:
            raise TimingError("ready period must be between 1 and 4096")
        if type(self.busy_cycles) is not int or not 0 <= self.busy_cycles < self.period:
            raise TimingError("busy cycles must be smaller than the period")

    def at(self, edge):
        """Repeat an explicit low-before-high ready schedule."""
        return edge % self.period >= self.busy_cycles


class Traffic:
    """Hold packet transfers while comparing both implementations on identical arrivals."""

    def __init__(self, sizes, width):
        """Store each arrival cycle and a complete packet's synthetic byte image."""
        at = 0
        self.unstarted = deque()
        for tag, shape in enumerate(sizes, 1):
            at += shape.gap
            if at > 120000 or tag > 4_000:
                raise TimingError("traffic exceeds comparison event horizon")
            frame = bytes((17 * tag + i * 31) & 255 for i in range(shape.length))
            self.unstarted.append((at, tuple(to_beats(frame, width, tag))))
        self.offered = None
        self.active = deque()
        self.accepted_bytes = 0

    def offer(self, edge):
        """Return the ready source beat or wait for the earliest packet arrival."""
        if self.offered is not None:
            return self.offered
        if not self.active and self.unstarted and self.unstarted[0][0] <= edge:
            _, upcoming = self.unstarted.popleft()
            self.active.extend(upcoming)
        if self.active:
            self.offered = self.active.popleft()
        return self.offered

    def retire(self, width):
        """Advance only after a real source handshake and retain transfer byte counts."""
        self.accepted_bytes += len(self.offered.bytes(width))
        self.offered = None

    @property
    def done(self):
        """Require the queue, current packet, and pending beat to be empty together."""
        return not (self.offered or self.active or self.unstarted)


class Timings:
    """Track first and final edges of each independently tagged packet."""

    def __init__(self, size):
        """Initialize source, egress, backlog and utilization counters."""
        self.arrived = {}
        self.commits = {}
        self.first = {}
        self.ends = {}
        self.received = Counter()
        self.peak_backlog = 0
        self.edge_count = 0
        self.source_stalls = 0
        self.sink_stalls = 0
        self.simultaneous = 0
        self.size = size

    def step(self, cycle, offered, flowing, emitted, ready, iw, ow):
        """Account for only ready/valid transfers and their physical padding."""
        took = offered is not None and flowing
        sent = emitted is not None and ready
        self.edge_count = cycle + 1
        self.source_stalls += int(offered is not None and not flowing)
        self.sink_stalls += int(emitted is not None and not ready)
        self.simultaneous += int(took and sent)
        if took:
            self.arrived.setdefault(offered.tag, cycle)
            if offered.last:
                self.commits[offered.tag] = cycle
        if sent:
            tag = emitted.tag
            self.first.setdefault(tag, cycle)
            self.received[tag] += len(emitted.bytes(ow))
            if emitted.last:
                if tag in self.ends:
                    raise TimingError(f"duplicate packet end for tag {tag}")
                self.ends[tag] = cycle
        self.peak_backlog = max(self.peak_backlog, len(self.arrived) - len(self.ends))
        return took

    def metrics(self):
        """Summarize first-visibility, packet completion, and total active duration."""
        want = {index: value.length for index, value in enumerate(self.size, 1)}
        if dict(self.received) != want or set(self.ends) != set(want):
            raise TimingError("comparison did not conserve every packet length and boundary")
        complete = [self.ends[k] - self.arrived[k] for k in want]
        output_lead = [self.first[k] - self.commits[k] for k in want]
        av = sum(want.values())
        return {
            "packets": len(want),
            "payload_bytes": av,
            "cycles": self.edge_count,
            "payload_per_cycle": str(Fraction(av, self.edge_count)),
            "source_stall_edges": self.source_stalls,
            "sink_stall_edges": self.sink_stalls,
            "simultaneous_edges": self.simultaneous,
            "max_packets_in_flight": self.peak_backlog,
            "outputs_started_before_commit": sum(value < 0 for value in output_lead),
            "first_after_commit_p50": quantile(output_lead, 50),
            "first_after_commit_p95": quantile(output_lead, 95),
            "complete_after_input_start_p95": quantile(complete, 95),
        }


def summarize(sizes, geometry, downstream, mode, cycle_limit=160000):
    """Drive one reference implementation without using its internal history as a latency oracle."""
    geometry.check()
    frames = tuple(sizes)
    if not frames or any(not 1 <= item.length <= geometry.packet_limit for item in frames):
        raise TimingError("all packets must fit both implementations")
    iw, ow = geometry.input_width, geometry.output_width
    traffic = Traffic(frames, iw)
    timestamp = Timings(frames)
    if mode == "packet":
        dut = Gearbox(iw, ow, geometry.packet_limit)
    elif mode == "stream":
        dut = QueueOracle(iw, ow, geometry.byte_capacity)
    else:
        raise TimingError("unknown model implementation")
    for cycles in range(cycle_limit):
        current = traffic.offer(cycles)
        valid = current is not None
        inbound = current or Beat()
        ready = downstream.at(cycles)
        if mode == "packet":
            status = dut.advance(Drive(valid, inbound, ready, True))
            outgoing = status.output
            source_ready = status.input_ready
        else:
            status = dut.advance(Edge(inbound, valid, ready))
            outgoing = status.outgoing if status.valid else None
            source_ready = status.ready
        if timestamp.step(cycles, current, source_ready, outgoing, ready, iw, ow):
            traffic.retire(iw)
        if traffic.done and len(timestamp.ends) == len(frames):
            return timestamp.metrics()
    raise TimingError("traffic did not drain within the elapsed-clock budget")


def compare(sizes, geometry, downstream):
    """Return cycle- and space-based comparison points for the same packet list."""
    frames = tuple(sizes)
    packet = summarize(frames, geometry, downstream, "packet")
    stream = summarize(frames, geometry, downstream, "stream")
    tag_bits = 16
    stream_bytes = geometry.byte_capacity
    return {
        "store_and_forward": packet,
        "cut_through": stream,
        "cycles_saved_by_cut_through": packet["cycles"] - stream["cycles"],
        "payload_storage_bytes": {"store_and_forward": 2 * geometry.packet_limit, "cut_through": stream_bytes},
        "cut_through_metadata_bits": stream_bytes * (tag_bits + 1),
        "sink_pause": vars(downstream),
    }


def load(path):
    """Read a strict list of lengths and gaps without implicit defaults."""
    source = path.open(encoding="utf-8", newline="")
    with source:
        rows = csv.DictReader(source, strict=True)
        if rows.fieldnames != ["length", "gap"]:
            raise TimingError("expected length,gap CSV header")
        result = []
        for item in rows:
            try:
                result.append(Arrival.parse(item))
            except ValueError as exc:
                raise TimingError(f"row {rows.line_num}: {exc}") from exc
            if len(result) > 4000:
                raise TimingError("at most 4000 frames can be assigned unique tags")
    return result


def main():
    """Print a beginner-readable latency and storage trade-off for a finite workload."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traffic", type=Path)
    parser.add_argument("--input", type=int, default=4)
    parser.add_argument("--output", type=int, default=8)
    parser.add_argument("--bank", type=int, default=128)
    parser.add_argument("--fifo", type=int, default=12)
    parser.add_argument("--period", type=int, default=7)
    parser.add_argument("--busy", type=int, default=3)
    args = parser.parse_args()
    try:
        results = compare(load(args.traffic), Ports(args.input, args.output, args.bank, args.fifo),
                          Ready(args.period, args.busy))
    except (OSError, ValueError, csv.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
