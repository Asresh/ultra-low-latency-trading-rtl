"""Pack a batch of arithmetic requests and replies into a bounded wire image."""

import argparse
from dataclasses import dataclass, replace
import io
import json
from pathlib import Path
from struct import Struct
from zlib import crc32

from notional_batch import ensure_distinct, read_orders
from notional_model import Kind, Reply, Request, calculate, unsigned
from notional_workload import encode, model_latency

MAGIC = b"NOM1"
HEADER = Struct("<4sBBHI")
ORDER = Struct("<HIIHBBQ")
REPLY = Struct("<HBBQQQ")
CHECK = Struct("<I")
SEQUENCE = Struct("<I")
MAX_RECORDS = 100000
MAX_IMAGE = HEADER.size + MAX_RECORDS * (ORDER.size + REPLY.size + CHECK.size + SEQUENCE.size)


class ImageError(ValueError):
    """A binary notional image violates its framing or arithmetic contract."""


def read_exact(file, size, label):
    """Read an entire fixed-size record or report the exact truncated field."""
    block = file.read(size)
    if len(block) != size:
        raise ImageError(f"truncated {label}")
    return block


@dataclass(frozen=True)
class ImageHeader:
    fraction_bits: int
    count: int

    def __post_init__(self):
        """Validate the fixed-point format and cap the stream's advertised size."""
        if not 0 <= self.fraction_bits <= 31:
            raise ImageError("price fractional width is outside 0–31")
        if not 1 <= self.count <= MAX_RECORDS:
            raise ImageError(f"image must contain 1–{MAX_RECORDS} records")

    def bytes(self):
        """Encode a versioned fixed-layout header with reserved fields set to zero."""
        return HEADER.pack(MAGIC, self.fraction_bits, 0, 0, self.count)

    @classmethod
    def read(cls, file):
        """Check version and reserved fields before trusting the advertised count."""
        raw = read_exact(file, HEADER.size, "image header")
        magic, fraction_bits, mode, reserved, count = HEADER.unpack(raw)
        if magic != MAGIC:
            raise ImageError("file does not begin with the NOM1 signature")
        if mode or reserved:
            raise ImageError("image header reserved fields must be zero")
        return cls(fraction_bits, count)


def encode_order(request):
    """Pack one request's booleans into flags, retaining the optional limit bit."""
    flags = int(request.sell) | (int(request.limit is not None) << 1)
    return ORDER.pack(
        request.tag, request.price, request.quantity, request.fee_bps,
        request.rounding, flags, request.limit or 0,
    )


def decode_order(data):
    """Recover the request and reject reserved bits and noncanonical optional fields."""
    tag, price, qty, fee, mode, flags, limit = ORDER.unpack(data)
    if flags & ~3:
        raise ImageError("request reserved flags must be clear")
    if not flags & 2 and limit:
        raise ImageError("disabled order limit must be zero")
    return Request(tag, price, qty, fee, mode, bool(flags & 1), limit if flags & 2 else None)


def encode_reply(reply):
    """Pack output status, inexact indication, and cash fields into a fixed record."""
    flags = int(reply.sell) | (int(reply.inexact) << 1)
    return REPLY.pack(
        unsigned(reply.tag, 16, "reply tag"), Kind(reply.status), flags,
        unsigned(reply.gross, 64, "gross"), unsigned(reply.fee, 64, "fee"),
        unsigned(reply.cash, 64, "cash"),
    )


def decode_reply(data):
    """Decode a typed reply with exactly two defined flag bits."""
    tag, status, flags, gross, fee, cash = REPLY.unpack(data)
    if flags & ~3:
        raise ImageError("reply reserved flag bits must be clear")
    try:
        kind = Kind(status)
    except ValueError as exc:
        raise ImageError(f"unknown reply code {status}") from exc
    return Reply(tag, kind, bool(flags & 1), gross, fee, cash, bool(flags & 2))


@dataclass(frozen=True)
class ImageRecord:
    sequence: int
    request: Request
    reply: Reply

    def encoded(self):
        """Cover sequence and the complete request/reply pair with one CRC-32."""
        seq = SEQUENCE.pack(unsigned(self.sequence, 32, "record sequence"))
        body = seq + encode_order(self.request) + encode_reply(self.reply)
        return body + CHECK.pack(crc32(body))

    @classmethod
    def predict(cls, sequence, request, fraction_bits):
        """Produce a binary image row from the trusted integer calculation."""
        return cls(sequence, request, calculate(request, fraction_bits))

    def tape(self):
        """Convert the binary row into standard testbench fields including expected time."""
        return encode((*self.request.wire(), *self.reply.wire(), model_latency(self.reply.status)))

    def check(self, fraction_bits):
        """Distinguish transport corruption from a validly encoded but incorrect reply."""
        expected = calculate(self.request, fraction_bits)
        if self.reply != expected:
            raise ImageError(
                f"record {self.sequence}: arithmetic reply differs for tag {self.request.tag}"
            )

    @classmethod
    def read(cls, file, sequence):
        """Check framing, CRC, and record sequence before decoding a typed row."""
        body = read_exact(file, SEQUENCE.size + ORDER.size + REPLY.size, f"record {sequence}")
        crc = read_exact(file, CHECK.size, f"record {sequence} CRC")
        if CHECK.unpack(crc)[0] != crc32(body):
            raise ImageError(f"record {sequence}: CRC mismatch")
        expected = SEQUENCE.unpack_from(body)[0]
        if expected != sequence:
            raise ImageError(f"record {sequence}: sequence mismatch, stored {expected}")
        order_end = SEQUENCE.size + ORDER.size
        try:
            order = decode_order(body[SEQUENCE.size:order_end])
            reply = decode_reply(body[order_end:])
        except ValueError as exc:
            raise ImageError(f"record {sequence}: {exc}") from exc
        return cls(sequence, order, reply)


