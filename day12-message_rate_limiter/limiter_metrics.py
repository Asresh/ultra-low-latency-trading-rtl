"""Cycle-latency and per-key pressure reports from completed pin audits."""

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from limiter_protocol import Operation, Status


def nearest_rank(counts, numerator, denominator):
    """Choose a percentile directly from integer bins without expanding samples."""
    if not 0 < numerator <= denominator:
        raise ValueError("percentile must be in (0, 1]")
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("histogram counts must be nonnegative integers")
    size = sum(counts.values())
    if size == 0:
        return None
    wanted = (size * numerator + denominator - 1) // denominator
    cumulative = 0
    for bin_value, count in sorted(counts.items()):
        cumulative += count
        if cumulative >= wanted:
            return bin_value
    raise AssertionError("histogram did not contain its requested rank")


def histogram_record(counts):
    """Produce sparse bins and useful percentiles for JSON output."""
    if not counts:
        return {"samples": 0, "min": None, "p50": None, "p95": None, "max": None, "bins": {}}
    occupied = sorted(value for value, count in counts.items() if count)
    return {
        "samples": sum(counts.values()),
        "min": occupied[0] if occupied else None,
        "p50": nearest_rank(counts, 1, 2),
        "p95": nearest_rank(counts, 19, 20),
        "max": occupied[-1] if occupied else None,
        "bins": {str(value): counts[value] for value in occupied},
    }


@dataclass
class KeyReport:
    """Delivered traffic totals labelled by independent limiter key."""

    key: int
    first_cycle: int = -1
    last_cycle: int = -1
    changes: int = 0
    admitted_cost: int = 0
    requested_cost: int = 0
    streak: int = 0
    max_streak: int = 0
    statuses: Counter = field(default_factory=Counter)
    latencies: Counter = field(default_factory=Counter)
    oldest_issue: int = -1
    newest_issue: int = -1

    def deliver(self, transfer):
        """Update one grouping with latency, denial runs, and token totals."""
        command, response = transfer.command, transfer.response
        if self.key != response.key:
            raise ValueError("mismatched pressure-report key")
        if self.last_cycle >= transfer.delivered:
            raise ValueError("deliveries for one key must be strictly ordered")
        if self.first_cycle == -1:
            self.first_cycle = transfer.delivered
            self.oldest_issue = transfer.accepted
        self.last_cycle = transfer.delivered
        self.newest_issue = transfer.accepted
        self.statuses[response.status] += 1
        self.latencies[transfer.latency] += 1
        if command.op == Operation.CONFIG and response.status == Status.OK:
            self.changes += 1
            self.streak = 0
        elif command.op == Operation.OFFER:
            if response.status == Status.PASS:
                self.admitted_cost += command.amount
                self.requested_cost += command.amount
                self.streak = 0
            elif response.status == Status.LIMITED:
                self.requested_cost += command.amount
                self.streak += 1
                self.max_streak = max(self.streak, self.max_streak)

    def to_record(self):
        """Return a pressure report with JSON field names rather than enums."""
        return {
            "key": self.key,
            "first_delivery": self.first_cycle,
            "last_delivery": self.last_cycle,
            "first_issue": self.oldest_issue,
            "last_issue": self.newest_issue,
            "configurations": self.changes,
            "admitted_cost": self.admitted_cost,
            "requested_cost": self.requested_cost,
            "max_limited_streak": self.max_streak,
            "statuses": {
                status.name: count for status, count in sorted(self.statuses.items())
            },
            "latency": histogram_record(self.latencies),
        }


def summarize_keys(transfers):
    """Report each encountered key while preserving per-key delivery order."""
    reports = {}
    for transfer in transfers:
        key = transfer.response.key
        if key not in reports:
            reports[key] = KeyReport(key)
        reports[key].deliver(transfer)
    return [report.to_record() for _, report in sorted(reports.items())]


@dataclass(frozen=True)
class Interval:
    """Half-open range over accepted or delivered simulation edge numbers."""

    first: int = 0
    last: int = -1

    def __post_init__(self):
        """Accept -1 as an open upper bound and reject inverted intervals."""
        if type(self.first) is not int or self.first < 0:
            raise ValueError("first cycle must be nonnegative")
        if type(self.last) is not int or (self.last != -1 and self.last < self.first):
            raise ValueError("last cycle must be -1 or at least first")

    def contains(self, cycle):
        """Decide whether a cycle is inside the requested timeline window."""
        return cycle >= self.first and (self.last == -1 or cycle < self.last)


def filter_transfers(transfers, *, key=None, status=None, interval=Interval(), by_issue=False):
    """Select a trace window while preserving chronological delivery order."""
    for transfer in transfers:
        if key is not None and key != transfer.response.key:
            continue
        if status is not None and status != transfer.response.status:
            continue
        cycle = transfer.accepted if by_issue else transfer.delivered
        if interval.contains(cycle):
            yield transfer


TIMELINE_FIELDS = (
    "issue_cycle", "delivery_cycle", "latency", "key", "tag", "operation", "status",
    "tick", "amount", "enabled", "anchor", "capacity", "refill", "before", "balance",
    "passed", "limited", "charged", "saturated",
)


def transfer_row(transfer):
    """Map command and result fields to a readable timeline CSV record."""
    command = transfer.command
    response = transfer.response
    return (
        transfer.accepted, transfer.delivered, transfer.latency, command.key,
        command.tag, command.op.name, response.status.name, command.tick, command.amount,
        response.enabled, response.anchor, response.capacity, response.rate,
        response.before, response.balance, response.passed, response.limited,
        response.charged, response.saturated,
    )


def write_timeline(path, transfers):
    """Write only already-validated transfers, keeping empty windows parseable."""
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(TIMELINE_FIELDS)
        writer.writerows(transfer_row(transfer) for transfer in transfers)
