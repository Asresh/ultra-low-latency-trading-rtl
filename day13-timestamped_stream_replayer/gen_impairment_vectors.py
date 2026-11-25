"""Produce reproducible replay impairment input and output pin fixtures."""

import argparse
from dataclasses import replace
from pathlib import Path
import random

from replay_impairment_model import Beat, Geometry, Model, Port, Retirement
from replay_impairment_recipe import Action, Control, Operation, Rule, packed
from replay_program import U32, U64


DROP_TAG = 0x10
DUPLICATE_TAG = 0x20
CORRUPT_TAG = 0x30
PLAIN_TAG = 0x40
SEED = 0xFEED2001


def raw_packet(payload, tag, run=1, bank=0, first_due=0, pass_id=1):
    for offset in range(0, len(payload), 8):
        data = payload[offset:offset+8]
        due = first_due + offset//8
        yield Beat(int.from_bytes(data.ljust(8, b"\x9d"), "little"), (1 << len(data))-1,
                   offset == 0, offset+8 >= len(payload), tag, pass_id, due, due+1, bank, run)


class Clock:
    def __init__(self, model):
        self.model = model
        self.wall = 0

    def cycle(self, **inputs):
        step = Port(wall=self.wall, **inputs)
        self.wall = min(U64, self.wall+1)
        return step

    def idle(self, count=1, **inputs):
        for _ in range(count):
            yield self.cycle(**inputs)

    def send(self, beat=None, control=None, **inputs):
        for _ in range(8192):
            step = self.cycle(beat=beat, control=control, **inputs)
            signals = self.model.observe(step)
            yield step
            if (beat is None or signals.s_ready) and (control is None or signals.cfg_ready):
                return
        raise RuntimeError("directed input remained blocked")

    def frame(self, payload, tag, **inputs):
        scope = {key: inputs.pop(key) for key in ("run", "bank", "first_due", "pass_id") if key in inputs}
        for beat in raw_packet(payload, tag, **scope):
            yield from self.send(beat=beat, **inputs)

    def drain(self, **inputs):
        for _ in range(8192):
            if not self.model.queue and self.model.replay is None and self.model.audit is None:
                return
            yield self.cycle(**inputs)
        raise RuntimeError("directed queue did not drain")


def standard_rules():
    return (
        Rule(tag=DROP_TAG, tag_mask=0xffff, action=Action.DROP),
        Rule(tag=DUPLICATE_TAG, tag_mask=0xffff, action=Action.DUPLICATE, delay=7,
             jitter_mask=0x07, copy_gap=4, run=1),
        Rule(tag=CORRUPT_TAG, tag_mask=0xffff, action=Action.CORRUPT, flip_offset=8, flip_mask=0x80),
        Rule(bank=1, first=1, last=3, action=Action.PASS, delay=4, jitter_mask=0x03),
    )


