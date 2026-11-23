"""Store replay and result rows as self-describing, checksummed capture files."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct
import zlib

from quote_schema import Drive, Profile, Result, Sample, mask


MAGIC = b"BQ11"
VERSION = 1
HEADER = struct.Struct(">4sBBBBBBBBB")
FRAME = struct.Struct(">I")
MAX_RECORDS = 50_000_000


class Kind(IntEnum):
    DRIVES = 1
    TRACE = 2


class ArchiveError(ValueError):
    """A packed quote capture has a malformed header, row, or checksum."""


@dataclass(frozen=True)
class Field:
    name: str
    width: int
    signed: bool = False

    @property
    def low(self):
        """Return the minimum value when interpreting the field numerically."""
        return -(1 << (self.width - 1)) if self.signed else 0

    @property
    def high(self):
        """Return the inclusive maximum for this field's representation."""
        return (1 << (self.width - int(self.signed))) - 1

    def encode(self, value):
        """Validate numeric range and convert signed values into two's complement."""
        if not isinstance(value, int) or not self.low <= value <= self.high:
            raise ArchiveError(f"{self.name} does not fit: {value!r}")
        return int(value) & mask(self.width)

    def decode(self, packed):
        """Mask the incoming bits and sign-extend when the sign bit is set."""
        value = packed & mask(self.width)
        if self.signed and value >> (self.width - 1):
            value -= 1 << self.width
        return value


def layout_drives(p):
    """Describe the fields of one packed driver row in low-to-high order."""
    return (
        Field("tick", p.time_w),
        Field("reset", 1),
        Field("ack_ready", 1),
        Field("result_ready", 1),
        Field("cmd_valid", 1),
        Field("op", 3),
        Field("cmd_instrument", p.inst_w),
        Field("cmd_venue", p.venue_w),
        Field("sequence", p.seq_w),
        Field("bid_price", p.price_w),
        Field("ask_price", p.price_w),
        Field("bid_qty", p.qty_w),
        Field("ask_qty", p.qty_w),
        Field("cmd_tag", p.tag_w),
        Field("query_valid", 1),
        Field("query_instrument", p.inst_w),
        Field("query_max_age", p.time_w),
        Field("query_tag", p.tag_w),
    )


def layout_winner(p, side):
    """Describe the contiguous fields for one returned bid or ask."""
    return (
        Field(f"{side}_valid", 1),
        Field(f"{side}_venue", p.venue_w),
        Field(f"{side}_price", p.price_w),
        Field(f"{side}_qty", p.qty_w),
        Field(f"{side}_age", p.time_w),
        Field(f"{side}_sequence", p.seq_w),
    )


def layout_trace(p):
    """Describe one result row, omitting the implicit consecutive cycle number."""
    return (
        Field("cmd_ready", 1),
        Field("query_ready", 1),
        Field("ack_valid", 1),
        Field("ack_code", 4),
        Field("ack_tag", p.tag_w),
        Field("result_valid", 1),
        Field("result_tag", p.tag_w),
        Field("result_code", 4),
        Field("state", 3),
        Field("result_tick", p.time_w),
        Field("active", p.venues),
        Field("stale", p.venues),
        *layout_winner(p, "bid"),
        *layout_winner(p, "ask"),
        Field("spread", p.price_w + 1, signed=True),
    )


