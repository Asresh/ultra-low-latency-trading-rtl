"""Measure how rounding choices change integer amounts on synthetic order tapes."""

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from fractions import Fraction
import json
from pathlib import Path
from statistics import median

from notional_batch import read_orders
from notional_model import FEE_SCALE, Kind, Rounding, calculate, request_fault


def exact_amounts(req, fraction_bits):
    """Compute unquantized price times quantity and its fee using rational numbers."""
    gross = Fraction(req.price * req.quantity, 1 << fraction_bits)
    fee = gross * Fraction(req.fee_bps, FEE_SCALE)
    cash = gross - fee if req.sell else gross + fee
    return gross, fee, cash


def fraction_text(number):
    """Represent a rational amount without losing precision through a float."""
    if isinstance(number, int) or number.denominator == 1:
        return str(number)
    return f"{number.numerator}/{number.denominator}"


def describe(series):
    """Summarize a finite set of signed rational errors exactly."""
    return {
        "samples": len(series),
        "min": fraction_text(min(series, default=Fraction())),
        "max": fraction_text(max(series, default=Fraction())),
        "median": fraction_text(median(series) if series else Fraction()),
        "mean": fraction_text(sum(series, Fraction()) / len(series) if series else Fraction()),
        "zero_error": sum(value == 0 for value in series),
    }


@dataclass(frozen=True)
class Changed:
    position: int
    instrument: str
    tag: int
    excludes: tuple
    includes: tuple
    floor_cash: int
    ceil_cash: int
    even_cash: int

    def row(self):
        """Record the policy decisions as readable names in the generated report."""
        return {**vars(self), "excludes": list(self.excludes), "includes": list(self.includes)}


class RoundingStudy:
    """Accumulate exact fees, debit error, and policy sensitivity by rounding mode."""

    def __init__(self, fraction_bits):
        """Store enough data to calculate median error across each tested mode."""
        if not 0 <= fraction_bits <= 31:
            raise ValueError("invalid price fraction width")
        self.fraction_bits = fraction_bits
        self.orders = 0
        self.invalid = Counter()
        self.changes = []
        self.mode_status = {mode: Counter() for mode in Rounding}
        self.errors = {
            mode: {field: [] for field in ("gross", "fee", "cash")}
            for mode in Rounding
        }
        self.saturated = {mode: 0 for mode in Rounding}
        self.rounding_ranges = Counter()

    def check_order(self, instrument, req):
        """Compare all finite rounding variants with the unquantized rational value."""
        self.orders += 1
        fault = request_fault(req)
        if fault != Kind.OK:
            self.invalid[fault.name] += 1
            return
        ideal = exact_amounts(req, self.fraction_bits)
        variants = {}
        for mode in Rounding:
            calibrated = replace(req, rounding=mode)
            out = calculate(calibrated, self.fraction_bits)
            variants[mode] = out
            self.mode_status[mode][out.status.name] += 1
            if out.status == Kind.OVERFLOW:
                self.saturated[mode] += 1
                continue
            for field, decimal in zip(("gross", "fee", "cash"), ideal):
                self.errors[mode][field].append(getattr(out, field) - decimal)
        self.inspect_variants(instrument, req, variants)

    def inspect_variants(self, instrument, req, variants):
        """Record the value spread and orders whose decision depends on rounding."""
        cash_values = [reply.cash for reply in variants.values() if reply.status != Kind.OVERFLOW]
        if cash_values:
            width = max(cash_values) - min(cash_values)
            self.rounding_ranges[width] += 1
        passes = tuple(mode.name for mode, reply in variants.items() if reply.status == Kind.OK)
        failures = tuple(mode.name for mode, reply in variants.items() if reply.status != Kind.OK)
        if passes and failures:
            self.changes.append(Changed(
                self.orders,
                instrument,
                req.tag,
                failures,
                passes,
                variants[Rounding.FLOOR].cash,
                variants[Rounding.CEIL].cash,
                variants[Rounding.NEAREST_EVEN].cash,
            ))

    def summary(self, change_limit):
        """Keep change details bounded while retaining full counts and ranges."""
        if change_limit < 0:
            raise ValueError("change limit cannot be negative")
        return {
            "rows": self.orders,
            "invalid_fields": dict(self.invalid),
            "rounding_sensitive_count": len(self.changes),
            "rounding_sensitive_examples": [item.row() for item in self.changes[:change_limit]],
            "cash_spread": dict(self.rounding_ranges),
            "modes": {
                mode.name: {
                    "outcomes": dict(self.mode_status[mode]),
                    "saturated_results": self.saturated[mode],
                    "error": {field: describe(values) for field, values in self.errors[mode].items()},
                }
                for mode in Rounding
            },
        }


def main():
    """Summarize a synthetic CSV workload using exact values and independent rounding policies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("orders", type=Path)
    parser.add_argument("--fraction", type=int, default=8)
    parser.add_argument("--examples", type=int, default=10)
    args = parser.parse_args()
    try:
        study = RoundingStudy(args.fraction)
        with args.orders.open(encoding="utf-8", newline="") as source:
            for order in read_orders(source, args.fraction):
                study.check_order(order.instrument, order.request)
        print(json.dumps(study.summary(args.examples), indent=2))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