class Reader:
    """Consume exactly the declared number of records from an image."""

    def __init__(self, file):
        """Store the file position immediately after its validated header."""
        self.file = file
        self.header = ImageHeader.read(file)
        self.sequence = 0

    def next(self):
        """Return a record or reject trailing garbage at the declared image boundary."""
        if self.sequence >= self.header.count:
            if self.file.read(1):
                raise ImageError("extra bytes after the final record")
            return None
        row = ImageRecord.read(self.file, self.sequence)
        self.sequence += 1
        return row

    def records(self, check_math=True):
        """Decode one record at a time and optionally recompute its arithmetic."""
        while row := self.next():
            if check_math:
                row.check(self.header.fraction_bits)
            yield row


def save_image(file, header, orders):
    """Write a header and exactly the declared count from a request iterable."""
    file.write(header.bytes())
    rows = 0
    for sequence, order in enumerate(orders):
        if sequence >= header.count:
            raise ImageError("request stream exceeded its declared header count")
        row = ImageRecord.predict(sequence, order, header.fraction_bits)
        file.write(row.encoded())
        rows += 1
    if rows != header.count:
        raise ImageError("request stream shorter than its declared header count")
    return rows


def compile_image(source, target, fraction_bits):
    """Validate CSV input before opening a distinct binary output path."""
    ensure_distinct([source, target])
    requests = []
    with source.open(encoding="utf-8", newline="") as file:
        for order in read_orders(file, fraction_bits):
            requests.append(order.request)
            if len(requests) > MAX_RECORDS:
                raise ImageError("CSV exceeds binary record limit")
    header = ImageHeader(fraction_bits, len(requests))
    contents = io.BytesIO()
    count = save_image(contents, header, requests)
    target.write_bytes(contents.getvalue())
    return count


def check_size(path):
    """Apply a cheap size limit before parsing advertised counts or allocating buffers."""
    if path.stat().st_size > MAX_IMAGE:
        raise ImageError("binary image size exceeds maximum")


def image_to_tape(source, target):
    """Validate each image row before exposing the standard simulator text."""
    ensure_distinct([source, target])
    check_size(source)
    with source.open("rb") as file:
        rows = [row.tape() for row in Reader(file).records()]
    target.write_text("\n".join(rows) + "\n", encoding="ascii")
    return len(rows)


def explain(path, limit):
    """Report binary batch outcomes and a bounded number of example records."""
    if limit < 0:
        raise ImageError("example limit cannot be negative")
    check_size(path)
    statuses = {kind.name: 0 for kind in Kind}
    examples = []
    with path.open("rb") as file:
        reader = Reader(file)
        for row in reader.records():
            statuses[row.reply.status.name] += 1
            if len(examples) < limit:
                examples.append({
                    "index": row.sequence,
                    "request": vars(row.request),
                    "result": {**vars(row.reply), "status": row.reply.status.name},
                })
        header = reader.header
    return {"price_fraction_bits": header.fraction_bits,
            "records": header.count, "outcomes": statuses, "examples": examples}


def main():
    """Provide separate pack, tape, and inspect operations for binary fixtures."""
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    packer = subcommands.add_parser("pack", help="convert exact-price CSV to NOM1")
    packer.add_argument("source", type=Path)
    packer.add_argument("target", type=Path)
    packer.add_argument("--fraction", type=int, default=8)
    text = subcommands.add_parser("tape", help="emit an Icarus comparison tape")
    text.add_argument("source", type=Path)
    text.add_argument("target", type=Path)
    inspector = subcommands.add_parser("inspect", help="verify and summarize a binary batch")
    inspector.add_argument("source", type=Path)
    inspector.add_argument("--examples", type=int, default=3)
    args = parser.parse_args()
    try:
        if args.action == "pack":
            print(f"Wrote {compile_image(args.source, args.target, args.fraction)} rows.")
        elif args.action == "tape":
            print(f"Unpacked {image_to_tape(args.source, args.target)} rows.")
        else:
            print(json.dumps(explain(args.source, args.examples), indent=2))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
