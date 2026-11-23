"""Independent market-by-order and quote-publisher oracle.

The book stores only individual orders. It reconstructs depths and the BBO
on demand by scanning them. The RTL instead maintains depth/count tables.
"""

from dataclasses import dataclass, fields, replace
from enum import IntEnum
from typing import Dict, Iterable, List, Optional, Tuple


class Operation(IntEnum):
    ADD = 0
    CANCEL = 1
    EXECUTE = 2
    DELETE = 3
    REPLACE = 4
    HEARTBEAT = 5
    SNAP_BEGIN = 6
    SNAP_ADD = 7
    SNAP_END = 8
    LEVEL = 9
    ORDER = 10
    BBO = 11
    STAT = 12


class MarketMode(IntEnum):
    COLD = 0
    BOOT = 1
    LIVE = 2
    FENCED = 3


class Status(IntEnum):
    OK = 0
    BANK = 1
    MODE = 2
    GAP = 3
    CODE = 4
    ID = 5
    PRICE = 6
    QTY = 7
    DUPLICATE = 8
    ABSENT = 9
    REPLACE_ID = 10
    STAT = 11


class Reason(IntEnum):
    QUOTING = 0
    INACTIVE = 1
    NO_SIDES = 2
    CROSSED = 3
    SPREAD = 4
    ZERO_SIZE = 5


class Statistic(IntEnum):
    COMMANDS = 0
    UPDATES = 1
    GAPS = 2
    FAULTS = 3
    TIMEOUTS = 4
    SNAPSHOTS = 5
    LIVE_ORDERS = 6
    AGE = 7


STREAM_OPS = frozenset((
    Operation.ADD, Operation.CANCEL, Operation.EXECUTE,
    Operation.DELETE, Operation.REPLACE, Operation.HEARTBEAT,
))
MUTATIONS = frozenset((
    Operation.ADD, Operation.CANCEL, Operation.EXECUTE,
    Operation.DELETE, Operation.REPLACE, Operation.SNAP_ADD,
))
SNAPSHOT_OPS = frozenset((Operation.SNAP_ADD, Operation.SNAP_END))
QUERY_OPS = frozenset((Operation.LEVEL, Operation.ORDER, Operation.BBO, Operation.STAT))


@dataclass(frozen=True)
class Config:
    banks: int = 2
    levels: int = 16
    slots: int = 16
    quantity_width: int = 12
    sequence_width: int = 16
    epoch_width: int = 8
    tag_width: int = 16
    stat_width: int = 32
    revision_width: int = 16
    idle_ticks: int = 16
    quote_size: int = 12
    half_spread: int = 1
    skew_ticks: int = 1
    minimum_spread: int = 2
    imbalance_q8: int = 64

    @staticmethod
    def mask(width: int) -> int:
        return (1 << width) - 1

    @property
    def quantity_max(self) -> int:
        return self.mask(self.quantity_width)

    @property
    def instrument_max(self) -> int:
        return self.mask(self.banks.bit_length())

    @property
    def sequence_max(self) -> int:
        return self.mask(self.sequence_width)

    @property
    def price_max(self) -> int:
        return self.mask(self.levels.bit_length())

    @property
    def order_id_max(self) -> int:
        return self.mask(self.slots.bit_length())

    def rtl_parameters(self) -> Dict[str, int]:
        return {
            "BANKS": self.banks,
            "LEVELS": self.levels,
            "SLOTS": self.slots,
            "QTY_W": self.quantity_width,
            "SEQ_W": self.sequence_width,
            "EPOCH_W": self.epoch_width,
            "TAG_W": self.tag_width,
            "STAT_W": self.stat_width,
            "REV_W": self.revision_width,
            "MAX_IDLE_TICKS": self.idle_ticks,
            "QUOTE_SIZE": self.quote_size,
            "HALF_SPREAD": self.half_spread,
            "SKEW_TICKS": self.skew_ticks,
            "MIN_MARKET_SPREAD": self.minimum_spread,
            "IMBALANCE_Q8": self.imbalance_q8,
        }


PROFILES = {
    "default": Config(),
    "asym": Config(
        banks=3, levels=7, slots=9, quantity_width=10, sequence_width=6,
        epoch_width=3, stat_width=12, revision_width=6, idle_ticks=5,
        quote_size=17, half_spread=1, skew_ticks=2, minimum_spread=2, imbalance_q8=96,
    ),
    "edge": Config(
        banks=1, levels=3, slots=3, quantity_width=4, sequence_width=4,
        epoch_width=2, stat_width=8, revision_width=4, idle_ticks=1,
        quote_size=3, half_spread=1, skew_ticks=1, minimum_spread=1, imbalance_q8=128,
    ),
    "size_off": Config(
        banks=1, levels=5, slots=4, quantity_width=5, sequence_width=5,
        epoch_width=3, stat_width=8, revision_width=4, idle_ticks=3,
        quote_size=0, half_spread=2, skew_ticks=1, minimum_spread=1, imbalance_q8=0,
    ),
}


