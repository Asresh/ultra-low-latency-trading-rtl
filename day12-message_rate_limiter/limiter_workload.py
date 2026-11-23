"""Compile human-readable arrival and sink schedules into stable hardware inputs."""

import csv
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from limiter_capture import prepare
from limiter_model import CycleModel
from limiter_protocol import Command, Drive, Manifest, Operation


WORKLOAD_HEADER = ("cycle", "action", "key", "tick", "amount", "rate", "enable", "tag")
STALL_HEADER = ("start", "stop")


def parse_number(text, label, *, optional=False):
    """Read an unsigned decimal value and report the exact bad column."""
    if optional and text == "":
        return 0
    if not text or not text.isascii() or not text.isdecimal():
        raise ValueError(f"{label}: expected an unsigned decimal integer")
    return int(text)


def line_rows(path, header):
    """Return numbered CSV records while enforcing exact names and widths."""
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, strict=True)
        try:
            actual = tuple(next(reader, []))
            if actual != header:
                expected = ",".join(header)
                raise ValueError(f"{path}: expected header {expected}")
            for row in reader:
                if len(row) != len(header):
                    raise ValueError(
                        f"{path}:{reader.line_num}: expected {len(header)} columns, got {len(row)}"
                    )
                yield reader.line_num, [value.strip() for value in row]
        except csv.Error as exc:
            raise ValueError(f"{path}:{reader.line_num}: {exc}") from exc


@dataclass(frozen=True)
class Arrival:
    """One logical action becoming available on a host-side clock cycle."""

    cycle: int
    command: Optional[Command] = None

    @property
    def reset(self):
        """Distinguish a reset marker from an arriving command."""
        return self.command is None

    def to_row(self):
        """Write a canonical CSV row for review and round-trip checks."""
        if self.reset:
            return (self.cycle, "reset", "", "", "", "", "", "")
        command = self.command
        return (
            self.cycle,
            command.op.name.lower(),
            command.key,
            command.tick,
            command.amount,
            command.rate,
            command.enable,
            command.tag,
        )


@dataclass(frozen=True)
class Stall:
    """Half-open bus interval where the result consumer deasserts ready."""

    start: int
    stop: int

    def __post_init__(self):
        """Reject empty, backwards, or negative stall ranges."""
        if (
            type(self.start) is not int or type(self.stop) is not int
            or not 0 <= self.start < self.stop
        ):
            raise ValueError("stall requires 0 <= start < stop")

    def contains(self, cycle):
        """Test membership in the half-open range."""
        return self.start <= cycle < self.stop

    def to_row(self):
        """Write start and end in the stall-file column order."""
        return (self.start, self.stop)


def parse_arrival(row, geometry):
    """Convert one CSV event into a reset marker or width-checked command."""
    cycle_text, action, key, tick, amount, rate, enable, tag = row
    cycle = parse_number(cycle_text, "cycle")
    if action == "reset":
        if any(row[2:]):
            raise ValueError("reset rows must have empty command fields")
        return Arrival(cycle)
    try:
        operation = Operation[action.upper()]
    except KeyError as exc:
        raise ValueError(f"unknown action {action!r}") from exc
    request = Command(
        operation,
        parse_number(key, "key"),
        parse_number(tick, "tick"),
        parse_number(amount, "amount", optional=True),
        parse_number(rate, "rate", optional=True),
        parse_number(enable, "enable", optional=True),
        parse_number(tag, "tag", optional=True),
    )
    request.validate(geometry)
    return Arrival(cycle, request)


def read_arrivals(path, geometry):
    """Load a workload and preserve file order for same-cycle requests."""
    arrivals = []
    for number, row in line_rows(path, WORKLOAD_HEADER):
        try:
            arrivals.append(parse_arrival(row, geometry))
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
    verify_arrivals(arrivals)
    return arrivals


def read_stalls(path):
    """Load ready-low windows and reject overlap or backwards ordering."""
    if path is None:
        return []
    intervals = []
    for number, row in line_rows(path, STALL_HEADER):
        try:
            intervals.append(
                Stall(parse_number(row[0], "start"), parse_number(row[1], "stop"))
            )
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
    verify_stalls(intervals)
    return intervals


def verify_arrivals(arrivals):
    """Require ordered cycles and prevent ambiguous same-cycle reset commands."""
    last = None
    for event in arrivals:
        if type(event.cycle) is not int or event.cycle < 0:
            raise ValueError("arrival cycles must be nonnegative integers")
        if last is not None:
            if last.cycle > event.cycle:
                raise ValueError("arrival cycles must be nondecreasing")
            if last.cycle == event.cycle and (last.reset or event.reset):
                raise ValueError(f"reset must be the only arrival in cycle {event.cycle}")
        last = event


def verify_stalls(intervals):
    """Require nondecreasing disjoint windows for single-pass consumption."""
    last = None
    for interval in intervals:
        if last is not None and interval.start < last.stop:
            raise ValueError("stall windows overlap or are out of order")
        last = interval


def write_csv(path, header, rows):
    """Write reproducible CSV using explicit newline handling on every platform."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_inputs(directory, arrivals, stalls):
    """Export the two portable text files consumed by the workload runner."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_csv(directory / "workload.csv", WORKLOAD_HEADER, (event.to_row() for event in arrivals))
    write_csv(directory / "stalls.csv", STALL_HEADER, (window.to_row() for window in stalls))


