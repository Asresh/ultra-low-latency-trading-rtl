"""Build deterministic directed and random reserve matcher scenarios."""

from dataclasses import replace
from random import Random

from reserve_model import Command, ImportPart, Management, Operation, Prevention, Scope, TimeInForce


def operation(opcode, bank, order_id=0, owner=0, **changes):
    """Create an administrative transaction with explicit ownership."""
    return Command(bank=bank, operation=opcode, order_id=order_id, owner=owner, **changes)


def import_command(part, id=0, **changes):
    """Emit one import stage, using the order-id lane for declared row count."""
    return operation(Operation.IMPORT, 0, order_id=id, tif=part, **changes)


def manage(part, bank=0, owner=1, **changes):
    """Encode reserve order management with all unused bulk fields at zero."""
    return replace(Command(bank=bank, owner=owner, operation=Operation.MANAGE, tif=part, quantity=0), **changes)


def management_examples(geometry):
    """Demonstrate retained and renewed priority before a scoped owner withdrawal."""
    price = geometry.prices // 2
    yield Command(side=1, order_id=0, price=price, quantity=8, display=3, owner=1)
    yield Command(side=1, order_id=1, price=price, quantity=6, owner=2)
    yield manage(Management.REPLACE, order_id=0, side=1, price=price, quantity=6, display=3)
    yield Command(order_id=2, price=price, quantity=1, tif=TimeInForce.IOC)
    yield manage(Management.REPLACE, owner=2, order_id=0, side=1, price=price, quantity=7, display=3)
    yield manage(Management.REPLACE, order_id=0, side=0, price=price, quantity=7, display=3)
    yield manage(Management.REPLACE, order_id=0, side=1, price=price, quantity=4, display=5)
    yield manage(Management.REPLACE, order_id=0, side=1, price=price, quantity=8, display=3)
    yield Command(order_id=2, price=price, quantity=1, tif=TimeInForce.IOC)
    yield Command(side=1, order_id=2, price=price, quantity=4, owner=1)
    yield manage(Management.PREVIEW, side=1, price=price, stp=Scope.LEVEL)
    yield manage(Management.CANCEL, side=1, price=price, stp=Scope.THROUGH)
    yield operation(Operation.ORDER, 0, order_id=1, owner=2)
    yield operation(Operation.SNAPSHOT, 0)


