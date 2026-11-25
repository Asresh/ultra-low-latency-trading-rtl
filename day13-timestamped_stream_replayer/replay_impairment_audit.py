"""Decode impairment decisions and check that a recorded audit is complete."""

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from replay_impairment_recipe import Action, Verdict
from replay_program import ReplayError, U64


@dataclass(frozen=True)
class Decision:
    sequence: int
    bank: int
    run: int
    tag: int
    pass_id: int
    ordinal: int
    revision: int
    rule_slot: int
    action: Action
    verdict: Verdict
    arrival: int
    terminal: int
    eligible: int
    size: int


def decode(sequence, word, rules, run_width, revision_width):
    width = rules.bit_length()
    total = 280 + run_width + revision_width + width
    if not 0 <= sequence < U64 or not 0 <= word < 1 << total:
        raise ReplayError("audit sequence or word exceeds the target geometry")
    layout = (1, run_width, 16, 16, 32, revision_width, width, 2, 4, 64, 64, 64, 17)
    values = []
    for bits in reversed(layout):
        values.append(word & ((1 << bits)-1))
        word >>= bits
    values.reverse()
    values[7] = Action(values[7])
    values[8] = Verdict(values[8])
    record = Decision(sequence, *values)
    if record.rule_slot > rules:
        raise ReplayError("audit refers to a nonexistent rule slot")
    if record.terminal < record.arrival or record.eligible < record.arrival:
        raise ReplayError("audit tick ordering is invalid")
    if record.verdict.value < 4 and record.action.value != record.verdict.value:
        raise ReplayError("audit applied an unexpected action")
    if record.verdict == Verdict.FLIP_MISS and record.action != Action.CORRUPT:
        raise ReplayError("offset miss requires a corruption request")
    return record


def read_audits(source, rules, run_width, revision_width):
    for number, line in enumerate(source, 1):
        fields = line.partition("#")[0].split()
        if not fields:
            continue
        try:
            if len(fields) != 2:
                raise ReplayError("expected decimal sequence and hexadecimal audit")
            yield decode(int(fields[0], 10), int(fields[1], 16), rules, run_width, revision_width)
        except ValueError as error:
            raise ReplayError(f"audit line {number}: {error}") from error


def complete(records, cursor, lost):
    if lost:
        raise ReplayError("hardware audit reported losses")
    if not 0 <= cursor <= U64:
        raise ReplayError("invalid terminal audit cursor")
    observed = 0
    for record in records:
        if record.sequence != observed:
            raise ReplayError(f"audit sequence {observed} is missing or duplicated")
        observed += 1
        yield record
    if observed != cursor:
        raise ReplayError(f"audit ended at cursor {observed}, expected {cursor}")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("capture", type=Path)
    cli.add_argument("--rules", type=int, default=4)
    cli.add_argument("--run-width", type=int, default=32)
    cli.add_argument("--revision-width", type=int, default=16)
    cli.add_argument("--cursor", type=int, required=True)
    cli.add_argument("--lost", type=int, required=True)
    args = cli.parse_args()
    try:
        if args.rules < 1 or not 1 <= args.run_width <= 32 or args.revision_width < 1:
            raise ReplayError("invalid audit geometry")
        with args.capture.open(encoding="ascii") as source:
            records = tuple(complete(read_audits(source, args.rules, args.run_width, args.revision_width),
                                     args.cursor, args.lost))
        for record in records:
            print(json.dumps(dict(asdict(record), action=record.action.name, verdict=record.verdict.name)))
    except (OSError, ValueError) as error:
        cli.exit(1, f"{cli.prog}: {error}\n")


if __name__ == "__main__":
    main()
