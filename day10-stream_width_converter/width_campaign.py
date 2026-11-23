"""Compile, simulate, and audit a reproducible matrix of stream widths."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess

from width_audit import verify
from width_capture import Geometry, convert_to_binary
from width_profile import profile as timeline_profile
from width_workload import generate

PROJECT = Path(__file__).resolve().parent
NAME = re.compile(r"[a-z][a-z0-9_-]{0,24}\Z")
SUMMARY = "campaign.json"
MAX_PROFILES = 20
MAX_PACKETS = 2400
NATIVE_TIMEOUT = 45


class CampaignError(ValueError):
    """A profile is invalid or failed one of its verification phases."""


def value(data, key, minimum, maximum, default=None):
    """Validate configuration integers without accepting JSON booleans."""
    amount = data.get(key, default)
    if type(amount) is not int or not minimum <= amount <= maximum:
        raise CampaignError(f"{key} must be an integer between {minimum} and {maximum}")
    return amount


@dataclass(frozen=True)
class Profile:
    name: str
    geometry: Geometry
    seed: int
    packets: int

    @classmethod
    def parse(cls, data):
        """Load a bounded compile-time geometry and replay seed."""
        if not isinstance(data, dict):
            raise CampaignError("profiles must be JSON objects")
        fields = {"name", "input", "output", "capacity", "seed", "packets"}
        extra = set(data) - fields
        if extra:
            raise CampaignError(f"unrecognized profile field {sorted(extra)[0]!r}")
        name = data.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise CampaignError("profile name must be a short lowercase identifier")
        input_width = value(data, "input", 1, 16)
        output_width = value(data, "output", 1, 16)
        capacity = value(data, "capacity", 1, 384)
        seed = value(data, "seed", 0, 0xFFFFFFFF)
        packets = value(data, "packets", 40, MAX_PACKETS, 180)
        if ((capacity + input_width - 1) // input_width + 12) * (packets + 7) > 80000:
            raise CampaignError("profile risks exceeding the simulation edge budget")
        return cls(name, Geometry(input_width, output_width, capacity), seed, packets)

    def parameters(self):
        """Build safe Icarus overrides for the project-owned testbench."""
        return [
            f"-Ptb_packet_gearbox.IN_BYTES={self.geometry.input_width}",
            f"-Ptb_packet_gearbox.OUT_BYTES={self.geometry.output_width}",
            f"-Ptb_packet_gearbox.MAX_FRAME={self.geometry.capacity}",
        ]

    def values(self):
        """Represent the source config without tool-specific command flags."""
        return {
            "name": self.name, **vars(self.geometry), "seed": self.seed, "packets": self.packets,
        }


def parse_profiles(rows):
    """Require unique target paths for all jobs before launching a process."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PROFILES:
        raise CampaignError(f"expected a list of 1–{MAX_PROFILES} profiles")
    result = []
    names = set()
    for row in rows:
        item = Profile.parse(row)
        if item.name in names:
            raise CampaignError(f"duplicate profile name {item.name!r}")
        names.add(item.name)
        result.append(item)
    return tuple(result)


def defaults():
    """Exercise equal width, extreme packet tails, and both width directions."""
    return parse_profiles([
        {"name": "narrow", "input": 1, "output": 7, "capacity": 21, "seed": 42},
        {"name": "equal", "input": 4, "output": 4, "capacity": 47, "seed": 93},
        {"name": "asymmetric", "input": 7, "output": 3, "capacity": 55, "seed": 3806},
        {"name": "wide", "input": 16, "output": 7, "capacity": 192, "seed": 1221},
    ])


def sha256(path):
    """Fingerprint an artifact incrementally for deterministic regression evidence."""
    result = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            result.update(chunk)
    return result.hexdigest()


