"""Drive deterministic dual-lane simulations and verify every sampled pin."""

from __future__ import annotations

import argparse
import csv
import json
import random
import shlex
import subprocess
from collections import deque
from itertools import zip_longest
from pathlib import Path

from notional_model import Kind, Request
from notional_pair_model import (
    DRIVE_FIELDS, PIN_FIELDS, Drive, PairModel, PairTraceError, PinAudit, Pins,
    describe_phases, predict_saturated,
)
from notional_workload import cases


ROOT = Path(__file__).resolve().parent
TOP = "tb_notional_pair_cycle"
PROFILES = (0, 8, 31)
TRANSACTION_FIELDS = (
    "ordinal", "lane", "tag", "status", "admitted", "available", "visible", "delivered",
    "service", "order_wait", "downstream_wait",
)


def tool_words(raw):
    """Split optional wrappers and paths while keeping tool arguments literal."""
    terms = shlex.split(raw)
    if not terms:
        raise ValueError("tool override may not be blank")
    return terms


def offsets(row):
    """Yield numeric words for a compact, ASCII-only simulator format."""
    return " ".join(format(value, "x") for value in row)


def hex_rows(path, columns):
    """Read fixed-size records with useful diagnostics for malformed captures."""
    with Path(path).open(encoding="ascii") as source:
        for number, line in enumerate(source, 1):
            fields = line.split()
            if len(fields) != columns:
                raise PairTraceError(f"{path}:{number}: expected {columns} columns")
            words = []
            for field in fields:
                if any(character not in "0123456789abcdefABCDEF" for character in field):
                    raise PairTraceError(f"{path}:{number}: invalid hexadecimal word {field}")
                words.append(int(field, 16))
            yield tuple(words)


def save_hex(path, records):
    """Write exactly one normalized signal bundle per line."""
    with Path(path).open("w", encoding="ascii", newline="\n") as target:
        for record in records:
            target.write(offsets(record))
            target.write("\n")


def clean_request(tag, fraction_bits, *, fee=500, quantity=11, sell=False):
    """Create a valid price just above one whole unit at every tested width."""
    return Request(tag, (1 << fraction_bits) + 1, quantity, fee, 2, sell)


def malformed_request(tag, case):
    """Select one of the four validation shortcuts with a unique wire cause."""
    if case == 0:
        return Request(tag, 1, 0)
    if case == 1:
        return Request(tag, 0, 7)
    if case == 2:
        return Request(tag, 1, 7, 10_001)
    if case == 3:
        return Request(tag, 1, 7, 0, 3)
    raise ValueError("malformed scenario must be in 0..3")