def directed(model):
    g = model.geometry
    clock = Clock(model)
    yield clock.cycle(reset=True)
    yield from clock.send(control=Control(Operation.CLEAR, 2))
    yield from clock.send(control=Control(Operation.READ, slot=g.rules))
    yield from clock.send(control=Control(Operation.WRITE, slot=0, rule=Rule(first=7, last=6)))
    yield from clock.send(control=Control(Operation.WRITE, slot=0, rule=Rule(action=Action.CORRUPT)))
    yield from clock.send(control=Control(Operation.CLEAR))
    for slot, rule in enumerate(standard_rules()):
        yield from clock.send(control=Control(Operation.WRITE, slot=slot, rule=rule))
    yield from clock.send(control=Control(Operation.READ, slot=0, active=True))
    yield from clock.send(control=Control(Operation.READ, slot=0))
    yield from clock.idle(5, reply_ready=False)

    # COMMIT falls between beats of an existing packet. The old rule is pinned.
    head, tail = raw_packet(b"precommit", DROP_TAG)
    yield from clock.send(beat=head, ready=False)
    yield from clock.send(control=Control(Operation.COMMIT, seed=SEED), ready=False)
    yield from clock.send(beat=tail, ready=False)
    yield from clock.idle(8, ready=False)
    yield from clock.drain()

    # Fill the packet queue. A drop consumes no packet memory even at capacity.
    for index in range(g.queue):
        yield from clock.frame(bytes([40+index, 105]), PLAIN_TAG, ready=False)
    yield from clock.frame(b"drop without a queue slot", DROP_TAG, ready=False)
    for _ in range(3):
        yield from clock.frame(b"drop while audit is blocked", DROP_TAG, ready=False, audit_ready=False)
    yield from clock.drain()

    yield from clock.frame(b"two identical outputs", DUPLICATE_TAG, first_due=21)
    yield from clock.idle(14, ready=False)
    yield from clock.drain()
    yield from clock.frame(b"first-beat and a later flip", CORRUPT_TAG)
    yield from clock.drain()
    yield from clock.frame(b"short", CORRUPT_TAG)
    yield from clock.drain()

    malformed = tuple(raw_packet(b"inconsistent tag", PLAIN_TAG))
    yield from clock.send(beat=malformed[0])
    for beat in malformed[1:]:
        yield from clock.send(beat=replace(beat, tag=PLAIN_TAG+1))
    yield from clock.frame(bytes(range(8))*g.max_beats+b"!", PLAIN_TAG)
    yield from clock.send(beat=Beat(1, 0x55, True, True, PLAIN_TAG, 1, 0, 0, 0, 1))
    yield from clock.send(beat=next(raw_packet(b"stopped before its last beat", PLAIN_TAG)))
    yield clock.cycle(abort=Retirement(1, 1))
    yield clock.cycle(abort=Retirement(0, 1))
    yield from clock.frame(b"new owner", DUPLICATE_TAG, run=2, bank=1)
    yield from clock.frame(b"delayed sibling", PLAIN_TAG, run=2, bank=1)
    yield from clock.drain()
    yield from clock.send(control=Control(Operation.READ, slot=1, active=True))
    yield from clock.send(control=Control(Operation.READ, slot=1, active=False))
    while model.rules.revision < min(7, (1 << g.revision_width)-1):
        yield from clock.send(control=Control(Operation.COMMIT, model.rules.revision,
                                              seed=SEED))
    yield from clock.send(control=Control(Operation.COMMIT, model.rules.revision))
    yield from clock.idle(1)
    # Run a fresh experiment near saturation even if the preceding revision
    # counter exhausted. A delayed frame can still leave at the last tick.
    yield clock.cycle(reset=True)
    rule = Rule(delay=19, jitter_mask=0xff)
    yield from clock.send(control=Control(Operation.WRITE, slot=0, rule=rule))
    yield from clock.send(control=Control(Operation.COMMIT, seed=1))
    clock.wall = U64 - 3
    yield from clock.frame(b"end time", PLAIN_TAG)
    yield from clock.drain()
    yield clock.cycle(reset=True)


def random_control(model, rng):
    op = rng.choices(tuple(Operation), (9, 11, 2, 5))[0]
    key = rng.randrange(1 << model.geometry.key_width)
    revision = model.rules.revision if rng.random() < 0.9 else rng.randrange(
        1 << model.geometry.revision_width
    )
    rule = rng.choice(standard_rules())
    rule = replace(rule, first=rng.randrange(0, 3), delay=rng.randrange(0, 20), bank=rng.choice((None, 0, 1)))
    return Control(op, revision, key, bool(rng.randrange(2)), rng.randrange(1 << 32), rule)


def random_frame(model, rng, run, bank):
    g = model.geometry
    size = rng.randint(1, min(96, g.max_beats*8 + 5))
    tag = rng.choice((DROP_TAG, DUPLICATE_TAG, CORRUPT_TAG, PLAIN_TAG))
    out = list(raw_packet(bytes(rng.randrange(256) for _ in range(size)), tag, run, bank,
                          rng.randrange(300), rng.randrange(1, 4)))
    if rng.random() < 0.07:
        out[-1] = replace(out[-1], keep=rng.choice((0, 0x50, 0x80)))
    elif len(out) > 1 and rng.random() < 0.06:
        out[-1] = replace(out[-1], first=True)
    return out


