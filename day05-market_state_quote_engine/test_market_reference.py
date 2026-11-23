"""Known-answer fixtures for the independent book, lease and quote models."""

from dataclasses import replace
import unittest

from market_reference import (
    BBO, Command, Config, CycleInput, Depth, Engine, Instrument, Intent, Leg,
    MarketMode, Operation, Order, PROFILES, Publisher, Reason, Statistic, Status,
    quote_policy,
)


def cmd(op, *, seq=0, id_=0, new_id=0, side=False, px=0, qty=0, bank=0, stat=0, tag=0):
    """Create a protocol command with compact defaults for focused model tests."""
    return Command(bank, op, seq, id_, new_id, side, px, qty, stat, tag)


def feed(engine, command=None, *, tick=False, response_ready=True, quote_ready=True, reset=False):
    """Drive a command and advance the model to its expected output edge."""
    return engine.cycle(CycleInput(reset, tick, command, response_ready, quote_ready)).pins


class InstrumentCase(unittest.TestCase):
    def setUp(self):
        """Initialize the independent model and geometry exercised by this test group."""
        self.cfg = Config()
        self.bank = Instrument(self.cfg)

    def begin(self, sequence=0):
        """Begin a snapshot for the selected bank before inserting test orders."""
        return self.bank.process(cmd(Operation.SNAP_BEGIN, seq=sequence)).response

    def send(self, operation, status=Status.OK, **payload):
        """Submit one command and return its modeled response."""
        sequence = payload.pop("seq", self.bank.expected)
        result = self.bank.process(cmd(operation, seq=sequence, **payload)).response
        self.assertEqual(result.status, status)
        return result

    def bootstrap(self):
        """Build an active order book through a complete snapshot sequence."""
        self.begin()
        self.send(Operation.SNAP_ADD, id_=0, qty=17, px=3)
        self.send(Operation.SNAP_ADD, id_=1, qty=13, px=9, side=True)
        self.send(Operation.SNAP_END)

    def test_cold_diagnostic_and_bootstrap_wrap(self):
        """Check that cold diagnostic and bootstrap wrap."""
        result = self.send(Operation.HEARTBEAT, Status.MODE)
        self.assertEqual(result.mode, MarketMode.COLD)
        self.assertEqual(result.expected, 0)
        result = self.begin(self.cfg.sequence_max)
        self.assertEqual((result.mode, result.expected, result.epoch), (MarketMode.BOOT, 0, 1))
        self.send(Operation.SNAP_ADD, id_=0, qty=2, px=4)
        result = self.send(Operation.SNAP_END)
        self.assertEqual((result.mode, result.expected, result.epoch), (MarketMode.LIVE, 2, 1))
        self.assertEqual(self.bank.statistic(Statistic.SNAPSHOTS), 1)

    def test_boot_probe_observes_state_without_permitting_quote(self):
        """Check that boot probe observes state without permitting quote."""
        self.begin(40)
        self.send(Operation.SNAP_ADD, id_=0, qty=20, px=1)
        self.send(Operation.SNAP_ADD, id_=1, qty=7, px=5, side=True)
        result = self.send(Operation.BBO)
        self.assertEqual((result.mode, result.bid, result.ask), (MarketMode.BOOT, 1, 5))
        self.assertEqual(result.expected, 43)
        self.assertFalse(self.bank.desired().active)
        self.send(Operation.SNAP_END)
        self.assertTrue(self.bank.desired().active)

    def test_replace_same_level_keeps_aggregate_and_absence(self):
        """Check that replace same level keeps aggregate and absence."""
        self.bootstrap()
        self.send(Operation.ADD, id_=2, qty=31, px=3)
        self.send(Operation.REPLACE, id_=0, new_id=3, qty=23, px=3, side=True)
        result = self.send(Operation.LEVEL, px=3)
        self.assertEqual((result.count, result.quantity), (2, 54))
        self.assertFalse(self.send(Operation.ORDER, id_=0).present)
        new = self.send(Operation.ORDER, id_=3)
        self.assertFalse(new.side)
        self.assertEqual(new.quantity, 23)
        self.assertEqual(self.bank.statistic(Statistic.LIVE_ORDERS), 3)

    def test_replace_different_level_clears_old_bitmap_slot(self):
        """Check that replace different level clears old bitmap slot."""
        self.bootstrap()
        self.send(Operation.REPLACE, id_=0, new_id=7, qty=19, px=4)
        old_level = self.send(Operation.LEVEL, px=3)
        best = self.send(Operation.BBO)
        self.assertEqual((old_level.present, old_level.count, old_level.quantity), (False, 0, 0))
        self.assertEqual((best.bid, best.bid_quantity, best.bid_count), (4, 19, 1))
        self.assertFalse(self.send(Operation.ORDER, id_=0).present)
        self.assertTrue(self.send(Operation.ORDER, id_=7).present)

    def test_partial_cancel_execution_then_delete(self):
        """Check that partial cancel execution then delete."""
        self.bootstrap()
        self.send(Operation.CANCEL, id_=0, qty=5)
        self.send(Operation.EXECUTE, id_=0, qty=8)
        self.assertEqual(self.send(Operation.ORDER, id_=0).quantity, 4)
        self.assertEqual(self.send(Operation.LEVEL, px=3).count, 1)
        self.send(Operation.EXECUTE, id_=0, qty=4)
        self.assertFalse(self.send(Operation.ORDER, id_=0).present)
        self.send(Operation.DELETE, id_=1)
        self.assertEqual(self.bank.book.orders, {})
        result = self.send(Operation.BBO)
        self.assertFalse(result.bid_present or result.ask_present)

    def test_cancel_equality_fences_and_preserves_pre_fault_book(self):
        """Check that cancel equality fences and preserves pre fault book."""
        self.bootstrap()
        seq = self.bank.expected
        result = self.send(Operation.CANCEL, Status.QTY, id_=0, qty=17)
        self.assertEqual((result.expected, result.mode), (seq, MarketMode.FENCED))
        self.assertEqual(self.send(Operation.ORDER, id_=0).quantity, 17)
        self.assertEqual(self.bank.statistic(Statistic.FAULTS), 1)
        self.assertEqual(self.bank.statistic(Statistic.GAPS), 0)
        self.send(Operation.DELETE, Status.MODE, id_=1)
        self.assertEqual(self.bank.statistic(Statistic.FAULTS), 1)

    def test_replace_reject_priority_and_destination_preserved(self):
        """Check that replace reject priority and destination preserved."""
        self.bootstrap()
        self.send(Operation.REPLACE, Status.DUPLICATE, id_=0, new_id=1,
                  qty=0, px=self.cfg.levels)
        self.assertEqual(self.bank.book.orders[0], Order(False, 3, 17))
        self.assertEqual(self.bank.book.orders[1], Order(True, 9, 13))
        self.assertEqual(self.bank.statistic(Statistic.UPDATES), 2)

    def test_gap_precedes_payload_errors(self):
        """Check that gap precedes payload errors."""
        self.bootstrap()
        self.send(Operation.ADD, Status.GAP, seq=self.bank.expected+1, id_=self.cfg.slots)
        self.assertEqual(self.bank.statistic(Statistic.GAPS), 1)
        self.assertEqual(self.bank.statistic(Statistic.FAULTS), 0)

    def test_no_age_progress_during_boot(self):
        """Check that no age progress during boot."""
        self.begin()
        for _ in range(self.cfg.idle_ticks+2):
            self.bank.tick(True, None)
        self.assertEqual((self.bank.mode, self.bank.age), (MarketMode.BOOT, 0))
        self.send(Operation.SNAP_END)
        self.bank.tick(True, None)
        self.assertEqual(self.bank.age, 1)

    def test_heartbeat_resets_lease_on_expiry_edge(self):
        """Check that heartbeat resets lease on expiry edge."""
        self.bootstrap()
        for _ in range(self.cfg.idle_ticks-1):
            self.bank.tick(True, None)
        watchdog = self.bank.process(cmd(Operation.HEARTBEAT, seq=self.bank.expected))
        self.bank.tick(True, watchdog)
        self.assertEqual(self.bank.age, 0)
        self.assertEqual(self.bank.mode, MarketMode.LIVE)
        for _ in range(self.cfg.idle_ticks):
            self.bank.tick(True, None)
        self.assertEqual(self.bank.mode, MarketMode.FENCED)
        self.assertEqual(self.bank.statistic(Statistic.TIMEOUTS), 1)
        self.assertEqual(self.bank.book.orders[0].size, 17)

    def test_mode_fault_wins_over_coincident_timeout(self):
        """Check that mode fault wins over coincident timeout."""
        self.bootstrap()
        for _ in range(self.cfg.idle_ticks-1):
            self.bank.tick(True, None)
        outcome = self.bank.process(cmd(Operation.HEARTBEAT, seq=self.bank.expected+1))
        self.bank.tick(True, outcome)
        self.assertEqual(self.bank.statistic(Statistic.TIMEOUTS), 0)
        self.assertEqual(self.bank.statistic(Statistic.GAPS), 1)
        self.assertEqual(self.bank.age, self.cfg.idle_ticks-1)

    def test_query_counters_before_their_own_increment(self):
        """Check that query counters before their own increment."""
        self.bootstrap()
        responses = [self.send(Operation.STAT, stat=Statistic.COMMANDS) for _ in range(5)]
        self.assertEqual([item.statistic for item in responses], [4, 5, 6, 7, 8])
        count = self.send(Operation.STAT, stat=Statistic.UPDATES)
        self.assertEqual(count.statistic, 2)
        self.assertEqual(self.send(Operation.STAT, stat=Statistic.SNAPSHOTS).statistic, 1)

    def test_snapshot_restarts_only_current_epoch_data_in_place(self):
        """Check that snapshot restarts only current epoch data in place."""
        self.bootstrap()
        self.send(Operation.ADD, Status.ID, id_=self.cfg.slots, qty=5)
        response = self.begin(899)
        self.assertEqual((response.epoch, response.expected), (2, 900))
        self.assertFalse(self.send(Operation.BBO).bid_present)
        self.assertEqual(self.bank.statistic(Statistic.UPDATES), 2)
        self.assertEqual(self.bank.statistic(Statistic.FAULTS), 1)

    def test_widened_aggregate_spans_all_slots(self):
        """Check that widened aggregate spans all slots."""
        self.begin(5)
        for key in range(self.cfg.slots):
            self.send(Operation.SNAP_ADD, id_=key, qty=self.cfg.quantity_max, px=6)
        self.send(Operation.SNAP_END)
        quote = self.send(Operation.BBO)
        required = self.cfg.slots*self.cfg.quantity_max
        self.assertEqual((quote.bid_count, quote.bid_quantity), (self.cfg.slots, required))
        self.assertGreater(required, self.cfg.quantity_max)


