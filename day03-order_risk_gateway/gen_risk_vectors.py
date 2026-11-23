"""Write independently scored directed and randomized SystemVerilog vectors."""

from __future__ import annotations

import argparse
import random
from collections import Counter
from dataclasses import replace
from pathlib import Path

from risk_model import Key, Operation as Op, Policy, PROFILES, Scenario, Status as Code


BUY = Key(0, 0, 0)
SELL = Key(0, 0, 1)
OTHER_ACCOUNT = Key(1, 0, 0)
OTHER_INSTRUMENT = Key(0, 1, 0)


def clinic(s: Scenario) -> None:
    """Show that unrelated sides, instruments, and accounts are isolated."""
    s.order(Op.NEW, BUY, 0, price=20, qty=10, expected=Code.DISABLED)
    s.order(7, BUY, 0, expected=Code.BAD_OP)
    s.order(Op.NEW, Key(3, 0, 0), 0, price=20, qty=1, expected=Code.BAD_KEY)
    s.order(Op.SNAPSHOT, Key(0, 3, 0), 0, expected=Code.BAD_KEY)
    s.order(Op.SNAPSHOT, BUY, 7, expected=Code.BAD_SLOT)
    s.order(Op.NEW, BUY, 0, generation=1, price=20, qty=1, expected=Code.BAD_FIELDS)
    s.order(Op.NEW, BUY, 0, event=1, price=20, qty=1, expected=Code.BAD_FIELDS)
    s.order(Op.NEW, BUY, 0, price=20, qty=0, expected=Code.BAD_FIELDS)

    guarded = Policy(1, 10, 60, 25, 1500, 2)
    invalid = replace(guarded, min_price=70)
    s.configure(BUY, invalid)
    s.configure(Key(3, 0, 0), guarded)
    s.order(Op.NEW, BUY, 0, price=20, qty=10, expected=Code.DISABLED)
    s.configure(BUY, replace(guarded, max_open_orders=6))
    s.order(Op.NEW, BUY, 0, price=20, qty=10, expected=Code.DISABLED)
    for key in (BUY, SELL, OTHER_ACCOUNT, OTHER_INSTRUMENT):
        s.configure(key, guarded)
    a = s.order(Op.NEW, BUY, 0, price=20, qty=10, expected=Code.OK)
    assert a.generation == 1 and a.reserved == 200 and a.open_orders == 1
    s.order(Op.NEW, BUY, 0, price=20, qty=1, expected=Code.SLOT_BUSY)
    s.order(Op.NEW, BUY, 1, price=9, qty=1, expected=Code.PRICE_LIMIT)
    s.order(Op.NEW, BUY, 1, price=61, qty=1, expected=Code.PRICE_LIMIT)
    s.order(Op.NEW, BUY, 1, price=25, qty=26, expected=Code.QTY_LIMIT)
    s.order(Op.NEW, BUY, 1, price=60, qty=25, expected=Code.OPEN_NOTIONAL)
    b = s.order(Op.NEW, BUY, 1, price=60, qty=20, expected=Code.OK)
    assert b.open_notional == 1400 and b.open_orders == 2
    s.order(Op.NEW, BUY, 2, price=10, qty=1, expected=Code.OPEN_COUNT)
    c = s.order(Op.NEW, SELL, 2, price=10, qty=2, expected=Code.OK)
    d = s.order(Op.NEW, OTHER_ACCOUNT, 3, price=10, qty=2, expected=Code.OK)
    e = s.order(Op.NEW, OTHER_INSTRUMENT, 4, price=10, qty=2, expected=Code.OK)
    assert c.open_notional == d.open_notional == e.open_notional == 20

    # A non-owner sees its own ledger and no metadata about the occupied slot.
    cross = s.order(Op.SNAPSHOT, OTHER_ACCOUNT, 0, expected=Code.OK)
    assert cross.open_orders == 1 and cross.generation == 0 and cross.live == 0
    s.order(Op.CANCEL, SELL, 0, a.generation, 1, expected=Code.WRONG_OWNER)
    s.order(Op.FILL, BUY, 0, a.generation ^ 1, 1, 20, 1, Code.STALE_GENERATION)
    s.order(Op.FILL, BUY, 0, a.generation, 2, 20, 1, Code.EVENT_SEQUENCE)
    s.order(Op.FILL, BUY, 0, a.generation, 1, 20, 11, Code.OVERFILL)
    s.order(Op.FILL, BUY, 0, a.generation, 1, 0, 1, Code.BAD_FIELDS)
    x = s.order(Op.FILL, BUY, 0, a.generation, 1, 19, 4, Code.OK)
    assert (x.leaves, x.reserved, x.open_notional, x.executed_notional) == (6, 120, 1320, 76)
    s.order(Op.FILL, BUY, 0, a.generation, 1, 19, 4, Code.EVENT_SEQUENCE)
    s.order(Op.REJECT, BUY, 0, a.generation, 2, expected=Code.REJECT_AFTER_FILL)
    s.order(Op.AMEND, BUY, 0, a.generation, 2, 61, 1, Code.PRICE_LIMIT)
    s.order(Op.AMEND, BUY, 0, a.generation, 2, 60, 6, Code.OPEN_NOTIONAL)
    x = s.order(Op.AMEND, BUY, 0, a.generation, 2, 20, 15, Code.OK)
    assert (x.leaves, x.reserved, x.open_orders, x.open_notional) == (15, 300, 2, 1500)
    s.order(Op.CANCEL, BUY, 0, a.generation, 3, 1, 0, Code.BAD_FIELDS)
    x = s.order(Op.CANCEL, BUY, 0, a.generation, 3, expected=Code.OK)
    assert x.open_orders == 1 and x.open_notional == 1200 and x.executed_notional == 76
    s.order(Op.CANCEL, BUY, 0, a.generation, 3, expected=Code.NOT_LIVE)

    # Lowering a policy acts prospectively. Reconciliation survives a disabled key.
    s.configure(BUY, replace(guarded, max_open_notional=100))
    s.order(Op.NEW, BUY, 0, price=10, qty=1, expected=Code.OPEN_NOTIONAL)
    s.configure(BUY, replace(guarded, enable=0))
    s.order(Op.AMEND, BUY, 1, b.generation, 1, 20, 1, Code.DISABLED)
    s.order(Op.FILL, BUY, 1, b.generation, 1, 80, 1, Code.OK)
    s.order(Op.CANCEL, BUY, 1, b.generation, 2, expected=Code.OK)
    s.order(Op.REJECT, SELL, 2, c.generation, 1, expected=Code.OK)
    s.order(Op.REJECT, OTHER_ACCOUNT, 3, d.generation, 1, expected=Code.OK)
    s.order(Op.REJECT, OTHER_INSTRUMENT, 4, e.generation, 1, expected=Code.OK)


