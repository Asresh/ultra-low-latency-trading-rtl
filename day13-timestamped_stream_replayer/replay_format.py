"""Versioned binary and CSV serializers for complete, checked replay tapes."""

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from struct import Struct
from tempfile import NamedTemporaryFile
from zlib import crc32

from replay_program import Beat, LANES, Packet, ReplayError, Tape, U16, U32, U64, unsigned

MAGIC = b"TRP1"
HEADER = Struct("<4sHHII")
RECORD = Struct("<IQHBB")
VERSION = 1
CSV_FIELDS = ("tick", "tag", "last", "payload")
PACKET_FIELDS = ("time_ns", "tag", "payload")


def parse_integer(value, bound, label):
    """Parse decimal or explicitly hexadecimal text before checking the field width."""
    if value is None:
        raise ReplayError(f"{label} is missing")
    text = value.strip()
    if not text:
        raise ReplayError(f"{label} is empty")
    base = 16 if text.lower().startswith("0x") else 10
    try:
        result = int(text, base)
    except ValueError as exc:
        raise ReplayError(f"invalid {label}: {text!r}") from exc
    return unsigned(result, bound, label)


def parse_last(value):
    """Accept exactly the CSV marker values 0 and 1."""
    return bool(parse_integer(value, 1, "last"))


def parse_payload(value, max_length=None):
    """Decode unseparated hexadecimal bytes, optionally enforcing a maximum length."""
    if value is None:
        raise ReplayError("payload is missing")
    text = value.strip()
    if not text or len(text) % 2 or any(ch not in "0123456789abcdefABCDEF" for ch in text):
        raise ReplayError("payload must be nonempty contiguous hex with complete bytes")
    payload = bytes.fromhex(text)
    if max_length is not None and len(payload) > max_length:
        raise ReplayError(f"payload contains more than {max_length} bytes")
    return payload


def parse_decimal(value, label):
    """Convert finite decimal or exponent text to an exact nonnegative rational."""
    try:
        decimal = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReplayError(f"{label} is not a decimal number") from exc
    if not decimal.is_finite() or decimal < 0:
        raise ReplayError(f"{label} must be finite and nonnegative")
    return Fraction(decimal)


def require_header(reader, expected):
    """Require the exact column order so duplicate or misspelled names fail early."""
    if tuple(reader.fieldnames or ()) != expected:
        raise ReplayError(f"CSV header must be {','.join(expected)}")


def complete_row(row, number):
    """Reject extra or missing cells before converting individual field types."""
    if None in row or None in row.values():
        raise ReplayError(f"CSV row {number} has the wrong number of fields")


