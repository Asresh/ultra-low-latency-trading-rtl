"""Independent integer reference for port, group and session limit accounting."""

from dataclasses import dataclass, replace
from enum import IntEnum


class Op(IntEnum):
    CONFIG_PORT = 0
    CONFIG_GROUP = 1
    CONFIG_SESSION = 2
    INSPECT_PORT = 3
    INSPECT_GROUP = 4
    INSPECT_SESSION = 5
    OFFER = 6


class Status(IntEnum):
    OK = 0
    PASS = 1
    LIMITED = 2
    BAD_SESSION = 3
    BAD_FIELDS = 4
    DISABLED = 5
    BAD_TICK = 6
    BAD_GROUP = 7


@dataclass(frozen=True)
class Geometry:
    groups: int = 3
    sessions: int = 7
    tick_w: int = 12
    token_w: int = 12
    count_w: int = 16
    tag_w: int = 16

    @property
    def command_widths(self):
        return (
            3, self.sessions.bit_length(), self.groups.bit_length(), self.tick_w,
            self.token_w, self.token_w, self.token_w, 1, 1, self.tag_w,
        )

    @property
    def snapshot_widths(self):
        return (
            self.token_w, 1, self.tick_w, self.token_w, self.token_w,
            self.token_w, self.token_w, self.count_w, self.count_w, 2,
        )


@dataclass(frozen=True)
class Command:
    op: int
    session: int = 0
    group: int = 0
    tick: int = 0
    amount: int = 0
    rate: int = 0
    reserve: int = 0
    enable: bool = False
    critical: bool = False
    tag: int = 0

    def pack(self, geometry):
        return pack(zip(vars(self).values(), geometry.command_widths))


@dataclass(frozen=True)
class Bucket:
    capacity: int = 0
    rate: int = 0
    reserve: int = 0
    balance: int = 0
    anchor: int = 0
    admitted: int = 0
    denied: int = 0
    overflow: int = 0

    @property
    def enabled(self):
        return self.capacity != 0

    def elapsed(self, tick, width):
        return (tick-self.anchor) % (1 << width)

    def refilled(self, tick, width):
        delta = self.elapsed(tick, width)
        return replace(self, balance=min(self.capacity, self.balance + self.rate*delta), anchor=tick)

    def moved(self, amount, admitted, denied, max_count):
        count = self.admitted + admitted
        denied_count = self.denied + denied
        lost = (count > max_count) | ((denied_count > max_count) << 1)
        return replace(
            self, balance=self.balance-amount, admitted=min(count, max_count),
            denied=min(denied_count, max_count), overflow=self.overflow | lost,
        )

    def snapshot(self, before, geometry):
        fields = (
            before, self.enabled, self.anchor, self.capacity, self.rate, self.reserve,
            self.balance, self.admitted, self.denied, self.overflow,
        )
        return pack(zip(fields, geometry.snapshot_widths))


EMPTY = Bucket()


def pack(fields):
    result = 0
    for value, width in fields:
        if not 0 <= value < 1 << width:
            raise ValueError(f"{value} does not fit in {width} bits")
        result = (result << width) | value
    return result


@dataclass(frozen=True)
class Decision:
    command: Command
    group: int
    critical: bool
    status: Status
    scope: int
    detail: int
    before: tuple
    after: tuple

    def pack(self, geometry):
        fields = (
            (self.command.op, 3), (self.command.session, geometry.sessions.bit_length()),
            (self.group, geometry.groups.bit_length()), (self.command.tag, geometry.tag_w),
            (self.status, 4), (self.scope, 3), (self.detail, 3), (self.critical, 1),
        )
        snapshot_width = sum(geometry.snapshot_widths)
        rows = (
            (bucket.snapshot(before, geometry), snapshot_width)
            for before, bucket in zip(self.before, self.after)
        )
        return pack((*fields, *rows))


def mask(flags):
    return sum(bool(flag) << i for i, flag in enumerate(flags))


