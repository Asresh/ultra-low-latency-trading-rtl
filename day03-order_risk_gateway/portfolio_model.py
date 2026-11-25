"""Transaction-level reference for the opt-in aggregate risk interface."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from enum import IntEnum

from risk_model import Decision, GatewayModel, Key, Operation, Policy, Profile, Request, Status


PORTFOLIO_LIMIT = 22


class Denial(IntEnum):
    NONE = 0
    ACCOUNT_GROSS = 1
    ACCOUNT_COUNT = 2
    ACCOUNT_USAGE = 3
    FIRM_GROSS = 4
    FIRM_COUNT = 5
    FIRM_USAGE = 6


class ControlOp(IntEnum):
    SET = 0
    READ = 1
    ROLL = 2


class ControlStatus(IntEnum):
    OK = 0
    BAD_SCOPE = 1
    BAD_COUNT = 2
    STALE_EPOCH = 3
    BAD_OP = 4


@dataclass(frozen=True)
class Caps:
    """Use None for an inactive check, including an inactive zero limit."""

    gross: int | None = None
    count: int | None = None
    usage: int | None = None

    @classmethod
    def read_fields(cls, mask: int, gross: int, count: int, usage: int) -> Caps:
        values = (gross, count, usage)
        return cls(*(value if mask & (1 << bit) else None for bit, value in enumerate(values)))

    def wire_fields(self) -> tuple[int, int, int, int]:
        values = (self.gross, self.count, self.usage)
        return (
            sum(1 << bit for bit, value in enumerate(values) if value is not None),
            *(value if value is not None else 0 for value in values),
        )

    def exceeded(self, view: Exposure) -> int:
        """Return a one-based cap selector with contract priority."""
        for reason, value, limit in (
            (1, view.gross, self.gross),
            (2, view.orders, self.count),
            (3, view.gross + view.turnover, self.usage),
        ):
            if limit is not None and value > limit:
                return reason
        return 0


@dataclass(frozen=True)
class Exposure:
    gross: int = 0
    turnover: int = 0
    orders: int = 0

    def changed(self, gross_delta: int, count_delta: int) -> Exposure:
        return replace(self, gross=self.gross + gross_delta, orders=self.orders + count_delta)


@dataclass(frozen=True)
class Control:
    op: int
    firm: int = 0
    account: int = 0
    mask: int = 0
    gross_limit: int = 0
    count_limit: int = 0
    usage_limit: int = 0
    epoch: int = 0


@dataclass(frozen=True)
class ControlReply:
    status: ControlStatus
    caps: Caps
    exposure: Exposure
    epoch: int


@dataclass(frozen=True)
class Receipt:
    decision: Decision
    reason: Denial
    account: Exposure
    firm: Exposure
    epoch: int


class PortfolioModel:
    """Scan the native model's order objects instead of using an incremental mirror."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.gateway = GatewayModel(profile)
        self.reset()

    def reset(self) -> None:
        self.gateway.reset()
        self.caps: dict[int | None, Caps] = {}
        self.epoch_turnover: dict[int, int] = defaultdict(int)
        self.epoch = 0

    @property
    def halted(self) -> bool:
        return self.gateway.halted

    def halt(self) -> None:
        self.gateway.halted = True

    def write_native_policy(self, key: Key, policy: Policy) -> None:
        self.gateway.write_policy(key, policy)

    def exposure(self, account: int | None) -> Exposure:
        if account is not None and not 0 <= account < self.profile.accounts:
            return Exposure()
        reserved = [
            order.reserved
            for order in self.gateway.orders.values()
            if order.live and (account is None or order.key.account == account)
        ]
        recent = sum(self.epoch_turnover.values()) if account is None else self.epoch_turnover[account]
        return Exposure(sum(reserved), recent, len(reserved))

    def control(self, command: Control) -> ControlReply:
        scope_ok = command.account == 0 if command.firm else 0 <= command.account < self.profile.accounts
        scope = None if command.firm else command.account
        limits = Caps.read_fields(command.mask, command.gross_limit, command.count_limit, command.usage_limit)
        status = ControlStatus.OK
        if command.op == ControlOp.SET:
            if not scope_ok:
                status = ControlStatus.BAD_SCOPE
            elif limits.count is not None and limits.count > self.profile.slots:
                status = ControlStatus.BAD_COUNT
            else:
                self.caps[scope] = limits
        elif command.op == ControlOp.READ:
            if not scope_ok:
                status = ControlStatus.BAD_SCOPE
        elif command.op == ControlOp.ROLL:
            if not command.firm or not scope_ok:
                status = ControlStatus.BAD_SCOPE
            elif command.epoch <= self.epoch:
                status = ControlStatus.STALE_EPOCH
            else:
                self.epoch = command.epoch
                self.epoch_turnover.clear()
        else:
            status = ControlStatus.BAD_OP
        caps, exposure = (self.caps.get(scope, Caps()), self.exposure(scope)) if scope_ok else (Caps(), Exposure())
        return ControlReply(status, caps, exposure, self.epoch)

    def expansion(self, req: Request) -> tuple[int, int]:
        if self.halted or not self.gateway.valid_key(req.key) or not 0 <= req.slot < self.profile.slots:
            return 0, 0
        old = self.gateway.orders.get(req.slot)
        price_qty = req.price * req.qty
        if req.op == Operation.NEW and req.is_well_formed(Operation.NEW):
            if old is None or (not old.live and old.generation < self.profile.last_generation):
                return price_qty, 1
        if req.op == Operation.AMEND and req.is_well_formed(Operation.AMEND):
            if (old and old.live and req.key == old.key and req.generation == old.generation
                    and req.event == old.event + 1 and req.event < self.profile.last_event):
                return max(0, price_qty - old.reserved), 0
        return 0, 0

    def check(self, req: Request) -> Denial:
        gross_delta, count_delta = self.expansion(req)
        if not gross_delta:
            return Denial.NONE
        for scope, base in ((req.account, 0), (None, 3)):
            candidate = self.exposure(scope).changed(gross_delta, count_delta)
            reason = self.caps.get(scope, Caps()).exceeded(candidate)
            if reason:
                return Denial(base + reason)
        return Denial.NONE

    def transact(self, req: Request) -> Receipt:
        reason = self.check(req)
        if reason:
            snapshot = self.gateway.transact(replace(req, op=Operation.SNAPSHOT))
            decision = replace(snapshot, op=req.op, code=PORTFOLIO_LIMIT)
        else:
            prior_execution = self.gateway.executed.get(req.key, 0)
            decision = self.gateway.transact(req)
            if req.op == Operation.FILL and decision.code == Status.OK:
                self.epoch_turnover[req.account] += decision.executed_notional - prior_execution
        return Receipt(decision, reason, self.exposure(req.account), self.exposure(None), self.epoch)
