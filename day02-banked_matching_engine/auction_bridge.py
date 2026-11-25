"""Convert one audited auction close into a standalone reserve-book checkpoint."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path

from auction_model import Kind, PROFILES as AUCTION_PROFILES
from auction_verify import Scoreboard, parse_capture
from reserve_checkpoint import BadCheckpoint, Checkpoint, ImageOrder, write_tape
from reserve_model import PROFILES as RESERVE_PROFILES


@dataclass(frozen=True)
class Closing:
    reset_epoch: int
    epoch: int
    tag: int
    indicative_tick: int
    paired_quantity: int
    trades: int
    expired: int
    rows: tuple


def collect_closings(capture, geometry):
    """Recognize only complete CLOSE transactions after a full pin-level audit."""
    scoreboard = Scoreboard(geometry)
    seen = 0
    closings = []
    active = None
    carry = []
    reset_epoch = 0
    initialized = False
    for sample in parse_capture(capture, geometry):
        if not initialized and not sample.reset:
            raise ValueError("capture must start at a reset boundary")
        initialized = True
        scoreboard.step(sample)
        seen += 1
        if sample.reset:
            reset_epoch += 1
            active = None
            carry.clear()
        if sample.result is None or not sample.sink_ready:
            continue
        event = sample.result
        if event.kind == Kind.CLEARING:
            active = event
        elif event.kind == Kind.CARRY:
            carry.append(ImageOrder(
                event.first, event.side, event.price, event.quantity, event.quantity,
                0, event.first_owner,
            ))
        elif event.kind == Kind.DONE:
            if not active or len(carry) != event.carried:
                raise ValueError(f"closing summary is incomplete for tag 0x{event.tag:x}")
            closings.append(Closing(
                reset_epoch, event.epoch, event.tag, active.price, active.quantity,
                event.count, event.expired, tuple(carry),
            ))
            active = None
            carry.clear()
    if not seen or scoreboard.expected or active:
        raise ValueError("capture ended before its last acknowledged result")
    return closings


def choose_closing(closings, index=None):
    """Require explicit disambiguation when the capture contains several closes."""
    if not closings:
        raise ValueError("no complete and acknowledged auction close in capture")
    if index is None:
        if len(closings) != 1:
            raise ValueError("select --close-index when a capture contains more than one auction close")
        return closings[0]
    if not 0 <= index < len(closings):
        raise ValueError("close index is outside the observed list")
    return closings[index]


def checkpoint_for(closing, geometry, bank):
    """Validate capacity, ABI widths, and an uncrossed limit book before export."""
    try:
        return Checkpoint(geometry, bank, closing.rows)
    except (BadCheckpoint, ValueError) as exc:
        raise ValueError(f"residual book cannot be loaded safely: {exc}") from exc


def check_paths(capture, image, tape):
    """Reject destructive filename and inode aliasing across the workflow."""
    paths = [capture, image] + ([tape] if tape is not None else [])
    for index, first in enumerate(paths):
        for second in paths[index+1:]:
            if first.resolve() == second.resolve() or (
                first.exists() and second.exists() and first.samefile(second)
            ):
                raise ValueError("capture, image, and optional restore tape must be distinct files")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("image", type=Path)
    parser.add_argument("--auction-profile", choices=AUCTION_PROFILES, default="default")
    parser.add_argument("--reserve-profile", choices=RESERVE_PROFILES, default="default")
    parser.add_argument("--close-index", type=int)
    parser.add_argument("--bank", type=int, default=0)
    parser.add_argument("--restore-tape", type=Path)
    args = parser.parse_args()
    try:
        check_paths(args.capture, args.image, args.restore_tape)
        closings = collect_closings(args.capture, AUCTION_PROFILES[args.auction_profile])
        closing = choose_closing(closings, args.close_index)
        checkpoint = checkpoint_for(closing, RESERVE_PROFILES[args.reserve_profile], args.bank)
        raw = checkpoint.to_bytes()
        args.image.write_bytes(raw)
        if args.restore_tape is not None:
            write_tape(checkpoint.restore(capture_after=True), checkpoint.geometry, args.restore_tape)
    except (OSError, AssertionError, ValueError) as exc:
        parser.exit(2, f"auction carry bridge: {exc}\n")
    print(json.dumps({
        "reset_epoch": closing.reset_epoch, "epoch": closing.epoch, "close_tag": closing.tag,
        "clearing_tick": closing.indicative_tick, "paired_quantity": closing.paired_quantity,
        "trades": closing.trades, "expired": closing.expired, "carried": len(closing.rows),
        "bank": args.bank, "checkpoint_bytes": len(raw),
    }, indent=2))


if __name__ == "__main__":
    main()
