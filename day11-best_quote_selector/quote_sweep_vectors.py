"""Drive and compare deterministic RTL sweep plans and scheduling races."""

from __future__ import annotations

import argparse
import random
from collections import Counter
from dataclasses import astuple, dataclass, replace
from itertools import zip_longest
from pathlib import Path

from quote_model import QuoteBook
from quote_schema import Ack, Command, PROFILES, Profile, mask
from quote_sweep_model import FeePolicy, SweepCode, SweepQuery, SweepResult, plan
from quote_tool import simulation


@dataclass(frozen=True)
class Config:
    venue: int
    enabled: int
    cap: int
    fee: int


@dataclass(frozen=True)
class Cycle:
    tick: int
    reset: int = 0
    ack_ready: int = 1
    result_ready: int = 1
    command: Command | None = None
    config: Config | None = None
    commit: int = 0
    query: SweepQuery | None = None

    def encode(self) -> str:
        def offer(record, empty_width):
            return (0, *([0] * empty_width)) if record is None else (1, *astuple(record))

        words = (
            self.tick, self.reset, self.ack_ready, self.result_ready,
            *offer(self.command, 9), *offer(self.config, 4), self.commit, *offer(self.query, 8),
        )
        return " ".join(str(int(word)) for word in words)


@dataclass(frozen=True)
class Sample:
    number: int
    epoch: int
    cfg_error: int
    cmd_ready: int
    cfg_ready: int
    commit_ready: int
    query_ready: int
    ack: Ack | None = None
    result: SweepResult | None = None

    def encode(self, profile: Profile, fee_width: int) -> str:
        ack = f"1 {int(self.ack.code)} {self.ack.tag}" if self.ack else "0 0 0"
        result = f"1 {self.result.encode(profile, fee_width)}" if self.result else "0" + " 0"*10
        return (f"{self.number} {self.cmd_ready} {self.cfg_ready} {self.commit_ready} "
                f"{self.cfg_error} {self.epoch} {self.query_ready} {ack} {result}")


class SweepMachine:
    """Schedule already computed plans by completion time in a one-result machine."""

    def __init__(self, profile: Profile, fee_width: int, epoch_width: int = 8):
        self.profile = profile
        self.fee_width = fee_width
        self.epoch_mask = mask(epoch_width)
        self.book = QuoteBook(profile)
        self.cycles: list[Cycle] = []
        self.samples: list[Sample] = []
        self.checked = Counter()
        self.clear()

    def clear(self) -> None:
        self.book.reset()
        default = FeePolicy(1, self.profile.qty_max, 0)
        self.active = [default] * self.profile.venues
        self.shadow = self.active.copy()
        self.epoch = self.cfg_error = 0
        self.pending: tuple[int, SweepResult] | None = None
        self.ack: Ack | None = None

    def step(self, drive: Cycle) -> Sample:
        number = len(self.cycles)
        self.cycles.append(drive)
        if drive.reset:
            self.checked["reset"] += 1
            if self.pending:
                self.checked["reset_pending"] += 1
            self.clear()
            sample = Sample(number, 0, 0, 0, 0, 0, 0)
            self.samples.append(sample)
            return sample

        output = self.pending[1] if self.pending and self.pending[0] <= number else None
        query_ready = int(self.pending is None or (output is not None and drive.result_ready))
        cmd_ready = int(self.ack is None or drive.ack_ready)
        cfg_ready = int(not drive.commit)
        sample = Sample(number, self.epoch, self.cfg_error, cmd_ready, cfg_ready,
                        1, query_ready, self.ack, output)
        self.samples.append(sample)
        if self.ack and not drive.ack_ready:
            self.checked["ack_stall"] += 1
        if output and not drive.result_ready:
            self.checked["result_stall"] += 1
        if drive.query and not query_ready:
            self.checked["source_stall"] += 1
        if output and drive.result_ready:
            self.pending = None
            self.checked["result"] += 1
            self.checked["status_" + output.status.name] += 1

        # Queries sample both the quote and active policy before other actions.
        if drive.query and query_ready:
            response = plan(self.book, self.active, self.epoch, drive.query, drive.tick)
            self.pending = (number + response.scan_steps + 1, response)
            self.checked["query"] += 1
            if drive.command and cmd_ready:
                self.checked["data_race"] += 1
            if drive.commit:
                self.checked["policy_race"] += 1

        if cmd_ready:
            self.ack = self.book.apply(drive.command, drive.tick) if drive.command else None
            if self.ack:
                self.checked["command"] += 1

        self.cfg_error = 0
        if drive.commit:
            self.checked["commit"] += 1
            self.active, self.shadow = self.shadow, self.active
            self.epoch = (self.epoch + 1) & self.epoch_mask
        elif drive.config:
            row = drive.config
            if row.venue >= self.profile.venues:
                self.cfg_error = 1
                self.checked["config_error"] += 1
            else:
                self.shadow[row.venue] = FeePolicy(row.enabled, row.cap, row.fee)
                self.checked["config"] += 1
        return sample

    def write_drive(self, drive_path: Path, trace_path: Path) -> None:
        """Write the validated cycle stimulus before invoking the simulator."""
        if self.pending is not None or self.ack is not None:
            raise AssertionError("stimulus leaves an undrained result")
        with drive_path.open("w", encoding="ascii") as stream:
            for cycle in self.cycles:
                print(cycle.encode(), file=stream)
        if trace_path.exists():
            trace_path.unlink()

    def compare(self, trace_path: Path) -> None:
        with trace_path.open(encoding="ascii") as stream:
            for expected, actual in zip_longest(self.samples, stream):
                if expected is None or actual is None:
                    raise AssertionError("simulation and model produced different sample counts")
                expected_row = expected.encode(self.profile, self.fee_width)
                if actual.strip() != expected_row:
                    raise AssertionError(f"cycle {expected.number}\nRTL {actual.strip()}\n"
                                         f"ref {expected_row}\ndrive {self.cycles[expected.number]}")


