"""Run reproducible directed and randomized RTL comparisons across geometries."""

import argparse
from collections import Counter
import random
from pathlib import Path
import subprocess
from typing import Dict, Iterable, List, Optional, Tuple

from scheduler_model import Control, Drive, Geometry, Op, Order, Pins, PROFILES, Scheduler, Status


PROJECT = Path(__file__).resolve().parent
TOP = "tb_credit_order_scheduler"
Row = Tuple[Drive, Pins]


class Campaign:
    """Record drive/expected rows, trace outcomes, and check boundary reachability."""

    def __init__(self, geometry: Geometry) -> None:
        """Create separate reference state and coverage accounting for this run."""
        self.model = Scheduler(geometry)
        self.rows: List[Row] = []
        self.coverage: Counter = Counter()
        self.generated_orders = 0

    def order(self, key: int, cost: int) -> Order:
        """Allocate a distinct test tag and deterministically varied payload."""
        self.generated_orders += 1
        tag = self.generated_orders & 0xFFFF
        return Order(key, cost, tag, (self.generated_orders * 0x9E3779B9) & 0xFFFFFFFF)

    def step(self, drive: Drive = Drive()) -> Pins:
        """Collect coverage from the agreed handshakes, then save one row."""
        model = self.model
        before_held = model.held
        occupied = [len(lane.queue) for lane in model.lanes]
        debt = [lane.debt for lane in model.lanes]
        pins = model.advance(drive)
        self.rows.append((drive, pins))
        if drive.reset:
            self.coverage["reset"] += 1
            if before_held is not None:
                self.coverage["reset_offer"] += 1
            return pins
        offer = pins.offer
        if offer is not None:
            if drive.sink_ready:
                self.coverage["sent"] += 1
                self.coverage[f"sent_key_{offer.key}"] += 1
            else:
                self.coverage["offer_stall"] += 1
        if pins.report is not None:
            self.coverage["report_taken" if drive.report_ready else "report_stall"] += 1
        if drive.order is not None:
            if not pins.order_ready:
                self.coverage["input_stall"] += 1
            elif pins.order_rejected:
                self.coverage["rejected"] += 1
            else:
                key = drive.order.key
                self.coverage["enqueued"] += 1
                if occupied[key] == model.geometry.depth:
                    self.coverage["full_replace"] += 1
        if drive.control is not None:
            if not pins.control_ready:
                self.coverage["control_stall"] += 1
            else:
                report = model.report
                self.coverage[report.status.name] += 1
                if report.status == Status.OK:
                    self.coverage[report.op.name] += 1
                    if report.op == Op.REFUND:
                        if offer is not None and drive.sink_ready and offer.key == report.key:
                            self.coverage["refund_and_send"] += 1
                        if debt[report.key] == report.capacity:
                            self.coverage["refund_exhausted_lane"] += 1
        return pins

    def startup(self) -> None:
        """Exercise a known credit lifecycle with explicit edge assertions."""
        model = self.model
        has_other = model.geometry.classes > 1
        has_space = model.geometry.depth > 1
        first, later = self.order(0, 3), self.order(0, 1)
        other = self.order(1, 2) if has_other else None
        inspect = Control(Op.INSPECT, 0)
        self.step(Drive(reset=True))
        self.step(Drive(control=Control(Op.CONFIGURE, 0, 3, 2)))
        self.step(Drive(control=Control(Op.CONFIGURE, 1, 2, 1) if has_other else None))
        self.step(Drive(order=first, control=inspect))
        assert model.report is not None and model.report.queued == 1
        pins = self.step(Drive(control=Control(Op.REFUND, 0, 1)))
        assert pins.offer == first and model.balances()[0] == (3, 3, 0)
        assert model.report.status == Status.OVER_REFUND
        waiting = Control(Op.CONFIGURE, 0, 2, 1)
        pins = self.step(Drive(order=other, control=waiting, report_ready=False))
        assert pins.offer == first and not pins.control_ready
        pins = self.step(Drive(order=later if has_space else None, control=waiting))
        assert pins.offer == first and model.report.status == Status.BUSY
        pins = self.step(Drive(order=None if has_space else later, control=inspect, sink_ready=True))
        assert pins.offer == first and model.balances()[0] == (3, 0, 3)
        assert model.report.queued == 1 and model.report.inflight == 3
        pins = self.step(Drive(control=Control(Op.REFUND, 0, 3), sink_ready=True))
        assert pins.offer == other and model.balances()[0] == (3, 3, 0)
        pins = self.step(Drive(control=inspect, sink_ready=True))
        assert pins.offer == later and model.report.inflight == 1
        self.step(Drive(reset=True))

    def boundaries(self) -> None:
        """Force queue wrap, maximum credits, and concurrent debit/refund."""
        shape = self.model.geometry
        self.step(Drive(control=Control(Op.CONFIGURE, 0, shape.max_credit, 15)))
        for _ in range(shape.depth):
            self.step(Drive(order=self.order(0, 1)))
        replacement = self.order(0, 1)
        stalled = self.step(Drive(order=replacement))
        assert not stalled.order_ready
        accepted = self.step(Drive(order=replacement, sink_ready=True))
        assert accepted.order_ready and accepted.offer is not None
        remaining = shape.depth
        while remaining:
            pins = self.step(Drive(control=Control(Op.REFUND, 0, 1), sink_ready=True))
            assert pins.offer is not None
            assert self.model.balances()[0] == (shape.max_credit, shape.max_credit - 1, 1)
            remaining -= 1
        self.step(Drive(order=self.order(0, 1)))
        self.step(Drive(reset=True))
        # Reset an actually held offer, not only a populated queue.
        self.step(Drive(control=Control(Op.CONFIGURE, 0, 1, 1)))
        self.step(Drive(order=self.order(0, 1)))
        self.step()
        self.step(Drive(reset=True))
        bad_key = shape.max_class_code
        assert bad_key >= shape.classes
        self.step(Drive(order=self.order(bad_key, 1), control=Control(Op.INSPECT, bad_key)))
        self.step(Drive(order=self.order(0, 0), control=Control(Op.RESERVED, 0)))
        self.step(Drive(control=Control(Op.CONFIGURE, 0, shape.max_credit, 1)))

    def weighted_cadence(self) -> None:
        """Saturate all queues and assert repeating normalized service order."""
        count, depth = self.model.geometry.classes, self.model.geometry.depth
        self.step(Drive(reset=True))
        weights = [1 + key % 3 if depth >= 3 else 1 for key in range(count)]
        pattern = [key for key, quota in enumerate(weights) for _ in range(quota)]
        for key, quota in enumerate(weights):
            self.step(Drive(control=Control(Op.CONFIGURE, key, self.model.geometry.max_credit, quota)))
        for key in range(count):
            for _ in range(depth):
                self.step(Drive(order=self.order(key, 1)))
        observed = []
        repetitions = min(4, self.model.geometry.max_credit // max(weights))
        for scheduled_key in pattern * repetitions:
            refill = self.order(scheduled_key, 1)
            pins = self.step(Drive(order=refill, sink_ready=True))
            assert pins.offer is not None and pins.offer.key == scheduled_key
            observed.append(pins.offer.key)
        assert observed == pattern * repetitions
        self.step(Drive(reset=True))

    def oversized_head(self) -> None:
        """Verify a blocked lane and restore it through an accepted capacity change."""
        shape = self.model.geometry
        self.step(Drive(reset=True))
        self.step(Drive(control=Control(Op.CONFIGURE, 0, 2, 1)))
        if shape.classes > 1:
            self.step(Drive(control=Control(Op.CONFIGURE, 1, 2, 1)))
        blocked = self.order(0, 3)
        self.step(Drive(order=blocked))
        younger = self.order(0, 1) if shape.depth > 1 else None
        if younger is not None:
            self.step(Drive(order=younger))
        other = self.order(1, 1) if shape.classes > 1 else None
        pins = self.step(Drive(order=other, sink_ready=True))
        assert pins.offer is None
        pins = self.step(Drive(sink_ready=True))
        assert pins.offer == other
        pins = self.step(Drive(control=Control(Op.CONFIGURE, 0, 4, 1), sink_ready=True))
        assert pins.offer is None
        assert self.model.report.status == Status.OK
        assert self.step(Drive(sink_ready=True)).offer == blocked
        assert self.step(Drive(sink_ready=True)).offer == younger

    def sustained_returns(self) -> None:
        """Maintain one accepted handoff every cycle with an adapter refund."""
        self.step(Drive(reset=True))
        self.step(Drive(control=Control(Op.CONFIGURE, 0, 4, 1)))
        outstanding = self.order(0, 1)
        self.step(Drive(order=outstanding))
        for beat in range(128):
            replacement = self.order(0, 1)
            completed = Control(Op.REFUND, 0, 1) if beat else None
            pins = self.step(Drive(order=replacement, control=completed, sink_ready=True))
            assert pins.offer == outstanding and pins.order_ready
            assert self.model.balances()[0] == (4, 3, 1)
            outstanding = replacement
        self.step(Drive(reset=True))

    def coverage_requirements(self) -> None:
        """Fail if a required transition went untested, even after a clean match."""
        mandatory = [
            "reset_offer", "sent", "offer_stall", "report_stall", "control_stall", "input_stall",
            "rejected", "full_replace", "BAD_CLASS", "BAD_FIELDS", "BUSY", "OVER_REFUND",
            "CONFIGURE", "REFUND", "INSPECT", "refund_and_send", "refund_exhausted_lane",
        ]
        mandatory += [f"sent_key_{key}" for key in range(self.model.geometry.classes)]
        absent = [name for name in mandatory if self.coverage[name] == 0]
        if absent:
            raise AssertionError(f"coverage missed: {absent}")


def random_control(rng: random.Random, campaign: Campaign) -> Control:
    """Bias commands toward useful refunds but inject every malformed operation."""
    geometry = campaign.model.geometry
    key = rng.randrange(geometry.classes)
    lane = campaign.model.lanes[key]
    weighted_choice = rng.randrange(10)
    if weighted_choice < 4 and lane.debt:
        amount = lane.debt if rng.randrange(2) else rng.randint(1, lane.debt)
        return Control(Op.REFUND, key, amount)
    if weighted_choice < 6:
        weight = rng.randint(1, 15)
        amount = rng.choice([0, 1, 2, geometry.max_credit, rng.randint(1, geometry.max_credit)])
        return Control(Op.CONFIGURE, key, amount, weight)
    if weighted_choice < 8:
        return Control(Op.INSPECT, key)
    return Control(rng.choice(list(Op)), rng.randrange(geometry.max_class_code + 1),
                   rng.randint(0, geometry.max_credit), rng.randrange(16))


def random_order(rng: random.Random, campaign: Campaign) -> Order:
    """Mix valid traffic, zero-cost inputs and payloads that need more credit."""
    geometry = campaign.model.geometry
    key = rng.randrange(geometry.classes)
    if rng.randrange(20) == 0:
        return campaign.order(rng.randrange(geometry.max_class_code + 1), rng.randrange(2))
    lane = campaign.model.lanes[key]
    cost = rng.choice([1, 1, 2, max(1, lane.capacity),
                       rng.randint(1, min(8, geometry.max_credit))])
    return campaign.order(key, cost)


def add_random(campaign: Campaign, seed: int, cycles: int) -> None:
    """Drive both producers independently and keep valid requests stable on stalls."""
    rng = random.Random(seed)
    model = campaign.model
    offered_order: Optional[Order] = None
    offered_control: Optional[Control] = None
    for cycle in range(cycles):
        if cycle % 211 == 0:
            campaign.step(Drive(reset=True))
            offered_order = None
            offered_control = None
            for key in range(model.geometry.classes):
                campaign.step(Drive(control=Control(
                    Op.CONFIGURE, key, rng.randint(1, model.geometry.max_credit), rng.randint(1, 4))))
        if offered_order is None and rng.randrange(8) < 7:
            offered_order = random_order(rng, campaign)
        if offered_control is None and rng.randrange(8) < 5:
            offered_control = random_control(rng, campaign)
        drive = Drive(offered_order, offered_control, rng.randrange(4) < 3, rng.randrange(3) < 2)
        pins = campaign.step(drive)
        if pins.order_ready:
            offered_order = None
        if pins.control_ready:
            offered_control = None
    campaign.step(Drive(reset=True))
    campaign.step(Drive(sink_ready=True))


def vectors(output: Path, rows: Iterable[Row]) -> None:
    """Write paired hexadecimal input and expected-pin files for the bench."""
    output.mkdir(parents=True, exist_ok=True)
    with (output / "drive.hex").open("w") as drive_file:
        with (output / "expected.hex").open("w") as golden_file:
            for drive, expected in rows:
                drive_file.write(" ".join(format(value, "x") for value in drive.fields()) + "\n")
                golden_file.write(" ".join(format(value, "x") for value in expected.fields()) + "\n")


def compare_rtl(campaign: Campaign, output: Path, tools: Dict[str, str],
                wave: Optional[Path]) -> None:
    """Compile the actual RTL for this profile and run the pin-exact comparison."""
    shape = campaign.model.geometry
    command = [tools["iverilog"], "-g2012", "-Wall", "-s", TOP]
    for parameter, value in (("CLASSES", shape.classes), ("DEPTH", shape.depth),
                             ("CREDIT_W", shape.credit_bits)):
        command.extend(["-P", f"{TOP}.{parameter}={value}"])
    executable = output / "test.vvp"
    command.extend(["-o", str(executable), "-f", "rtl.f", f"{TOP}.sv"])
    subprocess.run(command, cwd=PROJECT, check=True, timeout=60)
    run = [tools["vvp"], str(executable), f"+vectors={output}"]
    if wave is not None:
        wave.parent.mkdir(parents=True, exist_ok=True)
        run.append(f"+wave={wave}")
    subprocess.run(run, cwd=PROJECT, check=True, timeout=60)


def main() -> None:
    """Run the checked geometry matrix with optional waveform for one profile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--build", type=Path, default=Path("build"))
    parser.add_argument("--profile", choices=sorted(PROFILES), action="append")
    parser.add_argument("--cycles", type=int, default=3500)
    parser.add_argument("--seed", type=lambda text: int(text, 0), default=0x161CEE)
    parser.add_argument("--wave", type=Path)
    args = parser.parse_args()
    profiles = args.profile or list(PROFILES)
    if args.wave is not None and len(profiles) != 1:
        parser.error("--wave requires exactly one --profile")
    if args.cycles < 0:
        parser.error("--cycles must be nonnegative")
    for index, profile in enumerate(profiles):
        campaign = Campaign(PROFILES[profile])
        campaign.startup()
        campaign.boundaries()
        campaign.weighted_cadence()
        campaign.oversized_head()
        campaign.sustained_returns()
        add_random(campaign, args.seed + index, args.cycles)
        campaign.coverage_requirements()
        output = (args.build / profile).resolve()
        vectors(output, campaign.rows)
        print(f"Checking {profile} {PROFILES[profile]}: {len(campaign.rows)} cycles", flush=True)
        compare_rtl(campaign, output, {"iverilog": args.iverilog, "vvp": args.vvp},
                    args.wave.resolve() if args.wave is not None else None)
        print(f"{profile} coverage {dict(sorted(campaign.coverage.items()))}", flush=True)


if __name__ == "__main__":
    main()
