"""Check the wrapper under sink order changes, replacement, and reset fencing."""

import argparse
import json
import random
import subprocess
from collections import Counter
from itertools import zip_longest
from pathlib import Path

from limiter_gate_model import GateDrive, GateModel
from limiter_protocol import Command, PROFILES, Status, read_rows, write_rows
from run_limiter_matrix import command_words


class GateSchedule:
    """Build stable dual-sink traffic and remember exactly which cases occurred."""

    def __init__(self):
        """Use the default wire geometry and separate sink counters."""
        self.geometry = PROFILES["default"]
        self.model = GateModel(self.geometry)
        self.drives = []
        self.expect = []
        self.covered = Counter()
        self.tag = 0

    def edge(self, event):
        """Record expected transfers and qualifying stalls on a single edge."""
        old_audit = self.model.audit
        old_output = self.model.output
        pins = self.model.step(event)
        self.drives.append(event.words())
        self.expect.append(pins.words())
        if event.reset:
            self.covered["reset"] += 1
            if old_audit:
                self.covered["reset_discarded_audit"] += 1
            if old_output:
                self.covered["reset_discarded_data"] += 1
            return False
        audit_fire = pins.audit is not None and bool(event.audit_ready)
        output_fire = pins.output is not None and bool(event.output_ready)
        accepted = bool(event.valid and pins.input_ready)
        if accepted:
            self.covered["accepted"] += 1
        elif event.valid:
            self.covered["held_request"] += 1
        if audit_fire:
            self.covered["audit"] += 1
            self.covered[Status(pins.audit[3]).name] += 1
            if pins.output and not output_fire:
                self.covered["audit_before_data"] += 1
        elif pins.audit:
            self.covered["held_audit"] += 1
        if output_fire:
            self.covered["forwarded"] += 1
            if pins.audit and not audit_fire:
                self.covered["data_before_audit"] += 1
        elif pins.output:
            self.covered["held_data"] += 1
        if audit_fire and output_fire:
            self.covered["same_edge_sinks"] += 1
        if accepted and (audit_fire or output_fire):
            self.covered["in_place_replacement"] += 1
        return accepted

    def reset(self):
        """Fence both outputs and erase the configured policies."""
        self.edge(GateDrive(reset=1, audit_ready=1, output_ready=1))

    def idle(self, a_ready=1, d_ready=1):
        """Advance without an input, allowing either consumer to transfer."""
        self.edge(GateDrive(audit_ready=a_ready, output_ready=d_ready))

    def next_tag(self):
        """Issue a unique correlation tag modulo the configured wire width."""
        tag = self.tag
        self.tag = (self.tag + 1) & self.geometry.tag_max
        return tag

    def drive(self, command, data, pattern=()):
        """Offer the same source values across every independently stalled edge."""
        sequence = (*pattern, (1, 1))
        for a_ready, d_ready in sequence:
            drive = GateDrive(0, 1, a_ready, d_ready, data, command)
            if self.edge(drive):
                return
        raise AssertionError("command could not enter an open gate")

    def offer(self, key, tick, cost, data, pattern=()):
        """Build one tagged data offer for the split audit/data paths."""
        command = Command.offer(key, self.geometry.wrapped(tick), cost, self.next_tag())
        self.drive(command, data, pattern)

    def config(self, key, tick, cap, rate, pattern=()):
        """Configure one key without allocating a downstream data transfer."""
        command = Command.configure(key, self.geometry.wrapped(tick), cap, rate, self.next_tag())
        self.drive(command, 0, pattern)

    def inspect(self, key, tick, pattern=()):
        """Query a key and route the resulting snapshot through audit only."""
        command = Command.inspect(key, self.geometry.wrapped(tick), self.next_tag())
        self.drive(command, 0, pattern)


