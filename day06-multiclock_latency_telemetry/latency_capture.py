"""Check and decode the FPGA telemetry wire words from offline captures."""

import argparse
import csv
from dataclasses import asdict, dataclass
from fractions import Fraction
import json
from pathlib import Path
import sys

from latency_reference import (
    HALF64, LIMIT32, LIMIT64, Report, Status, bucket_bounds, bucket_of, modular_delta,
)

HIST_MAGIC = 0x4C545331
TRACE_MAGIC = 0x4C545231
WORD_WIDTHS = {"event": 320, "trace": 320, "histogram": 128}


class CaptureError(ValueError):
    """Invalid transport words or truncated frames."""


def bitfield(value, offset, width):
    return (value >> offset) & ((1 << width) - 1)


def read_words(stream, width, raw=False):
    nbytes = (width + 7) // 8
    if raw:
        index = 0
        while True:
            data = stream.read(nbytes)
            if not data:
                break
            if len(data) != nbytes:
                raise CaptureError(f"incomplete binary word at offset {index * nbytes}")
            yield int.from_bytes(data, "little")
            index += 1
        return
    nchars = 2 * nbytes
    allowed = frozenset("0123456789abcdefABCDEF")
    for row, text in enumerate(stream, 1):
        token = text.partition("#")[0].strip()
        if not token:
            continue
        token = token.removeprefix("0x").removeprefix("0X")
        if len(token) != nchars or set(token) - allowed:
            raise CaptureError(f"invalid {width}-bit word on line {row}")
        yield int(token, 16)


class WordStream:
    def __init__(self, words):
        self.words = iter(words)
        self.offset = 0

    def start(self):
        try:
            word = next(self.words)
        except StopIteration:
            return None
        self.offset += 1
        return word

    def take(self, label):
        value = self.start()
        if value is None:
            raise CaptureError(f"unexpected EOF while reading {label} at word {self.offset}")
        return value


class FrameSequence:
    def __init__(self, first_id):
        self.next_id = first_id

    def consume(self, current):
        if self.next_id is not None and current != self.next_id:
            raise CaptureError(f"frame sequence {current}, expected {self.next_id}")
        self.next_id = (current + 1) & LIMIT32


def read_event(word):
    status = Status(bitfield(word, 317, 3))
    if bitfield(word, 256, 21):
        raise CaptureError("nonzero reserved bits in event")
    record = Report(
        status=status,
        tag=bitfield(word, 293, 16),
        event_class=bitfield(word, 309, 8),
        start_probe=bitfield(word, 285, 8),
        end_probe=bitfield(word, 277, 8),
        start_tick=bitfield(word, 192, 64),
        end_tick=bitfield(word, 128, 64),
        duration=bitfield(word, 64, 64),
        observed_tick=bitfield(word, 0, 64),
    )
    if record.status == Status.MATCH:
        actual = modular_delta(record.end_tick, record.start_tick)
        if actual >= HALF64 or actual != record.duration:
            raise CaptureError(f"impossible pair delta for tag {record.tag}")
    elif record.duration != 0:
        raise CaptureError(f"non-match delay for tag {record.tag}")
    if record.status == Status.NEGATIVE and modular_delta(record.end_tick, record.start_tick) < HALF64:
        raise CaptureError(f"nonnegative delay marked negative for tag {record.tag}")
    return record