def demo(machine: SweepMachine) -> None:
    """Check exact numbers for affected venues, fees, caps, and a shadow swap."""
    full = machine.profile.venue_mask
    invalid = machine.profile.venues
    buy = SweepQuery(0, 20, 1, 17, 106, 20, full)
    machine.step(Cycle(100, reset=1))
    machine.step(Cycle(101, command=Command.replace(0, 0, 21, (98, 13), (100, 13)),
                       config=Config(0, 1, 6, 8)))
    machine.step(Cycle(102, command=Command.replace(0, 1, 34, (96, 11), (101, 9)),
                       config=Config(1, 1, 8, -3)))
    machine.step(Cycle(103, command=Command.replace(0, 2, 53, (95, 6), (102, 6)),
                       config=Config(2, 1, 6, -4)))
    machine.step(Cycle(104, command=Command.replace(0, 0, 22, (98, 13), (110, 13)),
                       config=Config(invalid, 1, 1, 0), commit=1, query=buy))
    for tick in range(105, 110):
        machine.step(Cycle(tick, result_ready=0, config=Config(invalid, 1, 1, 0) if tick == 105 else None))

    responses = []
    for tick, request in (
        (110, replace(buy, tag=21, quantity=15)),
        (117, replace(buy, tag=22, quantity=15, all_or_none=1)),
        (124, replace(buy, tag=23, buy=0, quantity=9, limit=90, max_age=30)),
    ):
        first = machine.step(Cycle(tick, query=request))
        if first.result:
            responses.append(first.result)
        for wait in range(1, 7):
            sample = machine.step(Cycle(tick + wait))
            if sample.result:
                responses.append(sample.result)

    observed = [
        (report.status, report.epoch, report.quantity, report.value,
         [(child.venue, child.quantity) for child in report.children])
        for report in responses
    ]
    expected = [
        (SweepCode.FILLED, 0, 17, 1704, [(0, 13), (1, 4)]),
        (SweepCode.PARTIAL, 1, 14, 1372, [(1, 8), (2, 6)]),
        (SweepCode.AON_UNFILLED, 1, 0, 0, []),
        (SweepCode.FILLED, 1, 9, 891, [(1, 8), (2, 1)]),
    ]
    if observed != expected:
        raise AssertionError(f"demo benchmark mismatch {observed!r}")
    for tick, query in enumerate(
        [
            replace(buy, tag=24, venues=0),
            replace(buy, tag=25, instrument=machine.profile.instruments),
            replace(buy, tag=26, max_age=machine.profile.time_half),
            replace(buy, tag=27, quantity=0),
            replace(buy, tag=28, limit=0),
        ],
        start=140,
    ):
        machine.step(Cycle(tick, query=query))
    machine.step(Cycle(150))


def random_command(machine: SweepMachine, rng: random.Random, tag: int) -> Command:
    profile = machine.profile
    instrument = rng.randrange(profile.instruments)
    venue = rng.randrange(profile.venues)
    if rng.randrange(72) == 0:
        instrument = profile.instruments
    if rng.randrange(52) == 0:
        venue = profile.venues
    if rng.randrange(12) == 0:
        return Command.venue_enabled(instrument, venue, bool(rng.randrange(2)), tag=tag)

    last = machine.book.last_sequences.get((instrument, venue), 0)
    sequence = (last + rng.choice((1, 1, 1, 2, 0, profile.seq_half))) & mask(profile.seq_w)
    if rng.randrange(11) == 0:
        return Command.withdraw(instrument, venue, sequence, tag=tag)

    top = profile.price_max
    price = rng.choice((1, top, rng.randint(1, top), rng.randint(1, min(300, top))))
    best = (price, rng.randrange(1, profile.qty_max + 1))
    pair = rng.choice(((0, 0), (price, profile.qty_max),
                       (rng.randint(price, top), rng.randrange(1, profile.qty_max + 1))))
    bid, ask = (pair, best) if pair == (0, 0) and rng.randrange(2) else (best, pair)
    return Command.replace(instrument, venue, sequence, bid, ask, tag)


