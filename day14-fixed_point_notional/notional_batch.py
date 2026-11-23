"""Compile human-readable amounts into RTL tapes and reconcile batch summaries."""

import argparse
import csv
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re

from notional_model import Kind, Request, Rounding, calculate, rounded_divide
from notional_workload import encode, model_latency


ORDER_FIELDS = ("tag", "instrument", "side", "price", "quantity",
                "fee_bps", "rounding", "limit")
REPORT_FIELDS = ("tag", "instrument", "side", "raw_price", "quantity", "fee_bps",
                 "rounding", "limit", "status", "gross", "fee", "cash", "inexact")
SYMBOL = re.compile(r"[A-Z][A-Z0-9_.-]{0,15}\Z")
DECIMAL = re.compile(r"(0|[1-9][0-9]{0,15})(?:\.([0-9]{1,24}))?\Z")
UNSIGNED = re.compile(r"0|[1-9][0-9]{0,19}\Z")


class BatchError(ValueError):
    """An order or report did not conform to the public CSV contract."""


def natural(raw, label, bits):
    """Parse decimal unsigned integer text with a width and canonical spelling."""
    if not isinstance(raw, str) or not UNSIGNED.fullmatch(raw):
        raise BatchError(f"{label} must be canonical unsigned decimal text")
    value = int(raw)
    if value >= (1 << bits):
        raise BatchError(f"{label} exceeds {bits} bits")
    return value


def name(raw):
    """Accept simple uppercase instrument labels with bounded storage."""
    if not isinstance(raw, str) or not SYMBOL.fullmatch(raw):
        raise BatchError("instrument must be 1–16 uppercase letters, digits or ._-")
    return raw


def parse_decimal(raw):
    """Read nonnegative decimal text as an exact fraction, independent of context."""
    if not isinstance(raw, str):
        raise BatchError("price must be decimal text")
    match = DECIMAL.fullmatch(raw)
    if match is None:
        raise BatchError("price must be finite, nonnegative decimal text without an exponent")
    integer, fragment = match.groups()
    denominator = 10 ** len(fragment or "")
    numerator = int(integer) * denominator + int(fragment or "0")
    return Fraction(numerator, denominator)


def pack_price(price, fraction_bits, method=None):
    """Quantize an exact price for the hardware, with an explicit rounding policy if needed."""
    if not 0 <= fraction_bits <= 31:
        raise BatchError("price fractional width must be 0–31")
    scale = (1 << fraction_bits) * price.numerator
    word = rounded_divide(scale, price.denominator, method or Rounding.FLOOR)
    if word.inexact and method is None:
        raise BatchError("price is off the fixed-point grid")
    if word.amount >= (1 << 32):
        raise BatchError("quantized price exceeds 32 bits")
    return word.amount


def parse_side(text):
    """Turn the two CSV side names into the hardware buy/sell flag."""
    if text == "buy":
        return False
    if text == "sell":
        return True
    raise BatchError("side must be buy or sell")


def parse_rounding(text):
    """Map three public rounding labels to their hardware codes."""
    methods = {
        "floor": Rounding.FLOOR,
        "ceil": Rounding.CEIL,
        "nearest_even": Rounding.NEAREST_EVEN,
    }
    try:
        return methods[text]
    except KeyError as exc:
        raise BatchError("rounding must be floor, ceil, or nearest_even") from exc


@dataclass(frozen=True)
class Order:
    instrument: str
    request: Request

    @classmethod
    def from_csv(cls, row, fraction_bits, quantize):
        """Convert one exchange-independent row to the fixed hardware contract."""
        if any(row[field] is None for field in ORDER_FIELDS) or None in row:
            raise BatchError("row width differs from its header")
        instrument = name(row["instrument"])
        price = pack_price(parse_decimal(row["price"]), fraction_bits, quantize)
        quantity = natural(row["quantity"], "quantity", 32)
        fee = natural(row["fee_bps"], "fee bps", 16)
        tag = natural(row["tag"], "tag", 16)
        side = parse_side(row["side"])
        rounding = parse_rounding(row["rounding"])
        limit = natural(row["limit"], "limit", 64) if row["limit"] != "" else None
        return cls(instrument, Request(tag, price, quantity, fee, rounding, side, limit))

    def report(self, fraction_bits):
        """Retain instrument and original quantized request alongside predicted fields."""
        order = self.request
        reply = calculate(order, fraction_bits)
        return {
            "tag": order.tag,
            "instrument": self.instrument,
            "side": "sell" if order.sell else "buy",
            "raw_price": order.price,
            "quantity": order.quantity,
            "fee_bps": order.fee_bps,
            "rounding": Rounding(order.rounding).name.lower(),
            "limit": "" if order.limit is None else order.limit,
            "status": reply.status.name,
            "gross": reply.gross,
            "fee": reply.fee,
            "cash": reply.cash,
            "inexact": int(reply.inexact),
        }

    def tape_row(self, fraction_bits):
        """Append the arithmetic oracle's fields to the physical request fields."""
        reply = calculate(self.request, fraction_bits)
        return encode((*self.request.wire(), *reply.wire(), model_latency(reply.status)))


