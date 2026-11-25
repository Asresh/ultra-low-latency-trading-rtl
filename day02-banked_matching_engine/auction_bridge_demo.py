"""Replay true call-auction residuals through a fresh continuous reserve matcher."""

import argparse
from pathlib import Path

from auction_bridge import checkpoint_for, choose_closing, collect_closings
from auction_model import PROFILES as AUCTION
from reserve_checkpoint import Checkpoint, extract, write_tape
from reserve_demo import run
from reserve_model import Command, Kind, PROFILES as RESERVE
from reserve_verify import parse_trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--sim", type=Path, required=True)
    parser.add_argument("--vvp", default="vvp")
    args = parser.parse_args()
    args.build.mkdir(parents=True, exist_ok=True)
    checkpoint = checkpoint_for(
        choose_closing(collect_closings(args.capture, AUCTION["default"])), RESERVE["default"], 1,
    )
    image = args.build / "auction-carried.rsv"
    image.write_bytes(checkpoint.to_bytes())
    checkpoint = Checkpoint.from_bytes(image.read_bytes())
    tape, capture = args.build / "handoff.tape", args.build / "handoff.trace"
    commands = list(checkpoint.restore(first_tag=0x60, capture_after=True))
    sell = Command(bank=1, tag=0x77, order_id=5, owner=9, side=1, price=3, quantity=5)
    write_tape([*commands, sell], checkpoint.geometry, tape)
    run(args.vvp, args.sim, tape, capture)
    captured = tuple(extract(parse_trace(capture, checkpoint.geometry), checkpoint.geometry))
    if len(captured) != 1 or captured[0].checkpoint != checkpoint:
        raise AssertionError("continuous reserve book did not import the exact auction residuals")
    fills = [
        (sample.output.result.maker, sample.output.result.price, sample.output.result.quantity)
        for sample in parse_trace(capture, checkpoint.geometry)
        if sample.output is not None and sample.output_ready and sample.output.tag == 0x77
        and sample.output.result.kind == Kind.FILL
    ]
    if fills != [(2,7,4), (7,3,1)]:
        raise AssertionError(f"continuous fill price-time deviates after auction handoff: {fills}")
    print(f"Auction to reserve handoff checked two exact carries and continuous fills {fills}.")


if __name__ == "__main__":
    main()