@dataclass(frozen=True)
class Command:
    instrument: int = 0
    op: int = Operation.HEARTBEAT
    sequence: int = 0
    order_id: int = 0
    new_id: int = 0
    side: bool = False
    price: int = 0
    quantity: int = 0
    stat_index: int = 0
    tag: int = 0

    def values(self) -> Tuple[int, ...]:
        return tuple(int(getattr(self, field.name)) for field in fields(self))

    def is_query(self) -> bool:
        return self.op in QUERY_OPS

    def validate_width(self, config: Config) -> None:
        maximum = (
            config.instrument_max, 15, config.sequence_max, config.order_id_max,
            config.order_id_max, 1, config.price_max, config.quantity_max,
            15, config.mask(config.tag_width),
        )
        for field, value, limit in zip(fields(self), self.values(), maximum):
            if not 0 <= value <= limit:
                raise ValueError(f"{field.name}={value} is outside its encoded range 0..{limit}")


@dataclass(frozen=True)
class Response:
    instrument: int
    op: int
    sequence: int
    order_id: int
    tag: int
    status: int = Status.OK
    mode: int = MarketMode.COLD
    expected: int = 0
    epoch: int = 0
    present: bool = False
    side: bool = False
    price: int = 0
    quantity: int = 0
    count: int = 0
    statistic: int = 0
    bid_present: bool = False
    ask_present: bool = False
    bid: int = 0
    ask: int = 0
    bid_quantity: int = 0
    ask_quantity: int = 0
    bid_count: int = 0
    ask_count: int = 0

    @classmethod
    def echo(cls, command: Command, **fields_) -> "Response":
        return cls(
            instrument=command.instrument,
            op=command.op,
            sequence=command.sequence,
            order_id=command.order_id,
            tag=command.tag,
            **fields_,
        )


@dataclass(frozen=True)
class Order:
    side: bool
    price: int
    size: int


@dataclass(frozen=True)
class Depth:
    size: int = 0
    orders: int = 0


@dataclass(frozen=True)
class Leg:
    present: bool = False
    price: int = 0
    depth: Depth = Depth()


@dataclass(frozen=True)
class BBO:
    bid: Leg
    ask: Leg

    def diagnostic(self) -> Dict[str, int]:
        return {
            "bid_present": self.bid.present,
            "ask_present": self.ask.present,
            "bid": self.bid.price,
            "ask": self.ask.price,
            "bid_quantity": self.bid.depth.size,
            "ask_quantity": self.ask.depth.size,
            "bid_count": self.bid.depth.orders,
            "ask_count": self.ask.depth.orders,
        }


