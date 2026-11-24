"""Executable cycle contract for a credit-aware egress scheduler."""

from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Deque, Dict, Optional, Tuple


class Op(IntEnum):
    """Commands supported by the management port."""

    CONFIGURE = 0
    REFUND = 1
    INSPECT = 2
    RESERVED = 3


class Status(IntEnum):
    """Outcomes returned on the management response port."""

    OK = 0
    BAD_CLASS = 1
    BAD_FIELDS = 2
    BUSY = 3
    OVER_REFUND = 4


@dataclass(frozen=True)
class Geometry:
    """Set geometry and pin widths for one independently compiled design."""

    classes: int
    depth: int
    credit_bits: int

    @property
    def max_credit(self) -> int:
        """Return the largest credit amount representable on the ports."""
        return (1 << self.credit_bits) - 1

    @property
    def max_class_code(self) -> int:
        """Return the upper bound of the encoding, including unused values."""
        return (1 << max(1, (self.classes - 1).bit_length())) - 1


PROFILES = {
    "default": Geometry(classes=3, depth=4, credit_bits=8),
    "singleton": Geometry(classes=1, depth=1, credit_bits=3),
    "odd": Geometry(classes=5, depth=3, credit_bits=6),
}


@dataclass(frozen=True)
class Order:
    """An opaque one-beat order and the credits its transmission will spend."""

    key: int
    cost: int
    tag: int
    payload: int

    def fields(self) -> Tuple[int, ...]:
        """Return the wire fields in the order used by the checked bench."""
        return self.key, self.cost, self.tag, self.payload


@dataclass(frozen=True)
class Control:
    """A configuration, refund, or post-edge state request."""

    op: Op
    key: int
    amount: int = 0
    weight: int = 0

    def fields(self) -> Tuple[int, ...]:
        """Return the numerical command and its operand fields."""
        return int(self.op), self.key, self.amount, self.weight


@dataclass(frozen=True)
class Report:
    """A command outcome and a snapshot after all actions on its acceptance edge."""

    op: Op
    key: int
    status: Status
    capacity: int = 0
    available: int = 0
    inflight: int = 0
    queued: int = 0
    weight: int = 0

    def fields(self) -> Tuple[int, ...]:
        """Serialize the status and sampled ledger together."""
        return (int(self.op), self.key, int(self.status), self.capacity,
                self.available, self.inflight, self.queued, self.weight)


@dataclass(frozen=True)
class Drive:
    """Inputs stable ahead of one rising edge."""

    order: Optional[Order] = None
    control: Optional[Control] = None
    sink_ready: bool = False
    report_ready: bool = True
    reset: bool = False

    def fields(self) -> Tuple[int, ...]:
        """Store command fields and both consumer readiness bits."""
        order_fields = self.order.fields() if self.order is not None else (0, 0, 0, 0)
        ctl_fields = self.control.fields() if self.control is not None else (0, 0, 0, 0)
        return (int(self.reset), int(self.order is not None), *order_fields,
                int(self.sink_ready), int(self.control is not None), *ctl_fields,
                int(self.report_ready))


@dataclass(frozen=True)
class Pins:
    """Combinational outputs observed before the modeled rising edge."""

    order_ready: bool = False
    order_rejected: bool = False
    control_ready: bool = False
    offer: Optional[Order] = None
    report: Optional[Report] = None

    def fields(self) -> Tuple[int, ...]:
        """Include only meaningful payload on valid channels."""
        offer_fields = self.offer.fields() if self.offer is not None else (0, 0, 0, 0)
        report_fields = self.report.fields() if self.report is not None else (0,) * 8
        return (int(self.order_ready), int(self.order_rejected), int(self.control_ready),
                int(self.offer is not None), *offer_fields,
                int(self.report is not None), *report_fields)


@dataclass
class Lane:
    """Reference state with software FIFO and unbounded integer balances."""

    queue: Deque[Order] = field(default_factory=deque)
    capacity: int = 0
    debt: int = 0
    weight: int = 1

    @property
    def available(self) -> int:
        """Derive remaining credit from the conservation identity."""
        return self.capacity - self.debt

    def eligible(self) -> bool:
        """Allow only the queue head and require its full cost up front."""
        return bool(self.queue) and self.queue[0].cost <= self.available