def canonical_fraction(number):
    """Return exact finite decimal text for an integer or decimal-backed fraction."""
    number = Fraction(number)
    if number.denominator == 1:
        return str(number.numerator)
    denominator = number.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ReplayError("CSV cannot represent the rational time as finite decimal text")
    length = max(twos, fives)
    scaled = abs(number.numerator) * (10**length // number.denominator)
    whole, fraction = divmod(scaled, 10**length)
    sign = "-" if number < 0 else ""
    return f"{sign}{whole}.{fraction:0{length}d}"


def load_tape_csv(source):
    """Load normalized beats with lane masks derived entirely from payload length."""
    reader = csv.DictReader(source)
    require_header(reader, CSV_FIELDS)
    beats = []
    for number, row in enumerate(reader, 2):
        try:
            complete_row(row, number)
            beats.append(
                Beat.from_payload(
                    parse_integer(row["tick"], U32, "tick"),
                    parse_payload(row["payload"], LANES),
                    parse_integer(row["tag"], U16, "tag"),
                    parse_last(row["last"]),
                )
            )
        except ReplayError as exc:
            raise ReplayError(f"tape CSV line {number}: {exc}") from exc
    return Tape(tuple(beats))


def dump_tape_csv(tape, destination):
    """Write a canonical beat CSV in tape order."""
    stream = csv.writer(destination, lineterminator="\n")
    stream.writerow(CSV_FIELDS)
    for beat in tape.events:
        stream.writerow((beat.tick, beat.tag, int(beat.last), beat.payload.hex()))


@dataclass(frozen=True)
class StampedPacket:
    """Raw captured packet with an exact wall-time stamp in nanoseconds."""

    time_ns: Fraction
    tag: int
    payload: bytes

    def __post_init__(self):
        """Validate geometry at the file boundary without prematurely quantizing time."""
        try:
            time = Fraction(self.time_ns)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ReplayError("invalid packet timestamp") from exc
        if time < 0:
            raise ReplayError("packet timestamp must be nonnegative")
        unsigned(self.tag, U16, "tag")
        if type(self.payload) is not bytes or not self.payload:
            raise ReplayError("packet payload must be nonempty bytes")
        object.__setattr__(self, "time_ns", time)


def load_packet_csv(source):
    """Yield capture packets in file order with exact decimal nanosecond times."""
    reader = csv.DictReader(source)
    require_header(reader, PACKET_FIELDS)
    for number, row in enumerate(reader, 2):
        try:
            complete_row(row, number)
            yield StampedPacket(
                parse_decimal(row["time_ns"], "time_ns"),
                parse_integer(row["tag"], U16, "tag"),
                parse_payload(row["payload"]),
            )
        except ReplayError as exc:
            raise ReplayError(f"packet CSV line {number}: {exc}") from exc


def dump_packet_csv(packets, destination):
    """Write complete input packets and exact time values prior to quantization."""
    writer = csv.writer(destination, lineterminator="\n")
    writer.writerow(PACKET_FIELDS)
    for packet in packets:
        writer.writerow((canonical_fraction(packet.time_ns), packet.tag, packet.payload.hex()))


def encode_tape(tape):
    """Serialize fixed-size padded records and prefix an independently checkable CRC."""
    unsigned(len(tape.events), U32, "record count")
    body = bytearray()
    for event in tape.events:
        body.extend(RECORD.pack(event.tick, event.data, event.tag, event.keep, int(event.last)))
    header = HEADER.pack(MAGIC, VERSION, RECORD.size, len(tape.events), crc32(body))
    return header + body


def check_binary_header(blob, capacity=None):
    """Check version, geometry and length before allocating or iterating over records."""
    if len(blob) < HEADER.size:
        raise ReplayError("binary tape header is truncated")
    magic, version, size, count, checksum = HEADER.unpack_from(blob)
    if magic != MAGIC:
        raise ReplayError("binary tape magic does not identify a TRP1 image")
    if version != VERSION:
        raise ReplayError(f"unsupported tape version {version}")
    if size != RECORD.size:
        raise ReplayError("binary tape record geometry differs from the version")
    if capacity is not None and count > unsigned(capacity, U32, "capacity"):
        raise ReplayError(f"image has {count} beats, greater than device capacity {capacity}")
    if len(blob) != HEADER.size + count * size:
        raise ReplayError("binary tape length does not match its record count")
    return count, checksum


def decode_tape(blob, capacity=None):
    """Verify all binary bytes and return the validated tape and packet boundaries."""
    count, checksum = check_binary_header(blob, capacity)
    payload = memoryview(blob)[HEADER.size:]
    if crc32(payload) != checksum:
        raise ReplayError("binary tape CRC mismatch")
    events = []
    for index, (tick, data, tag, keep, flags) in enumerate(RECORD.iter_unpack(payload)):
        if flags not in (0, 1):
            raise ReplayError(f"record {index}: reserved flag bits are set")
        try:
            beat = Beat(tick, data, keep, bool(flags), tag)
        except ReplayError as exc:
            raise ReplayError(f"record {index}: {exc}") from exc
        if beat.data != data:
            raise ReplayError(f"record {index}: inactive lanes must be zero")
        events.append(beat)
    if len(events) != count:
        raise ReplayError("record decoder disagrees with the declared record count")
    return Tape(tuple(events))


def read_tape(path, capacity=None):
    """Read binary or CSV based on the path suffix, then enforce optional capacity."""
    path = Path(path)
    if path.suffix == ".trp":
        return decode_tape(path.read_bytes(), capacity)
    if path.suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as source:
            tape = load_tape_csv(source)
        return tape.checked_depth(capacity) if capacity is not None else tape
    raise ReplayError("tape file must have .trp or .csv extension")


def same_path(left, right):
    """Detect aliases and hard links before replacing files produced by a compiler."""
    left, right = Path(left), Path(right)
    if left.resolve() == right.resolve():
        return True
    return left.exists() and right.exists() and left.samefile(right)


def atomic_output(path, mode, emit):
    """Write and rename a temporary file only after the producer succeeds."""
    target = Path(path)
    binary = "b" in mode
    options = {} if binary else {"encoding": "utf-8", "newline": ""}
    temporary = None
    try:
        with NamedTemporaryFile(
            mode=mode, dir=target.parent, prefix=target.name+".", delete=False, **options
        ) as output:
            temporary = Path(output.name)
            emit(output)
        temporary.replace(target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_tape(tape, destination):
    """Write a tape atomically in the format specified by its name."""
    path = Path(destination)
    if path.suffix == ".trp":
        atomic_output(path, "wb", lambda file: file.write(encode_tape(tape)))
    elif path.suffix == ".csv":
        atomic_output(path, "w", lambda file: dump_tape_csv(tape, file))
    else:
        raise ReplayError("tape destination must have .trp or .csv extension")


def packet_offsets(packet):
    """Return compact metadata for every active payload span in a programmed packet."""
    if not isinstance(packet, Packet):
        raise ReplayError("packet_offsets expects a complete checked packet")
    return tuple(
        (beat.tick, index * LANES, len(beat.payload))
        for index, beat in enumerate(packet.beats())
    )
