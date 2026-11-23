"""Measure packet service and compare portable captures across width profiles."""

import argparse
from collections import Counter, deque
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path

from width_audit import AuditError
from width_capture import Geometry, read_rows
from width_model import Report, Status, full, validate_packet


@dataclass(frozen=True)
class PacketEvent:
    epoch: int
    ordinal: int
    tag: int
    data: bytes
    input_first: int
    input_last: int
    output_first: int
    output_last: int
    output_beats: int

    def fingerprint(self):
        """Compare actual packet order and contents across word widths."""
        return self.epoch, self.tag, self.data

    def row(self):
        """Report raw tick edges without interpreting them as physical timing."""
        return {
            "epoch": self.epoch,
            "ordinal": self.ordinal,
            "tag": self.tag,
            "length": len(self.data),
            "input": [self.input_first, self.input_last],
            "output": [self.output_first, self.output_last],
            "output_beats": self.output_beats,
            "first_after_commit": self.output_first - self.input_last,
            "complete_after_arrival": self.output_last - self.input_first,
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }


@dataclass(frozen=True)
class Committed:
    first: int
    last: int
    report: Report
    data: bytes


class PacketReader:
    """Track packets by sequence within each reset epoch, independent of tag reuse."""

    def __init__(self, geometry):
        """Record geometry and initialize all per-epoch queues."""
        self.geometry = geometry
        self.epoch = 0
        self.ordinal = 0
        self.input_beats = []
        self.in_first = 0
        self.out_first = 0
        self.output = bytearray()
        self.out_beats = 0
        self.pending = deque()
        self.decisions = deque()
        self.dropped_during_reset = 0
        self.status = Counter()

    def end_epoch(self):
        """Drop all in-flight captures according to the synchronous reset contract."""
        self.epoch += 1
        self.ordinal = 0
        self.dropped_during_reset += len(self.pending) + bool(self.input_beats)
        self.input_beats.clear()
        self.pending.clear()
        self.decisions.clear()
        self.output.clear()
        self.out_beats = 0

    def input(self, event):
        """Compare no outputs yet, collecting each physical byte to validate framing."""
        if not self.input_beats:
            self.in_first = event.cycle
        self.input_beats.append(event.value)
        if not event.value.last:
            return
        iw = self.geometry.input_width
        report, data = validate_packet(self.input_beats, iw, self.geometry.capacity)
        self.decisions.append(report)
        if report.status == Status.OK:
            self.pending.append(Committed(self.in_first, event.cycle, report, data))
        self.input_beats.clear()

    def output_event(self, event):
        """Read an output packet and publish its reconstructed identity and measured time."""
        if not self.pending:
            raise AuditError("output has no matching successful input packet")
        if not self.out_beats:
            self.out_first = event.cycle
        self.out_beats += 1
        expected = self.pending[0]
        beat = event.value
        if beat.tag != expected.report.tag:
            raise AuditError("output tag changed or packet order broke")
        if expected.last >= event.cycle:
            raise AuditError("store-and-forward output preceded packet commit")
        width = self.geometry.output_width
        selected = beat.bytes(width)
        if not selected or full(len(selected)) != beat.keep:
            raise AuditError("output keep must be a nonempty low-lane prefix")
        if not beat.last and len(selected) != width:
            raise AuditError("non-final output beat has unused lanes")
        if beat.data >> (8 * len(selected)):
            raise AuditError("output padding must be zero")
        self.output.extend(selected)
        if not beat.last:
            if len(self.output) >= len(expected.data):
                raise AuditError("output did not end at the expected payload size")
            return None
        data = bytes(self.output)
        if data != expected.data:
            raise AuditError("actual and reconstructed packet bytes differ")
        item = PacketEvent(
            self.epoch, self.ordinal, beat.tag, data, expected.first, expected.last,
            self.out_first, event.cycle, self.out_beats,
        )
        self.pending.popleft()
        self.output.clear()
        self.out_beats = 0
        self.ordinal += 1
        return item

    def apply(self, event):
        """Dispatch a decoded row and return a complete output record when available."""
        if event.channel == "reset":
            self.end_epoch()
        elif event.channel == "input":
            self.input(event)
        elif event.channel == "output":
            return self.output_event(event)
        else:
            if not self.decisions or self.decisions.popleft() != event.value:
                raise AuditError("report is not the next input's expected outcome")
            self.status[event.value.status.name] += 1
        return None

    def end(self):
        """Detect truncated captures with accepted work still outstanding."""
        if self.output or self.pending or self.decisions or self.input_beats:
            raise AuditError("trace ended during a packet or report")