class MarketOrders:
    def __init__(self, config: Config):
        self.config = config
        self.orders: Dict[int, Order] = {}

    def side_orders(self, side: bool) -> Iterable[Order]:
        return (order for order in self.orders.values() if order.side == side)

    def levels(self, side: bool) -> Dict[int, Depth]:
        result: Dict[int, Depth] = {}
        for order in self.side_orders(side):
            prior = result.get(order.price, Depth())
            result[order.price] = Depth(prior.size + order.size, prior.orders + 1)
        return result

    def depth(self, side: bool, price: int) -> Depth:
        return self.levels(side).get(price, Depth())

    def best(self, side: bool) -> Leg:
        prices = self.levels(side)
        if not prices:
            return Leg()
        price = min(prices) if side else max(prices)
        return Leg(True, price, prices[price])

    def bbo(self) -> BBO:
        return BBO(self.best(False), self.best(True))

    def check(self, command: Command) -> Status:
        cfg = self.config
        if command.order_id >= cfg.slots:
            return Status.ID
        existing = self.orders.get(command.order_id)
        if command.op in (Operation.ADD, Operation.SNAP_ADD):
            if command.price >= cfg.levels:
                return Status.PRICE
            if command.quantity == 0:
                return Status.QTY
            if existing is not None:
                return Status.DUPLICATE
            return Status.OK
        if existing is None:
            return Status.ABSENT
        if command.op == Operation.REPLACE:
            if command.new_id >= cfg.slots:
                return Status.ID
            if command.new_id == command.order_id:
                return Status.REPLACE_ID
            if command.new_id in self.orders:
                return Status.DUPLICATE
            if command.price >= cfg.levels:
                return Status.PRICE
            if command.quantity == 0:
                return Status.QTY
        if command.op == Operation.CANCEL and not 0 < command.quantity < existing.size:
            return Status.QTY
        if command.op == Operation.EXECUTE and not 0 < command.quantity <= existing.size:
            return Status.QTY
        return Status.OK

    def mutate(self, command: Command) -> None:
        order_id = command.order_id
        if command.op in (Operation.ADD, Operation.SNAP_ADD):
            self.orders[order_id] = Order(command.side, command.price, command.quantity)
        elif command.op == Operation.REPLACE:
            old = self.orders.pop(order_id)
            self.orders[command.new_id] = Order(old.side, command.price, command.quantity)
        elif command.op == Operation.DELETE:
            del self.orders[order_id]
        else:
            old = self.orders[order_id]
            if old.size == command.quantity:
                del self.orders[order_id]
            else:
                self.orders[order_id] = replace(old, size=(old.size - command.quantity))

    def assert_invariants(self) -> None:
        cfg = self.config
        if len(self.orders) > cfg.slots:
            raise AssertionError("reference exceeded order slot capacity")
        for order_id, order in self.orders.items():
            if not 0 <= order_id < cfg.slots:
                raise AssertionError(f"out-of-range order slot {order_id}")
            if not 0 <= order.price < cfg.levels:
                raise AssertionError(f"out-of-range price in {order}")
            if not 0 < order.size <= cfg.quantity_max:
                raise AssertionError(f"out-of-range quantity in {order}")


@dataclass(frozen=True)
class Intent:
    active: bool = False
    epoch: int = 0
    bid: int = 0
    ask: int = 0
    size: int = 0
    reason: int = Reason.INACTIVE
    pressure_bid: bool = False
    pressure_ask: bool = False

    def key(self) -> Tuple[int, ...]:
        return (int(self.active), self.epoch, self.bid, self.ask, self.size)


@dataclass(frozen=True)
class Quote:
    instrument: int
    active: bool
    epoch: int
    revision: int
    bid: int
    ask: int
    size: int
    reason: int

    def key(self) -> Tuple[int, ...]:
        return (int(self.active), self.epoch, self.bid, self.ask, self.size)

    @classmethod
    def from_intent(cls, instrument: int, revision: int, desired: Intent) -> "Quote":
        return cls(
            instrument=instrument, revision=revision, active=desired.active,
            epoch=desired.epoch, bid=desired.bid, ask=desired.ask,
            size=desired.size, reason=desired.reason,
        )


def quote_policy(cfg: Config, mode: MarketMode, epoch: int, bbo: BBO) -> Intent:
    if mode != MarketMode.LIVE:
        return Intent(epoch=epoch, reason=Reason.INACTIVE)
    bid, ask = bbo.bid, bbo.ask
    if not (bid.present and ask.present):
        return Intent(epoch=epoch, reason=Reason.NO_SIDES)

    delta = bid.depth.size - ask.depth.size
    total = bid.depth.size + ask.depth.size
    crossed_threshold = abs(delta) * 256 >= total * cfg.imbalance_q8
    up = crossed_threshold and delta > 0
    down = crossed_threshold and delta < 0
    inactive = Intent(epoch=epoch, pressure_bid=up, pressure_ask=down)
    if bid.price >= ask.price:
        return replace(inactive, reason=Reason.CROSSED)

    offset = cfg.skew_ticks * (int(up) - int(down))
    midpoint_times_two = bid.price + ask.price
    raw_buy = midpoint_times_two // 2 + offset - cfg.half_spread
    raw_sell = (midpoint_times_two + 1) // 2 + offset + cfg.half_spread
    grid = lambda price: min(cfg.levels - 1, max(0, price))
    buy = min(grid(raw_buy), ask.price - 1)
    sell = max(grid(raw_sell), bid.price + 1)
    if ask.price - bid.price < cfg.minimum_spread or buy >= sell:
        return replace(inactive, reason=Reason.SPREAD)
    size = min(cfg.quote_size, bid.depth.size, ask.depth.size)
    if size == 0:
        return replace(inactive, reason=Reason.ZERO_SIZE)
    if not (0 <= buy < sell < cfg.levels and buy < ask.price and sell > bid.price):
        raise AssertionError(f"policy produced a marketable or crossed quote: {buy}/{sell}")
    return Intent(True, epoch, buy, sell, size, Reason.QUOTING, up, down)


