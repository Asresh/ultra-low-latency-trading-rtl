"""Decode the deadline monitor's alert stream for a host-side collector."""

import argparse
from dataclasses import asdict, dataclass
import json
import sys
from typing import Iterable, Iterator, Tuple


ALERT_LAYOUT = (
    ("event_class", 8),
    ("tag", 16),
    ("start_probe", 8),
    ("stop_probe", 8),
    ("code", 8),
    ("consecutive", 16),
    ("limit_ticks", 64),
    ("duration_ticks", 64),
    ("processing_tick", 64),
)


def unpack_fields(word: int, layout: Tuple[Tuple[str, int], ...]) -> dict:
    width = sum(bits for _, bits in layout)
    if not 0 <= word < 1 << width:
        raise ValueError(f"value must fit in {width} unsigned bits")
    fields = {}
    for name, bits in reversed(layout):
        fields[name] = word & ((1 << bits) - 1)
        word >>= bits
    return fields


@dataclass(frozen=True)
class DeadlineAlert:
    rule: int
    revision: int
    event_class: int
    tag: int
    start_probe: int
    stop_probe: int
    code: int
    consecutive: int
    limit_ticks: int
    duration_ticks: int
    processing_tick: int

    @property
    def transition(self) -> str:
        return "trip" if self.code == 1 else "recovery"


def decode_alert(rule: int, revision: int, word: int) -> DeadlineAlert:
    if rule < 0 or revision < 1:
        raise ValueError("rule must be nonnegative and revision must be positive")
    fields = unpack_fields(word, ALERT_LAYOUT)
    alert = DeadlineAlert(rule, revision, **fields)
    if alert.code not in (1, 2):
        raise ValueError(f"unknown transition code {alert.code}")
    if alert.consecutive == 0:
        raise ValueError("transition streak must be positive")
    if (alert.duration_ticks > alert.limit_ticks) != (alert.code == 1):
        raise ValueError("duration is inconsistent with transition")
    return alert


def read_alerts(lines: Iterable[str]) -> Iterator[DeadlineAlert]:
    for line_no, line in enumerate(lines, 1):
        tokens = line.partition("#")[0].split()
        if not tokens:
            continue
        try:
            if len(tokens) != 3:
                raise ValueError("expected decimal rule, decimal revision, and hex payload")
            rule, revision, payload = tokens
            yield decode_alert(int(rule, 10), int(revision, 10), int(payload, 16))
        except ValueError as error:
            raise ValueError(f"line {line_no}: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", nargs="?", type=argparse.FileType("r"), default=sys.stdin)
    args = parser.parse_args()
    try:
        for alert in read_alerts(args.capture):
            print(json.dumps(dict(asdict(alert), transition=alert.transition)))
    except ValueError as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