def exhausted_handles(s: Scenario) -> None:
    s.reset()
    s.configure(BUY, Policy(1, 1, 80, 80, 5000, 5))
    for generation in range(1, s.profile.last_generation + 1):
        order = s.order(Op.NEW, BUY, 0, price=10, qty=10, expected=Code.OK)
        assert order.generation == generation and order.event == 0
        if generation > 1:
            s.order(Op.REJECT, BUY, 0, generation - 1, 1, expected=Code.STALE_GENERATION)
        closed = s.order(Op.REJECT, BUY, 0, generation, 1, expected=Code.OK)
        assert not closed.live and closed.generation == generation and closed.event == 1
    s.order(Op.NEW, BUY, 0, price=10, qty=5, expected=Code.GENERATION_EXHAUSTED)

    order = s.order(Op.NEW, BUY, 1, price=8, qty=4, expected=Code.OK)
    last = s.profile.last_event
    for seq in range(1, last):
        s.order(Op.AMEND, BUY, 1, order.generation, seq, 9 + (seq & 1), 4, Code.OK)
    s.order(Op.AMEND, BUY, 1, order.generation, last, 10, 5, Code.EVENT_EXHAUSTED)
    s.order(Op.FILL, BUY, 1, order.generation, last, 10, 1, Code.EVENT_EXHAUSTED)
    closed = s.order(Op.FILL, BUY, 1, order.generation, last, 10, 4, Code.OK)
    assert closed.event == last and not closed.live and closed.executed_notional == 40


