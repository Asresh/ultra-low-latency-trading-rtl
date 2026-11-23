"""Decode the risk gateway's offline RSK1 audit stream."""

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, fields
from itertools import zip_longest
import json
from pathlib import Path
import struct
import sys
import zlib

from risk_model import Decision, PROFILES, Status, decode_reply

MAGIC = 0x314B5352
VERSION = 1
WORD = struct.Struct("<I")
U32_MASK = (1 << 32) - 1
MAX_PAYLOAD_BITS = 4096


class CaptureError(ValueError):
    """An invalid or incomplete audit transport capture."""


@dataclass(frozen=True)
class Frame:
    frame_id: int
    decision: Decision


def file_words(source, binary):
    if binary:
        index = 0
        while True:
            data = source.read(WORD.size)
            if not data:
                return
            if len(data) != WORD.size:
                raise CaptureError(f"partial little-endian word at index {index}")
            yield WORD.unpack(data)[0]
            index += 1
    else:
        for line_number, line in enumerate(source, 1):
            line = line.strip()
            if not line:
                continue
            try:
                token = line.removeprefix("0x").removeprefix("0X")
                if len(token) != 8 or any(ch not in "0123456789abcdefABCDEF" for ch in token):
                    raise ValueError
                yield int(token, 16)
            except ValueError as exc:
                raise CaptureError(f"invalid hex audit word at line {line_number}: {line!r}") from exc


def frame_records(words, profile, first_id=0):
    next_id = first_id
    stream = iter(words)
    parsed = 0

    def take(label):
        try:
            return next(stream)
        except StopIteration as exc:
            raise CaptureError(f"record {parsed}: truncated at {label}") from exc

    for header in stream:
        if header != MAGIC:
            raise CaptureError(f"record {parsed}: bad magic {header:#010x}")
        descriptor = take("descriptor")
        version, width = descriptor >> 24, descriptor & ((1 << 24) - 1)
        if version != VERSION:
            raise CaptureError(f"record {parsed}: unsupported version {version}")
        if not 0 < width <= MAX_PAYLOAD_BITS or width != profile.response_bits:
            raise CaptureError(f"record {parsed}: width {width} != {profile.response_bits}")
        frame_id = take("frame id")
        if next_id is not None and frame_id != next_id:
            raise CaptureError(f"record {parsed}: frame id {frame_id} != expected {next_id}")
        payload = [take(f"payload word {i}") for i in range((width + 31) // 32)]
        crc = take("CRC")
        packet = b"".join(WORD.pack(word) for word in (header, descriptor, frame_id, *payload))
        want_crc = zlib.crc32(packet)
        if crc != want_crc:
            raise CaptureError(f"record {parsed}: CRC {crc:#010x} != computed {want_crc:#010x}")
        packed = int.from_bytes(packet[12:], "little")
        if packed >> width:
            raise CaptureError(f"record {parsed}: nonzero high payload padding")
        yield Frame(frame_id, decode_reply(profile, packed))
        next_id = (frame_id + 1) & U32_MASK
        parsed += 1


def expected_rows(source):
    for row, line in enumerate(source, 1):
        if not line.strip():
            continue
        try:
            values = [int(value, 10) for value in line.split()]
            yield Decision.from_wire_fields(values)
        except ValueError as exc:
            raise CaptureError(f"bad expected decision at line {row}: {exc}") from exc


def check_trace(actual, expected):
    statuses = Counter()
    count = 0
    for count, (frame, want) in enumerate(zip_longest(actual, expected), 1):
        if frame is None:
            raise CaptureError(f"missing audit frame for expected decision {count}")
        if want is None:
            raise CaptureError(f"unexpected frame id {frame.frame_id} after {count - 1} decisions")
        if frame.decision != want:
            details = []
            for field in fields(Decision):
                got_value = getattr(frame.decision, field.name)
                want_value = getattr(want, field.name)
                if got_value != want_value:
                    details.append(f"{field.name} {got_value} != {want_value}")
            raise CaptureError(f"audit frame {frame.frame_id}: {', '.join(details)}")
        try:
            status = Status(frame.decision.code).name
        except ValueError:
            status = str(frame.decision.code)
        statuses[status] += 1
    if count == 0:
        raise CaptureError("no audit frames or expected decisions")
    return count, statuses


def first_id_value(raw):
    if raw == "any":
        return None
    value = int(raw, 0)
    if not 0 <= value <= U32_MASK:
        raise argparse.ArgumentTypeError("frame id must fit 32 bits")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dump", "check"))
    parser.add_argument("--profile", choices=PROFILES, default="narrow")
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--binary", action="store_true", help="capture holds raw little-endian words")
    parser.add_argument("--first-id", type=first_id_value, default=0, help="number or 'any'")
    args = parser.parse_args()
    if args.command == "check" and args.expected is None:
        parser.error("check requires --expected")
    mode = "rb" if args.binary else "r"
    open_options = {} if args.binary else {"encoding": "ascii"}
    try:
        with args.capture.open(mode, **open_options) as source:
            records = frame_records(file_words(source, args.binary), PROFILES[args.profile], args.first_id)
            if args.command == "check":
                with args.expected.open(encoding="ascii") as expected:
                    count, statuses = check_trace(records, expected_rows(expected))
                coverage = ", ".join(f"{name}={total}" for name, total in sorted(statuses.items()))
                print(f"PASS RSK1 audit: {count} CRC-checked, sequenced decisions ({coverage})")
            else:
                for record in records:
                    print(json.dumps({"frame_id": record.frame_id, **asdict(record.decision)}))
    except (CaptureError, OSError, UnicodeError) as exc:
        print(f"audit: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
