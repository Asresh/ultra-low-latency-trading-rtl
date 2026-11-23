"""Convert raw probe intervals through a recorded, piecewise constant 32.32 clock."""

import argparse
from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path
import sys

from latency_capture import CaptureError, event_json, read_event, read_words
from latency_reference import HALF64, LIMIT64, Status, modular_delta
from pps_calibrate import Q32, nearest_fixed


class RateError(ValueError):
    """Invalid or incomplete rate-epoch input."""


def integer_field(row, key, limit):
    """Read a named raw field and check its unsigned range."""
    if key not in row:
        raise RateError(f"missing epoch field {key}")
    value = row[key]
    if type(value) is int:
        number = value
    elif type(value) is str:
        try:
            number = int(value, 0)
        except ValueError as exc:
            raise RateError(f"{key} is not an integer") from exc
    else:
        raise RateError(f"{key} is not an integer")
    if not 0 <= number <= limit:
        raise RateError(f"{key} must be between zero and {limit}")
    return number


@dataclass(frozen=True)
class RateEpoch:
    tick: int
    integer_ns: int
    fraction_ns: int

    @property
    def word(self):
        """Return the fixed-width word corresponding to this value or memory address."""
        return (self.integer_ns << 32) | self.fraction_ns

    @classmethod
    def from_mapping(cls, row):
        """Construct a validated typed record from external field names and values."""
        if not isinstance(row, dict) or set(row) != {"tick", "integer_ns", "fraction_ns"}:
            raise RateError("each epoch requires tick, integer_ns and fraction_ns fields")
        return cls(
            integer_field(row, "tick", LIMIT64),
            integer_field(row, "integer_ns", Q32 - 1),
            integer_field(row, "fraction_ns", Q32 - 1),
        )


class RateMap:
    def __init__(self, epochs):
        """Validate and retain the timestamped clock-rate epochs."""
        self.epochs = tuple(epochs)
        if not self.epochs:
            raise RateError("at least one recorded rate epoch is needed")
        self.base = self.epochs[0].tick
        self.offsets = []
        self.integral = []
        for i, epoch in enumerate(self.epochs):
            if not 0 <= epoch.tick <= LIMIT64 or not 0 < epoch.word <= LIMIT64:
                raise RateError("epoch does not fit the 64-bit raw tick and period domains")
            offset = modular_delta(epoch.tick, self.base)
            if offset >= HALF64 or (self.offsets and offset <= self.offsets[-1]):
                raise RateError("epochs must be strictly increasing within half a tick revolution")
            if self.offsets:
                prior_rate = self.epochs[i - 1].word
                partial = self.integral[-1] + (offset - self.offsets[-1]) * prior_rate
            else:
                partial = 0
            self.offsets.append(offset)
            self.integral.append(partial)

    @classmethod
    def parse(cls, raw):
        """Load external clock epochs and validate their sequence and geometry."""
        if not isinstance(raw, list):
            raise RateError("expected a JSON list of clock epochs")
        return cls(RateEpoch.from_mapping(item) for item in raw)

    def integrated(self, tick):
        """Convert cumulative modular ticks to exact elapsed time across the rate epochs."""
        offset = modular_delta(tick, self.base)
        if offset >= HALF64:
            raise RateError("tick precedes the earliest supplied epoch or has ambiguous age")
        epoch = bisect_right(self.offsets, offset) - 1
        return self.integral[epoch] + (offset - self.offsets[epoch]) * self.epochs[epoch].word

    def elapsed(self, start, stop):
        """Measure elapsed time between two timestamps across any intervening rate changes."""
        forward = modular_delta(stop, start)
        if forward >= HALF64:
            raise RateError("latency interval is negative or ambiguous")
        ticks_fraction = self.integrated(stop) - self.integrated(start)
        if ticks_fraction < 0:
            raise RateError("interval straddles the origin beyond the recorded epoch range")
        return Fraction(ticks_fraction, Q32)


def render(event, clock):
    """Produce the decoded event with time units available for display."""
    data = event_json(event)
    if event.status == Status.MATCH:
        interval = clock.elapsed(event.start_tick, event.end_tick)
        data.update({
            "duration_ns": float(interval),
            "duration_ns_rounded": nearest_fixed(interval, 1),
            "duration_q32": interval.numerator * Q32 // interval.denominator,
        })
    else:
        data.update({"duration_ns": None, "duration_ns_rounded": None, "duration_q32": None})
    return data


def main():
    """Decode timestamped observations using the supplied clock-rate map."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epochs", type=Path)
    parser.add_argument("events", type=Path)
    parser.add_argument("--binary", action="store_true")
    args = parser.parse_args()
    encoding = {} if args.binary else {"encoding": "ascii"}
    try:
        clock = RateMap.parse(json.loads(args.epochs.read_text(encoding="utf-8")))
        with args.events.open("rb" if args.binary else "r", **encoding) as source:
            for word in read_words(source, 320, args.binary):
                converted = render(read_event(word), clock)
                print(json.dumps(converted, separators=(",", ":")))
    except (RateError, CaptureError, OSError, ValueError) as exc:
        print(f"latency_units: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
