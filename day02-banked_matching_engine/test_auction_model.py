"""Known-answer, conservation and wire-contract tests for the auction oracle."""

from dataclasses import replace
from itertools import product
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
import unittest

from auction_bridge import Closing, check_paths, checkpoint_for, choose_closing
from auction_model import Auction, Command, Geometry, Kind, Op, Point, Reject, Result
from auction_verify import Sample, Scoreboard
from reserve_checkpoint import ImageOrder
from reserve_model import PROFILES as RESERVE


G = Geometry(9, 12, 8, 4, 3, 11)


def open_book(*, low=0, high=8, reference=4, half=False):
    book = Auction(G)
    [event] = book.execute(Command(Op.OPEN, 1, low=low, high=high, price=reference, reference_half=half))
    if event.kind != Kind.OPENED:
        raise AssertionError(f"invalid test opening: {event}")
    return book


def add(book, id, side, price, quantity, *, owner=1, market=False, carry=False):
    [event] = book.execute(Command(Op.ADD, book.epoch, id=id, side=side, price=price, owner=owner,
                                   quantity=quantity, carry=carry, market=market))
    if event.kind != Kind.ADDED:
        raise AssertionError(f"invalid test order: {event}")


class ClearingSelection(unittest.TestCase):
    def test_optimized_volume_overrides_reference(self):
        book = open_book(reference=7)
        add(book, 0, 0, 3, 7)
        add(book, 1, 1, 3, 9)
        add(book, 2, 1, 5, 3)
        self.assertEqual(book.select_price(), Point(3,7,9))

    def test_imbalance_precedes_distance_with_equal_executable_volume(self):
        book = open_book(reference=2)
        add(book, 0, 0, 2, 7)
        add(book, 1, 0, 5, 6)
        add(book, 2, 1, 2, 5)
        self.assertEqual(book.point(2), Point(2,13,5))
        self.assertEqual(book.point(3), Point(3,6,5))
        self.assertEqual(book.select_price(), Point(3,6,5))

    def test_fractional_reference_produces_explicit_lower_tick_tiebreak(self):
        book = open_book(reference=3, half=True)
        add(book, 0, 0, 5, 4)
        add(book, 1, 1, 2, 4)
        [event] = book.execute(Command(Op.INDICATE, 1))
        self.assertEqual((event.price, event.quantity, event.reference_half), (3,4,True))
        self.assertEqual(book.select_price(), Point(3,4,4))
        closest = open_book(reference=4)
        for order in book.orders.values():
            add(closest, order.id, order.side, order.price, order.quantity)
        self.assertEqual(closest.select_price(), Point(4,4,4))

    def test_only_scans_candidates_inside_inclusive_price_collar(self):
        book = open_book(low=6, high=6, reference=6)
        add(book, 0, 0, 5, 3, carry=True)
        add(book, 1, 1, 4, 3, carry=True)
        results = book.execute(Command(Op.CLOSE, 1))
        self.assertEqual([r.kind for r in results], [Kind.CLEARING,Kind.CARRY,Kind.CARRY,Kind.DONE])
        self.assertEqual((results[0].price, results[0].quantity), (6,0))

    def test_histogram_sums_do_not_clip_to_per_order_quantity_width(self):
        book = open_book(reference=3)
        for i in range(G.slots):
            add(book, i, i % 2, 3, G.quantity_max)
        [event] = book.execute(Command(Op.INDICATE, 1))
        self.assertEqual((event.bids, event.asks, event.quantity), (1530, 1530, 1530))
        self.assertEqual(event, Result.unpack(event.pack(G), G))

    def test_all_points_monotone_for_exhaustive_two_limit_and_two_market_cases(self):
        for bid_tick, ask_tick, bid_market, ask_market in product(range(4),range(4),(0,1),(0,1)):
            book = open_book(reference=1)
            add(book, 0, 0, bid_tick if not bid_market else 0, 2, market=bid_market)
            add(book, 1, 1, ask_tick if not ask_market else 0, 3, market=ask_market)
            points = [book.point(p) for p in range(G.prices)]
            for a,b in zip(points,points[1:]):
                self.assertGreaterEqual(a.bids,b.bids)
                self.assertLessEqual(a.asks,b.asks)


