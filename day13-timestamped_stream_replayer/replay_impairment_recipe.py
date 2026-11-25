"""Compile deterministic replay impairment recipes and predict packet decisions."""

import argparse
from dataclasses import dataclass
from enum import IntEnum
import json
from pathlib import Path
from typing import Optional, Tuple

from replay_format import read_tape
from replay_program import ReplayError, U16, U32, U64


class Action(IntEnum):
    PASS = 0
    DROP = 1
    DUPLICATE = 2
    CORRUPT = 3


class Operation(IntEnum):
    WRITE = 0
    READ = 1
    CLEAR = 2
    COMMIT = 3


class Status(IntEnum):
    OK = 0
    STALE = 1
    SLOT_RANGE = 2
    BAD_RULE = 3
    EXHAUSTED = 4


class Verdict(IntEnum):
    FORWARD = 0
    DROPPED = 1
    DUPLICATED = 2
    CORRUPTED = 3
    FRAMING = 4
    CAPACITY = 5
    TRUNCATED = 6
    FLIP_MISS = 7


def number(value, width, label):
    if type(value) is str:
        try:
            value = int(value, 0)
        except ValueError as error:
            raise ReplayError(f"{label} is not an integer") from error
    if type(value) is not int or not 0 <= value < 1 << width:
        raise ReplayError(f"{label} must fit in {width} unsigned bits")
    return value


def packed(*fields):
    word = 0
    for value, width in fields:
        integer = int(value)
        if not 0 <= integer < 1 << width:
            raise ReplayError(f"packed field {integer} exceeds {width} unsigned bits")
        word = word << width | integer
    return word


def xorshift32(value):
    value ^= value << 13 & U32
    value ^= value >> 17
    return (value ^ (value << 5)) & U32


@dataclass(frozen=True)
class Match:
    run: int
    bank: int
    tag: int
    pass_id: int
    ordinal: int

    def jitter_source(self, seed):
        bank_mix = 0x9E3779B9 if self.bank else 0
        multiplexed = self.tag << 16 | self.pass_id
        return xorshift32(seed ^ (self.run & U32) ^ multiplexed ^ self.ordinal ^ bank_mix)


@dataclass(frozen=True)
class Rule:
    enabled: bool = True
    tag: int = 0
    tag_mask: int = 0
    run: Optional[int] = None
    bank: Optional[int] = None
    first: int = 0
    last: int = U32
    action: Action = Action.PASS
    delay: int = 0
    jitter_mask: int = 0
    copy_gap: int = 0
    flip_offset: int = 0
    flip_mask: int = 0

    @classmethod
    def parse(cls, row, run_width):
        if not isinstance(row, dict):
            raise ReplayError("rule must be an object")
        allowable = {
            "enabled", "tag", "tag_mask", "run", "bank", "first", "last", "action", "delay",
            "jitter_mask", "copy_gap", "flip_offset", "flip_mask",
        }
        if set(row) - allowable:
            raise ReplayError(f"unknown rule fields {sorted(set(row) - allowable)}")
        enabled = row.get("enabled", True)
        if type(enabled) is not bool:
            raise ReplayError("enabled must be a Boolean")
        try:
            action = Action[row.get("action", "pass").upper()]
        except (AttributeError, KeyError) as error:
            raise ReplayError("action must be pass, drop, duplicate, or corrupt") from error
        tag_mask = U16 if "tag" in row else 0
        kwargs = {
            name: number(row.get(name, default), width, name)
            for name, width, default in (
                ("tag", 16, 0), ("tag_mask", 16, tag_mask), ("first", 32, 0), ("last", 32, U32),
                ("delay", 32, 0), ("jitter_mask", 8, 0), ("copy_gap", 32, 0),
                ("flip_offset", 16, 0), ("flip_mask", 8, 0),
            )
        }
        for name, width in (("run", run_width), ("bank", 1)):
            kwargs[name] = None if row.get(name) is None else number(row[name], width, name)
        result = cls(enabled=enabled, action=action, **kwargs)
        if not result.valid:
            raise ReplayError("enabled rule needs an ordered ordinal range and a nonzero corruption mask")
        return result

    @property
    def valid(self):
        return not self.enabled or self.first <= self.last and (
            self.action != Action.CORRUPT or self.flip_mask != 0
        )

    def word(self, run_width):
        return packed(
            (self.enabled, 1), (self.tag, 16), (self.tag_mask, 16), (self.run is None, 1),
            (self.run or 0, run_width), (self.bank is None, 1), (self.bank or 0, 1),
            (self.first, 32), (self.last, 32), (self.action, 2), (self.delay, 32),
            (self.jitter_mask, 8), (self.copy_gap, 32), (self.flip_offset, 16), (self.flip_mask, 8),
        )

    def matches(self, frame):
        return (
            self.enabled and (frame.tag & self.tag_mask) == (self.tag & self.tag_mask) and
            self.run in (None, frame.run) and self.bank in (None, frame.bank) and
            self.first <= frame.ordinal <= self.last
        )

    def dominates(self, later):
        """Check whether every frame matched by a later rule already matches this one."""
        return (
            self.enabled and later.enabled and self.first <= later.first <= later.last <= self.last and
            self.run in (None, later.run) and self.bank in (None, later.bank) and
            (self.tag_mask & later.tag_mask) == self.tag_mask and
            ((self.tag ^ later.tag) & self.tag_mask) == 0
        )


@dataclass(frozen=True)
class Control:
    op: Operation
    revision: int = 0
    slot: int = 0
    active: bool = False
    seed: int = 0
    rule: Optional[Rule] = None