def quantile(data, percentage):
    """Use nearest rank for percentiles in clock-cycle units."""
    if not 1 <= percentage <= 100:
        raise ValueError("percentile must be between 1 and 100")
    if not data:
        return None
    ordered = sorted(data)
    index = (len(ordered) * percentage + 99) // 100 - 1
    return ordered[index]


def timelines(path):
    """Load a verified WGC1 trace and collect its clock edges and output packets."""
    with path.open("rb") as file:
        geometry = Geometry.read(file)
        decoder = PacketReader(geometry)
        results = []
        first_cycle = None
        last_cycle = None
        accepted_edges = Counter()
        for event in read_rows(file, geometry):
            if first_cycle is None:
                first_cycle = event.cycle
            last_cycle = event.cycle
            accepted_edges[event.channel] += 1
            packet = decoder.apply(event)
            if packet is not None:
                results.append(packet)
    decoder.end()
    return geometry, decoder, first_cycle, last_cycle, accepted_edges, results


def profile(path, example_limit=5):
    """Distinguish active output utilization, packet latency, and elapsed cycles."""
    if example_limit < 0:
        raise ValueError("example limit cannot be negative")
    geometry, decoder, first, last, accepted, packets = timelines(path)
    cycles = last - first + 1 if first is not None else 0
    payload_bytes = sum(len(packet.data) for packet in packets)
    first_delays = [packet.output_first - packet.input_last for packet in packets]
    total_delays = [packet.output_last - packet.input_first for packet in packets]
    return {
        "geometries": vars(geometry),
        "elapsed_cycles": cycles,
        "accepted_edges": dict(accepted),
        "matched_reports": dict(decoder.status),
        "packets": len(packets),
        "dropped_at_reset": decoder.dropped_during_reset,
        "output_payload_bytes": payload_bytes,
        "output_bytes_per_cycle": str(Fraction(payload_bytes, max(1, cycles))),
        "first_beat_latency_percentiles": {p: quantile(first_delays, p) for p in (50, 90, 99, 100)},
        "full_packet_latency_percentiles": {p: quantile(total_delays, p) for p in (50, 90, 99, 100)},
        "examples": [packet.row() for packet in packets[:example_limit]],
    }


def compare(left, right, preserve_epochs=True):
    """Check equal packet boundaries across widths and compare observed latency deltas."""
    left_packets = timelines(left)[-1]
    right_packets = timelines(right)[-1]
    if len(left_packets) != len(right_packets):
        raise AuditError("captures contain different numbers of output packets")
    first_deltas = []
    finish_deltas = []
    for index, (a, b) in enumerate(zip(left_packets, right_packets), 1):
        left_key, right_key = a.fingerprint(), b.fingerprint()
        if not preserve_epochs:
            left_key, right_key = left_key[1:], right_key[1:]
        if left_key != right_key:
            raise AuditError(f"ordered output packet {index} differs")
        first_deltas.append((b.output_first - b.input_last) - (a.output_first - a.input_last))
        finish_deltas.append((b.output_last - b.input_first) - (a.output_last - a.input_first))
    return {
        "same_packet_payloads": len(left_packets),
        "first_delta_right_minus_left": {p: quantile(first_deltas, p) for p in (50, 95, 100)},
        "completion_delta_right_minus_left": {p: quantile(finish_deltas, p) for p in (50, 95, 100)},
    }


def main():
    """Read one profile or diff two captures whose packet contents should match."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path, nargs="?")
    parser.add_argument("--examples", type=int, default=5)
    parser.add_argument("--ignore-epochs", action="store_true")
    args = parser.parse_args()
    try:
        result = (compare(args.left, args.right, not args.ignore_epochs)
                  if args.right else profile(args.left, args.examples))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
