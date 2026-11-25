"""Independent integer and cycle reference for the PPS frequency controller."""

from collections import Counter, deque
from dataclasses import dataclass
from enum import IntEnum
from fractions import Fraction
from typing import Optional, Tuple

from pps_discipline_audit import Audit, Code, Mode
from latency_reference import LIMIT32, LIMIT64, HALF64


Q32 = 1 << 32
INITIAL = 8 * Q32
DIVISION_EDGES = 97


class Op(IntEnum):
    PROGRAM = 0
    INSPECT = 1
    RESEED = 2
    CLEAR = 3


class Status(IntEnum):
    OK = 0
    STALE_CONFIG = 1
    BAD_POLICY = 2
    EXHAUSTED = 3


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    reference_ns: int = 0
    min_ticks: int = 0
    max_ticks: int = 0
    timeout: int = 0
    max_age: int = 0
    jitter: int = 0
    minimum: int = 0
    maximum: int = 0
    max_step: int = 0
    windows: int = 0

    def accepts(self, period):
        return (
            self.reference_ns > 0 and 0 < self.min_ticks <= self.max_ticks < HALF64 and
            self.max_ticks < self.timeout < HALF64 and self.max_age <= self.timeout and
            0 < self.minimum <= period <= self.maximum and self.max_step > 0 and self.windows > 0
        )


@dataclass(frozen=True)
class Command:
    op: Op
    revision: int = 0
    policy: Policy = Policy()


@dataclass(frozen=True)
class Reply:
    op: Op
    status: Status
    revision: int
    period: int
    mode: Mode


@dataclass(frozen=True)
class Measurement:
    tick: int
    median: int
    target: int
    cycle_due: int
    overflow: bool


@dataclass(frozen=True)
class Offer:
    measurement: Measurement
    period: int


@dataclass(frozen=True)
class Inputs:
    now: int = 0
    sample: Optional[int] = None
    control: Optional[Command] = None
    reset: bool = False
    reply_ready: bool = True
    audit_ready: bool = True
    rate_ready: bool = True


@dataclass(frozen=True)
class Pins:
    cfg_ready: bool
    sample_ready: bool
    reply: Optional[Reply]
    audit: Optional[Audit]
    rate: Optional[Offer]
    audit_occupancy: int
    audit_cursor: int
    mode: Mode
    anchor_valid: bool
    anchor_tick: int
    active_period: int
    intervals: int
    streak: int
    counts: Tuple[int, ...]


COUNTERS = ("sampled", "invalid", "usable", "applied", "rejections", "holdovers", "lost")


def delta(a, b):
    return (a - b) & LIMIT64


def measured_period(reference_ns, ticks):
    if ticks <= 0:
        raise ValueError("measurement duration must be positive")
    return round(Fraction(reference_ns * Q32, ticks))


def bump(counts, key):
    counts[key] = min(LIMIT32, counts[key] + 1)


