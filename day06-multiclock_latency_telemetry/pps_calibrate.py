"""Estimate the reference clock period from independent one-pulse-per-second observations."""

import argparse
from bisect import bisect_left
import csv
from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path
import sys

from latency_reference import HALF64, LIMIT64, modular_delta

NANOSECONDS_PER_SECOND = 1_000_000_000
Q32 = 1 << 32
PPM = 1_000_000


class CalibrationError(ValueError):
    """Insufficient or physically inconsistent PPS observations."""


def exact_decimal(token):
    """Parse a base-10 input without first introducing binary floating-point error."""
    try:
        return Fraction(token)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError(f"invalid number {token!r}") from exc


def median(values):
    """Compute the exact central value of a finite sample window."""
    sorted_values = sorted(values)
    if not sorted_values:
        raise CalibrationError("cannot estimate an empty population")
    midpoint = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[midpoint]
    return (sorted_values[midpoint - 1] + sorted_values[midpoint]) / 2


def nearest_fixed(value, scale):
    """Convert a rational value to the requested fixed-point scale."""
    scaled = value * scale
    quotient, remainder = divmod(scaled.numerator, scaled.denominator)
    # Round to nearest, ties to even.
    if (2 * remainder > scaled.denominator or
            (2 * remainder == scaled.denominator and quotient % 2)):
        quotient += 1
    return quotient


def unsigned_integer(raw, label):
    """Parse a nonnegative integer with a label for diagnostics."""
    try:
        if raw.startswith(("0x", "0X")):
            result = int(raw, 16)
        else:
            result = int(raw, 10)
    except (AttributeError, ValueError) as exc:
        raise CalibrationError(f"{label} is an unsigned integer") from exc
    if not 0 <= result <= LIMIT64:
        raise CalibrationError(f"{label} does not fit 64 bits")
    return result


@dataclass(frozen=True)
class PPS:
    second: int
    tick: int
    reference_ns: int


@dataclass(frozen=True)
class Window:
    first: int
    last: int
    ticks: int
    nanoseconds: int

    @property
    def period(self):
        """Return raw elapsed ticks per second for the selected PPS interval."""
        return Fraction(self.nanoseconds, self.ticks)


@dataclass(frozen=True)
class Estimate:
    period: Fraction
    phase_at_anchor: Fraction
    anchor_second: int
    anchor_tick: int
    used_windows: int
    excluded_windows: int
    used_second_span: int
    window_mad_ns: Fraction
    max_residual_ns: Fraction
    nominal: Fraction

    @property
    def rate_word(self):
        """Quantize the estimated period to the hardware register representation."""
        fixed = nearest_fixed(self.period, Q32)
        if not 0 < fixed < 1 << 64:
            raise CalibrationError("computed period outside the unsigned 32.32 register range")
        return fixed

    @property
    def rounded_period(self):
        """Convert the programmed fixed-point period back to engineering units."""
        return Fraction(self.rate_word, Q32)

    @property
    def rate_ppm(self):
        # Sign describes raw oscillator frequency, rather than period.
        """Express the measured rate error relative to the nominal clock in parts per million."""
        return (self.nominal / self.period - 1) * PPM

    def as_json(self, armed):
        """Expose the estimate and register settings in a serializable object."""
        bits = self.rate_word
        return {
            "period_ns": float(self.period),
            "quantized_ns": float(self.rounded_period),
            "nominal_period_ns": float(self.nominal),
            "raw_frequency_error_ppm": float(self.rate_ppm),
            "anchor_tick": self.anchor_tick,
            "anchor_second": self.anchor_second,
            "external_ns_at_anchor": float(self.phase_at_anchor),
            "accepted_windows": self.used_windows,
            "rejected_windows": self.excluded_windows,
            "accepted_span_seconds": self.used_second_span,
            "window_mad_ns_per_tick": float(self.window_mad_ns),
            "max_phase_residual_ns": float(self.max_residual_ns),
            "writes": [
                {"offset": "0x010", "value": f"0x{bits >> 32:08x}", "wstrb": "0xf"},
                {"offset": "0x014", "value": f"0x{bits & 0xffffffff:08x}", "wstrb": "0xf"},
                {"offset": "0x008", "value": "0x00000011" if armed else "0x00000010", "wstrb": "0x1"},
            ],
        }


def read_pps(rows):
    """Read raw PPS records and validate the integer fields in each row."""
    reader = csv.DictReader(rows)
    fields = set(reader.fieldnames or ())
    if not {"second", "tick"}.issubset(fields):
        raise CalibrationError("expected CSV columns 'second' and 'tick'")
    for row_number, row in enumerate(reader, 2):
        try:
            second = unsigned_integer(row.get("second"), "second")
            tick = unsigned_integer(row.get("tick"), "tick")
            explicit = row.get("reference_ns")
            if explicit:
                reference = unsigned_integer(explicit, "reference_ns")
            else:
                reference = second * NANOSECONDS_PER_SECOND
                if reference > LIMIT64:
                    raise CalibrationError("second count exceeds nanosecond range")
        except CalibrationError as exc:
            raise CalibrationError(f"row {row_number}: {exc}") from exc
        yield PPS(second, tick, reference)