def gate_cases(seed, operations):
    """Hit every sink ordering first, then mix unrelated keyed traffic."""
    rng = random.Random(seed)
    plan = GateSchedule()
    plan.reset()
    plan.config(0, 0, 8, 8)
    plan.offer(0, 0, 4, 0xA5000001)
    plan.idle(a_ready=1, d_ready=0)
    plan.offer(0, 0, 2, 0xA5000002, pattern=((1, 0), (0, 0), (0, 1)))
    plan.idle(a_ready=0, d_ready=1)
    plan.offer(0, 0, 4, 0xA5000003, pattern=((0, 1), (0, 0), (1, 0)))
    plan.idle(a_ready=0, d_ready=0)
    plan.inspect(0, 0, pattern=((0, 1), (1, 0)))
    plan.offer(0, 1, 4, 0xA5000004)
    plan.idle(a_ready=1, d_ready=0)
    plan.reset()
    plan.offer(0, 0, 1, 0xA5000005)
    plan.config(1, 2, 6, 6)
    plan.offer(1, 2, 1, 0xA5000006)
    plan.idle(a_ready=0, d_ready=1)
    plan.reset()

    ticks = [0] * plan.geometry.keys
    for key in range(plan.geometry.keys):
        plan.config(key, 0, 16, 1 + key)
    for number in range(operations):
        key = rng.randrange(plan.geometry.keys)
        ticks[key] += rng.randrange(3)
        pattern = tuple((rng.randrange(2), rng.randrange(2)) for _ in range(rng.randrange(5)))
        if number and number % 137 == 0:
            plan.reset()
        if number % 13 == 0:
            plan.config(key, ticks[key], 16, 1 + key, pattern)
        elif number % 19 == 0:
            plan.inspect(key, ticks[key], pattern)
        else:
            plan.offer(key, ticks[key], 1 + rng.randrange(24), rng.getrandbits(32), pattern)
        if number % 7 == 0:
            plan.idle(rng.randrange(2), rng.randrange(2))
    plan.idle()
    plan.idle()
    return plan


def compare_capture(plan, actual_file):
    """Require equality of every non-don't-care pin and the expected trace length."""
    sentinel = object()
    for cycle, (wanted, actual) in enumerate(
        zip_longest(plan.expect, read_rows(actual_file, 15), fillvalue=sentinel)
    ):
        if wanted == sentinel or actual == sentinel:
            raise AssertionError(f"gate trace length mismatch at cycle {cycle}")
        if wanted != actual:
            different = [
                f"pin[{index}] expected {expected:#x} got {observed:#x}"
                for index, (expected, observed) in enumerate(zip(wanted, actual))
                if expected != observed
            ]
            raise AssertionError(f"gate cycle {cycle}: {', '.join(different)}")


def run(args):
    """Generate a fixed seed trace and run the independently compared RTL bench."""
    args.output.mkdir(parents=True, exist_ok=True)
    plan = gate_cases(args.seed, args.operations)
    drivers = args.output / "inputs.hex"
    actual_file = args.output / "capture.hex"
    binary = args.output / "gate.vvp"
    write_rows(drivers, plan.drives)
    write_rows(args.output / "expected.hex", plan.expect)
    compile_words = command_words(args.iverilog) + [
        "-g2012", "-Wall", "-s", "tb_message_rate_gate",
        "-o", str(binary), "-f", "rtl.f", "tb_message_rate_gate.sv",
    ]
    command = subprocess.run(compile_words, text=True, capture_output=True)
    (args.output / "elaboration.log").write_text(command.stdout + command.stderr, encoding="utf-8")
    if command.returncode:
        raise RuntimeError(f"gate compilation failed. See {args.output / 'elaboration.log'}")
    arguments = command_words(args.vvp) + [
        str(binary), f"+source={drivers}", f"+sink={actual_file}",
    ]
    if args.wave is not None:
        arguments.append(f"+wave={args.wave}")
    subprocess.run(arguments, check=True)
    compare_capture(plan, actual_file)
    must_cover = {
        "audit_before_data", "data_before_audit", "same_edge_sinks", "in_place_replacement",
        "held_request", "held_audit", "held_data", "reset_discarded_audit",
        "reset_discarded_data", "OK", "PASS", "LIMITED", "DISABLED",
    }
    if must_cover - plan.covered.keys():
        raise AssertionError(f"missing gate coverage: {sorted(must_cover - plan.covered.keys())}")
    (args.output / "summary.json").write_text(
        json.dumps(dict(plan.covered), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("checked gate transfers", dict(plan.covered))


def main():
    """Choose compile tools, output folder, and seed for the wrapper matrix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--seed", type=int, default=315)
    parser.add_argument("--operations", type=int, default=400)
    parser.add_argument("--output", type=Path, default=Path("build/gate"))
    parser.add_argument("--wave", type=Path, help="capture the same checked run as VCD")
    args = parser.parse_args()
    if args.seed < 0 or args.operations < 0:
        parser.error("seed and operation count must be nonnegative")
    run(args)


if __name__ == "__main__":
    main()