class SinkSchedule:
    """Open the sink unless the current cycle falls in a configured stall."""

    def __init__(self, intervals):
        """Copy verified intervals so the caller's input remains reusable."""
        verify_stalls(intervals)
        self.windows = deque(intervals)
        self.last_cycle = -1

    def ready(self, cycle):
        """Advance the interval cursor and return the current ready bit."""
        if cycle < self.last_cycle:
            raise ValueError("cannot rewind a sink schedule")
        self.last_cycle = cycle
        while self.windows and self.windows[0].stop <= cycle:
            self.windows.popleft()
        if not self.windows:
            return 1
        return int(not self.windows[0].contains(cycle))


@dataclass(frozen=True)
class DroppedArrival:
    """Queued producer command discarded by a synchronous reset."""

    queued: int
    reset: int
    tag: int
    key: int

    def to_record(self):
        """Report the cancellation without treating it as a limiter decision."""
        return {
            "arrival_cycle": self.queued,
            "reset_cycle": self.reset,
            "tag": self.tag,
            "key": self.key,
        }


@dataclass(frozen=True)
class Program:
    """Result of compiling the host arrival queue and downstream sink pattern."""

    inputs: Tuple[Drive, ...]
    submitted: int
    accepted: int
    peak_queue: int
    dropped: Tuple[DroppedArrival, ...]

    def to_record(self):
        """Summarize source-side decisions separately from RTL discards."""
        return {
            "cycles": len(self.inputs),
            "submitted": self.submitted,
            "accepted": self.accepted,
            "peak_queue": self.peak_queue,
            "reset_discarded_at_source": [drop.to_record() for drop in self.dropped],
        }

    def prepare(self, output, profile, geometry):
        """Create all compile-time inputs for the common simulator driver."""
        manifest = Manifest(profile, "csv", 0, len(self.inputs), geometry)
        prepare(output, manifest, self.inputs)


def compile_program(geometry, arrivals, stalls, *, max_queue=256, max_cycles=100_000):
    """Produce one valid/ready-compliant schedule with a finite producer queue."""
    if type(max_queue) is not int or max_queue < 1:
        raise ValueError("max_queue must be a positive integer")
    if type(max_cycles) is not int or max_cycles < 2:
        raise ValueError("max_cycles must leave room to reset and drain")
    arrivals = tuple(arrivals)
    stalls = tuple(stalls)
    verify_arrivals(arrivals)
    sink = SinkSchedule(stalls)
    waiting = deque()
    plan = deque(arrivals)
    dropped = []
    peak = 0
    accepted = 0
    arrived = 0
    model = CycleModel(geometry)
    rows = []
    if plan and plan[-1].cycle >= max_cycles:
        raise ValueError("last arrival lies outside the cycle limit")
    if stalls and stalls[-1].stop > max_cycles:
        raise ValueError("sink stall lies outside the cycle limit")
    for cycle in range(max_cycles):
        reset = False
        while plan and plan[0].cycle == cycle:
            event = plan.popleft()
            if event.reset:
                reset = True
            else:
                event.command.validate(geometry)
                waiting.append(event)
                arrived += 1
        if len(waiting) > max_queue:
            raise ValueError(f"cycle {cycle}: source queue exceeded {max_queue} messages")
        peak = max(peak, len(waiting))
        if reset:
            for event in waiting:
                dropped.append(DroppedArrival(event.cycle, cycle, event.command.tag, event.command.key))
            waiting.clear()
            drive = Drive(reset=1, ready=sink.ready(cycle))
        elif waiting:
            drive = Drive(valid=1, ready=sink.ready(cycle), command=waiting[0].command)
        else:
            drive = Drive(ready=sink.ready(cycle))
        observation = model.step(drive)
        rows.append(drive)
        if drive.valid and observation.input_ready:
            waiting.popleft()
            accepted += 1
        if not plan and not waiting and model.outbox is None:
            if not reset and not drive.valid:
                return Program(tuple(rows), arrived, accepted, peak, tuple(dropped))
    raise ValueError("workload did not drain within max_cycles")


def sample_program(geometry):
    """Create a compact, readable example that includes a stalled reset fence."""
    cap = min(geometry.token_max, 6)
    tag = 0
    first = 0
    other = min(geometry.keys - 1, 1)
    note = [Arrival(0)]
    if other != first:
        note.append(Arrival(1, Command.configure(other, 0, cap, 1, tag)))
        tag += 1
    note.extend(
        (
            Arrival(2, Command.configure(first, 0, cap, 1, tag)),
            Arrival(5, Command.offer(first, 0, cap, tag + 1)),
            Arrival(5, Command.offer(first, 0, 2, tag + 2)),
            Arrival(6, Command.inspect(other, 1, tag + 3)),
            Arrival(10),
            Arrival(11, Command.inspect(first, 0, tag + 4)),
            Arrival(12, Command.configure(first, 3, cap, 2, tag + 5)),
            Arrival(13, Command.offer(first, 4, 2, tag + 6)),
            Arrival(15, Command.inspect(first, 5, tag + 7)),
        )
    )
    safe = [
        event if event.reset else Arrival(
            event.cycle,
            Command(
                event.command.op,
                event.command.key,
                geometry.wrapped(event.command.tick),
                min(event.command.amount, geometry.token_max),
                min(event.command.rate, geometry.token_max),
                event.command.enable,
                event.command.tag & geometry.tag_max,
            ),
        )
        for event in note
    ]
    return safe, [Stall(5, 12)]