def random_config(machine: SweepMachine, rng: random.Random) -> Config:
    profile = machine.profile
    fee_max = 1 << (machine.fee_width - 1)
    fee = rng.choice((-fee_max, fee_max - 1, 0, rng.randrange(-fee_max, fee_max)))
    venue = rng.randrange(profile.venues + int(rng.randrange(9) == 0))
    cap = rng.choice((0, 1, profile.qty_max, rng.randint(0, profile.qty_max)))
    return Config(venue, int(rng.randrange(12) != 0), cap, fee)


def random_query(machine: SweepMachine, rng: random.Random, tag: int) -> SweepQuery:
    p = machine.profile
    source = rng.randrange(p.instruments + int(rng.randrange(12) == 0))
    buy = rng.randrange(2)
    quantity = rng.choice((1, p.qty_max, 0, p.qty_max * p.venues,
                           rng.randrange(1, p.qty_max * p.venues + 1)))
    limit = rng.choice((0, p.price_max if buy else 1, p.price_max if buy else 1,
                        rng.randint(1, p.price_max)))
    max_age = rng.choice((0, 12, p.time_half, p.time_half - 1))
    venues = p.venue_mask if rng.randrange(4) else rng.randrange(p.venue_mask + 1)
    return SweepQuery(source, tag, buy, quantity, limit, max_age, venues, rng.randrange(2))


def regress(machine: SweepMachine, seed: int, count: int) -> None:
    """Run held command, query and config sources across arbitration and resets."""
    rng = random.Random(seed)
    profile = machine.profile
    tick = mask(profile.time_w) - 6
    tag_max = mask(profile.tag_w)
    machine.step(Cycle(tick, reset=1))
    for i in range(265):
        machine.step(Cycle(tick, commit=1))

    query = command = config = None
    for i in range(count + profile.venues + 5):
        tick = (tick + rng.randrange(1, 5)) & mask(profile.time_w)
        produce = i < count
        reset = produce and i > 0 and i % 373 == 0
        if produce and not reset:
            if command is None and rng.randrange(4) != 0:
                command = random_command(machine, rng, i & tag_max)
            if query is None and rng.randrange(6) != 0:
                query = random_query(machine, rng, (i + 2) & tag_max)
            if config is None and rng.randrange(4) == 0:
                config = random_config(machine, rng)
        drive = Cycle(tick, int(reset), int(not produce or rng.randrange(5) != 0),
                      int(not produce or rng.randrange(3) != 0),
                      command, config, int(produce and rng.randrange(10) == 0), query)
        sample = machine.step(drive)
        if reset or sample.cmd_ready:
            command = None
        if reset or sample.cfg_ready:
            config = None
        if reset or sample.query_ready:
            query = None
    if command or config or query:
        raise AssertionError("source was never drained")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--fee-width", type=int, default=12)
    parser.add_argument("--cycles", type=int, default=2400)
    parser.add_argument("--seed", type=lambda text: int(text, 0), default=0xBB011)
    parser.add_argument("--waveform", type=Path)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    if args.demo and (args.profile != "default" or args.fee_width != 12):
        parser.error("the known-answer demo uses the default profile and 12-bit fees")
    if not 2 <= args.fee_width <= 28 or args.cycles < 0:
        parser.error("require 2..28 fee bits and a nonnegative cycle count")

    machine = SweepMachine(PROFILES[args.profile], args.fee_width)
    if args.profile == "default" and args.fee_width == 12:
        demo(machine)
    if not args.demo:
        regress(machine, args.seed, args.cycles)
        required = {"data_race", "policy_race", "result_stall", "source_stall", "ack_stall",
                    "config_error", "reset_pending", *(f"status_{code.name}" for code in SweepCode)}
        missed = sorted(name for name in required if machine.checked[name] == 0)
        if args.cycles >= 2400 and missed:
            raise AssertionError(f"missing coverage {missed!r}")
    args.output.mkdir(exist_ok=True, parents=True)
    drive_path = args.output / "drive.txt"
    trace_path = args.output / "trace.txt"
    machine.write_drive(drive_path, trace_path)
    simulation(args.vvp, args.image, drive_path, trace_path, args.waveform)
    machine.compare(trace_path)
    print(f"PASS {args.profile} sweep: {len(machine.samples)} edges, "
          f"{machine.checked['query']} queries, {machine.checked['command']} commands, "
          f"{machine.checked['commit']} policy commits")
    print(" " + " ".join(f"{key}={value}" for key, value in sorted(machine.checked.items())))


if __name__ == "__main__":
    main()
