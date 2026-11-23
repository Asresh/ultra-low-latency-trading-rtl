"""Known-answer checks for the independent oracle used by the RTL scoreboard."""

from dataclasses import replace
import unittest

from matching_model import Book, Kind, PROFILES, Reference, Reject, TimeInForce
from matching_workload import cancel, interleave, level, new, reduce


class BookSemantics(unittest.TestCase):
    # Create a fresh reference configuration for each model test.
    def setUp(self):
        self.profile = PROFILES["default"]
        self.book = Book(self.profile)

    # Apply a command and return its terminal event for concise assertions.
    def event(self, command):
        events = self.book.apply(command)
        self.assertEqual(len(events), 1)
        return events[0]

    # Verify one side and price reports the expected open quantity and order count.
    def assert_query(self, side, price, quantity, count):
        result = self.event(level(0, price, side))
        self.assertEqual(result.kind, Kind.LEVEL)
        self.assertEqual(result.level_quantity, quantity)
        self.assertEqual(result.level_count, count)

    # Check that overlapping input errors report the contract's first rejection reason.
    def test_reject_precedence(self):
        bad = new(0, self.profile.slots, self.profile.prices, 0, tif=3)
        self.assertEqual(self.event(bad).reject, Reject.ID)
        self.assertEqual(self.event(replace(bad, order_id=0)).reject, Reject.PRICE)
        bad = replace(bad, order_id=0, price=0)
        self.assertEqual(self.event(bad).reject, Reject.QUANTITY)
        self.assertEqual(self.event(replace(bad, quantity=1)).reject, Reject.TIME_IN_FORCE)
        self.assertEqual(self.event(new(0, 0, 0, 2)).kind, Kind.REST)
        self.assertEqual(self.event(new(0, 0, 0, 3)).reject, Reject.DUPLICATE)
        self.assertEqual(self.event(cancel(0, 1)).reject, Reject.ABSENT)
        self.assertEqual(self.event(reduce(0, 0, 3)).reject, Reject.REDUCE)
        self.assertEqual(self.event(new(0, 1, 0, 1, True, TimeInForce.POST_ONLY)).reject,
                         Reject.POST_ONLY)
        self.assert_query(False, 0, 2, 1)

    # Verify that reducing size does not move an order behind later arrivals.
    def test_partial_maker_and_reduction_keep_priority(self):
        self.event(new(0, 7, 3, 9, True))
        self.event(new(0, 1, 3, 5, True))
        result = self.event(reduce(0, 7, 6))
        self.assertEqual((result.kind, result.quantity, result.remaining), (Kind.REDUCE, 3, 6))
        first_fill = self.event(new(0, 2, 3, 2))
        self.assertEqual((first_fill.maker, first_fill.quantity, first_fill.last), (7, 2, True))
        self.assert_query(True, 3, 9, 2)
        sweep = self.book.apply(new(0, 2, 3, 5))
        self.assertEqual([event.maker for event in sweep], [7, 1])
        self.assertEqual([event.quantity for event in sweep], [4, 1])
        self.assertEqual([event.remaining for event in sweep], [1, 0])
        self.assert_query(True, 3, 4, 1)

    # Check price-first and FIFO selection when a sell order crosses bids.
    def test_sell_chooses_highest_bid_then_oldest(self):
        self.event(new(0, 0, 3, 5))
        self.event(new(0, 1, 5, 2))
        self.event(new(0, 2, 5, 3))
        sweep = self.book.apply(new(0, 4, 3, 7, True))
        self.assertEqual([r.maker for r in sweep], [1, 2, 0])
        self.assertEqual([r.price for r in sweep], [5, 5, 3])
        self.assertEqual([r.quantity for r in sweep], [2, 3, 2])
        self.assert_query(False, 3, 3, 1)
        self.assert_query(True, 3, 0, 0)

    # Check that IOC expires and GTC rests any unfilled quantity.
    def test_ioc_and_gtc_handle_remainders_differently(self):
        self.event(new(0, 0, 2, 1, True))
        ioc = self.book.apply(new(0, 1, 2, 6, tif=TimeInForce.IOC))
        self.assertEqual([r.kind for r in ioc], [Kind.FILL, Kind.EXPIRE])
        self.assertEqual(ioc[-1].quantity, 5)
        self.assertEqual(ioc[-1].remaining, 0)
        self.assertEqual(self.event(cancel(0, 1)).reject, Reject.ABSENT)
        self.event(new(0, 0, 2, 1, True))
        gtc = self.book.apply(new(0, 1, 2, 6))
        self.assertEqual([r.kind for r in gtc], [Kind.FILL, Kind.REST])
        self.assertEqual(gtc[-1].quantity, 5)
        self.assertEqual(gtc[-1].remaining, 5)
        self.assert_query(False, 2, 5, 1)

    # Verify the aggregate can represent multiple maximum-sized orders.
    def test_full_level_aggregate_extends_quantity_width(self):
        for order_id in range(self.profile.slots):
            self.event(new(0, order_id, 5, self.profile.qty_max))
        total = self.profile.slots * self.profile.qty_max
        self.assert_query(False, 5, total, self.profile.slots)
        for order_id in range(self.profile.slots):
            self.assertEqual(self.event(cancel(0, order_id)).quantity, self.profile.qty_max)
            total -= self.profile.qty_max
            self.assert_query(False, 5, total, self.profile.slots - order_id - 1)

    # Match one incoming order against the maximum number of resting makers.
    def test_one_command_can_consume_all_other_slots(self):
        capacity = self.profile.slots
        for order_id in range(capacity - 1):
            self.event(new(0, order_id, 1, 1, True))
        outcomes = self.book.apply(new(0, capacity - 1, 1, capacity, tif=TimeInForce.IOC))
        self.assertEqual(len(outcomes), capacity)
        self.assertEqual([event.maker for event in outcomes[:-1]], list(range(capacity - 1)))
        self.assertEqual(outcomes[-1].kind, Kind.EXPIRE)
        self.assertEqual(outcomes[-1].quantity, 1)
        self.assert_query(True, 1, 0, 0)