@dataclass(frozen=True)
class BankOutcome:
    response: Response
    refresh: bool = False
    faulted: bool = False


class Instrument:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.book = MarketOrders(cfg)
        self.mode = MarketMode.COLD
        self.expected = 0
        self.epoch = 0
        self.age = 0
        self.statistics = [0] * 6

    def increment(self, stat: Statistic) -> None:
        wrapping = self.cfg.mask(self.cfg.stat_width)
        self.statistics[stat] = (self.statistics[stat] + 1) & wrapping

    def statistic(self, index: int) -> int:
        if index == Statistic.LIVE_ORDERS:
            return len(self.book.orders) & self.cfg.mask(self.cfg.stat_width)
        if index == Statistic.AGE:
            return self.age & self.cfg.mask(self.cfg.stat_width)
        return self.statistics[index]

    def response(self, command: Command, status: Status = Status.OK, **data) -> Response:
        return Response.echo(
            command, status=status, mode=self.mode, expected=self.expected, epoch=self.epoch, **data,
        )

    def query(self, command: Command) -> Response:
        if command.op == Operation.ORDER:
            if command.order_id >= self.cfg.slots:
                return self.response(command, Status.ID)
            order = self.book.orders.get(command.order_id)
            if order is None:
                return self.response(command)
            return self.response(
                command, present=True, side=order.side, price=order.price, quantity=order.size,
            )
        if command.op == Operation.LEVEL:
            if command.price >= self.cfg.levels:
                return self.response(command, Status.PRICE)
            depth = self.book.depth(command.side, command.price)
            return self.response(
                command, present=(depth.orders > 0), side=command.side, price=command.price,
                quantity=depth.size, count=depth.orders,
            )
        if command.op == Operation.BBO:
            return self.response(command, **self.book.bbo().diagnostic())
        if command.stat_index not in range(8):
            return self.response(command, Status.STAT)
        return self.response(command, statistic=self.statistic(command.stat_index))

    def fault(self, command: Command, reason: Status) -> BankOutcome:
        self.mode = MarketMode.FENCED
        self.increment(Statistic.GAPS if reason == Status.GAP else Statistic.FAULTS)
        return BankOutcome(self.response(command, reason), faulted=True)

    def process(self, command: Command) -> BankOutcome:
        if command.is_query():
            outcome = BankOutcome(self.query(command))
        else:
            outcome = self.apply_sequenced(command)
        self.increment(Statistic.COMMANDS)
        self.book.assert_invariants()
        return outcome

    def apply_sequenced(self, command: Command) -> BankOutcome:
        if command.op == Operation.SNAP_BEGIN:
            self.book = MarketOrders(self.cfg)
            self.mode = MarketMode.BOOT
            self.epoch = (self.epoch + 1) & self.cfg.mask(self.cfg.epoch_width)
            self.expected = (command.sequence + 1) & self.cfg.sequence_max
            return BankOutcome(self.response(command), refresh=True)

        if command.op not in STREAM_OPS and command.op not in SNAPSHOT_OPS:
            if self.mode in (MarketMode.LIVE, MarketMode.BOOT):
                return self.fault(command, Status.CODE)
            return BankOutcome(self.response(command, Status.CODE))
        if (command.op in STREAM_OPS and self.mode != MarketMode.LIVE) or (
            command.op in SNAPSHOT_OPS and self.mode != MarketMode.BOOT
        ):
            return BankOutcome(self.response(command, Status.MODE))
        if command.sequence != self.expected:
            return self.fault(command, Status.GAP)
        if command.op in MUTATIONS:
            reason = self.book.check(command)
            if reason != Status.OK:
                return self.fault(command, reason)
            self.book.mutate(command)
            self.increment(Statistic.UPDATES)
        if command.op == Operation.SNAP_END:
            self.mode = MarketMode.LIVE
            self.increment(Statistic.SNAPSHOTS)
        self.expected = (self.expected + 1) & self.cfg.sequence_max
        return BankOutcome(self.response(command), refresh=True)

    def tick(self, pulse: bool, outcome: Optional[BankOutcome]) -> None:
        if outcome and outcome.refresh:
            self.age = 0
        elif outcome and outcome.faulted:
            return
        elif pulse and self.mode == MarketMode.LIVE:
            self.age += 1
            if self.age == self.cfg.idle_ticks:
                self.mode = MarketMode.FENCED
                self.increment(Statistic.TIMEOUTS)

    def desired(self) -> Intent:
        return quote_policy(self.cfg, self.mode, self.epoch, self.book.bbo())


