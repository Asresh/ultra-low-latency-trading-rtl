"""Construct reproducible command streams and independent sink schedules."""

from dataclasses import dataclass, replace
from random import Random
from typing import Iterable, List, Tuple

from matching_model import Book, Command, Opcode, Profile, TimeInForce


@dataclass(frozen=True)
class Beat:
    command: Command
    gap: int = 0

    def line(self) -> str:
        return " ".join(str(value) for value in (*self.command.vector(), self.gap))


@dataclass
class Scenario:
    name: str
    beats: List[Beat]
    schedule: List[Tuple[bool, bool]]

    def add(self, command: Command, gap: int = 0) -> None:
        tagged = replace(command, tag=len(self.beats) & 0xFFFF)
        self.beats.append(Beat(tagged, gap))

    def extend(self, commands: Iterable[Command]) -> None:
        for command in commands:
            self.add(command)


def new(
    instrument: int, order_id: int, price: int, quantity: int,
    side: bool = False, tif: int = TimeInForce.GTC,
) -> Command:
    return Command(
        instrument=instrument,
        opcode=Opcode.NEW,
        tif=tif,
        side=side,
        order_id=order_id,
        price=price,
        quantity=quantity,
    )


def cancel(instrument: int, order_id: int) -> Command:
    return Command(instrument=instrument, opcode=Opcode.CANCEL, order_id=order_id)


def reduce(instrument: int, order_id: int, quantity: int) -> Command:
    return Command(
        instrument=instrument, opcode=Opcode.REDUCE, order_id=order_id, quantity=quantity
    )


def level(instrument: int, price: int, side: bool, tag_id: int = 0) -> Command:
    return Command(
        instrument=instrument,
        opcode=Opcode.LEVEL,
        side=side,
        price=price,
        order_id=tag_id,
    )


def basic_tape(p: Profile, bank: int) -> List[Command]:
    edge = p.prices - 1
    peak = p.qty_max
    return [
        level(bank, edge, True, p.id_max),
        new(bank, p.slots, p.prices, 0, tif=3),
        new(bank, 0, p.prices, 1),
        new(bank, 0, edge, 0),
        new(bank, 0, edge, 1, tif=3),
        cancel(bank, 0),
        reduce(bank, 0, 1),
        new(bank, 0, edge, peak, True, TimeInForce.POST_ONLY),
        new(bank, 0, edge, 1, True),
        reduce(bank, 0, peak),
        reduce(bank, 0, 0),
        level(bank, edge, True),
        reduce(bank, 0, peak - 1),
        new(bank, 1, edge, 2, False, TimeInForce.POST_ONLY),
        new(bank, 1, edge, 1, False, TimeInForce.IOC),
        level(bank, edge, True),
        cancel(bank, 0),
        new(bank, 1, edge, 1, False, TimeInForce.IOC),
        new(bank, 0, edge, 5),
        new(bank, 1, edge, 6, True, TimeInForce.IOC),
        level(bank, edge, False),
        level(bank, p.prices, False),
    ]


def queue_tape(p: Profile, bank: int) -> List[Command]:
    if p.slots < 5 or p.prices < 3:
        return []
    low = 1
    high = p.prices - 1
    return [
        new(bank, 0, low, 9, True),
        new(bank, 1, low, 6, True),
        new(bank, 2, high, 5, True),
        new(bank, 3, low, 8, True),
        new(bank, 4, low, 7, True),
        reduce(bank, 0, 4),
        cancel(bank, 3),
        cancel(bank, 4),
        level(bank, low, True),
        new(bank, 3, high, 12, False, TimeInForce.IOC),
        level(bank, high, True),
        new(bank, 0, high, 8, False),
        level(bank, high, False),
        new(bank, 4, low, 3),
        new(bank, 2, high, 10),
        new(bank, 1, low, 24, True),
        level(bank, low, True),
        cancel(bank, 1),
        level(bank, low, True),
    ]


def full_tape(p: Profile, bank: int) -> List[Command]:
    edge = p.prices - 1
    orders = [
        new(bank, order_id, edge, 1, True)
        for order_id in range(p.slots - 1)
    ]
    orders.append(level(bank, edge, True))
    orders.append(new(
        bank, p.slots - 1, edge, min(p.slots, p.qty_max), False, TimeInForce.IOC
    ))
    orders.append(level(bank, edge, True))
    return orders


def aggregate_tape(p: Profile, bank: int) -> List[Command]:
    edge = p.prices - 1
    adds = [new(bank, i, edge, p.qty_max) for i in range(p.slots)]
    queries = [
        level(bank, edge, False),
        new(bank, p.slots - 1, edge, 1, True, TimeInForce.IOC),
        reduce(bank, 0, 1),
        level(bank, edge, False),
    ]
    cancels = [cancel(bank, i) for i in reversed(range(p.slots))]
    return [*adds, *queries, *cancels, level(bank, edge, False)]


def interleave(*tapes: List[Command]) -> Iterable[Command]:
    for position in range(max((len(tape) for tape in tapes), default=0)):
        for tape in tapes:
            if position < len(tape):
                yield tape[position]


