"""Produce exact multi-leg cash expectations with unbounded Python integers."""
import argparse
import random
from pathlib import Path

from notional_model import Kind, Request, U64, calculate


def rows(path, maximum, fraction, count, seed):
    rng = random.Random(seed)
    count_width = (maximum + 1).bit_length()
    acc_width = 65 + (maximum - 1).bit_length()
    no_bad_leg = (1 << count_width) - 1
    debit = Request(0, 0xFFFFFFFF, 0xFFFFFFFF)
    credit = Request(0, 0xFFFFFFFF, 0xFFFFFFFF, sell=True)
    invalid = Request(0, 0, 0)
    rounded = Request(0, 129, 3, 17, 2)
    overflow = Request(0, 0xFFFFFFFF, 0xFFFFFFFF, 10000, 2)
    sample = [
        (1, U64, None, [rounded]),
        (1, 0, None, [debit]),
        (1, None, U64, [credit]),
        (1, None, 1, [debit]),
        (1, 0, 0, [credit]),
        (1, None, None, [invalid]),
        (1, None, None, [overflow]),
        (0, None, None, [rounded] * (maximum + 4)),
        (maximum + 1, None, None, [rounded]),
        (maximum, None, None, [debit] * maximum),
        (maximum, None, None, [credit] * maximum),
        (maximum, None, None, [rounded] * (maximum - 1) + [invalid, credit]),
        (maximum, None, None, [credit] * (maximum - 1) + [debit]),
    ]
    if maximum > 1:
        paired_sale = Request(0, rounded.price, rounded.quantity, sell=True)
        sample.insert(0, (2, 0, 0, [Request(0, rounded.price, rounded.quantity), paired_sale]))
        sample += [(maximum, None, None, [debit]), (2, 0, None, [debit, credit])]

    for index in range(count):
        length = rng.randint(1, maximum)
        declared = rng.choice([length] * 8 + [0, maximum + 1, rng.randint(1, maximum)])
        legs = [
            Request(0, rng.choice([rng.randrange(1 << 32), rng.randrange(257)]),
                    rng.choice([rng.randrange(1 << 32), rng.randrange(257)]),
                    rng.randrange(10001 + (index % 19 == 0)),
                    rng.randrange(3 + (index % 11 == 0)), bool(rng.randrange(2)))
            for _ in range(length)
        ]
        if index % 17 == 0:
            legs.append(invalid)
        debit_limit = None if index & 1 else rng.randrange(1 << 64)
        credit_limit = None if index & 2 else rng.randrange(1 << 64)
        sample.append((declared, debit_limit, credit_limit, legs))

    with path.open("w") as stream:
        def emit(*fields):
            stream.write(" ".join(f"{int(value):x}" for value in fields) + "\n")

        for index, (declared, debit_limit, credit_limit, legs) in enumerate(sample):
            tag = index % 15
            replies = [calculate(leg, fraction) for leg in legs[:declared]] if 1 <= declared <= maximum else []
            gross = sum(reply.gross for reply in replies if reply.status == Kind.OK)
            fee = sum(reply.fee for reply in replies if reply.status == Kind.OK)
            cash = sum(reply.cash * (-1 if reply.sell else 1)
                       for reply in replies if reply.status == Kind.OK)
            inexact = any(reply.inexact for reply in replies if reply.status == Kind.OK)
            bad_index, leg_status = next(
                ((leg, reply.status) for leg, reply in enumerate(replies) if reply.status != Kind.OK),
                (no_bad_leg, Kind.OK),
            )
            if not 1 <= declared <= maximum or len(legs) != declared:
                status = 1
            elif leg_status != Kind.OK:
                status = 2
            elif debit_limit is not None and cash > debit_limit:
                status = 3
            elif credit_limit is not None and cash > -credit_limit:
                status = 4
            else:
                status = 0
            emit(tag, declared, debit_limit is not None, debit_limit or 0,
                 credit_limit is not None, credit_limit or 0, len(legs))
            for leg in legs:
                emit(leg.price, leg.quantity, leg.fee_bps, leg.rounding, leg.sell)
            emit(status, min(len(legs), maximum + 1), bad_index, leg_status, gross, fee,
                 cash & ((1 << acc_width) - 1), inexact)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--maximum", type=int, required=True)
    parser.add_argument("--fraction", type=int, required=True)
    parser.add_argument("--count", type=int, default=220)
    parser.add_argument("--seed", type=int, default=440601)
    options = parser.parse_args()
    if options.maximum < 1 or options.count < 0:
        parser.error("maximum must be positive and count must be nonnegative")
    rows(options.output, options.maximum, options.fraction, options.count, options.seed)


if __name__ == "__main__":
    main()
