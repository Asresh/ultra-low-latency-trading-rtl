#!/usr/bin/env python3
"""Replay, observe, and audit three physically cascaded width converters."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import dataclass
from itertools import zip_longest
import json
from pathlib import Path
import random
import subprocess
import sys
import zlib

from width_model import Beat, Drive, Gearbox, Report, Status, full, validate_packet
from width_recipe import Packet, Source, Window, channel_ready, fields, integer, list_values, ordered_windows
from width_stream import Edge, QueueOracle


MODES = ("validated", "streaming")
STAGES = 3


class ChainError(ValueError):
    """A cascade description, sample, or delivery violated the workbench contract."""


@dataclass(frozen=True)
class Stage:
    iw: int
    ow: int
    depth: int
    mode: str

    def __post_init__(self):
        """Check formats and the minimum streaming space needed for forward progress."""
        for width in (self.iw, self.ow):
            integer(width, "byte lanes", 16, 1)
        integer(self.depth, "stage depth", 0xFFFF, 1)
        if self.mode not in MODES:
            raise ChainError(f"unknown conversion mode {self.mode!r}")
        if self.mode == "streaming" and self.depth < self.iw + self.ow:
            raise ChainError("streaming stage must accommodate both complete beat widths")


@dataclass(frozen=True)
class Dimensions:
    stages: tuple[Stage, ...]

    def __post_init__(self):
        """Require the three connected stages used by the structural testbench."""
        if len(self.stages) != STAGES:
            raise ChainError("the chain bench has three conversion stages")
        for left, right in zip(self.stages, self.stages[1:]):
            if left.ow != right.iw:
                raise ChainError("a stage output width must equal its successor's input width")

    @classmethod
    def create(cls, widths, modes, depths):
        """Build three stage descriptors from four boundary widths and three policies."""
        widths = tuple(widths)
        modes = tuple(modes)
        depths = tuple(depths)
        if len(widths) != STAGES + 1 or len(modes) != STAGES or len(depths) != STAGES:
            raise ChainError("expected four widths, three modes, and three depths")
        return cls(tuple(Stage(left, right, depth, mode)
                         for left, right, depth, mode in zip(widths, widths[1:], depths, modes)))

    @property
    def widths(self):
        """Return the external widths and every internal link width in byte lanes."""
        return (self.stages[0].iw, *(stage.ow for stage in self.stages))

    def parameters(self):
        """Return the elaboration parameters for the Icarus testbench instance."""
        result = {f"W{index}": width for index, width in enumerate(self.widths)}
        for index, stage in enumerate(self.stages):
            result[f"D{index}"] = stage.depth
            result[f"M{index}"] = int(stage.mode == "streaming")
        return result


@dataclass(frozen=True)
class Plan:
    dimensions: Dimensions
    packets: tuple[Packet, ...]
    sink_stalls: tuple[Window, ...] = ()
    report_stalls: tuple[tuple[Window, ...], ...] = ((), (), ())

    @property
    def geometry(self):
        """Expose the input width required by the common timed packet source."""
        from width_capture import Geometry
        layout = self.dimensions.stages[0]
        return Geometry(layout.iw, layout.ow, layout.depth)

    @classmethod
    def parse(cls, obj):
        """Decode a named cascade and bounded source schedule without writing output yet."""
        fields(obj, {"widths", "modes", "depths", "packets"}, {"sink_stalls", "report_stalls"})
        dims = Dimensions.create(
            list_values(obj["widths"], "widths"),
            list_values(obj["modes"], "modes"),
            list_values(obj["depths"], "depths"),
        )
        packets = []
        for index, record in enumerate(list_values(obj["packets"], "packets")):
            try:
                packets.append(Packet.parse(record, dims.widths[0]))
            except ValueError as exc:
                raise ChainError(f"packet {index}: {exc}") from exc
        output = ordered_windows(obj.get("sink_stalls", []), "sink stalls")
        raw_reports = list_values(obj.get("report_stalls", [[], [], []]), "report stalls")
        reports = tuple(ordered_windows(item, f"stage {index} report stalls")
                        for index, item in enumerate(raw_reports))
        return cls(dims, tuple(packets), output, reports).check()

    def check(self):
        """Reject malformed input to a trusted first stage and ensure ordered timed arrivals."""
        if len(self.report_stalls) != STAGES:
            raise ChainError("report stalls need one entry for each stage")
        if any(left.start > right.start for left, right in zip(self.packets, self.packets[1:])):
            raise ChainError("packet arrivals must be sorted in time")
        for windows in (self.sink_stalls, *self.report_stalls):
            if any(window.start < 0 or window.stop <= window.start for window in windows):
                raise ChainError("empty or negative stall window")
        if self.dimensions.stages[0].mode == "streaming":
            width = self.dimensions.widths[0]
            for index, packet in enumerate(self.packets):
                report, _ = validate_packet(list(packet.beats(width)), width, 262144)
                if report.status != Status.OK:
                    raise ChainError(f"packet {index} is malformed before a trusted first stage")
        return self


@dataclass(frozen=True)
class InputEdge:
    reset: bool = False
    offered: Beat | None = None
    idle_keep: int = 0
    sink_ready: bool = True
    report_ready: tuple[bool, ...] = (True, True, True)

    def words(self):
        """Encode the cascade testbench's immutable physical stimulus columns."""
        beat = self.offered or Beat(keep=self.idle_keep)
        return (
            int(self.reset), int(self.offered is not None), beat.data, beat.keep,
            int(beat.last), beat.tag, int(self.sink_ready), *(int(v) for v in self.report_ready),
        )


