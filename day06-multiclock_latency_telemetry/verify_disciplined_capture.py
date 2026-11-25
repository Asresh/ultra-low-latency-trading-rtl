"""Reconstruct physical PPS bench time by integrating the actual update audits."""

import argparse
from pathlib import Path

from pps_discipline_audit import Code, rate_epochs, read_audits
from pps_discipline_model import INITIAL


def verify(directory):
    metadata = directory / "pps-discipline-audit-meta.txt"
    lost, cursor = map(int, metadata.read_text(encoding="ascii").split())
    with (directory / "pps-discipline-audits.txt").open(encoding="ascii") as source:
        audits = tuple(read_audits(source))
    kinds = {audit.code for audit in audits}
    if not {Code.EARLY, Code.STALE_SAMPLE, Code.JITTER, Code.APPLIED}.issubset(kinds):
        raise ValueError("live PPS trace is missing a required fault or correction")
    mapping = rate_epochs(audits, 0, INITIAL, lost, cursor)
    periods = tuple(epoch.word for epoch in mapping.epochs)
    changes = tuple(right - left for left, right in zip(periods, periods[1:]))
    if sum(step > 0 for step in changes) < 2 or sum(step < 0 for step in changes) < 2:
        raise ValueError("live capture does not exercise both oscillator drift directions")
    if any(abs(step) > 1 << 28 for step in changes):
        raise ValueError("applied update exceeds the bench policy's slew limit")
    final_tick = 0
    with (directory / "pps-discipline-clock.txt").open(encoding="ascii") as source:
        for line in source:
            tick_text, time_text = line.split()
            tick, hardware = int(tick_text, 10), int(time_text, 16)
            if tick != final_tick + 1:
                raise ValueError(f"capture lost an accumulator observation at tick {tick}")
            if mapping.integrated(tick) != hardware:
                raise ValueError(f"clock interval starting at or before tick {tick} used the wrong rate")
            final_tick = tick
    if final_tick <= mapping.epochs[-1].tick:
        raise ValueError("capture ended before the final proposed period could be used")
    return final_tick, len(mapping.epochs), len(audits)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        ticks, epochs, audits = verify(args.directory)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(f"PASS integrated PPS capture: {ticks} exact ticks, {epochs} clock epochs, {audits} audit records")


if __name__ == "__main__":
    main()