class InstrumentIsolation(unittest.TestCase):
    # Maintain distinct books even when instruments reuse identical order IDs.
    def test_equal_ids_do_not_share_liquidity(self):
        reference = Reference(PROFILES["default"])
        reference.execute(new(0, 0, 3, 5, True))
        reference.execute(new(1, 0, 3, 9, True))
        reference.execute(new(1, 1, 3, 3))
        bank_zero = reference.execute(level(0, 3, True))[0]
        bank_one = reference.execute(level(1, 3, True))[0]
        self.assertEqual((bank_zero.level_quantity, bank_one.level_quantity), (5, 6))
        self.assertEqual((bank_zero.command_sequence, bank_one.command_sequence), (1, 2))

    # Ensure unsupported instruments affect the error sequence alone.
    def test_invalid_instruments_share_only_the_error_lane(self):
        profile = PROFILES["wide"]
        reference = Reference(profile)
        invalid = [profile.banks, profile.instrument_max, profile.banks + 1]
        events = [reference.execute(level(key, 0, False))[0] for key in invalid]
        self.assertEqual([event.instrument for event in events], invalid)
        self.assertEqual([event.event_sequence for event in events], [0, 1, 2])
        self.assertTrue(all(event.reject == Reject.INSTRUMENT for event in events))
        self.assertEqual(reference.execute(level(0, 0, False))[0].event_sequence, 0)

    # Check multiple fills consume separate event sequence values across wrap.
    def test_event_sequence_advances_within_a_command_and_wraps(self):
        profile = replace(PROFILES["default"], sequence_width=2)
        reference = Reference(profile)
        reference.execute(new(0, 0, 1, 2, True))
        reference.execute(new(0, 1, 1, 2, True))
        events = reference.execute(new(0, 2, 1, 5))
        self.assertEqual([event.event_sequence for event in events], [2, 3, 0])
        self.assertEqual([event.command_sequence for event in events], [2, 2, 2])
        self.assertEqual(reference.execute(cancel(0, 2))[0].command_sequence, 3)
        self.assertEqual(reference.execute(level(0, 1, True))[0].command_sequence, 0)

    # Verify that legal cross-bank ordering cannot alter each bank's observable history.
    def test_legal_cross_bank_interleavings_keep_local_results(self):
        profile = PROFILES["default"]
        tapes = [
            [new(bank, 0, 2, 5, True), new(bank, 1, 2, 2), level(bank, 2, True), cancel(bank, 0)]
            for bank in range(profile.banks)
        ]
        left = Reference(profile)
        right = Reference(profile)
        serial = [[] for _ in tapes]
        crossed = [[] for _ in tapes]
        for tape in tapes:
            for command in tape:
                serial[command.instrument].extend(left.execute(command))
        for command in interleave(*reversed(tapes)):
            crossed[command.instrument].extend(right.execute(command))
        self.assertEqual(serial, crossed)

    # Check that reset flushes both book state and sequence state.
    def test_reset_clears_live_orders_and_resets_each_sequence(self):
        reference = Reference(PROFILES["default"])
        reference.execute(new(0, 0, 3, 5, True))
        reference.execute(new(1, 0, 3, 9, True))
        reference.execute(new(3, 0, 3, 9, True))
        reference.reset()
        for instrument in range(4):
            event = reference.execute(level(instrument, 3, True))[0]
            self.assertEqual((event.event_sequence, event.command_sequence), (0, 0))
            self.assertEqual(event.level_quantity, 0)


if __name__ == "__main__":
    unittest.main()
