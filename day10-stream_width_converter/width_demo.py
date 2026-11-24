"""Run the introductory JSON packet example all the way through Icarus and back."""

import argparse
import json
from pathlib import Path

from width_audit import capture, verify
from width_campaign import run_tool, sha256
from width_capture import Geometry, convert_to_binary
from width_excerpt import epoch_summaries
from width_profile import timelines
from width_recipe import Recipe, compile_recipe

EXAMPLE = Path(__file__).resolve().parent / "fixtures" / "first_packets.json"
EXPECTED = ((65, "1122334455"), (65, "aabbccddeeff001122"), (67, "be"))


def lesson(source, binary, directory, vvp, waveform=None):
    """Verify and explain the exact known packets emitted by the article fixture."""
    value = json.loads(source.read_text(encoding="utf-8"))
    recipe = Recipe.parse(value)
    if recipe.geometry != Geometry(4, 8, 128):
        raise ValueError("example must match the Makefile's 4-to-8 lesson binary")
    directory.mkdir(parents=True, exist_ok=True)
    tape = directory / "lesson.txt"
    trace = directory / "lesson.trace"
    archive = directory / "lesson.wgc"
    edges = compile_recipe(source, tape)
    run_tool([vvp, str(binary.resolve()), "+small", f"+tape={tape.resolve()}",
              f"+trace={trace.resolve()}", *([f"+waveform={waveform.resolve()}"] if waveform else [])],
             directory / "simulate.log")
    observed = verify(trace, 4, 8, 128)
    if observed["reports"] != {"OK": 3, "BAD_KEEP": 1}:
        raise ValueError("introductory workload must contain one malformed packet")
    convert_to_binary(trace, archive, recipe.geometry)
    rows = timelines(archive)[-1]
    want = tuple((item.tag, item.data.hex()) for item in rows)
    if want != EXPECTED:
        raise ValueError(f"lesson packet order or byte values changed: {want}")
    checks = epoch_summaries(archive)
    traces = raw_summary(trace)
    if len(checks["epochs"]) != 1 or traces["accepted_inputs"] != 8:
        raise ValueError("lesson changed its fixed scenario coverage")
    return {
        "edges": edges, "input_transfers": traces["accepted_inputs"],
        "reports": observed["reports"], "packets": [item.row() for item in rows],
        "capture_sha256": sha256(archive),
    }


def raw_summary(trace):
    """Inspect the textual hardware observations independently of binary export."""
    counts = {"accepted_inputs": 0, "output_transfers": 0, "report_transfers": 0}
    keys = {"input": "accepted_inputs", "output": "output_transfers", "report": "report_transfers"}
    with trace.open(encoding="ascii") as file:
        for event in capture(file, 4, 8):
            if event.channel in keys:
                counts[keys[event.channel]] += 1
    return counts


def main():
    """Create the beginner-readable evidence artifact used by the README."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--waveform", type=Path, help="optional VCD for the checked lesson")
    args = parser.parse_args()
    result = lesson(EXAMPLE, args.binary, args.output, args.vvp, args.waveform)
    print("PASS introductory recipe through RTL")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