class Scheduler:
    """Build expected offers and reports using a queue and one debt per lane."""

    def __init__(self, geometry: Geometry) -> None:
        """Start with empty queues and no configured credits."""
        self.geometry = geometry
        self.reset()

    def reset(self) -> None:
        """Drop all pending work and initialize the round-robin scan."""
        self.lanes = [Lane() for _ in range(self.geometry.classes)]
        self.pointer = 0
        self.served = 0
        self.held: Optional[Order] = None
        self.report: Optional[Report] = None

    def ordered_keys(self) -> Tuple[int, ...]:
        """Return the circular priority list anchored at the current pointer."""
        keys = tuple(range(self.geometry.classes))
        return keys[self.pointer:] + keys[:self.pointer]

    def select(self) -> Optional[Order]:
        """Choose the first eligible head or retain the order already offered."""
        if self.held is not None:
            return self.held
        return next((self.lanes[key].queue[0] for key in self.ordered_keys()
                     if self.lanes[key].eligible()), None)

    def snapshot(self, control: Control, status: Status) -> Report:
        """Build a report from the post-edge state of the addressed lane."""
        if status == Status.BAD_CLASS:
            return Report(control.op, control.key, status)
        lane = self.lanes[control.key]
        return Report(control.op, control.key, status, lane.capacity,
                      lane.available, lane.debt, len(lane.queue), lane.weight)

    def classify(self, control: Control, offer: Optional[Order]) -> Status:
        """Validate a command against credit debt and offers from before the edge."""
        if control.key >= self.geometry.classes:
            return Status.BAD_CLASS
        lane = self.lanes[control.key]
        if control.op == Op.CONFIGURE:
            if control.weight == 0:
                return Status.BAD_FIELDS
            if lane.debt or (offer is not None and offer.key == control.key):
                return Status.BUSY
        elif control.op == Op.REFUND:
            if not control.amount or control.weight:
                return Status.BAD_FIELDS
            if control.amount > lane.debt:
                return Status.OVER_REFUND
        elif control.op == Op.INSPECT:
            if control.amount or control.weight:
                return Status.BAD_FIELDS
        else:
            return Status.BAD_FIELDS
        return Status.OK

    def view(self, drive: Drive) -> Pins:
        """Resolve handshakes without updating queue contents or credits."""
        if drive.reset:
            return Pins()
        offer = self.select()
        if drive.order is None or drive.order.key >= self.geometry.classes:
            order_ready = True
        else:
            lane = self.lanes[drive.order.key]
            frees_entry = (offer is not None and drive.sink_ready
                           and offer.key == drive.order.key)
            order_ready = (drive.order.cost == 0
                           or len(lane.queue) < self.geometry.depth or frees_entry)
        rejected = (drive.order is not None and order_ready
                    and (drive.order.cost == 0 or drive.order.key >= self.geometry.classes))
        return Pins(order_ready, rejected, self.report is None or drive.report_ready,
                    offer, self.report)

    def transfer(self, offer: Order) -> None:
        """Spend the offered cost and select the next priority pointer."""
        lane = self.lanes[offer.key]
        assert lane.queue[0] == offer and lane.eligible(), offer
        lane.queue.popleft()
        lane.debt += offer.cost
        quota = self.served + 1 if self.pointer == offer.key else 1
        if quota < lane.weight:
            self.pointer = offer.key
            self.served = quota
        else:
            self.pointer = (offer.key + 1) % self.geometry.classes
            self.served = 0

    def apply(self, control: Control, transferred: bool) -> None:
        """Update only the lane addressed by a previously validated command."""
        lane = self.lanes[control.key]
        if control.op == Op.CONFIGURE:
            lane.capacity = control.amount
            lane.weight = control.weight
            if not transferred and self.pointer == control.key:
                self.served = 0
        elif control.op == Op.REFUND:
            lane.debt -= control.amount

    def verify(self) -> None:
        """Check reference invariants after every non-reset transition."""
        for lane in self.lanes:
            assert 0 <= lane.debt <= lane.capacity <= self.geometry.max_credit
            assert len(lane.queue) <= self.geometry.depth
            assert 1 <= lane.weight <= 15
        if self.held is not None:
            lane = self.lanes[self.held.key]
            assert lane.eligible() and lane.queue[0] == self.held
        assert 0 <= self.pointer < self.geometry.classes
        assert 0 <= self.served < 15

    def advance(self, drive: Drive) -> Pins:
        """Return pre-edge pins, then atomically apply that edge's handshakes."""
        pins = self.view(drive)
        if drive.reset:
            self.reset()
            return pins

        command = drive.control if pins.control_ready else None
        status = self.classify(command, pins.offer) if command is not None else Status.OK
        transmitted = pins.offer is not None and drive.sink_ready
        self.held = None if transmitted else pins.offer
        if transmitted:
            self.transfer(pins.offer)
        if drive.order is not None and pins.order_ready and not pins.order_rejected:
            self.lanes[drive.order.key].queue.append(drive.order)
        if command is not None and status == Status.OK:
            self.apply(command, transmitted)
        if command is not None:
            self.report = self.snapshot(command, status)
        elif drive.report_ready:
            self.report = None
        self.verify()
        return pins

    def balances(self) -> Dict[int, Tuple[int, int, int]]:
        """Expose diagnostic tuples containing capacity, free credit and debt."""
        return {key: (lane.capacity, lane.available, lane.debt)
                for key, lane in enumerate(self.lanes)}