@dataclass(frozen=True)
class Observation:
    source_ready: bool
    output: Beat | None
    report: Report | None
    occupancy: int

    def words(self):
        """Flatten one stage's pre-edge ready/valid interface and byte occupancy."""
        data = self.output or Beat()
        report = self.report or Report()
        return (
            int(self.source_ready), int(self.output is not None), data.data, data.keep,
            int(data.last), data.tag, int(self.report is not None), report.tag,
            report.length, int(report.status), self.occupancy,
        )


class ValidatingStage:
    def __init__(self, dimensions):
        """Instantiate the packet-level two-bank oracle for one physical stage."""
        self.model = Gearbox(dimensions.iw, dimensions.ow, dimensions.depth)

    def observe(self, active, incoming, report_ready, reset):
        """Observe the registered output and ingress space without committing any beat."""
        sample = self.model.observe(Drive(active, incoming, False, report_ready, reset))
        return Observation(sample.input_ready, sample.output, None if reset else sample.report, 0)

    def advance(self, active, incoming, ready, report_ready, reset):
        """Apply the simultaneously calculated input and output handshake decisions."""
        self.model.advance(Drive(active, incoming, ready, report_ready, reset))

    def drained(self):
        """Check that no partial frame, committed output, or diagnostic remains."""
        return not self.model.input_packet and not self.model.pending and self.model.report is None


class TrustedStage:
    def __init__(self, dimensions):
        """Instantiate the byte-deque cut-through oracle for one trusted stage."""
        self.model = QueueOracle(dimensions.iw, dimensions.ow, dimensions.depth)

    def observe(self, active, incoming, report_ready, reset):
        """Derive byte space and stable output from the deque before the edge."""
        count = len(incoming.bytes(self.model.iw))
        ready = not reset and 0 < count <= self.model.capacity - len(self.model.data)
        outgoing = Beat() if reset else self.model.candidate()
        return Observation(ready, outgoing if outgoing.keep else None, None, len(self.model.data))

    def advance(self, active, incoming, ready, report_ready, reset):
        """Pop and append the exact transferred byte counts without predicting neighbor state."""
        self.model.advance(Edge(incoming, active, ready, reset))

    def drained(self):
        """Check that no byte is waiting for more data or downstream acceptance."""
        return not self.model.data


class Cascade:
    def __init__(self, dimensions):
        """Connect independent packet and byte queue models in physical lane order."""
        self.stages = tuple(
            (ValidatingStage if item.mode == "validated" else TrustedStage)(item)
            for item in dimensions.stages
        )

    def step(self, edge):
        """Observe left to right, then commit with readiness from the adjacent right-hand input."""
        values = []
        inputs = []
        present = edge.offered is not None
        offered = edge.offered or Beat(keep=edge.idle_keep)
        for stage, report_ready in zip(self.stages, edge.report_ready):
            inputs.append((present, offered))
            observed = stage.observe(present, offered, report_ready, edge.reset)
            values.append(observed)
            present = observed.output is not None
            offered = observed.output or Beat()
        right_ready = [value.source_ready for value in values[1:]] + [edge.sink_ready]
        for index, stage in enumerate(self.stages):
            stage.advance(
                *inputs[index], right_ready[index], edge.report_ready[index], edge.reset
            )
        return tuple(values)

    def drained(self):
        """Require every stage's data and report paths to be empty."""
        return all(stage.drained() for stage in self.stages)