def directed(geometry):
    """Cover the contractual differences from the original matcher."""
    price = geometry.prices // 2
    yield Command(side=1, order_id=0, price=price, quantity=12, display=3, owner=1)
    yield Command(side=1, order_id=1, price=price, quantity=4, owner=2)
    yield operation(Operation.LEVEL, 0, side=1, price=price)
    # The refreshed slice loses time priority, so the regular maker executes next.
    yield Command(side=0, order_id=2, price=price, quantity=8, owner=3, tif=TimeInForce.IOC)
    yield operation(Operation.ORDER, 0, order_id=0, owner=1)
    yield operation(Operation.REDUCE, 0, order_id=0, owner=1, quantity=2)
    yield operation(Operation.ORDER, 0, order_id=0, owner=1)
    yield operation(Operation.CANCEL, 0, order_id=0, owner=2)
    yield operation(Operation.LEVEL, 0, side=1, price=price)
    yield operation(Operation.CANCEL, 0, order_id=0, owner=1)

    yield Command(side=1, order_id=0, price=price, quantity=5, display=2, owner=1)
    yield Command(side=1, order_id=1, price=price, quantity=8, display=2, owner=2)
    # A failed FOK must not cancel the same-owner order at the front.
    yield Command(
        side=0, order_id=2, price=price, quantity=9, owner=1, tif=TimeInForce.FOK,
        stp=Prevention.CANCEL_MAKER,
    )
    yield operation(Operation.ORDER, 0, order_id=0, owner=1)
    yield Command(
        side=0, order_id=2, price=price, quantity=6, owner=1, tif=TimeInForce.FOK,
        stp=Prevention.CANCEL_MAKER,
    )
    yield operation(Operation.ORDER, 0, order_id=1, owner=2)
    yield Command(
        side=0, order_id=2, price=price, quantity=3, owner=2, tif=TimeInForce.IOC,
        stp=Prevention.CANCEL_TAKER,
    )
    yield Command(
        side=0, order_id=2, price=price, quantity=3, owner=3, tif=TimeInForce.IOC,
    )
    yield Command(
        side=0, order_id=2, price=price, quantity=3, owner=2, tif=TimeInForce.FOK,
        stp=Prevention.CANCEL_TAKER,
    )
    yield Command(price=geometry.prices)
    yield Command(order_id=geometry.slots)
    yield Command(bank=geometry.banks)
    yield Command(quantity=0)
    yield Command(quantity=1, display=2)
    yield Command(stp=3)
    yield manage(3)
    yield operation(Operation.REDUCE, 0, order_id=2, owner=0, quantity=1)
    yield Command(side=1, order_id=0, price=price, quantity=6, owner=0)
    yield Command(side=0, order_id=2, price=price, quantity=1, tif=TimeInForce.POST, owner=1)
    yield operation(Operation.REDUCE, 0, order_id=0, owner=0, quantity=6)
    yield Command(side=1, order_id=0, price=price, quantity=3, owner=2)
    yield (3, 60)
    # More than one bank can progress while another bank is draining egress.
    for i in range(40):
        yield operation(Operation.LEVEL, i % geometry.banks, side=i % 2, price=price)
    yield (1, 128)
    yield (2, 0)
    yield operation(Operation.LEVEL, 0, side=1, price=price)
    yield operation(Operation.SNAPSHOT, 0)
    yield Command(side=1, order_id=0, price=price, quantity=5, display=3, owner=1)
    yield operation(Operation.SNAPSHOT, 0)
    yield import_command(ImportPart.BEGIN, 2)
    yield import_command(ImportPart.ROW, 1, side=1, price=price, quantity=7, display=3, import_visible=1)
    yield import_command(ImportPart.ROW, 1, side=1, price=price, quantity=7, display=3, import_visible=1)
    yield import_command(ImportPart.ROW, 2, side=0, price=price, quantity=4, import_visible=4)
    yield import_command(ImportPart.COMMIT)
    yield operation(Operation.SNAPSHOT, 0)
    yield operation(Operation.CANCEL, 0, order_id=0, owner=1)
    yield import_command(ImportPart.ROW, 2, side=1, price=price, quantity=4, import_visible=4)
    yield import_command(ImportPart.COMMIT)
    yield (3, 40)
    yield operation(Operation.SNAPSHOT, 0)
    yield Command(order_id=0, price=price, quantity=4, owner=1)
    yield import_command(ImportPart.BEGIN, 1)
    yield import_command(ImportPart.ABORT)
    yield operation(Operation.SNAPSHOT, 0)
    yield (1, 100)
    yield (2, 0)
    for index, item in enumerate(management_examples(geometry)):
        if index == 11:
            yield (3, 44)
        yield item
    yield import_command(ImportPart.BEGIN, 0)
    yield manage(Management.REPLACE, owner=2, order_id=1, side=1, price=price, quantity=1)
    yield manage(Management.PREVIEW, owner=2)
    yield import_command(ImportPart.ABORT)
    yield manage(Management.CANCEL, owner=2)
    if geometry.prices > 1:
        yield Command(order_id=0, price=0, quantity=3, owner=1)
        yield Command(order_id=1, price=1, quantity=3, owner=2, side=1)
        yield manage(Management.REPLACE, order_id=0, side=0, price=1, quantity=2)
        yield operation(Operation.ORDER, 0, order_id=0, owner=1)
    yield (1, 100)
    yield (3, 40)
    yield manage(Management.PREVIEW, owner=1)
    yield (2, 0)


