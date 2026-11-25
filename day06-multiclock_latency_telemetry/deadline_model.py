"""Independent report, control, and alert model for the latency deadline tap."""

from collections import Counter, deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


U16_MAX = (1 << 16) - 1
U32_MAX = (1 << 32) - 1
U64_MAX = (1 << 64) - 1


class Op(IntEnum):
    PROGRAM = 0
    INSPECT = 1
    CLEAR = 2


class Result(IntEnum):
    DONE = 0
    BAD_RULE = 1
    STALE = 2
    BAD_POLICY = 3
    EXHAUSTED = 4
    BAD_OPERATION = 5


def pack(*fields):
    word = 0
    for value, width in fields:
        if not 0 <= value < 1 << width:
            raise ValueError(f"value {value} exceeds unsigned {width}-bit field")
        word = (word << width) | value
    return word


def bump(counter, key):
    counter[key] = min(U32_MAX, counter[key] + 1)


@dataclass(frozen=True)
class Report:
    event_class: int = 3
    tag: int = 0
    start: int = 1
    stop: int = 4
    duration: int = 0
    tick: int = 0
    status: int = 1

    def wire(self):
        return pack(
            (self.status, 3), (self.event_class, 8), (self.tag, 16),
            (self.start, 8), (self.stop, 8), (0, 21), (0, 64), (0, 64),
            (self.duration, 64), (self.tick, 64),
        )


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    event_class: int = 0
    start: int = 0
    stop: int = 0
    wild: int = 0
    limit: int = 0
    trip: int = 0
    recover: int = 0

    def allows(self, report):
        if not self.enabled:
            return False
        for mask, rule, value in (
            (4, self.event_class, report.event_class),
            (2, self.start, report.start),
            (1, self.stop, report.stop),
        ):
            if rule != value and not self.wild & mask:
                return False
        return True

    def wire(self):
        return pack(
            (self.enabled, 1), (self.event_class, 8), (self.start, 8), (self.stop, 8),
            (self.wild, 3), (self.limit, 64), (self.trip, 16), (self.recover, 16),
        )


@dataclass(frozen=True)
class Command:
    op: int
    rule: int = 0
    revision: int = 0
    policy: Policy = Policy()


@dataclass(frozen=True)
class Input:
    report: Optional[Report] = None
    command: Optional[Command] = None
    alert_ready: bool = True
    reply_ready: bool = True
    reset: bool = False


@dataclass
class Rule:
    policy: Policy = Policy()
    revision: int = 0
    alarm: bool = False
    bad: int = 0
    good: int = 0
    stats: Counter = field(default_factory=Counter)
    worst: int = 0

    def snapshot(self):
        p = self.policy
        return pack(
            (p.enabled, 1), (p.event_class, 8), (p.start, 8), (p.stop, 8),
            (p.wild, 3), (self.alarm, 1), (p.limit, 64), (p.trip, 16), (p.recover, 16),
            (self.bad, 16), (self.good, 16),
            (self.stats["matched"], 32), (self.stats["breaches"], 32),
            (self.stats["trip"], 32), (self.stats["recover"], 32),
            (self.stats["lost"], 32), (self.worst, 64),
        )

    def classify(self, duration):
        bump(self.stats, "matched")
        self.worst = max(self.worst, duration)
        if duration > self.policy.limit:
            bump(self.stats, "breaches")
            self.bad = min(U16_MAX, self.bad + 1)
            self.good = 0
            if self.bad >= self.policy.trip and not self.alarm:
                self.alarm = True
                bump(self.stats, "trip")
                return 1, self.bad
        else:
            self.bad = 0
            self.good = min(U16_MAX, self.good + 1)
            if self.good >= self.policy.recover and self.alarm:
                self.alarm = False
                bump(self.stats, "recover")
                return 2, self.good
        return 0, 0

    def program(self, policy):
        self.policy = policy
        self.revision += 1
        self.alarm = False
        self.bad = 0
        self.good = 0


