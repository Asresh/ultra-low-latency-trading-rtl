"""Independent full-book price discovery and execution model for a call auction."""

from collections import deque
from dataclasses import dataclass, replace
from enum import IntEnum


class Op(IntEnum):
    OPEN = 0
    ADD = 1
    CANCEL = 2
    REDUCE = 3
    INDICATE = 4
    CLOSE = 5
    ABORT = 6
    INSPECT = 7


class Kind(IntEnum):
    REJECT = 0
    OPENED = 1
    ADDED = 2
    CANCELED = 3
    REDUCED = 4
    INSPECTED = 5
    INDICATIVE = 6
    CLEARING = 7
    TRADE = 8
    CARRY = 9
    EXPIRE = 10
    DONE = 11
    ABORTED = 12


class Reject(IntEnum):
    NONE = 0
    PHASE = 1
    EPOCH = 2
    FIELDS = 3
    PRICE = 4
    ID = 5
    DUPLICATE = 6
    ABSENT = 7
    OWNER = 8
    QUANTITY = 9


@dataclass(frozen=True)
class Geometry:
    prices: int = 16
    slots: int = 16
    quantity_bits: int = 16
    owner_bits: int = 8
    epoch_bits: int = 16
    tag_bits: int = 16

    @property
    def epoch_mask(self):
        return (1 << self.epoch_bits)-1

    @property
    def quantity_max(self):
        return (1 << self.quantity_bits)-1

    @property
    def order_bits(self):
        return self.slots.bit_length()

    @property
    def price_bits(self):
        return self.prices.bit_length()

    @property
    def sum_bits(self):
        return self.quantity_bits+self.order_bits

    @property
    def command_widths(self):
        return (
            3, self.epoch_bits, self.tag_bits, self.order_bits, self.owner_bits, 1, 1, 1, 1,
            self.price_bits, self.price_bits, self.price_bits, self.quantity_bits,
        )

    @property
    def result_widths(self):
        return (
            4, 4, 1, self.epoch_bits, self.tag_bits, self.order_bits, self.order_bits,
            self.owner_bits, self.owner_bits, 1, 1, 1, 1, self.price_bits,
            self.sum_bits, self.sum_bits, self.sum_bits, self.order_bits, self.order_bits, self.order_bits,
        )

    def parameters(self):
        return dict(PRICES=self.prices, SLOTS=self.slots, QTY_W=self.quantity_bits,
                    OWNER_W=self.owner_bits, EPOCH_W=self.epoch_bits, TAG_W=self.tag_bits)


PROFILES = {
    "default": Geometry(),
    "odd": Geometry(11, 9, 8, 3, 3, 12),
    "tick1": Geometry(1, 4, 5, 2, 2, 10),
    "slot1": Geometry(5, 1, 6, 2, 2, 11),
}


def pack(fields, widths):
    data = 0
    for raw, bits in zip(fields, widths):
        field = int(raw)
        if not 0 <= field < 1 << bits:
            raise ValueError(f"field {raw!r} exceeds unsigned width {bits}")
        data = (data << bits) | field
    return data


def unpack(data, widths):
    fields = []
    for size in reversed(widths):
        fields.append(data & ((1 << size)-1))
        data >>= size
    if data:
        raise ValueError("wire word exceeds the geometry's packed width")
    return tuple(reversed(fields))


@dataclass(frozen=True)
class Command:
    op: Op
    epoch: int
    tag: int = 0
    id: int = 0
    owner: int = 0
    side: int = 0
    market: bool = False
    carry: bool = False
    reference_half: bool = False
    price: int = 0
    low: int = 0
    high: int = 0
    quantity: int = 0

    def pack(self, geometry):
        return pack(vars(self).values(), geometry.command_widths)

    @classmethod
    def unpack(cls, data, geometry):
        return cls(*unpack(data, geometry.command_widths))


@dataclass(frozen=True)
class Result:
    kind: Kind
    reason: Reject = Reject.NONE
    last: bool = True
    epoch: int = 0
    tag: int = 0
    first: int = 0
    second: int = 0
    first_owner: int = 0
    second_owner: int = 0
    side: int = 0
    market: bool = False
    carry: bool = False
    reference_half: bool = False
    price: int = 0
    quantity: int = 0
    bids: int = 0
    asks: int = 0
    count: int = 0
    carried: int = 0
    expired: int = 0

    def pack(self, geometry):
        return pack(vars(self).values(), geometry.result_widths)

    @classmethod
    def unpack(cls, data, geometry):
        return cls(*unpack(data, geometry.result_widths))


@dataclass(frozen=True)
class Order:
    id: int
    owner: int
    side: int
    market: bool
    carry: bool
    price: int
    quantity: int

    def eligible(self, tick):
        return self.market or (self.price <= tick if self.side else self.price >= tick)

    def priority(self):
        adjusted_price = self.price if self.side else -self.price
        return (not self.market, adjusted_price)

    def result(self, kind, *, last=True):
        return Result(kind, last=last, first=self.id, first_owner=self.owner, side=self.side,
                      market=self.market, carry=self.carry, price=self.price, quantity=self.quantity)


@dataclass(frozen=True)
class Point:
    tick: int
    bids: int
    asks: int

    @property
    def volume(self):
        return min(self.bids, self.asks)

    def rank(self, reference_x2):
        return -self.volume, abs(self.bids-self.asks), abs(2*self.tick-reference_x2), self.tick


