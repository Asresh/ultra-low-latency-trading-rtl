"""Produce deterministic order tapes with integer-oracle replies for the RTL bench."""

import argparse
from collections import Counter
from dataclasses import replace
from pathlib import Path
import random

from notional_model import Kind, Request, Rounding, U64, calculate

MAX32 = (1 << 32) - 1


def inclusive_limit(order, frac):
    """Make a copy that accepts a cash value exactly equal to its limit."""
    result = calculate(order, frac)
    return replace(order, limit=result.cash)


def just_outside(order, frac):
    """Pick the closest failing limit when a representable one exists."""
    result = calculate(order, frac)
    limit = result.cash + (1 if order.sell else -1)
    return replace(order, limit=limit) if 0 <= limit <= U64 else order


def rounding_examples(frac):
    """Cover residues just below, at, and above half in both quotient parities."""
    scale = 1 << frac
    offsets = {0, max(0, scale // 2 - 1), scale // 2, min(scale - 1, scale // 2 + 1), scale - 1}
    for whole in range(4):
        for offset in sorted(offsets):
            price = whole * scale + offset
            if not 0 < price <= MAX32:
                continue
            for mode in Rounding:
                for sell in (False, True):
                    yield Request(0, price, 1, 5000, mode, sell)


def fee_examples(frac):
    """Exercise denominator residues at exact boundaries and ties for the fee."""
    scale = 1 << frac
    price = min(MAX32, scale)
    for amount in (1, 2, 3, 5, 17, 100, 101, 9999, 10000, 10001):
        for bps in (0, 1, 4999, 5000, 5001, 9999, 10000):
            for mode in Rounding:
                yield Request(0, price, amount, bps, mode, bool(amount % 2))


def validation_examples():
    """Exercise every input error code and precedence when faults overlap."""
    for price in (0, 1, MAX32):
        for qty in (0, 1, MAX32):
            for bps in (0, 10000, 10001, 65535):
                for mode in (Rounding.FLOOR, 3):
                    if price and qty and bps <= 10000 and mode != 3:
                        continue
                    yield Request(0, price, qty, bps, mode, bool(bps & 1), U64)


def boundary_orders(frac):
    """Check maximal operands and exact modal limits on both sides."""
    for px, qty in ((MAX32, MAX32), (MAX32 - 1, MAX32), (1, MAX32), (MAX32, 1)):
        for fee in (0, 1, 9900, 10000):
            for sell in (False, True):
                for rounding in Rounding:
                    order = Request(0, px, qty, fee, rounding, sell)
                    yield order
                    yield inclusive_limit(order, frac)
                    yield just_outside(order, frac)


def random_orders(frac, seed, count):
    """Produce diverse, reproducible limits, faults, and multiplications."""
    rng = random.Random(seed)
    for _ in range(count):
        price = rng.choice((rng.randint(1, MAX32), rng.randint(1, 65535), MAX32))
        qty = rng.choice((rng.randint(1, MAX32), rng.randint(1, 65535), 1))
        fee = rng.randrange(10001)
        mode = rng.randrange(3)
        order = Request(0, price, qty, fee, mode, bool(rng.getrandbits(1)))
        variant = rng.randrange(13)
        if variant == 0:
            order = replace(order, quantity=0)
        elif variant == 1:
            order = replace(order, price=0)
        elif variant == 2:
            order = replace(order, fee_bps=rng.randrange(10001, 65536))
        elif variant == 3:
            order = replace(order, rounding=3)
        elif variant in (4, 5):
            order = inclusive_limit(order, frac)
        elif variant in (6, 7):
            order = just_outside(order, frac)
        elif variant == 8:
            order = replace(order, limit=rng.getrandbits(64))
        yield order


def cases(frac, seed, count):
    """Yield directed families before seeded cases with reused tags."""
    directed = (
        validation_examples(),
        rounding_examples(frac),
        fee_examples(frac),
        boundary_orders(frac),
        random_orders(frac, seed, count),
    )
    index = 0
    for family in directed:
        for order in family:
            yield replace(order, tag=index % 97)
            index += 1


def model_latency(status):
    """State the first possible output handshake for an unstalled request."""
    bad_fields = (Kind.BAD_QUANTITY, Kind.BAD_PRICE, Kind.BAD_FEE, Kind.BAD_MODE)
    return 1 if status in bad_fields else 85


def encode(fields):
    """Use a compact all-hex fixture schema that SV fscanf can read."""
    return " ".join(f"{int(word):x}" for word in fields)


def write_tape(path, frac, seed, count):
    """Write request plus expected reply fields and return status coverage."""
    summary = Counter()
    with path.open("w", encoding="ascii") as target:
        for request in cases(frac, seed, count):
            reply = calculate(request, frac)
            summary[reply.status.name] += 1
            target.write(encode((*request.wire(), *reply.wire(), model_latency(reply.status))) + "\n")
    return dict(summary)


def main():
    """Generate integrated tapes for integer, Q24.8 and extreme fractional prices."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", type=Path)
    ap.add_argument("--random", type=int, default=650)
    args = ap.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    for frac, seed in ((0, 565), (8, 14008), (31, 198983)):
        summary = write_tape(args.directory / f"q{frac}.txt", frac, seed, args.random)
        print(f"Q frac={frac}: {sum(summary.values())} cases {summary}")


if __name__ == "__main__":
    main()
