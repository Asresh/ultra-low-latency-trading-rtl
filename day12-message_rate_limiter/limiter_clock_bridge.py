"""Schedule real-time offers on a short modular hardware timestamp port."""

import json
from dataclasses import dataclass
from pathlib import Path

from limiter_protocol import Command
from limiter_tuning import Policy
from limiter_workload import Arrival, line_rows, parse_number


ABSOLUTE_HEADER = ("tick", "key", "cost")


@dataclass(frozen=True)
class AbsoluteOffer:
    """One offer with an unwrapped time from a capture or host clock."""

    tick: int
    key: int
    cost: int

    def __post_init__(self):
        """Validate semantic fields before a fixed-width command can be emitted."""
        if type(self.tick) is not int or self.tick < 0:
            raise ValueError("offer tick must be a nonnegative integer")
        if type(self.key) is not int or self.key < 0:
            raise ValueError("offer key must be a nonnegative integer")
        if type(self.cost) is not int or self.cost <= 0:
            raise ValueError("offer cost must be a positive integer")


@dataclass(frozen=True)
class PlanEntry:
    """Describe the purpose and unwrapped timestamp of one generated action."""

    arrival: Arrival
    purpose: str
    absolute_tick: int
    source_row: int

    def to_record(self):
        """Retain software correlation even when the wire tag wraps."""
        command = self.arrival.command
        return {
            "cycle": self.arrival.cycle,
            "purpose": self.purpose,
            "absolute_tick": self.absolute_tick,
            "source_row": self.source_row,
            "key": command.key,
            "tag": command.tag,
            "encoded_tick": command.tick,
        }


@dataclass(frozen=True)
class ClockPlan:
    """Bridge output containing one bus-cycle arrival per generated command."""

    entries: tuple
    inspections: int
    offers: int

    @property
    def arrivals(self):
        """Expose ordered events to the normal workload compiler."""
        return tuple(entry.arrival for entry in self.entries)

    def to_record(self):
        """List generated clock-refresh actions and original offer identities."""
        return {
            "inspections": self.inspections,
            "offers": self.offers,
            "rows": [row.to_record() for row in self.entries],
        }


def read_absolute(path):
    """Parse chronological unwrapped offers before performing time conversion."""
    result = []
    for number, row in line_rows(path, ABSOLUTE_HEADER):
        try:
            offer = AbsoluteOffer(*(parse_number(value, label) for value, label in zip(row, ABSOLUTE_HEADER)))
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
        result.append(offer)
    return tuple(result)


def policies_from_records(records, geometry):
    """Read JSON keyed policy configurations and reject duplicate market streams."""
    if not isinstance(records, list):
        raise ValueError("policies must be an array")
    answer = {}
    for number, config in enumerate(records, 1):
        if not isinstance(config, dict) or set(config) != {"key", "capacity", "rate"}:
            raise ValueError(f"policy row {number} needs key, capacity, and rate")
        key = config["key"]
        if type(key) is not int or not 0 <= key < geometry.keys:
            raise ValueError(f"invalid key in policy row {number}")
        if key in answer:
            raise ValueError(f"duplicate policy for key {key}")
        policy = Policy(config["capacity"], config["rate"])
        if policy.capacity > geometry.token_max:
            raise ValueError(f"capacity for key {key} exceeds the token port")
        answer[key] = policy
    return answer


def read_policies(path, geometry):
    """Load exact key, capacity, and refill fields from the policy file."""
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    return policies_from_records(records, geometry)


def compile_absolute(geometry, demands, policies, *, start_tick=0, max_inspections=4096):
    """Insert the fewest per-key inspect commands to stay within the half range."""
    if type(start_tick) is not int or start_tick < 0:
        raise ValueError("start tick must be nonnegative")
    if type(max_inspections) is not int or max_inspections < 0:
        raise ValueError("maximum inspections must be nonnegative")
    entries = []
    latest = {}
    inspections = 0
    present = start_tick
    demand_count = 0

    def entry(command, detail, tick, row):
        """Use command order as bus arrival cycle and retain its absolute provenance."""
        cycle = len(entries)
        entries.append(PlanEntry(Arrival(cycle, command), detail, tick, row))

    def tag():
        """Encode a physical correlation tag without losing the separate row index."""
        return len(entries) & geometry.tag_max

    for key, policy in sorted(policies.items()):
        if type(key) is not int or key < 0 or key >= geometry.keys:
            raise ValueError(f"policy key {key!r} is not implemented")
        if not isinstance(policy, Policy) or policy.capacity > geometry.token_max:
            raise ValueError(f"policy for key {key} is missing or unsupported")
        latest[key] = start_tick
        encoded = geometry.wrapped(start_tick)
        entry(Command.configure(key, encoded, policy.capacity, policy.rate, tag()), "configure", start_tick, -1)
    max_forward = geometry.half - 1
    for row, offer in enumerate(demands):
        if not isinstance(offer, AbsoluteOffer):
            raise TypeError("expected an AbsoluteOffer")
        if offer.tick < present:
            raise ValueError(f"absolute time moved backwards at row {row}")
        if offer.key not in latest:
            raise ValueError(f"missing policy for offer key {offer.key} at row {row}")
        if offer.cost > geometry.token_max:
            raise ValueError(f"offer cost exceeds the token port at row {row}")
        present = offer.tick
        before = latest[offer.key]
        # The final OFFER itself advances the last safe segment. Every preceding
        # full-width segment requires a separate INSPECT to refresh the anchor.
        extra = max(0, (offer.tick - before - 1) // max_forward)
        if inspections + extra > max_inspections:
            raise ValueError(f"row {row}: clock refreshes exceed {max_inspections}")
        for _ in range(extra):
            before += max_forward
            encoded = geometry.wrapped(before)
            entry(Command.inspect(offer.key, encoded, tag()), "refresh", before, row)
        inspections += extra
        latest[offer.key] = offer.tick
        encoded = geometry.wrapped(offer.tick)
        entry(Command.offer(offer.key, encoded, offer.cost, tag()), "offer", offer.tick, row)
        demand_count += 1
    return ClockPlan(tuple(entries), inspections, demand_count)
