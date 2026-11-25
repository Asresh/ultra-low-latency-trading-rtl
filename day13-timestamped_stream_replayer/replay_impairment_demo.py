"""Drive real banked replay from checked TRP images and audit the FPGA impairment results."""

import argparse
import csv
from dataclasses import dataclass
from itertools import zip_longest
import json
from pathlib import Path
from typing import Tuple

from replay_format import atomic_output, read_tape, write_tape
from replay_impairment_audit import complete, read_audits
from replay_impairment_recipe import Action, Verdict, load_recipe
from replay_program import Packet, ReplayError, Tape, U64, lanes_from_keep


RULES, RUN_WIDTH, REVISION_WIDTH, DEPTH = 4, 5, 4, 16
RUNS = ((0, 1, 2), (1, 2, 1))
COLUMNS = ("wall", "bank", "run", "tag", "pass", "first", "last", "data", "keep",
           "due", "offered", "ordinal", "copy", "action", "revision", "eligible")


def demo_seed(directory):
    """Create the complete reproducible software input to the native integration bench."""
    directory.mkdir(parents=True, exist_ok=True)
    recipe = {
        "version": 1,
        "seed": "0xdb70f10a",
        "rules": [
            {"tag": 101, "action": "drop"},
            {"tag": 102, "action": "duplicate", "delay": 6, "jitter_mask": 3, "copy_gap": 4},
            {"tag": 103, "action": "corrupt", "flip_offset": 8, "flip_mask": "0x80"},
            {"bank": 1, "first": 1, "last": 3, "action": "pass", "delay": 10, "jitter_mask": 7},
        ],
    }
    images = (
        Tape.from_packets((
            Packet.paced(101, b"discarded", 0),
            Packet.paced(102, b"replicated source frame", 3),
            Packet.paced(103, bytes(range(20)), 7),
            Packet.paced(103, b"small", 11),
            Packet.paced(105, b"untouched", 15),
        )),
        Tape.from_packets((
            Packet.paced(102, bytes(range(21)), 1),
            Packet.paced(201, b"bank B delayed", 7),
            Packet.paced(101, b"also dropped", 9),
            Packet.paced(202, b"independent run", 12),
        )),
    )
    for (bank, _, _), image in zip(RUNS, images):
        write_tape(image.checked_depth(DEPTH), directory / f"bank-{bank}.trp")
    atomic_output(directory / "recipe.json", "w", lambda target: json.dump(recipe, target, indent=2))


def build(directory, stimulus):
    recipe = load_recipe(directory / "recipe.json", RULES, RUN_WIDTH)
    commands = tuple(recipe.commands(0))
    images = tuple(read_tape(directory / f"bank-{bank}.trp", DEPTH) for bank, _, _ in RUNS)

    def emit(target):
        target.write(f"{len(commands)}\n")
        for command in commands:
            word = command.rule.word(RUN_WIDTH) if command.rule else 0
            target.write(
                f"{int(command.op):x} {command.revision:x} {command.slot:x} {command.seed:x} {word:x}\n"
            )
        for image in images:
            target.write(f"{len(image.events)}\n")
            for beat in image.events:
                target.write(f"{beat.tick:x} {beat.data:x} {beat.keep:x} {int(beat.last):x} {beat.tag:x}\n")

    atomic_output(stimulus, "w", emit)


@dataclass(frozen=True)
class Accepted:
    wall: int
    bank: int
    run: int
    tag: int
    pass_id: int
    first: int
    last: int
    data: int
    keep: int
    due: int
    offered: int
    ordinal: int
    copy: int
    action: int
    revision: int
    eligible: int

    @property
    def payload(self):
        size = lanes_from_keep(self.keep)
        if self.data >> (8*size):
            raise ReplayError("unused output lanes are nonzero")
        return self.data.to_bytes(size, "little")


@dataclass(frozen=True)
class Frame:
    beats: Tuple[Accepted, ...]

    @property
    def first(self):
        return self.beats[0]

    @property
    def last(self):
        return self.beats[-1]

    @property
    def payload(self):
        return b"".join(beat.payload for beat in self.beats)


def recorded_frames(path, output):
    packet = []
    prior = -1
    with path.open(encoding="ascii", newline="") as source:
        csv_file = csv.reader(source)
        if tuple(next(csv_file, ())) != COLUMNS:
            raise ReplayError(f"unexpected transfer CSV header in {path.name}")
        for line, fields in enumerate(csv_file, 2):
            if len(fields) != len(COLUMNS):
                raise ReplayError(f"{path.name}:{line}: incorrect transfer field count")
            beat = Accepted(*(int(field, 16 if name in ("data", "keep") else 10)
                              for name, field in zip(COLUMNS, fields)))
            if beat.wall <= prior or beat.first != int(not packet) or beat.last not in (0, 1):
                raise ReplayError(f"{path.name}:{line}: bad transfer clock or framing")
            if not beat.last and beat.keep != 255:
                raise ReplayError(f"{path.name}:{line}: partial nonfinal beat")
            if packet:
                first = packet[0]
                same_scope = (first.run, first.bank, first.tag, first.pass_id) == (
                    beat.run, beat.bank, beat.tag, beat.pass_id
                )
                same_rule = (first.ordinal, first.copy, first.action, first.revision, first.eligible) == (
                    beat.ordinal, beat.copy, beat.action, beat.revision, beat.eligible
                )
                if not same_scope or (output and not same_rule):
                    raise ReplayError(f"{path.name}:{line}: mid-packet metadata change")
            packet.append(beat)
            prior = beat.wall
            if beat.last:
                yield Frame(tuple(packet))
                packet.clear()
        if packet:
            raise ReplayError(f"{path.name}: capture ended inside a packet")