def read_orders(source, fraction_bits, quantize=None):
    """Read exact CSV headers and report the source row on validation errors."""
    data = csv.DictReader(source, strict=True)
    if data.fieldnames != list(ORDER_FIELDS):
        raise BatchError(f"required CSV header is {','.join(ORDER_FIELDS)}")
    for row in data:
        try:
            yield Order.from_csv(row, fraction_bits, quantize)
        except ValueError as exc:
            raise BatchError(f"line {data.line_num}: {exc}") from exc


def ensure_distinct(paths):
    """Catch path and hard-link aliases before two roles open the same file."""
    for index, source in enumerate(paths):
        for target in paths[index + 1:]:
            if source.resolve() == target.resolve():
                raise BatchError(f"{source} and {target} refer to the same path")
            if source.exists() and target.exists() and source.samefile(target):
                raise BatchError(f"{source} and {target} refer to the same file")


def report_csv(target, rows):
    """Publish matching field order independent of Python's dict construction order."""
    with target.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=REPORT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def compile_orders(source, report, tape, fraction_bits, quantize=None):
    """Validate every input before replacing an existing report or simulator tape."""
    ensure_distinct([source, report, tape])
    with source.open(encoding="utf-8", newline="") as file:
        orders = list(read_orders(file, fraction_bits, quantize))
    if not orders:
        raise BatchError("batch must contain at least one order")
    computed = [order.report(fraction_bits) for order in orders]
    rows = [order.tape_row(fraction_bits) for order in orders]
    report_csv(report, computed)
    tape.write_text("\n".join(rows) + "\n", encoding="ascii")
    return {
        "fraction_bits": fraction_bits,
        "records": len(rows),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "tape_sha256": hashlib.sha256(tape.read_bytes()).hexdigest(),
        "total_by_instrument": summarize(computed),
    }


@dataclass
class Summary:
    requests: int = 0
    accepted: int = 0
    inexact: int = 0
    gross: int = 0
    fee: int = 0
    buy_debits: int = 0
    sell_proceeds: int = 0

    def add(self, record):
        """Add only executable cash values while still counting failed calculations."""
        self.requests += 1
        self.inexact += int(record["inexact"])
        if record["status"] != Kind.OK.name:
            return
        self.accepted += 1
        self.gross += int(record["gross"])
        self.fee += int(record["fee"])
        if record["side"] == "buy":
            self.buy_debits += int(record["cash"])
        else:
            self.sell_proceeds += int(record["cash"])

    def values(self):
        """Include signed net cash change without a hardware-width accumulator."""
        result = dict(vars(self))
        result["net_cash_change"] = self.sell_proceeds - self.buy_debits
        return result


def summarize(records):
    """Group accepted cash flows by instrument in stable lexical order."""
    total = {}
    for record in records:
        instrument = record["instrument"]
        if instrument not in total:
            total[instrument] = Summary()
        total[instrument].add(record)
    return {symbol: accumulator.values() for symbol, accumulator in sorted(total.items())}


def main():
    """Expose decimal-to-hardware compilation without a proprietary simulator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("orders", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("tape", type=Path)
    parser.add_argument("--fraction", type=int, default=8)
    parser.add_argument("--quantize", choices=("floor", "ceil", "nearest_even"))
    args = parser.parse_args()
    try:
        mode = parse_rounding(args.quantize) if args.quantize else None
        print(json.dumps(compile_orders(args.orders, args.report, args.tape, args.fraction, mode),
                         indent=2, sort_keys=True))
    except (ValueError, OSError, csv.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
