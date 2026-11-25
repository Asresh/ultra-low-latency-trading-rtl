"""Generate checked command, market and inventory quote handshakes."""
from argparse import ArgumentParser
from collections import Counter
from dataclasses import replace
from pathlib import Path
import random

from inventory_reference import Command, Manager, Op, Quote, Shape


def packed(*pairs):
    value = 0
    for field, width in pairs:
        value = (value << width) | (int(field) & ((1 << width)-1))
    return value


def packed_list(values, width):
    return packed(*((item, width) for item in reversed(values)))


def control_word(s, command):
    cmd = command or Command(Op.INSPECT, 0, 0)
    return packed(
        (command is not None, 1), (cmd.op, 2), (cmd.instrument, s.instrument),
        (cmd.tag, s.tag), (cmd.revision, s.rev), (cmd.sequence, s.seq),
        (cmd.sell, 1), (cmd.quantity, s.qty), (cmd.position, s.pos),
        (cmd.floor, s.pos), (cmd.ceiling, s.pos), (cmd.bid_cap, s.qty),
        (cmd.ask_cap, s.qty), (cmd.trigger, s.pos), (cmd.skew, s.price),
    )


def quote_word(s, quote):
    q = quote or Quote(0, False, False, 0, 0, 0, 0)
    return packed(
        (quote is not None, 1), (q.instrument, s.instrument),
        (q.active, 1), (q.current, 1), (q.epoch, s.epoch),
        (q.bid, s.price), (q.ask, s.price), (q.size, s.qty),
    )


def reply_word(s, reply):
    values = reply or (0,)*10
    widths = (2, s.instrument, s.tag, 4, 1, s.rev, s.seq, s.pos, 32, 32)
    return packed((reply is not None, 1), *zip(values, widths))


def result_word(s, plan, current):
    fields = ((0,)*9 if plan is None else (
        plan.instrument, plan.epoch, plan.revision, plan.bid, plan.ask,
        plan.bid_size, plan.ask_size, plan.position, plan.reason,
    ))
    widths = (s.instrument, s.epoch, s.rev, s.price, s.price, s.qty, s.qty, s.pos, 3)
    return packed((plan is not None, 1), (current, 1), *zip(fields, widths))


def random_command(s, model, rng, cycle):
    op = rng.choices(list(Op), weights=[3, 9, 2, 1], k=1)[0]
    i = rng.randrange(s.instruments if rng.random() < 0.93 else 1 << s.instrument)
    common = Command(op, i, cycle)
    if i >= s.instruments:
        return common
    old = model.books[i]
    revision = old.revision if rng.random() < 0.88 else rng.randrange(1 << s.rev)
    common = replace(common, revision=revision)
    seq_max = (1 << s.seq)-1
    if op == Op.INSTALL:
        return replace(common, sequence=rng.randint(0, seq_max),
                       position=rng.randint(-128, 127),
                       floor=rng.randint(-128, 4), ceiling=rng.randint(-4, 127),
                       bid_cap=rng.randrange(15), ask_cap=rng.randrange(15),
                       trigger=rng.randrange(84), skew=rng.randrange(s.levels+1))
    if op != Op.FILL:
        return common
    if old.last and rng.random() < 0.31:
        number, side, amount = old.last
        if rng.random() < 0.45:
            side = not side
    else:
        number = rng.choice([old.next_fill]*6+[rng.randint(0, seq_max)])
        side, amount = rng.randrange(2), rng.choice([0]+list(range(1, 16))*4)
    return replace(common, sequence=number, sell=side, quantity=amount)


def direction(cycle, s):
    first = Command(Op.INSTALL, 0, 1, sequence=1)
    fill = Command(Op.FILL, 0, 2, revision=1, sequence=1, quantity=6)
    seq_max = (1 << s.seq)-1
    commands = {
        2: first,
        10: fill,
        11: replace(fill, tag=3),
        20: replace(fill, tag=4, sequence=2, sell=True, quantity=3),
        23: replace(fill, tag=5, revision=0, sequence=3),
        25: fill,
        26: replace(first, instrument=(1 << s.instrument)-1, tag=6),
        32: replace(fill, tag=7, sequence=5),
        35: replace(first, tag=8, revision=1, position=126, sequence=4),
        38: replace(fill, tag=9, revision=2, sequence=4, quantity=2),
        40: replace(first, tag=10, revision=2, position=-9, sequence=seq_max),
        44: replace(fill, tag=11, revision=3, sequence=seq_max, sell=True, quantity=1),
        45: replace(fill, tag=12, revision=3, sequence=seq_max, sell=True, quantity=1),
        46: replace(fill, tag=13, revision=3),
        48: replace(first, tag=14, revision=3, floor=1),
        52: replace(first, tag=15, revision=3, sequence=5, position=5),
        54: replace(fill, tag=16, revision=4, sequence=5, quantity=0),
        58: replace(first, tag=17, revision=4),
        61: replace(fill, tag=18, revision=5, sequence=1, quantity=1),
        64: replace(fill, tag=19, revision=5, sequence=1, quantity=2),
        70: replace(first, tag=20, revision=5, bid_cap=0, ask_cap=0),
        73: replace(first, tag=21, revision=6, position=-1),
        75: replace(first, tag=22, revision=7),
    }
    quote = Quote(0, True, True, 1, 5, min(s.levels-1, 12), 9)
    quotes = {5: quote, 30: replace(quote, bid=14, ask=3),
              34: quote, 50: replace(quote, epoch=2), 55: replace(quote, epoch=2)}
    if s.instruments > 1:
        commands[3] = replace(first, instrument=1, position=-8, floor=-12)
        quotes[6] = replace(quote, instrument=1, bid=3, ask=min(s.levels-1, 6))
    return commands.get(cycle), quotes.get(cycle)