@dataclass(frozen=True)
class HistogramClass:
    number: int
    bucket_counts: tuple
    counter_bits: int
    overflow: bool
    count: int
    total: int
    minimum: int
    maximum: int

    def validate(self):
        cap = (1 << self.counter_bits) - 1
        if self.count > cap or max(self.bucket_counts, default=0) > cap:
            raise CaptureError(f"class {self.number}: counter exceeds bit width")
        if self.count == 0:
            if self.total or self.minimum or self.maximum or any(self.bucket_counts) or self.overflow:
                raise CaptureError(f"class {self.number}: inconsistent empty class")
            return
        if self.minimum > self.maximum:
            raise CaptureError(f"class {self.number}: invalid bounds")
        if self.total < self.minimum:
            raise CaptureError(f"class {self.number}: sum smaller than minimum")
        bins_observed = [index for index, count in enumerate(self.bucket_counts) if count]
        bins = len(self.bucket_counts)
        if not bins_observed or bins_observed[0] != bucket_of(self.minimum, bins):
            raise CaptureError(f"class {self.number}: minimum is outside first populated bin")
        if bins_observed[-1] != bucket_of(self.maximum, bins):
            raise CaptureError(f"class {self.number}: maximum is outside last populated bin")
        if not self.overflow:
            if sum(self.bucket_counts) != self.count:
                raise CaptureError(f"class {self.number}: bucket count disagrees with total")
            if not self.count * self.minimum <= self.total <= self.count * self.maximum:
                raise CaptureError(f"class {self.number}: sum is outside sample bounds")

    def percentile_interval(self, numerator, denominator=100):
        if not 0 < numerator <= denominator:
            raise ValueError("quantile must be in (0, 1]")
        # Once either a bucket or the total saturates, rank is unknown.
        if self.count == 0 or self.overflow:
            return None
        wanted = (self.count * numerator + denominator - 1) // denominator
        cumulative = 0
        for index, count in enumerate(self.bucket_counts):
            cumulative += count
            if cumulative >= wanted:
                lo, hi = bucket_bounds(index, len(self.bucket_counts))
                return max(lo, self.minimum), min(hi, self.maximum)
        raise CaptureError("histogram rank missing")

    def mean(self):
        if self.count and not self.overflow:
            return Fraction(self.total, self.count)
        return None

    def summarize(self):
        mean = self.mean()
        return {
            "class": self.number,
            "count": self.count,
            "min_ticks": self.minimum,
            "max_ticks": self.maximum,
            "mean_ticks": float(mean) if mean is not None else None,
            "p50_ticks_interval": self.percentile_interval(50),
            "p99_ticks_interval": self.percentile_interval(99),
            "overflow": self.overflow,
        }


@dataclass(frozen=True)
class HistogramFrame:
    frame_id: int
    cut_tick: int
    groups: tuple


@dataclass(frozen=True)
class TraceFrame:
    frame_id: int
    trigger_tick: int
    trigger_offset: int
    trigger_status: Status
    threshold: bool
    records: tuple


def parse_histogram_class(stream, index, word):
    if bitfield(word, 124, 4) != 1 or bitfield(word, 116, 8) != index:
        raise CaptureError(f"expected class {index} totals at word {stream.offset}")
    bin_count = bitfield(word, 108, 8)
    width = bitfield(word, 100, 8)
    overflow = bool(bitfield(word, 99, 1))
    if not 5 <= bin_count <= 64 or not 1 <= width <= 32 or bitfield(word, 96, 3):
        raise CaptureError("invalid class summary geometry or reserved bits")
    b = stream.take("class bounds")
    if bitfield(b, 124, 4) != 2 or bitfield(b, 116, 8) != index:
        raise CaptureError(f"expected class {index} bounds at word {stream.offset}")
    minimum = bitfield(b, 0, 64)
    max_hi = bitfield(b, 64, 52)
    max_low = 0
    counters = []
    for bucket_no in range(bin_count):
        k = stream.take("bucket count")
        if bitfield(k, 124, 4) != 3 or bitfield(k, 116, 8) != index or bitfield(k, 108, 8) != bucket_no:
            raise CaptureError(f"expected class {index}, bin {bucket_no} at word {stream.offset}")
        tail = bitfield(k, 96, 12)
        if bitfield(k, 32, 64) or (bucket_no and tail != 0):
            raise CaptureError("invalid histogram bucket reserved bits")
        if bucket_no == 0:
            max_low = tail
        counters.append(bitfield(k, 0, 32))
    group = HistogramClass(
        index, tuple(counters), width, overflow, bitfield(word, 64, 32),
        bitfield(word, 0, 64), minimum, (max_hi << 12) | max_low,
    )
    group.validate()
    return group


def histogram_frames(words, classes, first_id=0):
    if not 1 <= classes <= 256:
        raise ValueError("histogram class count is outside 1..256")
    stream = WordStream(words)
    sequence = FrameSequence(first_id)
    while True:
        head = stream.start()
        if head is None:
            return
        if bitfield(head, 96, 32) != HIST_MAGIC:
            raise CaptureError(f"LTS1 magic missing at word {stream.offset}")
        frame_id = bitfield(head, 64, 32)
        sequence.consume(frame_id)
        groups = []
        for index in range(classes):
            group = parse_histogram_class(stream, index, stream.take("class totals"))
            if groups and (len(group.bucket_counts), group.counter_bits) != (
                len(groups[0].bucket_counts), groups[0].counter_bits
            ):
                raise CaptureError("histogram classes disagree on counter geometry")
            groups.append(group)
        yield HistogramFrame(frame_id, bitfield(head, 0, 64), tuple(groups))


