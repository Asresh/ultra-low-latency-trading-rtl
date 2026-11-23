"""Independent exchange model using a dictionary and age timestamps."""

from dataclasses import dataclass, fields, replace
from enum import IntEnum
from typing import Dict, Iterator, List, Optional, Tuple


class Opcode(IntEnum):
    NEW = 0
    CANCEL = 1
    REDUCE = 2
    LEVEL = 3


class TimeInForce(IntEnum):
    GTC = 0
    IOC = 1
    POST_ONLY = 2


class Kind(IntEnum):
    FILL = 0
    REST = 1
    CANCEL = 2
    REJECT = 3
    REDUCE = 4
    EXPIRE = 5
    LEVEL = 6


class Reject(IntEnum):
    NONE = 0
    ID = 1
    PRICE = 2
    QUANTITY = 3
    DUPLICATE = 4
    ABSENT = 5
    TIME_IN_FORCE = 6
    POST_ONLY = 7
    REDUCE = 8
    INSTRUMENT = 9


@dataclass(frozen=True)
class Profile:
    banks: int
    prices: int
    slots: int
    quantity_width: int
    command_depth: int
    event_depth: int
    sequence_width: int = 8

    @property
    def count_mask(self) -> int:
        return (1 << self.sequence_width) - 1

    @property
    def qty_max(self) -> int:
        return (1 << self.quantity_width) - 1

    @property
    def instrument_max(self) -> int:
        return (1 << self.banks.bit_length()) - 1

    @property
    def id_max(self) -> int:
        return (1 << self.slots.bit_length()) - 1

    @property
    def price_max(self) -> int:
        return (1 << self.prices.bit_length()) - 1


PROFILES = {
    "default": Profile(3, 12, 12, 16, 4, 4),
    "wide": Profile(4, 7, 9, 12, 3, 2),
    "tiny": Profile(1, 1, 2, 4, 1, 1),
}


@dataclass(frozen=True)
class Command:
    instrument: int = 0
    opcode: int = Opcode.NEW
    tif: int = TimeInForce.GTC
    side: bool = False
    order_id: int = 0
    price: int = 0
    quantity: int = 1
    tag: int = 0

    def vector(self) -> Tuple[int, ...]:
        return tuple(int(getattr(self, f.name)) for f in fields(self))


@dataclass(frozen=True)
class Event:
    instrument: int
    tag: int
    command_sequence: int
    event_sequence: int
    kind: int
    last: bool
    order_id: int
    side: bool = False
    maker: int = 0
    price: int = 0
    quantity: int = 0
    remaining: int = 0
    reject: int = Reject.NONE
    level_quantity: int = 0
    level_count: int = 0


@dataclass(frozen=True)
class Order:
    side: bool
    price: int
    quantity: int
    age: int


@dataclass(frozen=True)
class Result:
    kind: int
    last: bool = True
    side: bool = False
    maker: int = 0
    price: int = 0
    quantity: int = 0
    remaining: int = 0
    reject: int = Reject.NONE
    level_quantity: int = 0
    level_count: int = 0

    @staticmethod
    def invalid(reason: Reject) -> "Result":
        return Result(kind=Kind.REJECT, reject=reason)