def arithmetic_limits(s: Scenario) -> None:
    """Exercise full-width notional, exact ceilings, and sums with a carry."""
    s.reset()
    p = s.profile
    widest = Policy(1, 1, p.max_price, p.max_qty, p.max_notional, p.slots)
    s.configure(BUY, widest)
    s.configure(SELL, widest)
    a = s.order(Op.NEW, BUY, 0, price=p.max_price, qty=p.max_qty, expected=Code.OK)
    assert a.reserved == p.max_price * p.max_qty
    s.order(Op.NEW, BUY, 1, price=p.max_price, qty=3, expected=Code.OPEN_NOTIONAL)
    b = s.order(Op.NEW, BUY, 1, price=p.max_price, qty=1, expected=Code.OK)
    c = s.order(Op.NEW, BUY, 2, price=p.max_qty, qty=1, expected=Code.OK)
    assert c.open_notional == p.max_notional
    s.order(Op.NEW, BUY, 3, price=1, qty=1, expected=Code.OPEN_NOTIONAL)
    s.order(Op.FILL, BUY, 0, a.generation, 1, p.max_price, p.max_qty, Code.OK)
    s.order(Op.FILL, BUY, 1, b.generation, 1, p.max_price, 1, Code.OK)
    c = s.order(Op.FILL, BUY, 2, c.generation, 1, p.max_qty, 1, Code.OK)
    assert c.open_notional == 0 and c.executed_notional == p.max_notional
    d = s.order(Op.NEW, BUY, 3, price=1, qty=2, expected=Code.OK)
    last = p.last_event
    for event in range(1, last):
        s.order(Op.AMEND, BUY, 3, d.generation, event, 1, 2, Code.OK)
    # A nonterminal fill cannot consume the final sequence or latch overflow.
    late_partial = s.order(Op.FILL, BUY, 3, d.generation, last, 1, 1, Code.EVENT_EXHAUSTED)
    assert late_partial.event == last - 1 and late_partial.leaves == 2
    e = s.order(Op.NEW, SELL, 4, price=5, qty=4, expected=Code.OK)
    overflow = s.order(Op.FILL, BUY, 3, d.generation, last, 1, 2, Code.EXEC_OVERFLOW)
    assert overflow.live and overflow.event == last - 1 and overflow.executed_notional == p.max_notional
    s.order(Op.NEW, SELL, 1, price=5, qty=1, expected=Code.HALTED)
    s.order(Op.AMEND, SELL, 4, e.generation, 1, 5, 1, Code.HALTED)
    s.order(Op.FILL, SELL, 4, e.generation, 1, 4, 1, Code.OK)
    s.order(Op.CANCEL, SELL, 4, e.generation, 2, expected=Code.OK)
    s.order(Op.CANCEL, BUY, 3, d.generation, last, expected=Code.OK)


def emergency_stop(s: Scenario) -> None:
    s.reset()
    s.configure(BUY, Policy(1, 1, 60, 50, 700, 5))
    a = s.order(Op.NEW, BUY, 0, price=5, qty=5, expected=Code.OK)
    s.halt()
    s.order(Op.NEW, BUY, 1, price=5, qty=5, expected=Code.HALTED)
    s.order(Op.AMEND, BUY, 0, a.generation, 1, 5, 5, Code.HALTED)
    s.order(Op.FILL, BUY, 0, a.generation, 1, 10, 2, Code.OK)
    s.order(Op.CANCEL, BUY, 0, a.generation, 2, expected=Code.OK)
    s.reset()
    s.order(Op.SNAPSHOT, BUY, 0, expected=Code.OK)
    s.order(Op.NEW, BUY, 0, price=5, qty=5, expected=Code.DISABLED)


def key_space(s: Scenario) -> list[Key]:
    return [
        Key(account, instrument, side)
        for account in range(s.profile.accounts)
        for instrument in range(s.profile.instruments)
        for side in range(2)
    ]


def random_policy(s: Scenario, rng: random.Random) -> Policy:
    lo = rng.randint(1, 7)
    hi = rng.randint(60, min(s.profile.max_price, 180))
    return Policy(
        int(rng.random() >= 0.13), lo, hi,
        rng.randint(6, 58), rng.randint(50, 8500), rng.randint(1, s.profile.slots),
    )


