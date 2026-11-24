#!/usr/bin/env python3
"""Generate, simulate, verify, and review offline best-quote traces."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys

from quote_analysis import analyze, render_markdown
from quote_capture import deserialize, join_cycles, serialize
from quote_replay import parse_window, read_events, schedule
from quote_schema import PROFILES
from quote_verify import (
    TraceError, compare_trace, read_drives, read_samples, save_report, write_drives,
)
from quote_workload import all_workloads


def select_profile(args):
    """Look up the profile shared by compiler parameters, input widths, and checking."""
    return PROFILES[args.profile]


def reject_aliases(inputs, outputs):
    """Catch equal paths or existing hard links before opening an output for writing."""
    protected = [Path(path) for path in inputs]
    for path in outputs:
        target = Path(path)
        for prior in protected:
            same_name = target.resolve() == prior.resolve()
            same_file = target.exists() and prior.exists() and target.samefile(prior)
            if same_name or same_file:
                raise ValueError(f"{target} aliases {prior}")
        protected.append(target)


def ensure_parents(paths):
    """Create only the directories necessary for the requested artifacts."""
    for path in paths:
        Path(path).parent.mkdir(exist_ok=True, parents=True)


def simulation(vvp, image, drive_path, trace_path, waveform=None):
    """Run the independent RTL process, reporting tool failures with captured output."""
    arguments = [
        vvp, str(image), f"+input={Path(drive_path).resolve()}",
        f"+output={Path(trace_path).resolve()}",
        *([f"+waveform={Path(waveform).resolve()}"] if waveform is not None else []),
    ]
    finished = subprocess.run(arguments, capture_output=True, text=True, check=False)
    if finished.returncode:
        details = finished.stderr.strip() or finished.stdout.strip()
        raise TraceError(f"RTL simulation exited {finished.returncode}: {details}")
    if not Path(trace_path).is_file():
        raise TraceError("simulator returned success without a trace")
    return finished.stdout.strip()


def simulate_command(args):
    """Execute an already compiled image against a saved cycle driver."""
    reject_aliases([args.driver, args.image], [args.trace])
    ensure_parents([args.trace])
    # Validate the driver before the first simulator edge.
    profile = select_profile(args)
    if not tuple(read_drives(args.driver, profile)):
        raise ValueError("simulation needs a nonempty driver")
    print(simulation(args.vvp, args.image, args.driver, args.trace))


def checked_capture(profile, driver_path, trace_path, name="capture", workload=None):
    """Return both handshake coverage and a logical dump after a complete bus comparison."""
    drives = tuple(read_drives(driver_path, profile))
    samples = tuple(read_samples(trace_path, profile))
    checks = compare_trace(drives, samples, profile, name, workload)
    return checks, tuple(join_cycles(drives, samples))


def write_capture(path, records):
    """Write one human-readable logical transfer per JSON line."""
    with Path(path).open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(serialize(record), sort_keys=True) + "\n")


def read_capture(path, profile):
    """Load logical records with useful diagnostics about malformed or inconsistent rows."""
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                yield deserialize(json.loads(line), profile)
            except (TypeError, ValueError) as exc:
                raise TraceError(f"{path}:{number}: {exc}") from exc


def _optional_outputs(args, names):
    """Select explicitly requested report files for path validation and creation."""
    return [getattr(args, name) for name in names if getattr(args, name) is not None]


def audit_command(args):
    """Check every captured cycle and optionally emit readable timing and market reports."""
    outputs = _optional_outputs(args, ("json", "capture", "markdown"))
    reject_aliases([args.driver, args.trace], outputs)
    profile = select_profile(args)
    checks, records = checked_capture(profile, args.driver, args.trace)
    report = analyze(records, profile)
    ensure_parents(outputs)
    if args.json is not None:
        save_report(args.json, {"timing": checks, "market": report})
    if args.capture is not None:
        write_capture(args.capture, records)
    if args.markdown is not None:
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(
        f"PASS {checks['edges']['cycles']} matched cycles, "
        f"{checks['command_latency']['count']} acknowledgements, "
        f"{checks['query_latency']['count']} results"
    )


def report_command(args):
    """Aggregate a previously exported logical capture without running a simulator."""
    outputs = _optional_outputs(args, ("json", "markdown"))
    reject_aliases([args.capture], outputs)
    report = analyze(read_capture(args.capture, select_profile(args)), select_profile(args))
    ensure_parents(outputs)
    if args.json is not None:
        save_report(args.json, report)
    if args.markdown is not None:
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
    else:
        print(render_markdown(report), end="")


def parameters_command(args):
    """Print Icarus named override flags for one simulation geometry."""
    selected = select_profile(args).sv_parameters()
    for name, value in selected.items():
        print(f"-Ptb_best_quote_selector.{name}={value}", end=" ")
    print()


def replay_command(args):
    """Compile dated JSON input and blocked windows into deterministic driver rows."""
    outputs = [args.driver]
    if args.report is not None:
        outputs.append(args.report)
    reject_aliases([args.events], outputs)
    profile = select_profile(args)
    replay = schedule(
        profile, read_events(args.events, profile), args.window,
        tick_origin=args.tick_origin, max_cycles=args.max_cycles,
    )
    ensure_parents(outputs)
    write_drives(args.driver, replay.drives, profile)
    if args.report is not None:
        report = {
            "cycles": len(replay.drives),
            "accepted": [asdict(item) | {"delay": item.delay} for item in replay.accepted],
            "canceled": [asdict(item) for item in replay.canceled],
        }
        save_report(args.report, report)
    print(f"Scheduled {len(replay.accepted)} transfers in {len(replay.drives)} cycles")


def _run_workload(profile, workload, image, vvp, output):
    """Save and compare one scenario's inputs, raw capture, and verification summary."""
    driver = output / "drive.txt"
    trace = output / "trace.txt"
    metrics = output / "verification.json"
    reject_aliases([image], [driver, trace, metrics])
    output.mkdir(parents=True, exist_ok=True)
    write_drives(driver, workload.drives, profile)
    simulation(vvp, image, driver, trace)
    checks, records = checked_capture(profile, driver, trace, workload.name, workload)
    save_report(metrics, checks)
    return checks, records


