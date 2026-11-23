"""Executable behavioral specification for the risk transaction interface.

The model derives open exposure by summing the live order objects on each
query. The RTL instead maintains a bounded counter ledger incrementally.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import astuple, dataclass, fields, replace
from enum import IntEnum
from pathlib import Path
from typing import NamedTuple


class Operation(IntEnum):
    NEW = 0
    FILL = 1
    CANCEL = 2
    REJECT = 3
    AMEND = 4
    SNAPSHOT = 5


class Status(IntEnum):
    OK = 0
    BAD_OP = 1
    BAD_KEY = 2
    BAD_SLOT = 3
    BAD_FIELDS = 4
    SLOT_BUSY = 5
    GENERATION_EXHAUSTED = 6
    NOT_LIVE = 7
    WRONG_OWNER = 8
    STALE_GENERATION = 9
    EVENT_SEQUENCE = 10
    DISABLED = 11
    QTY_LIMIT = 12
    PRICE_LIMIT = 13
    OPEN_NOTIONAL = 14
    OPEN_COUNT = 15
    OVERFILL = 16
    REJECT_AFTER_FILL = 17
    EXEC_OVERFLOW = 18
    HALTED = 19
    LEDGER_INVARIANT = 20
    EVENT_EXHAUSTED = 21


class Action(IntEnum):
    CONFIG = 0
    ORDER = 1
    HALT = 2
    RESET = 3
    DRAIN = 4


class Key(NamedTuple):
    account: int
    instrument: int
    sell: int


def bits_for(n: int) -> int:
    return max(1, (n - 1).bit_length())


@dataclass(frozen=True)
class Profile:
    name: str
    accounts: int
    instruments: int
    slots: int
    price_bits: int
    qty_bits: int
    generation_bits: int
    event_bits: int
    tag_bits: int = 16

    @property
    def notional_bits(self) -> int:
        return self.price_bits + self.qty_bits

    @property
    def max_notional(self) -> int:
        return (1 << self.notional_bits) - 1

    @property
    def max_price(self) -> int:
        return (1 << self.price_bits) - 1

    @property
    def max_qty(self) -> int:
        return (1 << self.qty_bits) - 1

    @property
    def last_generation(self) -> int:
        return (1 << self.generation_bits) - 1

    @property
    def last_event(self) -> int:
        return (1 << self.event_bits) - 1

    @property
    def response_widths(self) -> tuple[int, ...]:
        return (
            3, self.tag_bits, bits_for(self.accounts), bits_for(self.instruments),
            1, bits_for(self.slots), self.generation_bits, self.event_bits, 5, 1,
            self.qty_bits, self.price_bits, self.notional_bits, self.notional_bits,
            self.notional_bits, bits_for(self.slots + 1),
        )

    @property
    def response_bits(self) -> int:
        return sum(self.response_widths)


PROFILES = {
    "narrow": Profile("narrow", 3, 3, 5, 8, 8, 3, 4),
    "wide": Profile("wide", 3, 3, 5, 24, 16, 3, 4),
}


@dataclass(frozen=True)
class Policy:
    enable: int
    min_price: int
    max_price: int
    max_order_qty: int
    max_open_notional: int
    max_open_orders: int

    def is_active(self, max_slots: int) -> bool:
        return (
            bool(self.enable)
            and 0 < self.min_price <= self.max_price
            and self.max_order_qty > 0
            and self.max_open_notional > 0
            and 0 < self.max_open_orders <= max_slots
        )


@dataclass(frozen=True)
class Request:
    op: int
    tag: int
    account: int
    instrument: int
    sell: int
    slot: int
    generation: int
    event: int
    price: int
    qty: int

    @property
    def key(self) -> Key:
        return Key(self.account, self.instrument, self.sell)

    def is_well_formed(self, op: Operation) -> bool:
        if op is Operation.NEW:
            return self.generation == self.event == 0 and self.price > 0 and self.qty > 0
        if op in (Operation.FILL, Operation.AMEND):
            return self.price > 0 and self.qty > 0
        if op in (Operation.CANCEL, Operation.REJECT):
            return self.price == self.qty == 0
        return True


@dataclass(frozen=True)
class OpenOrder:
    key: Key
    generation: int
    event: int
    price: int
    qty: int
    ever_filled: bool = False

    @property
    def reserved(self) -> int:
        return self.price * self.qty

    @property
    def live(self) -> bool:
        return self.qty > 0


@dataclass(frozen=True)
class Decision:
    op: int
    tag: int
    account: int
    instrument: int
    sell: int
    slot: int
    generation: int
    event: int
    code: int
    live: int
    leaves: int
    price: int
    reserved: int
    open_notional: int
    executed_notional: int
    open_orders: int

    @classmethod
    def from_wire_fields(cls, values: list[int]) -> Decision:
        if len(values) != len(fields(cls)):
            raise ValueError(f"expected {len(fields(cls))} decision fields, got {len(values)}")
        return cls(*values)


class GatewayModel:
    """Replay old generations safely and maintain session realized notional."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.reset()

    def reset(self) -> None:
        self.orders: dict[int, OpenOrder] = {}
        self.policies: dict[Key, Policy] = {}
        self.executed: dict[Key, int] = {}
        self.halted = False

    def valid_key(self, key: Key) -> bool:
        return (
            0 <= key.account < self.profile.accounts
            and 0 <= key.instrument < self.profile.instruments
            and key.sell in (0, 1)
        )

    def write_policy(self, key: Key, policy: Policy) -> None:
        if self.valid_key(key):
            if policy.is_active(self.profile.slots):
                self.policies[key] = policy
            else:
                self.policies.pop(key, None)

    def open_orders(self, key: Key) -> list[OpenOrder]:
        return [
            order
            for order in self.orders.values()
            if order.key == key and order.live
        ]

    def ledger(self, key: Key) -> tuple[int, int, int]:
        orders = self.open_orders(key)
        open_amount = sum(order.reserved for order in orders)
        return open_amount, self.executed.get(key, 0), len(orders)

    def check_admission(self, req: Request, delta_open: int, delta_count: int) -> Status:
        if self.halted:
            return Status.HALTED
        policy = self.policies.get(req.key)
        if policy is None:
            return Status.DISABLED
        if req.qty > policy.max_order_qty:
            return Status.QTY_LIMIT
        if not policy.min_price <= req.price <= policy.max_price:
            return Status.PRICE_LIMIT
        amount, _, count = self.ledger(req.key)
        if amount + delta_open > policy.max_open_notional:
            return Status.OPEN_NOTIONAL
        if count + delta_count > policy.max_open_orders:
            return Status.OPEN_COUNT
        return Status.OK

    def _new(self, req: Request, old: OpenOrder | None) -> Status:
        if old is not None and old.live:
            return Status.SLOT_BUSY
        previous_generation = old.generation if old is not None else 0
        if previous_generation == self.profile.last_generation:
            return Status.GENERATION_EXHAUSTED
        candidate = OpenOrder(req.key, previous_generation + 1, 0, req.price, req.qty)
        code = self.check_admission(req, candidate.reserved, 1)
        if code is Status.OK:
            self.orders[req.slot] = candidate
        return code

    def _fill(self, req: Request, prior: OpenOrder) -> Status:
        if req.qty > prior.qty:
            return Status.OVERFILL
        new_total = self.executed.get(req.key, 0) + req.price * req.qty
        if new_total > self.profile.max_notional:
            self.halted = True
            return Status.EXEC_OVERFLOW
        if req.event == self.profile.last_event and req.qty < prior.qty:
            return Status.EVENT_EXHAUSTED
        self.orders[req.slot] = replace(
            prior, event=req.event, qty=prior.qty - req.qty, ever_filled=True
        )
        self.executed[req.key] = new_total
        return Status.OK

    def _reconcile(self, op: Operation, req: Request, prior: OpenOrder | None) -> Status:
        if prior is None or not prior.live:
            return Status.NOT_LIVE
        if prior.key != req.key:
            return Status.WRONG_OWNER
        if req.generation != prior.generation:
            return Status.STALE_GENERATION
        if prior.event == self.profile.last_event:
            return Status.EVENT_EXHAUSTED
        if req.event != prior.event + 1:
            return Status.EVENT_SEQUENCE
        if op is Operation.FILL:
            return self._fill(req, prior)
        if op is Operation.REJECT and prior.ever_filled:
            return Status.REJECT_AFTER_FILL
        if op in (Operation.CANCEL, Operation.REJECT):
            self.orders[req.slot] = replace(prior, event=req.event, qty=0)
            return Status.OK
        if req.event == self.profile.last_event:
            return Status.EVENT_EXHAUSTED
        candidate = replace(prior, event=req.event, price=req.price, qty=req.qty)
        code = self.check_admission(req, candidate.reserved - prior.reserved, 0)
        if code is Status.OK:
            self.orders[req.slot] = candidate
        return code

    def _commit_request(self, req: Request) -> Status:
        try:
            op = Operation(req.op)
        except ValueError:
            return Status.BAD_OP
        if not self.valid_key(req.key):
            return Status.BAD_KEY
        if not 0 <= req.slot < self.profile.slots:
            return Status.BAD_SLOT
        if not req.is_well_formed(op):
            return Status.BAD_FIELDS
        old = self.orders.get(req.slot)
        if op is Operation.NEW:
            return self._new(req, old)
        if op is Operation.SNAPSHOT:
            return Status.OK
        return self._reconcile(op, req, old)

    def transact(self, req: Request) -> Decision:
        code = self._commit_request(req)
        visible = self.orders.get(req.slot)
        if visible is not None and visible.key != req.key:
            visible = None
        amount, executed, count = self.ledger(req.key)
        return Decision(
            req.op, req.tag, req.account, req.instrument, req.sell, req.slot,
            visible.generation if visible else 0,
            visible.event if visible else 0,
            code,
            int(visible.live) if visible else 0,
            visible.qty if visible else 0,
            visible.price if visible else 0,
            visible.reserved if visible else 0,
            amount, executed, count,
        )


