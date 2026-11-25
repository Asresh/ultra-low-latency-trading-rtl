"""Insert drain barriers into checked packet and audit tapes."""
from argparse import ArgumentParser
from pathlib import Path
from tempfile import TemporaryDirectory
import random

from integrity_vectors import build


def read_lines(file):
    return [tuple(int(item, 16) for item in line.split())
            for line in file.read_text().splitlines() if line.strip()]


def rows_to(path, rows):
    path.write_text("".join(" ".join(f"{item:x}" for item in row)+"\n" for row in rows))


def split_input(rows):
    """Split the checked source tape at its real last markers."""
    batch = []
    for beat in rows:
        batch.append(beat)
        if beat[2]:
            yield batch
            batch = []
    if batch:
        raise ValueError("input tape ended with a partial packet")


def make_case(destination, source_lanes, sink_lanes, capacity, seed, count, count_width):
    rng = random.Random(seed)
    destination.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination) as work:
        root = Path(work)
        build(root, source_lanes, sink_lanes, capacity, seed, count)
        incoming = list(split_input(read_lines(root / "input.txt")))
        published = read_lines(root / "packets.txt")
        diagnostics = read_lines(root / "audit.txt")
    if len(incoming) != len(diagnostics):
        raise ValueError("input and diagnostics have different frame counts")

    commands = []
    reports = []
    approved = 0
    retired = 0
    marker = 0
    mask = (1 << count_width)-1

    def issue_marker(deferred=False):
        nonlocal marker
        marker += 1
        tag = (0xb000 + marker) & 0xffff
        return (2 if deferred else 1, 0, 0, 0, tag, 0, 0)

    def complete_marker(command):
        finalized = (1, *command[1:])
        commands.append(finalized)
        reports.append((1, finalized[4], 0, 0, 0, 0, 0,
                        retired & mask, approved & mask, approved))

    complete_marker(issue_marker())
    complete_marker(issue_marker())
    planned = False
    for index, (frame, record) in enumerate(zip(incoming, diagnostics)):
        # Issue one barrier while a source packet is open. Its command
        # may only transfer after the packet's real last beat.
        if index > 0 and not planned and len(frame) > 1:
            commands.append((0,*frame[0]))
            deferred = issue_marker(deferred=True)
            commands.append(deferred)
            commands.extend((0,*beat) for beat in frame[1:])
            planned = True
        else:
            deferred = None
            commands.extend((0,*beat) for beat in frame)
        retired += 1
        approved += record[5] == 0
        reports.append((0,*record,0,0,0))
        if deferred:
            complete_marker(deferred)
        if index in (1, 2, 4) or rng.randrange(11) == 0:
            complete_marker(issue_marker())
    complete_marker(issue_marker())
    rows_to(destination / "commands.txt", commands)
    rows_to(destination / "packets.txt", published)
    rows_to(destination / "reports.txt", reports)
    rows_to(destination / "totals.txt", [(len(reports), len(published), approved, marker)])
    print(f"CDC oracle {retired} inputs, {approved} outputs, {marker} barriers")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--in-bytes", type=int, required=True)
    parser.add_argument("--out-bytes", type=int, required=True)
    parser.add_argument("--capacity", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--frames", type=int, default=220)
    parser.add_argument("--count-width", type=int, default=4)
    args = parser.parse_args()
    make_case(args.directory, args.in_bytes, args.out_bytes, args.capacity,
              args.seed, args.frames, args.count_width)


if __name__ == "__main__":
    main()
