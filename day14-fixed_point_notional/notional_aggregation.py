"""Explain how separate and merged arithmetic differ for otherwise identical orders."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path

from notional_batch import Order, read_orders
from notional_model import Kind, Request, calculate

MAX_QUANTITY = (1 << 32) - 1


class GroupError(ValueError):
    """An input batch cannot be compared under one aggregation policy."""


@dataclass(frozen=True)
class GroupKey:
    instrument: str
    price: int
    fee_bps: int
    rounding: int
    sell: bool

    @classmethod
    def for_order(cls, payload):
        """Merge only orders with the exact same side, pricing, and rounding contract."""
        req = payload.request
        return cls(payload.instrument, req.price, req.fee_bps, req.rounding, req.sell)

    def order(self, quantity):
        """Use a neutral tag and checked aggregate size for the arithmetic comparison."""
        return Request(0, self.price, quantity, self.fee_bps, self.rounding, self.sell)


@dataclass
class Totals:
    quantity: int = 0
    orders: int = 0
    gross: int = 0
    fee: int = 0
    cash: int = 0
    inexact: int = 0

    def record(self, quantity, reply):
        """Accumulate unbounded host totals from ordinary successful arithmetic replies."""
        if reply.status != Kind.OK:
            raise GroupError(f"failed arithmetic '{reply.status.name}' cannot be aggregated")
        self.quantity += quantity
        self.orders += 1
        self.gross += reply.gross
        self.fee += reply.fee
        self.cash += reply.cash
        self.inexact += int(reply.inexact)

    def row(self):
        """Serialize separate and aggregated fields without truncation to 64 bits."""
        return dict(vars(self))

    def subtract(self, baseline):
        """Summarize the rounding change on cash, gross, and fee."""
        return {
            "cash": self.cash - baseline.cash,
            "gross": self.gross - baseline.gross,
            "fee": self.fee - baseline.fee,
        }


@dataclass(frozen=True)
class GroupResult:
    key: GroupKey
    separate: Totals
    aggregated: Totals
    chunks: tuple

    def row(self, sample_chunks):
        """Return reproducible totals and a bounded sample of merged quantities."""
        return {
            "group": vars(self.key),
            "separate": self.separate.row(),
            "aggregated": self.aggregated.row(),
            "delta_aggregated_minus_separate": self.aggregated.subtract(self.separate),
            "chunks": list(self.chunks[:sample_chunks]),
            "total_chunks": len(self.chunks),
        }


def partition(quantity, size_limit):
    """Yield maximal legal positive chunks followed by any remaining tail."""
    if not 1 <= size_limit <= MAX_QUANTITY:
        raise GroupError("aggregate chunk limit must be a positive uint32")
    if quantity <= 0:
        raise GroupError("cannot partition zero quantity")
    while quantity:
        chunk = min(quantity, size_limit)
        yield chunk
        quantity -= chunk


def largest_feasible(key, frac, upper):
    """Find the largest chunk with representable arithmetic, excluding cash-limit policy."""
    low = 0
    high = upper + 1
    while high - low > 1:
        next_size = (high + low) // 2
        reply = calculate(key.order(next_size), frac)
        if reply.status == Kind.OK:
            low = next_size
        elif reply.status == Kind.OVERFLOW:
            high = next_size
        else:
            raise GroupError(f"invalid group pricing: {reply.status.name}")
    if low == 0:
        raise GroupError("the arithmetic cannot represent even one unit")
    return low


class AggregationStudy:
    """Group successful rows with identical policy and compare rounding stages."""

    def __init__(self, frac, maximum_chunk=MAX_QUANTITY):
        """Ensure group configuration is fixed while rows are accumulated."""
        if not 0 <= frac <= 31:
            raise GroupError("fraction width must be 0–31")
        if type(maximum_chunk) is not int or not 1 <= maximum_chunk <= MAX_QUANTITY:
            raise GroupError("maximum aggregate quantity is outside its physical field")
        self.frac = frac
        self.maximum_chunk = maximum_chunk
        self.rows = 0
        self.groups = {}
        self.excluded = {}

    def add(self, payload):
        """Record an order by side and pricing, isolating limit/validation failures."""
        if not isinstance(payload, Order):
            raise TypeError("study expects parsed CSV order objects")
        self.rows += 1
        reply = calculate(payload.request, self.frac)
        if reply.status != Kind.OK:
            prior = self.excluded.get(reply.status.name, 0)
            self.excluded[reply.status.name] = prior + 1
            return
        key = GroupKey.for_order(payload)
        totals = self.groups.setdefault(key, Totals())
        totals.record(payload.request.quantity, reply)

    def result(self, key, baseline):
        """Calculate legal aggregate chunks with the fixed arithmetic configuration."""
        largest = largest_feasible(key, self.frac, self.maximum_chunk)
        if (baseline.quantity + largest - 1) // largest > 100000:
            raise GroupError("aggregate would require more than 100000 hardware requests")
        chunks = tuple(partition(baseline.quantity, largest))
        settled = Totals()
        for quantity in chunks:
            reply = calculate(key.order(quantity), self.frac)
            settled.record(quantity, reply)
        if settled.quantity != baseline.quantity:
            raise RuntimeError("aggregation did not conserve quantity")
        return GroupResult(key, baseline, settled, chunks)

    def inspect(self, sample_chunks=6):
        """Produce stable reports even when the apparent order of CSV symbols changes."""
        if type(sample_chunks) is not int or sample_chunks < 0:
            raise GroupError("chunk example count must be nonnegative")
        sorted_keys = sorted(self.groups, key=lambda key: (
            key.instrument, key.sell, key.price, key.fee_bps, key.rounding,
        ))
        by_key = [self.result(key, self.groups[key]).row(sample_chunks) for key in sorted_keys]
        return {
            "input_rows": self.rows,
            "excluded_by_status": dict(sorted(self.excluded.items())),
            "groups": by_key,
        }


def main():
    """Compare rounded transaction amounts on a synthetic CSV while preserving side."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("orders", type=Path)
    parser.add_argument("--fraction", type=int, default=8)
    parser.add_argument("--max-quantity", type=int, default=MAX_QUANTITY)
    parser.add_argument("--examples", type=int, default=6)
    args = parser.parse_args()
    try:
        study = AggregationStudy(args.fraction, args.max_quantity)
        with args.orders.open(encoding="utf-8", newline="") as file:
            for order in read_orders(file, args.fraction):
                study.add(order)
        print(json.dumps(study.inspect(args.examples), indent=2, sort_keys=True))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
