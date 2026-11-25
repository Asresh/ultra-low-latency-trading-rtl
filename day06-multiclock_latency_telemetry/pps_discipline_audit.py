"""Decode PPS servo audit words and produce loss-checked clock-rate epochs."""

import argparse
from dataclasses import asdict, dataclass
from enum import IntEnum
import json
from pathlib import Path
from typing import Iterable, Iterator

from latency_units import RateEpoch, RateMap
from latency_reference import LIMIT32, LIMIT64


class Code(IntEnum):
    DISABLED = 0
    STALE_SAMPLE = 1
    SEED = 2
    REVERSE = 3
    EARLY = 4
    LATE = 5
    WARMUP = 6
    JITTER = 7
    QUORUM = 8
    MEASURE = 9
    RATE_RANGE = 10
    UNCHANGED = 11
    APPLIED = 12
    ABANDONED = 13


class Mode(IntEnum):
    OFF = 0
    ACQUIRE = 1
    LOCKED = 2
    HOLDOVER = 3


@dataclass(frozen=True)
class Audit:
    revision: int
    code: Code
    mode: Mode
    tick: int
    sample_tick: int
    delta: int
    target: int
    period: int
    sequence: int = 0

    def word(self):
        return ((int(self.code) << 2 | int(self.mode)) << 320 |
                self.tick << 256 | self.sample_tick << 192 | self.delta << 128 |
                self.target << 64 | self.period)

    def json_row(self):
        return dict(asdict(self), code=self.code.name, mode=self.mode.name)


def decode(sequence: int, revision: int, value: int) -> Audit:
    if not 0 <= sequence < LIMIT64 or revision < 0 or value < 0 or value >= 1 << 326:
        raise ValueError("sequence, revision or audit word is outside its unsigned geometry")
    values = [(value >> shift) & LIMIT64 for shift in (256, 192, 128, 64, 0)]
    audit = Audit(revision, Code(value >> 322), Mode((value >> 320) & 3), *values, sequence)
    if audit.period == 0:
        raise ValueError("active audit period is zero")
    if audit.code in (Code.APPLIED, Code.UNCHANGED) and (audit.target == 0 or audit.delta == 0):
        raise ValueError("successful arithmetic audit lacks a target or measurement")
    if audit.code == Code.UNCHANGED and audit.period != audit.target:
        raise ValueError("unchanged period differs from the target")
    if audit.code < Code.RATE_RANGE and audit.target:
        raise ValueError("PPS observation unexpectedly contains a computed target")
    return audit


def read_audits(source: Iterable[str]) -> Iterator[Audit]:
    for number, line in enumerate(source, 1):
        fields = line.partition("#")[0].split()
        if not fields:
            continue
        try:
            if len(fields) != 3:
                raise ValueError("expected decimal sequence, decimal revision and hex audit word")
            yield decode(int(fields[0], 10), int(fields[1], 10), int(fields[2], 16))
        except ValueError as error:
            raise ValueError(f"line {number}: {error}") from error


def rate_epochs(audits, initial_tick, initial_period, lost_audits, audit_cursor):
    if lost_audits != 0:
        raise ValueError("a dropped audit could conceal a period update")
    if not 0 <= initial_tick <= LIMIT64 or not 0 < initial_period <= LIMIT64:
        raise ValueError("initial clock epoch must have an unsigned tick and positive period")

    def epoch(tick, period):
        return RateEpoch(tick, period >> 32, period & LIMIT32)

    output = [epoch(initial_tick, initial_period)]
    expected = 0
    for audit in audits:
        if audit.sequence != expected:
            raise ValueError(f"missing or duplicate audit at sequence {expected}")
        expected += 1
        if audit.code == Code.APPLIED:
            if audit.period == output[-1].word:
                raise ValueError("applied audit did not change the period")
            output.append(epoch(audit.tick, audit.period))
    if expected != audit_cursor:
        raise ValueError(f"capture ended at sequence {expected}, expected cursor {audit_cursor}")
    return RateMap(output)


def unsigned_literal(value):
    try:
        parsed = int(value, 0)
        if not 0 <= parsed <= LIMIT64:
            raise ValueError("outside 64-bit unsigned range")
        return parsed
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="operation", required=True)
    decode_cmd = subs.add_parser("decode", help="write tagged audit records as JSON Lines")
    decode_cmd.add_argument("capture", type=Path)
    epochs_cmd = subs.add_parser("epochs", help="rebuild the verified piecewise rate history")
    epochs_cmd.add_argument("capture", type=Path)
    epochs_cmd.add_argument("--initial-tick", type=unsigned_literal, default=0)
    epochs_cmd.add_argument("--initial-period", type=unsigned_literal, required=True)
    epochs_cmd.add_argument("--lost-audits", type=unsigned_literal, required=True)
    epochs_cmd.add_argument("--audit-cursor", type=unsigned_literal, required=True)
    args = parser.parse_args()
    try:
        with args.capture.open(encoding="ascii") as source:
            records = read_audits(source)
            if args.operation == "decode":
                for audit in records:
                    print(json.dumps(audit.json_row(), separators=(",", ":")))
            else:
                clock = rate_epochs(records, args.initial_tick, args.initial_period, args.lost_audits,
                                    args.audit_cursor)
                print(json.dumps([asdict(epoch) for epoch in clock.epochs], indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
