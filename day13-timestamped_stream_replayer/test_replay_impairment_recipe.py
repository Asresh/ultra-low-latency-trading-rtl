"""Check policy validation, deterministic replay actions, and audit completeness."""

from dataclasses import replace
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from replay_impairment_audit import complete, decode, read_audits
from replay_impairment_model import ActionRecord, Geometry
from replay_impairment_recipe import Action, Control, Match, Operation, Recipe, Rule, Verdict
from replay_impairment_recipe import encode_commands, load_recipe, resolve
from replay_program import Packet, ReplayError, Tape, U64


class RecipeTest(unittest.TestCase):
    def test_strict_geometry_and_action_validation(self):
        for fields in (
            [{"action": "mutate"}], [{"delay": True}], [{"first": 3, "last": 2}],
            [{"action": "corrupt"}], [{"bank": 2}], [{"run": 32}], [{"expire": 9}],
        ):
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(ReplayError, "rule 0"):
                    Recipe.parse({"version": 1, "seed": 1, "rules": fields}, 4, 5)
        with self.assertRaisesRegex(ReplayError, "geometry"):
            Recipe.parse({"version": 1, "seed": 1, "rules": [{"tag": 1}, {"tag": 2}]}, 1, 5)

    def test_json_rejects_duplicate_field(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "recipe.json"
            path.write_text('{"version":1,"seed":0,"rules":[{"tag":1,"tag":2}]}', encoding="ascii")
            with self.assertRaisesRegex(ReplayError, "duplicate JSON field tag"):
                load_recipe(path, 4, 5)

    def test_clear_stage_and_one_commit_with_revision(self):
        plan = Recipe.parse({"version": 1, "seed": "0x3142", "rules": [{"tag": "0x12", "action": "drop"}]})
        commands = tuple(plan.commands(5))
        self.assertEqual(tuple(cmd.op for cmd in commands), (Operation.CLEAR, Operation.WRITE, Operation.COMMIT))
        self.assertEqual(tuple(cmd.revision for cmd in commands), (5, 5, 5))
        self.assertEqual(commands[-1], Control(Operation.COMMIT, 5, seed=0x3142))
        self.assertEqual(int(tuple(encode_commands(plan, 5))[1]["rule"], 16), commands[1].rule.word(32))

    def test_priority_and_rule_superset_detection(self):
        older = Rule(tag=0x80, tag_mask=0xF0, bank=1, first=2, last=9, action=Action.DROP)
        contained = Rule(tag=0x81, tag_mask=0xFF, bank=1, first=3, last=7)
        separate_run = replace(contained, tag=0x71)
        recipe = Recipe(0, (older, contained, separate_run), 4, 4)
        self.assertEqual(recipe.shadowed(), ((1, 0),))
        self.assertEqual(resolve(recipe.rules, Match(1, 1, 0x81, 1, 6), 2, 0).action, Action.DROP)
        self.assertEqual(resolve(recipe.rules, Match(1, 0, 0x81, 1, 6), 2, 0).slot, 3)

    def test_predictions_repeatable_without_a_wall_clock(self):
        plan = Recipe.parse({"version": 1, "seed": 8, "rules": [
            {"tag": 1, "action": "duplicate", "delay": 11, "jitter_mask": 31, "copy_gap": 5},
            {"tag": 2, "action": "corrupt", "flip_offset": 8, "flip_mask": 128},
        ]})
        tape = Tape.from_packets((Packet.paced(1, b"demo", 0), Packet.paced(2, b"01234567X", 9),
                                  Packet.paced(2, b"short", 14)))
        actual = list(plan.predict(tape, 4, 1, 2))
        self.assertEqual(actual, list(plan.predict(tape, 4, 1, 2)))
        self.assertEqual([row["ordinal"] for row in actual], list(range(6)))
        self.assertEqual(actual[0]["payloads"], ["64656d6f", "64656d6f"])
        self.assertEqual(actual[1]["payloads"], ["3031323334353637d8"])
        self.assertEqual(actual[2]["verdict"], "flip_miss")
        self.assertEqual(actual[3]["pass"], 2)
        self.assertLessEqual(actual[0]["delay_from_ingress"], 42)


class AuditTest(unittest.TestCase):
    def fixture(self):
        geometry = Geometry()
        scope = Match(20, 1, 800, 5, 7)
        policy = resolve((Rule(action=Action.CORRUPT, delay=15, flip_offset=3, flip_mask=0xff),),
                         scope, 4, 198)
        return geometry, ActionRecord(scope, policy, U64-2, U64-1, Verdict.CORRUPTED, 100, 0)

    def test_decode_saturated_tick_and_sequence(self):
        g, audit = self.fixture()
        line = f"0 {audit.word(g):x} # checked record\n"
        item, = read_audits(StringIO(line), g.rules, g.run_width, g.revision_width)
        self.assertEqual((item.arrival, item.terminal, item.eligible), (U64-2, U64-1, U64))
        self.assertEqual((item.action, item.verdict, item.size), (Action.CORRUPT, Verdict.CORRUPTED, 100))
        self.assertEqual(tuple(complete([item], 1, 0)), (item,))

    def test_audit_tail_cannot_be_silently_missing(self):
        g, audit = self.fixture()
        item = decode(0, audit.word(g), g.rules, g.run_width, g.revision_width)
        for entries, cursor, lost, label in (
            ([item], 2, 0, "cursor 1"), ([item, item], 2, 0, "sequence 1"),
            ([item], 1, 1, "reported losses"),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ReplayError, label):
                    tuple(complete(entries, cursor, lost))


if __name__ == "__main__":
    unittest.main()
