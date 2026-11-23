#!/usr/bin/env python3
"""Compare logical quote captures for semantic or latency regressions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
import sys

from quote_capture import CommandCapture, Flush, QueryCapture
from quote_schema import PROFILES
from quote_tool import read_capture, reject_aliases
from quote_verify import compare_fields, latency_summary, save_report


@dataclass(frozen=True)
class Epoch:
    commands: tuple[CommandCapture, ...]
    queries: tuple[QueryCapture, ...]
    reset: Flush | None


def split_epochs(records):
    """Group both ordered channels by reset boundaries with chronology checks."""
    previous = -1
    commands = []
    queries = []
    for record in records:
        if isinstance(record, Flush):
            edge = record.cycle
        elif isinstance(record, CommandCapture):
            edge = record.delivered
            commands.append(record)
        elif isinstance(record, QueryCapture):
            edge = record.delivered
            queries.append(record)
        else:
            raise ValueError("unrecognized capture record")
        if edge < previous:
            raise ValueError("capture records moved backwards")
        previous = edge
        if isinstance(record, Flush):
            yield Epoch(tuple(commands), tuple(queries), record)
            commands.clear()
            queries.clear()
    yield Epoch(tuple(commands), tuple(queries), None)


def _identity(observation):
    """Return all data relevant to the accepted operation, excluding pipeline timing."""
    if isinstance(observation, CommandCapture):
        return observation.tick, observation.request, observation.ack
    if isinstance(observation, QueryCapture):
        return observation.tick, observation.request, observation.result
    return observation


@dataclass
class Counts:
    matched: int = 0
    baseline_only: int = 0
    candidate_only: int = 0
    mismatched: int = 0

    def report(self):
        """Return every count with consistent integer fields for CI consumption."""
        return {
            "matched": self.matched,
            "baseline_only": self.baseline_only,
            "candidate_only": self.candidate_only,
            "mismatched": self.mismatched,
        }


class CaptureDifference:
    def __init__(self, limit):
        """Prepare bounded diagnostics and separate per-channel timing distributions."""
        if type(limit) is not int or limit < 0:
            raise ValueError("difference limit must be a nonnegative integer")
        self.limit = limit
        self.entries = []
        self.differences = 0
        self.channels = {"command": Counts(), "query": Counts()}
        self.reference_latency = {"command": [], "query": []}
        self.observed_latency = {"command": [], "query": []}
        self.latency_delta = {"command": [], "query": []}

    def _note(self, label, summary):
        """Count every defect while retaining only the configured number of examples."""
        self.differences += 1
        if len(self.entries) < self.limit:
            self.entries.append({"location": label, "detail": summary})

    def _compare_pair(self, baseline, current, channel, label):
        """Align channel-local order and compare timing only for semantically equal records."""
        counters = self.channels[channel]
        if current is None:
            counters.baseline_only += 1
            self._note(label, f"missing tag {baseline.request.tag}")
            return
        if baseline is None:
            counters.candidate_only += 1
            self._note(label, f"extra tag {current.request.tag}")
            return
        target = _identity(baseline)
        visible = _identity(current)
        if visible != target:
            counters.mismatched += 1
            names = ("tick", "request", "ack" if channel == "command" else "result")
            changed = []
            for field, actual, expected in zip(names, visible, target):
                changed.extend(compare_fields(actual, expected, f"{field}."))
            self._note(label, ", ".join(changed))
            return
        counters.matched += 1
        self.reference_latency[channel].append(baseline.latency)
        self.observed_latency[channel].append(current.latency)
        self.latency_delta[channel].append(current.latency - baseline.latency)

    def _compare_reset(self, baseline, current, epoch):
        """Compare canceled transaction tags without tying them to absolute reset timing."""
        label = f"epoch {epoch} reset"
        if baseline is None or current is None:
            if baseline != current:
                self._note(label, "reset boundary differs")
            return
        if (baseline.commands, baseline.queries) != (current.commands, current.queries):
            self._note(label, "flushed command or query tags differ")

    def compare_epoch(self, baseline, current, epoch):
        """Compare a whole epoch without letting interleaved acknowledgements reorder queries."""
        if baseline is None:
            self._note(f"epoch {epoch}", "candidate has an extra reset epoch")
            baseline = Epoch((), (), None)
        if current is None:
            self._note(f"epoch {epoch}", "candidate ends before this reset epoch")
            current = Epoch((), (), None)
        for channel, expected, observed in (
            ("command", baseline.commands, current.commands),
            ("query", baseline.queries, current.queries),
        ):
            for index, pair in enumerate(zip_longest(expected, observed)):
                self._compare_pair(*pair, channel, f"epoch {epoch} {channel} {index}")
        self._compare_reset(baseline.reset, current.reset, epoch)

    def _timing_report(self, channel, maximum_p95):
        """Report each paired timing distribution and its p95 limit status."""
        baseline = latency_summary(self.reference_latency[channel])
        candidate = latency_summary(self.observed_latency[channel])
        difference = latency_summary(self.latency_delta[channel])
        p95 = difference["p95"]
        return {
            "reference": baseline,
            "candidate": candidate,
            "paired_delta": difference,
            "p95_limit_exceeded": p95 is not None and maximum_p95 is not None and p95 > maximum_p95,
        }

    def report(self, maximum_p95):
        """Summarize semantic parity and paired latency deltas for both channels."""
        timing = {channel: self._timing_report(channel, maximum_p95) for channel in self.channels}
        return {
            "semantic_differences": self.differences,
            "details": self.entries,
            "channels": {channel: counts.report() for channel, counts in self.channels.items()},
            "timing": timing,
            "ok": not self.differences and not any(
                item["p95_limit_exceeded"] for item in timing.values()
            ),
        }


def compare_captures(baseline, candidate, *, details=20, maximum_p95=None):
    """Compare two captures and optionally allow only a bounded paired p95 increase."""
    if maximum_p95 is not None and (type(maximum_p95) is not int or maximum_p95 < 0):
        raise ValueError("maximum p95 increase must be a nonnegative integer")
    result = CaptureDifference(details)
    for index, pair in enumerate(zip_longest(split_epochs(baseline), split_epochs(candidate))):
        result.compare_epoch(*pair, index)
    return result.report(maximum_p95)


def arguments(argv=None):
    """Parse baseline and candidate files plus optional latency and report controls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--profile", choices=PROFILES, default="default")
    parser.add_argument("--maximum-p95", type=int)
    parser.add_argument("--details", type=int, default=20)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    """Read both files and distinguish parity failure from malformed input."""
    args = arguments(argv)
    try:
        if args.output is not None:
            reject_aliases([args.baseline, args.candidate], [args.output])
        profile = PROFILES[args.profile]
        report = compare_captures(
            read_capture(args.baseline, profile), read_capture(args.candidate, profile),
            details=args.details, maximum_p95=args.maximum_p95,
        )
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            save_report(args.output, report)
        answer = "PASS" if report["ok"] else "FAIL"
        print(f"{answer} {report['semantic_differences']} semantic differences")
        for name, timing in report["timing"].items():
            print(f"{name} paired p95 delay {timing['paired_delta']['p95']}")
        return int(not report["ok"])
    except (ValueError, OSError) as exc:
        print(f"quote_compare: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