def write_tape(path, shape, cycles, seed):
    rng, model = random.Random(seed), Manager(shape)
    n = shape.instruments
    epochs, eligible, buy, sell = [1]*n, [True]*n, [0]*n, [0]*n
    to_send = None
    statuses, reasons, tallies = Counter(), Counter(), Counter()
    with path.open("w") as stream:
        for i in range(cycles):
            reset = i in (0, 438, 921)
            if reset:
                to_send = None
            r_ready = i < 80 or rng.randrange(5) != 0
            m_ready = i >= 19 and (i < 80 or rng.randrange(8) in (1, 2, 3))
            if i < 80:
                to_send, quote = direction(i, shape)
                buy[0] = 4 if 14 <= i < 28 else 0
                if i == 49:
                    eligible[0] = False
                    epochs[0] = 2
                if i == 51:
                    eligible[0] = True
            else:
                bank = rng.randrange(n)
                if rng.random() < 0.06:
                    eligible[bank] = not eligible[bank]
                if rng.random() < 0.03:
                    epochs[bank] = (epochs[bank]+1) % (1 << shape.epoch)
                if rng.random() < 0.21:
                    buy[bank], sell[bank] = (rng.choice([0]*4+[rng.randrange(1 << shape.reserve), 3, 7])
                                              for _ in range(2))
                if to_send is None and rng.random() < 0.64 and not reset:
                    to_send = random_command(shape, model, rng, i)
                quote = None
                if rng.random() < 0.65 and not reset:
                    bid = rng.randrange(shape.levels-2)
                    quote = Quote(bank, rng.random() < 0.8, rng.random() < 0.86,
                                  epochs[bank] - (rng.random() < 0.05),
                                  bid, rng.randint(bid+1, shape.levels-1) if rng.random() < 0.9
                                  else max(0, bid-1), rng.randrange(32))
                    quote = replace(quote, epoch=quote.epoch % (1 << shape.epoch))

            command_ready, reply, quote_ready, plan, current, sides_bid, sides_ask = model.step(
                reset, to_send, r_ready, quote, m_ready, eligible, epochs, buy, sell)
            row = (
                int(reset), int(r_ready), int(m_ready), control_word(shape, to_send),
                quote_word(shape, quote), packed_list(eligible, 1), packed_list(epochs, shape.epoch),
                packed_list(buy, shape.reserve), packed_list(sell, shape.reserve),
                int(command_ready), reply_word(shape, reply), int(quote_ready),
                result_word(shape, plan, current), packed_list(sides_bid, shape.qty), packed_list(sides_ask, shape.qty),
            )
            stream.write(" ".join(f"{value:x}" for value in row)+"\n")
            if to_send and command_ready:
                to_send = None
                tallies["commands"] += 1
            if reply and r_ready:
                statuses[int(reply[3])] += 1
            if plan:
                if m_ready and current:
                    reasons[int(plan.reason)] += 1
                tallies["waived"] += int(m_ready and not current)
                tallies["offer_hold"] += int(not m_ready)
    print(f"Inventory oracle {cycles} cycles, {tallies['commands']} commands, "
          f"{sum(reasons.values())} current and {tallies['waived']} waived transfers, "
          f"{tallies['offer_hold']} held, statuses={sorted(statuses)}, reasons={sorted(reasons)}")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--instruments", required=True, type=int)
    parser.add_argument("--levels", required=True, type=int)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--cycles", default=1700, type=int)
    args = parser.parse_args()
    if args.instruments < 1 or args.levels < 8:
        parser.error("fixture requires at least one instrument and eight levels")
    write_tape(args.path, Shape(args.instruments, args.levels), args.cycles, args.seed)


if __name__ == "__main__":
    main()
