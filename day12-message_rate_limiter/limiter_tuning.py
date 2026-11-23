"""Evaluate candidate bucket policies against an absolute-time, single-key workload."""

import csv
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


DEMAND_HEADER = ("tick", "cost")


@dataclass(frozen=True)
class Demand:
    """One immutable request cost in a non-wrapping software time domain."""

    tick: int
    cost: int

    def __post_init__(self):
        """Reject negative time and zero cost before evaluating any policy."""
        if type(self.tick) is not int or self.tick < 0:
            raise ValueError("demand tick must be nonnegative")
        if type(self.cost) is not int or self.cost <= 0:
            raise ValueError("demand cost must be positive")


@dataclass(frozen=True)
class Policy:
    """Capacity and refill in integer message units and logical ticks."""

    capacity: int
    rate: int

    def __post_init__(self):
        """Mirror the relevant size constraints of one enabled RTL bucket."""
        if type(self.capacity) is not int or self.capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if type(self.rate) is not int or not 0 <= self.rate <= self.capacity:
            raise ValueError("rate must be an integer between zero and capacity")


@dataclass(frozen=True)
class Score:
    """Admission totals and spread of denials for one fixed policy."""

    policy: Policy
    offered: int
    passed: int
    offered_tokens: int
    charged: int
    worst_streak: int
    end_balance: int
    insufficient: int
    oversize: int

    @property
    def limited(self):
        """Compute total denials, including costs greater than capacity."""
        return self.offered - self.passed

    @property
    def pass_ratio(self):
        """Compute an exact message admission ratio without float rounding."""
        return Fraction(self.passed, self.offered) if self.offered else Fraction(1)

    @property
    def charge_ratio(self):
        """Compute the admitted fraction of requested token amount."""
        return Fraction(self.charged, self.offered_tokens) if self.offered_tokens else Fraction(1)

    def to_record(self):
        """Report fractions as strings so readers see the exact ratio."""
        return {
            "capacity": self.policy.capacity,
            "rate": self.policy.rate,
            "offered": self.offered,
            "passed": self.passed,
            "limited": self.limited,
            "offered_tokens": self.offered_tokens,
            "charged": self.charged,
            "worst_limited_streak": self.worst_streak,
            "end_balance": self.end_balance,
            "oversize": self.oversize,
            "insufficient": self.insufficient,
            "pass_ratio": str(self.pass_ratio),
            "charge_ratio": str(self.charge_ratio),
        }


def decimal(text, name):
    """Decode a small workload field and include context in parse failures."""
    if not isinstance(text, str):
        raise ValueError(f"{name} must be a string")
    value = text.strip()
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError(f"{name} must be an unsigned decimal integer")
    return int(value)


def parse_demand_rows(handle, name="<input>"):
    """Read demand CSV in file order to preserve same-tick offer priority."""
    reader = csv.reader(handle, strict=True)
    try:
        header = tuple(next(reader, []))
        if header != DEMAND_HEADER:
            raise ValueError(f"{name}: expected tick,cost header")
        previous = 0
        for row in reader:
            line = reader.line_num
            if len(row) != 2:
                raise ValueError(f"{name}:{line}: expected two columns")
            try:
                tick = decimal(row[0], "tick")
                cost = decimal(row[1], "cost")
                demand = Demand(tick, cost)
                if demand.tick < previous:
                    raise ValueError("ticks must be nondecreasing")
            except ValueError as exc:
                raise ValueError(f"{name}:{line}: {exc}") from exc
            previous = demand.tick
            yield demand
    except csv.Error as exc:
        raise ValueError(f"{name}:{reader.line_num}: {exc}") from exc


def load_demands(path):
    """Load a demand file before any policy search starts."""
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return tuple(parse_demand_rows(handle, str(path)))


def verify_demands(demands):
    """Check direct API calls which did not go through the CSV parser."""
    previous = -1
    for demand in demands:
        if not isinstance(demand, Demand):
            raise TypeError("expected a Demand record")
        if demand.tick < previous:
            raise ValueError("ticks must be nondecreasing")
        previous = demand.tick


