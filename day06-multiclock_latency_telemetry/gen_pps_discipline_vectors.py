"""Build reproducible clock fault and holdover campaigns for Icarus."""

import argparse
from dataclasses import replace
from pathlib import Path
import random

from latency_reference import LIMIT64
from pps_discipline_model import Command, Discipline, Inputs, Op, Policy, Q32


POLICY = Policy(True, 1000, 85, 155, 480, 38, 3, 6*Q32, 12*Q32, Q32//16, 2)


def pack(*fields):
    word = 0
    for value, width in fields:
        if not 0 <= value < (1 << width):
            raise ValueError(f"{value} exceeds unsigned {width} bits")
        word = (word << width) | value
    return word


class RawClock:
    """Schedule event cycles while preserving samples through backpressure."""

    def __init__(self, model):
        self.model = model
        self.tick = 0

    def cycle(self, **signals):
        result = Inputs(now=self.tick, **signals)
        self.tick = (self.tick + 1) & LIMIT64
        return result

    def wait(self, count, **signals):
        for _ in range(count):
            yield self.cycle(**signals)

    def control(self, command, **signals):
        for _ in range(4096):
            port = self.cycle(control=command, **signals)
            ready = self.model.inspect(port).cfg_ready
            yield port
            if ready:
                return
        raise RuntimeError("directed configuration never transferred")

    def pps(self, tick, *, now=None, **signals):
        self.tick = (tick if now is None else now) & LIMIT64
        for _ in range(4096):
            port = self.cycle(sample=tick & LIMIT64, **signals)
            ready = self.model.inspect(port).sample_ready
            yield port
            if ready:
                return
        raise RuntimeError("directed PPS sample never transferred")

    def policy(self, policy, **signals):
        return self.control(Command(Op.PROGRAM, self.model.revision, policy), **signals)

    def series(self, seed, intervals, **signals):
        yield from self.pps(seed, **signals)
        edge = seed
        for interval in intervals:
            edge += interval
            yield from self.pps(edge, **signals)


def directed(model):
    clock = RawClock(model)
    yield clock.cycle(reset=True)
    yield from clock.pps(0)
    yield from clock.policy(Policy(enabled=True))
    yield from clock.control(Command(Op.RESEED, 2))
    yield from clock.policy(replace(POLICY, reference_ns=960, windows=1))
    yield clock.cycle(reply_ready=False)
    yield clock.cycle(reply_ready=False)
    yield from clock.series(12, (120, 120, 120))
    yield from clock.wait(110)
    # This exact ratio leaves the active period unchanged and signals lock.
    yield from clock.control(Command(Op.INSPECT))
    yield from clock.policy(POLICY)
    start = LIMIT64 - 600
    yield from clock.pps(start)
    yield from clock.pps(start, now=start + 1)                  # Repeated capture.
    yield from clock.pps(start - 80, now=start + 2)             # Stale arrival.
    yield from clock.pps(start + 5)                             # Early edge.
    for bounce in range(6, 26):
        yield from clock.pps(start + bounce, audit_ready=False)
    yield from clock.wait(5)
    index = start
    for interval in (120, 120, 135, 120, 120, 120, 120):
        index += interval
        yield from clock.pps(index, rate_ready=False)
    yield from clock.wait(111, rate_ready=False)
    if not model.offer:
        raise RuntimeError("directed slew did not reach the rate output")
    yield from clock.wait(8, rate_ready=False)
    yield from clock.wait(6)
    yield from clock.pps(index + 220)                           # Late but before watchdog.
    clock.tick = (index + 220 + POLICY.timeout) & LIMIT64
    yield from clock.wait(4)                                  # Hold last accepted rate.
    yield from clock.pps(clock.tick + 3)                       # Reacquire.

    yield from clock.policy(replace(POLICY, reference_ns=LIMIT64, windows=1))
    yield from clock.series(clock.tick+1, (120, 120, 120))
    yield from clock.wait(105)                                # Period overflow audit.
    short = replace(POLICY, timeout=156, windows=1)
    yield from clock.policy(short)
    yield from clock.series(clock.tick+1, (120, 120, 120))
    clock.tick = (clock.tick + 200) & LIMIT64
    yield from clock.wait(105)                                # Obsolete division.

    yield from clock.policy(short)
    yield from clock.series(clock.tick+1, (120, 120, 120), rate_ready=False)
    yield from clock.wait(170, rate_ready=False)               # Held proposal crosses timeout.
    if not model.offer or model.mode != 3:
        raise RuntimeError("directed proposal hold did not cross the watchdog threshold")
    yield from clock.wait(6)
    yield from clock.policy(replace(POLICY, reference_ns=930, windows=1))
    yield from clock.series(clock.tick+1, (120, 120, 120))
    yield from clock.wait(105)                                # Rate can slew downward too.

    while model.revision < min(7, model.high_revision):
        yield from clock.control(Command(Op.RESEED, model.revision))
    yield from clock.control(Command(Op.RESEED, model.revision))
    yield from clock.control(Command(Op.CLEAR, model.revision))
    yield from clock.wait(10)
    yield clock.cycle(reset=True)


def drifting_clocks(model, seed):
    rng = random.Random(seed ^ 0x7253)
    for oscillator_ticks in (111, 127, 139):
        clock = RawClock(model)
        yield clock.cycle(reset=True)
        policy = replace(POLICY, min_ticks=80, max_ticks=170, jitter=4,
                         max_step=Q32//8, windows=rng.choice((1, 2)))
        yield from clock.policy(policy)
        pulse = 10
        yield from clock.pps(pulse)
        for interval_index in range(15):
            adjusted = oscillator_ticks + rng.randrange(-1, 2)
            if interval_index == 8:
                # A clean but isolated period outlier enters the rolling history.
                adjusted += 16
            pulse += adjusted
            yield from clock.pps(pulse)
            yield from clock.wait(99, audit_ready=rng.random() > 0.05,
                                  rate_ready=rng.random() > 0.08)
            yield from clock.wait(5)
    yield clock.cycle(reset=True)


def choose_control(model, rng):
    op = rng.choices(tuple(Op), (8, 52, 5, 12))[0]
    revision = model.revision if rng.random() < 0.88 else rng.randrange(model.high_revision+1)
    if op != Op.PROGRAM:
        return Command(op, revision)
    options = replace(POLICY, enabled=rng.random() < 0.84,
                      reference_ns=rng.choice((0, 870, 960, 1000, 1066, 1600, LIMIT64)),
                      timeout=rng.choice((200, 320, 480, 650)), max_age=rng.choice((0, 15, 38, 80)),
                      jitter=rng.choice((3, 8, 32, 64)),
                      max_step=rng.choice((10, Q32//16, Q32//8, 2*Q32)),
                      windows=rng.choice((1, 2, 3, 5)))
    return Command(op, revision, options)


def randomized(model, seed, cycles):
    rng = random.Random(seed)
    clock = RawClock(model)
    command = None
    observation = None
    for _ in range(cycles):
        if command is None and rng.random() < 0.074:
            command = choose_control(model, rng)
        if rng.random() < 0.04:
            clock.tick = (clock.tick + rng.choice((35, 70, 120, 121, 150, 512))) & LIMIT64
        if observation is None and rng.random() < 0.07:
            observation = (clock.tick - rng.choice((-1, 0, 0, 0, 1, 3, 20, 60))) & LIMIT64
        reset = rng.random() < 0.002
        port = clock.cycle(sample=observation, control=command, reset=reset,
                           reply_ready=rng.random() < 0.64, audit_ready=rng.random() < 0.55,
                           rate_ready=rng.random() < 0.56)
        before = model.inspect(port)
        if reset or before.cfg_ready:
            command = None
        if reset or before.sample_ready:
            observation = None
        yield port
    yield clock.cycle(reset=True)
    yield clock.cycle()


def vector(port, pins, rev_width):
    command = port.control or Command(Op.INSPECT)
    policy = command.policy
    reply, audit, rate = pins.reply, pins.audit, pins.rate
    return " ".join(f"{word:x}" for word in (
        pack((port.reset, 1), (port.control is not None, 1), (port.sample is not None, 1),
             (port.reply_ready, 1), (port.audit_ready, 1), (port.rate_ready, 1)),
        pack((command.op, 2), (command.revision, rev_width), (policy.enabled, 1),
             (policy.reference_ns, 64), (policy.min_ticks, 64), (policy.max_ticks, 64),
             (policy.timeout, 64), (policy.max_age, 64), (policy.jitter, 64),
             (policy.minimum, 64), (policy.maximum, 64), (policy.max_step, 64), (policy.windows, 8)),
        port.now,
        port.sample or 0,
        pack((pins.cfg_ready, 1), (pins.sample_ready, 1), (reply is not None, 1),
             (audit is not None, 1), (rate is not None, 1)),
        0 if reply is None else pack((reply.op, 2), (reply.status, 3), (reply.revision, rev_width),
                                     (reply.period, 64), (reply.mode, 2)),
        0 if audit is None else pack((audit.sequence, 64), (audit.revision, rev_width), (audit.word(), 326)),
        0 if rate is None else rate.period,
        pack((pins.audit_cursor, 64), (pins.audit_occupancy, 16)),
        pack((pins.mode, 2), (pins.anchor_valid, 1), (pins.anchor_tick, 64),
             (pins.active_period, 64), (pins.intervals, 2), (pins.streak, 8)),
        pack(*((value, 32) for value in pins.counts)),
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--revision-width", type=int, default=3)
    parser.add_argument("--seed", type=int, default=93117)
    parser.add_argument("--cycles", type=int, default=6800)
    args = parser.parse_args()
    if args.depth < 1 or args.revision_width < 3 or args.cycles < 0:
        parser.error("require positive depth, at least three revision bits and nonnegative cycles")
    model = Discipline(args.depth, args.revision_width)
    with args.output.open("w", encoding="ascii") as output:
        sequences = [directed(model)]
        if args.cycles:
            sequences.extend((drifting_clocks(model, args.seed), randomized(model, args.seed, args.cycles)))
        for sequence in sequences:
            for port in sequence:
                pins = model.advance(port)
                output.write(vector(port, pins, args.revision_width) + "\n")


if __name__ == "__main__":
    main()
