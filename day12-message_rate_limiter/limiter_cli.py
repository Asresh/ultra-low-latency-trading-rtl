"""Prepare, replay, audit, and tune Day 12 workloads from the command line."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from limiter_capture import CaptureMismatch, audit_directory
from limiter_clock_bridge import compile_absolute, read_absolute, read_policies
from limiter_envelope import capacity_grid, period_grid
from limiter_metrics import Interval, filter_transfers, histogram_record, summarize_keys, write_timeline
from limiter_protocol import PROFILES, Status
from limiter_tuning import frontier, load_demands, minimum_full_pass_grid, sweep
from limiter_workload import (
    compile_program,
    read_arrivals,
    read_stalls,
    sample_program,
    write_inputs,
)
from run_limiter_matrix import elaborate, verify_simulation


def existing_alias(first, second):
    """Detect equal paths and hardlinks before opening a generated file."""
    if first.resolve() == second.resolve():
        return True
    return first.exists() and second.exists() and first.samefile(second)


def protect_sources(sources, targets):
    """Reject output paths that collide with any input or another output."""
    checked = []
    for target in targets:
        for source in (*sources, *checked):
            if existing_alias(source, target):
                raise ValueError(f"output {target} would overwrite {source}")
        checked.append(target)


def dump(record, destination=None):
    """Print a JSON report or write the same text to a selected file."""
    text = json.dumps(record, indent=2, sort_keys=True)
    if destination is None:
        print(text)
    else:
        destination.write_text(text + "\n", encoding="utf-8")


def output_names(folder, *names):
    """Make path restrictions and audit outputs consistent across subcommands."""
    return tuple(folder / name for name in names)


def prepare(args):
    """Read CSV traffic and compute a producer schedule from its ready policy."""
    geometry = PROFILES[args.profile]
    sources = [args.arrivals]
    if args.stalls:
        sources.append(args.stalls)
    programs = output_names(args.output, "manifest.json", "inputs.hex", "source.json")
    if args.run:
        programs += output_names(args.output, "capture.hex", "simulation.log", "summary.json")
    protect_sources(sources, programs)
    commands = read_arrivals(args.arrivals, geometry)
    stalls = read_stalls(args.stalls)
    program = compile_program(
        geometry, commands, stalls, max_queue=args.queue, max_cycles=args.cycles
    )
    program.prepare(args.output, args.profile, geometry)
    result = {"source": program.to_record()}
    dump(result, args.output / "source.json")
    if args.run:
        # Place the temporary compiled testbench away from the supplied CSV.
        # Its observations are retained in the requested output directory.
        with TemporaryDirectory(prefix="day12-", dir=args.output) as scratch:
            binary = elaborate(args.iverilog, args.profile, geometry, Path(scratch))
            report = verify_simulation(args.vvp, binary, args.output)
        result["pins"] = report.to_record()
        dump(result, args.output / "summary.json")
    dump(result)


def sample(args):
    """Export a portable two-file CSV example with reset and backpressure."""
    arrivals, stalls = sample_program(PROFILES[args.profile])
    protect_sources([], output_names(args.output, "workload.csv", "stalls.csv"))
    write_inputs(args.output, arrivals, stalls)
    print(f"Wrote {len(arrivals)} arrivals and {len(stalls)} stall windows to {args.output}")


def named_status(value):
    """Match status names in user input and retain argparse's concise errors."""
    try:
        return Status[value.upper()]
    except KeyError as exc:
        choices = ", ".join(status.name for status in Status)
        raise argparse.ArgumentTypeError(f"expected one of {choices}") from exc


def audit(args):
    """Recheck a raw simulator capture and optionally export a filtered timeline."""
    manifest, report, transfers = audit_directory(args.directory)
    selected = tuple(
        filter_transfers(
            transfers,
            key=args.key,
            status=args.status,
            interval=Interval(args.first, args.last),
            by_issue=args.by_issue,
        )
    )
    result = {
        "manifest": manifest.to_record(),
        "pins": report.to_record(),
        "latency": histogram_record(report.latencies),
        "window_count": len(selected),
        "window_keys": summarize_keys(selected),
    }
    source = output_names(args.directory, "inputs.hex", "capture.hex", "manifest.json")
    outputs = [p for p in (args.timeline, args.json) if p is not None]
    protect_sources(source, outputs)
    if args.timeline:
        write_timeline(args.timeline, selected)
    dump(result, args.json)


def integer_set(raw):
    """Parse comma-separated integers and inclusive ranges without eval."""
    values = set()
    for piece in raw.split(","):
        text = piece.strip()
        fields = text.split("-")
        if (
            len(fields) not in (1, 2)
            or any(not part or not part.isascii() or not part.isdecimal() for part in fields)
        ):
            raise argparse.ArgumentTypeError(f"invalid nonnegative integer or range {piece!r}")
        start = int(fields[0])
        stop = int(fields[-1])
        if start > stop or stop - start > 4096:
            raise argparse.ArgumentTypeError(f"range must ascend across at most 4097 values: {piece}")
        values.update(range(start, stop + 1))
    return sorted(values)