class Campaign:
    """Build a legal source stream around an independent cycle model."""

    def __init__(self, fraction_bits, maximum_cycles=60_000):
        """Retain a replayable input tape and its possible visible outputs."""
        self.reference = PairModel(fraction_bits)
        self.maximum_cycles = maximum_cycles
        self.inputs = []
        self.expected = []
        self.label = ""
        self.markers = {}

    @property
    def fraction_bits(self):
        """Expose the exact fixed-point profile used for data generation."""
        return self.reference.fraction_bits

    @property
    def index(self):
        """Return the next pre-edge cycle number in the tape."""
        return len(self.inputs)

    def mark(self, name):
        """Retain named boundaries so a reader can find a waveform scenario."""
        if name in self.markers:
            raise ValueError(f"duplicate campaign marker {name}")
        self.markers[name] = self.index
        self.label = name

    def tick(self, drive):
        """Advance one edge and preserve both the drive and expected pins."""
        if self.index >= self.maximum_cycles:
            raise RuntimeError(f"deadline exceeded during {self.label}")
        observation = self.reference.advance(drive)
        self.inputs.append(drive)
        self.expected.append(observation.pins)
        return observation

    def idle(self, count=1, *, ready=True):
        """Advance the calculators while the source does not offer work."""
        if count < 0:
            raise ValueError("idle interval must be nonnegative")
        for _ in range(count):
            self.tick(Drive(ready=ready))

    def reset(self, count=1, *, request=None, ready=True):
        """Reset on the selected edges, ignoring a concurrently driven request."""
        if count < 1:
            raise ValueError("reset interval must contain an edge")
        for _ in range(count):
            self.tick(Drive(reset=True, request=request, ready=ready))

    def issue(self, request, sink=None):
        """Hold valid and every request bit until it is accepted."""
        while True:
            ready = sink(self.index) if sink is not None else True
            event = self.tick(Drive(request=request, ready=ready))
            if event.admitted:
                return event.admitted

    def wait_visible(self):
        """Wait for an output with the sink held low and return its first cycle."""
        while True:
            idle = Drive(ready=False)
            snapshot = self.reference.pins(idle)
            if snapshot.output_valid:
                return self.index
            if snapshot.outstanding == 0:
                raise RuntimeError("no request is pending")
            self.tick(idle)

    def drain(self):
        """Run with no upstream offers until both lanes retire."""
        while self.reference.pending:
            self.tick(Drive())
        self.idle(2)

    def write(self, build):
        """Export inputs, oracle pins, and named boundary markers to one folder."""
        build = Path(build)
        build.mkdir(parents=True, exist_ok=True)
        save_hex(build / "drive.hex", (row.wire() for row in self.inputs))
        save_hex(build / "oracle.hex", (row.wire() for row in self.expected))
        (build / "markers.json").write_text(
            json.dumps(self.markers, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


def directed(plan):
    """Prove expected ordering under compute, full-queue, and reset conditions."""
    frac = plan.fraction_bits
    plan.mark("warm_up")
    plan.reset(2)
    plan.idle(2)
    first = clean_request(1, frac)
    plan.issue(first)
    plan.drain()

    plan.mark("slow_head_fast_tail")
    plan.issue(clean_request(20, frac, fee=351))
    plan.issue(malformed_request(20, 1))
    plan.wait_visible()
    plan.idle(5, ready=False)
    plan.idle()
    plan.drain()

    plan.mark("full_queue_bubble")
    plan.issue(malformed_request(22, 2), lambda _: False)
    plan.issue(malformed_request(22, 3), lambda _: False)
    plan.issue(clean_request(23, frac))
    plan.drain()

    plan.mark("one_in_one_out")
    plan.issue(malformed_request(24, 0))
    plan.issue(clean_request(25, frac, sell=True))
    plan.drain()

    plan.mark("reset_busy_and_ready")
    plan.issue(clean_request(13, frac))
    plan.issue(malformed_request(14, 3))
    plan.idle(3, ready=False)
    plan.reset(request=clean_request(15, frac))
    plan.idle(3)
    plan.issue(clean_request(13, frac, fee=0, quantity=16))
    plan.wait_visible()
    plan.reset(ready=True)
    plan.issue(malformed_request(16, 0))
    plan.drain()

    plan.mark("sustained_pair")
    for index in range(16):
        plan.issue(clean_request(index % 3, frac, fee=7 + index, quantity=1 + index))
    plan.drain()


def randomized(plan, seed, operations):
    """Mix realistic costs and malformed traffic under seeded sink pauses."""
    rng = random.Random(seed ^ (plan.fraction_bits << 9))
    plan.mark("mixed_seed")
    requests = cases(plan.fraction_bits, seed, operations)
    for index, request in enumerate(requests):
        if index % 31 == 11:
            plan.idle(1 + rng.randrange(4), ready=False)
            plan.reset(request=request, ready=bool(rng.randrange(2)))
        if index % 17 == 0:
            plan.idle(rng.randrange(5))
        pause = rng.randrange(4)
        polarity = rng.randrange(11)

        def readiness(cycle):
            """Hold the sink periodically so each source can reach a new slot."""
            return (cycle + polarity) % 11 >= pause

        plan.issue(request, readiness)
    plan.drain()


def make_campaign(frac, seed, operations):
    """Create both directed timing sections and a repeatable mixed workload."""
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be nonnegative")
    if type(operations) is not int or operations < 0:
        raise ValueError("random operation count must be nonnegative")
    ceiling = (operations + 200) * 150
    campaign = Campaign(frac, maximum_cycles=ceiling)
    directed(campaign)
    randomized(campaign, seed, operations)
    return campaign


def verify_pair_capture(frac, drive_path, pins_path):
    """Read raw hardware observations alongside the original drive tape."""
    audit = PinAudit(frac)
    drives = hex_rows(drive_path, len(DRIVE_FIELDS))
    samples = hex_rows(pins_path, len(PIN_FIELDS))
    recent = deque(maxlen=3)
    sentinel = object()
    for index, (drive, pin) in enumerate(zip_longest(drives, samples, fillvalue=sentinel)):
        if drive is sentinel:
            raise PairTraceError(f"cycle {index}: additional capture row with no drive")
        if pin is sentinel:
            raise PairTraceError(f"cycle {index}: missing capture for a driven edge")
        try:
            command = Drive.read(drive)
            received = Pins.read(pin)
            audit.check(command, received)
        except ValueError as exc:
            history = "; ".join(recent)
            raise PairTraceError(f"{exc} (edge {index}, recent {history or 'none'})") from exc
        recent.append(f"{index}:{offsets(drive)}")
    if not audit.stats.cycles:
        raise PairTraceError("capture is empty")
    return audit.finish(), audit.transfers, audit.discards


def require_campaign(stats, transfers):
    """Require both lane and delay classes and the intended edge cases."""
    categories = {
        "full": stats.full_cycles,
        "malformed": stats.service[1],
        "calculated": stats.service[85],
        "reset cancelled": stats.discarded,
        "upstream held": stats.request_stalls,
        "downstream held": stats.response_stalls,
        "head unfinished": stats.compute_order_stalls,
        "head stalled": stats.downstream_order_stalls,
        "simultaneous": stats.simultaneous,
        "lane 0": stats.lanes[0],
        "lane 1": stats.lanes[1],
    }
    empty = [name for name, value in categories.items() if value == 0]
    if empty:
        raise PairTraceError(f"pair campaign missed {', '.join(empty)}")
    if not any(event.order_wait and event.service == 1 for event in transfers):
        raise PairTraceError("no malformed shortcut reached a blocked tail position")
    if Kind.BAD_QUANTITY.name not in stats.status:
        raise PairTraceError("missing zero-quantity shortcut")


def check_sustained(markers, transfers):
    """Use ordering constraints to predict the warm, always-ready pair run."""
    first = markers["sustained_pair"]
    end = markers["mixed_seed"]
    observed = [item for item in transfers if first <= item.job.admitted < end]
    if len(observed) != 16:
        raise PairTraceError("sustained phase expected 16 delivered clean jobs")
    expected = predict_saturated([85] * len(observed))
    if [item.job.admitted - first for item in observed] != [item.issue for item in expected]:
        raise PairTraceError("sustained admissions disagree with independent schedule bounds")
    if [item.delivered - first for item in observed] != [item.delivery for item in expected]:
        raise PairTraceError("sustained completions disagree with independent schedule bounds")
    return {
        "service_cycles": 85,
        "warm_issue_period": expected[2].issue - expected[0].issue,
        "requests_per_period": 2,
        "transfer_edges": [item.delivery for item in expected],
    }


def write_transfers(path, transfers):
    """Serialize completed arithmetic and delay measurements in transfer order."""
    with Path(path).open("w", encoding="ascii", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=TRANSACTION_FIELDS, lineterminator="\n")
        writer.writeheader()
        for record in transfers:
            writer.writerow(record.record())


def compile_profile(iverilog, fraction_bits, folder):
    """Compile the standalone pair driver for one selected hardware width."""
    binary = folder / "pair.vvp"
    arguments = tool_words(iverilog) + [
        "-g2012", "-Wall", "-I.", "-s", TOP, f"-P{TOP}.PRICE_FRAC={fraction_bits}",
        "-o", str(binary), "-f", "rtl.f", f"{TOP}.sv",
    ]
    result = subprocess.run(arguments, cwd=ROOT, text=True, capture_output=True)
    log = folder / "elaboration.log"
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"pair elaboration failed. See {log}")
    return binary


def simulate(vvp, binary, folder):
    """Run Icarus with the captured schedule and save its logs and pin rows."""
    arguments = tool_words(vvp) + [
        str(binary), f"+drive={folder / 'drive.hex'}", f"+capture={folder / 'capture.hex'}",
    ]
    result = subprocess.run(arguments, cwd=ROOT, text=True, capture_output=True)
    log = folder / "simulation.log"
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"pair simulation failed. See {log}")