def checked_row(values: tuple[int, ...], widths: tuple[int, ...], label: str) -> None:
    if len(values) != len(widths):
        raise ValueError(f"{label} has {len(values)} fields, expected {len(widths)}")
    for offset, (value, width) in enumerate(zip(values, widths)):
        if not 0 <= value < 1 << width:
            raise ValueError(f"{label} field {offset}: {value} does not fit {width} bits")


def encode_reply(profile: Profile, decision: Decision) -> int:
    values = astuple(decision)
    checked_row(values, profile.response_widths, f"decision #{decision.tag}")
    packed = 0
    for value, width in zip(values, profile.response_widths):
        packed = (packed << width) | value
    return packed


def decode_reply(profile: Profile, packed: int) -> Decision:
    if not 0 <= packed < 1 << profile.response_bits:
        raise ValueError("audit payload exceeds decision width")
    values = []
    for width in reversed(profile.response_widths):
        values.append(packed & ((1 << width) - 1))
        packed >>= width
    return Decision.from_wire_fields(values[::-1])


class Scenario:
    """Record checked vector rows and the expected decisions in issue order."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.model = GatewayModel(profile)
        self.stimulus: list[tuple[int, ...]] = []
        self.expected: list[Decision] = []
        self.codes: Counter[Status] = Counter()
        self.operations: Counter[Operation] = Counter()
        self.tag = 0

    def _event(self, kind: Action, *payload: int) -> None:
        if len(payload) > 10:
            raise ValueError("stimulus payload exceeds the fixed vector row")
        self.stimulus.append((kind, *payload, *([0] * (10 - len(payload)))))

    def configure(self, key: Key, policy: Policy) -> None:
        prof = self.profile
        payload = (*key, *astuple(policy))
        checked_row(
            payload,
            (bits_for(prof.accounts), bits_for(prof.instruments), 1, 1,
             prof.price_bits, prof.price_bits, prof.qty_bits, prof.notional_bits,
             bits_for(prof.slots + 1)),
            "configuration",
        )
        self.model.write_policy(key, policy)
        self._event(Action.CONFIG, *payload)

    def halt(self) -> None:
        self.model.halted = True
        self._event(Action.HALT)

    def reset(self) -> None:
        self.model.reset()
        self._event(Action.RESET)

    def drain(self) -> None:
        self._event(Action.DRAIN)

    def order(
        self, op: int, key: Key, slot: int, generation: int = 0,
        event: int = 0, price: int = 0, qty: int = 0,
        expected: Status | None = None,
    ) -> Decision:
        p = self.profile
        req = Request(op, self.tag, *key, slot, generation, event, price, qty)
        self.tag += 1
        checked_row(
            astuple(req),
            (3, p.tag_bits, bits_for(p.accounts), bits_for(p.instruments), 1,
             bits_for(p.slots), p.generation_bits, p.event_bits, p.price_bits, p.qty_bits),
            f"request #{req.tag}",
        )
        self._event(Action.ORDER, *astuple(req))
        reply = self.model.transact(req)
        if expected is not None and reply.code != expected:
            raise AssertionError(f"{req}: expected {expected.name}, model got {Status(reply.code).name}")
        self.expected.append(reply)
        self.codes[Status(reply.code)] += 1
        if op in Operation._value2member_map_:
            self.operations[Operation(op)] += 1
        return reply

    def write(self, output: Path) -> None:
        output.mkdir(parents=True, exist_ok=True)
        with (output / "stimulus.txt").open("w", encoding="ascii") as vectors:
            for row in self.stimulus:
                print(*(int(value) for value in row), file=vectors)
        with (output / "expected.txt").open("w", encoding="ascii") as replies:
            for decision in self.expected:
                print(*(int(value) for value in astuple(decision)), file=replies)
