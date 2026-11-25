"""Compile, capture and independently audit the single-price auction RTL."""

import argparse
from collections import Counter, deque
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
import subprocess

from auction_model import Auction, Command, Kind, PROFILES, Result
from auction_workload import directed, epochs, worked_example, write_tape


@dataclass(frozen=True)
class Sample:
    cycle: int
    reset: bool
    source_ready: bool
    sink_ready: bool
    command: Command
    result: Result


def parse_capture(path, geometry):
    """Decode all observed pre-edge ready/valid values and payloads."""
    with path.open(encoding="ascii") as capture:
        for number, line in enumerate(capture):
            fields = line.split()
            if len(fields) != 8 or int(fields[0]) != number or any(
                bit not in ("0", "1") for bit in fields[1:6]
            ):
                raise ValueError(f"malformed or discontinuous auction cycle {number}")
            reset, in_valid, in_ready, out_valid, out_ready = map(lambda bit: bit == "1", fields[1:6])
            incoming, outgoing = (int(word, 16) for word in fields[6:8])
            yield Sample(
                number, reset, in_ready, out_ready,
                Command.unpack(incoming, geometry) if in_valid else None,
                Result.unpack(outgoing, geometry) if out_valid else None,
            )


class Scoreboard:
    def __init__(self, geometry):
        self.geometry = geometry
        self.reference = Auction(geometry)
        self.expected = deque()
        self.deadline = None
        self.offered = None
        self.held_command = None
        self.metrics = Counter()
        self.kinds = Counter()
        self.rejections = Counter()

    @staticmethod
    def require(condition, cycle, message):
        if not condition:
            raise AssertionError(f"auction cycle {cycle}: {message}")

    def reset(self, sample):
        self.require(not sample.source_ready and not sample.result, sample.cycle, "reset leaked a handshake")
        self.metrics["reset_cycles"] += 1
        self.metrics["flushed_results"] += len(self.expected)
        self.reference = Auction(self.geometry)
        self.expected.clear()
        self.deadline = None
        self.offered = None
        self.held_command = None

    def retire(self, sample):
        expected, cycle = self.expected[0], sample.cycle
        self.require(sample.result == expected, cycle, f"expected {expected} but received {sample.result}")
        if not sample.sink_ready:
            self.metrics["stalled_results"] += 1
            self.offered = expected
            return
        self.metrics["accepted_results"] += 1
        self.kinds[Kind(expected.kind).name] += 1
        if expected.reason:
            self.rejections[expected.reason] += 1
        self.expected.popleft()
        self.offered = None
        if self.expected:
            following = self.expected[0]
            self.deadline = cycle + (2 if following.kind == Kind.DONE else 1)
        else:
            self.deadline = None

    def step(self, sample):
        cycle = sample.cycle
        self.metrics["cycles"] += 1
        if sample.reset:
            self.reset(sample)
            return
        self.require(sample.source_ready == (not self.expected), cycle,
                     "command admission did not wait for the preceding terminal transfer")
        if self.held_command is not None:
            self.require(sample.command == self.held_command, cycle, "source changed a blocked request")
        if self.offered is not None:
            self.require(sample.result == self.offered, cycle, "held result changed or disappeared")
        if self.deadline is not None and self.offered is None:
            self.require((sample.result is not None) == (cycle == self.deadline), cycle,
                         f"result was expected to start on edge {self.deadline}")
        if sample.result is not None:
            self.require(self.expected, cycle, "unsolicited result")
            self.retire(sample)
        if sample.command is not None and not sample.source_ready:
            self.held_command = sample.command
            self.metrics["stalled_commands"] += 1
        else:
            self.held_command = None
        if sample.command is not None and sample.source_ready:
            band_before = self.reference.band
            events = self.reference.execute(sample.command)
            self.expected.extend(events)
            first = events[0]
            self.deadline = cycle + (
                band_before.stop+2 if first.kind in (Kind.INDICATIVE, Kind.CLEARING) else 1
            )
            self.metrics["accepted_commands"] += 1
            if first.kind == Kind.OPENED and first.epoch == 0:
                self.metrics["epoch_wraps"] += 1


def audit(capture, geometry, submitted):
    """Check semantics and handshake timing to the drained end of capture."""
    scoreboard = Scoreboard(geometry)
    for sample in parse_capture(capture, geometry):
        scoreboard.step(sample)
    if scoreboard.metrics["accepted_commands"] != submitted or scoreboard.expected:
        raise AssertionError("auction simulation did not accept and account for the complete tape")
    return scoreboard