def run_tool(args, log, timeout=NATIVE_TIMEOUT):
    """Execute a fixed argument vector without passing config through a shell."""
    with log.open("w", encoding="utf-8") as output:
        try:
            subprocess.run(args, cwd=PROJECT, stdout=output, stderr=subprocess.STDOUT,
                           timeout=timeout, check=True)
        except subprocess.TimeoutExpired as exc:
            raise CampaignError(f"tool timed out after {timeout}s, see {log.name}") from exc
        except subprocess.CalledProcessError as exc:
            raise CampaignError(f"tool returned {exc.returncode}, see {log.name}") from exc
        except OSError as exc:
            raise CampaignError(f"unable to start {args[0]}") from exc


def run_profile(root, specification, compiler, runtime):
    """Build from the candidate geometry and inspect actual transfers independently."""
    work = root / specification.name
    work.mkdir(parents=True, exist_ok=True)
    geometry = specification.geometry
    generated = generate(
        work,
        "stimulus",
        geometry.input_width,
        geometry.output_width,
        geometry.capacity,
        specification.seed,
        specification.packets,
    )
    tape = work / "stimulus.txt"
    executable = work / "test.vvp"
    trace = work / "observed.trace"
    data = work / "observed.wgc"
    run_tool(
        [compiler, "-g2012", "-s", "tb_packet_gearbox", *specification.parameters(),
         "-o", str(executable), "-f", "rtl.f", "tb_packet_gearbox.sv"],
        work / "compile.log",
    )
    run_tool(
        [runtime, str(executable), f"+tape={tape}", f"+trace={trace}"],
        work / "simulation.log",
    )
    audit = verify(trace, geometry.input_width, geometry.output_width, geometry.capacity)
    convert_to_binary(trace, data, geometry)
    timeline = timeline_profile(data, 0)
    if audit["packets"] != timeline["packets"] or audit["bytes"] != timeline["output_payload_bytes"]:
        raise CampaignError("transport and packet timelines disagree on delivered bytes")
    return {
        "profile": specification.values(),
        "stimulus_cycles": generated,
        "traffic": audit,
        "latency": timeline["first_beat_latency_percentiles"],
        "hashes": {"stimulus": sha256(tape), "trace": sha256(trace), "portable_trace": sha256(data)},
    }


def write_summary(root, results):
    """Publish one stable table after all rows have fully passed or failed."""
    results = tuple(results)
    report = {
        "profiles": len(results),
        "passed": sum("error" not in row for row in results),
        "total_cycles": sum(row.get("stimulus_cycles", 0) for row in results),
        "total_output_packets": sum(row.get("traffic", {}).get("packets", 0) for row in results),
        "results": list(results),
    }
    path = root / SUMMARY
    candidate = root / "campaign.tmp"
    candidate.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    candidate.replace(path)
    return report


def campaign(root, profiles, compiler, runtime, jobs=1):
    """Run matrix rows concurrently while preserving original order in the report."""
    if not 1 <= jobs <= 8:
        raise CampaignError("worker count must be between 1 and 8")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    results = []
    with ThreadPoolExecutor(max_workers=jobs) as workers:
        scheduled = [(item, workers.submit(run_profile, root, item, compiler, runtime))
                     for item in profiles]
        for item, future in scheduled:
            try:
                results.append(future.result())
            except (CampaignError, ValueError, OSError) as exc:
                results.append({"profile": item.values(), "error": str(exc)})
    return write_summary(root, results)


def arguments():
    """Define the campaign's explicit simulator, concurrency and profile settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--jobs", type=int, default=2)
    return parser


def main():
    """Run the entire matrix and return a failing exit code if any row failed."""
    parser = arguments()
    args = parser.parse_args()
    try:
        plan = parse_profiles(json.loads(args.profiles.read_text())) if args.profiles else defaults()
        report = campaign(args.output, plan, args.iverilog, args.vvp, args.jobs)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    for row in report["results"]:
        name = row["profile"]["name"]
        if "error" in row:
            print(f"FAIL {name}: {row['error']}")
        else:
            cycles = row["stimulus_cycles"]
            packets = row["traffic"]["packets"]
            print(f"PASS {name}: {cycles} edges, {packets} complete packets")
    if report["passed"] != report["profiles"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
