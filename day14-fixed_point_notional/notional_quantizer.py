"""Choose a price fraction width using an exact error budget on a synthetic grid."""

import argparse
from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path

from notional_batch import parse_decimal, parse_rounding
from notional_model import Rounding, rounded_divide
from notional_study import fraction_text

MAX_RAW = (1 << 32) - 1
MAX_PRICES = 50000


class GridError(ValueError):
    """No fixed-point grid can preserve the configured inputs or budget."""


@dataclass(frozen=True)
class Point:
    source: Fraction
    raw: int
    represented: Fraction
    error: Fraction

    def values(self):
        """Report the original and represented price without float approximation."""
        return {
            "input": fraction_text(self.source),
            "raw": self.raw,
            "represented": fraction_text(self.represented),
            "signed_error": fraction_text(self.error),
        }


@dataclass(frozen=True)
class Candidate:
    fraction_bits: int
    overflow: int
    zero_codes: int
    exact_codes: int
    collisions: int
    distinct_levels: int
    maximum_error: Fraction
    mean_error: Fraction
    worst: Point

    def evaluate(self, allowed_error, preserve_levels):
        """Accept a layout only when every distinct positive price remains usable."""
        return (self.overflow == 0 and self.zero_codes == 0 and self.maximum_error <= allowed_error
                and (not preserve_levels or self.collisions == 0))

    def values(self):
        """Return a compact report for one fixed-point format."""
        return {
            "fraction_bits": self.fraction_bits,
            "integer_bits": 32 - self.fraction_bits,
            "positive_levels": self.distinct_levels,
            "rounded_to_zero": self.zero_codes,
            "out_of_range": self.overflow,
            "exact": self.exact_codes,
            "collapsed_levels": self.collisions,
            "maximum_absolute_error": fraction_text(self.maximum_error),
            "mean_absolute_error": fraction_text(self.mean_error),
            "largest_error_example": self.worst.values() if self.worst else None,
        }


def point(value, fraction_bits, mode):
    """Round one rational price on the candidate's binary grid."""
    scale = 1 << fraction_bits
    code = rounded_divide(value.numerator * scale, value.denominator, mode).amount
    represented = Fraction(code, scale)
    return Point(value, code, represented, represented - value)


def build_candidate(prices, fraction_bits, mode):
    """Scan distinct prices for range, collisions, and exact quantization error."""
    if not 0 <= fraction_bits <= 31:
        raise GridError("fraction width must be between 0 and 31")
    raw_codes = set()
    overflow = 0
    zeros = 0
    exact = 0
    count = 0
    largest = Fraction(0)
    total_error = Fraction(0)
    witness = None
    for price in sorted(set(prices)):
        if price <= 0:
            raise GridError("price samples must be strictly positive")
        quantized = point(price, fraction_bits, mode)
        if quantized.raw > MAX_RAW:
            overflow += 1
        elif quantized.raw == 0:
            zeros += 1
        else:
            raw_codes.add(quantized.raw)
        absolute = abs(quantized.error)
        if absolute > largest or witness is None:
            largest = absolute
            witness = quantized
        exact += int(absolute == 0)
        total_error += absolute
        count += 1
    if not count:
        raise GridError("at least one candidate price is required")
    collisions = count - overflow - zeros - len(raw_codes)
    return Candidate(fraction_bits, overflow, zeros, exact, collisions, count,
                     largest, total_error / count, witness)


@dataclass(frozen=True)
class Grid:
    budget: Fraction
    mode: Rounding
    preserve_levels: bool
    candidates: tuple
    selected: Candidate

    def values(self, include_all=False):
        """Report the chosen grid and nearby failures with their independent evidence."""
        lowest = self.selected.fraction_bits if self.selected is not None else None
        options = self.candidates if include_all or lowest is None else tuple(
            decision for decision in self.candidates if abs(decision.fraction_bits - lowest) <= 2
        )
        return {
            "absolute_error_budget": fraction_text(self.budget),
            "rounding": self.mode.name,
            "preserve_distinct_prices": self.preserve_levels,
            "selected_fraction_bits": lowest,
            "selected": self.selected.values() if self.selected is not None else None,
            "alternatives": [
                {"eligible": item.evaluate(self.budget, self.preserve_levels), **item.values()}
                for item in options
            ],
        }


def choose(prices, budget, mode, *, preserve_levels=True, floor=0, ceiling=31):
    """Choose the fewest fractional bits among all representable candidates."""
    if not 0 <= floor <= ceiling <= 31:
        raise GridError("fraction bounds must satisfy 0 <= minimum <= maximum <= 31")
    if type(preserve_levels) is not bool:
        raise GridError("preserve_levels must be a boolean")
    if budget < 0:
        raise GridError("absolute error budget must be nonnegative")
    mode = Rounding(mode)
    distinct = set(prices)
    if not distinct or len(distinct) > MAX_PRICES:
        raise GridError("price-grid workload is empty or beyond its record limit")
    candidates = tuple(build_candidate(distinct, width, mode) for width in range(floor, ceiling+1))
    best = next((entry for entry in candidates if entry.evaluate(budget, preserve_levels)), None)
    return Grid(budget, mode, preserve_levels, candidates, best)


def read_prices(source):
    """Read one exact positive decimal value per nonempty line with original row labels."""
    seen = set()
    for index, line in enumerate(source, 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            value = parse_decimal(text)
            if value <= 0:
                raise GridError("price must be strictly positive")
            if value not in seen:
                seen.add(value)
                if len(seen) > MAX_PRICES:
                    raise GridError("price list exceeds distinct record limit")
        except ValueError as exc:
            raise GridError(f"line {index}: {exc}") from exc
    return tuple(sorted(seen))


def main():
    """Run a deterministic fixed-point grid study for a small decimal-price fixture."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--absolute-error", default="0")
    parser.add_argument("--rounding", choices=("floor", "ceil", "nearest_even"), default="nearest_even")
    parser.add_argument("--minimum-bits", type=int, default=0)
    parser.add_argument("--maximum-bits", type=int, default=31)
    parser.add_argument("--allow-collisions", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    try:
        with args.source.open(encoding="utf-8") as file:
            prices = read_prices(file)
        result = choose(prices, parse_decimal(args.absolute_error), parse_rounding(args.rounding),
                        preserve_levels=not args.allow_collisions, floor=args.minimum_bits,
                        ceiling=args.maximum_bits)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result.values(args.all), indent=2, sort_keys=True))
    if result.selected is None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