class Book:
    """The model makes no assumptions about RTL linked lists or occupancy."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.orders: Dict[int, Order] = {}
        self.age = 0

    def level(self, side: bool, price: int) -> Tuple[int, int]:
        entries = tuple(
            order for order in self.orders.values()
            if order.side == side and order.price == price
        )
        return sum(order.quantity for order in entries), len(entries)

    @staticmethod
    def crosses(command: Command, maker: Order) -> bool:
        if maker.side == command.side:
            return False
        if command.side:
            return maker.price >= command.price
        return maker.price <= command.price

    def eligible(self, command: Command) -> Iterator[Tuple[int, Order]]:
        makers = [
            (order_id, order)
            for order_id, order in self.orders.items()
            if self.crosses(command, order)
        ]
        makers.sort(key=lambda item: (
            -item[1].price if command.side else item[1].price,
            item[1].age,
        ))
        return iter(makers)

    def query(self, command: Command) -> List[Result]:
        if command.price >= self.profile.prices:
            return [Result.invalid(Reject.PRICE)]
        depth, count = self.level(command.side, command.price)
        return [Result(
            kind=Kind.LEVEL,
            side=command.side,
            price=command.price,
            level_quantity=depth,
            level_count=count,
        )]

    def modify(self, command: Command, resting: Optional[Order]) -> List[Result]:
        if resting is None:
            return [Result.invalid(Reject.ABSENT)]
        if command.opcode == Opcode.CANCEL:
            del self.orders[command.order_id]
            return [Result(
                kind=Kind.CANCEL,
                side=resting.side,
                price=resting.price,
                quantity=resting.quantity,
            )]
        if not 0 < command.quantity < resting.quantity:
            return [Result.invalid(Reject.REDUCE)]
        self.orders[command.order_id] = replace(resting, quantity=command.quantity)
        return [Result(
            kind=Kind.REDUCE,
            side=resting.side,
            price=resting.price,
            quantity=resting.quantity - command.quantity,
            remaining=command.quantity,
        )]

    def add(self, command: Command, resting: Optional[Order]) -> List[Result]:
        if command.price >= self.profile.prices:
            return [Result.invalid(Reject.PRICE)]
        if command.quantity == 0:
            return [Result.invalid(Reject.QUANTITY)]
        if command.tif not in TimeInForce._value2member_map_:
            return [Result.invalid(Reject.TIME_IN_FORCE)]
        if resting is not None:
            return [Result.invalid(Reject.DUPLICATE)]
        matches = list(self.eligible(command))
        if matches and command.tif == TimeInForce.POST_ONLY:
            return [Result.invalid(Reject.POST_ONLY)]

        results: List[Result] = []
        remaining = command.quantity
        for maker_id, maker in matches:
            filled = min(remaining, maker.quantity)
            remaining -= filled
            if filled == maker.quantity:
                del self.orders[maker_id]
            else:
                self.orders[maker_id] = replace(maker, quantity=maker.quantity - filled)
            results.append(Result(
                kind=Kind.FILL,
                last=(remaining == 0),
                side=command.side,
                maker=maker_id,
                price=maker.price,
                quantity=filled,
                remaining=remaining,
            ))
            if remaining == 0:
                return results
        if command.tif == TimeInForce.IOC:
            results.append(Result(
                kind=Kind.EXPIRE,
                side=command.side,
                price=command.price,
                quantity=remaining,
            ))
        else:
            self.orders[command.order_id] = Order(
                side=command.side,
                price=command.price,
                quantity=remaining,
                age=self.age,
            )
            self.age += 1
            results.append(Result(
                kind=Kind.REST,
                side=command.side,
                price=command.price,
                quantity=remaining,
                remaining=remaining,
            ))
        return results

    def apply(self, command: Command) -> List[Result]:
        if command.opcode == Opcode.LEVEL:
            results = self.query(command)
        elif command.order_id >= self.profile.slots:
            results = [Result.invalid(Reject.ID)]
        else:
            resting = self.orders.get(command.order_id)
            results = (
                self.add(command, resting) if command.opcode == Opcode.NEW
                else self.modify(command, resting)
            )
        self.check_invariants()
        self.check_results(command, results)
        return results

    def check_invariants(self) -> None:
        bids = []
        asks = []
        ages = set()
        for order_id, order in self.orders.items():
            if not 0 <= order_id < self.profile.slots:
                raise AssertionError(f"bad live ID {order_id}")
            if not 0 <= order.price < self.profile.prices:
                raise AssertionError(f"bad live price {order.price}")
            if not 0 < order.quantity <= self.profile.qty_max:
                raise AssertionError(f"bad live quantity {order.quantity}")
            if order.age in ages or order.age >= self.age:
                raise AssertionError(f"duplicate or bad arrival age {order.age}")
            ages.add(order.age)
            if order.side:
                asks.append(order.price)
            else:
                bids.append(order.price)
        if bids and asks and max(bids) >= min(asks):
            raise AssertionError("model produced crossed liquidity")

    def check_results(self, command: Command, results: List[Result]) -> None:
        if not results or any(result.last for result in results[:-1]) or not results[-1].last:
            raise AssertionError(f"bad terminal events for {command}: {results}")
        if command.opcode == Opcode.NEW and results[0].kind != Kind.REJECT:
            if sum(result.quantity for result in results) != command.quantity:
                raise AssertionError(f"incoming quantity was not conserved for {command}")
        if any(result.kind == Kind.FILL and not 0 < result.quantity for result in results):
            raise AssertionError(f"zero fill for {command}")
        if len(results) > self.profile.slots:
            raise AssertionError(f"execution exceeded the maker bound for {command}")


class Reference:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.books = []
        self.command_seq: List[int] = []
        self.event_seq: List[int] = []
        self.reset()

    def reset(self) -> None:
        self.books = [Book(self.profile) for _ in range(self.profile.banks)]
        self.command_seq = [0] * (self.profile.banks + 1)
        self.event_seq = [0] * (self.profile.banks + 1)

    def lane_for(self, instrument: int) -> int:
        return min(instrument, self.profile.banks)

    def execute(self, command: Command) -> List[Event]:
        lane = self.lane_for(command.instrument)
        if lane == self.profile.banks:
            raw_events = [Result.invalid(Reject.INSTRUMENT)]
        else:
            raw_events = self.books[lane].apply(command)
        command_seq = self.command_seq[lane]
        self.command_seq[lane] = (command_seq + 1) & self.profile.count_mask
        events = []
        for raw in raw_events:
            event_seq = self.event_seq[lane]
            self.event_seq[lane] = (event_seq + 1) & self.profile.count_mask
            secondary = {field.name: getattr(raw, field.name) for field in fields(Result)}
            events.append(Event(
                instrument=command.instrument,
                tag=command.tag,
                order_id=command.order_id,
                command_sequence=command_seq,
                event_sequence=event_seq,
                **secondary,
            ))
        return events
