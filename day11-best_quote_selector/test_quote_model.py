"""Check quote-book rules with known-answer, exhaustive, and property tests."""

from dataclasses import replace
from itertools import product
from random import Random
import unittest

from quote_model import Machine, QuoteBook, market_state, price_error
from quote_schema import (
    Ack, Code, Command, Drive, Op, PROFILES, Profile, Query, Result, State, Winner,
    bounded, mask,
)


class StoreRules(unittest.TestCase):
    def setUp(self):
        """Use a fresh small book and ordinary price bands for each case."""
        self.profile = Profile(3, 3, 8, 6, 8, 6, 8)
        self.book = QuoteBook(self.profile)

    def post(self, venue, sequence, *, bid=(10, 5), ask=(12, 3), tick=1, instrument=0, tag=0):
        """Publish one replacement into the model and return its status."""
        command = Command.replace(instrument, venue, sequence, bid, ask, tag)
        return self.book.apply(command, tick).code

    def test_initial_state(self):
        """Check all rows begin enabled, unseen, and unquoted."""
        for row in self.book.snapshot():
            self.assertTrue(row.enabled)
            self.assertIsNone(row.last_sequence)
            self.assertIsNone(row.quote)
        self.assertEqual(self.book.query(Query(0, 10, 77), 100), Result(tag=77, tick=100))

    def test_local_cross_is_atomic(self):
        """Reject a local cross without changing an older valid quote or fence."""
        self.assertEqual(self.post(0, 5), Code.OK)
        before = self.book.status(0, 0)
        self.assertEqual(self.post(0, 6, bid=(13, 1), ask=(12, 1)), Code.BAD_PRICE)
        self.assertEqual(self.book.status(0, 0), before)
        self.assertEqual(self.post(0, 6), Code.OK)
        self.assertEqual(self.book.status(0, 0).last_sequence, 6)

    def test_every_zero_price_quantity_shape(self):
        """Check all low-valued payload combinations against the canonical side contract."""
        for bid, ask, bq, aq in product((0, 1, 2), repeat=4):
            cmd = Command(Op.REPLACE, 0, 0, 0, bid, ask, bq, aq)
            ok_bid = bid >= 1 and bq >= 1
            ok_ask = ask >= 1 and aq >= 1
            empty_bid = bid == bq == 0
            empty_ask = ask == aq == 0
            allowed = (ok_bid and empty_ask or ok_ask and empty_bid or
                       ok_bid and ok_ask and bid <= ask)
            self.assertEqual(price_error(cmd), not allowed, cmd)

    def test_disable_preserves_serial_and_destroys_quote(self):
        """Disable, reenable, and require a strictly forward update after reopening."""
        self.assertEqual(self.post(1, 8), Code.OK)
        self.assertEqual(self.book.apply(Command.venue_enabled(0, 1, False), 2).code, Code.OK)
        mid = self.book.status(0, 1)
        self.assertFalse(mid.enabled)
        self.assertEqual(mid.last_sequence, 8)
        self.assertIsNone(mid.quote)
        self.assertEqual(self.post(1, 9), Code.DISABLED)
        self.assertEqual(self.book.apply(Command.venue_enabled(0, 1, True), 4).code, Code.OK)
        self.assertEqual(self.post(1, 8), Code.BAD_SEQ)
        self.assertEqual(self.post(1, 9), Code.OK)

    def test_empty_withdraw_establishes_fence(self):
        """Recognize that a valid removal consumes its serial even when the row was empty."""
        self.assertEqual(self.book.apply(Command.withdraw(0, 2, 9, 55), 10), Ack(Code.OK, 55))
        self.assertEqual(self.post(2, 9), Code.BAD_SEQ)
        self.assertEqual(self.post(2, 8), Code.BAD_SEQ)
        self.assertEqual(self.post(2, 10), Code.OK)

    def test_withdraw_irrelevant_fields(self):
        """Ignore market payload bits on a command whose only effect is removing a row."""
        self.post(0, 1)
        order = Command(Op.WITHDRAW, 0, 0, 2, 31, 1, 25, 0, 54)
        self.assertEqual(self.book.apply(order, 1), Ack(Code.OK, 54))
        self.assertIsNone(self.book.status(0, 0).quote)
        self.assertEqual(self.book.status(0, 0).last_sequence, 2)

    def test_invalid_indices_and_opcode_priority(self):
        """Reject invalid addressing before interpreting the opcode or local content."""
        malformed = Command(7, 3, 3, 0, 5, 0, 0, 0, 3)
        before = self.book.snapshot()
        self.assertEqual(self.book.apply(malformed, 8), Ack(Code.BAD_INST, 3))
        self.assertEqual(self.book.apply(replace(malformed, instrument=1), 8), Ack(Code.BAD_VENUE, 3))
        self.assertEqual(self.book.apply(replace(malformed, instrument=1, venue=1), 8), Ack(Code.BAD_OP, 3))
        self.assertEqual(self.book.snapshot(), before)

    def test_serial_and_disabled_priority(self):
        """Check venue policy and serial validation precede shape validation."""
        bad_shape = Command(Op.REPLACE, 0, 0, 15, 3, 0, 0, 0)
        self.assertEqual(self.book.apply(bad_shape, 1).code, Code.BAD_PRICE)
        self.post(0, 15)
        self.assertEqual(self.book.apply(bad_shape, 1).code, Code.BAD_SEQ)
        self.book.apply(Command.venue_enabled(0, 0, False), 1)
        self.assertEqual(self.book.apply(bad_shape, 1).code, Code.DISABLED)

    def test_query_on_invalid_instrument_prioritizes_address(self):
        """Return errors with original tag and clock plus quiet masks and sides."""
        result = self.book.query(Query(3, self.profile.time_half, 31), 199)
        self.assertEqual(result, Result(tag=31, tick=199, code=Code.BAD_INST))
        result = self.book.query(Query(2, self.profile.time_half, 32), 200)
        self.assertEqual(result, Result(tag=32, tick=200, code=Code.BAD_AGE))

    def test_same_venue_number_independent_by_instrument(self):
        """Reuse a venue and serial for a distinct instrument without state collision."""
        self.assertEqual(self.post(0, 42, instrument=0), Code.OK)
        self.assertEqual(self.post(0, 42, instrument=1, bid=(21, 9), ask=(30, 2)), Code.OK)
        self.assertEqual(self.book.query(Query(0, 2), 1).bid.price, 10)
        self.assertEqual(self.book.query(Query(1, 2), 1).bid.price, 21)
        self.assertEqual(self.book.query(Query(2, 2), 1).state, State.EMPTY)
        self.book.apply(Command.venue_enabled(0, 0, False), 3)
        self.assertEqual(self.book.query(Query(1, 2), 3).state, State.OPEN)

    def test_status_rejects_negative_coordinates(self):
        """Prevent Python's negative indexing from masquerading as a valid table address."""
        for instrument, venue in ((-1, 0), (0, -1), (3, 0), (0, 3)):
            with self.assertRaises(IndexError):
                self.book.status(instrument, venue)

    def test_reset_releases_disabled_and_seen_rows(self):
        """Reset all independent stores and let formerly old serials start a new epoch."""
        self.post(0, 33)
        self.post(1, 42)
        self.book.apply(Command.venue_enabled(0, 0, False), 2)
        self.book.reset()
        self.assertEqual(self.book.snapshot(), QuoteBook(self.profile).snapshot())
        self.assertEqual(self.post(0, 2), Code.OK)

    def test_snapshot_immutable_after_upsert(self):
        """A prior readout must remain a point-in-time observation."""
        self.post(0, 5)
        snapshot = self.book.snapshot()
        self.post(0, 6, bid=(18, 4), ask=(19, 4))
        self.assertEqual(snapshot[0].quote.bid_price, 10)

    def test_time_age_boundaries(self):
        """Distinguish exact deadline, one beyond it, and half-range ambiguity."""
        bitmax = mask(self.profile.time_w)
        self.post(0, 0, tick=bitmax - 1)
        accepted = self.book.query(Query(0, 3), 1)
        self.assertEqual(accepted.bid.age, 3)
        self.assertEqual(accepted.active, 1)
        self.assertEqual(accepted.stale, 0)
        late = self.book.query(Query(0, 3), 2)
        self.assertEqual(late.stale, 1)
        self.assertEqual(late.bid, Winner())
        edge = bounded(bitmax - 1 + self.profile.time_half, self.profile.time_w)
        beyond_wrap = self.book.query(Query(0, self.profile.time_half - 1), edge)
        self.assertEqual(beyond_wrap.stale, 1)

    def test_time_age_zero_requires_same_tick(self):
        """A zero maximum age accepts a quote at its own update timestamp only."""
        self.post(0, 1, tick=51)
        self.assertTrue(self.book.query(Query(0, 0), 51).bid.valid)
        self.assertFalse(self.book.query(Query(0, 0), 52).bid.valid)

    def test_independent_side_winners_and_cross(self):
        """Allow two distinct venues to create a crossed consolidated market."""
        self.post(0, 1, bid=(19, 4), ask=(21, 6), tick=3)
        self.post(1, 55, bid=(12, 5), ask=(18, 1), tick=5)
        result = self.book.query(Query(0, 12, 79), 9)
        self.assertEqual(result.active, 3)
        self.assertEqual(result.bid, Winner(1, 0, 19, 4, 6, 1))
        self.assertEqual(result.ask, Winner(1, 1, 18, 1, 4, 55))
        self.assertEqual(result.state, State.CROSSED)
        self.assertEqual(result.spread, -1)

    def test_tie_is_venue_index_not_size_or_time(self):
        """A stable tie winner is unaffected by later peer updates or larger quantities."""
        self.post(2, 21, bid=(30, 31), ask=(40, 31), tick=18)
        self.post(0, 17, bid=(30, 1), ask=(40, 2), tick=9)
        self.post(1, 1, bid=(30, 63), ask=(40, 63), tick=20)
        actual = self.book.query(Query(0, 20), 21)
        self.assertEqual(actual.bid, Winner(1, 0, 30, 1, 12, 17))
        self.assertEqual(actual.ask, Winner(1, 0, 40, 2, 12, 17))
        actual = self.book.query(Query(0, 2), 21)
        self.assertEqual(actual.bid.venue, 1)
        self.assertEqual(actual.ask.venue, 1)
        self.assertEqual(actual.stale, 5)

    def test_market_state_truth_table(self):
        """Distinguish missing-side states from each sign of the two-sided spread."""
        empty = Winner()
        lower = Winner(1, 0, 3, 2, 0, 0)
        upper = Winner(1, 0, 5, 2, 0, 0)
        for bid, ask, wanted in (
            (empty, empty, State.EMPTY), (lower, empty, State.BID_ONLY),
            (empty, upper, State.ASK_ONLY), (lower, upper, State.OPEN),
            (lower, lower, State.LOCKED), (upper, lower, State.CROSSED),
        ):
            self.assertEqual(market_state(bid, ask), wanted)

    def test_invariants_across_random_control_and_updates(self):
        """Keep disabled, seen, quoted, and instrument tables aligned under mixed traffic."""
        rng = Random(91)
        for tick in range(1_300):
            inst = rng.randrange(3)
            venue = rng.randrange(3)
            op = rng.randrange(5)
            serial = rng.randrange(64)
            update = Command(op, inst, venue, serial, 21, 24, 2, 8)
            before = self.book.snapshot()
            ack = self.book.apply(update, tick % 256)
            if ack.code != Code.OK:
                self.assertEqual(self.book.snapshot(), before)
            self.book.check_invariants()


