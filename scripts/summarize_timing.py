"""Summarize nextpnr core timing reports as a Markdown table."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, nargs="+")
    args = parser.parse_args()
    print("| Core | Target (MHz) | Routed Fmax (MHz) | Target met | Comb cells | FFs | 18×18 DSPs |")
    print("| --- | ---: | ---: | :---: | ---: | ---: | ---: |")
    for path in args.reports:
        report = json.loads(path.read_text(encoding="utf-8"))
        clocks = report["fmax"]
        if len(clocks) != 1:
            raise ValueError(f"{path}: expected one timed clock, got {list(clocks)}")
        clock = next(iter(clocks.values()))
        utilization = report["utilization"]
        cells = [utilization[key]["used"] for key in ("TRELLIS_COMB", "TRELLIS_FF", "MULT18X18D")]
        core = path.name.split(".")[0]
        meets_target = "Yes" if clock["achieved"] >= clock["constraint"] else "No"
        print(
            f"| {core} | {clock['constraint']:.2f} | {clock['achieved']:.2f} "
            f"| {meets_target} | {cells[0]} | {cells[1]} | {cells[2]} |"
        )


if __name__ == "__main__":
    main()