def evaluate(policy, demands):
    """Spend from a full starting bucket while retaining exact absolute time."""
    available = policy.capacity
    previous = 0
    offered = passed = offered_tokens = charged = 0
    current_streak = worst_streak = insufficient = oversize = 0
    for demand in demands:
        if demand.tick < previous:
            raise ValueError("ticks must be nondecreasing")
        available = min(policy.capacity, available + policy.rate * (demand.tick - previous))
        previous = demand.tick
        offered += 1
        offered_tokens += demand.cost
        if demand.cost <= available:
            passed += 1
            charged += demand.cost
            available -= demand.cost
            current_streak = 0
        else:
            if demand.cost > policy.capacity:
                oversize += 1
            else:
                insufficient += 1
            current_streak += 1
            worst_streak = max(current_streak, worst_streak)
    return Score(
        policy,
        offered,
        passed,
        offered_tokens,
        charged,
        worst_streak,
        available,
        insufficient,
        oversize,
    )


def accepts_all(policy, demands):
    """Return early on any denial so monotone full-pass searches stay cheap."""
    available = policy.capacity
    previous = 0
    for demand in demands:
        if demand.tick < previous:
            raise ValueError("ticks must be nondecreasing")
        available = min(policy.capacity, available + policy.rate * (demand.tick - previous))
        if demand.cost > available:
            return False
        available -= demand.cost
        previous = demand.tick
    return True


def minimum_refill(capacity, demands):
    """Find the least rate which admits every offer, or None if impossible."""
    traffic = tuple(demands)
    verify_demands(traffic)
    low = 0
    high = capacity
    if not accepts_all(Policy(capacity, high), traffic):
        return None
    while low < high:
        middle = (low + high) // 2
        if accepts_all(Policy(capacity, middle), traffic):
            high = middle
        else:
            low = middle + 1
    return low


def dominant(left, right):
    """Say whether left improves an objective without losing on the others."""
    lcap, rcap = left.policy.capacity, right.policy.capacity
    lrate, rrate = left.policy.rate, right.policy.rate
    comparisons = (
        (lcap, rcap), (lrate, rrate), (-left.passed, -right.passed),
        (-left.charged, -right.charged), (left.worst_streak, right.worst_streak),
    )
    return all(a <= b for a, b in comparisons) and any(a < b for a, b in comparisons)


def frontier(scores):
    """Find all nondominated scores, sorted by capacity and then refill."""
    runs = tuple(scores)
    usable = [score for score in runs if not any(dominant(other, score) for other in runs)]
    return sorted(usable, key=lambda score: (score.policy.capacity, score.policy.rate))


def candidate_policies(capacities, rates):
    """Build one unique legal policy per pair in the configured policy grid."""
    caps = sorted(set(capacities))
    slopes = sorted(set(rates))
    if not caps or not slopes:
        raise ValueError("provide at least one capacity and rate")
    if any(type(capacity) is not int or capacity <= 0 for capacity in caps):
        raise ValueError("capacities must be positive integers")
    if any(type(slope) is not int or slope < 0 for slope in slopes):
        raise ValueError("rates must be nonnegative integers")
    result = tuple(
        Policy(capacity, slope)
        for capacity in caps
        for slope in slopes
        if slope <= capacity
    )
    if not result:
        raise ValueError("every proposed rate exceeds the available capacities")
    return result


def sweep(demands, capacities, rates):
    """Run every candidate. Admission count alone need not be monotone."""
    requests = tuple(demands)
    verify_demands(requests)
    return [evaluate(policy, requests) for policy in candidate_policies(capacities, rates)]


def burst_demand(demands):
    """Collapse same-tick requests to explain unrecoverable capacity misses."""
    totals = Counter()
    for demand in demands:
        totals[demand.tick] += demand.cost
    return sorted(totals.items())


def minimum_full_pass_grid(demands, capacities):
    """Check the capacity lower bound implied by the largest same-tick burst."""
    traffic = tuple(demands)
    verify_demands(traffic)
    bursts = burst_demand(traffic)
    largest = max((amount for _, amount in bursts), default=0)
    result = []
    for cap in sorted(set(capacities)):
        Policy(cap, 0)
        rate = None if cap < largest else minimum_refill(cap, traffic)
        result.append({"capacity": cap, "minimum_refill": rate})
    return {"largest_same_tick_cost": largest, "policies": result}
