"""Known-answer and adversarial checks for the reserve matcher oracle."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reserve_model import (
    Command, Geometry, Kind, Operation, Prevention, Reject, ReserveBook,
    ReserveMatcher, Result, TimeInForce, unpack,
)
from reserve_verify import CheckFailure, PinSample, Scoreboard, parse_trace


G = Geometry(3, 7, 7, 8, 3, 2, sequence_width=2, owner_width=3)


def new(id, price, quantity, side=1, owner=0, **changes):
    """Construct valid ordinary maker input defaults for a focused test."""
    return Command(
        order_id=id, price=price, quantity=quantity, side=side, owner=owner, **changes,
    )


class ReserveSemantics(unittest.TestCase):
    def setUp(self):
        """Separate fixture books prevent time-priority leakage across tests."""
        self.book = ReserveBook(G)

    def apply(self, *commands):
        """Return the last command results after building its fixture state."""
        for command in commands:
            results = self.book.execute(command)
        return results

    def test_iceberg_replenishes_behind_resting_peer(self):
        results = self.apply(
            new(0, 3, 8, display=2),
            new(1, 3, 3),
            new(2, 3, 6, side=0),
        )
        self.assertEqual(
            [(r.maker, r.quantity, r.remaining, r.visible, r.refreshed, r.last) for r in results],
            [(0, 2, 4, 2, True, False), (1, 3, 1, 0, False, False), (0, 1, 0, 1, False, True)],
        )

    def test_alternating_refresh_keeps_equal_price_fair(self):
        results = self.apply(
            new(0, 2, 8, display=2),
            new(1, 2, 8, display=2),
            new(2, 2, 10, side=0),
        )
        self.assertEqual([r.maker for r in results], [0, 1, 0, 1, 0])
        self.assertEqual([r.quantity for r in results], [2, 2, 2, 2, 2])
        self.assertEqual([r.refreshed for r in results], [True] * 5)

    def test_small_fill_does_not_refresh_or_reorder(self):
        self.apply(new(0, 1, 7, display=4), new(1, 1, 5), new(2, 1, 1, side=0))
        [fill] = self.apply(new(3, 1, 2, side=0))
        self.assertEqual((fill.maker, fill.visible, fill.refreshed), (0, 1, False))

    def test_sell_against_highest_bid_even_after_refresh(self):
        results = self.apply(
            new(0, 3, 8, side=0, display=2),
            new(1, 4, 8, side=0, display=2),
            new(2, 3, 9),
        )
        self.assertEqual([r.price for r in results], [4, 4, 4, 4, 3])
        self.assertEqual([r.maker for r in results], [1, 1, 1, 1, 0])
        self.assertEqual(results[-1].remaining, 0)

    def test_post_only_checks_crossing_before_self_cancel(self):
        [result] = self.apply(
            new(0, 1, 8, owner=2),
            new(1, 2, 3, side=0, owner=2, tif=TimeInForce.POST, stp=Prevention.CANCEL_MAKER),
        )
        self.assertEqual(result, Result.reject(Reject.POST))
        self.assertEqual(self.book.orders[0].total, 8)

    def test_failed_fok_preserves_self_owned_order_and_reserve(self):
        self.apply(new(0, 2, 9, owner=1), new(1, 2, 6, owner=2, display=2))
        before = dict(self.book.orders)
        [result] = self.apply(new(
            2, 2, 7, side=0, owner=1, tif=TimeInForce.FOK, stp=Prevention.CANCEL_MAKER,
        ))
        self.assertEqual(result, Result.reject(Reject.FOK))
        self.assertEqual(self.book.orders, before)

    def test_exact_fok_uses_hidden_counterparty_size(self):
        results = self.apply(
            new(0, 2, 9, owner=1),
            new(1, 2, 6, owner=2, display=2),
            new(2, 2, 6, side=0, owner=1, tif=TimeInForce.FOK, stp=Prevention.CANCEL_MAKER),
        )
        self.assertEqual([r.kind for r in results], [Kind.STP_MAKER] + [Kind.FILL] * 3)
        self.assertEqual([r.quantity for r in results], [9, 2, 2, 2])
        self.assertEqual([r.remaining for r in results], [6, 4, 2, 0])
        self.assertFalse(self.book.orders)

    def test_cancel_taker_preserves_maker_after_prior_fill(self):
        results = self.apply(
            new(0, 2, 4, owner=1),
            new(1, 3, 8, owner=2, display=2),
            new(2, 4, 7, side=0, owner=2, stp=Prevention.CANCEL_TAKER),
        )
        self.assertEqual([(r.kind, r.quantity) for r in results], [(Kind.FILL, 4), (Kind.STP_TAKER, 3)])
        self.assertEqual(self.book.orders[1].total, 8)
        self.assertEqual(self.book.orders[1].visible, 2)

    def test_reduction_consumes_hidden_then_visible_without_requeue(self):
        self.apply(new(0, 3, 15, display=6, owner=2), new(1, 3, 4))
        preserve_age = self.book.orders[0].time_priority
        [first] = self.apply(Command(operation=Operation.REDUCE, owner=2, quantity=9))
        [second] = self.apply(Command(operation=Operation.REDUCE, owner=2, quantity=4))
        self.assertEqual([(first.total, first.visible), (second.total, second.visible)], [(9, 6), (4, 4)])
        self.assertEqual(self.book.orders[0].time_priority, preserve_age)
        [fill] = self.apply(new(2, 3, 4, side=0))
        self.assertEqual(fill.maker, 0)

    def test_owner_controls_modification_and_order_diagnostics(self):
        self.apply(new(0, 2, 5, owner=2))
        for operation in (Operation.ORDER, Operation.CANCEL, Operation.REDUCE):
            with self.subTest(operation=operation):
                [result] = self.apply(Command(operation=operation, order_id=0, owner=1))
                self.assertEqual(result, Result.reject(Reject.OWNER))
                self.assertEqual(self.book.orders[0].total, 5)

    def test_rest_caps_slice_when_taker_crossed_first(self):
        results = self.apply(new(0, 2, 7), new(1, 2, 10, side=0, display=8))
        self.assertEqual([r.kind for r in results], [Kind.FILL, Kind.REST])
        self.assertEqual((results[-1].visible, results[-1].peak), (3, 8))

    def test_level_quantities_distinguish_hidden_and_displayed(self):
        [result] = self.apply(
            new(0, 2, 19, display=2), new(1, 2, 6),
            Command(operation=Operation.LEVEL, price=2, side=1),
        )
        self.assertEqual((result.level_total, result.level_visible, result.level_count), (25, 8, 2))


class WireAndAudit(unittest.TestCase):
    def test_result_pack_has_independent_expected_low_bits(self):
        result = Result(Kind.FILL, total=19, reason=Reject.FOK, refreshed=True, level_count=3)
        packed = result.payload(G)
        self.assertEqual(packed & 0x7f, (3 << 4) | Reject.FOK)
        self.assertEqual(Result(**unpack(packed, G.result_layout())), result)

    def test_command_rejects_field_overflow_without_masking(self):
        with self.assertRaisesRegex(ValueError, "owner"):
            Command(owner=8).payload(G)
        with self.assertRaisesRegex(ValueError, "quantity"):
            Command(quantity=-1).payload(G)

    def test_packing_matches_fixed_command_bit_positions(self):
        command = Command(operation=Operation.REDUCE, order_id=5, price=3, owner=4,
                          quantity=0x11, display=0x23)
        packed = command.payload(G)
        self.assertEqual(packed, 0x8158891804)
        self.assertEqual(Command.decode(G, 0, 0, packed), command)

    def test_invalid_bank_has_an_independent_sequence_lane(self):
        model = ReserveMatcher(G)
        invalid = Command(bank=G.banks)
        self.assertEqual(
            [model.execute(invalid)[0].command_sequence for _ in range(6)], [0, 1, 2, 3, 0, 1],
        )
        self.assertEqual(model.execute(Command(bank=1))[0].event_sequence, 0)

    def test_capture_rejects_unknown_control(self):
        with TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.txt"
            capture.write_text("0 0 x 1 0 0 0 0 0 0 0 0 0 0 0 0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown control"):
                list(parse_trace(capture, G))

    def test_audit_rejects_unsettled_payload_under_sink_stall(self):
        checker = Scoreboard(G)
        command = Command()
        replay = ReserveMatcher(G).execute(command)[0]
        checker.step(PinSample(0, False, True, True, False, False, command, None, 0))
        checker.step(PinSample(1, False, False, True, True, False, None, replay, 1))
        tampered = replace(replay, tag=replay.tag ^ 1)
        with self.assertRaisesRegex(CheckFailure, "stalled output changed"):
            checker.step(PinSample(2, False, False, True, True, True, None, tampered, 1))

    def test_audit_rejects_capture_that_ends_before_needed_output(self):
        checker = Scoreboard(G)
        command = Command()
        checker.step(PinSample(0, False, True, True, False, False, command, None, 0))
        with self.assertRaisesRegex(CheckFailure, "undelivered"):
            checker.summary()


if __name__ == "__main__":
    unittest.main()