def randomized(model, seed, cycles):
    clock = Clock(model)
    rng = random.Random(seed)
    pending = []
    command = None
    run, bank = 1, 0
    yield from clock.idle(1, reset=True)
    for slot, rule in enumerate(standard_rules()):
        yield from clock.send(control=Control(Operation.WRITE, slot=slot, rule=rule))
    yield from clock.send(control=Control(Operation.COMMIT, seed=seed))
    for _ in range(cycles):
        if command is None and rng.random() < 0.073:
            command = random_control(model, rng)
        if not pending and rng.random() < 0.42:
            if rng.random() < 0.042:
                run = (run + 1) % (1 << model.geometry.run_width)
                bank ^= 1
            pending = random_frame(model, rng, run, bank)
        abort = Retirement(bank, run) if pending and rng.random() < 0.017 else None
        reset = rng.random() < 0.0012
        step = clock.cycle(beat=pending[0] if pending else None, control=command, abort=abort,
                           reset=reset, ready=rng.random() < 0.47,
                           audit_ready=rng.random() < 0.62, reply_ready=rng.random() < 0.58)
        before = model.observe(step)
        if before.cfg_ready or reset:
            command = None
        if reset:
            pending = []
        elif before.s_ready and pending:
            pending.pop(0)
        yield step
    for beat in pending:
        yield from clock.send(beat=beat)
    yield from clock.drain()
    yield from clock.idle(2)


def beat_fields(beat, width):
    return (
        (beat.data, 64), (beat.keep, 8), (beat.first, 1), (beat.last, 1), (beat.tag, 16),
        (beat.pass_id, 16), (beat.due, 48), (beat.offered, 48), (beat.bank, 1), (beat.run, width),
    )


def vector(port, pins, g):
    ctrl = port.control or Control(Operation.READ)
    audit, reply, out = pins.audit, pins.reply, pins.output
    abort = port.abort or Retirement(0, 0)
    rule_word = ctrl.rule.word(g.run_width) if ctrl.rule else 0
    return " ".join(f"{word:x}" for word in (
        pack_flags(port), port.wall,
        packed(*beat_fields(port.beat or Beat(), g.run_width)),
        packed((abort.bank, 1), (abort.run, g.run_width)),
        packed((ctrl.op, 2), (ctrl.revision, g.revision_width), (ctrl.slot, g.key_width),
               (ctrl.active, 1), (ctrl.seed, 32), (rule_word, 198+g.run_width)),
        packed((pins.s_ready, 1), (pins.cfg_ready, 1), (reply is not None, 1),
               (out is not None, 1), (audit is not None, 1)),
        0 if reply is None else packed(
            (reply.op, 2), (reply.status, 3), (reply.revision, g.revision_width),
            (reply.slot, g.key_width), (reply.seed, 32), (reply.word, 198+g.run_width),
        ),
        0 if out is None else packed(
            *beat_fields(out.beat, g.run_width), (out.copy, 1), (out.revision, g.revision_width),
            (out.ordinal, 32), (out.action, 2), (out.release, 64),
        ),
        0 if audit is None else packed(
            (audit.sequence, 64),
            (audit.word(g), 280+g.run_width+g.revision_width+g.key_width),
        ),
        packed((pins.occupancy, 16), (pins.peak, 16), (pins.audit_cursor, 64)),
        packed(*((value, 32) for value in pins.counts)),
    ))


def pack_flags(port):
    return packed((port.reset, 1), (port.beat is not None, 1), (port.control is not None, 1),
                  (port.abort is not None, 1), (port.ready, 1), (port.reply_ready, 1),
                  (port.audit_ready, 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--queue", type=int, default=3)
    parser.add_argument("--maximum", type=int, default=4)
    parser.add_argument("--run-width", type=int, default=5)
    parser.add_argument("--revision-width", type=int, default=3)
    parser.add_argument("--seed", type=int, default=91281)
    parser.add_argument("--cycles", type=int, default=7500)
    args = parser.parse_args()
    if not (1 <= args.queue <= 16 and 4 <= args.maximum <= 64 and
            2 <= args.run_width <= 32 and args.revision_width >= 3 and args.cycles >= 0):
        parser.error("unsupported generated campaign geometry")
    geometry = Geometry(4, args.queue, args.maximum, args.run_width, args.revision_width)
    model = Model(geometry)
    with args.output.open("w", encoding="ascii") as stream:
        for campaign in (directed(model), randomized(model, args.seed, args.cycles)):
            for port in campaign:
                pins = model.step(port)
                stream.write(vector(port, pins, geometry) + "\n")


if __name__ == "__main__":
    main()
