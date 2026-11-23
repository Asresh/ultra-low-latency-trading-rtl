"""Analyze raw and transported delay samples without assuming evenly sampled traffic."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import json
import sys

from latency_capture import CaptureError, read_event, read_words
from latency_reference import HALF64, Status, modular_delta


@dataclass(frozen=True)
class Quantile:
    rank: int
    value: int


@dataclass(frozen=True)
class BucketWindow:
    first_tick: int
    last_tick: int
    samples: int
    maximum: int


@dataclass(frozen=True)
class PathBudget:
    event_class: int
    start_probe: int
    stop_probe: int
    percentile: Fraction
    limit: int
    minimum_samples: int

    @property
    def key(self):
        return self.event_class, self.start_probe, self.stop_probe


def quantile(sorted_values, percentile):
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be a fraction in (0, 1]")
    n = len(sorted_values)
    if not n:
        return None
    numerator = n * percentile.numerator
    rank = (numerator + percentile.denominator - 1) // percentile.denominator
    return Quantile(rank, sorted_values[rank - 1])


def percentile_value(text):
    try:
        number = Fraction(text) / 100
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError("expected a percentage") from exc
    if not 0 < number <= 1:
        raise argparse.ArgumentTypeError("percentile must be in (0,100]")
    return number


def parse_path(raw):
    try:
        a, b, c = [int(token, 10) for token in raw.split(":")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("path must be CLASS:START_PROBE:STOP_PROBE") from exc
    if any(value < 0 or value > 255 for value in (a, b, c)):
        raise argparse.ArgumentTypeError("path numbers must fit eight bits")
    return a, b, c


def parse_budget(raw):
    fragments = raw.split(",")
    if len(fragments) != 4:
        raise argparse.ArgumentTypeError("budget needs PATH,PERCENT,LIMIT_TICKS,MIN_SAMPLES")
    key = parse_path(fragments[0])
    percentile = percentile_value(fragments[1])
    try:
        limit = int(fragments[2])
        minimum = int(fragments[3])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("budget limit and sample floor must be integers") from exc
    if limit < 0 or minimum < 1:
        raise argparse.ArgumentTypeError("limit must be positive with at least one sample")
    return PathBudget(*key, percentile, limit, minimum)


class Profile:
    def __init__(self):
        self.status_counts = Counter()
        self.by_path = defaultdict(list)
        self.timeline = defaultdict(list)
        self.totals_by_class = Counter()
        self.observed = 0
        self.first_tick = None
        self.last_tick = None

    def add(self, event):
        if self.first_tick is None:
            self.first_tick = event.observed_tick
        if self.last_tick is not None and modular_delta(event.observed_tick, self.last_tick) >= HALF64:
            raise CaptureError("decreasing observation order in report capture")
        if modular_delta(event.observed_tick, self.first_tick) >= HALF64:
            raise CaptureError("capture is too long for modular time arithmetic")
        self.last_tick = event.observed_tick
        self.observed += 1
        self.status_counts[event.status] += 1
        self.totals_by_class[event.event_class] += 1
        if event.status == Status.MATCH:
            key = (event.event_class, event.start_probe, event.end_probe)
            self.by_path[key].append(event.duration)
            self.timeline[key].append((
                modular_delta(event.observed_tick, self.first_tick),
                event.duration,
            ))

    @classmethod
    def from_capture(cls, source):
        result = cls()
        for word in read_words(source, 320):
            result.add(read_event(word))
        if not result.observed:
            raise CaptureError("capture contained no result words")
        return result

    def group_summary(self, key, quantiles):
        sorted_values = sorted(self.by_path.get(key, ()))
        if not sorted_values:
            return {"matched": 0, "min_ticks": None, "max_ticks": None, "quantiles": {}}
        return {
            "matched": len(sorted_values),
            "min_ticks": sorted_values[0],
            "max_ticks": sorted_values[-1],
            "quantiles": {
                str(float(level * 100)): quantile(sorted_values, level).value
                for level in quantiles
            },
        }

    def windows(self, key, size):
        if size <= 0:
            raise ValueError("window size must be positive")
        buckets = defaultdict(list)
        for tick, duration in self.timeline.get(key, ()):
            buckets[tick // size].append(duration)
        return [
            BucketWindow(index * size, (index + 1) * size - 1, len(values), max(values))
            for index, values in sorted(buckets.items())
        ]

    def meets(self, budget):
        sorted_values = sorted(self.by_path.get(budget.key, ()))
        result = quantile(sorted_values, budget.percentile)
        enough = len(sorted_values) >= budget.minimum_samples
        if not enough:
            reason = "insufficient_samples"
        elif result.value > budget.limit:
            reason = "over_budget"
        else:
            reason = "met"
        return {
            "path": budget.key,
            "samples": len(sorted_values),
            "minimum_samples": budget.minimum_samples,
            "percentile": float(budget.percentile * 100),
            "value_ticks": result.value if result else None,
            "budget_ticks": budget.limit,
            "verdict": reason,
        }


def empirical_gap(sample_a, sample_b):
    if not sample_a or not sample_b:
        return None
    a = Counter(sample_a)
    b = Counter(sample_b)
    cumulative_a = cumulative_b = 0
    gap = Fraction(0)
    where = None
    for cutoff in sorted(set(a) | set(b)):
        cumulative_a += a[cutoff]
        cumulative_b += b[cutoff]
        delta = abs(
            Fraction(cumulative_a, len(sample_a)) - Fraction(cumulative_b, len(sample_b))
        )
        if delta > gap:
            gap, where = delta, cutoff
    return {"largest_cdf_gap": float(gap), "cutoff_ticks": where}


def profile_report(profile, quantiles, group_filter, reference=None, window=None, budgets=()):
    keys = set(profile.by_path)
    if reference is not None:
        keys.update(reference.by_path)
    if group_filter and group_filter not in keys:
        keys.add(group_filter)
    if group_filter:
        keys.intersection_update({group_filter})
    groups = []
    for key in sorted(keys):
        row = {"path": key, **profile.group_summary(key, quantiles)}
        if reference is not None:
            base_samples = reference.by_path.get(key, ())
            curr_samples = profile.by_path.get(key, ())
            row["baseline_matched"] = len(base_samples)
            row["cdf_comparison"] = empirical_gap(base_samples, curr_samples)
            if base_samples and curr_samples:
                before = quantile(sorted(base_samples), Fraction(99, 100)).value
                after = quantile(sorted(curr_samples), Fraction(99, 100)).value
                row["p99_change_ticks"] = after - before
        if window:
            row["windows"] = [vars(i) for i in profile.windows(key, window)]
        groups.append(row)
    return {
        "observations": profile.observed,
        "elapsed_ticks": modular_delta(profile.last_tick, profile.first_tick),
        "status": {k.name: count for k, count in sorted(profile.status_counts.items())},
        "by_path": groups,
        "budgets": [profile.meets(budget) for budget in budgets],
    }


def open_profile(path):
    with path.open(encoding="ascii") as source:
        return Profile.from_capture(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--path", type=parse_path)
    parser.add_argument("--percentile", action="append", type=percentile_value)
    parser.add_argument("--window", type=int)
    parser.add_argument("--budget", type=parse_budget, action="append", default=[])
    args = parser.parse_args()
    if args.window is not None and args.window <= 0:
        parser.error("--window must be positive")
    try:
        profile = open_profile(args.capture)
        baseline = open_profile(args.baseline) if args.baseline else None
        percentiles = args.percentile or [Fraction(1, 2), Fraction(99, 100)]
        report = profile_report(profile, percentiles, args.path, baseline, args.window, args.budget)
        print(json.dumps(report, indent=2))
        return 0 if all(t["verdict"] == "met" for t in report["budgets"]) else 2
    except (CaptureError, ValueError, OSError, UnicodeError) as exc:
        print(f"latency_profile: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
