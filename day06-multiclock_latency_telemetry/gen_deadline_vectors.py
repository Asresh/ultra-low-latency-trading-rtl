"""Produce cycle-exact tap input and expected output for the RTL scoreboard."""

import argparse
from pathlib import Path
import random

from deadline_model import Command, Input, Monitor, Op, Policy, Report, U16_MAX, U64_MAX, pack


def directed(rules, depth):
    yield Input(reset=True)
    exact = Policy(True, 3, 1, 4, 0, 100, 2, 2)
    fallback = Policy(True, 0, 0, 0, 7, 110, 1, 1)
    yield Input(report=Report(duration=101), command=Command(Op.PROGRAM, policy=exact))
    if rules > 1:
        yield Input(command=Command(Op.PROGRAM, 1, policy=fallback))
    yield Input(report=Report(tag=1, duration=100, tick=10))
    yield Input(report=Report(tag=2, duration=101, tick=11))
    yield Input(report=Report(tag=3, duration=1, tick=12, status=2))
    yield Input(report=Report(tag=4, duration=102, tick=13), alert_ready=False)
    yield Input(report=Report(tag=5, duration=100, tick=14), alert_ready=False)
    yield Input(report=Report(tag=6, duration=100, tick=15), alert_ready=False)
    yield Input(command=Command(Op.INSPECT), alert_ready=False)
    yield Input(command=Command(Op.INSPECT, rules), reply_ready=False, alert_ready=False)
    yield Input(command=Command(Op.INSPECT, rules), alert_ready=False)
    yield Input()
    yield Input()
    yield Input(report=Report(tag=7, stop=9, duration=213, tick=16))
    yield Input(report=Report(tag=8, stop=9, duration=110, tick=17))
    yield Input(command=Command(Op.PROGRAM, revision=0, policy=fallback))
    yield Input(command=Command(Op.PROGRAM, revision=1, policy=Policy(enabled=True)))
    yield Input(command=Command(3))
    yield Input(command=Command(Op.PROGRAM, revision=1, policy=fallback))
    for index in range(depth + 4):
        yield Input(
            report=Report(tag=20+index, duration=700 if index % 2 == 0 else 100, tick=20+index),
            alert_ready=False,
        )
    yield Input(report=Report(tag=50, duration=22, tick=50), alert_ready=True)
    yield Input(
        report=Report(tag=51, duration=900, tick=51),
        command=Command(Op.CLEAR, revision=2),
        alert_ready=True,
    )
    yield Input(command=Command(Op.INSPECT))
    yield Input(
        report=Report(tag=52, duration=U64_MAX, tick=U64_MAX),
        command=Command(Op.PROGRAM, revision=2, policy=exact),
    )
    yield Input(command=Command(Op.INSPECT))
    for revision in range(3, 8):
        yield Input(command=Command(Op.PROGRAM, revision=revision, policy=exact))
    for _ in range(depth + 1):
        yield Input()


def random_inputs(model, cycles, seed):
    rng = random.Random(seed)
    command = None
    key_width = model.count.bit_length()
    probes = (0, 1, 4, 7, 255)
    values = (0, 1, 21, 22, 100, 101, 109, 110, 111, 900, U64_MAX - 1, U64_MAX)
    for cycle in range(cycles):
        reset = rng.random() < 0.008
        report = None
        if rng.random() < 0.83:
            report = Report(
                rng.choice(probes), rng.randrange(1 << 16),
                rng.choice(probes), rng.choice(probes), rng.choice(values), cycle + 100,
                rng.choice((1, 1, 1, 1, 0, 2, 3, 4, 5, 6, 7)),
            )
        if command is None and rng.random() < 0.65:
            slot = rng.randrange(1 << key_width)
            revision = 0 if slot >= model.count else model.rules[slot].revision
            if rng.random() < 0.24:
                revision = rng.randrange(model.revision_max + 1)
            command = Command(
                rng.choice((Op.PROGRAM, Op.PROGRAM, Op.INSPECT, Op.INSPECT, Op.CLEAR, 3)),
                slot,
                revision,
                Policy(
                    rng.random() < 0.7, rng.choice(probes), rng.choice(probes),
                    rng.choice(probes), rng.choice((0, 1, 2, 4, 7, 7)),
                    rng.choice(values), rng.choice((0, 1, 1, 2, 3, U16_MAX)),
                    rng.choice((0, 1, 1, 2, 3, U16_MAX)),
                ),
            )
        port = Input(report, command, rng.random() < 0.42, rng.random() < 0.48, reset)
        if reset or model.inspect(port).ctl_ready:
            command = None
        yield port


def vector(port, output, key_width, rule_width, revision_width):
    report = port.report or Report()
    command = port.command or Command(Op.PROGRAM)
    reply = output.reply
    alert = output.alert
    return " ".join(f"{word:x}" for word in (
        pack((port.reset, 1), (port.report is not None, 1), (port.command is not None, 1),
             (port.alert_ready, 1), (port.reply_ready, 1)),
        report.wire(),
        pack((command.op, 2), (command.rule, key_width), (command.revision, revision_width),
             (command.policy.wire(), 124)),
        pack((output.ctl_ready, 1), (output.reply_valid, 1), (output.alert_valid, 1)),
        pack((reply.status, 3), (reply.op, 2), (reply.slot, key_width),
             (reply.revision, revision_width), (reply.data, 381)),
        pack((alert.slot, rule_width), (alert.revision, revision_width), (alert.data, 256)),
        output.used,
        pack((output.seen, 32), (output.nonmatch, 32),
             (output.unclassified, 32), (output.lost, 32)),
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cycles", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=97813)
    parser.add_argument("--rules", type=int, default=3)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--revision-width", type=int, default=3)
    args = parser.parse_args()
    if min(args.rules, args.depth) < 1 or args.revision_width < 3 or args.cycles < 0:
        parser.error("require positive rule count and depth, revision width ≥ 3, and nonnegative cycles")
    model = Monitor(args.rules, args.depth, args.revision_width)
    inputs = (
        directed(args.rules, args.depth),
        random_inputs(model, args.cycles, args.seed),
        (Input(),) * (args.depth + 2),
    )
    with args.output.open("w") as file:
        for segment in inputs:
            for port in segment:
                before = model.advance(port)
                file.write(vector(
                    port, before, args.rules.bit_length(),
                    max(1, (args.rules-1).bit_length()), args.revision_width,
                ) + "\n")


if __name__ == "__main__":
    main()
