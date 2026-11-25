"""Verify queue priority and an owner-scoped withdrawal on the real RTL."""

import argparse
from pathlib import Path

from reserve_demo import run
from reserve_model import Kind, PROFILES
from reserve_verify import parse_trace
from reserve_workload import management_examples


def write_demo(geometry, destination):
    """Issue a known management sequence, including a held bulk response."""
    with destination.open("w", encoding="ascii") as stream:
        for offset, command in enumerate(management_examples(geometry)):
            if offset == 11:
                print("1 0 0 a", file=stream)
                print("3 0 0 a", file=stream)
            tag = 0x40 + offset
            packed = command.payload(geometry)
            print(f"0 {command.bank:x} {tag:x} {packed:x}", file=stream)


def check_demo(geometry, capture):
    """Compare the key transfers to fixed answers besides the full reference audit."""
    grouped = {}
    held_cancel = 0
    for sample in parse_trace(capture, geometry):
        event = sample.output
        if event and sample.output_ready:
            grouped.setdefault(event.tag, []).append((sample.cycle*10+5, event.result))
        elif event and event.result.kind == Kind.MASS_CANCEL:
            held_cancel += 1

    def rows(tag):
        return [value for _, value in grouped[tag]]

    [kept] = rows(0x42)
    [renewed] = rows(0x47)
    if (kept.refreshed, kept.total, kept.visible, renewed.refreshed, renewed.total, renewed.visible) != (
        False, 6, 3, True, 8, 3,
    ):
        raise AssertionError("replace priority or visible state differs from the worked example")
    if [(rows(tag)[0].maker, rows(tag)[0].quantity) for tag in (0x43, 0x48)] != [(0, 1), (1, 1)]:
        raise AssertionError("same-price fill priority did not reflect the replacements")
    for tag, kind in ((0x4A, Kind.MASS_PREVIEW), (0x4B, Kind.MASS_CANCEL)):
        *orders, terminal = rows(tag)
        if [(r.maker, r.total, r.visible, r.kind) for r in orders] != [(0, 8, 3, kind), (2, 4, 4, kind)]:
            raise AssertionError("bulk accepted order image differs")
        if (terminal.level_count, terminal.level_total, terminal.level_visible) != (2, 12, 7):
            raise AssertionError("bulk totals differ")
    *surviving, final = rows(0x4D)
    if [(row.maker, row.owner, row.total) for row in surviving] != [(1, 2, 5)] or final.level_count != 1:
        raise AssertionError("the owner-scoped operation touched another account")
    if not held_cancel:
        raise AssertionError("the directed fixture never stalled its cancellation traffic")
    print(f"Managed reserve fixture passed with {held_cancel} held cancel cycles.")
    for tag in (0x42, 0x43, 0x47, 0x48, 0x4A, 0x4B):
        details = [(edge, Kind(result.kind).name, result.maker, result.last) for edge, result in grouped[tag]]
        print(f"  tag 0x{tag:x} transfers {details}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim", required=True, type=Path)
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--waveform", type=Path)
    args = parser.parse_args()
    geometry = PROFILES["default"]
    args.build.mkdir(parents=True, exist_ok=True)
    tape = args.build / "management.tape"
    capture = args.build / "management.trace"
    write_demo(geometry, tape)
    run(args.vvp, args.sim, tape, capture, args.waveform)
    check_demo(geometry, capture)


if __name__ == "__main__":
    main()