class AllocationAndEpoch(unittest.TestCase):
    def test_market_then_price_then_time_respects_partially_reduced_row(self):
        book = open_book(reference=4)
        for id, side, price, qty, market in (
            (0,0,4,5,False),(1,0,4,7,False),(2,0,6,2,False),(3,0,0,3,True),
            (4,1,4,9,False),
        ):
            add(book,id,side,price,qty,market=market)
        [reduced] = book.execute(Command(Op.REDUCE,1,owner=1,id=0,quantity=2))
        self.assertEqual(reduced.quantity,2)
        [indicative] = book.execute(Command(Op.INDICATE,1))
        close = book.execute(Command(Op.CLOSE,1))
        self.assertEqual(indicative.quantity,close[0].quantity)
        self.assertEqual([(r.first,r.quantity) for r in close if r.kind == Kind.TRADE],
                         [(3,3),(2,2),(0,2),(1,2)])
        residual = [r for r in close if r.kind == Kind.EXPIRE]
        self.assertEqual([(r.first,r.quantity) for r in residual],[(1,5)])

    def test_cancel_reused_id_gets_new_age_even_with_old_tag(self):
        book = open_book(reference=4)
        add(book, 5, 0, 4, 5)
        add(book, 2, 0, 4, 5)
        book.execute(Command(Op.CANCEL,1,id=5,owner=1))
        add(book, 5, 0, 4, 5)
        add(book, 8, 1, 4, 5)
        fills = [r for r in book.execute(Command(Op.CLOSE,1)) if r.kind == Kind.TRADE]
        self.assertEqual([r.first for r in fills],[2])

    def test_stale_earlier_epoch_and_foreign_owner_preserve_all_entries(self):
        book = open_book()
        add(book, 0, 0, 4, 20, owner=3)
        before = book.orders.copy()
        for request, reason in (
            (Command(Op.CANCEL,0),Reject.EPOCH),
            (Command(Op.CANCEL,1,owner=4),Reject.OWNER),
            (Command(Op.OPEN,2),Reject.PHASE),
            (Command(Op.CLOSE,1,reference_half=True),Reject.FIELDS),
        ):
            [reply] = book.execute(request)
            self.assertEqual(reply.reason,reason)
            self.assertEqual(book.orders,before)

    def test_epoch_wrap_requires_exact_next_modulo_code(self):
        book = Auction(Geometry(1,1,3,2,2,4))
        for serial in range(1,11):
            code = serial & 3
            [wrong] = book.execute(Command(Op.OPEN,(code+1)&3))
            self.assertEqual(wrong.reason,Reject.EPOCH)
            self.assertEqual(book.execute(Command(Op.OPEN,code))[0].kind,Kind.OPENED)
            self.assertEqual(book.execute(Command(Op.ABORT,code))[0].kind,Kind.ABORTED)

    def test_random_execution_conserves_each_order_and_both_sides(self):
        rng = Random(212903)
        for _ in range(50):
            book = open_book(low=2,high=7,reference=4,half=True)
            original = {}
            for id in range(G.slots):
                side = rng.randrange(2)
                size = rng.randint(1,85)
                market = rng.randrange(4) == 0
                carry = not market and rng.randrange(2) == 1
                add(book,id,side,0 if market else rng.randrange(G.prices),size,market=market,carry=carry)
                original[id] = size
            events = book.execute(Command(Op.CLOSE,1))
            traded = 0
            for event in events:
                if event.kind == Kind.TRADE:
                    original[event.first] -= event.quantity
                    original[event.second] -= event.quantity
                    self.assertEqual(event.price,events[0].price)
                    traded += event.quantity
                elif event.kind in (Kind.CARRY,Kind.EXPIRE):
                    original[event.first] -= event.quantity
            self.assertEqual(original,dict.fromkeys(original,0))
            self.assertEqual(traded,events[-1].quantity)
            self.assertFalse(book.orders)


class CaptureAndBridge(unittest.TestCase):
    def test_wire_payload_round_trip_and_spill_rejection(self):
        command = Command(Op.OPEN, 3, 44, price=2, high=7, reference_half=True)
        self.assertEqual(command, Command.unpack(command.pack(G),G))
        self.assertRaises(ValueError,replace(command, price=31).pack,G)
        self.assertRaises(ValueError,Result.unpack,1 << sum(G.result_widths),G)

    def test_scoreboard_detects_stall_motion_and_invalid_offer_latency(self):
        op = Command(Op.OPEN,1,price=3,high=6)
        valid = Result(Kind.OPENED,epoch=1,price=3)
        for got,error in (
            (None,"start on edge"),
            (replace(valid,price=4),"expected Result"),
        ):
            check = Scoreboard(G)
            check.step(Sample(0,True,False,False,None,None))
            check.step(Sample(1,False,True,False,op,None))
            with self.assertRaisesRegex(AssertionError,error):
                check.step(Sample(2,False,False,False,None,got))
        check = Scoreboard(G)
        check.step(Sample(0,True,False,False,None,None))
        check.step(Sample(1,False,True,False,op,None))
        check.step(Sample(2,False,False,False,None,valid))
        with self.assertRaisesRegex(AssertionError,"held result"):
            check.step(Sample(3,False,False,False,None,replace(valid,price=2)))

    def test_bridge_rejects_crossed_collar_residual(self):
        crossed = (
            ImageOrder(0,0,5,1,1,0,1),
            ImageOrder(1,1,4,1,1,0,2),
        )
        close = Closing(1,2,3,6,0,0,0,crossed)
        with self.assertRaisesRegex(ValueError,"crossed"):
            checkpoint_for(close,RESERVE["default"],0)

    def test_bridge_requires_complete_unambiguous_close_and_unique_files(self):
        close = Closing(1,2,3,5,1,1,0,())
        self.assertRaises(ValueError,choose_closing,[])
        self.assertRaises(ValueError,choose_closing,[close,close])
        self.assertEqual(choose_closing([close,close],1),close)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            filename = root/"trace"
            filename.write_text("trace",encoding="ascii")
            alias = root/"alias"
            alias.symlink_to(filename)
            with self.assertRaisesRegex(ValueError,"distinct files"):
                check_paths(filename,alias,None)


if __name__ == "__main__":
    unittest.main()
