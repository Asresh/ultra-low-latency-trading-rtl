"""Reconcile accepted hardware notional transactions with an ordered CSV or NOM1 plan."""

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from fractions import Fraction
import csv
from itertools import islice
import json
from math import ceil, floor
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from typing import Optional

from notional_batch import ensure_distinct, parse_rounding, read_orders
from notional_capture import ReplyEvent, RequestEvent, read_events
from notional_model import Kind, Reply, Request, Rounding, U64
from notional_wire import MAX_RECORDS, Reader, check_size
from notional_workload import model_latency

MISSING_INSTRUMENT = "<unlabeled>"
UNMATCHED_INSTRUMENT = "<unmatched>"
VALIDATION_ERRORS = frozenset((Kind.BAD_QUANTITY, Kind.BAD_PRICE, Kind.BAD_FEE, Kind.BAD_MODE))


class ReconcileError(ValueError):
    """Plan or trace framing is incompatible with a trustworthy reconciliation."""


def integer(value, label, low=0, high=U64):
    """Validate a bounded natural number used for indexing or measurement."""
    if type(value) is not int or not low <= value <= high:
        raise ReconcileError(f"{label} must be within {low}–{high}")
    return value


def rounding_result(amount, mode):
    """Round an exact Fraction using a separate host arithmetic implementation."""
    if mode == Rounding.FLOOR:
        return floor(amount)
    if mode == Rounding.CEIL:
        return ceil(amount)
    if mode == Rounding.NEAREST_EVEN:
        return round(amount)
    raise ReconcileError(f"unsupported rounding mode {mode}")


@dataclass(frozen=True)
class Mathematics:
    """Independent result and the exact unrounded rational values at both stages."""

    reply: Reply
    exact_gross: Optional[Fraction] = None
    exact_fee: Optional[Fraction] = None

    @property
    def gross_error(self):
        """Return the gross's signed rounding difference in emitted integer units."""
        return self.reply.gross - self.exact_gross if self.exact_gross is not None else Fraction(0)

    @property
    def fee_error(self):
        """Return the fee's signed rounding difference after rounded gross is fixed."""
        return self.reply.fee - self.exact_fee if self.exact_fee is not None else Fraction(0)


def mathematical_reply(request, fraction_bits):
    """Calculate validation precedence and both rounding stages with exact rationals."""
    integer(fraction_bits, "fraction width", 0, 31)
    if request.quantity == 0:
        status = Kind.BAD_QUANTITY
    elif request.price == 0:
        status = Kind.BAD_PRICE
    elif request.fee_bps > 10000:
        status = Kind.BAD_FEE
    elif request.rounding == 3:
        status = Kind.BAD_MODE
    else:
        status = Kind.OK
    if status in VALIDATION_ERRORS:
        return Mathematics(Reply(request.tag, status, request.sell))
    raw_gross = Fraction(request.price * request.quantity, 2**fraction_bits)
    gross = rounding_result(raw_gross, request.rounding)
    raw_fee = Fraction(gross * request.fee_bps, 10000)
    fee = rounding_result(raw_fee, request.rounding)
    amount = gross - fee if request.sell else gross + fee
    if amount > U64:
        status = Kind.OVERFLOW
    elif request.limit is not None and (
        amount < request.limit if request.sell else amount > request.limit
    ):
        status = Kind.LIMIT
    reply = Reply(request.tag, status, request.sell, gross, fee, min(amount, U64),
                  raw_gross.denominator != 1 or raw_fee.denominator != 1)
    return Mathematics(reply, raw_gross, raw_fee)


@dataclass(frozen=True)
class Planned:
    """One planned order identified by its batch ordinal, even when a tag repeats."""

    sequence: int
    instrument: str
    request: Request
    calculated: Mathematics

    @property
    def reply(self):
        """Return the independently computed wire fields for this planned request."""
        return self.calculated.reply


