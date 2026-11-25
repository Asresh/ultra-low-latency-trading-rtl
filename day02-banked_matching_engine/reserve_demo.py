"""Capture a live book and verify restored priority in a fresh RTL run."""

import argparse
from pathlib import Path
import subprocess

from reserve_checkpoint import Checkpoint, ImageOrder, extract, write_tape
from reserve_model import Command, Kind, Operation, PROFILES
from reserve_verify import audit, parse_trace


def run(simulator, image, tape, trace, waveform=None):
    """Drive one simulation and check its entire captured pin trace."""
    args = [simulator, str(image), f"+tape={tape}", f"+trace={trace}"]
    if waveform:
        args.append(f"+waveform={waveform}")
    subprocess.run(args, check=True)
    return audit(trace, PROFILES["default"])


def capture_source(args, geometry):
    """Build a nontrivial book, including a replenished order and a partial maker."""
    tape = args.build / "example-source.tape"
    trace = args.build / "example-source.trace"
    commands = (
        Command(tag=0x10, side=1, order_id=0, price=4, quantity=9, display=3, owner=1),
        Command(tag=0x11, side=1, order_id=1, price=4, quantity=7, owner=2),
        Command(tag=0x12, side=1, order_id=2, price=5, quantity=5, display=2, owner=3),
        Command(tag=0x13, side=0, order_id=3, price=4, quantity=4, owner=4),
        Command(tag=0x14, operation=Operation.SNAPSHOT),
    )
    write_tape(commands, geometry, tape)
    lines = tape.read_text(encoding="ascii").splitlines()
    lines.insert(3, "3 0 0 7")
    tape.write_text("\n".join(lines) + "\n", encoding="ascii")
    run(args.vvp, args.sim, tape, trace, args.waveform)
    captures = list(extract(parse_trace(trace, geometry), geometry))
    if len(captures) != 1:
        raise AssertionError(f"expected one snapshot, got {len(captures)}")
    want = (
        ImageOrder(1, 1, 4, 6, 6, 0, 2),
        ImageOrder(2, 1, 5, 5, 2, 2, 3),
        ImageOrder(0, 1, 4, 6, 3, 3, 1),
    )
    found = captures[0]
    if found.tag != 0x14 or found.checkpoint.orders != want:
        raise AssertionError(f"source book content differs from the worked example: {found!r}")
    file_path = args.build / "example-checkpoint.rsv"
    file_path.write_bytes(found.checkpoint.to_bytes())
    return Checkpoint.from_bytes(file_path.read_bytes())


def restore_target(args, checkpoint):
    """Validate the exact imported image and the resulting fill order."""
    tape = args.build / "example-restore.tape"
    trace = args.build / "example-restore.trace"
    geometry = checkpoint.geometry
    destination = 1
    first_tag = 0x30
    commands = list(checkpoint.restore(first_tag, destination, capture_after=True))
    commands.append(Command(
        bank=destination, tag=0x41, side=0, order_id=3, price=5, quantity=8, owner=7,
    ))
    write_tape(commands, geometry, tape)
    stats = run(args.vvp, args.sim, tape, trace)
    captures = list(extract(parse_trace(trace, geometry), geometry))
    if len(captures) != 1 or captures[0].checkpoint != Checkpoint(
        geometry, destination, checkpoint.orders,
    ):
        raise AssertionError("readback after restore disagrees with the captured book")
    fills = []
    for sample in parse_trace(trace, geometry):
        event = sample.output
        if (sample.output_ready and event and event.tag == 0x41
                and event.result.kind == Kind.FILL):
            fills.append((event.result.maker, event.result.quantity))
    if fills != [(1, 6), (0, 2)]:
        raise AssertionError(f"restored queue lost price-time priority: {fills}")
    print(
        f"Checkpoint round trip checked {len(checkpoint.orders)} rows, {fills} "
        f"and {stats['coverage']['accepted_results']} restored results."
    )


def main():
    """Run checked source capture and restored-state use in two fresh simulations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim", required=True, type=Path)
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--waveform", type=Path)
    args = parser.parse_args()
    args.build.mkdir(parents=True, exist_ok=True)
    image = capture_source(args, PROFILES["default"])
    restore_target(args, image)


if __name__ == "__main__":
    main()
