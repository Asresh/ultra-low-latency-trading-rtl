"""Cycle-accurate independent model for the ordered two-lane notional wrapper."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field, replace
from typing import Iterable

from notional_model import Kind, Reply, Request, calculate, unsigned


REQUEST_FIELDS = (
    "tag", "price", "quantity", "fee_bps", "rounding", "sell", "limit_enabled", "limit",
)
REPLY_FIELDS = ("tag", "status", "sell", "gross", "fee", "cash", "inexact")
DRIVE_FIELDS = ("reset", "valid", "ready", *REQUEST_FIELDS)
PIN_FIELDS = ("input_ready", "output_valid", "outstanding", *REPLY_FIELDS)
ERROR_SHORTCUT = frozenset({Kind.BAD_QUANTITY, Kind.BAD_PRICE, Kind.BAD_FEE, Kind.BAD_MODE})
FEE_BITS = 80


class PairTraceError(ValueError):
    """An invalid wire record or a timing mismatch with a cycle number."""


def bit(value, name):
    """Decode a single bit without accepting any wider hardware value."""
    return bool(unsigned(value, 1, name))


def wire_row(values, size, name):
    """Require exactly as many columns as the named trace row declares."""
    values = tuple(values)
    if len(values) != size:
        raise PairTraceError(f"{name} expected {size} fields, got {len(values)}")
    return values


def calculation_delay(reply):
    """Return the first visible cycle relative to the request-transfer edge."""
    # A malformed request enters the reply register on the admission edge.
    # Valid requests take three setup clocks, eighty divider digit clocks,
    # one final clock and then become visible at the following sampled edge.
    if reply.status in ERROR_SHORTCUT:
        return 1
    return 3 + FEE_BITS + 1 + 1


def decode_request(values):
    """Recover the explicit optional limit and validate all other wire fields."""
    tag, price, quantity, fee, mode, sell, limit_enable, limit = wire_row(
        values, len(REQUEST_FIELDS), "request",
    )
    enabled = bit(limit_enable, "limit enabled")
    if not enabled and limit:
        raise PairTraceError("disabled limit must have a zero wire value")
    return Request(tag, price, quantity, fee, mode, bit(sell, "sell"), limit if enabled else None)


def decode_reply(values):
    """Recover a reply and independently enforce the exported field widths."""
    tag, status, sell, gross, fee, cash, inexact = wire_row(values, len(REPLY_FIELDS), "reply")
    for name, value, width in (
        ("tag", tag, 16), ("gross", gross, 64), ("fee", fee, 64), ("cash", cash, 64),
    ):
        unsigned(value, width, name)
    return Reply(tag, Kind(status), bit(sell, "sell"), gross, fee, cash, bit(inexact, "inexact"))


@dataclass(frozen=True)
class Drive:
    """One cycle of reset, an optional ready/valid request, and sink readiness."""

    reset: bool = False
    request: Request | None = None
    ready: bool = True

    def __post_init__(self):
        """Require exact Boolean controls and a width-validated request object."""
        if type(self.reset) is not bool or type(self.ready) is not bool:
            raise ValueError("reset and ready must be Boolean")
        if self.request is not None and not isinstance(self.request, Request):
            raise TypeError("request must be a notional Request")

    @property
    def valid(self):
        """An optional request determines whether the source asserts valid."""
        return self.request is not None

    def wire(self):
        """Return normalized hardware fields suitable for the portable testbench."""
        return (
            int(self.reset),
            int(self.valid),
            int(self.ready),
            *(self.request.wire() if self.valid else (0,) * len(REQUEST_FIELDS)),
        )

    @classmethod
    def read(cls, values):
        """Decode all controls and require zeroed payload when valid is low."""
        reset, valid, ready, *payload = wire_row(values, len(DRIVE_FIELDS), "drive")
        request_visible = bit(valid, "request valid")
        if not request_visible and any(payload):
            raise PairTraceError("invalid request payload is not zero")
        request = decode_request(payload) if request_visible else None
        return cls(bit(reset, "reset"), request, bit(ready, "sink ready"))


@dataclass(frozen=True)
class Pins:
    """Observable signals immediately before one active clock edge."""

    input_ready: bool
    reply: Reply | None
    outstanding: int

    @property
    def output_valid(self):
        """Presence of a reply determines whether the result is visible."""
        return self.reply is not None

    def wire(self):
        """Export canonical zero words for an inactive output payload."""
        return (
            int(self.input_ready),
            int(self.output_valid),
            self.outstanding,
            *(self.reply.wire() if self.output_valid else (0,) * len(REPLY_FIELDS)),
        )

    @classmethod
    def read(cls, values):
        """Rebuild pin observations and reject impossible reservation counts."""
        ready, valid, count, *payload = wire_row(values, len(PIN_FIELDS), "pins")
        if not 0 <= unsigned(count, 2, "outstanding") <= 2:
            raise PairTraceError("outstanding count exceeds two lane slots")
        visible = bit(valid, "output valid")
        if not visible and any(payload):
            raise PairTraceError("inactive reply must have a zero payload")
        if visible and count == 0:
            raise PairTraceError("valid output requires an outstanding request")
        reply = decode_reply(payload) if visible else None
        return cls(bit(ready, "input ready"), reply, count)


@dataclass(frozen=True)
class Job:
    """Immutable result ticket held by one execution lane and the order queue."""

    ordinal: int
    lane: int
    admitted: int
    available: int
    request: Request
    reply: Reply
    first_visible: int | None = None

    def visible(self, cycle):
        """Remember the first cycle when this result became the visible head."""
        if self.first_visible is None:
            return replace(self, first_visible=cycle)
        return self


@dataclass(frozen=True)
class Transfer:
    """One ordered result delivery and its independent compute/queue delays."""

    job: Job
    delivered: int

    @property
    def service(self):
        """Report the execution lane delay without any output ordering effect."""
        return self.job.available - self.job.admitted

    @property
    def order_wait(self):
        """Count time spent ready behind an older request."""
        if self.job.first_visible is None:
            raise ValueError("a delivered result must first have been visible")
        return self.job.first_visible - self.job.available

    @property
    def downstream_wait(self):
        """Count cycles when a visible result waited for sink readiness."""
        if self.job.first_visible is None:
            raise ValueError("a delivered result must first have been visible")
        return self.delivered - self.job.first_visible

    def record(self):
        """Export issue, ready, visible, and delivered edges for one transaction."""
        return {
            "ordinal": self.job.ordinal,
            "lane": self.job.lane,
            "tag": self.job.request.tag,
            "status": self.job.reply.status.name,
            "admitted": self.job.admitted,
            "available": self.job.available,
            "visible": self.job.first_visible,
            "delivered": self.delivered,
            "service": self.service,
            "order_wait": self.order_wait,
            "downstream_wait": self.downstream_wait,
        }


@dataclass(frozen=True)
class Edge:
    """Accounting for one completed bus edge."""

    pins: Pins
    admitted: Job | None = None
    completed: Transfer | None = None
    discarded: tuple[Job, ...] = ()


class PairModel:
    """Dequeue by admission order while lanes complete on independent deadlines."""

    def __init__(self, fraction_bits):
        """Start with two idle lanes and a round-robin preference for lane zero."""
        if type(fraction_bits) is not int or not 0 <= fraction_bits <= 31:
            raise ValueError("fraction bits must be in 0..31")
        self.fraction_bits = fraction_bits
        self.cycle = 0
        self.serial = 0
        self.preferred = 0
        self.pending = deque()

    def available_lane(self):
        """Select the preferred free lane using the pre-edge reservations."""
        reserved = {job.lane for job in self.pending}
        if self.preferred not in reserved:
            return self.preferred
        alternate = 1 - self.preferred
        return alternate if alternate not in reserved else None

    def pins(self, drive):
        """Compute normalized pins from current queue order and the head deadline."""
        count = len(self.pending)
        if drive.reset:
            return Pins(False, None, count)
        ready = self.available_lane() is not None
        front = self.pending[0] if count else None
        reply = front.reply if front is not None and front.available <= self.cycle else None
        return Pins(ready, reply, count)

    def head_of_line(self):
        """Detect completed younger work that cannot pass an unfinished head."""
        return (
            len(self.pending) == 2
            and self.pending[0].available > self.cycle
            and self.pending[1].available <= self.cycle
        )

    def ordered_hold(self):
        """Detect a ready younger lane queued behind a ready but held older lane."""
        return (
            len(self.pending) == 2
            and self.pending[0].available <= self.cycle
            and self.pending[1].available <= self.cycle
        )

    def advance(self, drive):
        """Transfer or reset work using only signals and capacity from this edge."""
        if not isinstance(drive, Drive):
            raise TypeError("expected a Drive record")
        observed = self.pins(drive)
        lane = self.available_lane() if observed.input_ready else None
        issue = None
        completion = None
        discarded = ()
        if drive.reset:
            discarded = tuple(self.pending)
            self.pending.clear()
            self.preferred = 0
        else:
            if observed.output_valid:
                first = self.pending[0].visible(self.cycle)
                self.pending[0] = first
                if drive.ready:
                    completion = Transfer(self.pending.popleft(), self.cycle)
            if drive.valid and observed.input_ready:
                reply = calculate(drive.request, self.fraction_bits)
                issue = Job(
                    self.serial, lane, self.cycle, self.cycle + calculation_delay(reply),
                    drive.request, reply,
                )
                self.pending.append(issue)
                self.preferred = 1 - lane
                self.serial += 1
        if len(self.pending) > 2:
            raise AssertionError("independent model exceeded lane capacity")
        self.cycle += 1
        return Edge(observed, issue, completion, discarded)


def differences(expected, actual):
    """Name each differing pin and print its expected and observed values."""
    return ", ".join(
        f"{name} expected={before:#x} actual={after:#x}"
        for name, before, after in zip(PIN_FIELDS, expected.wire(), actual.wire())
        if before != after
    )


@dataclass
class PairCounters:
    """Group measured throughput, pressure, resets, and the two latency classes."""

    cycles: int = 0
    reset_cycles: int = 0
    accepted: int = 0
    completed: int = 0
    discarded: int = 0
    full_cycles: int = 0
    request_stalls: int = 0
    response_stalls: int = 0
    compute_order_stalls: int = 0
    downstream_order_stalls: int = 0
    simultaneous: int = 0
    status: Counter = field(default_factory=Counter)
    lanes: Counter = field(default_factory=Counter)
    service: Counter = field(default_factory=Counter)
    latency: Counter = field(default_factory=Counter)

    def record(self):
        """Convert histogram keys into stable JSON object keys."""
        scalar_names = (
            "cycles", "reset_cycles", "accepted", "completed", "discarded", "full_cycles",
            "request_stalls", "response_stalls", "compute_order_stalls", "downstream_order_stalls",
            "simultaneous",
        )
        result = {name: getattr(self, name) for name in scalar_names}
        for name in ("status", "lanes", "service", "latency"):
            result[name] = {str(key): value for key, value in sorted(getattr(self, name).items())}
        return result


class PinAudit:
    """Compare actual pins and enforce ownership and stability on both interfaces."""

    def __init__(self, fraction_bits):
        """Start with empty held payloads, request ordering, and counters."""
        self.model = PairModel(fraction_bits)
        self.stats = PairCounters()
        self.held_source = None
        self.held_sink = None
        self.transfers = []
        self.discards = []

    def check_hold(self, drive, pins, cycle):
        """Require immutable fields for a blocked request or blocked response."""
        if drive.reset:
            self.held_source = None
            self.held_sink = None
            return
        if self.held_source is not None and self.held_source != drive.request:
            raise PairTraceError(f"cycle {cycle}: stalled request changed")
        if self.held_sink is not None and self.held_sink != pins.reply:
            raise PairTraceError(f"cycle {cycle}: stalled reply changed")
        self.held_source = drive.request if drive.valid and not pins.input_ready else None
        self.held_sink = pins.reply if pins.output_valid and not drive.ready else None

    def check(self, drive, pins):
        """Compare one pre-edge snapshot and classify its actual bus activity."""
        cycle = self.model.cycle
        self.check_hold(drive, pins, cycle)
        computed_wait = self.model.head_of_line()
        downstream_wait = self.model.ordered_hold() and not drive.ready
        expected = self.model.advance(drive)
        if expected.pins != pins:
            raise PairTraceError(f"cycle {cycle}: {differences(expected.pins, pins)}")
        stats = self.stats
        stats.cycles += 1
        if drive.reset:
            stats.reset_cycles += 1
            stats.discarded += len(expected.discarded)
            self.discards.extend(expected.discarded)
            return
        stats.full_cycles += int(pins.outstanding == 2)
        stats.request_stalls += int(drive.valid and not pins.input_ready)
        stats.response_stalls += int(pins.output_valid and not drive.ready)
        stats.compute_order_stalls += int(computed_wait)
        stats.downstream_order_stalls += int(downstream_wait)
        if expected.admitted is not None:
            stats.accepted += 1
            stats.lanes[expected.admitted.lane] += 1
        if expected.completed is not None:
            actual = expected.completed
            self.transfers.append(actual)
            stats.completed += 1
            stats.status[actual.job.reply.status.name] += 1
            stats.service[actual.service] += 1
            stats.latency[actual.delivered - actual.job.admitted] += 1
        if expected.admitted is not None and expected.completed is not None:
            stats.simultaneous += 1

    def finish(self):
        """Require a drained schedule and prove basic accounting conservation."""
        if self.model.pending or self.held_source is not None:
            raise PairTraceError("capture ended without draining outstanding work")
        if self.stats.accepted != self.stats.completed + self.stats.discarded:
            raise PairTraceError("accepted count did not balance against deliveries and resets")
        ordinals = [item.job.ordinal for item in self.transfers]
        if ordinals != sorted(set(ordinals)):
            raise PairTraceError("deliveries must be strictly ordered by admission")
        return self.stats


def audit_rows(fraction_bits, rows: Iterable[tuple[Drive, Pins]]):
    """Verify a full cycle-pair stream and return counters and transaction records."""
    audit = PinAudit(fraction_bits)
    for drive, pins in rows:
        audit.check(drive, pins)
    return audit.finish(), audit.transfers, audit.discards


@dataclass(frozen=True)
class Prediction:
    """Ordered bus event times for saturated source and always-ready sink."""

    ordinal: int
    issue: int
    ready: int
    delivery: int

    def to_record(self):
        """Show the three independent scheduling timestamps for one operation."""
        return {
            "ordinal": self.ordinal,
            "issue": self.issue,
            "ready": self.ready,
            "delivery": self.delivery,
        }


def predict_saturated(delays):
    """Calculate saturated scheduling by order constraints instead of lane simulation."""
    predictions = []
    for ordinal, latency in enumerate(delays):
        if type(latency) is not int or latency <= 0:
            raise ValueError("service delay must be positive")
        # The source can issue once per edge. Once two slots are occupied,
        # work i cannot be admitted until the edge after work i-2 transfers.
        earliest_source = predictions[-1].issue + 1 if ordinal else 0
        capacity_edge = predictions[-2].delivery + 1 if ordinal >= 2 else 0
        issue = max(earliest_source, capacity_edge)
        ready = issue + latency
        prior_delivery = predictions[-1].delivery + 1 if ordinal else 0
        delivery = max(ready, prior_delivery)
        predictions.append(Prediction(ordinal, issue, ready, delivery))
    return predictions


@dataclass
class PhaseReport:
    """Summarize a labeled phase using the observed transaction times."""

    name: str
    start: int
    stop: int
    accepted: int = 0
    delivered: int = 0
    discarded: int = 0
    peak_order_wait: int = 0
    peak_downstream_wait: int = 0
    first_admission: int | None = None
    last_admission: int | None = None
    last_delivery: int | None = None
    total_service: int = 0
    total_order_wait: int = 0
    total_downstream_wait: int = 0
    statuses: Counter = field(default_factory=Counter)

    def contains(self, cycle):
        """Check the half-open interval assigned to this phase."""
        return self.start <= cycle < self.stop

    def admit(self, job):
        """Track the actual admission span for this cohort."""
        if not self.contains(job.admitted):
            raise ValueError("job belongs to a different phase")
        self.accepted += 1
        if self.first_admission is None:
            self.first_admission = job.admitted
            self.last_admission = job.admitted
        else:
            self.first_admission = min(self.first_admission, job.admitted)
            self.last_admission = max(self.last_admission, job.admitted)

    def finish_job(self, transfer):
        """Add one admission and its later delivery, even across phase boundaries."""
        self.admit(transfer.job)
        self.delivered += 1
        if self.last_delivery is None:
            self.last_delivery = transfer.delivered
        else:
            self.last_delivery = max(self.last_delivery, transfer.delivered)
        self.peak_order_wait = max(self.peak_order_wait, transfer.order_wait)
        self.peak_downstream_wait = max(self.peak_downstream_wait, transfer.downstream_wait)
        self.total_service += transfer.service
        self.total_order_wait += transfer.order_wait
        self.total_downstream_wait += transfer.downstream_wait
        self.statuses[transfer.job.reply.status.name] += 1

    def cancel_job(self, job):
        """Count a reset-cancelled admission without inventing a latency."""
        self.admit(job)
        self.discarded += 1

    def record(self):
        """Return phase counters and the distinct causes of result latency."""
        return {
            "name": self.name,
            "start": self.start,
            "stop": self.stop,
            "accepted": self.accepted,
            "delivered": self.delivered,
            "discarded": self.discarded,
            "first_admission": self.first_admission,
            "last_admission": self.last_admission,
            "last_delivery": self.last_delivery,
            "peak_order_wait": self.peak_order_wait,
            "peak_downstream_wait": self.peak_downstream_wait,
            "total_service": self.total_service,
            "total_order_wait": self.total_order_wait,
            "total_downstream_wait": self.total_downstream_wait,
            "statuses": dict(sorted(self.statuses.items())),
        }


def describe_phases(markers, cycles, transfers, discarded):
    """Group every observed admission into the corresponding campaign interval."""
    if type(cycles) is not int or cycles < 0:
        raise ValueError("cycle count must be nonnegative")
    if not isinstance(markers, dict):
        raise ValueError("phase markers must be a name-to-cycle map")
    for name, position in markers.items():
        if not isinstance(name, str) or not name:
            raise ValueError("phase names must be nonempty strings")
        if type(position) is not int or not 0 <= position < cycles:
            raise ValueError("phase marker is outside capture")
    ordered = sorted(markers.items(), key=lambda pair: pair[1])
    if not ordered or ordered[0][1] != 0:
        raise ValueError("phase markers must include the start of capture")
    if len(set(markers.values())) != len(markers):
        raise ValueError("phase markers share an edge")
    ranges = [
        PhaseReport(name, start, ordered[index + 1][1] if index + 1 < len(ordered) else cycles)
        for index, (name, start) in enumerate(ordered)
    ]

    def region(job):
        """Locate the cohort using admission time rather than delivery time."""
        for phase in ranges:
            if phase.contains(job.admitted):
                return phase
        raise ValueError("admitted job is outside the marked capture")

    for transfer in transfers:
        region(transfer.job).finish_job(transfer)
    for job in discarded:
        region(job).cancel_job(job)
    return [phase.record() for phase in ranges]