def known_answers(capture, geometry):
    """Verify the exact worked price, five matched pairs, and three residuals."""
    relevant = [
        (sample.cycle*10+15, sample.result)
        for sample in parse_capture(capture, geometry)
        if sample.result is not None and sample.sink_ready and sample.result.kind >= Kind.INDICATIVE
    ]
    first, clearing, *tail = (result for _, result in relevant)
    if (first.kind, clearing.kind, clearing.price, clearing.quantity, clearing.bids, clearing.asks) != (
        Kind.INDICATIVE, Kind.CLEARING, 7, 17, 21, 17,
    ):
        raise AssertionError("worked example produced an incorrect clearing point")
    fills = [(row.first, row.second, row.quantity) for row in tail if row.kind == Kind.TRADE]
    residual = [(row.kind, row.first, row.quantity) for row in tail if row.kind in (Kind.CARRY, Kind.EXPIRE)]
    if fills != [(0,3,4), (0,4,2), (1,4,3), (1,5,5), (2,5,3)]:
        raise AssertionError(f"worked pairs disagreed: {fills}")
    if residual != [(Kind.CARRY,2,4), (Kind.EXPIRE,6,9), (Kind.CARRY,7,9)]:
        raise AssertionError(f"worked carry and expiry disagreed: {residual}")
    if (tail[-1].kind, tail[-1].quantity, tail[-1].count, tail[-1].carried, tail[-1].expired) != (
        Kind.DONE, 17, 5, 2, 1,
    ):
        raise AssertionError("worked closing summary disagreed")
    print([(time, Kind(result.kind).name, result.first, result.second) for time,result in relevant])


def run(args, profile, items, example=False):
    geometry = PROFILES[profile]
    directory = args.build / ("example" if example else profile)
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "auction.vvp"
    tape, capture = directory / "commands.txt", directory / "capture.txt"
    submitted = write_tape(tape, geometry, items, first_tag=0x20 if example else 0)
    top = "tb_call_auction_engine"
    compile_args = [args.iverilog, "-g2012", "-s", top]
    compile_args.extend(f"-P{top}.{key}={value}" for key,value in geometry.parameters().items())
    compile_args.extend(("-o", str(binary), "call_auction_engine.sv", f"{top}.sv"))
    subprocess.run(compile_args, check=True)
    command = [args.vvp, str(binary), f"+tape={tape}", f"+trace={capture}"]
    if args.wave:
        args.wave.parent.mkdir(parents=True, exist_ok=True)
        command.append(f"+wave={args.wave}")
    subprocess.run(command, check=True)
    stats = audit(capture, geometry, submitted)
    print(f"{profile}: {dict(stats.metrics)} {dict(stats.kinds)}", flush=True)
    if example:
        known_answers(capture, geometry)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--seed", type=int, default=30271)
    parser.add_argument("--epochs", type=int, default=135)
    parser.add_argument("--profile", choices=PROFILES, action="append")
    parser.add_argument("--build", type=Path, default=Path("build")/"auction")
    parser.add_argument("--example", action="store_true")
    parser.add_argument("--wave", type=Path)
    args = parser.parse_args()
    if args.epochs < 1 or args.wave and not args.example:
        parser.error("epoch count must be positive and waveform capture uses --example")
    if args.example:
        run(args, "default", worked_example(), example=True)
        return
    all_kinds = Counter()
    for profile in args.profile or PROFILES:
        g = PROFILES[profile]
        records = chain(directed(g), ((2, 0),), epochs(g, args.epochs, args.seed))
        stats = run(args, profile, records)
        all_kinds.update(stats.kinds)
        if len(stats.rejections) != 9 or not all(
            stats.metrics[key] for key in ("flushed_results", "stalled_results", "stalled_commands")
        ):
            raise AssertionError(f"incomplete coverage in {profile}: {stats.rejections} {stats.metrics}")
        if g.epoch_bits <= 3 and not stats.metrics["epoch_wraps"]:
            raise AssertionError(f"missing epoch wrap in {profile}")
    if not args.profile and len(all_kinds) != len(Kind):
        raise AssertionError(f"some auction event kinds did not appear: {all_kinds}")


if __name__ == "__main__":
    main()
