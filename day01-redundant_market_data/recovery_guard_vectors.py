"""Generate checked recovery transport cases with byte faults and concurrent stalls."""

import argparse
from dataclasses import replace
from pathlib import Path
import random

from recovery_guard_model import (
    Beat, Control, Geometry, Inputs, Model, MODULO, Operation, Phase, Request, Route,
)
from wire_codec import make_packet


SESSION_A = b"CHANNEL001"
SESSION_B = b"CHANNEL002"
SESSION_C = b"CHANNEL003"
SESSIONS = (SESSION_A, SESSION_B, SESSION_C)
WRAP = MODULO - 3


def pack(*fields):
    result = 0
    for value, width in fields:
        if not 0 <= value < 1 << width:
            raise ValueError(f"{value} does not fit unsigned {width} bits")
        result = result << width | value
    return result


def packet_beats(data, route):
    for offset in range(0, len(data), 8):
        chunk = data[offset:offset + 8]
        yield Beat(int.from_bytes(chunk.ljust(8, b"\x96"), "little"),
                   (1 << len(chunk)) - 1, offset == 0, offset + 8 >= len(data), route)


def send(model, command=None, beat=None, **kwargs):
    port = Inputs(command=command, beat=beat, **kwargs)
    for _ in range(8192):
        pins = model.observe(port)
        yield port
        if (command is None or pins.command_ready) and (beat is None or pins.rx_ready):
            return
    raise RuntimeError("input never transferred in the modeled scenario")


def packet(model, wire, route, *, fault=None, **kwargs):
    beats = list(packet_beats(wire, route))
    if fault == "keep":
        beats[-1] = replace(beats[-1], keep=0x55)
    elif fault == "first":
        beats[0] = replace(beats[0], first=False)
    elif fault == "second-first":
        beats[1] = replace(beats[1], first=True)
    elif fault == "nonfinal-keep":
        beats[0] = replace(beats[0], keep=0x7F)
    for index, beat in enumerate(beats):
        # Sideband on continuation beats is deliberately different.
        if index:
            beat = replace(beat, route=Route(0, 0, 0))
        yield from send(model, beat=beat, **kwargs)


def quiet(model, **kwargs):
    for _ in range(8192):
        if model.candidate is None and not model.replay:
            return
        yield Inputs(**kwargs)
    raise RuntimeError("modeled frame never finished")


def sync(slot, epoch, session, enabled=True):
    return Control(Operation.SYNC, Route(slot, epoch), enabled, Request(session))


def arm(slot, epoch, session, seq, count, ttl=700):
    return Control(Operation.ARM, Route(slot, epoch), request=Request(session, seq, count), ttl=ttl)


def wire(session, seq, *sizes):
    return make_packet(session, seq % MODULO, tuple(bytes([11 + index]) * size
                                                  for index, size in enumerate(sizes)))


def capacity_packet(geometry):
    for blocks in range(1, min(geometry.blocks, geometry.window) + 1):
        remaining = geometry.packet_bytes - 20 - 2 * blocks
        if not 0 <= remaining <= geometry.message_bytes * blocks:
            continue
        small, extra = divmod(remaining, blocks)
        return tuple(small + (index < extra) for index in range(blocks))
    raise ValueError("fixture geometry cannot encode a packet that fills capture exactly")


