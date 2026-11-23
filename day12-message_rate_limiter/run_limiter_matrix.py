"""Build profile-specific Icarus runs, then verify and summarize each capture."""

import argparse
import json
import shlex
import subprocess
from collections import Counter
from pathlib import Path

from limiter_capture import audit_directory, prepare
from limiter_coverage import required_for_standard_matrix
from limiter_protocol import Manifest, PROFILES
from limiter_scenarios import scenarios


TOP = "tb_message_rate_limiter"


def command_words(command):
    """Split a tool override while preserving quoted paths and wrapper arguments."""
    result = shlex.split(command)
    if not result:
        raise ValueError("tool command may not be empty")
    return result


def elaborate(iverilog, profile, geometry, output):
    """Compile one parameter geometry with RTL and the common vector driver."""
    binary = output / "limiter.vvp"
    output.mkdir(parents=True, exist_ok=True)
    overrides = [
        f"-P{TOP}.KEYS={geometry.keys}",
        f"-P{TOP}.TICK_W={geometry.tick_w}",
        f"-P{TOP}.TOKEN_W={geometry.token_w}",
        f"-P{TOP}.COUNT_W={geometry.count_w}",
        f"-P{TOP}.TAG_W={geometry.tag_w}",
    ]
    args = command_words(iverilog) + [
        "-g2012", "-Wall", "-s", TOP, "-o", str(binary), *overrides,
        "-f", "rtl.f", f"{TOP}.sv",
    ]
    completed = subprocess.run(args, capture_output=True, text=True)
    (output / "elaboration.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(f"{profile} elaboration failed. See {output / 'elaboration.log'}")
    return binary


def verify_simulation(vvp, binary, directory):
    """Execute the simulator and compare actual ready/valid traces independently."""
    capture = directory / "capture.hex"
    args = command_words(vvp) + [
        str(binary), f"+vectors={directory / 'inputs.hex'}", f"+capture={capture}",
    ]
    completed = subprocess.run(args, capture_output=True, text=True)
    (directory / "simulation.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(f"simulation failed. See {directory / 'simulation.log'}")
    _, report, _ = audit_directory(directory)
    return report


def run_profile(args, profile):
    """Exercise four workload families and keep a per-profile decision summary."""
    geometry = PROFILES[profile]
    profile_output = args.output / profile
    binary = elaborate(args.iverilog, profile, geometry, profile_output)
    records = {}
    for scenario, schedule in scenarios(geometry, args.seed, args.operations):
        folder = profile_output / scenario
        manifest = Manifest(profile, scenario, args.seed, len(schedule.inputs), geometry)
        prepare(folder, manifest, schedule.inputs)
        result = verify_simulation(args.vvp, binary, folder)
        records[scenario] = result.to_record()
        print(f"{profile}/{scenario} {result.summary()}", flush=True)
    return records


def check_coverage(results, profile_names):
    """Fail if the standard matrix never hit an expected protocol condition."""
    statuses = Counter()
    counters = Counter()
    categories = Counter()
    for profile in profile_names:
        for report in results[profile].values():
            statuses.update(report["statuses"])
            categories.update(report["coverage"])
            for name in ("transfers", "discarded", "input_stalled", "output_stalled", "replaced"):
                counters[name] += report[name]
    required = {"OK", "PASS", "LIMITED", "BAD_FIELDS", "DISABLED", "BAD_TICK"}
    if any(PROFILES[name].keys < (1 << PROFILES[name].key_w) for name in profile_names):
        required.add("BAD_KEY")
    missing = required - statuses.keys()
    if missing:
        raise AssertionError(f"missing status coverage: {sorted(missing)}")
    if not all(counters[name] for name in ("transfers", "discarded", "replaced", "output_stalled")):
        raise AssertionError(f"incomplete handshake coverage: {counters}")
    edge_cases = required_for_standard_matrix(profile_names)
    missed = edge_cases - categories.keys()
    if missed:
        raise AssertionError(f"missing boundary categories: {sorted(missed)}")
    return {"statuses": dict(statuses), "totals": dict(counters), "coverage": dict(categories)}


def main():
    """Parse a test plan and retain all directed and random captures on disk."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--output", type=Path, default=Path("build/matrix"))
    parser.add_argument("--profile", action="append", choices=PROFILES)
    parser.add_argument("--seed", type=int, default=121203)
    parser.add_argument("--operations", type=int, default=1500)
    args = parser.parse_args()
    if args.seed < 0 or args.operations < 0:
        parser.error("seed and operation count must be nonnegative")
    names = list(dict.fromkeys(args.profile or PROFILES))
    results = {}
    for profile in names:
        results[profile] = run_profile(args, profile)
    totals = check_coverage(results, names)
    record = {"profiles": results, **totals}
    (args.output / "summary.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"checked {sum(len(v) for v in results.values())} Icarus runs: {totals['totals']}")


if __name__ == "__main__":
    main()
