"""Build deterministic active-bank, staging-bank, and output pin comparisons."""

import argparse
from pathlib import Path
import random

from banked_replay_model import BankedReplay, CANCEL, INSPECT, Port, Request
from replay_program import Command, Operation, U64


def pack(*fields):
    word = 0
    for value, bits in fields:
        if not 0 <= value < 1 << bits:
            raise ValueError(f"{value} exceeds {bits} bits")
        word = (word << bits) | value
    return word


def append(tick=0, tag=0xA1, last=True, keep=0xFF, data=0x8899AABBCCDDEEFF):
    return Command(Operation.APPEND, tick=tick, data=data, keep=keep, last=last, tag=tag)


def send(model, command, bank=0, run_id=0, release=0, ready=True, reply_ready=True):
    port = Port(Request(command, bank, run_id, release), ready=ready, reply_ready=reply_ready)
    while not model.observe(port).command_ready:
        yield port
    yield port


def idle(cycles, ready=True, reply_ready=True):
    yield from (Port(ready=ready, reply_ready=reply_ready) for _ in range(cycles))


def directed(model):
    yield Port(reset=True)
    yield from send(model, Command.start())
    yield from send(model, append(last=False))
    yield from send(model, Command.start())
    yield from send(model, append(tick=15, tag=0xA2))
    yield from send(model, append(tick=16, keep=0x7))
    yield from send(model, Command.start(repeats=2, gap=2), release=model.wall + 7, ready=False)
    yield from send(model, Command(INSPECT), ready=False)
    while model.owner is None:
        yield Port(ready=False)
    yield from send(model, append(tag=0xB1, keep=0x1), 1, ready=False)
    yield from send(model, Command.start(repeats=2, gap=1), 1, release=model.wall + 1, ready=False)
    yield from send(model, Command(Operation.CLEAR), 1, ready=False)
    yield from send(model, Command.start(), 1, ready=False)
    yield from send(model, Command(Operation.STOP), run_id=2, ready=False)
    yield from send(model, Command(Operation.PAUSE), run_id=1, ready=False)
    yield from idle(4, ready=False, reply_ready=False)
    yield from send(model, Command(Operation.RESUME), run_id=1, ready=False)
    yield from send(model, Command(Operation.STOP), run_id=1, ready=False)
    yield from idle(3, ready=False)
    yield from idle(12)
    yield from send(model, Command(INSPECT), 0)
    yield from send(model, Command(INSPECT), 1)
    yield from send(model, Command.start(repeats=0), 0)

    target_tick = model.wall + 5
    yield from send(model, Command.start(), 0, release=target_tick)
    yield from send(model, Command(CANCEL), 0, run_id=2)
    while model.wall < target_tick:
        yield Port()
    yield from send(model, Command(CANCEL), 0, run_id=3)
    yield from idle(4)
    if model.max_id < 10:
        for token in range(4, model.max_id + 1):
            yield from send(model, Command.start(), 1, release=model.wall + 25)
            yield from send(model, Command(CANCEL), 1, run_id=token)
        yield from send(model, Command.start(), 1)
    yield from send(model, Command(Operation.CLEAR))
    yield from send(model, Command.start())
    yield Port(reset=True)


def random_request(model, rng):
    bank = rng.randrange(2)
    op = rng.choices(range(8), (30, 7, 18, 5, 5, 10, 9, 16))[0]
    image = model.banks[bank].image
    token = model.snapshot(bank)[1]
    run_id = token if rng.random() < 0.8 else rng.randrange(min(20, model.max_id + 1))
    release = model.wall + rng.choice((-1, 0, 1, 2, 10, 25)) if model.wall > 0 else 0
    if op == Operation.APPEND:
        previous = image[-1] if image else None
        tick = previous.tick if previous else 0
        tick = max(0, tick + rng.choice((-1, 0, 1, 2, 4)))
        tag = previous.tag if previous and not previous.last else rng.randrange(1 << 16)
        if rng.random() < 0.15:
            tag ^= 1
        last = rng.random() < 0.76
        keep = rng.choice((0, 1, 7, 0xFF, 0xFA)) if rng.random() < 0.42 else 0xFF
        cmd = append(tick, tag, last, keep, rng.randrange(U64 + 1))
    elif op == Operation.START:
        cmd = Command.start(rng.choice((0, 1, 1, 1, 2, 3)), rng.choice((0, 1, 3, 8)))
    else:
        cmd = Command(op)
    return Request(cmd, bank, run_id, release)


def random_ports(model, seed, count):
    rng = random.Random(seed)
    request = None
    for _ in range(count):
        if request is None and rng.random() < 0.8:
            request = random_request(model, rng)
        reset = rng.random() < 0.009
        port = Port(request, reset, rng.random() < 0.62, rng.random() < 0.58)
        if reset or model.observe(port).command_ready:
            request = None
        yield port
    if request:
        yield from send(model, request.command, request.bank, request.run_id, request.release)
    yield Port(reset=True)
    yield Port()


def row(port, pins, run_width, count_width):
    request = port.request or Request(Command(INSPECT))
    cmd = request.command
    reply, event, retirement = pins.reply, pins.offer, pins.retirement
    return " ".join(f"{word:x}" for word in (
        pack((port.reset, 1), (port.request is not None, 1), (port.reply_ready, 1), (port.ready, 1)),
        pack((cmd.op, 3), (request.bank, 1), (request.run_id, run_width), (cmd.tick, 32),
             (cmd.data, 64), (cmd.keep, 8), (cmd.last, 1), (cmd.tag, 16), (cmd.repeats, 16),
             (cmd.gap, 32), (request.release, 64)),
        pack((pins.command_ready, 1), (reply is not None, 1), (event is not None, 1),
             (pins.active, 1), (pins.paused, 1), (pins.started, 1), (pins.retired, 1)),
        0 if reply is None else pack(
            (reply.status, 4), (reply.op, 3), (reply.bank, 1), (reply.run_id, run_width),
            (reply.state, 2), (reply.length, count_width), (reply.image_open, 1),
        ),
        0 if event is None else pack(
            (event.event.data, 64), (event.event.keep, 8), (event.event.last, 1),
            (event.event.tag, 16), (event.due, 48), (event.offered, 48), (event.pass_number, 16),
        ),
        pack((pins.bank, 1), (pins.run_id, run_width)),
        pack((pins.now, 48), (pins.wall, 64), (pins.start_tick, 64)),
        0 if retirement is None else pack(
            (retirement.bank, 1), (retirement.run_id, run_width), (retirement.stopped, 1),
            (retirement.truncated, 1), (retirement.tick, 64), (retirement.stats.beats, 32),
            (retirement.stats.packets, 32), (retirement.stats.late_beats, 32), (retirement.stats.passes, 32),
        ),
        pack(*((counter, 32) for counter in pins.counters)),
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--run-width", type=int, default=3)
    parser.add_argument("--seed", type=int, default=91027)
    parser.add_argument("--cycles", type=int, default=6500)
    args = parser.parse_args()
    if args.depth < 2 or args.run_width < 3 or args.cycles < 0:
        parser.error("directed fixtures need depth ≥ 2, run width ≥ 3, and nonnegative cycles")
    model = BankedReplay(args.depth, args.run_width)
    with args.path.open("w", encoding="ascii") as output:
        for sequence in (directed(model), random_ports(model, args.seed, args.cycles)):
            for port in sequence:
                pins = model.advance(port)
                output.write(row(port, pins, args.run_width, args.depth.bit_length()) + "\n")


if __name__ == "__main__":
    main()