def directed(model):
    g = model.geometry
    yield Inputs(reset=True)
    yield from send(model, Control(Operation.INSPECT, Route(g.slots)))
    yield from send(model, sync(0, 2, SESSION_A))
    yield from send(model, sync(0, 1, SESSION_A))
    yield from send(model, sync(0, 2, SESSION_A))
    yield from send(model, sync(1, 1, SESSION_B))
    yield from send(model, sync(2, 1, SESSION_C, False))
    yield from send(model, arm(2, 1, SESSION_C, 1, 1))
    yield from send(model, arm(0, 2, SESSION_A, WRAP, 0))
    yield from send(model, arm(0, 2, SESSION_B, WRAP, 1))
    yield from packet(model, wire(SESSION_A, WRAP, 4), Route(0, 2, 0))
    yield from quiet(model)
    yield from packet(model, wire(SESSION_C, 1, 3), Route(2, 1, 0))
    yield from quiet(model)
    yield from send(model, arm(0, 2, SESSION_A, WRAP, 5))
    yield from send(model, arm(0, 2, SESSION_A, WRAP, 5, 50))
    yield from send(model, arm(0, 2, SESSION_A, WRAP, 4))
    yield from send(model, Control(Operation.CANCEL, Route(0, 2, 2)))

    for packet_wire, route, fault in (
        (wire(SESSION_A, WRAP, 4), Route(g.slots, 2, 1), None),
        (wire(SESSION_A, WRAP, 4), Route(0, 1, 1), None),
        (wire(SESSION_B, WRAP, 4), Route(0, 2, 1), None),
        (wire(SESSION_A, WRAP, 4), Route(0, 2, 0), None),
        (wire(SESSION_A, WRAP - 1, 4), Route(0, 2, 1), None),
        (wire(SESSION_A, WRAP + 4, 1, 2), Route(0, 2, 1), None),
        (wire(SESSION_A, WRAP, 0), Route(0, 2, 1), "first"),
        (wire(SESSION_A, WRAP, 0), Route(0, 2, 1), "second-first"),
        (wire(SESSION_A, WRAP, 0), Route(0, 2, 1), "nonfinal-keep"),
        (wire(SESSION_A, WRAP, 4), Route(0, 2, 1), "keep"),
        (wire(SESSION_A, WRAP, 9)[:-1], Route(0, 2, 1), None),
        (wire(SESSION_A, WRAP, 9) + b"\0", Route(0, 2, 1), None),
        (wire(SESSION_A, WRAP, 1)[:21], Route(0, 2, 1), None),
        (wire(SESSION_A, WRAP, g.message_bytes + 1), Route(0, 2, 1), None),
        (wire(SESSION_A, WRAP, *([1] * (g.blocks + 1))), Route(0, 2, 1), None),
        (wire(SESSION_A, WRAP), Route(0, 2, 1), None),
        (make_packet(SESSION_A, WRAP, declared_count=65535), Route(0, 2, 1), None),
        (wire(SESSION_A, WRAP, 3, 9, 12) + bytes(g.packet_bytes), Route(0, 2, 1), None),
    ):
        yield from packet(model, packet_wire, route, fault=fault)
        yield from quiet(model)
    yield from packet(model, wire(SESSION_A, 0, 3, 1), Route(0, 2, 1))
    yield from quiet(model)
    yield from packet(model, wire(SESSION_A, 0, 3), Route(0, 2, 1))
    yield from quiet(model)
    yield from packet(model, wire(SESSION_A, MODULO-1, 3, 1), Route(0, 2, 1))
    yield from quiet(model)
    yield Inputs()
    yield from packet(model, wire(SESSION_A, WRAP, 1, 4, 8), Route(0, 2, 1),
                      replay_ready=False, audit_ready=False)
    for _ in range(100):
        if model.replay:
            break
        yield Inputs(replay_ready=False, audit_ready=False)
    else:
        raise RuntimeError("directed repair was not admitted")

    # The admitted result and its audit are independently backpressured.
    yield from send(model, sync(1, 2, SESSION_B), replay_ready=False, audit_ready=False)
    replacing = sync(0, 3, SESSION_A)
    for _ in range(8):
        yield Inputs(command=replacing, replay_ready=False, audit_ready=False)
    yield from send(model, replacing, audit_ready=False)
    yield Inputs(reply_ready=False, audit_ready=False)
    yield Inputs(reply_ready=False, audit_ready=False)
    yield Inputs()
    yield from packet(model, wire(SESSION_A, WRAP, 1), Route(0, 2, 1))
    yield from quiet(model)

    yield from send(model, arm(1, 2, SESSION_B, 5, 1, ttl=2))
    yield from packet(model, wire(SESSION_B, 5, 1), Route(1, 2, 1))
    yield from quiet(model)
    yield from send(model, arm(1, 2, SESSION_B, 6, 1))
    yield from send(model, Control(Operation.CANCEL, Route(1, 2, 2)))
    yield from packet(model, wire(SESSION_B, 6, 1), Route(1, 2, 2))
    yield from quiet(model)
    yield from send(model, Control(Operation.CANCEL, Route(1, 2, 2)))
    maximum = min(7, (1 << g.token_width) - 1)
    for token in range(3, maximum + 1):
        yield from send(model, arm(1, 2, SESSION_B, token, 1))
        yield from send(model, Control(Operation.CANCEL, Route(1, 2, token)))
    yield from send(model, arm(1, 2, SESSION_B, 9, 1))
    sizes = capacity_packet(g)
    yield from send(model, sync(2, 2, SESSION_C))
    yield from send(model, arm(2, 2, SESSION_C, 12, len(sizes)))
    yield from packet(model, wire(SESSION_C, 12, *sizes), Route(2, 2, 1))
    yield from quiet(model)
    for slot in range(g.slots + 1):
        for index in range(23):
            yield Inputs(stat_slot=slot, stat_index=index)
    yield Inputs(reset=True)


def choose_command(model, randomizer):
    slot = randomizer.randrange(model.geometry.slots)
    entry = model.slots[slot]
    session = SESSIONS[slot % len(SESSIONS)]
    epoch_max = (1 << model.geometry.epoch_width) - 1
    if not entry.epoch or randomizer.random() < 0.12:
        return sync(slot, min(epoch_max, entry.epoch + 1), session, randomizer.random() < 0.85)
    if randomizer.random() < 0.55:
        seq = randomizer.choice((0, MODULO-2, 57, entry.request.sequence))
        return arm(slot, entry.epoch, session, seq,
                   randomizer.randint(1, model.geometry.window), randomizer.choice((3, 45, 120, 500)))
    if randomizer.random() < 0.45:
        return Control(Operation.CANCEL, Route(slot, entry.epoch, entry.token))
    return Control(Operation.INSPECT, Route(slot))