class Auction:
    """Use insertion-ordered rows and recompute each price from the entire live book."""

    def __init__(self, geometry):
        self.geometry = geometry
        self.epoch = 0
        self.orders = {}
        self.reference = None
        self.reference_half = False
        self.band = None

    def point(self, tick):
        bids, asks = 0, 0
        for order in self.orders.values():
            if order.eligible(tick):
                if order.side:
                    asks += order.quantity
                else:
                    bids += order.quantity
        return Point(tick, bids, asks)

    def select_price(self):
        midpoint = 2*self.reference + self.reference_half
        return min((self.point(tick) for tick in self.band), key=lambda point: point.rank(midpoint))

    def validate(self, command):
        collecting = self.band is not None
        if (command.op == Op.OPEN) == collecting:
            return Reject.PHASE
        epoch = ((self.epoch+1) & self.geometry.epoch_mask) if command.op == Op.OPEN else self.epoch
        if command.epoch != epoch:
            return Reject.EPOCH
        order_identity = any((command.id, command.owner, command.side, command.market, command.carry))
        if command.op == Op.OPEN:
            if order_identity or command.quantity:
                return Reject.FIELDS
            if not 0 <= command.low <= command.price <= command.high < self.geometry.prices or (
                command.reference_half and command.price == command.high
            ):
                return Reject.PRICE
            return Reject.NONE
        if command.low or command.high or command.reference_half:
            return Reject.FIELDS
        if command.op in (Op.INDICATE, Op.CLOSE, Op.ABORT):
            return Reject.FIELDS if order_identity or command.price or command.quantity else Reject.NONE
        if command.id >= self.geometry.slots:
            return Reject.ID
        if command.op == Op.ADD:
            if command.market and (command.price or command.carry):
                return Reject.FIELDS
            if command.price >= self.geometry.prices:
                return Reject.PRICE
            if not command.quantity:
                return Reject.QUANTITY
            return Reject.DUPLICATE if command.id in self.orders else Reject.NONE
        if command.side or command.market or command.carry or command.price or (
            command.op != Op.REDUCE and command.quantity
        ):
            return Reject.FIELDS
        order = self.orders.get(command.id)
        if order is None:
            return Reject.ABSENT
        if order.owner != command.owner:
            return Reject.OWNER
        if command.op == Op.REDUCE and not 0 < command.quantity < order.quantity:
            return Reject.QUANTITY
        return Reject.NONE

    def execute(self, command):
        reason = self.validate(command)
        if reason != Reject.NONE:
            results = [Result(Kind.REJECT, reason=reason)]
        else:
            results = self.dispatch(command)
        assert [result.last for result in results] == [False] * (len(results)-1) + [True]
        return [replace(result, epoch=command.epoch, tag=command.tag) for result in results]

    def dispatch(self, command):
        if command.op == Op.OPEN:
            self.epoch = command.epoch
            self.band = range(command.low, command.high+1)
            self.reference = command.price
            self.reference_half = command.reference_half
            return [Result(Kind.OPENED, price=self.reference, reference_half=self.reference_half)]
        if command.op == Op.ABORT:
            count = len(self.orders)
            self.orders.clear()
            self.band = None
            return [Result(Kind.ABORTED, count=count)]
        if command.op == Op.ADD:
            order = Order(command.id, command.owner, command.side, command.market,
                          command.carry, command.price, command.quantity)
            self.orders[order.id] = order
            return [order.result(Kind.ADDED)]
        if command.op == Op.CANCEL:
            return [self.orders.pop(command.id).result(Kind.CANCELED)]
        if command.op == Op.REDUCE:
            order = replace(self.orders[command.id], quantity=command.quantity)
            self.orders[order.id] = order
            return [order.result(Kind.REDUCED)]
        if command.op == Op.INSPECT:
            return [self.orders[command.id].result(Kind.INSPECTED)]
        price = self.select_price()
        closing = command.op == Op.CLOSE
        summary = Result(Kind.CLEARING if closing else Kind.INDICATIVE, last=not closing,
                         price=price.tick, quantity=price.volume, bids=price.bids, asks=price.asks,
                         count=len(self.orders), reference_half=self.reference_half)
        if closing:
            return [summary, *self.close(price)]
        return [summary]

    def close(self, price):
        orders = self.orders.copy()
        bids, asks = [
            deque(sorted(
                (row for row in self.orders.values() if row.side == side and row.eligible(price.tick)),
                key=Order.priority,
            ))
            for side in (0, 1)
        ]
        fills = []
        matched = 0
        filled = dict.fromkeys(orders, 0)
        while bids and asks:
            bid, ask = bids[0], asks[0]
            quantity = min(bid.quantity, ask.quantity)
            fills.append(Result(
                Kind.TRADE, last=False, first=bid.id, second=ask.id,
                first_owner=bid.owner, second_owner=ask.owner, price=price.tick, quantity=quantity,
            ))
            for queue, resting in ((bids, bid), (asks, ask)):
                filled[resting.id] += quantity
                if quantity == resting.quantity:
                    queue.popleft()
                    del orders[resting.id]
                else:
                    order = replace(resting, quantity=resting.quantity-quantity)
                    queue[0] = order
                    orders[resting.id] = order
            matched += quantity
        assert matched == price.volume
        assert all(
            filled[key] + (orders[key].quantity if key in orders else 0) == original.quantity
            for key, original in self.orders.items()
        )
        ends = [order.result(Kind.CARRY if order.carry else Kind.EXPIRE, last=False)
                for order in orders.values()]
        totals = Result(Kind.DONE, price=price.tick, quantity=price.volume, count=len(fills),
                        carried=sum(row.carry for row in ends), expired=sum(not row.carry for row in ends))
        self.orders.clear()
        self.band = None
        return [*fills, *ends, totals]
