"""Independent transaction-level reference for the supervised venue boundary."""

from __future__ import annotations

from collections import deque
from dataclasses import astuple, dataclass, field, replace
from enum import IntEnum

from portfolio_model import Denial, PortfolioModel, Receipt
from risk_model import Key, Operation, Profile, Request, Status, encode_reply


class LeaseOp(IntEnum):
    ARM = 0
    FENCE = 1
    READ = 2


class LeaseStatus(IntEnum):
    OK = 0
    BAD_VENUE = 1
    BAD_OP = 2
    BAD_FIELDS = 3
    BAD_EPOCH = 4
    LINK_DOWN = 5


@dataclass(frozen=True)
class LeaseCommand:
    op: int
    venue: int
    epoch: int = 0
    timeout: int = 0


@dataclass
class Lease:
    epoch: int = 0
    remaining: int = 0
    period: int = 0


class Leases:
    def __init__(self, count: int):
        self.rows = [Lease() for _ in range(count)]
        self.losses = 0
        self.ignored = 0
        self.expirations = 0

    def active(self, venue: int) -> bool:
        return 0 <= venue < len(self.rows) and self.rows[venue].remaining > 0

    def epoch(self, venue: int) -> int:
        return self.rows[venue].epoch

    def drop(self, venue: int) -> None:
        self.rows[venue].remaining = 0

    def set_losses(self, bitmap: int) -> None:
        self.losses = bitmap
        for venue in range(len(self.rows)):
            if bitmap & (1 << venue):
                self.drop(venue)

    def pulse(self, ticks: int, heartbeat: tuple[int, int] | None = None) -> None:
        matched = None
        if heartbeat is not None:
            venue, epoch = heartbeat
            if self.active(venue) and self.epoch(venue) == epoch:
                self.rows[venue].remaining = self.rows[venue].period
                matched = venue
            else:
                self.ignored = min(self.ignored + 1, 0xFFFFFFFF)
        for _ in range(ticks):
            for venue, state in enumerate(self.rows):
                if state.remaining and venue != matched:
                    state.remaining -= 1
                    if not state.remaining:
                        self.expirations = min(self.expirations + 1, 0xFFFFFFFF)
            matched = None

    def command(self, request: LeaseCommand) -> tuple[int, ...]:
        op, venue, epoch, limit = astuple(request)
        if not 0 <= venue < len(self.rows):
            return LeaseStatus.BAD_VENUE, op, venue, 0, 0, 0
        state = self.rows[venue]
        status = LeaseStatus.OK
        if op not in LeaseOp._value2member_map_:
            status = LeaseStatus.BAD_OP
        elif (op == LeaseOp.ARM) != bool(limit) or (op == LeaseOp.READ and epoch):
            status = LeaseStatus.BAD_FIELDS
        elif (op == LeaseOp.ARM and epoch <= state.epoch) or (op == LeaseOp.FENCE and epoch != state.epoch):
            status = LeaseStatus.BAD_EPOCH
        elif op == LeaseOp.ARM and self.losses & (1 << venue):
            status = LeaseStatus.LINK_DOWN
        elif op == LeaseOp.ARM:
            self.rows[venue] = state = Lease(epoch, limit, limit)
        elif op == LeaseOp.FENCE:
            self.drop(venue)
        return status, op, venue, state.epoch, state.remaining, bool(state.remaining)


class Result(IntEnum):
    APPLIED = 0
    RISK_REFUSED = 1
    LEASE_CLOSED = 2
    BAD_ADDRESS = 3
    BAD_FIELDS = 4
    WRONG_ORDER = 5
    UNSENT = 6
    REPLAY = 7
    OLD_REPORT = 8
    REPORT_GAP = 9
    CHANGED_REPORT = 10
    REUSED_MATCH = 11
    WRONG_PHASE = 12
    CANCEL_QUEUED = 13
    ENTRY_ACKED = 14
    CANCEL_REFUSED = 15
    ROLLED_BACK = 16
    LAST_SEQUENCE = 17
    WRONG_CANCEL_ACTION = 18
    ACTION_EXHAUSTED = 19


class Source(IntEnum):
    STRATEGY = 0
    VENUE = 1
    RECOVERY = 2