def choose_packet(model, rng):
    slot = rng.randrange(model.geometry.slots)
    entry = model.slots[slot]
    session = entry.request.session
    fault = rng.choice((None, None, None, None, "keep", "second-first", "truncate"))
    if rng.random() < 0.10:
        session = b"WRONG00001"
    eligible = entry.phase == Phase.LIVE and bool(entry.sequence_map)
    seq = rng.choice(tuple(entry.sequence_map)) if eligible else entry.request.sequence
    if rng.random() < 0.2:
        seq = (seq - rng.randrange(0, 3)) % MODULO
    body_count = rng.randint(1, min(3, model.geometry.blocks))
    payload = wire(session, seq, *(rng.randrange(11) for _ in range(body_count)))
    route = Route(slot, entry.epoch, entry.token)
    if rng.random() < 0.13:
        route = replace(route, token=0)
    if fault == "truncate":
        payload = payload[:-1]
    out = list(packet_beats(payload, route))
    if fault == "keep":
        out[-1] = replace(out[-1], keep=0x55)
    elif fault == "second-first":
        out[1] = replace(out[1], first=True)
    return out


def random_ports(model, seed, duration):
    rng = random.Random(seed)
    command = None
    remaining = []
    for _ in range(duration):
        if command is None and rng.random() < 0.26:
            command = choose_command(model, rng)
        if not remaining and rng.random() < 0.31:
            remaining = choose_packet(model, rng)
        beat = remaining[0] if remaining else None
        reset = rng.random() < 0.0025
        port = Inputs(command, beat, reset, rng.random() < 0.64, rng.random() < 0.66,
                      rng.random() < 0.58, rng.randrange(model.geometry.slots + 1), rng.randrange(32))
        pins = model.observe(port)
        if pins.command_ready or reset:
            command = None
        if reset:
            remaining = []
        elif beat and pins.rx_ready:
            remaining.pop(0)
        yield port
    if command:
        yield from send(model, command=command)
    for beat in remaining:
        yield from send(model, beat=beat)
    yield from quiet(model)
    yield Inputs()


def route_fields(route, g):
    return (route.slot, g.key_width), (route.epoch, g.epoch_width), (route.token, g.token_width)


def beat_word(beat, g):
    return pack((beat.data, 64), (beat.keep, 8), (beat.first, 1), (beat.last, 1),
                *route_fields(beat.route, g))


def vector(port, pins, geometry):
    ctrl = port.command or Control(Operation.INSPECT)
    reply, replay, audit = pins.reply, pins.replay, pins.audit
    stat_slot = port.stat_slot
    return " ".join(f"{word:x}" for word in (
        pack((port.reset, 1), (port.command is not None, 1), (port.beat is not None, 1),
             (port.reply_ready, 1), (port.replay_ready, 1), (port.audit_ready, 1)),
        pack((ctrl.op, 2), *route_fields(ctrl.route, geometry), (ctrl.enabled, 1),
             (int.from_bytes(ctrl.request.wire(), "big"), 160), (ctrl.ttl, 32)),
        0 if port.beat is None else beat_word(port.beat, geometry),
        pack((stat_slot, geometry.key_width), (port.stat_index, 5)),
        pack((pins.command_ready, 1), (pins.rx_ready, 1), (reply is not None, 1),
             (replay is not None, 1), (audit is not None, 1), (pins.stat_valid, 1)),
        0 if reply is None else pack(
            (reply.op, 2), (reply.status, 4), *route_fields(reply.snapshot.route, geometry),
            (reply.snapshot.enabled, 1), (reply.snapshot.phase, 3),
            (int.from_bytes(reply.snapshot.request.wire(), "big"), 160),
            (reply.snapshot.coverage, geometry.window), (reply.snapshot.ttl, 32),
        ),
        0 if replay is None else beat_word(replay, geometry),
        0 if audit is None else pack(
            (audit.status, 4), *route_fields(audit.route, geometry),
            (audit.sequence, 64), (audit.count, 16), (audit.length, 16),
            (audit.coverage, geometry.window), (audit.complete, 1),
        ),
        pack((pins.stat_value, 32), *((value, 32) for value in pins.totals)),
    ))


def write(path, geometry, seed, cycles):
    model = Model(geometry)
    with path.open("w", encoding="ascii") as stream:
        for sequence in (directed(model), random_ports(model, seed, cycles)):
            for port in sequence:
                pins = model.advance(port)
                stream.write(vector(port, pins, geometry) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cycles", type=int, default=5400)
    parser.add_argument("--seed", type=int, default=73591)
    parser.add_argument("--default", action="store_true", help="use default production module geometry")
    args = parser.parse_args()
    if args.cycles < 0:
        parser.error("cycles must be nonnegative")
    geometry = Geometry(3, 256, 64, 16, 16, 16, 16) if args.default else Geometry()
    write(args.output, geometry, args.seed, args.cycles)


if __name__ == "__main__":
    main()