@dataclass(frozen=True)
class Plan:
    """Fixed-point format and absolute, consecutive sequence of expected requests."""

    fraction_bits: int
    entries: tuple
    source: str

    def __post_init__(self):
        """Reject invalid geometry and accidental renumbering across a planned slice."""
        integer(self.fraction_bits, "fraction width", 0, 31)
        if not self.entries:
            raise ReconcileError("a reconciliation plan needs at least one order")
        first = self.entries[0].sequence
        for index, entry in enumerate(self.entries, first):
            if entry.sequence != index:
                raise ReconcileError("planned rows must have consecutive order sequences")

    def selected(self, start=None, count=None):
        """Select absolute ordinals so selecting again never renumbers a plan window."""
        first = self.entries[0].sequence
        last = self.entries[-1].sequence
        start = first if start is None else integer(start, "sequence start", first, last)
        available = last+1-start
        count = available if count is None else integer(count, "sequence count", 1, available)
        begin = start-first
        return Plan(self.fraction_bits, self.entries[begin:begin+count], self.source)


def csv_plan(source, fraction_bits, quantize=None):
    """Read human-readable orders and compute independent expected reply fields."""
    integer(fraction_bits, "CSV fraction width", 0, 31)
    entries = []
    for index, order in enumerate(islice(read_orders(source, fraction_bits, quantize), MAX_RECORDS+1)):
        if index == MAX_RECORDS:
            raise ReconcileError(f"CSV plan exceeds {MAX_RECORDS} rows")
        entries.append(Planned(index, order.instrument, order.request,
                               mathematical_reply(order.request, fraction_bits)))
    return Plan(fraction_bits, tuple(entries), "CSV")


def image_plan(source, expected_fraction=None):
    """Check binary framing, CRCs, sequence, and Maths before trusting any row."""
    image = Reader(source)
    fraction = image.header.fraction_bits
    if expected_fraction is not None and expected_fraction != fraction:
        raise ReconcileError(f"NOM1 has Q{fraction}, expected Q{expected_fraction}")
    normalized = []
    for row in image.records(check_math=False):
        predicted = mathematical_reply(row.request, fraction)
        if row.reply != predicted.reply:
            raise ReconcileError(f"NOM1 record {row.sequence}: stored reply disagrees with exact arithmetic")
        normalized.append(Planned(row.sequence, MISSING_INSTRUMENT, row.request, predicted))
    return Plan(fraction, tuple(normalized), "NOM1")