class Event(IntEnum):
    ACK = 0
    FILL = 1
    CANCELED = 2
    REJECT = 3
    CANCEL_REJECT = 4
    EXPIRED = 5


class Cancel(IntEnum):
    NONE = 0
    QUEUED = 1
    SENT = 2


@dataclass(frozen=True)
class Intent:
    cancel: int
    tag: int
    venue: int
    epoch: int
    account: int
    instrument: int
    sell: int
    slot: int
    generation: int = 0
    price: int = 0
    quantity: int = 0

    @property
    def key(self) -> Key:
        return Key(self.account, self.instrument, self.sell)


@dataclass(frozen=True)
class Report:
    event: int
    tag: int
    venue: int
    epoch: int
    slot: int
    generation: int
    sequence: int
    price: int = 0
    quantity: int = 0
    match: int = 0
    action: int = 0

    @property
    def evidence(self) -> tuple[int, ...]:
        return self.sequence, self.event, self.price, self.quantity, self.action, self.match


@dataclass
class Route:
    intent: Intent
    generation: int
    live: bool = True
    submitted: bool = False
    acked: bool = False
    stop: bool = False
    unwind_failed: bool = False
    cancel: Cancel = Cancel.NONE
    cancel_action: int = 0
    sequence: int = 0
    native_sequence: int = 0
    leaves: int = 0
    evidence: deque = field(default_factory=deque)


@dataclass(frozen=True)
class Offer:
    cancel: bool
    order_tag: int
    venue: int
    original_epoch: int
    network_epoch: int
    account: int
    instrument: int
    sell: int
    slot: int
    generation: int
    action: int
    price: int
    quantity: int


@dataclass(frozen=True)
class Audit:
    source: Source
    status: Result
    tag: int
    venue: int
    epoch: int
    slot: int
    generation: int
    sequence: int
    native: Receipt | None = None

    def pins(self, profile: Profile) -> tuple[int, ...]:
        fields = astuple(self)[:-1]
        if self.native is None:
            return (*fields, *([0] * 10))
        native = self.native
        return (*fields, 1, encode_reply(profile, native.decision), native.reason,
                *astuple(native.account), *astuple(native.firm), native.epoch)