def verify_points(points):
    """Check PPS samples are ordered and unambiguous across counter wrap."""
    if len(points) < 3:
        raise CalibrationError("at least three PPS points are needed")
    for previous, current in zip(points, points[1:]):
        if current.second <= previous.second:
            raise CalibrationError("PPS second numbers must be strictly increasing")
        if current.reference_ns <= previous.reference_ns:
            raise CalibrationError("reference nanoseconds must be strictly increasing")
        tick_delta = modular_delta(current.tick, previous.tick)
        if not 0 < tick_delta < HALF64:
            raise CalibrationError("positive reference tick progress below half the counter range is required")
    if modular_delta(points[-1].tick, points[0].tick) >= HALF64:
        raise CalibrationError("capture covers ambiguous tick span")


def interval_windows(points, min_seconds, max_seconds):
    """Build candidate intervals within the requested time-window bounds."""
    seconds = [point.second for point in points]
    for start, a in enumerate(points):
        end = bisect_left(seconds, a.second + min_seconds, lo=start + 1)
        if end == len(points):
            break
        b = points[end]
        if b.second - a.second > max_seconds:
            continue
        ticks = modular_delta(b.tick, a.tick)
        if 0 < ticks < HALF64:
            yield Window(start, end, ticks, b.reference_ns - a.reference_ns)


def estimate_period(
    points,
    nominal=Fraction(8),
    min_seconds=2,
    max_seconds=16,
    min_windows=3,
    jitter_ticks=3,
    ppm_limit=Fraction(10000),
):
    """Fit a period from consistent PPS windows subject to jitter and rate limits."""
    if nominal <= 0 or not 1 <= min_seconds <= max_seconds:
        raise CalibrationError("period and window bounds must be positive")
    if min_windows < 2 or jitter_ticks < 0 or ppm_limit <= 0:
        raise CalibrationError("invalid window, jitter or drift bound")
    points = tuple(points)
    verify_points(points)
    windows = list(interval_windows(points, min_seconds, max_seconds))
    if len(windows) < min_windows:
        raise CalibrationError("not enough windows within the requested time bounds")
    centre = median([window.period for window in windows])
    mad = median([abs(window.period - centre) for window in windows])
    inliers = [
        window for window in windows if abs(window.period - centre) <= max(
            6 * mad,
            centre * Fraction(jitter_ticks, window.ticks),
        )
    ]
    if len(inliers) < min_windows or 2 * len(inliers) <= len(windows):
        raise CalibrationError("PPS windows lack a majority of consistent readings")
    period = median([window.period for window in inliers])
    if abs(nominal / period - 1) * PPM > ppm_limit:
        raise CalibrationError(f"computed oscillator drift exceeds {float(ppm_limit):.2f} ppm")
    # Phase is computed offline. The FPGA only consumes the period and never
    # jumps its live time accumulator to this external anchor.
    used_indices = sorted({index for w in inliers for index in (w.first, w.last)})
    anchor = points[used_indices[0]]
    intercept = [
        Fraction(points[index].reference_ns) -
        modular_delta(points[index].tick, anchor.tick) * period
        for index in used_indices
    ]
    phase = median(intercept)
    span = points[used_indices[-1]].second - anchor.second
    return Estimate(
        period, phase, anchor.second, anchor.tick, len(inliers), len(windows) - len(inliers),
        span, mad, max(abs(offset - phase) for offset in intercept), nominal,
    )


def main():
    """Estimate a register period from PPS data and quality constraints."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--nominal-ns", type=exact_decimal, default=Fraction(8))
    parser.add_argument("--min-seconds", type=int, default=2)
    parser.add_argument("--max-seconds", type=int, default=16)
    parser.add_argument("--min-windows", type=int, default=3)
    parser.add_argument("--jitter-ticks", type=int, default=3)
    parser.add_argument("--max-drift-ppm", type=exact_decimal, default=Fraction(10000))
    parser.add_argument("--keep-armed", action="store_true")
    args = parser.parse_args()
    try:
        with args.capture.open(encoding="ascii", newline="") as capture:
            data = tuple(read_pps(capture))
        estimate = estimate_period(
            data, args.nominal_ns, args.min_seconds, args.max_seconds,
            args.min_windows, args.jitter_ticks, args.max_drift_ppm,
        )
        print(json.dumps(estimate.as_json(args.keep_armed), indent=2))
    except (CalibrationError, OSError, csv.Error, UnicodeError) as exc:
        print(f"pps_calibrate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