def check(directory):
    recipe = load_recipe(directory / "recipe.json", RULES, RUN_WIDTH)
    source_frames = iter(recorded_frames(directory / "impairment-source.csv", False))
    output_frames = iter(recorded_frames(directory / "impairment-sink.csv", True))
    cursor, lost = map(int, (directory / "impairment-meta.txt").read_text(encoding="ascii").split())
    with (directory / "impairment-audits.txt").open(encoding="ascii") as source:
        audits = iter(tuple(complete(read_audits(source, RULES, RUN_WIDTH, REVISION_WIDTH), cursor, lost)))

    source_count = output_count = 0
    verdicts = set()
    for bank, run, repeats in RUNS:
        tape = read_tape(directory / f"bank-{bank}.trp", DEPTH)
        inputs = (packet for _ in range(repeats) for packet in tape.packets())
        predictions = recipe.predict(tape, run, bank, repeats)
        for packet, prediction in zip_longest(inputs, predictions):
            frame = next(source_frames, None)
            audit = next(audits, None)
            if packet is None or prediction is None or frame is None or audit is None:
                raise ReplayError("missing source or audit frame")
            first = frame.first
            identity = (bank, run, packet.tag, prediction["pass"])
            if identity != (first.bank, first.run, first.tag, first.pass_id) or frame.payload != packet.payload:
                raise ReplayError("upstream RTL replay disagrees with original tape")
            expected_due = tuple(first.due + tick - packet.start for tick in packet.ticks)
            if tuple(beat.due for beat in frame.beats) != expected_due:
                raise ReplayError("the upstream replay changed per-beat schedule offsets")

            source_count += 1
            delay = prediction["delay_from_ingress"]
            release = min(U64, first.wall + delay)
            verdict = Verdict[prediction["verdict"].upper()]
            action = Action[prediction["action"].upper()]
            rule = prediction["rule_slot"] if prediction["rule_slot"] is not None else RULES
            expected_record = (identity, prediction["ordinal"], first.wall, frame.last.wall,
                               release, verdict, action, rule, len(packet.payload))
            actual_record = ((audit.bank, audit.run, audit.tag, audit.pass_id), audit.ordinal,
                             audit.arrival, audit.terminal, audit.eligible, audit.verdict,
                             audit.action, audit.rule_slot, audit.size)
            if expected_record != actual_record or audit.revision != prediction["revision"]:
                raise ReplayError(f"wrong packet decision at source frame {source_count}")
            verdicts.add(verdict)
            for copy, payload in enumerate(prediction["payloads"]):
                target = next(output_frames, None)
                if target is None:
                    raise ReplayError("missing output copy")
                actual = target.first
                effect = Action.PASS if verdict == Verdict.FLIP_MISS else action
                if (actual.bank, actual.run, actual.tag, actual.pass_id) != identity or (
                    target.payload.hex() != payload or actual.copy != copy or
                    actual.action != effect or actual.revision != prediction["revision"] or actual.ordinal != audit.ordinal
                ):
                    raise ReplayError(f"output copy identity or payload disagrees at frame {source_count}")
                if actual.eligible != release or actual.wall < release:
                    raise ReplayError("output transfer precedes its independently computed release")
                if tuple((b.due, b.offered, b.keep) for b in target.beats) != tuple(
                    (b.due, b.offered, b.keep) for b in frame.beats
                ):
                    raise ReplayError("output changed the upstream beat timing or lane masks")
                release = min(U64, target.last.wall + prediction["copy_gap"] + 1)
                output_count += 1
    if any(next(iterator, None) is not None for iterator in (source_frames, output_frames, audits)):
        raise ReplayError("output stream contains an extra packet or audit")
    if not {Verdict.DROPPED, Verdict.DUPLICATED, Verdict.CORRUPTED, Verdict.FLIP_MISS} <= verdicts:
        raise ReplayError("integration recipe missed a required fault action")
    return source_count, output_count, len(verdicts)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("operation", choices=("seed", "build", "check"))
    cli.add_argument("directory", type=Path)
    args = cli.parse_args()
    try:
        if args.operation == "seed":
            demo_seed(args.directory)
        elif args.operation == "build":
            build(args.directory, args.directory / "commands.txt")
        else:
            sources, copies, verdicts = check(args.directory)
            print(f"PASS banked impairment audit: {sources} original frames, {copies} copies, {verdicts} verdicts")
    except (OSError, ValueError) as error:
        cli.exit(1, f"{cli.prog}: {error}\n")


if __name__ == "__main__":
    main()