class PolicyCase(unittest.TestCase):
    def setUp(self):
        """Initialize the independent model and geometry exercised by this test group."""
        self.cfg = Config()

    @staticmethod
    def bbo(bid, ask, buy_size=17, sell_size=17):
        """Build or read a best-bid and best-offer fixture for the test policy."""
        return BBO(Leg(True, bid, Depth(buy_size, 1)), Leg(True, ask, Depth(sell_size, 1)))

    def ask_policy(self, bbo, cfg=None):
        """Evaluate the quote policy with the fixture's selected ask parameters."""
        return quote_policy(cfg or self.cfg, MarketMode.LIVE, 4, bbo)

    def test_midpoint_rounds_outward(self):
        """Check that midpoint rounds outward."""
        answer = self.ask_policy(self.bbo(2, 9))
        self.assertEqual((answer.bid, answer.ask, answer.size), (4, 7, 12))
        self.assertEqual(answer.epoch, 4)
        self.assertFalse(answer.pressure_bid or answer.pressure_ask)

    def test_quarter_threshold_applies_at_equality_in_both_directions(self):
        """Check that quarter threshold applies at equality in both directions."""
        up = self.ask_policy(self.bbo(2, 8, 5, 3))
        down = self.ask_policy(self.bbo(2, 8, 3, 5))
        self.assertEqual((up.bid, up.ask, up.size, up.pressure_bid), (5, 7, 3, True))
        self.assertEqual((down.bid, down.ask, down.size, down.pressure_ask), (3, 5, 3, True))

    def test_below_threshold_uses_no_skew(self):
        """Check that below threshold uses no skew."""
        answer = self.ask_policy(self.bbo(2, 8, 11, 9))
        self.assertEqual((answer.bid, answer.ask), (4, 6))
        self.assertFalse(answer.pressure_bid or answer.pressure_ask)

    def test_asymmetric_skew_is_still_passive_at_both_ends(self):
        """Check that asymmetric skew is still passive at both ends."""
        cfg = replace(self.cfg, skew_ticks=12, minimum_spread=1)
        high = self.ask_policy(self.bbo(12, 14, 100, 1), cfg)
        low = self.ask_policy(self.bbo(1, 3, 1, 100), cfg)
        self.assertEqual((high.bid, high.ask), (13, 15))
        self.assertEqual((low.bid, low.ask), (0, 2))

    def test_reasons_report_invalid_cross_spread_and_zero_size(self):
        """Check that reasons report invalid cross spread and zero size."""
        locked = self.ask_policy(self.bbo(7, 7))
        narrow = self.ask_policy(self.bbo(7, 8))
        absent = self.ask_policy(BBO(Leg(), Leg()))
        disabled = self.ask_policy(self.bbo(2, 4), replace(self.cfg, quote_size=0))
        self.assertEqual(
            [locked.reason, narrow.reason, absent.reason, disabled.reason],
            [Reason.CROSSED, Reason.SPREAD, Reason.NO_SIDES, Reason.ZERO_SIZE],
        )
        self.assertTrue(all(not q.active and not q.size for q in (locked, narrow, absent, disabled)))


