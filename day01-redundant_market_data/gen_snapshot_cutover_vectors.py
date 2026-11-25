"""Generate byte-checked scope fences and seeded cutover fault campaigns."""

import argparse
from dataclasses import replace
from pathlib import Path
import random
from zlib import crc32

from snapshot_cutover_model import (
    Event, Geometry, Header, Inputs, Model, Phase, Prepared, ReceiverAck, RecordType, Row, U64,
)


SESSION = int.from_bytes(b"BOOKSNAP01", "big")


def words_crc(words):
    return crc32(b"".join(word.to_bytes(16, "little") for word in words))


def pack(*fields):
    word = 0
    for value, bits in fields:
        if not 0 <= value < 1 << bits:
            raise ValueError(f"{value} exceeds {bits} unsigned bits")
        word = word << bits | value
    return word


def event(slot, epoch, sequence, payload=0xA0):
    return Event(slot, epoch, ((sequence & U64) << 520) | (0x44 << 448) | payload)


def send(model, channel, item, **levels):
    keys = {"begin": "begin_ready", "row": "row_ready", "prepared": "prepared_ready",
            "ack": "ack_ready", "event": "s_ready"}
    for _ in range(8192):
        port = Inputs(**{channel: item}, **levels)
        ready = getattr(model.observe(port), keys[channel])
        yield port
        if ready:
            return
    raise RuntimeError(f"{channel} remained blocked")


def until(model, phase, **levels):
    for _ in range(8192):
        if model.phase == phase:
            return
        yield Inputs(**levels)
    raise RuntimeError(f"phase {phase.name} remained unreachable")


def header(identity, slot=0, epoch=1, sequence=500, rows=(), force=False, spacing=7):
    return Header(identity, slot, epoch, SESSION, sequence, len(rows), words_crc(rows), force, spacing)


def stage(model, details, rows, **levels):
    yield from send(model, "begin", details, **levels)
    for index, word in enumerate(rows):
        yield from send(model, "row", Row(details.identity, word, index+1 == len(rows)), **levels)
    yield from until(model, Phase.PREPARE, **levels)


def stage_ack(model, details):
    yield from send(model, "prepared", Prepared(details.identity, True))
    yield from until(model, Phase.WAIT_ACK)


