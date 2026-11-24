"""Independent boundary checks for normalized fields and the trace scoreboard."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from check_drop_copy import REFERENCE, compare, read_rows, write_rows
from drop_copy_model import Inputs, Key, Lane, Oracle, Payload, Status, WORD32


class PackedFields(unittest.TestCase):
    """Check field offsets and the independent per-field masks."""

    def test_key_boundaries(self):
        """Exercise the most significant bit of every identity component."""
        self.assertEqual(Key(0x8000, 0, 0, 0).packed(), 1 << 73)
        self.assertEqual(Key(0, 0x80, 0, 0).packed(), 1 << 57)
        self.assertEqual(Key(0, 0, 0x8000, 0).packed(), 1 << 49)
        self.assertEqual(Key(0, 0, 0, 0x80000000).packed(), 1 << 33)
        self.assertEqual(Key(0, 0, 0, 0, 2).packed(), 2)

    def test_payload_boundaries_and_diff(self):
        """Validate individual bits and the OR of all comparison field masks."""
        self.assertEqual(Payload(1 << 31, 0, 0, 0).packed(), 1 << 88)
        self.assertEqual(Payload(0, 1 << 23, 0, 0).packed(), 1 << 56)
        self.assertEqual(Payload(0, 0, 1 << 31, 0).packed(), 1 << 32)
        self.assertEqual(Payload(0, 0, 0, 1).packed(), 1)
        updates = (
            ({"order": 4}, 8), ({"quantity": 40}, 4),
            ({"price": 3}, 2), ({"side": 1}, 1),
        )
        for update, expected in updates:
            with self.subTest(field=update):
                self.assertEqual(REFERENCE.diff(replace(REFERENCE, **update)), expected)
        self.assertEqual(REFERENCE.diff(Payload(7, 20, 24, 1)), 15)

    def test_invalid_parameters(self):
        """Ensure non-power-of-two geometry and zero durations are rejected."""
        for parameters in [(0, 3, 5), (3, 3, 5), (2, 0, 5), (2, 3, 0)]:
            with self.subTest(parameters=parameters):
                with self.assertRaises(ValueError):
                    Oracle(*parameters)


class OrderingBoundaries(unittest.TestCase):
    """Use small manually checked cases to pin down the scheduling contract."""

    def setUp(self):
        """Start at the end of tick space with one set and short windows."""
        self.core = Oracle(sets=1, wait=3, history=3, reset_tick=WORD32 - 1)
        self.key = Key(0, 2, 89, 24)
        self.row = (self.key, REFERENCE)

    def test_pair_across_tick_wrap(self):
        """A counterpart after tick wrap can still pair inside the wait window."""
        self.core.advance(Inputs(primary=self.row))
        self.core.advance(Inputs())
        self.core.advance(Inputs(copy=self.row))
        self.assertEqual(self.core.report.status, Status.MATCH)
        self.assertEqual(self.core.report.age, 2)
        self.assertEqual(self.core.tick, 1)

    def test_config_has_priority_over_overdue_event(self):
        """A same-edge restart flushes an overdue pending event before maintenance."""
        self.core.advance(Inputs(primary=self.row))
        self.core.advance(Inputs())
        self.core.advance(Inputs())
        self.assertEqual(self.core.due()[0], self.key)
        pins = self.core.advance(Inputs(epoch=1, copy=self.row))
        self.assertFalse(pins[Lane.COPY])
        self.assertEqual(self.core.report.status, Status.RESTART)
        self.assertEqual(self.core.report.flushed, 1)
        self.assertEqual(self.core.counts["missing"], 0)
        self.core.advance(Inputs(copy=self.row))
        self.assertEqual(self.core.report.status, Status.STALE_EPOCH)

    def test_terminal_entry_becomes_reusable_at_history_boundary(self):
        """A duplicate one cycle before retirement must leave the original age."""
        self.core.advance(Inputs(primary=self.row))
        self.core.advance(Inputs(copy=self.row))
        self.core.advance(Inputs())
        self.core.advance(Inputs(copy=self.row))
        self.assertEqual(self.core.report.status, Status.DUPLICATE)
        self.assertEqual(self.core.report.age, 2)
        self.core.advance(Inputs(copy=self.row))
        self.assertEqual(self.core.report.status, Status.STAGED)
        self.assertEqual(self.core.pending(), 1)

    def test_reject_tag_epoch_before_payload_validation(self):
        """An old observation is classified by epoch before invalid fields."""
        self.core.advance(Inputs(epoch=1))
        self.core.advance(Inputs(primary=(replace(self.key, execution=0), REFERENCE)))
        self.assertEqual(self.core.report.status, Status.STALE_EPOCH)
        self.assertEqual(self.core.counts["observed"], 1)
        self.assertEqual(self.core.counts["rejected"], 1)

    def test_saturation_and_hard_reset(self):
        """The scoreboard clamps diagnostics and an explicit reset starts new state."""
        self.core.counts["observed"] = WORD32
        self.core.counts["flushed"] = WORD32 - 1
        for execution in (24, 25):
            self.core.advance(Inputs(primary=(replace(self.key, execution=execution), REFERENCE)))
        self.core.advance(Inputs(epoch=1))
        self.assertEqual(self.core.counts["observed"], WORD32)
        self.assertEqual(self.core.counts["flushed"], WORD32)
        self.core.advance(Inputs(reset=True))
        self.assertEqual(self.core.epoch, 0)
        self.assertEqual(self.core.tick, WORD32 - 1)
        self.assertEqual(set(self.core.counts.values()), {0})


class CheckFileBehavior(unittest.TestCase):
    """Check that bench artifacts produce actionable diagnostics when corrupt."""

    def test_reject_wrong_count_width_and_hex(self):
        """Validate parsing of the row count, field count and hex representation."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "values.hex"
            for content, expected in (("3\n1 2\n", "claims 3"), ("1\n1\n", "expected 2"),
                                      ("1\n1 w\n", "invalid hexadecimal")):
                with self.subTest(data=content):
                    path.write_text(content)
                    with self.assertRaisesRegex(ValueError, expected):
                        read_rows(path, 2)

    def test_pin_comparison_names_mismatch(self):
        """A corrupt actual response should identify the cycle and exact pin."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            idle = Oracle(1, 3, 5).sample(Inputs())
            actual = list(idle)
            actual[14] = 15
            write_rows(path / "expected.hex", [idle])
            write_rows(path / "observed.hex", [actual])
            with self.assertRaisesRegex(AssertionError, "cycle 0: tick=0xf expected 0xfffffff0"):
                compare(path)