def make_schedule(plan, limit=300_000):
    """Produce a complete scheduled trace within an explicit edge budget."""
    plan.check()
    if type(limit) is not int or not 1 <= limit <= 500_000:
        raise ChainError("edge limit must be a positive integer at most 500000")
    source = Source(plan)
    pipeline = Cascade(plan.dimensions)
    idle_keep = full(plan.dimensions.widths[0])
    for cycle in range(limit):
        if cycle == 0:
            edge = InputEdge(reset=True, idle_keep=idle_keep)
        else:
            offered = source.offer(cycle)
            edge = InputEdge(
                offered=offered, idle_keep=idle_keep,
                sink_ready=channel_ready(plan.sink_stalls, cycle),
                report_ready=tuple(channel_ready(windows, cycle) for windows in plan.report_stalls),
            )
        sample = pipeline.step(edge)
        yield edge, sample
        if edge.offered is not None and sample[0].source_ready:
            source.retire(cycle)
        if source.done and pipeline.drained():
            return
    raise ChainError(f"cascade did not drain within {limit} edges")


def _hex_row(values):
    """Write the mixed-width trace row in ordinary hexadecimal text."""
    return " ".join(f"{int(value):x}" for value in values)


def write_schedule(folder, program):
    """Retain the source-only tape and independent expectation as separate files."""
    folder.mkdir(parents=True, exist_ok=True)
    drive_file = folder / "drive.txt"
    expected_file = folder / "expected.txt"
    count = 0
    with drive_file.open("w", encoding="ascii") as drives, expected_file.open("w", encoding="ascii") as replies:
        for index, (drive, observed) in enumerate(program):
            drives.write(_hex_row(drive.words()) + "\n")
            rows = [part.words() for part in observed]
            flat = [item for row in rows for item in row]
            replies.write(_hex_row((index, *flat)) + "\n")
            count += 1
    return count


def _bits(value, label):
    """Require a complete observed Boolean rather than accepting arbitrary integers."""
    if value not in (0, 1):
        raise ChainError(f"{label} is not Boolean")
    return bool(value)


def _parse_sample(columns, dimensions):
    """Decode one three-stage sample and enforce all per-link physical widths."""
    stages = []
    for index, shape in enumerate(dimensions.stages):
        start = 1 + index * 11
        edge = columns[start:start+11]
        ready, valid, data, keep, last, tag, rv, rt, count, code, used = edge
        valid = _bits(valid, "output valid")
        rv = _bits(rv, "report valid")
        if valid:
            outgoing = Beat(data, keep, _bits(last, "last"), tag)
            outgoing.validate(shape.ow)
        else:
            if any((data, keep, last, tag)):
                raise ChainError("invalid output payload is not zeroed in the trace")
            outgoing = None
        if rv:
            integer(rt, "report tag", 0xFFFF)
            integer(count, "report byte count", 0xFFFF)
            report = Report(rt, count, Status(code))
        else:
            if any((rt, count, code)):
                raise ChainError("invalid report payload is not zeroed in the trace")
            report = None
        integer(used, "occupancy", shape.depth if shape.mode == "streaming" else 0)
        if shape.mode == "streaming" and report is not None:
            raise ChainError("trusted stage produced an unexpected diagnostic")
        stages.append(Observation(_bits(ready, "source ready"), outgoing, report, used))
    return tuple(stages)


def read_samples(path, dimensions):
    """Load physical samples with strict count, chronology, and field checks."""
    with Path(path).open(encoding="ascii") as stream:
        for index, row in enumerate(stream):
            try:
                columns = tuple(int(part, 16) for part in row.split())
                if len(columns) != 1 + STAGES * 11:
                    raise ChainError("unexpected stage field count")
                if columns[0] != index:
                    raise ChainError(f"expected edge {index}, got {columns[0]}")
                yield _parse_sample(columns, dimensions)
            except ValueError as exc:
                raise ChainError(f"{path}:{index + 1}: {exc}") from exc


