"""Portable packet-gearbox traces with streaming validation and reset boundaries."""

import argparse
import io
import json
from dataclasses import dataclass
from pathlib import Path
from struct import Struct
from zlib import crc32

from width_audit import AuditError, TraceAudit, Transfer, capture
from width_model import Beat, Report, Status, require_unsigned

MAGIC = b"WGC1"
HEADER = Struct("<4sBBH")
RECORD = Struct("<BQH")
CHECKSUM = Struct("<I")
BEAT_META = Struct("<HBH")
REPORT_FIELDS = Struct("<HHB")
KIND = {"reset": 0, "input": 1, "output": 2, "report": 3}
BY_KIND = {value: key for key, value in KIND.items()}


class CaptureError(ValueError):
    """A capture is incompatible with the selected wire format."""


@dataclass(frozen=True)
class Geometry:
    input_width: int
    output_width: int
    capacity: int

    def __post_init__(self):
        """Limit widths to the 16-bit mask and configured length fields."""
        if not 1 <= self.input_width <= 16:
            raise CaptureError("input width is outside 1–16 lanes")
        if not 1 <= self.output_width <= 16:
            raise CaptureError("output width is outside 1–16 lanes")
        if not 1 <= self.capacity <= 65535:
            raise CaptureError("packet capacity is outside 1–65535 bytes")

    def size_for(self, channel):
        """Derive the exact encoded body length for a known channel."""
        if channel == "input":
            return BEAT_META.size + self.input_width
        if channel == "output":
            return BEAT_META.size + self.output_width
        if channel == "report":
            return REPORT_FIELDS.size
        if channel == "reset":
            return 0
        raise CaptureError(f"unknown transfer channel {channel!r}")

    def width_for(self, channel):
        """Return the physical word width for a byte-transfer channel."""
        if channel == "input":
            return self.input_width
        if channel == "output":
            return self.output_width
        raise CaptureError(f"{channel} has no data lanes")

    def header(self):
        """Encode the magic and geometry in one fixed-size header."""
        return HEADER.pack(MAGIC, self.input_width, self.output_width, self.capacity)

    @classmethod
    def read(cls, file):
        """Load a validated capture header and leave the cursor at its first record."""
        raw = read_exact(file, HEADER.size, "capture header")
        magic, iw, ow, capacity = HEADER.unpack(raw)
        if magic != MAGIC:
            raise CaptureError("capture has the wrong magic or unsupported version")
        return cls(iw, ow, capacity)


def read_exact(file, count, label):
    """Reject short records while allowing a zero-length reset body."""
    data = file.read(count)
    if len(data) != count:
        raise CaptureError(f"truncated {label}: expected {count} bytes")
    return data


def encode_value(geometry, channel, value):
    """Encode one typed beat, report, or zero-length reset body."""
    if channel == "reset":
        if value is not None:
            raise CaptureError("reset record cannot carry data")
        return b""
    if channel == "report":
        if not isinstance(value, Report):
            raise CaptureError("report record requires a Report value")
        require_unsigned(value.tag, 16, "report tag")
        require_unsigned(value.length, 16, "report length")
        return REPORT_FIELDS.pack(value.tag, value.length, Status(value.status))
    width = geometry.width_for(channel)
    if not isinstance(value, Beat):
        raise CaptureError("data record requires a Beat value")
    value.validate(width)
    return BEAT_META.pack(value.keep, value.last, value.tag) + value.data.to_bytes(width, "little")


def decode_value(geometry, channel, body):
    """Decode a checked record body and revalidate flags and lane width."""
    if channel == "reset":
        return None
    if channel == "report":
        tag, length, raw_status = REPORT_FIELDS.unpack(body)
        return Report(tag, length, Status(raw_status))
    keep, ending, tag = BEAT_META.unpack_from(body)
    if ending not in (0, 1):
        raise CaptureError("beat last flag is not binary")
    value = Beat(int.from_bytes(body[BEAT_META.size:], "little"), keep, bool(ending), tag)
    value.validate(geometry.width_for(channel))
    return value


class Ordering:
    """Record order is monotonic, with one transfer per channel per edge."""

    def __init__(self):
        """Start without an active clock edge."""
        self.last = -1
        self.channels = set()

    def check(self, event):
        """Reject decreasing time and simultaneous reset plus normal transfer."""
        require_unsigned(event.cycle, 64, "cycle")
        if event.cycle < self.last:
            raise CaptureError("records moved backwards in time")
        if event.cycle != self.last:
            self.channels.clear()
            self.last = event.cycle
        if event.channel in self.channels:
            raise CaptureError("repeated channel on one cycle")
        if self.channels and (event.channel == "reset" or "reset" in self.channels):
            raise CaptureError("reset shares a cycle with a transfer")
        self.channels.add(event.channel)


