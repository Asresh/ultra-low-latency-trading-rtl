"""Derive cycle throughput and uninterrupted delivery runs from audited traces."""

import argparse
import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from limiter_capture import CaptureMismatch, audit_directory
from limiter_metrics import histogram_record


@dataclass(frozen=True)
class Run:
    """One closed range of consecutive edges that delivered a decision."""

    first: int
    last: int

    @property
    def size(self):
        """Count every transferred edge in the inclusive range."""
        return self.last - self.first + 1

    def to_record(self):
        """Export range endpoints and their total length."""
        return {"first": self.first, "last": self.last, "size": self.size}


def uninterrupted(cycles):
    """Split strictly increasing handshake edges into contiguous delivery runs."""
    first = previous = None
    for cycle in cycles:
        if type(cycle) is not int or cycle < 0:
            raise ValueError("handshake cycle must be nonnegative")
        if previous is not None and cycle <= previous:
            raise ValueError("handshake cycles must increase")
        if previous is None:
            first = cycle
        elif cycle != previous + 1:
            yield Run(first, previous)
            first = cycle
        previous = cycle
    if previous is not None:
        yield Run(first, previous)


def run_summary(cycles, total_cycles):
    """Report full-capture and active-window density as separate exact ratios."""
    runs = list(uninterrupted(cycles))
    transfers = sum(run.size for run in runs)
    if total_cycles < 0 or (runs and runs[-1].last >= total_cycles):
        raise ValueError("handshake outside recorded cycle count")
    span = (runs[-1].last - runs[0].first + 1) if runs else 0
    window_density = Fraction(transfers, span) if span else Fraction(0)
    capture_density = Fraction(transfers, total_cycles) if total_cycles else Fraction(0)
    best = max(runs, key=lambda run: run.size, default=None)
    return {
        "transfers": transfers,
        "first_active": runs[0].first if runs else None,
        "last_active": runs[-1].last if runs else None,
        "runs": len(runs),
        "longest_run": best.to_record() if best else None,
        "active_decisions_per_cycle": str(window_density),
        "capture_decisions_per_cycle": str(capture_density),
    }


def benchmark(directory):
    """Use delivered requests only and report reset cancellations explicitly."""
    manifest, report, transactions = audit_directory(directory)
    return {
        "profile": manifest.profile,
        "scenario": manifest.scenario,
        "seed": manifest.seed,
        "cycles": report.cycles,
        "reset_discarded": report.discarded,
        "sink_stalls": report.output_stalled,
        "successful_issues": run_summary((item.accepted for item in transactions), report.cycles),
        "deliveries": run_summary((item.delivered for item in transactions), report.cycles),
        "latencies": histogram_record(report.latencies),
    }


def main():
    """Summarize an existing verified trace without making clock-rate claims."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(benchmark(args.directory), indent=2, sort_keys=True))
    except (OSError, ValueError, CaptureMismatch) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