class QueueRules(unittest.TestCase):
    def setUp(self):
        """Initialize a cycle-based reference and convenient legal input records."""
        self.machine = Machine(PROFILES["solo"])
        self.machine.step(Drive(reset=1))
        self.quote = Command.replace(0, 0, 0, (7, 1), (9, 2), 15)
        self.query = Query(0, 9, 16)

    def test_latency_and_pre_state(self):
        """Observe acknowledgement one edge and snapshot two edges after acceptance."""
        first = self.machine.step(Drive(tick=1, command=self.quote, query=self.query))
        self.assertEqual((first.cmd_ready, first.query_ready), (1, 1))
        self.assertIsNone(first.ack)
        self.assertIsNone(first.result)
        second = self.machine.step(Drive(tick=2, query=replace(self.query, tag=17)))
        self.assertEqual(second.ack, Ack(Code.OK, 15))
        self.assertIsNone(second.result)
        third = self.machine.step(Drive(tick=3))
        self.assertEqual(third.result, Result(tag=16, tick=1))
        fourth = self.machine.step(Drive(tick=4))
        self.assertEqual(fourth.result.tag, 17)
        self.assertEqual(fourth.result.tick, 2)
        self.assertEqual(fourth.result.bid.age, 1)
        self.assertTrue(self.machine.drained())

    def test_output_slots_free_independently(self):
        """Block the reply bus, fill its two slots, and keep the command bus moving."""
        self.machine.step(Drive(tick=1, query=self.query, result_ready=0))
        self.machine.step(Drive(tick=2, query=replace(self.query, tag=17), result_ready=0))
        for edge in range(3, 7):
            sent = replace(self.quote, sequence=edge, tag=edge)
            sampled = self.machine.step(Drive(tick=edge, command=sent, result_ready=0))
            self.assertEqual(sampled.cmd_ready, 1)
            self.assertEqual(sampled.query_ready, 0)
            self.assertEqual(sampled.result.tag, 16)
        streamed = self.machine.step(Drive(tick=7, result_ready=1, query=replace(self.query, tag=18)))
        self.assertEqual(streamed.query_ready, 1)
        self.assertEqual(streamed.result.tag, 16)
        self.assertEqual(self.machine.step(Drive(tick=8)).result.tag, 17)
        self.assertEqual(self.machine.step(Drive(tick=9)).result.tag, 18)

    def test_held_ack_blocks_new_write_but_not_queries(self):
        """Keep the old tag and book while output stalls accepting a replacement."""
        self.machine.step(Drive(tick=1, command=self.quote, ack_ready=0))
        new = replace(self.quote, sequence=1, bid_price=8, tag=22)
        wait = self.machine.step(Drive(tick=2, command=new, ack_ready=0, query=self.query))
        self.assertEqual((wait.cmd_ready, wait.query_ready), (0, 1))
        self.assertEqual(wait.ack, Ack(Code.OK, 15))
        self.machine.step(Drive(tick=3, command=new, ack_ready=1))
        old = self.machine.step(Drive(tick=4)).result
        self.assertEqual(old.bid.price, 7)

    def test_reset_masks_handshakes_and_flushes_storage(self):
        """Discard simultaneous offered input and pending output on the reset edge."""
        self.machine.step(Drive(tick=1, command=self.quote, query=self.query))
        sample = self.machine.step(Drive(tick=2, reset=1, command=self.quote, query=self.query))
        self.assertEqual((sample.cmd_ready, sample.query_ready), (0, 0))
        self.assertIsNone(sample.ack)
        self.assertIsNone(sample.result)
        self.assertTrue(self.machine.drained())
        self.assertEqual(self.machine.book.query(self.query, 3).state, State.EMPTY)


if __name__ == "__main__":
    unittest.main()