@dataclass(frozen=True)
class Selected:
    slot: int
    revision: int
    action: Action
    delay: int
    copy_gap: int
    flip_offset: int
    flip_mask: int

    def mutate(self, payload):
        if self.action == Action.DROP:
            return Verdict.DROPPED, ()
        if self.action == Action.DUPLICATE:
            return Verdict.DUPLICATED, (payload, payload)
        if self.action == Action.CORRUPT:
            if self.flip_offset >= len(payload):
                return Verdict.FLIP_MISS, (payload,)
            changed = bytearray(payload)
            changed[self.flip_offset] ^= self.flip_mask
            return Verdict.CORRUPTED, (bytes(changed),)
        return Verdict.FORWARD, (payload,)


def resolve(rules, frame, revision, seed):
    for index, rule in enumerate(rules):
        if rule is not None and rule.matches(frame):
            delay = rule.delay + (frame.jitter_source(seed) & rule.jitter_mask)
            return Selected(index, revision, rule.action, delay, rule.copy_gap,
                            rule.flip_offset, rule.flip_mask)
    return Selected(len(rules), revision, Action.PASS, 0, 0, 0, 0)


@dataclass(frozen=True)
class Recipe:
    seed: int
    rules: Tuple[Rule, ...]
    slots: int
    run_width: int

    @classmethod
    def parse(cls, document, slots=4, run_width=32):
        if slots < 1 or not 1 <= run_width <= 32:
            raise ReplayError("rule capacity must be positive and run width must be 1–32 bits")
        if not isinstance(document, dict) or set(document) != {"version", "seed", "rules"}:
            raise ReplayError("recipe requires the version, seed and rules fields")
        if type(document["version"]) is not int or document["version"] != 1:
            raise ReplayError("unsupported recipe version")
        if not isinstance(document["rules"], list) or len(document["rules"]) > slots:
            raise ReplayError("rules must be a list that fits the target geometry")
        seed = number(document["seed"], 32, "seed")
        rules = []
        for index, row in enumerate(document["rules"]):
            try:
                rules.append(Rule.parse(row, run_width))
            except ReplayError as error:
                raise ReplayError(f"rule {index}: {error}") from error
        return cls(seed, tuple(rules), slots, run_width)

    def shadowed(self):
        output = []
        for index, rule in enumerate(self.rules):
            for prior, earlier in enumerate(self.rules[:index]):
                if earlier.dominates(rule):
                    output.append((index, prior))
                    break
        return tuple(output)

    def commands(self, revision):
        yield Control(Operation.CLEAR, revision)
        for slot, rule in enumerate(self.rules):
            yield Control(Operation.WRITE, revision, slot, rule=rule)
        yield Control(Operation.COMMIT, revision, seed=self.seed)

    def predict(self, tape, run, bank, passes=1, first_revision=1):
        number(run, self.run_width, "run")
        number(bank, 1, "bank")
        if not 1 <= passes <= U16:
            raise ReplayError("pass count must be positive and fit sixteen bits")
        table = self.rules + (None,) * (self.slots - len(self.rules))
        ordinal = 0
        for pass_id in range(1, passes + 1):
            for packet in tape.packets():
                scope = Match(run, bank, packet.tag, pass_id, ordinal)
                selected = resolve(table, scope, first_revision, self.seed)
                verdict, copies = selected.mutate(packet.payload)
                yield {
                    "ordinal": ordinal,
                    "revision": selected.revision,
                    "pass": pass_id,
                    "tag": packet.tag,
                    "relative_due": packet.start,
                    "rule_slot": None if selected.slot == self.slots else selected.slot,
                    "action": selected.action.name.lower(),
                    "verdict": verdict.name.lower(),
                    "delay_from_ingress": selected.delay,
                    "copy_gap": selected.copy_gap,
                    "payloads": [payload.hex() for payload in copies],
                }
                ordinal = min(U32, ordinal + 1)


def unique_object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ReplayError(f"duplicate JSON field {key}")
        output[key] = value
    return output


def load_recipe(path, slots, run_width):
    document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    return Recipe.parse(document, slots, run_width)


def encode_commands(recipe, revision):
    for command in recipe.commands(revision):
        yield {
            "op": int(command.op),
            "expected_revision": command.revision,
            "slot": command.slot,
            "seed": command.seed,
            "rule": format(command.rule.word(recipe.run_width) if command.rule else 0, "x"),
        }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("recipe", type=Path)
    cli.add_argument("--rules", type=int, default=4)
    cli.add_argument("--run-width", type=int, default=32)
    commands = cli.add_subparsers(dest="op", required=True)
    compile_cmd = commands.add_parser("compile", help="emit shadow-table writes and one atomic commit")
    compile_cmd.add_argument("--revision", type=lambda text: number(text, 32, "revision"), default=0)
    predict_cmd = commands.add_parser("predict", help="predict recipe actions on an existing TRP packet tape")
    predict_cmd.add_argument("tape", type=Path)
    predict_cmd.add_argument("--run", type=int, default=1)
    predict_cmd.add_argument("--bank", type=int, default=0)
    predict_cmd.add_argument("--passes", type=int, default=1)
    args = cli.parse_args()
    try:
        recipe = load_recipe(args.recipe, args.rules, args.run_width)
        if args.op == "compile":
            output = {"commands": list(encode_commands(recipe, args.revision)),
                      "shadowed_rules": recipe.shadowed()}
        else:
            output = list(recipe.predict(read_tape(args.tape), args.run, args.bank, args.passes))
        print(json.dumps(output, indent=2))
    except (ReplayError, OSError, ValueError) as error:
        cli.exit(1, f"{cli.prog}: {error}\n")


if __name__ == "__main__":
    main()