class Discipline:
    """Sort bounded measurement windows and use rational arithmetic for rate selection."""

    def __init__(self, depth, revision_width, initial=INITIAL):
        self.depth = depth
        self.high_revision = (1 << revision_width) - 1
        self.initial = initial
        self.reset()

    def reset_history(self):
        self.mode = Mode.ACQUIRE if self.policy.enabled else Mode.OFF
        self.anchor_valid = False
        self.anchor = 0
        self.history = deque(maxlen=3)
        self.streak = 0

    def reset(self):
        self.cycle = 0
        self.revision = 0
        self.policy = Policy()
        self.period = self.initial
        self.reply = None
        self.audits = deque()
        self.audit_cursor = 0
        self.math = None
        self.offer = None
        self.counts = Counter()
        self.reset_history()

    def config_status(self, control):
        if control.op != Op.INSPECT and control.revision != self.revision:
            return Status.STALE_CONFIG
        if control.op in (Op.PROGRAM, Op.RESEED) and self.revision == self.high_revision:
            return Status.EXHAUSTED
        if control.op == Op.PROGRAM and control.policy.enabled and not control.policy.accepts(self.period):
            return Status.BAD_POLICY
        return Status.OK

    def control_ready(self, port):
        blocked = bool(port.control and port.control.op in (Op.PROGRAM, Op.RESEED) and
                       (self.math or self.offer))
        return not port.reset and not blocked and (self.reply is None or port.reply_ready)

    def will_reseed(self, port):
        control = port.control
        return bool(control and control.op in (Op.PROGRAM, Op.RESEED) and
                    self.control_ready(port) and self.config_status(control) == Status.OK)

    def inspect(self, port):
        return Pins(
            self.control_ready(port),
            not port.reset and not (self.math or self.offer or self.will_reseed(port)),
            None if port.reset else self.reply,
            self.audits[0] if self.audits and not port.reset else None,
            None if port.reset else self.offer,
            len(self.audits),
            self.audit_cursor,
            self.mode,
            self.anchor_valid,
            self.anchor,
            self.period,
            len(self.history),
            self.streak,
            tuple(self.counts[key] for key in COUNTERS),
        )

    def classify(self, sample, tick):
        elapsed = delta(sample, self.anchor)
        age = delta(tick, sample)
        policy = self.policy
        if not policy.enabled:
            return Code.DISABLED, elapsed, None
        if age >= HALF64 or age > policy.max_age:
            return Code.STALE_SAMPLE, elapsed, None
        if not self.anchor_valid:
            return Code.SEED, elapsed, None
        if elapsed == 0 or elapsed >= HALF64:
            return Code.REVERSE, elapsed, None
        if elapsed < policy.min_ticks:
            return Code.EARLY, elapsed, None
        if elapsed > policy.max_ticks:
            return Code.LATE, elapsed, None
        next_window = sorted((*list(self.history)[-2:], elapsed))
        if len(next_window) < 3:
            return Code.WARMUP, elapsed, None
        if next_window[-1] - next_window[0] > policy.jitter:
            return Code.JITTER, elapsed, None
        choose = Code.QUORUM if self.streak + 1 < policy.windows else Code.MEASURE
        return choose, elapsed, next_window[1]

    def apply_config(self, control):
        status = self.config_status(control)
        if status == Status.OK:
            if control.op in (Op.PROGRAM, Op.RESEED):
                self.revision += 1
                if control.op == Op.PROGRAM:
                    self.policy = control.policy
                self.reset_history()
            elif control.op == Op.CLEAR:
                self.counts.clear()
        return Reply(control.op, status, self.revision, self.period, self.mode)

    def push(self, code, tick, sample_tick, ticks, target=0, period=None):
        sequence = self.audit_cursor
        self.audit_cursor = min(LIMIT64, self.audit_cursor + 1)
        if len(self.audits) == self.depth or sequence == LIMIT64:
            bump(self.counts, "lost")
            return
        self.audits.append(Audit(self.revision, code, self.mode, tick, sample_tick, ticks,
                                 target, self.period if period is None else period, sequence))

    def consume_sample(self, port, code, elapsed, median):
        bump(self.counts, "sampled")
        interval = elapsed if self.anchor_valid and code >= Code.REVERSE else 0
        self.push(code, port.now, port.sample, interval)
        if code < Code.WARMUP and code != Code.SEED:
            bump(self.counts, "invalid")
        if code in (Code.SEED, Code.LATE):
            self.anchor_valid = True
            self.anchor = port.sample
            self.history.clear()
            self.streak = 0
            if self.mode != Mode.HOLDOVER:
                self.mode = Mode.ACQUIRE
        elif code >= Code.WARMUP:
            bump(self.counts, "usable")
            self.anchor = port.sample
            self.history.append(elapsed)
            if code == Code.JITTER:
                self.streak = 0
                if self.mode != Mode.HOLDOVER:
                    self.mode = Mode.ACQUIRE
            elif code in (Code.QUORUM, Code.MEASURE):
                self.streak = min(255, self.streak + 1)
            if code == Code.MEASURE:
                target = measured_period(self.policy.reference_ns, median)
                self.math = Measurement(port.sample, median, target if target <= LIMIT64 else 0,
                                        self.cycle + DIVISION_EDGES, target > LIMIT64)

    def finish_math(self, port, watchdog):
        work = self.math
        self.math = None
        if not self.anchor_valid or watchdog:
            self.push(Code.ABANDONED, port.now, work.tick, work.median, work.target)
            bump(self.counts, "rejections")
        elif work.overflow or not self.policy.minimum <= work.target <= self.policy.maximum:
            self.push(Code.RATE_RANGE, port.now, work.tick, work.median, work.target)
            bump(self.counts, "rejections")
            self.streak = 0
            if self.mode != Mode.HOLDOVER:
                self.mode = Mode.ACQUIRE
        elif work.target == self.period:
            self.push(Code.UNCHANGED, port.now, work.tick, work.median, work.target)
            self.mode = Mode.LOCKED
        else:
            step = self.policy.max_step
            proposal = max(self.period - step, min(self.period + step, work.target))
            self.offer = Offer(work, proposal)

    def apply_rate(self, port, watchdog):
        offer = self.offer
        self.push(Code.APPLIED, (port.now+1) & LIMIT64, offer.measurement.tick,
                  offer.measurement.median, offer.measurement.target, offer.period)
        self.period = offer.period
        self.offer = None
        bump(self.counts, "applied")
        if self.anchor_valid and not watchdog:
            self.mode = Mode.LOCKED

    def advance(self, port):
        pins = self.inspect(port)
        if port.reset:
            self.reset()
            return pins
        reseed = self.will_reseed(port)
        if port.reply_ready:
            self.reply = None
        if port.audit_ready and self.audits:
            self.audits.popleft()
        if port.control and pins.cfg_ready:
            self.reply = self.apply_config(port.control)
        code = elapsed = median = None
        if port.sample is not None and pins.sample_ready:
            code, elapsed, median = self.classify(port.sample, port.now)
        refresh = code is not None and (code in (Code.SEED, Code.LATE) or code >= Code.WARMUP)
        watchdog = (self.policy.enabled and self.anchor_valid and not reseed and not refresh and
                    delta(port.now, self.anchor) >= self.policy.timeout)
        if pins.rate and port.rate_ready:
            self.apply_rate(port, watchdog)
        elif self.math and self.math.cycle_due == self.cycle:
            self.finish_math(port, watchdog)
        elif code is not None:
            self.consume_sample(port, code, elapsed, median)
        if watchdog:
            self.mode = Mode.HOLDOVER
            self.anchor_valid = False
            self.history.clear()
            self.streak = 0
            bump(self.counts, "holdovers")
        self.cycle += 1
        return pins