def load_markers(folder):
    """Read verified phase metadata saved with the original RTL schedule."""
    path = folder / "markers.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PairTraceError(f"{path}: invalid phase metadata") from exc
    if not isinstance(result, dict) or "sustained_pair" not in result or "mixed_seed" not in result:
        raise PairTraceError(f"{path}: missing campaign phase markers")
    return result


def shipment(transfers, phases, *, phase=None, tag=None, status=None):
    """Select report rows only after the entire capture has passed verification."""
    first = 0
    final = None
    if phase is not None:
        windows = [row for row in phases if row["name"] == phase]
        if not windows:
            raise ValueError(f"unknown phase {phase!r}")
        first, final = windows[0]["start"], windows[0]["stop"]
    if tag is not None and (type(tag) is not int or not 0 <= tag < 65536):
        raise ValueError("tag filter must fit sixteen bits")
    return [
        item for item in transfers
        if item.job.admitted >= first
        and (final is None or item.job.admitted < final)
        and (tag is None or item.job.request.tag == tag)
        and (status is None or item.job.reply.status == status)
    ]


def status_arg(option):
    """Translate a status name while keeping argparse's concise error context."""
    try:
        return Kind[option.upper()]
    except KeyError as exc:
        alternatives = ", ".join(item.name for item in Kind)
        raise argparse.ArgumentTypeError(f"status must be one of {alternatives}") from exc


