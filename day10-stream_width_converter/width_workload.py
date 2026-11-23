"""Generate replayable cycle tapes driven by directed and seeded packet scenarios."""

import argparse
from dataclasses import replace
from pathlib import Path
import random

from width_model import Beat, Drive, Gearbox, Report, full, to_beats


def payload(seed, size):
    """Generate repeatable data with enough lane variation to expose reordering."""
    return bytes((seed * 131 + n * 41 + (n >> 2)) & 255 for n in range(size))


def frames(width, maximum, rng, count):
    """Build legitimate frames and faults with a real last marker for recovery."""
    packets = []
    lengths = [1, max(1, width - 1), width, width + 1, 2 * width, maximum, maximum + 1]
    for index in range(len(lengths) + count):
        directed = index < len(lengths)
        size = lengths[index] if directed else rng.randint(1, maximum + width)
        packet = list(to_beats(payload(index, size), width, index % 73, fill=0xCD))
        kind = 0 if directed else rng.randrange(8)
        if kind == 2:
            packet[-1] = replace(packet[-1], keep=0)
        elif kind == 3:
            packet[-1] = replace(packet[-1], keep=1 << (width - 1))
        elif kind == 4 and len(packet) > 1:
            packet[1] = replace(packet[1], tag=packet[0].tag ^ 0x8000)
        elif kind == 5 and len(packet) > 1:
            packet[0] = replace(packet[0], keep=full(width - 1))
        packets.append(packet)
    return packets


def row(drive, observed):
    """Serialize one pre-edge oracle record for the Icarus testbench."""
    out = observed.output or Beat()
    report = observed.report or Report()
    return (
        drive.reset, drive.valid, drive.beat.data, drive.beat.keep,
        drive.beat.last, drive.beat.tag, drive.ready, drive.report_ready,
        observed.input_ready, observed.output is not None, out.data, out.keep,
        out.last, out.tag, observed.report is not None, report.tag, report.length,
        report.status, *observed.counters,
    )


def encode(values):
    """Use unsigned hexadecimal on every field so SV needs one format grammar."""
    return " ".join(f"{int(value):x}" for value in values)


def exercise(model, rng, packets):
    """Drive legal holds and separate packet/report stalls until every frame drains."""
    held = None
    queue = iter(beat for frame in packets for beat in frame)
    exhausted = False
    for cycle in range(150000):
        if held is None and not exhausted and (cycle < 48 or rng.randrange(5) != 0):
            held = next(queue, None)
            exhausted = held is None
        ready = cycle > 30 and rng.randrange(7) < 5
        report_ready = cycle > 20 and rng.randrange(7) < 5
        drive = Drive(held is not None, held or Beat(), ready, report_ready)
        observed = model.advance(drive)
        yield row(drive, observed)
        if observed.input_ready and held is not None:
            held = None
        if exhausted and held is None and not model.pending and model.report is None:
            return
    raise RuntimeError("reference failed to drain the directed traffic")


def reset_edges(model, rng):
    """Flush a held output, a valid report, and a partly captured frame."""
    widths = (model.input_width, model.output_width)
    for length, has_end, block_report in ((1, True, True), (widths[0] * 2, False, False)):
        beats = list(to_beats(payload(rng.randrange(256), length), widths[0], 0xCB))
        if not has_end:
            beats.pop()
        for beat in beats:
            drive = Drive(True, beat, False, not block_report)
            yield row(drive, model.advance(drive))
        drive = Drive(reset=True)
        yield row(drive, model.advance(drive))
        drive = Drive()
        yield row(drive, model.advance(drive))


def generate(directory, name, input_width, output_width, capacity, seed, count):
    """Create a tape and retain the number of observed rising edges."""
    rng = random.Random(seed)
    model = Gearbox(input_width, output_width, capacity)
    packet_rows = exercise(model, rng, frames(input_width, capacity, rng, count))
    directory.mkdir(parents=True, exist_ok=True)
    count_rows = 0
    with (directory / f"{name}.txt").open("w", encoding="ascii") as target:
        drive = Drive(reset=True)
        target.write(encode(row(drive, model.advance(drive))) + "\n")
        for values in reset_edges(model, rng):
            target.write(encode(values) + "\n")
            count_rows += 1
        for values in packet_rows:
            target.write(encode(values) + "\n")
            count_rows += 1
    return count_rows + 1


def main():
    """Generate the up/down and non-power-of-two study profiles."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--packets", type=int, default=240)
    args = parser.parse_args()
    for name, iw, ow, size, seed in (
        ("up", 4, 8, 128, 41910),
        ("down", 8, 4, 128, 80110),
        ("odd", 3, 5, 31, 35011),
        ("tiny", 1, 2, 3, 12310),
    ):
        edges = generate(args.directory, name, iw, ow, size, seed, args.packets)
        print(f"{name}: {edges} reference clock edges")


if __name__ == "__main__":
    main()
