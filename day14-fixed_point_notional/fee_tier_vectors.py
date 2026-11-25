"""Build signed marginal fee and exact turnover expectations for RTL."""
from argparse import ArgumentParser
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
import random

from notional_model import U64, rounded_divide


@dataclass(frozen=True)
class Profile:
    revision: int = 0
    next_sequence: int = 0
    volume: int = 0
    armed: bool = False
    thresholds: tuple = ()
    maker: tuple = ()
    taker: tuple = ()


@dataclass(frozen=True)
class Control:
    venue: int
    op: int
    revision: int = 0
    sequence: int = 1
    volume: int = 0
    thresholds: tuple = ()
    maker: tuple = ()
    taker: tuple = ()


@dataclass(frozen=True)
class Fill:
    venue: int
    revision: int
    sequence: int
    price: int
    qty: int
    maker: bool
    sell: bool
    rounding: int


def packed_rows(values, width):
    return sum((value & ((1 << width)-1)) << (index*width)
               for index, value in enumerate(values))


def expected_control(profiles, command, revision_limit):
    if command.venue >= len(profiles):
        return 1, Profile()
    profile = profiles[command.venue]
    if command.op == 3:
        return 10, profile
    if command.op != 1 and profile.revision != command.revision:
        return 2, profile
    if command.op == 0:
        if profile.revision == revision_limit:
            return 4, profile
        upper = command.thresholds + (U64,)
        if any(left >= right for left, right in zip((0,)+upper, upper)):
            return 3, profile
        if any(abs(rate) > 10000 for rate in command.maker+command.taker):
            return 3, profile
        return 0, Profile(profile.revision+1, command.sequence, command.volume, True,
                          command.thresholds, command.maker, command.taker)
    if command.op == 2:
        return 0, replace(profile, armed=False)
    return 0, profile


def expected_fill(profiles, fill, fraction, seq_max):
    venue = fill.venue
    if venue >= len(profiles):
        return (1,0,0,0,0,0,0), Profile()
    profile = profiles[venue]
    failure = (2 if fill.revision != profile.revision else
               5 if not profile.armed else
               6 if fill.sequence < profile.next_sequence else
               7 if fill.sequence > profile.next_sequence else
               8 if fill.price == 0 or fill.qty == 0 or fill.rounding == 3 else 0)
    gross = rounded_divide(fill.price*fill.qty, 1 << fraction, fill.rounding) if not failure else None
    updated = (profile.volume + gross.amount) if gross is not None else profile.volume
    if updated > U64:
        failure = 9
    if failure:
        return (failure,0,0,0,profile.volume,0,0), profile

    allocations = [
        max(0, min(updated, high) - max(profile.volume, low))
        for low, high in zip((0,)+profile.thresholds, profile.thresholds+(U64+1,))
    ]
    assert sum(allocations) == gross.amount
    rates = profile.maker if fill.maker else profile.taker
    net_numerator = sum(rate*amount for rate, amount in zip(rates, allocations))
    rounded = rounded_divide(abs(net_numerator), 10000, fill.rounding)
    fee = -rounded.amount if net_numerator < 0 else rounded.amount
    cash = gross.amount - fee if fill.sell else gross.amount + fee
    next_profile = replace(profile, volume=updated, armed=(fill.sequence != seq_max),
                           next_sequence=min(seq_max, fill.sequence+1))
    return (0,gross.amount,fee,cash,updated,sum(x>0 for x in allocations),
            gross.inexact or rounded.inexact), next_profile