def weighted_command(rng, geometry, recent, shadow):
    """Use current model state to bias the stimulus towards useful transitions."""
    bank = rng.randrange(geometry.banks) if rng.randrange(40) else geometry.banks
    operation_code = rng.choices(
        list(Operation)[:6], weights=(58, 14, 11, 10, 7, 4),
    )[0]
    price = rng.randrange(geometry.prices)
    side = rng.randrange(2)
    owner = rng.randrange(min(1 << geometry.owner_width, 4))
    quantity = rng.randint(1, min(64, geometry.max_quantity))
    order_id = rng.randrange(geometry.slots)
    resting = shadow.books[bank].orders if bank < geometry.banks else {}
    if rng.randrange(9) == 0:
        mode = rng.choice(tuple(Management))
        if mode == Management.REPLACE and resting:
            order_id, row = rng.choice(list(resting.items()))
            owner = row.owner if rng.randrange(6) else owner
            side = row.side if rng.randrange(9) else side
            price = row.price if rng.randrange(4) else price
            quantity = rng.choice((quantity, row.total, max(1, row.total//2)))
            display = row.peak if rng.randrange(4) else rng.randrange(quantity+1)
            return manage(mode, bank, owner, order_id=order_id, side=side, price=price,
                          quantity=quantity, display=display)
        if mode == Management.REPLACE:
            return manage(mode, bank, owner, order_id=order_id, side=side, price=price, quantity=quantity)
        if resting and rng.randrange(4):
            row = rng.choice(list(resting.values()))
            owner, side, price = row.owner, row.side, row.price
        scope = rng.choice(tuple(Scope))
        return manage(mode, bank, owner, stp=scope, side=side if scope else 0,
                      price=price if scope >= Scope.LEVEL else 0)
    if operation_code in (Operation.CANCEL, Operation.REDUCE, Operation.ORDER):
        if resting and rng.randrange(6):
            order_id, order = rng.choice(list(resting.items()))
            if rng.randrange(10):
                owner = order.owner
            if operation_code == Operation.REDUCE and order.total > 1 and rng.randrange(6):
                quantity = rng.randrange(1, order.total)
    elif operation_code == Operation.NEW:
        free = set(range(geometry.slots)) - resting.keys()
        if free and rng.randrange(9):
            order_id = rng.choice(sorted(free))
        if resting and rng.randrange(3):
            _, maker = rng.choice(list(resting.items()))
            side = 1 - maker.side
            price = maker.price
            if rng.randrange(3) == 0:
                owner = maker.owner
        elif recent and rng.randrange(3) == 0:
            price = recent[-1].price % geometry.prices
    tif = rng.choices(list(TimeInForce), weights=(57, 18, 10, 15))[0]
    stp = rng.choices(list(Prevention), weights=(55, 24, 21))[0]
    display = rng.randint(1, quantity) if rng.randrange(2) else 0
    command = Command(bank, 0, operation_code, tif, stp, side, order_id, price,
                      quantity, display, owner=owner)
    if rng.randrange(20):
        return command
    name, value = rng.choice([
        ("operation", 7), ("order_id", geometry.slots), ("price", geometry.prices),
        ("quantity", 0), ("stp", 3),
    ])
    return replace(command, **{name: value})


def campaign(geometry, seed, random_commands):
    """Tag the command tape and include reset fences and sink-stall directives."""
    from reserve_model import ReserveMatcher

    rng = Random(seed)
    shadow = ReserveMatcher(geometry)
    recent = []
    tag = 0

    def transact(command, sequence):
        """Assign the audit tag and maintain the stimulus policy's state view."""
        tagged = replace(command, tag=sequence % (1 << geometry.tag_width))
        shadow.execute(tagged)
        recent.append(tagged)
        return tagged

    for entry in directed(geometry):
        if isinstance(entry, Command):
            yield transact(entry, tag)
            tag += 1
        else:
            yield entry
            if entry[0] == 2:
                shadow.reset()
                recent.clear()
    for i in range(random_commands):
        if i and i % 433 == 0:
            yield (2, 0)
            shadow.reset()
            recent.clear()
        elif i and i % 67 == 0:
            yield (3, rng.randrange(4, 65))
        command = weighted_command(rng, geometry, recent, shadow)
        yield transact(command, tag)
        tag += 1