@dataclass(frozen=True)
class Span:
    tag: int
    first: int
    last: int
    beats: int
    payload: bytes

    @property
    def active_cycles(self):
        """Include both the first and last observed transfer edges."""
        return self.last - self.first + 1


class PacketWatch:
    def __init__(self, width):
        """Capture physical link beats into one ordered packet at a time."""
        self.width = width
        self.beats = []
        self.first = None

    def accept(self, beat, cycle):
        """Assemble accepted lanes without counting bubbles or stalled repeats."""
        if not self.beats:
            self.first = cycle
        self.beats.append(beat)
        if not beat.last:
            return None
        previous = tuple(self.beats)
        payload = b"".join(part.bytes(self.width) for part in previous)
        complete = Span(previous[0].tag, self.first, cycle, len(previous), payload)
        self.beats.clear()
        self.first = None
        return complete, previous


@dataclass(frozen=True)
class ExpectedPacket:
    ordinal: int
    tag: int
    payload: bytes


class Audit:
    def __init__(self, dimensions):
        """Prepare a cross-link resolution independent of stage timing or buffer design."""
        self.dimensions = dimensions
        self.ingress = PacketWatch(dimensions.widths[0])
        self.egress = tuple(PacketWatch(item.ow) for item in dimensions.stages)
        self.expected_packets = tuple(deque() for stage in dimensions.stages)
        self.expected_reports = tuple(deque() for stage in dimensions.stages)
        self.held_outputs = [None] * STAGES
        self.held_reports = [None] * STAGES
        self.old_input = None
        self.watermarks = [0] * STAGES
        self.byte_space_used = [0] * STAGES
        self.latencies = [[] for stage in dimensions.stages]
        self.packet_spans = [dict() for link in dimensions.widths]
        self.reports = [Counter() for stage in dimensions.stages]
        self.report_edges = [dict() for stage in dimensions.stages]
        self.predicted_routes = {}
        self.edge_count = 0
        self.stall_outputs = [0] * STAGES
        self.stall_reports = [0] * STAGES

    def _enqueue(self, finished, beats):
        """Project a whole accepted input frame through each policy in traffic order."""
        ordinal = len(self.packet_spans[0])
        self.packet_spans[0][ordinal] = finished
        stream = beats
        decisions = []
        self.predicted_routes[ordinal] = decisions
        for index, stage in enumerate(self.dimensions.stages):
            capacity = stage.depth if stage.mode == "validated" else 262144
            report, payload = validate_packet(stream, stage.iw, capacity)
            if stage.mode == "validated":
                self.expected_reports[index].append((ordinal, report))
            elif report.status != Status.OK:
                raise ChainError(f"input violated trusted stage {index}: {report.status.name}")
            decisions.append(report.status)
            if payload is None:
                return
            self.expected_packets[index].append(ExpectedPacket(ordinal, report.tag, payload))
            stream = tuple(_split(payload, stage.ow, report.tag))

    def _check_report(self, index, report, cycle):
        """Consume a diagnostic and require tag, length, error priority, and order."""
        queue = self.expected_reports[index]
        if not queue:
            raise ChainError(f"edge {cycle} stage {index}: extra report")
        ordinal, expected = queue.popleft()
        if report != expected:
            raise ChainError(f"edge {cycle} stage {index}: report {report!r} differs from {expected!r}")
        self.reports[index][report.status.name] += 1
        self.report_edges[index][ordinal] = cycle

    def _check_packet(self, index, packet, cycle):
        """Match an outgoing packet to its ordinal and verify causality and byte ordering."""
        measured, beats = packet
        queue = self.expected_packets[index]
        if not queue:
            raise ChainError(f"edge {cycle} stage {index}: output without an accepted ingress packet")
        expected = queue.popleft()
        stage = self.dimensions.stages[index]
        reference = tuple(_split(expected.payload, stage.ow, expected.tag))
        if beats != reference or measured.tag != expected.tag or measured.payload != expected.payload:
            raise ChainError(f"edge {cycle} stage {index}: packet {expected.ordinal} changed")
        inner = self.packet_spans[index].get(expected.ordinal)
        if inner is None:
            raise ChainError(f"edge {cycle} stage {index}: packet ended before input completed")
        start_floor = inner.last if stage.mode == "validated" else inner.first
        if measured.first <= start_floor or measured.last <= inner.last:
            raise ChainError(f"edge {cycle} stage {index}: noncausal pipeline timing")
        self.latencies[index].append({
            "packet": expected.ordinal,
            "tag": measured.tag,
            "first_after_first": measured.first - inner.first,
            "last_after_last": measured.last - inner.last,
            "output_active_span": measured.active_cycles,
            "bytes": len(measured.payload),
        })
        self.packet_spans[index+1][expected.ordinal] = measured

    def _check_hold(self, index, view, ready, report_ready, cycle):
        """Keep both stall contracts independent of data acceptance elsewhere."""
        if self.held_outputs[index] is not None and view.output != self.held_outputs[index]:
            raise ChainError(f"edge {cycle} stage {index}: held output changed")
        if self.held_reports[index] is not None and view.report != self.held_reports[index]:
            raise ChainError(f"edge {cycle} stage {index}: held report changed")
        self.held_outputs[index] = None if ready else view.output
        self.held_reports[index] = None if report_ready else view.report
        self.stall_outputs[index] += int(self.held_outputs[index] is not None)
        self.stall_reports[index] += int(self.held_reports[index] is not None)

    def _check_occupancy(self, index, incoming, outgoing, view, cycle):
        """Recompute cut-through occupancy directly from observed byte handshakes."""
        shape = self.dimensions.stages[index]
        if shape.mode != "streaming":
            return
        used = self.byte_space_used[index]
        if used != view.occupancy:
            raise ChainError(f"edge {cycle} stage {index}: observed occupancy differs from byte counts")
        self.watermarks[index] = max(self.watermarks[index], used)
        arrived = 0 if incoming is None else len(incoming.bytes(shape.iw))
        retired = 0 if outgoing is None else len(outgoing.bytes(shape.ow))
        after = used + arrived - retired
        if not 0 <= after <= shape.depth:
            raise ChainError(f"edge {cycle} stage {index}: transfer exceeds FIFO bounds")
        self.byte_space_used[index] = after

    def step(self, drive, observed):
        """Monitor a sampled edge using traffic conservation and handshakes alone."""
        cycle = self.edge_count
        self.edge_count += 1
        if drive.reset:
            if cycle:
                raise ChainError("use a new capture for each reset epoch")
            if any(view.source_ready or view.output or view.report for view in observed):
                raise ChainError("a cascade stage exposed activity during reset")
            return
        if self.old_input is not None and drive.offered != self.old_input:
            raise ChainError(f"edge {cycle}: source changed a stalled beat")
        active = drive.offered if observed[0].source_ready else None
        self.old_input = None if observed[0].source_ready else drive.offered
        if active is not None:
            completed = self.ingress.accept(active, cycle)
            if completed is not None:
                self._enqueue(*completed)
        local_ready = [view.source_ready for view in observed[1:]] + [drive.sink_ready]
        for index, (view, ready, diag_ready) in enumerate(
            zip(observed, local_ready, drive.report_ready)
        ):
            self._check_hold(index, view, ready, diag_ready, cycle)
            sent = view.output if ready else None
            self._check_occupancy(index, active, sent, view, cycle)
            if view.report is not None and diag_ready:
                self._check_report(index, view.report, cycle)
            if sent is not None:
                completed = self.egress[index].accept(sent, cycle)
                if completed is not None:
                    self._check_packet(index, completed, cycle)
            active = sent

    def finish(self):
        """Require all predicted packets and reports to drain, then summarize observed metrics."""
        outstanding = [
            *(len(watch.beats) for watch in (self.ingress, *self.egress)),
            *(len(queue) for queue in (*self.expected_packets, *self.expected_reports)),
            *self.byte_space_used,
        ]
        if any(outstanding):
            raise ChainError("unfinished packets or reports in audited trace")
        routes = self.packet_routes()
        layers = []
        for index, shape in enumerate(self.dimensions.stages):
            delivered = self.packet_spans[index+1]
            layers.append({
                "stage": index, "mode": shape.mode,
                "input_width": shape.iw, "output_width": shape.ow,
                "packets_out": len(delivered),
                "bytes_out": sum(len(span.payload) for span in delivered.values()),
                "reports": dict(self.reports[index]),
                "byte_occupancy_peak": self.watermarks[index],
                "output_stall_cycles": self.stall_outputs[index],
                "report_stall_cycles": self.stall_reports[index],
                "latency": self.latencies[index],
                "first_after_first": distribution(
                    row["first_after_first"] for row in self.latencies[index]
                ),
                "last_after_last": distribution(
                    row["last_after_last"] for row in self.latencies[index]
                ),
            })
        return {
            "cycles": self.edge_count,
            "input_packets": len(self.packet_spans[0]),
            "stages": layers,
            "packet_routes": routes,
            "end_to_end": distribution(
                entry["route"][-1]["output"]["last"] - entry["source"]["last"]
                for entry in routes if entry["delivered"]
            ),
        }

    def packet_routes(self):
        """Combine per-link observations by ordinal even when every packet reuses one tag."""
        routes = []
        for ordinal, ingress in self.packet_spans[0].items():
            decisions = self.predicted_routes[ordinal]
            path = []
            for index, stage in enumerate(self.dimensions.stages):
                before = self.packet_spans[index].get(ordinal)
                after = self.packet_spans[index+1].get(ordinal)
                reason = decisions[index] if index < len(decisions) else None
                diag = self.report_edges[index].get(ordinal)
                if reason is None:
                    if before is not None or after is not None or diag is not None:
                        raise ChainError(f"packet {ordinal} reached stage {index} unexpectedly")
                    path.append({"stage": index, "disposition": "UNREACHED"})
                    continue
                if before is None:
                    raise ChainError(f"packet {ordinal} has missing ingress at stage {index}")
                if stage.mode == "validated":
                    if diag is None or diag <= before.last:
                        raise ChainError(f"packet {ordinal} has noncausal report at stage {index}")
                elif diag is not None:
                    raise ChainError(f"trusted stage {index} unexpectedly has a report")
                if (reason == Status.OK) != (after is not None):
                    raise ChainError(f"packet {ordinal} survival disagrees with stage {index} outcome")
                path.append({
                    "stage": index, "disposition": reason.name,
                    "report_edge": diag,
                    "output": span_object(after) if after is not None else None,
                })
            routes.append({
                "packet": ordinal, "tag": ingress.tag,
                "source": span_object(ingress), "route": path,
                "delivered": ordinal in self.packet_spans[-1],
            })
        return routes