@dataclass(frozen=True)
class Reply:
    status: int = 0
    op: int = 0
    slot: int = 0
    revision: int = 0
    data: int = 0


@dataclass(frozen=True)
class Alert:
    slot: int = 0
    revision: int = 0
    data: int = 0


@dataclass(frozen=True)
class Output:
    ctl_ready: bool
    reply_valid: bool
    reply: Reply
    alert_valid: bool
    alert: Alert
    used: int
    seen: int
    nonmatch: int
    unclassified: int
    lost: int


class Monitor:
    def __init__(self, count, depth, revision_width):
        self.count = count
        self.depth = depth
        self.revision_max = (1 << revision_width) - 1
        self.reset()

    def reset(self):
        self.rules = [Rule() for _ in range(self.count)]
        self.alerts = deque()
        self.reply = Reply()
        self.reply_pending = False
        self.global_stats = Counter()

    def inspect(self, port):
        return Output(
            ctl_ready=not port.reset and (not self.reply_pending or port.reply_ready),
            reply_valid=not port.reset and self.reply_pending,
            reply=self.reply,
            alert_valid=not port.reset and bool(self.alerts),
            alert=self.alerts[0] if not port.reset and self.alerts else Alert(),
            used=len(self.alerts),
            seen=self.global_stats["seen"],
            nonmatch=self.global_stats["nonmatch"],
            unclassified=self.global_stats["unclassified"],
            lost=self.global_stats["lost"],
        )

    def process_control(self, command):
        if command.rule >= self.count:
            return Reply(Result.BAD_RULE, command.op, command.rule)
        rule = self.rules[command.rule]
        policy = command.policy
        status = Result.DONE
        if command.op not in (Op.PROGRAM, Op.INSPECT, Op.CLEAR):
            status = Result.BAD_OPERATION
        elif command.op != Op.INSPECT and command.revision != rule.revision:
            status = Result.STALE
        elif command.op == Op.PROGRAM:
            if rule.revision == self.revision_max:
                status = Result.EXHAUSTED
            elif policy.enabled and min(policy.trip, policy.recover) == 0:
                status = Result.BAD_POLICY
        revision = rule.revision + (status == Result.DONE and command.op == Op.PROGRAM)
        return Reply(status, command.op, command.rule, revision, rule.snapshot())

    def sample(self, report):
        bump(self.global_stats, "seen")
        if report.status != 1:
            bump(self.global_stats, "nonmatch")
            return
        for slot, rule in enumerate(self.rules):
            if rule.policy.allows(report):
                break
        else:
            bump(self.global_stats, "unclassified")
            return
        code, streak = rule.classify(report.duration)
        if not code:
            return
        if len(self.alerts) == self.depth:
            bump(rule.stats, "lost")
            bump(self.global_stats, "lost")
            return
        data = pack(
            (report.event_class, 8), (report.tag, 16), (report.start, 8), (report.stop, 8),
            (code, 8), (streak, 16), (rule.policy.limit, 64), (report.duration, 64),
            (report.tick, 64),
        )
        self.alerts.append(Alert(slot, rule.revision, data))

    def advance(self, port):
        before = self.inspect(port)
        if port.reset:
            self.reset()
            return before
        if before.alert_valid and port.alert_ready:
            self.alerts.popleft()
        if before.reply_valid and port.reply_ready:
            self.reply_pending = False
        update = None
        if before.ctl_ready and port.command is not None:
            self.reply = self.process_control(port.command)
            self.reply_pending = True
            if self.reply.status == Result.DONE:
                if port.command.op == Op.CLEAR:
                    rule = self.rules[port.command.rule]
                    rule.stats.clear()
                    rule.worst = 0
                elif port.command.op == Op.PROGRAM:
                    update = port.command
        if port.report is not None:
            self.sample(port.report)
        if update is not None:
            self.rules[update.rule].program(update.policy)
        return before