def directed(model):
    g = model.g
    yield Inputs(reset=True)
    for invalid in (
        header(1, slot=g.slots),
        header(0),
        header(1, epoch=(1 << g.epoch_width)-1),
        replace(header(1), count=g.rows+1),
        replace(header(1), crc=3),
    ):
        yield from send(model, "begin", invalid)
        yield Inputs()

    # Too few rows and a failed CRC leave regular live traffic intact.
    two_rows = (0xCAFFEE, (1 << 127) | 0xFEED)
    yield from send(model, "begin", header(2, rows=two_rows))
    yield from send(model, "row", Row(1, 0, True))
    yield from send(model, "event", event(0, 1, 44))
    yield from send(model, "row", Row(2, two_rows[0], True))
    yield Inputs()
    bad_packet = replace(header(3, rows=two_rows), crc=words_crc(two_rows) ^ 1)
    yield from send(model, "begin", bad_packet)
    yield from send(model, "row", Row(3, two_rows[0]))
    yield from send(model, "row", Row(3, two_rows[1], True))
    yield Inputs()

    rejected = header(4, rows=two_rows)
    yield from send(model, "begin", rejected)
    yield from send(model, "row", Row(4, two_rows[0]))
    yield from send(model, "row", Row(4, two_rows[1], True))
    yield from send(model, "event", event(1, 4, 90), restore_ready=False)
    for _ in range(5):
        yield Inputs(restore_ready=False)
    yield from until(model, Phase.PREPARE)
    yield from send(model, "prepared", Prepared(2, True))
    yield from send(model, "prepared", Prepared(4, False))
    yield from until(model, Phase.IDLE)

    clean = header(5, sequence=U64-1, rows=two_rows, force=True)
    yield from stage(model, clean, two_rows)
    yield from send(model, "event", event(0, 1, U64-8), m_ready=False)
    yield from send(model, "prepared", Prepared(5, True), m_ready=False)
    yield from send(model, "event", event(0, 1, U64-7), m_ready=False, config_ready=False)
    for _ in range(8):
        yield Inputs(m_ready=False, config_ready=False)
    yield from until(model, Phase.CONFIGURE, config_ready=False)
    for _ in range(5):
        yield Inputs(config_ready=False)
    yield from send(model, "event", event(1, 4, 91))
    yield from until(model, Phase.WAIT_ACK)
    yield from send(model, "event", event(0, 2, clean.sequence), restore_ready=False)
    yield from send(model, "ack", ReceiverAck(0, 0, 2, True), restore_ready=False)
    for offset in range(1, g.deltas):
        yield from send(model, "event", event(0, 2, clean.sequence+offset), restore_ready=False)
    extra = event(0, 2, clean.sequence+g.deltas)
    for _ in range(5):
        yield Inputs(event=extra, restore_ready=False)
    # Hold the first post-FIFO event stable until the buffered deltas drain.
    yield from send(model, "event", extra, done_ready=False)
    for _ in range(5):
        yield Inputs(done_ready=False)
    yield from until(model, Phase.IDLE)
    yield from send(model, "event", event(0, 1, 300))
    yield from send(model, "event", event(0, 2, clean.sequence+g.deltas+10))
    yield from send(model, "event", event(0, 2, clean.sequence+g.deltas+1))
    yield from send(model, "event", event(g.slots, 0, 1))

    # A discontinuity before COMMIT is offered can still abort the restore.
    before_gap = header(6, epoch=2, sequence=890, rows=two_rows)
    yield from stage(model, before_gap, two_rows)
    yield from stage_ack(model, before_gap)
    yield from send(model, "event", event(0, 3, 890))
    yield from send(model, "event", event(0, 3, 892))
    yield from send(model, "ack", ReceiverAck(0, 0, 3, True), restore_ready=False)
    yield from until(model, Phase.IDLE)

    # An offered COMMIT holds its value even if a delta fails while stalled.
    held_gap = header(7, epoch=3, sequence=990, rows=two_rows)
    yield from stage(model, held_gap, two_rows)
    yield from stage_ack(model, held_gap)
    yield from send(model, "ack", ReceiverAck(0, 0, 4, True), restore_ready=False)
    yield from send(model, "event", event(0, 4, 990), restore_ready=False)
    yield from send(model, "event", event(0, 4, 992), restore_ready=False)
    for _ in range(5):
        yield Inputs(restore_ready=False)
    yield from until(model, Phase.IDLE)
    yield from send(model, "event", event(0, 4, 991))

    native = header(8, epoch=4)
    yield from stage(model, native, ())
    yield from stage_ack(model, native)
    yield from send(model, "ack", ReceiverAck(2, 0, 5, True))
    yield from until(model, Phase.IDLE)
    yield from send(model, "event", event(0, 5, 1))
    yield from send(model, "event", event(1, 4, 92), monitor=1)

    empty = header(9, slot=1, epoch=4, sequence=100)
    yield from stage(model, empty, ())
    yield from stage_ack(model, empty)
    yield from send(model, "ack", ReceiverAck(0, 1, 5, True))
    yield from until(model, Phase.IDLE)
    yield from send(model, "event", event(1, 5, 100), monitor=1)
    yield Inputs(monitor=1)
    yield Inputs(reset=True)


def random_rounds(model, seed, rounds):
    rng = random.Random(seed)
    id_source = 20
    for index in range(rounds):
        if index % 15 == 0:
            yield Inputs(reset=True)
        id_source += 1
        target = rng.randrange(model.g.slots)
        ctx = model.contexts[target]
        observed = ctx.epoch if ctx.known else 1
        if observed == (1 << model.g.epoch_width)-1:
            yield Inputs(reset=True)
            observed = 1
        sequence = rng.choice((651, 24+index, U64-1, U64))
        rows = tuple(rng.randrange(1 << 128) for _ in range(rng.randint(0, model.g.rows)))
        details = header(id_source, target, observed, sequence, rows, bool(rng.randrange(2)))
        yield from stage(model, details, rows)
        yield from send(model, "prepared", Prepared(id_source, rng.random() > 0.1))
        if model.phase == Phase.FINALIZE:
            yield from until(model, Phase.IDLE)
            continue
        yield from until(model, Phase.WAIT_ACK)
        expected_epoch = observed + 1
        before = rng.randrange(model.g.deltas+1)
        for offset in range(before):
            corrupt = rng.random() < 0.10
            delta = event(target, expected_epoch, sequence+offset+int(corrupt), rng.getrandbits(346))
            yield from send(model, "event", delta)
        ack = ReceiverAck(0 if rng.random() < 0.93 else 2, target, expected_epoch, True)
        yield from send(model, "ack", ack)
        for _ in range(rng.randrange(8)):
            noise_slot = (target+1) % model.g.slots
            side = model.contexts[noise_slot]
            elsewhere = event(noise_slot, side.epoch, side.sequence, rng.getrandbits(346))
            yield Inputs(event=elsewhere, restore_ready=False, m_ready=True, monitor=noise_slot)
        yield from until(model, Phase.IDLE)
        for slot in range(model.g.slots+1):
            yield Inputs(monitor=slot)