@dataclass(frozen=True)
class Format:
    kind: Kind
    profile: Profile
    fields: tuple[Field, ...]

    @property
    def bits(self):
        """Return the number of significant bits per packed row."""
        return sum(field.width for field in self.fields)

    @property
    def bytes(self):
        """Round the packed row width up to a whole number of bytes."""
        return (self.bits + 7) // 8

    def pack(self, numbers):
        """Validate a full row and serialize it to a little-endian byte string."""
        if len(numbers) != len(self.fields):
            raise ArchiveError(f"expected {len(self.fields)} columns, got {len(numbers)}")
        packed = 0
        offset = 0
        for field, number in zip(self.fields, numbers):
            packed |= field.encode(number) << offset
            offset += field.width
        return packed.to_bytes(self.bytes, "little")

    def unpack(self, data):
        """Decode a row and reject noncanonical high padding bits."""
        if len(data) != self.bytes:
            raise ArchiveError(f"expected {self.bytes} packed bytes, got {len(data)}")
        packed = int.from_bytes(data, "little")
        numbers = []
        for field in self.fields:
            numbers.append(field.decode(packed))
            packed >>= field.width
        if packed:
            raise ArchiveError("nonzero packed-row padding bits")
        return tuple(numbers)

    def encode(self, record):
        """Validate a typed row and encode only its portable wire fields."""
        if self.kind == Kind.DRIVES:
            if not isinstance(record, Drive):
                raise ArchiveError("expected a driver row")
            record.check(self.profile)
        elif not isinstance(record, Sample):
            raise ArchiveError("expected a sample row")
        return self.pack(record.encode())

    def decode(self, raw):
        """Recover one typed row and check its encoded identity."""
        values = self.unpack(raw)
        if self.kind == Kind.DRIVES:
            result = Drive.decode(values, self.profile)
        else:
            result = Sample.decode(values, self.profile)
        if values != result.encode():
            raise ArchiveError("unused payload fields must be zero")
        return result


def format_for(kind, profile):
    """Choose the row layout associated with the given stream type and geometry."""
    try:
        kind = Kind(kind)
    except (TypeError, ValueError) as exc:
        raise ArchiveError(f"unknown archive kind {kind!r}") from exc
    constructor = layout_drives if kind == Kind.DRIVES else layout_trace
    return Format(kind, profile, constructor(profile))


def header_bytes(fmt):
    """Serialize a version, stream type, and all seven profile parameters."""
    p = fmt.profile
    return HEADER.pack(
        MAGIC, VERSION, fmt.kind, p.instruments, p.venues,
        p.price_w, p.qty_w, p.time_w, p.seq_w, p.tag_w,
    )


def read_header(file):
    """Read the binary header and reject invalid layouts before processing records."""
    raw = file.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise ArchiveError("truncated quote archive header")
    magic, version, kind, *shape = HEADER.unpack(raw)
    if magic != MAGIC:
        raise ArchiveError("unrecognized quote archive magic")
    if version != VERSION:
        raise ArchiveError(f"unsupported quote archive version {version}")
    try:
        return format_for(kind, Profile(*shape))
    except ValueError as exc:
        raise ArchiveError(f"invalid quote archive layout: {exc}") from exc


def row_checksum(raw, row):
    """Bind a row's packed payload to its physical position with CRC32."""
    return zlib.crc32(raw, row)


def write_archive(file, fmt, records):
    """Write one self-describing stream of delimited, checksummed binary rows."""
    file.write(header_bytes(fmt))
    count = 0
    for count, record in enumerate(records, 1):
        if count > MAX_RECORDS:
            raise ArchiveError("capture exceeds maximum record count")
        packed = fmt.encode(record)
        file.write(packed)
        file.write(FRAME.pack(row_checksum(packed, count)))
    return count


def read_records(file, fmt):
    """Yield verified rows until clean EOF and identify the first corrupt record."""
    for row in range(1, MAX_RECORDS + 2):
        raw = file.read(fmt.bytes)
        if not raw:
            return
        if row > MAX_RECORDS:
            raise ArchiveError("capture exceeds maximum record count")
        if len(raw) != fmt.bytes:
            raise ArchiveError(f"truncated payload at row {row}")
        checksum = file.read(FRAME.size)
        if len(checksum) != FRAME.size:
            raise ArchiveError(f"truncated checksum at row {row}")
        if FRAME.unpack(checksum)[0] != row_checksum(raw, row):
            raise ArchiveError(f"checksum mismatch at row {row}")
        try:
            yield fmt.decode(raw)
        except ValueError as exc:
            raise ArchiveError(f"row {row}: {exc}") from exc


def read_archive(file, expected=None):
    """Return a streaming reader and ensure an optional expected archive layout matches."""
    fmt = read_header(file)
    if expected is not None:
        if fmt.kind != expected.kind:
            raise ArchiveError("wrong archive stream type")
        if fmt.profile != expected.profile:
            raise ArchiveError("archive profile differs from expected geometry")
    return fmt, read_records(file, fmt)
