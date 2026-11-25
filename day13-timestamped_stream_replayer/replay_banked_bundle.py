"""Stage validated TRP partitions with alternating bank ownership in simulation."""

import argparse
import csv
from itertools import zip_longest
from pathlib import Path

from banked_replay_model import BankedReplay, Port, State
from gen_banked_replay_vectors import row, send
from replay_partition import read_bundle, read_bundle_parts, write_bundle
from replay_program import Command, Operation, Packet, ReplayError, Tape


HEADER = ("wall", "run", "bank", "data", "keep", "last", "tag", "due", "offered", "pass")


def fixture(directory):
    """Build a dense synthetic partition transition scenario with partial lanes."""
    tape = Tape.from_packets(
        Packet.paced(index + 101, bytes([index + 1]) * (9 + index % 6), 2 * index, 1)
        for index in range(6)
    )
    if directory.exists():
        if read_bundle(directory) != tape:
            raise ReplayError(f"existing demo bundle differs from fixture at {directory}")
    else:
        write_bundle(directory, tape, 3)


def commands(model, parts, depth):
    """Wait for ownership release and load the N+1 image while N is running."""
    yield Port(reset=True)
    origin = 4 * depth + 6
    for index, part in enumerate(parts):
        bank = index % 2
        while model.snapshot(bank)[0] != State.WRITABLE:
            yield Port()
        yield from send(model, Command(Operation.CLEAR), bank)
        for event in part.tape.events:
            yield from send(model, Command.append(event), bank)
        while model.pending:
            yield Port()
        yield from send(model, Command.start(), bank, release=origin + part.origin)
    while model.owner or model.pending:
        yield Port()
    yield Port()


def build(directory, output, depth, run_width, maximum):
    parts = read_bundle_parts(directory)
    if not parts:
        raise ReplayError("bundle has no nonempty partitions")
    if len(parts) >= 1 << run_width:
        raise ReplayError("bundle requires more run IDs than the hardware can assign")
    for part in parts:
        part.tape.checked_depth(depth)
    model = BankedReplay(depth, run_width)
    rows = []
    for index, port in enumerate(commands(model, parts, depth)):
        if index >= maximum:
            raise ReplayError(f"simulation cycle budget {maximum} exceeded")
        rows.append(row(port, model.advance(port), run_width, depth.bit_length()))
    output.write_text("\n".join(rows) + "\n", encoding="ascii")


def check(directory, capture):
    parts = read_bundle_parts(directory)
    if not parts:
        raise ReplayError("bundle has no nonempty partitions")
    expected = (
        (part.index + 1, part.index % 2, event)
        for part in parts for event in part.tape.events
    )
    previous_wall = -1
    count = 0
    with capture.open(encoding="ascii", newline="") as source:
        transfers = csv.DictReader(source)
        if tuple(transfers.fieldnames or ()) != HEADER:
            raise ReplayError("invalid transfer capture header")
        for count, (want, got) in enumerate(zip_longest(expected, transfers), 1):
            if want is None or got is None:
                raise ReplayError(f"wrong number of transferred beats at row {count}")
            if None in got or any(value is None for value in got.values()):
                raise ReplayError(f"wrong number of fields at transfer {count}")
            run_id, bank, event = want
            expected_values = (
                run_id, bank, event.data, event.keep, int(event.last), event.tag, event.tick, 1,
            )
            actual_values = (
                int(got["run"]), int(got["bank"]), int(got["data"], 16), int(got["keep"], 16),
                int(got["last"]), int(got["tag"]), int(got["due"]), int(got["pass"]),
            )
            wall = int(got["wall"])
            if expected_values != actual_values:
                raise ReplayError(f"wrong bank, run, framing, or payload at transfer {count}")
            if int(got["offered"]) < event.tick or wall <= previous_wall:
                raise ReplayError(f"invalid timestamp at transfer {count}")
            previous_wall = wall
    return len(parts), count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    demo = subparsers.add_parser("fixture", help="write a small deterministic demonstration bundle")
    demo.add_argument("directory", type=Path)
    generate = subparsers.add_parser("build", help="compile a bundle into checked cycle vectors")
    generate.add_argument("directory", type=Path)
    generate.add_argument("output", type=Path)
    generate.add_argument("--depth", type=int, default=3)
    generate.add_argument("--run-width", type=int, default=3)
    generate.add_argument("--max-cycles", type=int, default=100_000)
    audit = subparsers.add_parser("check", help="compare accepted RTL beats with the original images")
    audit.add_argument("directory", type=Path)
    audit.add_argument("capture", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "fixture":
            fixture(args.directory)
        elif args.operation == "build":
            if min(args.depth, args.run_width, args.max_cycles) < 1:
                raise ReplayError("all simulation bounds must be positive")
            build(args.directory, args.output, args.depth, args.run_width, args.max_cycles)
        else:
            parts, beats = check(args.directory, args.capture)
            print(f"PASS banked bundle: {parts} validated partitions, {beats} accepted beats")
    except (KeyError, OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