def tune(args):
    """Compare policies and show the smallest full-pass rates when requested."""
    traffic = load_demands(args.demands)
    max_value = PROFILES[args.profile].token_max
    if max(args.capacities) > max_value or max(args.rates) > max_value:
        raise ValueError(f"candidate fields must fit {args.profile} capacity {max_value}")
    candidates = len(args.capacities) * len(args.rates)
    if candidates > args.max_candidates:
        raise ValueError(f"candidate grid has {candidates} pairs, maximum {args.max_candidates}")
    scores = sweep(traffic, args.capacities, args.rates)
    survivors = frontier(scores)
    result = {
        "profile": args.profile,
        "offers": len(traffic),
        "candidates": len(scores),
        "frontier": [score.to_record() for score in survivors],
    }
    if args.full_pass:
        result["minimum_full_pass"] = minimum_full_pass_grid(traffic, args.capacities)
    if args.all_scores:
        result["scores"] = [score.to_record() for score in scores]
    if args.json:
        protect_sources([args.demands], [args.json])
    dump(result, args.json)


def envelope(args):
    """Find the hardest burst at each rate and report its request interval."""
    traffic = load_demands(args.demands)
    capacity = PROFILES[args.profile].token_max
    limit = len(args.rates) + len(args.windows or ())
    if limit > args.max_scans:
        raise ValueError(f"analysis requests {limit} scans, maximum {args.max_scans}")
    if max(args.rates) > capacity:
        raise ValueError(f"proposed rates must fit {args.profile} capacity {capacity}")
    result = {
        "profile": args.profile,
        "offers": len(traffic),
        "refill_requirements": capacity_grid(traffic, args.rates, max_capacity=capacity),
        "periods": period_grid(traffic, args.windows or ()),
    }
    if args.json:
        protect_sources([args.demands], [args.json])
    dump(result, args.json)


def bridge(args):
    """Convert absolute-time offers into a wrap-safe modular workload file."""
    geometry = PROFILES[args.profile]
    outputs = output_names(args.output, "workload.csv", "stalls.csv", "clock-map.json")
    protect_sources([args.demands, args.policies], outputs)
    traffic = read_absolute(args.demands)
    policies = read_policies(args.policies, geometry)
    plan = compile_absolute(
        geometry, traffic, policies, start_tick=args.start, max_inspections=args.max_inspections
    )
    write_inputs(args.output, plan.arrivals, [])
    dump(plan.to_record(), args.output / "clock-map.json")
    print(f"Encoded {plan.offers} offers with {plan.inspections} clock refreshes")


def cli():
    """Document every required input and bind a subcommand to its handler."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)

    example = commands.add_parser("sample", help="write a small demonstrator workload")
    example.add_argument("output", type=Path)
    example.add_argument("--profile", choices=PROFILES, default="default")
    example.set_defaults(handler=sample)

    program = commands.add_parser("prepare", help="compile CSV arrivals for the RTL bench")
    program.add_argument("arrivals", type=Path)
    program.add_argument("output", type=Path)
    program.add_argument("--stalls", type=Path)
    program.add_argument("--profile", choices=PROFILES, default="default")
    program.add_argument("--queue", type=int, default=256)
    program.add_argument("--cycles", type=int, default=100_000)
    program.add_argument("--run", action="store_true", help="build and run Icarus as well")
    program.add_argument("--iverilog", default="iverilog")
    program.add_argument("--vvp", default="vvp")
    program.set_defaults(handler=prepare)

    check = commands.add_parser("audit", help="validate a capture and export optional details")
    check.add_argument("directory", type=Path)
    check.add_argument("--key", type=int)
    check.add_argument("--status", type=named_status)
    check.add_argument("--first", type=int, default=0)
    check.add_argument("--last", type=int, default=-1)
    check.add_argument("--by-issue", action="store_true", help="apply bounds to issue cycles")
    check.add_argument("--timeline", type=Path)
    check.add_argument("--json", type=Path)
    check.set_defaults(handler=audit)

    candidates = commands.add_parser("tune", help="compare fixed policies for a tick,cost CSV")
    candidates.add_argument("demands", type=Path)
    candidates.add_argument("--capacities", type=integer_set, required=True)
    candidates.add_argument("--rates", type=integer_set, required=True)
    candidates.add_argument("--profile", choices=PROFILES, default="default")
    candidates.add_argument("--max-candidates", type=int, default=4096)
    candidates.add_argument("--full-pass", action="store_true")
    candidates.add_argument("--all-scores", action="store_true")
    candidates.add_argument("--json", type=Path)
    candidates.set_defaults(handler=tune)

    bounds = commands.add_parser("envelope", help="calculate exact full-pass burst bounds")
    bounds.add_argument("demands", type=Path)
    bounds.add_argument("--rates", type=integer_set, required=True)
    bounds.add_argument("--windows", type=integer_set)
    bounds.add_argument("--max-scans", type=int, default=128)
    bounds.add_argument("--profile", choices=PROFILES, default="default")
    bounds.add_argument("--json", type=Path)
    bounds.set_defaults(handler=envelope)

    wrap = commands.add_parser("bridge", help="encode unwrapped timestamps with automatic refresh")
    wrap.add_argument("demands", type=Path)
    wrap.add_argument("policies", type=Path)
    wrap.add_argument("output", type=Path)
    wrap.add_argument("--start", type=int, default=0)
    wrap.add_argument("--max-inspections", type=int, default=4096)
    wrap.add_argument("--profile", choices=PROFILES, default="default")
    wrap.set_defaults(handler=bridge)

    return parser


def main():
    """Run one subcommand and surface errors with their file or field context."""
    parser = cli()
    args = parser.parse_args()
    try:
        args.handler(args)
    except (OSError, ValueError, RuntimeError, CaptureMismatch) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