def adversarial_command(
    p: Profile, rng: Random, banks: List[Book], cursor: int,
) -> Command:
    if rng.randrange(13) == 0:
        instrument = rng.randint(p.banks, p.instrument_max)
        return level(instrument, rng.randrange(p.prices), bool(rng.randrange(2)))
    instrument = (cursor % p.banks) if rng.randrange(4) == 0 else rng.randrange(p.banks)
    book = banks[instrument]
    live = list(book.orders)
    free = [order_id for order_id in range(p.slots) if order_id not in book.orders]
    choice = rng.randrange(100)
    if choice < 14:
        price = rng.randrange(p.prices) if rng.randrange(8) else p.prices
        side = bool(rng.randrange(2))
        return level(instrument, price, side, rng.randint(0, p.id_max))
    if choice < 33:
        order_id = rng.choice(live) if live and rng.randrange(4) else rng.randint(0, p.id_max)
        return cancel(instrument, order_id)
    if choice < 48:
        order_id = rng.choice(live) if live and rng.randrange(4) else rng.randint(0, p.id_max)
        resting = book.orders.get(order_id)
        if resting and resting.quantity > 1 and rng.randrange(4):
            amount = rng.randrange(1, resting.quantity)
        elif resting:
            amount = rng.choice([0, resting.quantity, p.qty_max])
        else:
            amount = rng.randrange(1, p.qty_max + 1)
        return reduce(instrument, order_id, amount)

    order_id = rng.choice(free) if free and rng.randrange(5) else rng.randint(0, p.id_max)
    side = bool(rng.randrange(2))
    price = rng.randrange(p.prices) if rng.randrange(12) else rng.randint(p.prices, p.price_max)
    qty = rng.randrange(0, min(17, p.qty_max + 1))
    if rng.randrange(10) == 0:
        qty = rng.randint(1, p.qty_max)
    tif = rng.choices([0, 1, 2, 3], weights=[48, 32, 17, 3])[0]
    return new(instrument, order_id, price, qty, side, tif)


def default_sink(cycles: int) -> List[Tuple[bool, bool]]:
    return [(True, False), (True, False), *[(False, True)] * cycles]


def stress_sink(seed: int, commands: int) -> List[Tuple[bool, bool]]:
    rng = Random(seed ^ 0x4AC1)
    rows = default_sink(29)
    for cycle in range(commands * 4 + 550):
        dense_stall = cycle % 137 in range(62, 87)
        sparse_stall = rng.randrange(13) < 3
        rows.append((False, not (dense_stall or sparse_stall)))
    return rows


def stress(p: Profile, commands: int, seed: int) -> Scenario:
    result = Scenario("stress", [], stress_sink(seed, commands))
    books = [Book(p) for _ in range(p.banks)]

    tapes = [
        [*basic_tape(p, lane), *queue_tape(p, lane),
         *full_tape(p, lane), *aggregate_tape(p, lane)]
        for lane in range(p.banks)
    ]
    for command in interleave(*tapes):
        result.add(command)
        books[command.instrument].apply(command)

    for key in range(p.banks, p.instrument_max + 1):
        result.add(new(key, p.id_max, p.price_max, 0, tif=3))
        result.add(level(key, 0, False))

    rng = Random(seed)
    for index in range(commands):
        command = adversarial_command(p, rng, books, index)
        result.add(command, gap=rng.randint(1, 4) if rng.randrange(16) == 0 else 0)
        if command.instrument < p.banks:
            books[command.instrument].apply(command)

    for price in range(p.prices):
        for lane in range(p.banks):
            result.add(level(lane, price, False))
            result.add(level(lane, price, True))
    return result


def reset_storm(p: Profile) -> Scenario:
    schedule = default_sink(3)
    schedule += [(False, False)] * 44
    schedule += [(True, False)] * 3
    schedule += [(False, True)] * 16
    schedule += [(False, False)] * 37
    schedule += [(True, False)] * 2
    schedule += [(False, True)] * 96
    result = Scenario("reset", [], schedule)
    for cycle in range(192):
        bank = cycle % p.banks
        price = cycle % p.prices
        if cycle < 8 or cycle % 3 == 1:
            command = new(bank, cycle % p.slots, price, 1 + cycle % p.qty_max, bool(cycle % 2))
        else:
            command = level(bank, price, bool(cycle % 2))
        result.add(command)
    for lane in range(p.banks):
        for price in range(p.prices):
            result.add(level(lane, price, False))
            result.add(level(lane, price, True))
    return result


def benchmark(p: Profile, banks: int) -> Scenario:
    if not 1 <= banks <= p.banks:
        raise ValueError("benchmark needs between one and all implemented banks")
    result = Scenario(f"benchmark_{banks}", [], default_sink(2))
    for index in range(384):
        bank = index % banks
        price = (index // banks) % p.prices
        result.add(level(bank, price, bool(index % 2)))
    return result


def scenarios(p: Profile, commands: int, seed: int) -> Iterable[Scenario]:
    yield stress(p, commands, seed)
    yield reset_storm(p)
    yield benchmark(p, 1)
    if p.banks > 1:
        yield benchmark(p, p.banks)
