"""Compare generic synthesized limiter outputs to the independent Python oracle."""

import argparse
import json
import subprocess
from pathlib import Path

from limiter_capture import prepare
from limiter_protocol import Manifest, PROFILES
from limiter_scenarios import scenarios
from run_limiter_matrix import command_words, verify_simulation


def elaborate_netlist(netlist, iverilog, build):
    """Build the same cycle driver around a flattened ordinary Verilog netlist."""
    build.mkdir(parents=True, exist_ok=True)
    binary = build / "netlist.vvp"
    invocation = command_words(iverilog) + [
        "-g2012",
        "-DRATE_NETLIST",
        "-s", "tb_message_rate_limiter",
        "-o", str(binary),
        str(netlist),
        "tb_message_rate_limiter.sv",
    ]
    result = subprocess.run(invocation, text=True, capture_output=True)
    log = build / "elaboration.log"
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"netlist elaboration failed. See {log}")
    return binary


def verify_cases(binary, vvp, build, seed, operations):
    """Repeat default-geometry software comparison over the synthesized logic."""
    geometry = PROFILES["default"]
    summaries = {}
    for name, schedule in scenarios(geometry, seed, operations):
        folder = build / name
        manifest = Manifest("default", f"netlist_{name}", seed, len(schedule.inputs), geometry)
        prepare(folder, manifest, schedule.inputs)
        result = verify_simulation(vvp, binary, folder)
        print(f"netlist/{name} {result.summary()}")
        summaries[name] = result.to_record()
    return summaries


def main():
    """Elaborate a checked Yosys netlist and save its verified operation totals."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--netlist", type=Path, default=Path("build/limiter-netlist.v"))
    parser.add_argument("--output", type=Path, default=Path("build/netlist"))
    parser.add_argument("--operations", type=int, default=480)
    parser.add_argument("--seed", type=int, default=121203)
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default="vvp")
    args = parser.parse_args()
    if args.seed < 0 or args.operations < 0:
        parser.error("seed and operation count must be nonnegative")
    if not args.netlist.is_file():
        parser.error(f"missing synthesized netlist {args.netlist}")
    binary = elaborate_netlist(args.netlist, args.iverilog, args.output)
    result = verify_cases(binary, args.vvp, args.output, args.seed, args.operations)
    (args.output / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