def header_fields(head, g):
    return ((head.identity, 32), (head.slot, g.slot_width), (head.epoch, g.epoch_width),
            (head.session, 80), (head.sequence, 64), (head.count, g.count_width), (head.crc, 32),
            (head.force, 1), (head.spacing, g.wait_width))


def event_fields(evt, g):
    return ((evt.slot, g.slot_width), (evt.epoch, g.epoch_width), (evt.data, 588))


def vector(port, pins, g):
    row, prep, ack = port.row or Row(), port.prepared or Prepared(), port.ack or ReceiverAck()
    restore, cfg, done, info = pins.restore, pins.config, pins.done, pins.monitor
    input_flags = pack(*((value, 1) for value in (
        port.reset, port.begin is not None, port.row is not None, port.prepared is not None,
        port.ack is not None, port.event is not None, port.restore_ready, port.config_ready,
        port.m_ready, port.done_ready,
    )))
    output_flags = pack(*((value, 1) for value in (
        pins.begin_ready, pins.row_ready, pins.prepared_ready, pins.ack_ready, pins.s_ready,
        restore is not None, cfg is not None, pins.output is not None, done is not None, info is not None,
    )))
    return " ".join(f"{item:x}" for item in (
        input_flags, pack(*header_fields(port.begin or Header(), g)),
        pack((row.identity, 32), (row.word, 128), (row.last, 1)),
        pack((prep.identity, 32), (prep.ok, 1)),
        pack((ack.status, 2), (ack.slot, g.slot_width), (ack.epoch, g.epoch_width), (ack.active, 1)),
        pack(*event_fields(port.event or Event(), g)),
        port.monitor,
        output_flags,
        0 if restore is None else pack(
            (restore.kind, 2), (restore.header.identity, 32), (restore.header.slot, g.slot_width),
            (restore.header.epoch + 1, g.epoch_width), (restore.header.sequence, 64),
            (restore.header.count, g.count_width), (restore.index, g.count_width),
            (restore.header.crc, 32), (restore.word, 128),
        ),
        0 if cfg is None else pack(
            (cfg.slot, g.slot_width), (cfg.epoch, g.epoch_width), (1, 1), (cfg.force, 1),
            (cfg.session, 80), (cfg.sequence, 64), (cfg.spacing, g.wait_width),
        ),
        0 if pins.output is None else pack(*event_fields(pins.output, g)),
        0 if done is None else pack(
            (done.identity, 32), (done.slot, g.slot_width), (done.status, 4),
            (done.epoch, g.epoch_width), (done.receiver_installed, 1), (done.book_committed, 1),
            (done.buffered, g.delta_width), (done.crc, 32),
        ),
        0 if info is None else pack(
            (info.known, 1), (info.quarantined, 1), (info.epoch, g.epoch_width), (info.sequence, 64),
            *((count, 32) for count in info.counts),
        ),
        pack((pins.phase, 4), (pins.gap, 1), (pins.occupancy, g.delta_width), (pins.candidate, 64),
             *((count, 32) for count in pins.counts)),
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--default", action="store_true")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--seed", type=int, default=61387)
    parser.add_argument("--rounds", type=int, default=200)
    args = parser.parse_args()
    if args.depth < 1 or args.rounds < 0:
        parser.error("delta depth must be positive and rounds must be nonnegative")
    geometry = Geometry(3, 16, 16, 16, 8) if args.default else Geometry(deltas=args.depth)
    model = Model(geometry)
    with args.output.open("w", encoding="ascii") as stream:
        for campaign in (directed(model), random_rounds(model, args.seed, args.rounds)):
            for port in campaign:
                pins = model.advance(port)
                stream.write(vector(port, pins, geometry) + "\n")


if __name__ == "__main__":
    main()