def load_plan(path, fraction_bits=None, quantize=None):
    """Choose the file format by suffix and validate applicable CLI options."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        fraction = 8 if fraction_bits is None else fraction_bits
        with path.open(encoding="utf-8", newline="") as source:
            return csv_plan(source, fraction, quantize)
    if path.suffix.lower() == ".nom1":
        if quantize is not None:
            raise ReconcileError("NOM1 prices are already quantized")
        check_size(path)
        with path.open("rb") as source:
            return image_plan(source, fraction_bits)
    raise ReconcileError("planned input must end with .csv or .nom1")


@dataclass(frozen=True)
class CaptureSegment:
    """A trace fragment sharing the same physical clock after applying an offset."""

    path: Path
    offset: int = 0

    def __post_init__(self):
        """Ensure the fragment starts at a nonnegative bounded offset."""
        integer(self.offset, "segment offset")
        object.__setattr__(self, "path", Path(self.path))

    def record(self):
        """Describe adjusted capture provenance in the output report."""
        return {"path": str(self.path), "cycle_offset": self.offset}


def shifted(event, offset):
    """Shift admission, visibility, and acceptance by the same physical clock offset."""
    cycle = integer(event.cycle+offset, "adjusted cycle")
    if isinstance(event, RequestEvent):
        return RequestEvent(cycle, event.request)
    if isinstance(event, ReplyEvent):
        return ReplyEvent(cycle, integer(event.first_visible+offset, "adjusted visibility"), event.reply)
    raise ReconcileError("unknown hardware event type")


def captured_events(segments):
    """Merge file fragments in caller order with strictly increasing adjusted cycles."""
    prior = -1
    for segment in segments:
        try:
            with segment.path.open(encoding="ascii") as source:
                for raw in read_events(source):
                    event = shifted(raw, segment.offset)
                    if event.cycle <= prior:
                        raise ReconcileError(f"adjusted cycle {event.cycle} does not follow {prior}")
                    prior = event.cycle
                    yield event
        except (OSError, ValueError) as exc:
            raise ReconcileError(f"{segment.path}: {exc}") from exc


def difference(expected, actual):
    """Identify every differing typed field, keeping enum values machine-readable."""
    changes = {}
    for column in fields(expected):
        want = getattr(expected, column.name)
        got = getattr(actual, column.name)
        if want != got:
            changes[column.name] = {
                "expected": want.name if isinstance(want, Kind) else want,
                "observed": got.name if isinstance(got, Kind) else got,
            }
    return changes


@dataclass(frozen=True)
class Diagnostic:
    """Recoverable mismatch or incomplete observation with record and edge context."""

    code: str
    description: str
    sequence: Optional[int] = None
    cycle: Optional[int] = None
    fields: dict = field(default_factory=dict)

    def record(self):
        """Expose diagnostic values in a stable field layout."""
        return {
            "code": self.code,
            "sequence": self.sequence,
            "cycle": self.cycle,
            "description": self.description,
            "fields": self.fields,
        }


@dataclass
class FlowTotals:
    """Cash and quoted amounts accumulated only for observed Kind.OK replies."""

    transactions: int = 0
    ok: int = 0
    rejected: int = 0
    inexact: int = 0
    gross: int = 0
    fees: int = 0
    debits: int = 0
    proceeds: int = 0

    def accept(self, reply):
        """Count response statuses and add acknowledged buy or sell cash."""
        self.transactions += 1
        self.inexact += int(reply.inexact)
        if reply.status != Kind.OK:
            self.rejected += 1
            return
        self.ok += 1
        self.gross += reply.gross
        self.fees += reply.fee
        if reply.sell:
            self.proceeds += reply.cash
        else:
            self.debits += reply.cash

    def record(self):
        """Return exact unbounded totals and signed net credited cash."""
        result = asdict(self)
        result["net"] = self.proceeds - self.debits
        return result


def totals_delta(observed, planned):
    """Calculate signed observed-minus-planned differences for each independent measure."""
    now = observed.record()
    want = planned.record()
    return {field: now[field]-want[field] for field in now}


@dataclass(frozen=True)
class Pending:
    """One in-flight actual request and its ordinal planned identity, if still attributable."""

    sequence: int
    at: int
    request: Request
    expected: Optional[Planned]
    exact_request_match: bool


class Reconciler:
    """Compare ordered plan entries and capture handshakes without looking up by tag."""

    def __init__(self, plan, diagnostic_limit=100):
        """Prepare expected cash entries and a bounded diagnostic accumulator."""
        self.plan = plan
        self.issue_limit = integer(diagnostic_limit, "diagnostic limit", 0, MAX_RECORDS)
        self.row = 0
        self.actual_requests = 0
        self.actual_replies = 0
        self.last_cycle = -1
        self.pending = None
        self.last_reply = None
        self.closed = False
        self.counts = Counter()
        self.issues = []
        self.statuses = Counter()
        self.plan_statuses = Counter()
        self.latency = Counter()
        self.wait = Counter()
        self.gross_error = Fraction(0)
        self.fee_error = Fraction(0)
        self.plan_flows = {}
        self.actual_flows = {}
        self.attributed_replies = 0
        self.paired_replies = 0
        self.complete_match = 0
        for item in plan.entries:
            self.plan_statuses[item.reply.status.name] += 1
            self.planned_book(item.instrument).accept(item.reply)

    def planned_book(self, instrument):
        """Return or create the independent expected cash bucket for an instrument."""
        return self.plan_flows.setdefault(instrument, FlowTotals())

    def captured_book(self, instrument):
        """Return or create the actual cash bucket for the named attribution."""
        return self.actual_flows.setdefault(instrument, FlowTotals())

    def include(self, code, message, sequence=None, cycle=None, columns=None):
        """Count each mismatch and retain its details only within the reporting bound."""
        self.counts[code] += 1
        if len(self.issues) < self.issue_limit:
            self.issues.append(Diagnostic(code, message, sequence, cycle, columns or {}))

    def lost_request(self, reason, cycle=None):
        """Record an unmatched outstanding request without inventing a result."""
        waiting = self.pending
        if waiting is not None:
            self.include("incomplete_request", reason, waiting.sequence, cycle,
                         {"request_cycle": waiting.at, "tag": waiting.request.tag})
            self.pending = None

    def request(self, event):
        """Consume the next planned ordinal, comparing every wire field before attribution."""
        self.actual_requests += 1
        if self.pending is not None:
            self.include("request_overlap", "a second request arrived before the prior reply",
                         self.pending.sequence, event.cycle)
            self.lost_request("replaced by a later accepted request", event.cycle)
        first = self.plan.entries[0].sequence
        sequence = first+self.row
        entry = self.plan.entries[self.row] if self.row < len(self.plan.entries) else None
        self.row += 1
        matched = False
        if entry is None:
            self.include("unexpected_request", "request exceeds the selected interval", sequence, event.cycle)
        else:
            mismatches = difference(entry.request, event.request)
            matched = not mismatches
            if mismatches:
                self.include("request_mismatch", "accepted request differs from planned row",
                             sequence, event.cycle, mismatches)
        self.pending = Pending(sequence, event.cycle, event.request, entry, matched)

    def duplicate(self, event):
        """Recognize a repeated last result only when it cannot answer the current request."""
        return (
            self.last_reply is not None and self.last_reply.reply == event.reply
            and (self.pending is None or self.pending.request.tag != event.reply.tag)
        )

    @staticmethod
    def diagnostic_reply(event):
        """Expose unassigned cash and identity in debug output without posting a ledger item."""
        reply = event.reply
        return {
            "tag": reply.tag,
            "visibility": event.first_visible,
            "status": reply.status.name,
            "sell": reply.sell,
            "gross": reply.gross,
            "fee": reply.fee,
            "cash": reply.cash,
            "inexact": reply.inexact,
        }

    def reply(self, event):
        """Recompute the captured transaction, compare its timing, and account for cash."""
        self.actual_replies += 1
        if self.duplicate(event):
            self.include("duplicate_reply", "repeat of previous result with no matching outstanding tag",
                         self.pending.sequence if self.pending else None, event.cycle,
                         {"prior_cycle": self.last_reply.cycle, **self.diagnostic_reply(event)})
            return
        waiting = self.pending
        if waiting is None:
            self.include("unmatched_reply", "response has no associated accepted request",
                         None, event.cycle, self.diagnostic_reply(event))
            return
        self.paired_replies += 1
        computed = mathematical_reply(waiting.request, self.plan.fraction_bits)
        fields = difference(computed.reply, event.reply)
        if fields:
            self.include("reply_mismatch", "observed reply differs from independent arithmetic",
                         waiting.sequence, event.cycle, fields)
        elapsed = event.first_visible-waiting.at
        gap = event.cycle-event.first_visible
        want = model_latency(computed.reply.status)
        if elapsed != want or gap < 0:
            self.include("timing_mismatch", "response visibility differs from the arithmetic pipeline",
                         waiting.sequence, event.cycle,
                         {"elapsed": elapsed, "expected": want, "transfer_hold": gap})
        self.latency[elapsed] += 1
        self.wait[gap] += 1
        self.statuses[event.reply.status.name] += 1
        reference = waiting.expected
        if waiting.exact_request_match and event.reply.tag == waiting.request.tag and reference is not None:
            self.attributed_replies += 1
            bucket = reference.instrument
            if not fields and elapsed == want and gap >= 0:
                self.complete_match += 1
                self.gross_error += computed.gross_error
                self.fee_error += computed.fee_error
        else:
            bucket = UNMATCHED_INSTRUMENT
        self.captured_book(bucket).accept(event.reply)
        self.pending = None
        self.last_reply = event

    def apply(self, event):
        """Use strict order across fragment handoffs and one-outstanding protocol state."""
        if self.closed:
            raise ReconcileError("reconciler is already finalized")
        if not isinstance(event, (RequestEvent, ReplyEvent)):
            raise ReconcileError("unknown event record")
        integer(event.cycle, "physical edge")
        if event.cycle <= self.last_cycle:
            raise ReconcileError("capture must advance across every accepted edge")
        self.last_cycle = event.cycle
        if isinstance(event, RequestEvent):
            self.request(event)
        else:
            self.reply(event)

    def per_instrument(self):
        """Return planned, captured, and unexplained differences for every ledger bucket."""
        empty = FlowTotals()
        report = {}
        for instrument in sorted(set(self.plan_flows) | set(self.actual_flows)):
            planned = self.plan_flows.get(instrument, empty)
            observed = self.actual_flows.get(instrument, empty)
            report[instrument] = {
                "planned": planned.record(),
                "observed": observed.record(),
                "difference": totals_delta(observed, planned),
            }
        return report

    def finish(self):
        """Report missing suffix or incomplete requests and finalize the summary once."""
        if self.closed:
            raise ReconcileError("reconciler is already finalized")
        self.lost_request("capture stopped with an outstanding request")
        for row in self.plan.entries[min(self.row, len(self.plan.entries)):]:
            self.include("missing_request", "planned request has no observed admission",
                         row.sequence, columns={"tag": row.request.tag})
        self.closed = True
        return {
            "fraction_bits": self.plan.fraction_bits,
            "sequence_start": self.plan.entries[0].sequence,
            "sequence_stop": self.plan.entries[-1].sequence+1,
            "source_format": self.plan.source,
            "expected_requests": len(self.plan.entries),
            "observed_requests": self.actual_requests,
            "observed_replies": self.actual_replies,
            "paired_replies": self.paired_replies,
            "attributed_replies": self.attributed_replies,
            "fully_matched": self.complete_match,
            "issue_count": sum(self.counts.values()),
            "issues_by_code": dict(sorted(self.counts.items())),
            "diagnostics": [issue.record() for issue in self.issues],
            "omitted_diagnostics": sum(self.counts.values())-len(self.issues),
            "planned_statuses": dict(sorted(self.plan_statuses.items())),
            "captured_statuses": dict(sorted(self.statuses.items())),
            "first_visible_edges": dict(sorted(self.latency.items())),
            "response_hold_edges": dict(sorted(self.wait.items())),
            "exact_rounding_error": {"gross": str(self.gross_error), "fee": str(self.fee_error)},
            "instruments": self.per_instrument(),
        }


def reconcile(plan, events, diagnostic_limit=100):
    """Reconcile an iterable of captured edges and return a portable evidence report."""
    auditor = Reconciler(plan, diagnostic_limit)
    for event in events:
        auditor.apply(event)
    return auditor.finish()


def segments_for(paths, offsets=None):
    """Create one segment per capture, validating the explicit clock-offset list."""
    if not paths:
        raise ReconcileError("provide at least one hardware capture file")
    inner = [0]*len(paths) if offsets is None else offsets
    if len(paths) != len(inner):
        raise ReconcileError("supply one offset for each capture segment")
    return tuple(CaptureSegment(path, shift) for path, shift in zip(paths, inner))


def output_report(report, target):
    """Publish a diagnostic report atomically so failed runs cannot leave torn JSON."""
    if target is None:
        print(json.dumps(report, indent=2))
        return
    pending = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=target.parent,
                                prefix=target.name+".", delete=False) as file:
            pending = Path(file.name)
            json.dump(report, file, indent=2)
            file.write("\n")
        pending.replace(target)
    finally:
        if pending is not None and pending.exists():
            pending.unlink()


def run_cli(args):
    """Read the selected plan and trace pieces, then emit one machine-readable verdict."""
    if args.output is not None:
        for source in (args.plan, *args.captures):
            ensure_distinct([source, args.output])
    quantize = parse_rounding(args.quantize) if args.quantize is not None else None
    plan = load_plan(args.plan, args.fraction, quantize).selected(args.sequence_start, args.sequence_count)
    parts = segments_for(args.captures, args.offset)
    report = reconcile(plan, captured_events(parts), args.diagnostic_limit)
    report["segments"] = [part.record() for part in parts]
    output_report(report, args.output)
    return int(bool(report["issue_count"]))


def parser():
    """Create the public argument definitions with explicit capture ordering controls."""
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("plan", type=Path)
    command.add_argument("captures", type=Path, nargs="+")
    command.add_argument("--fraction", type=int, help="Q fraction width, defaulting to 8 for CSV")
    command.add_argument("--quantize", choices=("floor", "ceil", "nearest_even"))
    command.add_argument("--sequence-start", type=int, default=0)
    command.add_argument("--sequence-count", type=int)
    command.add_argument("--offset", type=int, action="append", help="one offset per trace fragment")
    command.add_argument("--diagnostic-limit", type=int, default=100)
    command.add_argument("--output", type=Path, help="optional path for JSON output")
    return command


def main(argv=None):
    """Return success, mismatch, or a distinct parsing error for CI consumers."""
    command = parser()
    args = command.parse_args(argv)
    try:
        return run_cli(args)
    except (OSError, ValueError, csv.Error) as exc:
        command.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