class Handoff:
    """Maintain logical routes and derive risk with the established portfolio oracle."""

    def __init__(self, profile: Profile, venues: int, report_width: int, history: int, action_width: int = 3):
        self.profile = profile
        self.venue_count = venues
        self.last_report = (1 << report_width) - 1
        self.history = history
        self.last_action = (1 << action_width) - 1
        self.reset()

    def reset(self) -> None:
        self.risk = PortfolioModel(self.profile)
        self.leases = Leases(self.venue_count)
        self.orders: dict[int, Route] = {}
        self.offered: Offer | None = None
        self.next_egress = 0
        self.next_unwind = 0
        self.handoff_fault = False
        self.corrupt_read = False

    def strategy(self, intent: Intent) -> Audit:
        audit = Audit(Source.STRATEGY, Result.APPLIED, intent.tag, intent.venue, intent.epoch,
                      intent.slot, intent.generation, 0)
        if (not self.risk.gateway.valid_key(intent.key)
                or not 0 <= intent.venue < self.venue_count or not 0 <= intent.slot < self.profile.slots):
            return self._status(audit, Result.BAD_ADDRESS)
        slot = self.orders.get(intent.slot)
        if intent.cancel:
            if intent.price or intent.quantity:
                return self._status(audit, Result.BAD_FIELDS)
            if not (slot and slot.live and slot.intent.key == intent.key and self._owns(
                    slot, intent.venue, intent.epoch, intent.generation)):
                return self._status(audit, Result.WRONG_ORDER)
            if slot.stop or slot.cancel != Cancel.NONE:
                return self._status(audit, Result.WRONG_PHASE)
            if slot.submitted:
                if slot.cancel_action == self.last_action:
                    return self._status(audit, Result.ACTION_EXHAUSTED)
                slot.cancel = Cancel.QUEUED
            else:
                slot.stop = True
            return self._status(audit, Result.CANCEL_QUEUED)
        if intent.generation or not intent.price or not intent.quantity:
            return self._status(audit, Result.BAD_FIELDS)
        if not self._original_lease(intent) or self.risk.halted:
            return self._status(audit, Result.LEASE_CLOSED)

        request = Request(Operation.NEW, intent.tag, *intent.key, intent.slot,
                          0, 0, intent.price, intent.quantity)
        receipt = self.risk.transact(request)
        if receipt.decision.code != Status.OK:
            return self._status(audit, Result.RISK_REFUSED, native=receipt)
        generation = receipt.decision.generation
        self.orders[intent.slot] = Route(intent, generation, leaves=receipt.decision.leaves,
                                         evidence=deque(maxlen=self.history))
        return self._status(audit, Result.APPLIED, native=receipt, generation=generation)

    @staticmethod
    def _owns(slot: Route, venue: int, epoch: int, generation: int) -> bool:
        return (venue, epoch, generation) == (slot.intent.venue, slot.intent.epoch, slot.generation)

    @staticmethod
    def _status(
        audit: Audit, status: Result, native: Receipt | None = None, generation: int | None = None,
    ) -> Audit:
        return Audit(audit.source, status, audit.tag, audit.venue, audit.epoch, audit.slot,
                     audit.generation if generation is None else generation, audit.sequence, native)

    def _original_lease(self, intent: Intent) -> bool:
        return self.leases.active(intent.venue) and self.leases.epoch(intent.venue) == intent.epoch

    def _report_gate(self, message: Report, route: Route | None) -> Result:
        if not 0 <= message.venue < self.venue_count or not 0 <= message.slot < self.profile.slots:
            return Result.BAD_ADDRESS
        shape = ((message.price and message.quantity and message.match) if message.event == Event.FILL
                 else not (message.price or message.quantity or message.match))
        cancel = message.event in (Event.CANCELED, Event.CANCEL_REJECT)
        if (message.event not in Event._value2member_map_ or not message.sequence
                or not shape or cancel != bool(message.action)):
            return Result.BAD_FIELDS
        if not route or not self._owns(route, message.venue, message.epoch, message.generation):
            return Result.WRONG_ORDER
        if not route.submitted:
            return Result.UNSENT
        retained = {entry[0]: entry for entry in route.evidence}
        if message.sequence in retained:
            return Result.REPLAY if message.evidence == retained[message.sequence] else Result.CHANGED_REPORT
        if message.sequence <= route.sequence:
            return Result.OLD_REPORT
        if not route.live:
            return Result.WRONG_PHASE
        if message.sequence != route.sequence + 1:
            return Result.REPORT_GAP
        if message.event == Event.FILL and any(entry[-1] == message.match for entry in route.evidence):
            return Result.REUSED_MATCH

        closes = (message.event in (Event.EXPIRED, Event.CANCELED, Event.REJECT)
                  or (message.event == Event.FILL and message.quantity == route.leaves))
        if message.sequence == self.last_report and not closes:
            return Result.LAST_SEQUENCE
        if message.event in (Event.ACK, Event.REJECT) and route.acked:
            return Result.WRONG_PHASE
        if cancel:
            if route.cancel != Cancel.SENT:
                return Result.WRONG_PHASE
            if message.action != route.cancel_action:
                return Result.WRONG_CANCEL_ACTION
        return Result.APPLIED

    def report(self, message: Report) -> Audit:
        audit = Audit(Source.VENUE, Result.APPLIED, message.tag, message.venue, message.epoch,
                      message.slot, message.generation, message.sequence)
        route = self.orders.get(message.slot)
        status = self._report_gate(message, route)
        if status is not Result.APPLIED:
            if status in (Result.REPORT_GAP, Result.CHANGED_REPORT, Result.REUSED_MATCH, Result.LAST_SEQUENCE):
                self.leases.drop(message.venue)
            return self._status(audit, status)
        assert route is not None
        if message.event == Event.ACK:
            route.acked = True
            result = self._status(audit, Result.ENTRY_ACKED)
        elif message.event == Event.CANCEL_REJECT:
            route.cancel = Cancel.NONE
            result = self._status(audit, Result.CANCEL_REFUSED)
        else:
            native_op = {Event.FILL: Operation.FILL, Event.REJECT: Operation.REJECT}.get(
                message.event, Operation.CANCEL,
            )
            receipt = self.risk.transact(Request(
                native_op, message.tag, *route.intent.key, message.slot, route.generation,
                route.native_sequence + 1, message.price, message.quantity,
            ))
            if receipt.decision.code != Status.OK:
                self.leases.drop(message.venue)
                return self._status(audit, Result.RISK_REFUSED, native=receipt)
            self._apply_receipt(route, receipt)
            result = self._status(audit, Result.APPLIED, native=receipt)

        route.sequence = message.sequence
        route.evidence.append(message.evidence)
        return result

    @staticmethod
    def _apply_receipt(route: Route, receipt: Receipt) -> None:
        route.native_sequence = receipt.decision.event
        route.live = receipt.decision.live
        route.leaves = receipt.decision.leaves
        if not route.live:
            route.cancel = Cancel.NONE

    def select_egress(self) -> Offer | None:
        """Collect one eligible offer in the scheduler's acknowledged scan order."""
        if self.offered:
            return self.offered
        candidates = []
        for slot, route in self.orders.items():
            order = route.intent
            if not route.live or not self.leases.active(order.venue):
                continue
            if route.submitted and route.cancel == Cancel.QUEUED:
                candidates.append((0, slot, route))
            elif not route.submitted and not route.stop and self._original_lease(order) and not self.risk.halted:
                candidates.append((1, slot, route))
        if not candidates:
            return None
        kind, slot, route = min(candidates, key=lambda row: (
            row[0], (row[1] - self.next_egress) % self.profile.slots,
        ))
        order = route.intent
        cancel = kind == 0
        self.next_egress = (slot + 1) % self.profile.slots
        self.offered = Offer(cancel, order.tag, order.venue, order.epoch, self.leases.epoch(order.venue),
                             *order.key, slot, route.generation, route.cancel_action+1 if cancel else 0,
                             0 if cancel else order.price, 0 if cancel else route.leaves)
        return self.offered

    def applicable(self, offer: Offer) -> bool:
        row = self.orders[offer.slot]
        if (not row.live or row.generation != offer.generation or not self.leases.active(offer.venue)
                or self.leases.epoch(offer.venue) != offer.network_epoch):
            return False
        if offer.cancel:
            return row.submitted and row.cancel == Cancel.QUEUED and offer.action == row.cancel_action+1
        return not row.submitted and not row.stop and not self.risk.halted

    def take_egress(self) -> tuple[int, ...]:
        assert self.offered is not None
        offer = self.offered
        self.offered = None
        valid = self.applicable(offer)
        if valid:
            route = self.orders[offer.slot]
            if offer.cancel:
                route.cancel = Cancel.SENT
                route.cancel_action = offer.action
            else:
                route.submitted = True
        return (valid, *astuple(offer))

    def unwind(self) -> list[Audit]:
        """Compensate proven-unsent reservations and preserve submitted exposure."""
        discarded = [(slot, route) for slot, route in self.orders.items()
                     if route.live and not route.submitted and not route.unwind_failed and
                     (route.stop or self.risk.halted or not self._original_lease(route.intent))]
        discarded.sort(key=lambda row: (row[0] - self.next_unwind) % self.profile.slots)
        outcomes = []
        for slot, route in discarded:
            order = route.intent
            request = Request(Operation.CANCEL, order.tag, *order.key,
                              slot, route.generation, route.native_sequence + 1, 0, 0)
            if self.corrupt_read:
                snapshot = self.risk.transact(replace(request, op=Operation.SNAPSHOT))
                decision = replace(snapshot.decision, op=Operation.CANCEL, code=Status.LEDGER_INVARIANT,
                                   open_notional=0, executed_notional=0, open_orders=0)
                receipt = Receipt(decision, Denial.NONE, snapshot.account, snapshot.firm, snapshot.epoch)
                route.unwind_failed = True
                self.handoff_fault = True
                self.risk.halt()
                status = Result.RISK_REFUSED
            else:
                receipt = self.risk.transact(request)
                if receipt.decision.code != Status.OK:
                    raise AssertionError("logical gateway refused unwind of a valid unsent reservation")
                self._apply_receipt(route, receipt)
                status = Result.ROLLED_BACK
            self.next_unwind = (slot + 1) % self.profile.slots
            outcomes.append(Audit(Source.RECOVERY, status, order.tag, order.venue,
                                  order.epoch, slot, route.generation, 0, receipt))
        return outcomes
