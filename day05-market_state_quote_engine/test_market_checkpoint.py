"""Recovery manifest and snapshot rehydration validation."""

from copy import deepcopy
from dataclasses import replace
from io import StringIO
import unittest

from market_checkpoint import (
    CheckpointError, Image, RestorePlan, assert_plan_rows, digest, from_inventory, parse_anchors,
)
from market_reference import Operation, Order, PROFILES
from market_replay import ReplaySession, fixture, read_rows, write_rows


def inventory_for(cfg):
    runner = ReplaySession(cfg)
    list(runner.expand(fixture(cfg)))
    return {"instruments": runner.inventory()}


class Restore(unittest.TestCase):
    def setUp(self):
        self.profile = "default"
        self.cfg = PROFILES[self.profile]
        self.inventory = inventory_for(self.cfg)
        self.anchors = {0: self.cfg.sequence_max, 1: 384}

    def plan(self, **options):
        return from_inventory(self.inventory, self.profile, self.anchors, **options)

    def test_recovery_rebuilds_two_instruments_and_sequence_wrap(self):
        plan = self.plan()
        result = assert_plan_rows(plan, plan.rows())
        banks = result["restored"]
        self.assertEqual([(bank["instrument"], bank["committed_orders"]) for bank in banks], [(0, 2), (1, 2)])
        self.assertEqual([bank["next_sequence"] for bank in banks], [3, 388])
        self.assertEqual(result["accepts"], 8)
        self.assertTrue(all(bank["bbo"]["bid_quantity"] == 15 for bank in banks))
        self.assertTrue(all(bank["bbo"]["ask_quantity"] == 9 for bank in banks))

    def test_interleaved_seed_preserves_local_command_order(self):
        tape = list(self.plan().commands())
        self.assertEqual([cmd.instrument for cmd in tape], [0, 1]*4)
        self.assertEqual(
            [cmd.op for cmd in tape if cmd.instrument == 0],
            [Operation.SNAP_BEGIN, Operation.SNAP_ADD, Operation.SNAP_ADD, Operation.SNAP_END],
        )
        self.assertEqual([cmd.sequence for cmd in tape if cmd.instrument == 0], [65535, 0, 1, 2])

    def test_grouped_recovery_and_gaps_do_not_change_bbo(self):
        quick = self.plan()
        grouped = self.plan(grouped=True, gap=3)
        self.assertEqual([cmd.instrument for cmd in grouped.commands()], [0]*4 + [1]*4)
        fast_out = assert_plan_rows(quick, quick.rows())
        slow_out = assert_plan_rows(grouped, grouped.rows())
        self.assertEqual(fast_out["restored"], slow_out["restored"])
        self.assertGreater(slow_out["cycles"], fast_out["cycles"])

    def test_partial_recovery_does_not_seed_unspecified_bank(self):
        plan = from_inventory(self.inventory, self.profile, {1: 6})
        self.assertEqual([cmd.instrument for cmd in plan.commands()], [1, 1, 1, 1])
        result = assert_plan_rows(plan, plan.rows())
        self.assertEqual([row["instrument"] for row in result["restored"]], [1])
        self.assertEqual(result["restored"][0]["next_sequence"], 10)

    def test_empty_image_reaches_live_but_remains_ineligible(self):
        image = Image(0, 4, 123, ())
        plan = RestorePlan(self.profile, (image,))
        result = assert_plan_rows(plan, plan.rows())["restored"][0]
        self.assertEqual((result["next_sequence"], result["committed_orders"]), (125, 0))
        self.assertFalse(result["eligible"])
        self.assertFalse(result["bbo"]["bid_present"] or result["bbo"]["ask_present"])

    def test_manifest_round_trip_and_canonical_order(self):
        original = self.plan()
        manifest = original.manifest()
        self.assertEqual(RestorePlan.read_manifest(manifest), original)
        self.assertEqual(manifest["digest"], digest(manifest["data"]))
        reversed_input = deepcopy(self.inventory)
        reversed_input["instruments"].reverse()
        for image in reversed_input["instruments"]:
            image["orders"].reverse()
        rebuilt = from_inventory(reversed_input, self.profile, self.anchors)
        self.assertEqual(rebuilt.manifest(), manifest)

    def test_csv_transport_preserves_every_restore_field(self):
        plan = self.plan()
        transcript = StringIO()
        write_rows(transcript, plan.rows())
        transcript.seek(0)
        result = assert_plan_rows(plan, read_rows(transcript, self.cfg))
        self.assertEqual(result["accepts"], 8)

    def test_missing_order_snapshot_add_is_not_accepted_as_smaller_image(self):
        plan = self.plan()
        rows = list(plan.rows())
        slot_add = next(i for i, row in enumerate(rows) if row.command and row.command.op == Operation.SNAP_ADD)
        del rows[slot_add]
        with self.assertRaisesRegex(CheckpointError, "expected"):
            assert_plan_rows(plan, rows)

    def test_altered_order_field_is_detected_by_exact_tape_check(self):
        plan = self.plan()
        rows = list(plan.rows())
        slot_add = next(i for i, row in enumerate(rows) if row.command and row.command.op == Operation.SNAP_ADD)
        entry = rows[slot_add]
        rows[slot_add] = replace(entry, command=replace(entry.command, quantity=33))
        with self.assertRaisesRegex(CheckpointError, "diverges"):
            assert_plan_rows(plan, rows)

    def test_manifest_rejects_changed_digest_and_unverified_expected_values(self):
        manifest = self.plan().manifest()
        corrupted = deepcopy(manifest)
        corrupted["data"]["images"][0]["orders"][0]["size"] = 90
        with self.assertRaisesRegex(CheckpointError, "digest"):
            RestorePlan.read_manifest(corrupted)
        corrupted = deepcopy(manifest)
        corrupted["expected"][0]["next_sequence"] += 1
        with self.assertRaisesRegex(CheckpointError, "expected outcomes"):
            RestorePlan.read_manifest(corrupted)

    def test_reject_duplicate_inventory_and_missing_bank(self):
        bad = deepcopy(self.inventory)
        bad["instruments"].append(bad["instruments"][0])
        with self.assertRaisesRegex(CheckpointError, "duplicate inventory"):
            from_inventory(bad, self.profile, self.anchors)
        bad = deepcopy(self.inventory)
        del bad["instruments"][1]
        with self.assertRaisesRegex(CheckpointError, "missing inventory"):
            from_inventory(bad, self.profile, self.anchors)

    def test_reject_image_overflow_zero_quantity_and_duplicate_id(self):
        rows = [{"id": 0, "side": True, "price": 0, "size": 2}]
        with self.assertRaisesRegex(CheckpointError, "repeats live slot"):
            Image.from_order_rows(0, 0, 0, rows*2, self.cfg)
        no_quantity = [{**rows[0], "size": 0}]
        with self.assertRaisesRegex(CheckpointError, "zero-sized"):
            Image.from_order_rows(0, 0, 0, no_quantity, self.cfg)
        with self.assertRaisesRegex(CheckpointError, "slot count"):
            Image.from_order_rows(0, 0, 0, rows*(self.cfg.slots+1), self.cfg)

    def test_anchor_parser_rejects_encoding_and_duplicates(self):
        self.assertEqual(parse_anchors(["0=17", "1=0"], self.cfg), {0: 17, 1: 0})
        for bad in (["AAPL=2"], ["0"], ["0=-1"], ["2=1"], ["0=70000"], ["0=1", "0=2"], []):
            with self.subTest(bad=bad), self.assertRaises(CheckpointError):
                parse_anchors(bad, self.cfg)

    def test_read_manifest_revalidates_digested_order_shape(self):
        manifest = self.plan().manifest()
        row = manifest["data"]["images"][0]["orders"][0]
        row["side"] = 0
        manifest["digest"] = digest(manifest["data"])
        with self.assertRaisesRegex(CheckpointError, "Boolean"):
            RestorePlan.read_manifest(manifest)

    def test_fenced_image_contains_data_but_recovering_becomes_live(self):
        self.assertTrue(all(row["mode"] == "FENCED" for row in self.inventory["instruments"]))
        restored = assert_plan_rows(self.plan(), self.plan().rows())
        self.assertTrue(all(row["eligible"] for row in restored["restored"]))

    def test_default_builder_rejects_zero_length_selection(self):
        with self.assertRaisesRegex(CheckpointError, "no instruments"):
            from_inventory(self.inventory, self.profile, {})


if __name__ == "__main__":
    unittest.main()
