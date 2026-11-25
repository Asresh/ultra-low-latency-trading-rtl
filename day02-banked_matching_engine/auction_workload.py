"""Create directed and adaptive call-auction input tapes."""

from dataclasses import replace
from random import Random

from auction_model import Auction, Command, Op


def worked_example():
    """A mixed market-and-limit cross with a known allocation and three residual rows."""
    epoch = 1
    yield Command(Op.OPEN, epoch, price=7, low=3, high=11)
    for id, owner, side, market, carry, price, amount in (
        (0,1,0,1,0,0,6), (1,2,0,0,1,10,8), (2,3,0,0,1,7,7),
        (3,4,1,1,0,0,4), (4,5,1,0,1,6,5), (5,6,1,0,1,7,8),
        (6,7,1,0,0,12,9), (7,8,0,0,1,3,9),
    ):
        yield Command(Op.ADD, epoch, id=id, owner=owner, side=side, market=market,
                      carry=carry, price=price, quantity=amount)
    yield Command(Op.INDICATE, epoch)
    yield (1, 24)
    yield (3, 22)
    yield Command(Op.CLOSE, epoch)


def directed(geometry):
    p = geometry.prices
    max_epoch = geometry.epoch_mask
    yield Command(Op.INDICATE, 0)
    yield Command(Op.OPEN, 0, high=p-1)
    yield Command(Op.OPEN, 1, price=p, high=p)
    yield Command(Op.OPEN, 1, quantity=1, high=p-1)
    yield Command(Op.OPEN, 1, price=p//2, high=p-1)
    yield Command(Op.OPEN, 1, high=p-1)
    yield Command(Op.ADD, 0, quantity=1)
    yield Command(Op.ADD, 1, id=geometry.slots, quantity=1)
    yield Command(Op.ADD, 1, quantity=0)
    yield Command(Op.ADD, 1, price=p, quantity=1)
    yield Command(Op.ADD, 1, market=True, carry=True, quantity=1)
    yield Command(Op.ADD, 1, owner=1, quantity=geometry.quantity_max, carry=True)
    yield Command(Op.ADD, 1, owner=1, quantity=1)
    yield Command(Op.CANCEL, 1, owner=0)
    yield Command(Op.REDUCE, 1, owner=1, quantity=geometry.quantity_max)
    yield Command(Op.REDUCE, 1, owner=1, quantity=1)
    yield Command(Op.INSPECT, 1, owner=1)
    yield Command(Op.CANCEL, 1, owner=1)
    yield Command(Op.CANCEL, 1, owner=1)
    yield Command(Op.INDICATE, 1)
    for id in range(geometry.slots):
        yield Command(Op.ADD, 1, id=id, owner=1, side=id % 2, quantity=geometry.quantity_max,
                      price=p-1 if not id % 2 else 0, carry=not id % 2)
    yield Command(Op.INDICATE, 1)
    yield (3, p+2*geometry.slots+19)
    yield Command(Op.CLOSE, 1)
    yield (1, p+3*geometry.slots+70)
    yield Command(Op.CLOSE, 1)
    limit = min(max_epoch+2, 10)
    for epoch in range(2,limit+1):
        code = epoch & max_epoch
        yield Command(Op.OPEN, code, high=p-1)
        yield Command(Op.ABORT, code)
    yield (1, 20)
    yield (3, 22)
    yield Command(Op.OPEN, (limit+1) & max_epoch, high=p-1)
    yield (2, 0)
    yield Command(Op.OPEN, 1, high=p-1)
    yield Command(Op.ABORT, 1)


def mutate(rng, geometry, book):
    p = geometry.prices
    q = geometry.quantity_max
    epoch = book.epoch
    if book.orders and rng.random() < 0.72:
        row = rng.choice(tuple(book.orders.values()))
        id, owner = row.id, row.owner
    else:
        id = rng.randrange(geometry.slots)
        row = book.orders.get(id)
        owner = rng.randrange(1 << geometry.owner_bits)
    mode = rng.choices(
        (Op.ADD, Op.CANCEL, Op.REDUCE, Op.INSPECT, Op.INDICATE),
        (52, 14, 14, 14, 6),
    )[0]
    if mode == Op.ADD:
        vacant = sorted(set(range(geometry.slots))-book.orders.keys())
        id = rng.choice(vacant) if vacant and rng.randrange(5) else id
        market = rng.randrange(5) == 0
        return Command(mode, epoch, id=id, owner=owner, side=rng.randrange(2), market=market,
                       price=0 if market else rng.randrange(p), carry=not market and rng.randrange(2),
                       quantity=rng.choice((1, q, rng.randint(1, q))))
    if mode == Op.REDUCE:
        quantity = 1 if not row or row.quantity == 1 else rng.randrange(1, row.quantity)
        return Command(mode, epoch, id=id, owner=owner, quantity=quantity)
    if mode == Op.INDICATE:
        return Command(mode, epoch)
    return Command(mode, epoch, id=id, owner=owner)


def epochs(geometry, count, seed):
    rng = Random(seed)
    p = geometry.prices
    book = Auction(geometry)
    for index in range(count):
        if index and index % 17 == 0:
            yield (2, 0)
            book = Auction(geometry)
        lo = rng.randrange(p)
        hi = rng.randrange(lo,p)
        reference = rng.randrange(lo,hi+1)
        code = (book.epoch+1) & geometry.epoch_mask
        fractional = reference < hi and rng.randrange(2) == 1
        opening = Command(Op.OPEN, code, low=lo, high=hi, price=reference, reference_half=fractional)
        book.execute(opening)
        yield opening
        for _ in range(rng.randint(geometry.slots, 3*geometry.slots+3)):
            command = mutate(rng, geometry, book)
            if rng.randrange(12) == 0:
                command = replace(command, epoch=(code+1) & geometry.epoch_mask)
            yield command
            book.execute(command)
        if index % 5 == 0:
            yield (3, rng.randint(9, p+2*geometry.slots+14))
        ending = Command(Op.ABORT if rng.randrange(9) == 0 else Op.CLOSE, code)
        book.execute(ending)
        yield ending


def write_tape(path, geometry, items, first_tag=0):
    accepted = 0
    with path.open("w", encoding="ascii") as stream:
        for record in items:
            if isinstance(record, Command):
                tag = (first_tag+accepted) & ((1 << geometry.tag_bits)-1)
                command = replace(record, tag=tag)
                print(f"0 0 {command.pack(geometry):x}", file=stream)
                accepted += 1
            else:
                directive, cycles = record
                print(f"{directive} {cycles} 0", file=stream)
    return accepted
