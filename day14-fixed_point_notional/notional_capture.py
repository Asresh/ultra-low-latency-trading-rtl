"""Replay a hardware request/reply capture against the exact arithmetic model."""

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path

from notional_model import Kind, Reply, Request, calculate, unsigned
from notional_workload import model_latency


class TraceError(ValueError):
    """A malformed or contradictory transaction capture."""


@dataclass(frozen=True)
class RequestEvent:
    cycle: int
    request: Request


@dataclass(frozen=True)
class ReplyEvent:
    cycle: int
    first_visible: int
    reply: Reply


def wire_numbers(tokens):
    """Decode unsigned hexadecimal fields from a simulator trace."""
    if any(not token or token.startswith(("-", "+")) for token in tokens):
        raise TraceError("hardware fields must be unsigned hex")
    try:
        return tuple(int(token, 16) for token in tokens)
    except ValueError as exc:
        raise TraceError("invalid hexadecimal hardware field") from exc


def bit(value, name):
    """Convert only a valid one-bit wire value to a Python boolean."""
    return bool(unsigned(value, 1, name))


def request_event(tokens):
    """Decode the request with a canonical optional limit field."""
    if len(tokens) != 10:
        raise TraceError("request must contain the cycle and eight wire fields")
    cycle = int(tokens[1])
    tag, price, qty, fee, mode, sell, enabled, limit = wire_numbers(tokens[2:])
    limit_enabled = bit(enabled, "limit enable")
    if not limit_enabled and limit:
        raise TraceError("disabled limit must be zero")
    request = Request(tag, price, qty, fee, mode, bit(sell, "sell"),
                      limit if limit_enabled else None)
    return RequestEvent(cycle, request)


def reply_event(tokens):
    """Decode a response with both observed visibility and transfer times."""
    if len(tokens) != 10:
        raise TraceError("response must contain two cycles and seven wire fields")
    cycle, first = (int(token) for token in tokens[1:3])
    tag, status, sell, gross, fee, cash, inexact = wire_numbers(tokens[3:])
    for name, value, width in (
        ("tag", tag, 16), ("gross", gross, 64), ("fee", fee, 64), ("cash", cash, 64),
    ):
        unsigned(value, width, name)
    reply = Reply(tag, Kind(status), bit(sell, "sell"), gross, fee, cash, bit(inexact, "inexact"))
    return ReplyEvent(cycle, first, reply)


def read_events(lines):
    """Stream ordered whole events with line numbers on decoding errors."""
    previous = -1
    for number, line in enumerate(lines, 1):
        tokens = line.split()
        try:
            if not tokens:
                raise TraceError("empty transaction row")
            if tokens[0] == "req":
                event = request_event(tokens)
            elif tokens[0] == "rsp":
                event = reply_event(tokens)
            else:
                raise TraceError("expected a req or rsp record")
            if event.cycle <= previous:
                raise TraceError("hardware capture must advance by at least one cycle")
            if isinstance(event, ReplyEvent) and not 0 <= event.first_visible <= event.cycle:
                raise TraceError("response visibility cycle exceeds transfer cycle")
        except ValueError as exc:
            raise TraceError(f"line {number}: {exc}") from exc
        previous = event.cycle
        yield event


class Audit:
    """Enforce single-outstanding order, exact results, and fixed compute latency."""

    def __init__(self, fraction_bits):
        """When empty, no visible response may enter the recorder."""
        if not 0 <= fraction_bits <= 31:
            raise ValueError("invalid fraction width")
        self.fraction_bits = fraction_bits
        self.pending = None
        self.statuses = Counter()
        self.latencies = Counter()
        self.hold_cycles = 0
        self.transactions = 0
        self.inexact = 0
        self.sells = 0

    def apply(self, event):
        """Handle a request or compare one response with its admitted request."""
        if isinstance(event, RequestEvent):
            if self.pending is not None:
                raise TraceError("request arrived with a transaction already in flight")
            self.pending = event
            return
        if self.pending is None:
            raise TraceError("response has no associated request")
        predicted = calculate(self.pending.request, self.fraction_bits)
        if event.reply != predicted:
            raise TraceError(f"at cycle {event.cycle}: observed {event.reply!r}, expected {predicted!r}")
        latency = event.first_visible - self.pending.cycle
        if latency != model_latency(predicted.status):
            raise TraceError(f"tag {predicted.tag} has unexpected first-visible cycle")
        self.pending = None
        self.hold_cycles += event.cycle - event.first_visible
        self.latencies[latency] += 1
        self.statuses[predicted.status.name] += 1
        self.transactions += 1
        self.inexact += int(predicted.inexact)
        self.sells += int(predicted.sell)

    def finish(self):
        """Reject incomplete traces and report the timing of all replayed requests."""
        if self.pending is not None:
            raise TraceError("input ended before its final response")
        if not self.transactions:
            raise TraceError("input contains no complete transaction")
        return {
            "fraction_bits": self.fraction_bits,
            "transactions": self.transactions,
            "inexact": self.inexact,
            "sell_count": self.sells,
            "output_hold_cycles": self.hold_cycles,
            "first_visible_latency": dict(self.latencies),
            "statuses": dict(self.statuses),
        }


def verify(path, fraction_bits):
    """Audit one capture file with a bounded amount of live transaction state."""
    audit = Audit(fraction_bits)
    with path.open(encoding="ascii") as file:
        for event in read_events(file):
            audit.apply(event)
    return audit.finish()


def main():
    """Check simulator traces in the standard three quantization profiles."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    for frac in (0, 8, 31):
        summary = verify(args.directory / f"q{frac}.trace", frac)
        print(f"PASS captured Q{frac} trace {json.dumps(summary, sort_keys=True)}")


if __name__ == "__main__":
    main()
