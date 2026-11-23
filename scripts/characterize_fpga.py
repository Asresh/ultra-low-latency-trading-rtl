"""Map and route the portfolio cores for Lattice ECP5 and record comparable results."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SAFE_TOKEN = re.compile(r"[a-zA-Z0-9_./-]+")
DEFAULT_TARGET_MHZ = 100


@dataclass(frozen=True)
class Core:
    folder: str
    top: str

    @property
    def cwd(self):
        return ROOT / self.folder


def project_cores():
    manifest = ROOT / "scripts" / "fpga_cores.txt"
    cores = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        tokens = shlex.split(line, comments=True)
        if not tokens:
            continue
        if len(tokens) != 2 or any(not SAFE_TOKEN.fullmatch(token) for token in tokens):
            raise ValueError(f"Invalid core record in {manifest}: {line}")
        cores.append(Core(*tokens))
    return cores


def sources(core):
    names = shlex.split((core.cwd / "rtl.f").read_text(encoding="utf-8"), comments=True)
    if not names or any(
        not SAFE_TOKEN.fullmatch(name) or not (core.cwd / name).is_file() for name in names
    ):
        raise ValueError(f"Invalid RTL source list: {core.folder}/rtl.f")
    return names


def run_logged(args, cwd, log_path, timeout):
    with (cwd / log_path).open("w", encoding="utf-8") as log:
        log.write(f"$ {shlex.join(args)}\n")
        log.flush()
        result = subprocess.run(
            args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False
        )
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}); see {cwd / log_path}")


def identify(tool, option):
    result = subprocess.check_output([tool, option], text=True, stderr=subprocess.STDOUT)
    return next((line for line in reversed(result.splitlines()) if line.strip()), "unknown")


def extract_report(report):
    clocks = report["fmax"]
    if len(clocks) != 1:
        raise ValueError(f"Expected one core clock, got {', '.join(clocks) or 'none'}")
    name, timing = next(iter(clocks.items()))
    utilization = report["utilization"]
    return {
        "clock": name,
        "target": timing["constraint"],
        "achieved": timing["achieved"],
        "comb": utilization["TRELLIS_COMB"]["used"],
        "ff": utilization["TRELLIS_FF"]["used"],
        "dsp": utilization["MULT18X18D"]["used"],
        "bram": utilization["DP16KD"]["used"],
    }


def synthesize(core, args, output):
    mapped = output / "mapped.json"
    commands = "; ".join((
        f"read_slang -I. --top {core.top} {' '.join(sources(core))}",
        f"synth_ecp5 -top {core.top} -json {mapped}",
        "check -assert",
        "stat",
    ))
    run_logged([args.yosys, "-Q", "-T", "-m", "slang", "-p", commands],
               core.cwd, output / "yosys.log", args.timeout)
    return mapped


def route(core, args, output, mapped, seed):
    stem = f"seed-{seed}"
    report_file = output / f"{stem}.report.json"
    routed_file = output / f"{stem}.routed.json"
    argv = [
        args.nextpnr, "--85k", "--package", "CABGA381", "--speed", "8",
        "--out-of-context", "--threads", "1", "--freq", str(args.frequency),
        "--seed", str(seed), "--timing-allow-fail", "--json", str(mapped),
        "--report", str(report_file), "--write", str(routed_file),
    ]
    run_logged(argv, core.cwd, output / f"{stem}.nextpnr.log", args.timeout)
    report = json.loads((core.cwd / report_file).read_text(encoding="utf-8"))
    return {"core": core.top, "project": core.folder, "seed": seed, **extract_report(report)}


def characterize(core, args):
    output = Path("build") / "characterization"
    (core.cwd / output).mkdir(parents=True, exist_ok=True)
    mapped = synthesize(core, args, output)
    if args.synth_only:
        return []
    return [route(core, args, output, mapped, seed) for seed in args.seeds]


def markdown(rows):
    header = (
        "| Core | Seed | Target MHz | Routed MHz | Met | Comb sites | FFs | DSPs | EBRs |\n"
        "| --- | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: |"
    )
    table = [header]
    for row in rows:
        status = "Yes" if row["achieved"] >= row["target"] else "No"
        table.append(
            f"| {row['core']} | {row['seed']} | {row['target']:.2f} | {row['achieved']:.2f} "
            f"| {status} | {row['comb']} | {row['ff']} | {row['dsp']} | {row['bram']} |"
        )
    return "\n".join(table) + "\n"


def positive(raw):
    number = int(raw)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main():
    cores = project_cores()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", action="append", choices=[core.top for core in cores])
    parser.add_argument("--frequency", type=positive, default=DEFAULT_TARGET_MHZ)
    parser.add_argument("--seeds", nargs="+", type=positive, default=[1])
    parser.add_argument("--jobs", type=positive, default=1)
    parser.add_argument("--timeout", type=positive, default=300, help="seconds per tool invocation")
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--nextpnr", default="nextpnr-ecp5")
    parser.add_argument("--synth-only", action="store_true")
    args = parser.parse_args()
    if args.core:
        cores = [core for core in cores if core.top in args.core]
    # Invocations run inside day folders. Make ./tool and ../tool relative
    # to the caller before changing those working directories.
    for key in ("yosys", "nextpnr"):
        value = getattr(args, key)
        if "/" in value:
            setattr(args, key, str(Path(value).resolve()))
    args.seeds = list(dict.fromkeys(args.seeds))

    rows = []
    failures = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        tasks = [(core, pool.submit(characterize, core, args)) for core in cores]
        for core, task in tasks:
            try:
                rows.extend(task.result())
                operation = "mapped" if args.synth_only else "mapped and routed"
                print(f"{core.top}: {operation}", file=sys.stderr, flush=True)
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
                failures.append(f"{core.top}: {exc}")
    if failures:
        parser.exit(1, "\n".join(failures) + "\n")
    if not args.synth_only:
        output = ROOT / "build" / "characterization"
        output.mkdir(parents=True, exist_ok=True)
        payload = {
            "device": "LFE5U-85F / CABGA381 / speed grade 8",
            "yosys": identify(args.yosys, "-V"),
            "nextpnr": identify(args.nextpnr, "--version"),
            "results": rows,
        }
        (output / "result.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        rendered = markdown(rows)
        (output / "result.md").write_text(rendered, encoding="utf-8")
        print(rendered, end="")


if __name__ == "__main__":
    main()