def trace_frames(words, first_id=0):
    stream = WordStream(words)
    sequence = FrameSequence(first_id)
    while True:
        head = stream.start()
        if head is None:
            return
        if head >> 160 or bitfield(head, 128, 32) != TRACE_MAGIC:
            raise CaptureError(f"LTR1 magic missing at word {stream.offset}")
        frame_id = bitfield(head, 96, 32)
        sequence.consume(frame_id)
        trigger_tick = bitfield(head, 32, 64)
        count = bitfield(head, 24, 8)
        offset = bitfield(head, 16, 8)
        status = Status(bitfield(head, 13, 3))
        threshold = bool(bitfield(head, 12, 1))
        if not offset < count <= 128 or bitfield(head, 0, 12):
            raise CaptureError("trace header has invalid count, offset or reserved bits")
        events = tuple(read_event(stream.take("trace record")) for _ in range(count))
        trigger = events[offset]
        if trigger.status != status or trigger.observed_tick != trigger_tick:
            raise CaptureError("trace trigger does not match indexed record")
        if threshold and trigger.status != Status.MATCH:
            raise CaptureError("latency threshold can only trigger on a successful pair")
        for a, b in zip(events, events[1:]):
            if modular_delta(b.observed_tick, a.observed_tick) >= HALF64:
                raise CaptureError("trace record times move backwards")
        yield TraceFrame(frame_id, trigger_tick, offset, status, threshold, events)


def event_json(event):
    payload = asdict(event)
    payload["status"] = event.status.name
    return payload


def format_histogram(frame, detailed):
    payload = {
        "frame": frame.frame_id,
        "cut_tick": frame.cut_tick,
        "classes": [group.summarize() for group in frame.groups],
    }
    if detailed:
        for row, group in zip(payload["classes"], frame.groups):
            row["buckets"] = group.bucket_counts
    return payload


def format_trace(frame):
    return {
        "frame": frame.frame_id,
        "trigger_tick": frame.trigger_tick,
        "trigger_offset": frame.trigger_offset,
        "trigger_status": frame.trigger_status.name,
        "threshold": frame.threshold,
        "records": [event_json(event) for event in frame.records],
    }


def frame_id_value(raw):
    if raw == "any":
        return None
    number = int(raw, 0)
    if not 0 <= number <= LIMIT32:
        raise argparse.ArgumentTypeError("frame id must be unsigned 32-bit")
    return number


def output_events(words, emit_csv, stream):
    writer = None
    count = 0
    for word in words:
        decoded = event_json(read_event(word))
        if emit_csv:
            if writer is None:
                writer = csv.DictWriter(stream, fieldnames=decoded)
                writer.writeheader()
            writer.writerow(decoded)
        else:
            print(json.dumps(decoded, separators=(",", ":")), file=stream)
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("format", choices=WORD_WIDTHS)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--binary", action="store_true", help="fixed-width little-endian words")
    parser.add_argument("--classes", type=int, default=4, help="class count for LTS1")
    parser.add_argument("--first-id", type=frame_id_value, default=0)
    parser.add_argument("--detailed", action="store_true", help="include histogram bucket counts")
    parser.add_argument("--csv", action="store_true", help="write events as CSV")
    args = parser.parse_args()
    if args.csv and args.format != "event":
        parser.error("--csv applies to event mode")
    options = {} if args.binary else {"encoding": "ascii"}
    try:
        with args.capture.open("rb" if args.binary else "r", **options) as inp:
            words = read_words(inp, WORD_WIDTHS[args.format], args.binary)
            if args.format == "event":
                count = output_events(words, args.csv, sys.stdout)
            else:
                if args.format == "histogram":
                    values = (format_histogram(f, args.detailed) for f in
                              histogram_frames(words, args.classes, args.first_id))
                else:
                    values = (format_trace(f) for f in trace_frames(words, args.first_id))
                count = 0
                for value in values:
                    print(json.dumps(value, separators=(",", ":")))
                    count += 1
            if count == 0:
                raise CaptureError("capture was empty")
    except (CaptureError, ValueError, OSError) as exc:
        print(f"latency_capture: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