def regress_command(args):
    """Run the directed and random traces for one already compiled geometry."""
    if args.cycles < 800:
        raise ValueError("random regression needs at least 800 cycles for stall coverage")
    seeds = tuple(args.seed) or (11, 71)
    if len(set(seeds)) != len(seeds):
        raise ValueError("regression seeds must be distinct")
    profile = select_profile(args)
    reports = []
    for workload in all_workloads(profile, args.cycles, seeds):
        output = args.output / workload.name
        checks, _ = _run_workload(profile, workload, args.image, args.vvp, output)
        reports.append(checks)
        edges = checks["edges"]
        print(
            f"PASS {args.profile}:{workload.name} {edges['cycles']} clocks "
            f"{edges.get('commands', 0)} commands {edges.get('queries', 0)} queries"
        )
    save_report(args.output / "summary.json", reports)


def _add_profile(sub):
    """Attach the single geometry selector used by every trace command."""
    sub.add_argument("--profile", choices=PROFILES, default="default")


def _add_simulation(sub):
    """Attach an existing simulation executable and an explicit vvp runner."""
    sub.add_argument("--image", type=Path, required=True)
    sub.add_argument("--vvp", default="vvp")


def _window_argument(text):
    """Convert one sink-window option to a useful argparse error on malformed input."""
    try:
        return parse_window(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def arguments(argv=None):
    """Define the command-line entry points and parse only the supplied arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    parameters = commands.add_parser("parameters", help="print Icarus parameter overrides")
    parameters.set_defaults(run=parameters_command)
    simulate = commands.add_parser("simulate", help="run the RTL against a saved driver")
    _add_simulation(simulate)
    simulate.add_argument("driver", type=Path)
    simulate.add_argument("trace", type=Path)
    simulate.set_defaults(run=simulate_command)
    replay = commands.add_parser("replay", help="compile a normalized JSON-lines input")
    replay.add_argument("events", type=Path)
    replay.add_argument("driver", type=Path)
    replay.add_argument("--window", action="append", type=_window_argument, default=[])
    replay.add_argument("--tick-origin", type=int, default=0)
    replay.add_argument("--max-cycles", type=int, default=100_000)
    replay.add_argument("--report", type=Path)
    replay.set_defaults(run=replay_command)
    audit = commands.add_parser("audit", help="independently check a cycle trace")
    audit.add_argument("driver", type=Path)
    audit.add_argument("trace", type=Path)
    audit.add_argument("--json", type=Path)
    audit.add_argument("--capture", type=Path)
    audit.add_argument("--markdown", type=Path)
    audit.set_defaults(run=audit_command)
    report = commands.add_parser("report", help="analyze a saved logical capture")
    report.add_argument("capture", type=Path)
    report.add_argument("--json", type=Path)
    report.add_argument("--markdown", type=Path)
    report.set_defaults(run=report_command)
    regress = commands.add_parser("regress", help="run directed and seeded verification")
    _add_simulation(regress)
    regress.add_argument("--output", type=Path, default=Path("build/regression"))
    regress.add_argument("--cycles", type=int, default=1800)
    regress.add_argument("--seed", action="append", type=int, default=[])
    regress.set_defaults(run=regress_command)
    for sub in (parameters, simulate, replay, audit, report, regress):
        _add_profile(sub)
    return parser.parse_args(argv)


def main(argv=None):
    """Dispatch the requested tool operation and map expected failures to process status."""
    args = arguments(argv)
    try:
        args.run(args)
    except (ValueError, OSError) as exc:
        print(f"quote_tool: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