def audit_run(args, frac, folder):
    """Audit a completed profile and export optional focused troubleshooting rows."""
    markers = load_markers(folder)
    stats, transfers, discarded = verify_pair_capture(frac, folder / "drive.hex", folder / "capture.hex")
    require_campaign(stats, transfers)
    phases = describe_phases(markers, stats.cycles, transfers, discarded)
    selected = shipment(
        transfers, phases, phase=args.select_phase,
        tag=args.select_tag, status=args.select_status,
    )
    write_transfers(folder / "transactions.csv", selected)
    print(
        f"PASS pair Q{frac} {stats.cycles} cycles, {stats.completed} checked, "
        f"{len(discarded)} reset discards, {stats.compute_order_stalls} younger-ready stalls"
    )
    record = stats.record()
    record["maximum_order_wait"] = max(event.order_wait for event in transfers)
    record["maximum_downstream_wait"] = max(event.downstream_wait for event in transfers)
    record["phases"] = phases
    record["predicted_sustained"] = check_sustained(markers, transfers)
    record["exported_transactions"] = len(selected)
    record["profile"] = frac
    return record


def run_profile(args, frac):
    """Generate and simulate a profile unless auditing files supplied by the caller."""
    folder = args.output.resolve() / f"q{frac}"
    if not args.audit_only:
        plan = make_campaign(frac, args.seed, args.operations)
        plan.write(folder)
        if args.generate_only:
            return {"cycles": plan.index}
        binary = compile_profile(args.iverilog, frac, folder)
        simulate(args.vvp, binary, folder)
    result = audit_run(args, frac, folder)
    if not args.audit_only:
        result["seed"] = args.seed
    return result


def main():
    """Run requested profiles with fixed seeds and JSON diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/pair-cycle"))
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--profile", type=int, action="append", choices=PROFILES)
    parser.add_argument("--seed", type=int, default=14007)
    parser.add_argument("--operations", type=int, default=145)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--generate-only", action="store_true")
    operation.add_argument("--audit-only", action="store_true", help="validate an existing capture")
    parser.add_argument("--select-phase", help="filter the exported CSV to one scenario")
    parser.add_argument("--select-tag", type=int, help="filter the CSV to one correlation tag")
    parser.add_argument("--select-status", type=status_arg, help="filter by result status")
    args = parser.parse_args()
    if args.seed < 0 or args.operations < 0:
        parser.error("seed and operations must be nonnegative")
    profiles = list(dict.fromkeys(args.profile or PROFILES))
    try:
        result = {str(frac): run_profile(args, frac) for frac in profiles}
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    (args.output / "summary.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