class Hierarchy:
    """A dictionary keyed by scope and ID provides a reference for atomic updates."""

    def __init__(self, geometry):
        self.geometry = geometry
        self.buckets = {}
        self.roles = {}

    def apply(self, command):
        g = self.geometry
        op = command.op
        cfg = op in (Op.CONFIG_PORT, Op.CONFIG_GROUP, Op.CONFIG_SESSION)
        session_only = op in (Op.CONFIG_SESSION, Op.INSPECT_SESSION)
        group_only = op in (Op.CONFIG_GROUP, Op.INSPECT_GROUP)
        offer = op == Op.OFFER
        selectors = (
            offer or op in (Op.CONFIG_PORT, Op.INSPECT_PORT), offer or group_only,
            offer or session_only,
        )
        session_ok = 0 <= command.session < g.sessions
        parent, critical = self.roles.get(command.session, (0, False))
        critical = critical and session_ok and (offer or session_only)
        group = parent if session_ok and op in (Op.OFFER, Op.INSPECT_SESSION) else command.group
        group_ok = 0 <= group < g.groups
        keys = (("port", 0), ("group", group), ("session", command.session))
        prior = tuple(self.buckets.get(key, EMPTY) if use else EMPTY for key, use in zip(keys, selectors))
        scope = mask(selectors)
        before = tuple(bucket.balance for bucket in prior)

        def decision(status, detail=0, after=prior):
            return Decision(command, group, critical, status, scope, detail, before, after)

        if (offer or session_only) and not session_ok:
            return decision(Status.BAD_SESSION)
        if (group_only or op == Op.CONFIG_SESSION and command.enable) and not group_ok:
            return decision(Status.BAD_GROUP)
        if not self.fields_valid(command, offer, cfg, session_only, group_only):
            return decision(Status.BAD_FIELDS)
        if cfg:
            target = selectors.index(True)
            updated = Bucket(command.amount, command.rate, command.reserve, command.amount, command.tick)
            self.buckets[keys[target]] = updated
            if session_only:
                critical = command.critical
                self.roles[command.session] = (group, critical)
            rows = list(prior)
            rows[target] = updated
            return decision(Status.OK, after=tuple(rows))

        disabled = mask(not bucket.enabled for bucket in prior)
        if offer and disabled:
            return decision(Status.DISABLED, disabled)
        stale = mask(
            item.enabled and item.elapsed(command.tick, g.tick_w) >= (1 << (g.tick_w-1))
            for item in prior
        )
        if stale:
            return decision(Status.BAD_TICK, stale)
        rows = tuple(bucket.refilled(command.tick, g.tick_w) if bucket.enabled else bucket for bucket in prior)
        before = tuple(bucket.balance for bucket in rows)
        limited = 0
        status = Status.OK
        if offer:
            limited = mask(bucket.balance - (0 if critical else bucket.reserve) < command.amount for bucket in rows)
            status = Status.LIMITED if limited else Status.PASS
            rows = tuple(
                bucket.moved(
                    0 if limited else command.amount, not limited, bool(limited & 1 << index),
                    (1 << g.count_w)-1,
                )
                for index, bucket in enumerate(rows)
            )
        self.buckets.update((key, row) for key, row in zip(keys, rows) if row.enabled)
        return decision(status, limited, rows)

    @staticmethod
    def fields_valid(command, offer, cfg, session, group):
        if command.op == 7 or command.session and not (session or offer):
            return False
        if command.group and not (group or command.op == Op.CONFIG_SESSION):
            return False
        if cfg and command.enable:
            if not (0 < command.amount and command.rate <= command.amount and command.reserve <= command.amount):
                return False
            return not command.reserve if session else not command.critical
        if cfg:
            return not (command.amount or command.rate or command.reserve or command.critical or session and command.group)
        return not (command.rate or command.reserve or command.critical or command.enable) and bool(command.amount) == offer