class EngineCase(unittest.TestCase):
    def setUp(self):
        """Initialize the independent model and geometry exercised by this test group."""
        self.cfg = replace(PROFILES["edge"], quote_size=3)
        self.engine = Engine(self.cfg)

    def seed(self):
        """Load the test engine's starting instrument state."""
        feed(self.engine, cmd(Operation.SNAP_BEGIN, seq=2))
        feed(self.engine, cmd(Operation.SNAP_ADD, seq=3, qty=10))
        feed(self.engine, cmd(Operation.SNAP_ADD, seq=4, id_=1, side=True, px=2, qty=8))
        feed(self.engine, cmd(Operation.SNAP_END, seq=5))

    def test_diagnostic_slot_accepts_and_drains_same_edge(self):
        """Check that diagnostic slot accepts and drains same edge."""
        a = cmd(Operation.BBO, tag=91)
        b = cmd(Operation.STAT, stat=Statistic.COMMANDS, tag=92)
        first = feed(self.engine, a)
        self.assertTrue(first.source_ready)
        self.assertIsNone(first.response)
        second = feed(self.engine, b)
        self.assertEqual(second.response.tag, 91)
        third = feed(self.engine)
        self.assertEqual((third.response.tag, third.response.statistic), (92, 1))
        self.assertIsNone(feed(self.engine).response)

    def test_diagnostic_stall_prevents_receipt_but_not_tick_fence(self):
        """Check that diagnostic stall prevents receipt but not tick fence."""
        self.seed()
        before = feed(self.engine, cmd(Operation.HEARTBEAT, seq=6), tick=True, response_ready=False)
        self.assertFalse(before.source_ready)
        self.assertTrue(before.live[0])
        later = feed(self.engine, response_ready=False)
        self.assertEqual(before.response, later.response)
        self.assertFalse(later.live[0])
        self.assertFalse(later.eligible[0])

    def test_registers_produce_three_cycle_unloaded_quote_latency(self):
        """Check that registers produce three cycle unloaded quote latency."""
        self.seed()
        one = feed(self.engine)
        two = feed(self.engine)
        three = feed(self.engine)
        self.assertTrue(one.eligible[0])
        self.assertIsNone(one.quote)
        self.assertIsNone(two.quote)
        self.assertTrue(three.quote.active)
        self.assertTrue(three.quote_current)
        self.assertEqual(three.quote.revision, 0)

    def test_held_quote_payload_immutable_but_fence_immediate(self):
        """Check that held quote payload immutable but fence immediate."""
        self.seed()
        feed(self.engine, quote_ready=False)
        feed(self.engine, quote_ready=False)
        before = feed(self.engine, tick=True, quote_ready=False)
        after = feed(self.engine, quote_ready=False)
        self.assertTrue(before.quote.active and before.quote_current)
        self.assertEqual(before.quote, after.quote)
        self.assertFalse(after.quote_current or after.eligible[0] or after.live[0])
        feed(self.engine)
        next_ = feed(self.engine)
        self.assertEqual((next_.quote.active, next_.quote.revision), (False, 1))

    def test_hold_superseded_same_epoch_desire(self):
        """Check that hold superseded same epoch desire."""
        self.seed()
        feed(self.engine, quote_ready=False)
        feed(self.engine, quote_ready=False)
        earlier = feed(self.engine, cmd(Operation.CANCEL, seq=6, id_=1, qty=7), quote_ready=False)
        later = feed(self.engine, quote_ready=False)
        self.assertEqual(earlier.quote, later.quote)
        self.assertEqual(earlier.epochs, later.epochs)
        self.assertTrue(later.eligible[0])
        self.assertFalse(later.quote_current)

    def test_reset_clears_visible_output_and_epoch(self):
        """Check that reset clears visible output and epoch."""
        self.seed()
        feed(self.engine, quote_ready=False)
        feed(self.engine, quote_ready=False)
        old = feed(self.engine, reset=True, response_ready=False, quote_ready=False)
        self.assertFalse(old.source_ready)
        self.assertIsNone(old.response)
        self.assertIsNone(old.quote)
        new = feed(self.engine)
        self.assertEqual(new.epochs, (0,))
        self.assertFalse(new.live[0])

    def test_equal_order_ids_cross_bank_are_independent(self):
        """Check that equal order ids cross bank are independent."""
        engine = Engine(PROFILES["default"])
        for bank, quantity in ((0, 10), (1, 22)):
            feed(engine, cmd(Operation.SNAP_BEGIN, bank=bank))
            feed(engine, cmd(Operation.SNAP_ADD, bank=bank, seq=1, qty=quantity))
            feed(engine, cmd(Operation.SNAP_END, bank=bank, seq=2))
        feed(engine, cmd(Operation.DELETE, bank=0, seq=3))
        self.assertFalse(engine.instruments[0].book.orders)
        self.assertEqual(engine.instruments[1].book.orders[0].size, 22)
        self.assertEqual(engine.instruments[1].expected, 3)