def write_capture(file, geometry, events):
    """Write canonical ordered rows with a CRC over each prefix and body."""
    file.write(geometry.header())
    order = Ordering()
    records = 0
    for event in events:
        order.check(event)
        if event.channel not in KIND:
            raise CaptureError(f"unrecognized channel {event.channel!r}")
        body = encode_value(geometry, event.channel, event.value)
        packet = RECORD.pack(KIND[event.channel], event.cycle, len(body)) + body
        file.write(packet)
        file.write(CHECKSUM.pack(crc32(packet)))
        records += 1
    return records


def read_rows(file, geometry):
    """Decode records incrementally and verify structure before releasing each row."""
    order = Ordering()
    index = 0
    while lead := file.read(1):
        index += 1
        try:
            head = lead + read_exact(file, RECORD.size - 1, "record prefix")
            kind, clock, length = RECORD.unpack(head)
            channel = BY_KIND.get(kind)
            if channel is None:
                raise CaptureError(f"unknown record type {kind}")
            if length != geometry.size_for(channel):
                raise CaptureError("record body length differs from its declared geometry")
            body = read_exact(file, length, "record body")
            (proof,) = CHECKSUM.unpack(read_exact(file, CHECKSUM.size, "record checksum"))
            if proof != crc32(head + body):
                raise CaptureError("record CRC mismatch")
            event = Transfer(channel, clock, decode_value(geometry, channel, body))
            order.check(event)
        except ValueError as exc:
            raise CaptureError(f"record {index}: {exc}") from exc
        yield event


def text_record(event):
    """Convert a typed record to the same human-readable text emitted by RTL."""
    prefix = f"{event.channel} {event.cycle}"
    if event.channel == "reset":
        return prefix
    value = event.value
    if event.channel == "report":
        return f"{prefix} {value.tag:x} {value.length:x} {value.status:x}"
    return f"{prefix} {value.data:x} {value.keep:x} {int(value.last):x} {value.tag:x}"


def check_destination(source, target):
    """Require separate input and output paths, including aliases on disk."""
    if source.resolve() == target.resolve():
        raise CaptureError("input and output refer to the same path")
    if target.exists() and source.samefile(target):
        raise CaptureError("input and output refer to the same file")


def convert_to_binary(source, target, geometry):
    """Inspect all text rows before opening the canonical binary output."""
    check_destination(source, target)
    with source.open(encoding="ascii") as rows:
        events = capture(rows, geometry.input_width, geometry.output_width)
        buffer = io.BytesIO()
        count = write_capture(buffer, geometry, events)
    target.write_bytes(buffer.getvalue())
    return count


def convert_to_text(source, target):
    """Return a readable trace only after every binary record validates."""
    check_destination(source, target)
    with source.open("rb") as data:
        geometry = Geometry.read(data)
        records = [text_record(event) for event in read_rows(data, geometry)]
    target.write_text("".join(row + "\n" for row in records), encoding="ascii")
    return len(records)


def inspect(source):
    """Run a semantic audit of a binary capture and report its geometry."""
    with source.open("rb") as file:
        geometry = Geometry.read(file)
        auditor = TraceAudit(geometry.input_width, geometry.output_width, geometry.capacity)
        for event in read_rows(file, geometry):
            auditor.apply(event)
    return {"geometry": vars(geometry), **auditor.finish()}


def argument_parser():
    """Define the focused pack, unpack, and semantic check commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(required=True, dest="action")
    encode = subcommands.add_parser("pack", help="convert RTL text to binary")
    encode.add_argument("source", type=Path)
    encode.add_argument("target", type=Path)
    encode.add_argument("--input-width", type=int, required=True)
    encode.add_argument("--output-width", type=int, required=True)
    encode.add_argument("--capacity", type=int, required=True)
    decode = subcommands.add_parser("unpack", help="read binary as canonical text")
    decode.add_argument("source", type=Path)
    decode.add_argument("target", type=Path)
    audit = subcommands.add_parser("check", help="audit packet contents and timing")
    audit.add_argument("source", type=Path)
    return parser


def main():
    """Run the selected file operation and report any malformed input."""
    parser = argument_parser()
    args = parser.parse_args()
    try:
        if args.action == "pack":
            geometry = Geometry(args.input_width, args.output_width, args.capacity)
            print(convert_to_binary(args.source, args.target, geometry))
        elif args.action == "unpack":
            print(convert_to_text(args.source, args.target))
        else:
            print(json.dumps(inspect(args.source), indent=2, sort_keys=True))
    except (ValueError, OSError, AuditError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