class Publisher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.last = [Intent()] * cfg.banks
        self.revisions = [0] * cfg.banks
        self.refresh_needed = [False] * cfg.banks
        self.next_first = 0
        self.output: Optional[Quote] = None
        self.receipt: Optional[Quote] = None

    def dirty(self, bank: int, present: Intent) -> bool:
        prior = self.last[bank]
        if (prior.active or present.active) and prior.key() != present.key():
            return True
        has_offer = self.output is not None and self.output.instrument == bank
        if self.receipt is not None and self.receipt.instrument == bank:
            refresh = self.receipt.key() != present.key()
        else:
            refresh = self.refresh_needed[bank]
        return present.active and refresh and not has_offer

    def choose(self, desired: List[Intent]) -> Optional[int]:
        bank_count = self.cfg.banks
        order = (*range(self.next_first, bank_count), *range(self.next_first))
        for bank in order:
            if self.dirty(bank, desired[bank]):
                return bank
        return None

    def advance(self, desired: List[Intent], ready: bool) -> Optional[int]:
        offer = self.output
        stalled = offer is not None and not ready
        selected = None if stalled else self.choose(desired)
        for bank, current in enumerate(desired):
            prior = self.last[bank]
            if self.receipt is not None and self.receipt.instrument == bank:
                self.refresh_needed[bank] = self.receipt.key() != current.key()
            elif prior.active and (not current.active or prior.epoch != current.epoch):
                self.refresh_needed[bank] = True
        self.receipt = offer if ready and offer is not None and offer.active else None
        if stalled:
            return None
        self.output = None
        if selected is not None:
            intent = desired[selected]
            self.output = Quote.from_intent(selected, self.revisions[selected], intent)
            self.last[selected] = intent
            self.revisions[selected] = (
                (self.revisions[selected] + 1) & self.cfg.mask(self.cfg.revision_width)
            )
            self.next_first = (selected + 1) % self.cfg.banks
        return selected


@dataclass(frozen=True)
class CycleInput:
    reset: bool
    tick: bool
    command: Optional[Command]
    response_ready: bool
    quote_ready: bool


@dataclass(frozen=True)
class Combinational:
    source_ready: bool
    response: Optional[Response]
    quote: Optional[Quote]
    live: Tuple[bool, ...]
    eligible: Tuple[bool, ...]
    pressure_bid: Tuple[bool, ...]
    pressure_ask: Tuple[bool, ...]
    epochs: Tuple[int, ...]
    quote_current: bool


@dataclass(frozen=True)
class CycleResult:
    pins: Combinational
    scheduled: Optional[int]


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        self.instruments = [Instrument(self.cfg) for _ in range(self.cfg.banks)]
        self.publisher = Publisher(self.cfg)
        self.intent_stage = [Intent()] * self.cfg.banks
        self.response: Optional[Response] = None

    def compute(self, cycle: CycleInput, desired: List[Intent]) -> Combinational:
        quote = self.publisher.output if not cycle.reset else None
        return Combinational(
            source_ready=not cycle.reset and (self.response is None or cycle.response_ready),
            response=self.response if not cycle.reset else None,
            quote=quote,
            live=tuple(not cycle.reset and bank.mode == MarketMode.LIVE for bank in self.instruments),
            eligible=tuple(not cycle.reset and intent.active for intent in desired),
            pressure_bid=tuple(not cycle.reset and intent.pressure_bid for intent in desired),
            pressure_ask=tuple(not cycle.reset and intent.pressure_ask for intent in desired),
            epochs=tuple(bank.epoch for bank in self.instruments),
            quote_current=quote is not None and quote.key() == desired[quote.instrument].key(),
        )

    def cycle(self, cycle: CycleInput) -> CycleResult:
        desired = [bank.desired() for bank in self.instruments]
        observed = self.compute(cycle, desired)
        if cycle.reset:
            self.reset()
            return CycleResult(observed, None)

        scheduled = self.publisher.advance(self.intent_stage, cycle.quote_ready)
        self.intent_stage = desired
        selected = None
        outcome = None
        if observed.source_ready:
            self.response = None
            if cycle.command is not None:
                command = cycle.command
                if command.instrument < self.cfg.banks:
                    selected = command.instrument
                    outcome = self.instruments[selected].process(command)
                    self.response = outcome.response
                else:
                    self.response = Response.echo(command, status=Status.BANK)
        for instrument, bank in enumerate(self.instruments):
            bank.tick(cycle.tick, outcome if instrument == selected else None)
        return CycleResult(observed, scheduled)
