"""Build arithmetic corners and seeded exact-ratio divider inputs."""

import argparse
from fractions import Fraction
from pathlib import Path
import random


U64 = (1 << 64) - 1


def cases(seed, count):
    yield from (
        (0, 0), (1, 0), (U64, 0), (0, 1), (0, U64), (1, U64),
        (1, 1 << 33), (3, 1 << 33), (5, 1 << 33), (7, 1 << 33),
        (U64, (1 << 32) - 1), (U64, 1 << 32), (U64, (1 << 32) + 1),
        ((1 << 32) - 1, 1), (1 << 32, 1), (1, 3), (1, 7),
        (1_000_000_000, 125_000_000), (1_000_000_000, 125_000_123),
    )
    rng = random.Random(seed)
    for i in range(count):
        if i % 4 == 0:
            yield (rng.randrange(1 << 64), rng.randrange(1 << 31))
        elif i % 4 == 1:
            yield (rng.randrange(1 << 32), rng.randrange(1 << 64))
        elif i % 4 == 2:
            yield (rng.randrange(1 << 64), rng.randrange(1 << 64))
        else:
            ns = rng.randrange(1 << 28)
            ticks = rng.randrange(1, 1 << 28)
            yield ns, ticks


def expectation(ns, ticks):
    result = round(Fraction(ns << 32, ticks)) if ticks else 0
    return result if result <= U64 else 0, int(result > U64), int(ticks == 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=19853)
    parser.add_argument("--count", type=int, default=200)
    args = parser.parse_args()
    if args.count < 0:
        parser.error("case count must be nonnegative")
    with args.output.open("w", encoding="ascii") as stream:
        for ns, ticks in cases(args.seed, args.count):
            fields = (ns, ticks, *expectation(ns, ticks))
            stream.write(" ".join(format(value, "x") for value in fields) + "\n")


if __name__ == "__main__":
    main()