def tape(path, venues, tiers, fraction, count, seed, seq_width=5, rev_width=3):
    rng = random.Random(seed)
    states = [Profile() for _ in range(venues)]
    statuses = Counter()
    sequence_limit = (1 << seq_width)-1
    revision_limit = (1 << rev_width)-1
    starts = (3, 8, 12, 19, 33)
    thresholds = starts[:tiers-1]
    maker = (10000,-2000,3000,-100,1000,500)[:tiers]
    taker = (-10000,10000,-9000,8000,-7000,6000)[:tiers]
    policy = Control(0,0,sequence=1,volume=2,thresholds=thresholds,maker=maker,taker=taker)
    price = min(0xffffffff,6 << fraction)
    fill = Fill(0,1,1,price,2,True,False,2)
    directed = [
        policy, fill, replace(fill,sequence=2,maker=False,sell=True), fill,
        replace(fill,sequence=7), replace(fill,sequence=3,price=0),
        Control(venues,1), Control(0,3), Control(0,1),
        replace(policy,revision=1,volume=U64-1),
        replace(fill,revision=2,sequence=1,price=price,qty=40),
        replace(policy,revision=2,sequence=sequence_limit,volume=0),
        replace(fill,revision=3,sequence=sequence_limit,price=price,qty=1),
        replace(fill,revision=3,sequence=sequence_limit),
        replace(policy,revision=3,maker=(0,)*(tiers-1)+(10001,)),
        replace(policy,revision=0), replace(fill,venue=venues),
        replace(policy,revision=3,maker=(-10000,)*tiers, taker=(10000,)*tiers),
        replace(fill,revision=4,price=0xffffffff,qty=0xffffffff,maker=False),
        replace(policy,revision=4), replace(policy,revision=5), replace(policy,revision=6),
        replace(policy,revision=7), replace(fill,revision=7),
    ]

    def write(stream, index, event):
        if isinstance(event, Control):
            status, next_profile = expected_control(states, event, revision_limit)
            if event.venue < venues:
                states[event.venue] = next_profile
            fields = (0,event.op,event.venue,event.revision,event.sequence,event.volume,
                      packed_rows(event.thresholds,64),packed_rows(event.maker,16),
                      packed_rows(event.taker,16),status,next_profile.revision,
                      next_profile.next_sequence,next_profile.volume,next_profile.armed)
        else:
            result, next_profile = expected_fill(states,event,fraction,sequence_limit)
            if event.venue < venues:
                states[event.venue] = next_profile
            status,gross,fee,cash,volume,slices,inexact = result
            fields = (1,index,event.venue,event.revision,event.sequence,event.price,event.qty,
                      event.maker,event.sell,event.rounding,status,gross,fee & ((1<<65)-1),
                      cash,volume,slices,inexact)
        statuses[status] += 1
        stream.write(" ".join(f"{int(value):x}" for value in fields)+"\n")

    with path.open("w") as output:
        for index, event in enumerate(directed):
            write(output,index,event)
        for index in range(len(directed),len(directed)+count):
            venue = rng.randrange(venues)
            old = states[venue]
            if rng.random() < 0.28:
                thresholds = tuple(sorted(rng.sample(range(1, 2500),tiers-1)))
                rates = tuple(rng.randint(-10000, 10000) for _ in range(2*tiers))
                mode = rng.choice((0,0,0,1,2))
                updated = Control(venue,mode,old.revision,rng.randrange(10),rng.randrange(200),
                                  thresholds,rates[:tiers],rates[tiers:])
            else:
                sequence = (old.next_sequence if rng.random()<0.80 else rng.randint(0,sequence_limit))
                shift = min(fraction,24)
                px = rng.choice((0,1,(rng.randrange(765)<<shift) & 0xffffffff,rng.getrandbits(32)))
                qty = rng.choice((0,1,rng.randrange(2,22),rng.getrandbits(32)))
                updated = Fill(venue,old.revision,sequence,px,qty,bool(rng.randrange(2)),
                               bool(rng.randrange(2)),rng.randrange(3))
            write(output,index,updated)
    print(f"Tiered fee oracle q={fraction} tiers={tiers} venues={venues}, "
          f"{sum(statuses.values())} events, statuses={sorted(statuses)}")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--venues", type=int, required=True)
    parser.add_argument("--tiers", type=int, required=True, choices=range(1,7))
    parser.add_argument("--fraction", type=int, required=True)
    parser.add_argument("--count", type=int, default=275)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()
    tape(args.path,args.venues,args.tiers,args.fraction,args.count,args.seed)


if __name__ == "__main__":
    main()