class PublicationCase(unittest.TestCase):
    def test_round_robin_order_and_per_lane_revision_wrap(self):
        """Check that round robin order and per lane revision wrap."""
        cfg = replace(Config(), banks=3, revision_width=2)
        publisher = Publisher(cfg)
        seen = []
        for iteration in range(15):
            desired = [Intent(True, 1, 0, 2+bank, iteration+1) for bank in range(cfg.banks)]
            selected = publisher.advance(desired, True)
            seen.append((selected, publisher.output.revision))
        self.assertEqual([lane for lane, _ in seen], [0, 1, 2]*5)
        self.assertEqual([revision for _, revision in seen], [0]*3+[1]*3+[2]*3+[3]*3+[0]*3)

    def test_inactive_epoch_change_is_suppressed_until_first_intent(self):
        """Check that inactive epoch change is suppressed until first intent."""
        publisher = Publisher(Config())
        for epoch in range(11):
            self.assertIsNone(publisher.advance([Intent(epoch=epoch), Intent()], True))
            self.assertIsNone(publisher.output)
        target = Intent(True, 11, 4, 7, 2, Reason.QUOTING)
        self.assertEqual(publisher.advance([target, Intent()], True), 0)
        self.assertEqual(publisher.output.epoch, 11)


if __name__ == "__main__":
    unittest.main()