def drive_live(s: Scenario, rng: random.Random, slot: int) -> None:
    target = s.model.orders[slot]
    op = rng.choices(
        (Op.FILL, Op.CANCEL, Op.REJECT, Op.AMEND, Op.SNAPSHOT, Op.NEW),
        weights=(35, 15, 9, 26, 10, 5),
    )[0]
    if op is Op.SNAPSHOT:
        s.order(op, target.key, slot)
        return
    if op is Op.NEW:
        s.order(op, target.key, slot, price=target.price, qty=target.qty)
        return
    key = target.key
    generation = target.generation
    sequence = target.event + 1
    if rng.random() < 0.07:
        key = Key(key.account, key.instrument, 1 - key.sell)
    if rng.random() < 0.09:
        generation ^= 1
    if rng.random() < 0.1:
        sequence = target.event
    if op == Op.FILL:
        price = rng.randint(1, min(s.profile.max_price, 190))
        qty = rng.randint(1, target.qty + (3 if rng.random() < 0.14 else 0))
    elif op == Op.AMEND:
        price = rng.randint(1, min(s.profile.max_price, 190))
        qty = rng.randint(1, 55)
    else:
        price = 0
        qty = 0
    s.order(op, key, slot, generation, sequence, price, qty)


def drive_free(s: Scenario, rng: random.Random, slot: int, keys: list[Key]) -> None:
    op = rng.choices((Op.NEW, Op.FILL, Op.SNAPSHOT, 6), weights=(78, 6, 13, 3))[0]
    key = rng.choice(keys)
    if op == Op.NEW:
        if rng.random() < 0.1:
            key = Key(3, key.instrument, key.sell)
        if rng.random() < 0.06:
            slot = 7
        s.order(op, key, slot, price=rng.randint(1, 130), qty=rng.randint(1, 40))
    elif op == Op.FILL:
        s.order(op, key, slot, generation=1, event=1, price=13, qty=1)
    else:
        s.order(op, key, slot)


def fuzz(s: Scenario, seed: int, count: int) -> None:
    rng = random.Random(seed)
    keys = key_space(s)
    for i in range(count):
        if i % 280 == 0:
            s.reset()
            for key in keys:
                s.configure(key, random_policy(s, rng))
        elif rng.random() < 0.045:
            s.configure(rng.choice(keys), random_policy(s, rng))
        occupied = [slot for slot, order in s.model.orders.items() if order.live]
        if occupied and rng.random() < 0.68:
            drive_live(s, rng, rng.choice(occupied))
        else:
            free = [slot for slot in range(s.profile.slots) if slot not in occupied]
            drive_free(s, rng, rng.choice(free or list(range(s.profile.slots))), keys)
    s.drain()


def report(s: Scenario) -> None:
    print(f"{s.profile.name}: {len(s.stimulus)} actions, {len(s.expected)} replies")
    print("Ops:", " ".join(f"{kind.name.lower()}={count}" for kind, count in sorted(s.operations.items())))
    summaries = (f"{status.name.lower()}={amount}" for status, amount in sorted(s.codes.items()))
    print("Codes:", " ".join(summaries))


def check_coverage(codes: Counter[Code]) -> None:
    reachable = set(Code) - {Code.LEDGER_INVARIANT}
    uncovered = sorted(reachable - set(codes))
    if uncovered:
        names = ", ".join(status.name for status in uncovered)
        raise ValueError(f"generator failed to exercise {names}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--output", metavar="DIRECTORY", type=Path, required=True)
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=0xD03A11)
    parser.add_argument("--operations", type=int, default=3600)
    args = parser.parse_args()
    if not 0 <= args.operations <= 60_000:
        parser.error("--operations must be between 0 and 60000 for 16-bit transaction tags")
    scenario = Scenario(PROFILES[args.profile])
    clinic(scenario)
    exhausted_handles(scenario)
    arithmetic_limits(scenario)
    emergency_stop(scenario)
    fuzz(scenario, args.seed, args.operations)
    check_coverage(scenario.codes)
    scenario.write(args.output)
    report(scenario)


if __name__ == "__main__":
    main()
