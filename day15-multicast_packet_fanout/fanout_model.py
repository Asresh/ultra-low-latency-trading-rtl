"""Executable packet-queue specification for the multicast fanout."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from enum import IntEnum


class Status(IntEnum):
    """Packet report codes on the RTL interface."""

    FORWARDED = 0
    FILTERED = 1
    CONGESTED = 2
    BAD_BEAT = 3
    BAD_TAG = 4
    TOO_LONG = 5
    BAD_GROUP = 6


@dataclass(frozen=True)
class Profile:
    """Geometry shared by vector encoder and simulation elaboration."""

    data_bytes: int
    ports: int
    groups: int
    slots: int
    depth: int
    max_beats: int
    tag_width: int

    @property
    def full_keep(self):
        """Return the lane mask for a full-width beat."""
        return (1 << self.data_bytes) - 1

    @property
    def all_ports(self):
        """Return the bit mask containing every implemented output port."""
        return (1 << self.ports) - 1

    def defines(self):
        """Map profile values to their SystemVerilog parameter names."""
        return dict(DATA_BYTES=self.data_bytes, PORTS=self.ports, GROUPS=self.groups,
                    SLOTS=self.slots, PORT_DEPTH=self.depth, MAX_BEATS=self.max_beats,
                    TAG_W=self.tag_width)


PROFILES = {
    "default": Profile(4, 3, 4, 4, 2, 8, 16),
    "odd": Profile(3, 5, 3, 5, 3, 5, 9),
    "solo": Profile(1, 1, 1, 1, 1, 1, 5),
}


@dataclass(frozen=True)
class Beat:
    """Raw ingress data and metadata for one offer."""

    data: int = 0
    keep: int = 0
    last: bool = False
    group: int = 0
    tag: int = 0


@dataclass(frozen=True)
class Route:
    """A destination mask plus the policy when a requested resource is full."""

    mask: int = 0
    may_drop: bool = False


@dataclass(frozen=True)
class Inputs:
    """Signals driven for one cycle before its rising edge."""

    rst: bool = False
    cfg: tuple[int, Route] | None = None
    offer: Beat | None = None
    ready: int = 0
    report_ready: bool = True

    def words(self):
        """Encode all stimulus ports in testbench column order."""
        group, route = self.cfg or (0, Route())
        beat = self.offer or Beat()
        return (self.rst, self.cfg is not None, group, route.mask, route.may_drop,
                self.offer is not None, beat.data, beat.keep, beat.last, beat.group,
                beat.tag, self.ready, self.report_ready)


@dataclass(frozen=True)
class OutputBeat:
    """One per-port payload beat after invalid lanes have been cleared."""

    data: int
    keep: int
    last: bool
    tag: int


@dataclass(frozen=True)
class Diagnostic:
    """Terminal classification and the route snapshot for one accepted packet."""

    status: Status
    tag: int
    group: int
    destinations: int
    selected_bytes: int


@dataclass(frozen=True)
class Sample:
    """Cycle observation, including all output ports and management counters."""

    cfg_ready: bool
    s_ready: bool
    outputs: tuple
    report: Diagnostic | None
    outcomes: tuple
    delivered: tuple
    occupancy: tuple
    resident: int

    def words(self, profile):
        """Flatten optional and per-port fields into the RTL trace column order."""
        outputs = [beat or OutputBeat(0, 0, False, 0) for beat in self.outputs]
        report = self.report or Diagnostic(Status.FORWARDED, 0, 0, 0, 0)
        return (
            self.cfg_ready, self.s_ready,
            pack((beat is not None for beat in self.outputs), 1),
            pack((beat.last for beat in outputs), 1),
            pack((beat.data for beat in outputs), profile.data_bytes * 8),
            pack((beat.keep for beat in outputs), profile.data_bytes),
            pack((beat.tag for beat in outputs), profile.tag_width),
            self.report is not None, report.status, report.tag, report.group,
            report.destinations, report.selected_bytes, *self.outcomes,
            pack(self.delivered, 32), pack(self.occupancy, profile.depth.bit_length()),
            self.resident,
        )


@dataclass
class Frame:
    """Packet in assembly with a frozen route decision and first-fault record."""

    group: int
    tag: int
    route: Route
    decision: Status
    beats: list
    shape_error: Status | None = None
    selected_bytes: int = 0


@dataclass(frozen=True, eq=False)
class Packet:
    """Immutable transmission shared across all subscribing receiver queues."""

    beats: tuple[OutputBeat, ...]


def pack(values, width):
    """Place fields consecutively with element zero in the least significant bits."""
    return sum(int(value) << (index * width) for index, value in enumerate(values))


class Fanout:
    """Model independent egress queues of shared immutable Python packet objects."""

    def __init__(self, profile):
        """Install geometry and initialize the same externally visible reset state."""
        self.p = profile
        self.reset()

    def reset(self):
        """Discard every route, packet, diagnostic, and measurement."""
        self.routes = [Route() for _ in range(self.p.groups)]
        self.queues = [deque() for _ in range(self.p.ports)]
        self.cursors = [0] * self.p.ports
        self.frame = None
        self.report = None
        self.outcomes = Counter()
        self.delivered = [0] * self.p.ports

    @property
    def resident(self):
        """Count distinct egress packet objects and any accepted packet in assembly."""
        packets = {packet for queue in self.queues for packet in queue}
        assembling = self.frame is not None and self.frame.decision == Status.FORWARDED
        return len(packets) + int(assembling)

    def route(self, beat):
        """Evaluate admissions against the current, pre-edge route and resources."""
        if beat.group >= self.p.groups:
            return Route(), Status.BAD_GROUP, True
        route = self.routes[beat.group]
        if not route.mask:
            return route, Status.FILTERED, True
        selected = [queue for port, queue in enumerate(self.queues) if route.mask >> port & 1]
        if self.resident < self.p.slots and all(len(queue) < self.p.depth for queue in selected):
            return route, Status.FORWARDED, True
        return route, Status.CONGESTED, route.may_drop

    def peek(self, inputs):
        """Return pre-edge signals, keeping metadata for invalid channels zero."""
        offer = inputs.offer or Beat()
        decided = self.frame is not None
        has_report_room = self.report is None or inputs.report_ready
        s_ready = not inputs.rst and (decided or (has_report_room and self.route(offer)[2]))
        outputs = tuple(
            queue[0].beats[self.cursors[port]] if queue and not inputs.rst else None
            for port, queue in enumerate(self.queues)
        )
        outcome = (
            self.outcomes["forwarded"], self.outcomes["filtered"],
            self.outcomes["congested"], self.outcomes["invalid"],
        )
        return Sample(not inputs.rst, s_ready, outputs, self.report, outcome,
                      tuple(self.delivered), tuple(map(len, self.queues)), self.resident)

    def step(self, inputs):
        """Observe the current edge, then make all accepted transactions visible."""
        sample = self.peek(inputs)
        if inputs.rst:
            self.reset()
            return sample
        start = None
        if inputs.offer is not None and sample.s_ready and self.frame is None:
            route, decision, _ = self.route(inputs.offer)
            start = Frame(inputs.offer.group, inputs.offer.tag, route, decision, [])
        if inputs.cfg is not None:
            group, new_route = inputs.cfg
            if group < self.p.groups:
                self.routes[group] = new_route
        if inputs.report_ready:
            self.report = None
        for port, beat in enumerate(sample.outputs):
            if beat is None or not (inputs.ready >> port & 1):
                continue
            if beat.last:
                self.queues[port].popleft()
                self.cursors[port] = 0
                self.delivered[port] = min(self.delivered[port] + 1, 0xffffffff)
            else:
                self.cursors[port] += 1
        if inputs.offer is not None and sample.s_ready:
            if start is not None:
                self.frame = start
            self.receive(inputs.offer)
        return sample

    def receive(self, beat):
        """Assemble a beat, preserving the first fault until a terminal result."""
        frame = self.frame
        lanes = [lane for lane in range(self.p.data_bytes) if beat.keep >> lane & 1]
        frame.selected_bytes = min(65535, frame.selected_bytes + len(lanes))
        if frame.shape_error is None:
            if not lanes or lanes != list(range(len(lanes))) or (
                not beat.last and len(lanes) != self.p.data_bytes
            ):
                frame.shape_error = Status.BAD_BEAT
            elif beat.tag != frame.tag:
                frame.shape_error = Status.BAD_TAG
            elif len(frame.beats) >= self.p.max_beats:
                frame.shape_error = Status.TOO_LONG
            else:
                data = sum((beat.data >> (8 * lane) & 255) << (8 * lane) for lane in lanes)
                frame.beats.append(OutputBeat(data, beat.keep, beat.last, frame.tag))
        if beat.last:
            self.close_frame()

    def close_frame(self):
        """Publish a valid immutable packet to every subscriber and record its result."""
        frame = self.frame
        status = frame.decision
        if status != Status.BAD_GROUP and frame.shape_error is not None:
            status = frame.shape_error
        if status == Status.FORWARDED:
            packet = Packet(tuple(frame.beats))
            for port, queue in enumerate(self.queues):
                if frame.route.mask >> port & 1:
                    queue.append(packet)
        counter = {
            Status.FORWARDED: "forwarded",
            Status.FILTERED: "filtered",
            Status.CONGESTED: "congested",
        }.get(status, "invalid")
        self.outcomes[counter] = min(0xffffffff, self.outcomes[counter] + 1)
        self.report = Diagnostic(status, frame.tag, frame.group, frame.route.mask, frame.selected_bytes)
        self.frame = None
