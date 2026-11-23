"""Find quantity boundaries for the exact hardware cash-limit rule."""

import argparse
from dataclasses import dataclass, replace
import json

from notional_batch import pack_price, parse_decimal, parse_rounding
from notional_model import Kind, Request, calculate

U32 = (1 << 32) - 1


class LimitError(ValueError):
    """The chosen limit cannot be analyzed for this request shape."""


@dataclass(frozen=True)
class Boundary:
    quantity: int
    cash: int
    gross: int
    fee: int
    code: Kind

    @classmethod
    def from_reply(cls, quantity, reply):
        """Keep enough fields to verify a candidate next to the boundary."""
        return cls(quantity, reply.cash, reply.gross, reply.fee, reply.status)

    def values(self):
        """Serialize a self-contained witness with an explicit status name."""
        return {**vars(self), "code": self.code.name}


@dataclass(frozen=True)
class QuantityPlan:
    side: str
    accepted: Boundary
    adjacent_reject: Boundary
    trials: int
    maximum: int

    def values(self):
        """Include accepted and rejected neighbors for independent replay."""
        boundary = self.accepted.values() if self.accepted else None
        rejected = self.adjacent_reject.values() if self.adjacent_reject else None
        return {
            "side": self.side,
            "accepted": boundary,
            "adjacent_reject": rejected,
            "evaluations": self.trials,
            "maximum": self.maximum,
        }


class CashLimits:
    """Test only actual positive quantities, memoizing binary-search probes."""

    def __init__(self, request, fraction_bits, maximum):
        """Check that the template describes a valid bounded cash constraint."""
        if type(maximum) is not int or not 1 <= maximum <= U32:
            raise LimitError("maximum quantity must be a positive uint32")
        if request.limit is None:
            raise LimitError("limit discovery requires an enabled cash limit")
        probe = calculate(replace(request, quantity=1, limit=None), fraction_bits)
        if probe.status not in (Kind.OK, Kind.OVERFLOW):
            raise LimitError(f"invalid pricing template: {probe.status.name}")
        self.request = request
        self.fraction_bits = fraction_bits
        self.maximum = maximum
        self.evaluated = {}

    def probe(self, quantity):
        """Calculate the exact cash fields for a positive quantity."""
        if quantity not in self.evaluated:
            reply = calculate(replace(self.request, quantity=quantity), self.fraction_bits)
            self.evaluated[quantity] = Boundary.from_reply(quantity, reply)
        return self.evaluated[quantity]

    def accepts(self, quantity):
        """Only a normal, representable, limit-passing reply defines feasibility."""
        return self.probe(quantity).code == Kind.OK

    def buy(self):
        """Find the largest feasible debit using a first-rejected sentinel."""
        accepted = 0
        rejected = self.maximum + 1
        while rejected - accepted > 1:
            middle = (accepted + rejected) // 2
            if self.accepts(middle):
                accepted = middle
            else:
                rejected = middle
        witness = self.probe(accepted) if accepted else None
        adjacent = self.probe(rejected) if rejected <= self.maximum else None
        return QuantityPlan("buy", witness, adjacent, len(self.evaluated), self.maximum)

    def sell(self):
        """Find the minimum quantity that reaches the required proceeds."""
        rejected = 0
        accepted = self.maximum + 1
        while accepted - rejected > 1:
            middle = (accepted + rejected) // 2
            if self.accepts(middle):
                accepted = middle
            else:
                rejected = middle
        witness = self.probe(accepted) if accepted <= self.maximum else None
        adjacent = self.probe(rejected) if rejected else None
        return QuantityPlan("sell", witness, adjacent, len(self.evaluated), self.maximum)


def quantity_plan(request, fraction_bits, maximum):
    """Find the side-specific boundary and verify the nearest failing order."""
    limits = CashLimits(request, fraction_bits, maximum)
    return limits.sell() if request.sell else limits.buy()


def recheck(plan, request, fraction_bits):
    """Replay the boundary witnesses and check that the neighboring order fails."""
    if plan.accepted is not None:
        tested = calculate(replace(request, quantity=plan.accepted.quantity), fraction_bits)
        if Boundary.from_reply(plan.accepted.quantity, tested) != plan.accepted or tested.status != Kind.OK:
            raise LimitError("accepted witness does not match a feasible RTL request")
    if plan.adjacent_reject is not None:
        tested = calculate(replace(request, quantity=plan.adjacent_reject.quantity), fraction_bits)
        if (Boundary.from_reply(plan.adjacent_reject.quantity, tested) != plan.adjacent_reject or
                tested.status == Kind.OK):
            raise LimitError("adjacent witness does not match a failed RTL request")
    if plan.adjacent_reject and plan.accepted:
        direction = -1 if request.sell else 1
        if plan.adjacent_reject.quantity != plan.accepted.quantity + direction:
            raise LimitError("reported witnesses are not adjacent")


def main():
    """Print a small reproducible what-if exploration of a synthetic cash limit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("side", choices=("buy", "sell"))
    parser.add_argument("price")
    parser.add_argument("limit", type=int)
    parser.add_argument("--fraction", type=int, default=8)
    parser.add_argument("--max-qty", type=int, default=10000)
    parser.add_argument("--fee-bps", type=int, default=0)
    parser.add_argument("--rounding", choices=("floor", "ceil", "nearest_even"), default="nearest_even")
    args = parser.parse_args()
    try:
        price = pack_price(parse_decimal(args.price), args.fraction)
        request = Request(0, price, 1, args.fee_bps, parse_rounding(args.rounding),
                          args.side == "sell", args.limit)
        result = quantity_plan(request, args.fraction, args.max_qty)
        recheck(result, request, args.fraction)
        print(json.dumps(result.values(), indent=2))
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