def span_object(span):
    """Record useful evidence about one completed link transaction without a raw packet dump."""
    return {
        "first": span.first,
        "last": span.last,
        "beats": span.beats,
        "bytes": len(span.payload),
        "crc32": f"{zlib.crc32(span.payload):08x}",
    }


def distribution(measurements):
    """Compute nearest-rank summaries of observed integer clock distances."""
    data = sorted(measurements)
    if not data:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None}
    size = len(data)
    return {
        "count": size,
        "min": data[0],
        "p50": data[(size * 50 + 99) // 100 - 1],
        "p95": data[(size * 95 + 99) // 100 - 1],
        "max": data[-1],
    }


def _metric(value):
    """Render an unobserved latency distinctly from zero cycles."""
    return str(value) if value is not None else "—"


def report_markdown(report, rows=20):
    """Make stage effects and a bounded sample of exact packet histories readable."""
    if type(rows) is not int or rows < 0:
        raise ChainError("report sample limit must be a nonnegative integer")
    stages = report["stages"]
    packets = report["packet_routes"]
    summary = report["end_to_end"]
    lines = [
        "# Width conversion cascade",
        "",
        f"Observed {report['input_packets']} accepted ingress packets in {report['cycles']} clocks.",
        f"Delivered {summary['count']} end-to-end packets with p95 last-byte delay "
        f"{_metric(summary['p95'])} cycles.",
        "",
        "| Stage | Mode | Lanes | Packets out | Bytes out | FIFO high water | Last-byte delay p95 |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for stage in stages:
        lines.append(
            f"| {stage['stage']} | {stage['mode']} | "
            f"{stage['input_width']} → {stage['output_width']} | {stage['packets_out']} | "
            f"{stage['bytes_out']} | {stage['byte_occupancy_peak']} | "
            f"{_metric(stage['last_after_last']['p95'])} |"
        )
    if rows:
        lines.extend((
            "", "## Packet examples", "",
            "| Ordinal | Tag | Source bytes | Stage 0 | Stage 1 | Stage 2 | Final |",
            "| ---: | ---: | ---: | --- | --- | --- | --- |",
        ))
        for packet in packets[:rows]:
            labels = " | ".join(stage["disposition"] for stage in packet["route"])
            source = packet["source"]["bytes"]
            state = "delivered" if packet["delivered"] else "rejected"
            lines.append(f"| {packet['packet']} | {packet['tag']} | {source} | {labels} | {state} |")
        if len(packets) > rows:
            lines.extend(("", f"The JSON report contains the other {len(packets) - rows} histories."))
    return "\n".join(lines) + "\n"



def _end_product(packet):
    """Return the delivered payload fingerprint or the terminal rejection reason."""
    if packet["delivered"]:
        last = packet["route"][-1]["output"]
        return "delivered", (last["bytes"], last["crc32"])
    first_rejection = next(
        (item["disposition"] for item in packet["route"]
         if item["disposition"] not in ("OK", "UNREACHED")),
        None,
    )
    if first_rejection is None:
        raise ChainError(f"packet {packet['packet']} has no terminal outcome")
    return "rejected", first_rejection


def _ingress_signature(record):
    """Keep identity independent of local frame beat widths and entry latency."""
    raw = record["source"]
    return record["packet"], record["tag"], raw["bytes"], raw["crc32"]


def _retirement_delay(record):
    """Measure final output transfer relative to the same packet's final input transfer."""
    return record["route"][-1]["output"]["last"] - record["source"]["last"]


def compare_routes(baseline, candidate):
    """Compare final semantics and paired latency for the same ordered ingress frames."""
    differences = []
    delays = []
    for reference, measured in zip_longest(baseline["packet_routes"], candidate["packet_routes"]):
        if reference is None or measured is None:
            raise ChainError("captures observed different numbers of input packets")
        if _ingress_signature(reference) != _ingress_signature(measured):
            raise ChainError(f"captures have different input packet {reference['packet']}")
        expected = _end_product(reference)
        actual = _end_product(measured)
        if expected != actual:
            differences.append({
                "packet": reference["packet"], "tag": reference["tag"],
                "baseline": expected, "candidate": actual,
            })
        elif expected[0] == "delivered":
            delays.append(_retirement_delay(measured) - _retirement_delay(reference))
    return {
        "equivalent": not differences,
        "differences": differences,
        "paired_latency_delta": distribution(delays),
    }


def _split(payload, width, tag):
    """Generate canonical outgoing words from a completed byte string for the audit."""
    for start in range(0, len(payload), width):
        part = payload[start:start + width]
        yield Beat(int.from_bytes(part, "little"), full(len(part)), start + width >= len(payload), tag)


def verify(plan, actual):
    """Compare every observed RTL sample, then independently audit causal packet histories."""
    audit = Audit(plan.dimensions)
    for cycle, (reference, observed) in enumerate(zip_longest(make_schedule(plan), actual)):
        if reference is None or observed is None:
            raise ChainError(f"edge {cycle}: trace and schedule lengths differ")
        drive, expected = reference
        for index, (wanted, current) in enumerate(zip(expected, observed)):
            if wanted != current:
                raise ChainError(
                    f"edge {cycle} stage {index}: expected {wanted!r}, observed {current!r}"
                )
        if len(observed) != STAGES:
            raise ChainError(f"edge {cycle}: expected {STAGES} stage samples")
        audit.step(drive, observed)
    return audit.finish()


def sample_plan(name, packets=76, seed=83421):
    """Create a reproducible cascade with capacity drops and stage-specific stalls."""
    sizes = {
        "roundtrip": ((4, 8, 3, 4), ("validated", "streaming", "validated"), (128, 17, 128)),
        "trusted": ((5, 3, 7, 2), ("streaming",) * 3, (11, 14, 13)),
        "filtered": ((8, 4, 2, 8), ("validated", "validated", "streaming"), (112, 45, 13)),
        "latecheck": ((3, 5, 4, 3), ("streaming", "validated", "validated"), (9, 96, 44)),
    }
    if name not in sizes:
        raise ChainError(f"unknown cascade profile {name}")
    dimensions = Dimensions.create(*sizes[name])
    rng = random.Random(seed)
    prepared = []
    for index in range(packets):
        length = rng.choice((1, 3, 5, 8, 40, 44, 45, 46, 47, 92, 112, 131))
        tag = index & 3 if index % 5 else index + 100
        part = bytes(rng.randrange(256) for byte in range(length))
        obj = {
            "tag": tag, "payload": part.hex(), "pad": rng.randrange(256),
            "at": 2 + index // 3, "interbeat": int(index % 7 == 0),
        }
        if dimensions.stages[0].mode == "validated":
            total = (length + dimensions.widths[0] - 1) // dimensions.widths[0]
            if index % 13 == 3:
                obj["edits"] = [{"beat": total - 1, "keep": 0}]
            elif index % 13 == 8 and total >= 2:
                obj["edits"] = [{"beat": total - 1, "tag": tag ^ 107}]
        prepared.append(Packet.parse(obj, dimensions.widths[0]))
    pauses = (Window(8, 120), Window(196, 230), Window(360, 434))
    diagnostics = ((Window(5, 25), Window(140, 191)), (Window(46, 72),), (Window(90, 154),))
    return Plan(dimensions, tuple(prepared), pauses, diagnostics).check()


def _safe_run(command, log):
    """Execute an elaboration or bench and leave its full diagnostic in an artifact."""
    finished = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
    log.write_text(f"{finished.stdout}{finished.stderr}", encoding="utf-8")
    if finished.returncode:
        raise ChainError(f"tool exited {finished.returncode}: see {log}")


def run(plan, directory, iverilog="iverilog", vvp="vvp"):
    """Compile the matching hardware chain, compare raw traces, and retain measurements."""
    expected_count = write_schedule(directory, make_schedule(plan))
    image = directory / "chain.vvp"
    args = [iverilog, "-g2012", "-s", "tb_width_chain", "-o", str(image)]
    for key, value in plan.dimensions.parameters().items():
        args.append(f"-Ptb_width_chain.{key}={value}")
    base = Path(__file__).resolve().parent
    args.extend(str(base / file) for file in (
        "packet_gearbox.sv", "streaming_gearbox.sv", "tb_width_chain.sv",
    ))
    _safe_run(args, directory / "elaboration.log")
    observed = directory / "observed.txt"
    _safe_run([vvp, str(image), f"+drive={directory / 'drive.txt'}", f"+sample={observed}"],
              directory / "simulation.log")
    report = verify(plan, read_samples(observed, plan.dimensions))
    if report["cycles"] != expected_count:
        raise ChainError("cycle count changed during verification")
    (directory / "report.json").write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    (directory / "report.md").write_text(report_markdown(report), encoding="utf-8")
    return report


def arguments(argv=None):
    """Parse profile, explicit plan, tool paths, and verification output location."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--profile", choices=("roundtrip", "trusted", "filtered", "latecheck", "all"),
                        default="all")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--reference", type=Path,
                        help="compare one selected run against a prior report.json")
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default="vvp")
    return parser.parse_args(argv)


def main(argv=None):
    """Run each selected profile and fail on hardware, capture, or conservation errors."""
    args = arguments(argv)
    profiles = ("roundtrip", "trusted", "filtered", "latecheck") if args.profile == "all" else (args.profile,)
    try:
        reference = None
        if args.reference is not None:
            if args.plan is None and args.profile == "all":
                raise ChainError("select one profile when comparing against a reference")
            reference = json.loads(args.reference.read_text(encoding="utf-8"))
        if args.plan is not None:
            recipe = Plan.parse(json.loads(args.plan.read_text(encoding="utf-8")))
            specifications = (("custom", recipe),)
        else:
            specifications = [(name, sample_plan(name)) for name in profiles]
        for name, plan in specifications:
            result = run(plan, args.destination / name, args.iverilog, args.vvp)
            counts = " → ".join(str(item["packets_out"]) for item in result["stages"])
            print(f"PASS cascade {name}: {result['cycles']} edges, "
                  f"{result['input_packets']} → {counts} packets")
            if reference is not None:
                comparisons = compare_routes(reference, result)
                path = args.destination / name / "comparison.json"
                path.write_text(json.dumps(comparisons, indent=2) + "\n", encoding="utf-8")
                if not comparisons["equivalent"]:
                    print(f"width_chain: end-to-end outcomes differ; see {path}", file=sys.stderr)
                    return 1
    except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"width_chain: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
