"""Generate an independent multi-session wire fixture for Icarus."""

import argparse
import random
from dataclasses import dataclass
from pathlib import Path

from wire_codec import MASK64, decode_itch, make_itch, make_packet, words


SESSION_NAMES = (b"RENAME0004", b"BETA000002", b"GAMMA00003")
FIRST_SEQUENCE = (904, 3, 502)
SLOT_COUNT = len(SESSION_NAMES)


@dataclass(frozen=True)
class WireFrame:
    slot: int
    datagram: bytes

    def encode(self, rng):
        """Only the first beat carries the route key selected by the fixture."""
        for data, keep, first, last in words(self.datagram, rng):
            key = self.slot if first else rng.randrange(SLOT_COUNT + 1)
            framing = first * 2 + last
            yield f"{key:d} {data:016x} {keep:02x} {framing:x}"


@dataclass(frozen=True)
class Phase:
    expected: tuple
    lanes: tuple

    def lines(self, rng):
        yield str(len(self.expected))
        for slot, event in self.expected:
            yield f"{slot:d} {event:0147x}"
        for lane in self.lanes:
            wire = [beat for frame in lane for beat in frame.encode(rng)]
            yield str(len(wire))
            yield from wire


def generate_message(rng):
    """Mix decoded orders, unsupported types, malformed bodies, and empty blocks."""
    draw = rng.randrange(16)
    if draw == 0:
        return b""
    if draw == 1:
        return b"Z synthetic extension"
    if draw == 2:
        return make_itch("D", rng)[:-1]
    if draw == 3:
        bad_side = bytearray(make_itch("F", rng))
        bad_side[19] = ord("!")
        return bytes(bad_side)
    return make_itch(rng.choice("AFECXDU"), rng)


def mix_lanes(rng, valid_frames, discarded):
    """Spread packets across both sources, often with a delayed redundant copy."""
    lanes = [[], []]
    for frame in valid_frames:
        target = rng.randrange(2)
        lanes[target].append(frame)
        if rng.randrange(4) != 0:
            lanes[target ^ 1].append(frame)
    for frame in discarded:
        lanes[rng.randrange(2)].append(frame)
    for lane in lanes:
        rng.shuffle(lane)
    return tuple(tuple(lane) for lane in lanes)


def progression(rng, phases):
    """Produce bounded bursts whose next phase follows full event retirement."""
    next_sequences = list(FIRST_SEQUENCE)
    for index in range(phases):
        expected, frames, rejected = [], [], []
        for slot, session in enumerate(SESSION_NAMES):
            start = next_sequences[slot]
            bodies = tuple(generate_message(rng) for _ in range(rng.randrange(2, 6)))
            for offset, body in enumerate(bodies):
                sequence = (start + offset) & MASK64
                expected.append((slot, decode_itch(sequence, body).bits()))
            next_sequences[slot] = (start + len(bodies)) & MASK64
            offset = 0
            while offset < len(bodies):
                step = rng.randrange(1, 3)
                block = bodies[offset:offset + step]
                wire = make_packet(session, start + offset, block)
                frames.append(WireFrame(slot, wire))
                offset += len(block)
            if index % 2 == 0:
                # An otherwise valid order from the wrong session stays in
                # the same physical feed, but cannot enter this context's window.
                wrong = SESSION_NAMES[(slot + 1) % SLOT_COUNT]
                rejected.append(WireFrame(slot, make_packet(wrong, start, bodies[:1])))
            if index % 3 == 0:
                # The final block is unavailable. Neither block may leak.
                rejected.append(WireFrame(
                    slot, make_packet(session, start, (make_itch("A", rng), b"truncated"))[:-2],
                ))
            if index % 4 == 0:
                rejected.append(WireFrame(SLOT_COUNT, make_packet(session, start, bodies[:1])))
        yield Phase(tuple(expected), mix_lanes(rng, frames, rejected))


def write_fixture(path, seed, phases):
    traffic_rng = random.Random(seed)
    lane_rng = random.Random(seed ^ 0x02af81bf)
    total = 0
    with path.open("w", encoding="ascii") as handle:
        handle.write(f"{phases}\n")
        for phase in progression(traffic_rng, phases):
            total += len(phase.expected)
            handle.writelines(line + "\n" for line in phase.lines(lane_rng))
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=73457)
    parser.add_argument("--phases", type=int, default=72)
    args = parser.parse_args()
    if not 1 <= args.phases <= 800:
        parser.error("--phases must be between 1 and 800")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    messages = write_fixture(args.output, args.seed, args.phases)
    print(f"Prepared {messages} expected events in {args.phases} independent phases")


if __name__ == "__main__":
    main()
