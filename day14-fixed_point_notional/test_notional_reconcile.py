"""Check batch identity, exact arithmetic, capture fragments, and error reporting."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from fractions import Fraction
from io import BytesIO, StringIO
import json
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
import unittest

from notional_batch import BatchError
from notional_capture import ReplyEvent, RequestEvent
from notional_model import Kind, Reply, Request, Rounding, U64, calculate
from notional_reconcile import (
    CaptureSegment, FlowTotals, Plan, Planned, ReconcileError, Reconciler,
    captured_events, csv_plan, difference, image_plan, load_plan, main, mathematical_reply,
    reconcile, rounding_result, segments_for, shifted, totals_delta,
)
from notional_wire import ImageError, ImageHeader, ImageRecord, save_image
from notional_workload import model_latency

HEADER = "tag,instrument,side,price,quantity,fee_bps,rounding,limit\n"


def book_csv(rows):
    """Create a plan fragment with the documented canonical field order."""
    return HEADER + "\n".join(",".join(str(cell) for cell in row) for row in rows) + "\n"


def plan_from_orders(orders, fraction, instruments=None):
    """Wrap raw fixture requests without duplicating the CSV validation path."""
    if instruments is None:
        instruments = ["TEST"] * len(orders)
    return Plan(fraction, tuple(
        Planned(index, symbol, order, mathematical_reply(order, fraction))
        for index, (symbol, order) in enumerate(zip(instruments, orders))
    ), "fixture")


def transactions(orders, frac, first=1):
    """Build transfers from the original integer model with varied accepted-output stalls."""
    at = []
    cycle = first
    for index, order in enumerate(orders):
        reference = calculate(order, frac)
        delay = model_latency(reference.status)
        waiting = index % 6
        at.append(RequestEvent(cycle, order))
        at.append(ReplyEvent(cycle+delay+waiting, cycle+delay, reference))
        cycle += delay + waiting + 1
    return tuple(at)


def trace_row(event):
    """Write one strict simulator-format request or response line."""
    if isinstance(event, RequestEvent):
        return "req " + str(event.cycle) + " " + " ".join(format(field, "x")
                                                         for field in event.request.wire())
    return (f"rsp {event.cycle} {event.first_visible} "
            + " ".join(format(field, "x") for field in event.reply.wire()))


def save_trace(path, rows):
    """Write a reproducible sequence of hardware-visible edge observations."""
    path.write_text("".join(trace_row(row)+"\n" for row in rows), encoding="ascii")


class MathTests(unittest.TestCase):
    """Prove by examples that the host uses independent exact rational arithmetic."""

    def test_both_rounding_points_keep_signed_error(self):
        """Quantize a quarter-valued notional then round its fee using each mode."""
        order = Request(4, 7, 3, 2500, Rounding.FLOOR)
        down = mathematical_reply(order, 2)
        self.assertEqual(down.exact_gross, Fraction(21,4))
        self.assertEqual(down.exact_fee, Fraction(5,4))
        self.assertEqual(down.gross_error, Fraction(-1,4))
        self.assertEqual(down.fee_error, Fraction(-1,4))
        self.assertEqual(down.reply, Reply(4, Kind.OK, False, 5, 1, 6, True))
        up = mathematical_reply(replace(order, rounding=Rounding.CEIL, sell=True), 2)
        self.assertEqual(up.exact_gross, Fraction(21,4))
        self.assertEqual(up.exact_fee, Fraction(3,2))
        self.assertEqual(up.gross_error, Fraction(3,4))
        self.assertEqual(up.fee_error, Fraction(1,2))
        self.assertEqual(up.reply, Reply(4, Kind.OK, True, 6, 2, 4, True))

    def test_ties_choose_even_in_both_gross_and_fee(self):
        """Use Fraction's tie-to-even rule consistently in both steps."""
        ties = (Fraction(1,2), Fraction(3,2), Fraction(5,2), Fraction(7,2))
        self.assertEqual([rounding_result(n, Rounding.NEAREST_EVEN) for n in ties], [0,2,2,4])
        order = Request(4, 7, 3, 5000, Rounding.NEAREST_EVEN)
        result = mathematical_reply(order, 1)
        self.assertEqual(result.reply, Reply(4, Kind.OK, False, 10, 5, 15, True))
        self.assertEqual(result.gross_error, Fraction(-1,2))
        self.assertEqual(result.fee_error, 0)
        with self.assertRaisesRegex(ReconcileError, "unsupported"):
            rounding_result(Fraction(1,2), 3)

    def test_validation_precedence_and_overflow(self):
        """Do not evaluate invalid domains or turn saturated buys into successful cash."""
        both = Request(7, 0, 0, 65535, 3, False, 0)
        zero_qty = mathematical_reply(both, 0)
        zero_price = mathematical_reply(replace(both, quantity=1), 0)
        bad_fee = mathematical_reply(replace(both, quantity=1, price=1), 0)
        bad_mode = mathematical_reply(replace(both, quantity=1, price=1, fee_bps=0), 0)
        self.assertEqual(
            [item.reply.status for item in (zero_qty, zero_price, bad_fee, bad_mode)],
            [Kind.BAD_QUANTITY, Kind.BAD_PRICE, Kind.BAD_FEE, Kind.BAD_MODE],
        )
        self.assertTrue(all(item.exact_gross is None and item.exact_fee is None
                            for item in (zero_qty, zero_price, bad_fee, bad_mode)))
        ext = (1 << 32)-1
        overflow = mathematical_reply(Request(9, ext, ext, 10000, 2, False, 3), 0)
        self.assertEqual(overflow.reply.status, Kind.OVERFLOW)
        self.assertEqual(overflow.reply.cash, U64)
        self.assertEqual(overflow.reply.fee, ext*ext)
        proceeds = mathematical_reply(Request(9, ext, ext, 10000, 2, True), 0)
        self.assertEqual(proceeds.reply.cash, 0)
        self.assertEqual(proceeds.reply.status, Kind.OK)

    def test_exact_cash_bound_direction_and_equality(self):
        """A buy ceiling and sell floor both allow an exact boundary."""
        request = Request(2, 2560, 100, 10, Rounding.NEAREST_EVEN, False, 1001)
        self.assertEqual(mathematical_reply(request, 8).reply.cash, 1001)
        self.assertEqual(mathematical_reply(request, 8).reply.status, Kind.OK)
        less = replace(request, limit=1000)
        self.assertEqual(mathematical_reply(less, 8).reply.status, Kind.LIMIT)
        sell = replace(request, sell=True, limit=999)
        self.assertEqual(mathematical_reply(sell, 8).reply.cash, 999)
        self.assertEqual(mathematical_reply(sell, 8).reply.status, Kind.OK)
        self.assertEqual(mathematical_reply(replace(sell, limit=1000), 8).reply.status, Kind.LIMIT)

    def test_fraction_oracle_matches_divmod_oracle_under_full_width_inputs(self):
        """Cross-check thousands of independent full-range arithmetic calculations."""
        rng = Random(1408946)
        for width in (0, 1, 4, 8, 15, 23, 31):
            for index in range(400):
                raw = Request(
                    index % 11, rng.randrange(2**32), rng.randrange(2**32),
                    rng.choice((0, 1, 10000, 10001, rng.randrange(0,10000))),
                    rng.randrange(4), bool(index & 1),
                    None if index % 3 else rng.randrange(2**64),
                )
                with self.subTest(frac=width, row=index):
                    self.assertEqual(mathematical_reply(raw,width).reply, calculate(raw,width))
        for width in (-1, 32, True):
            with self.subTest(width=width):
                with self.assertRaisesRegex(ReconcileError, "fraction width"):
                    mathematical_reply(Request(1,1,1), width)


class PlanTests(unittest.TestCase):
    """Check framing, sequence preservation and cross-format equivalence."""

    def test_csv_select_preserves_recycled_tags_and_absolute_ordinals(self):
        """Tags remain payload, so two distinct same-tag orders can be selected separately."""
        source = StringIO(book_csv([
            (7,"ABC","buy","1.5",5,15,"floor",""),
            (7,"DEF","sell","2.25",7,5,"ceil",10),
            (7,"ABC","buy","3.5",11,2,"nearest_even",30),
        ]))
        all_orders = csv_plan(source, 8)
        self.assertEqual([row.sequence for row in all_orders.entries], [0,1,2])
        sub = all_orders.selected(1,2)
        self.assertEqual([row.sequence for row in sub.entries], [1,2])
        self.assertEqual([row.request.tag for row in sub.entries], [7,7])
        self.assertEqual([row.instrument for row in sub.entries], ["DEF","ABC"])
        self.assertEqual(sub.entries[0].request.price, 576)
        self.assertEqual(sub.entries[0].reply.status, Kind.OK)
        self.assertEqual(sub.entries[1].reply.status, Kind.LIMIT)

    def test_csv_quantization_and_empty_plan(self):
        """Reject unrepresentable grids unless the host specifies a quantization mode."""
        raw = book_csv([(1,"SYM","buy","1.3",3,0,"nearest_even","")])
        with self.assertRaisesRegex(BatchError, "grid"):
            csv_plan(StringIO(raw), 2)
        order = csv_plan(StringIO(raw), 2, Rounding.CEIL).entries[0]
        self.assertEqual(order.request.price, 6)
        self.assertEqual(order.reply.cash, 4)
        with self.assertRaisesRegex(ReconcileError, "at least one"):
            csv_plan(StringIO(HEADER), 8)
        with self.assertRaisesRegex(BatchError, "required CSV header"):
            csv_plan(StringIO("required,other\n1,2"), 8)
        with self.assertRaisesRegex(BatchError, "line 2"):
            csv_plan(StringIO(HEADER + "abc,too,short\n"), 3)

    def test_selection_bounds_and_nonconsecutive_plan(self):
        """An apparent selected range must describe the actual contiguous source order."""
        base = plan_from_orders([Request(4,10,1), Request(4,11,1)], 0)
        for start, count in ((-1,1),(2,1),(1,0),(1,2),(0,-1)):
            with self.subTest(start=start,count=count):
                with self.assertRaisesRegex(ReconcileError, "sequence"):
                    base.selected(start,count)
        with self.assertRaisesRegex(ReconcileError, "consecutive"):
            replace(base, entries=(base.entries[1],base.entries[0]))

    def test_binary_semantically_equivalent_to_csv_and_validates_crc(self):
        """Verify that captured plans carry independent arithmetic and exact stored rows."""
        reference = plan_from_orders([Request(5,1024,7,10), Request(5,300,9,20,sell=True)], 8)
        binary = BytesIO()
        save_image(binary, ImageHeader(8,2), [row.request for row in reference.entries])
        raw = binary.getvalue()
        recovered = image_plan(BytesIO(raw), 8)
        self.assertEqual([row.request for row in recovered.entries], [row.request for row in reference.entries])
        self.assertEqual([row.reply for row in recovered.entries], [row.reply for row in reference.entries])
        self.assertEqual({row.instrument for row in recovered.entries}, {"<unlabeled>"})
        with self.assertRaisesRegex(ReconcileError, "expected Q7"):
            image_plan(BytesIO(raw), 7)
        with self.assertRaisesRegex(ImageError, "extra"):
            image_plan(BytesIO(raw+b"other"))
        with self.assertRaisesRegex(ImageError, "CRC"):
            image_plan(BytesIO(raw[:-1]+bytes([raw[-1]^1])))
        with self.assertRaisesRegex(ImageError, "truncated"):
            image_plan(BytesIO(raw[:-6]))

    def test_binary_with_forged_math_and_valid_crc_is_rejected(self):
        """A CRC guards transport, not the arithmetic meaning of those bytes."""
        order = Request(95,18,23,92,2)
        correct = mathematical_reply(order, 8).reply
        forged = ImageRecord(0, order, replace(correct, cash=correct.cash+1))
        source = BytesIO(ImageHeader(8,1).bytes() + forged.encoded())
        with self.assertRaisesRegex(ReconcileError, "disagrees with exact arithmetic"):
            image_plan(source)
        wrong_sequence = ImageRecord(4, order, correct)
        source = BytesIO(ImageHeader(8,1).bytes() + wrong_sequence.encoded())
        with self.assertRaisesRegex(ImageError, "sequence mismatch"):
            image_plan(source)

    def test_repeated_slice_uses_original_absolute_sequence_coordinates(self):
        """Subselection cannot reinterpret an original sequence as a list index."""
        complete = plan_from_orders([Request(4,index+1,1) for index in range(10)],0)
        cut = complete.selected(3,5)
        inside = cut.selected(4,2)
        self.assertEqual([row.sequence for row in inside.entries],[4,5])
        self.assertEqual([row.request.price for row in inside.entries],[5,6])
        self.assertEqual(inside.selected(),inside)
        self.assertEqual([row.sequence for row in cut.selected(count=2).entries],[3,4])
        for outside in (0,2,8,9):
            with self.subTest(outside=outside):
                with self.assertRaisesRegex(ReconcileError,"sequence start"):
                    cut.selected(outside)

    def test_path_formats_and_already_quantized_images(self):
        """Reject ambiguous extensions or an inappropriate binary quantization option."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = root/"plan.csv"
            text.write_text(book_csv([(1,"Z","buy",3,10,0,"floor","")]))
            image = root/"plan.nom1"
            with image.open("wb") as target:
                save_image(target,ImageHeader(12,1), [Request(1,4096,1)])
            self.assertEqual(load_plan(text).fraction_bits, 8)
            self.assertEqual(load_plan(image).fraction_bits, 12)
            with self.assertRaisesRegex(ReconcileError, "already quantized"):
                load_plan(image, quantize=Rounding.CEIL)
            with self.assertRaisesRegex(ReconcileError, "must end"):
                load_plan(root/"plan")


class ReconcileTests(unittest.TestCase):
    """Exercise ordered association, diagnosed errors, missing suffixes and net cash."""

    def setUp(self):
        """Build two same-tag orders and a different-tag rejected order."""
        self.buy = Request(7, 512, 10, 500, Rounding.FLOOR, False, 21)
        self.sell = Request(7, 256, 6, 500, Rounding.CEIL, True, 4)
        self.reject = Request(8, 256, 0, 0, Rounding.NEAREST_EVEN, False, 0)
        self.plan = plan_from_orders([self.buy,self.sell,self.reject], 8, ["ABC","XYZ","XYZ"])
        self.good = transactions([self.buy,self.sell,self.reject],8)

    def test_success_computes_independent_per_symbol_net_cash(self):
        """Each ordinal is matched despite a reused hardware tag."""
        result = reconcile(self.plan, self.good)
        self.assertEqual(result["fully_matched"], 3)
        self.assertEqual(result["issues_by_code"], {})
        self.assertEqual(result["planned_statuses"], {"OK":2,"BAD_QUANTITY":1})
        self.assertEqual(result["captured_statuses"], result["planned_statuses"])
        self.assertEqual(result["first_visible_edges"], {1:1,85:2})
        self.assertEqual(result["response_hold_edges"], {0:1,1:1,2:1})
        self.assertEqual(result["instruments"]["ABC"]["observed"]["debits"], 21)
        self.assertEqual(result["instruments"]["ABC"]["observed"]["net"], -21)
        self.assertEqual(result["instruments"]["XYZ"]["observed"]["proceeds"], 5)
        self.assertEqual(result["instruments"]["XYZ"]["observed"]["net"], 5)
        self.assertEqual(result["instruments"]["XYZ"]["observed"]["rejected"], 1)
        self.assertTrue(all(delta == 0 for ledgers in result["instruments"].values()
                            for delta in ledgers["difference"].values()))

    def test_same_tag_wrong_ordinal_is_reported_without_matching_later_rows(self):
        """A reorder cannot be mistaken for two valid same-tag matches."""
        wrong_order = transactions([self.sell,self.buy,self.reject],8)
        result = reconcile(self.plan,wrong_order)
        self.assertEqual(result["fully_matched"],1)
        self.assertEqual(result["paired_replies"],3)
        self.assertEqual(result["attributed_replies"],1)
        self.assertEqual(result["issues_by_code"], {"request_mismatch":2})
        self.assertEqual([row["sequence"] for row in result["diagnostics"]],[0,1])
        self.assertIn("sell",result["diagnostics"][0]["fields"])
        self.assertIn("price",result["diagnostics"][0]["fields"])
        orphan_book = result["instruments"]["<unmatched>"]
        self.assertEqual(orphan_book["observed"]["debits"], 21)
        self.assertEqual(orphan_book["observed"]["proceeds"],5)

    def test_cash_corruption_shows_concrete_amount_and_ledgers(self):
        """Keep measured and expected cash separate even when arithmetic disagrees."""
        original = self.good[1]
        fault = replace(original,reply=replace(original.reply,cash=55))
        trace = (self.good[0],fault)+self.good[2:]
        result = reconcile(self.plan,trace)
        self.assertEqual(result["issues_by_code"], {"reply_mismatch":1})
        self.assertEqual(result["diagnostics"][0]["fields"], {
            "cash":{"expected":21,"observed":55},
        })
        self.assertEqual(result["instruments"]["ABC"]["difference"]["debits"],34)
        self.assertEqual(result["instruments"]["ABC"]["difference"]["net"],-34)
        self.assertEqual(result["fully_matched"],2)
        self.assertEqual(result["attributed_replies"],3)

    def test_response_timing_mismatch_retains_observed_hold(self):
        """A plausible cash result still fails if it was visible on the wrong edge."""
        original = self.good[1]
        fault = replace(original,first_visible=original.first_visible-1)
        trace = (self.good[0],fault)+self.good[2:]
        result = reconcile(self.plan,trace)
        self.assertEqual(result["issues_by_code"], {"timing_mismatch":1})
        self.assertEqual(result["diagnostics"][0]["fields"],
                         {"elapsed":84,"expected":85,"transfer_hold":1})
        self.assertEqual(result["fully_matched"],2)
        self.assertTrue(all(delta == 0 for delta in result["instruments"]["ABC"]["difference"].values()))

    def test_duplicate_replies_cannot_double_count_cash(self):
        """Classify a repeated last result while an unrelated request is pending."""
        req0, rsp0, req1, rsp1, req2, rsp2 = transactions([self.buy,self.reject,self.sell],8,10)
        middle = plan_from_orders([self.buy,self.reject,self.sell],8)
        dup = ReplyEvent(req1.cycle+1,req1.cycle+1,rsp0.reply)
        shifted_1 = shifted(rsp1,1)
        trace = req0, rsp0, req1, dup, shifted_1, shifted(req2,2), shifted(rsp2,2)
        result = reconcile(middle,trace)
        self.assertEqual(result["observed_replies"],4)
        self.assertEqual(result["paired_replies"],3)
        self.assertEqual(result["issues_by_code"], {"duplicate_reply":1, "timing_mismatch":1})
        self.assertEqual(result["instruments"]["TEST"]["observed"]["debits"],21)

    def test_old_response_without_outstanding_request(self):
        """Unmatched and duplicate replies are counted without assigning an instrument."""
        initial = ReplyEvent(0,0,Reply(999,Kind.OK,False,1,0,1))
        duplicate = shifted(self.good[1],1)
        rest = tuple(shifted(event,2) for event in self.good[2:])
        result = reconcile(self.plan,(initial,*self.good[:2],duplicate,*rest))
        self.assertEqual(result["observed_replies"],5)
        self.assertEqual(result["issues_by_code"], {"unmatched_reply":1,"duplicate_reply":1})
        self.assertEqual(result["fully_matched"],3)
        self.assertEqual(result["instruments"]["ABC"]["observed"]["debits"],21)
        missing, repeated = result["diagnostics"]
        self.assertEqual(missing["fields"]["cash"],1)
        self.assertEqual(missing["fields"]["status"],"OK")
        self.assertEqual(repeated["fields"]["cash"],21)
        self.assertEqual(repeated["fields"]["prior_cycle"],self.good[1].cycle)

    def test_truncated_capture_reports_pending_and_missing_separately(self):
        """The final accepted request differs from a never-admitted planned order."""
        result = reconcile(self.plan,self.good[:3])
        self.assertEqual(result["issues_by_code"], {"incomplete_request":1, "missing_request":1})
        self.assertEqual([entry["sequence"] for entry in result["diagnostics"]],[1,2])
        self.assertEqual(result["fully_matched"],1)
        self.assertEqual(result["instruments"]["XYZ"]["difference"]["transactions"],-2)

    def test_overlapping_requests_are_diagnosed_and_do_not_force_ambiguous_realignment(self):
        """Retire one deterministic pending owner and report the prior request as incomplete."""
        at = (self.good[0], RequestEvent(3,self.sell), ReplyEvent(88,88,calculate(self.sell,8)))
        result = reconcile(self.plan,at)
        self.assertEqual(result["issues_by_code"], {
            "incomplete_request":1,"missing_request":1,"request_overlap":1,
        })
        self.assertEqual(result["paired_replies"],1)
        self.assertEqual(result["fully_matched"],1)
        self.assertEqual(result["instruments"]["ABC"]["observed"]["ok"],0)
        self.assertEqual(result["instruments"]["XYZ"]["observed"]["ok"],1)

    def test_unexpected_request_into_unattributed_total(self):
        """Retain arithmetic checks for traffic outside the selected plan interval."""
        late = tuple(shifted(event,self.good[-1].cycle+1) for event in self.good[:2])
        result = reconcile(self.plan,self.good+late)
        self.assertEqual(result["issues_by_code"], {"unexpected_request":1})
        self.assertEqual(result["observed_requests"],4)
        self.assertEqual(result["paired_replies"],4)
        self.assertEqual(result["attributed_replies"],3)
        self.assertEqual(result["instruments"]["<unmatched>"]["observed"]["net"],-21)

    def test_detail_limit_counts_every_issue_even_when_details_are_omitted(self):
        """Use bounded residual memory while preserving a complete aggregate verdict."""
        blank = reconcile(self.plan,[],diagnostic_limit=1)
        self.assertEqual(blank["issue_count"],3)
        self.assertEqual(blank["omitted_diagnostics"],2)
        self.assertEqual(len(blank["diagnostics"]),1)
        quiet = reconcile(self.plan,[],diagnostic_limit=0)
        self.assertEqual(quiet["issues_by_code"], {"missing_request":3})
        self.assertEqual(quiet["diagnostics"], [])
        self.assertEqual(quiet["omitted_diagnostics"],3)
        with self.assertRaisesRegex(ReconcileError, "diagnostic limit"):
            Reconciler(self.plan,-1)

    def test_offset_slices_can_keep_absolute_sequence_diagnostics(self):
        """A truncated plan slice always names the original batch index."""
        chosen = self.plan.selected(1,2)
        actual = transactions([self.sell,self.reject],8,337)
        report = reconcile(chosen,actual)
        self.assertEqual(report["sequence_start"],1)
        self.assertEqual(report["sequence_stop"],3)
        self.assertEqual(report["fully_matched"],2)
        altered = reconcile(chosen,actual[:-1])
        self.assertEqual(altered["diagnostics"][0]["sequence"],2)

    def test_counter_order_and_closed_state(self):
        """Reject duplicated physical edge numbers or appending after finalization."""
        checker = Reconciler(self.plan)
        with self.assertRaisesRegex(ReconcileError,"unknown"):
            checker.apply({"cycle":0})
        checker.apply(self.good[0])
        with self.assertRaisesRegex(ReconcileError, "advance"):
            checker.apply(self.good[0])
        checker.finish()
        with self.assertRaisesRegex(ReconcileError, "finalized"):
            checker.apply(self.good[1])
        with self.assertRaisesRegex(ReconcileError, "finalized"):
            checker.finish()

    def test_field_difference_and_summary_do_not_treat_rejections_as_cash(self):
        """Rejected operations stay visible as counts without affecting debits or proceeds."""
        math = mathematical_reply(self.buy,8).reply
        fields = difference(math,replace(math,status=Kind.LIMIT,cash=30))
        self.assertEqual(fields, {"status":{"expected":"OK","observed":"LIMIT"},
                                  "cash":{"expected":21,"observed":30}})
        baseline = FlowTotals()
        observed = FlowTotals()
        baseline.accept(math)
        observed.accept(replace(math,status=Kind.LIMIT,cash=30))
        self.assertEqual(totals_delta(observed,baseline)["net"],21)
        self.assertEqual(observed.record()["rejected"],1)
        self.assertEqual(observed.record()["debits"],0)


    def test_randomized_multisymbol_ledger_with_repeated_tag_space(self):
        """Build expected cash by hand from requests sharing just five distinct tags."""
        generator = Random(14067)
        symbols = ["AB", "SP", "BX"]
        orders = []
        routes = []
        for index in range(240):
            symbol = symbols[index % len(symbols)]
            side = bool(generator.getrandbits(1))
            price = generator.randrange(1,50000)*16
            quantity = generator.randrange(40)
            fee = generator.choice((0, 250, 500, 10000, 10001))
            order = Request(index%5,price,quantity,fee,0,side)
            orders.append(order)
            routes.append(symbol)
        oracle = {
            name: {"gross":0,"fees":0,"debits":0,"proceeds":0,"ok":0,"rejected":0}
            for name in symbols
        }
        for route, order in zip(routes, orders):
            book = oracle[route]
            if order.quantity == 0 or order.fee_bps > 10000:
                book["rejected"] += 1
                continue
            gross = order.price//16*order.quantity
            fee = gross*order.fee_bps//10000
            book["gross"] += gross
            book["fees"] += fee
            book["ok"] += 1
            book["proceeds" if order.sell else "debits"] += gross-fee if order.sell else gross+fee
        results = reconcile(plan_from_orders(orders,4,routes),transactions(orders,4))
        self.assertEqual(results["fully_matched"],240)
        self.assertEqual(results["issue_count"],0)
        for symbol, baseline in oracle.items():
            with self.subTest(symbol=symbol):
                observed = results["instruments"][symbol]["observed"]
                for field, value in baseline.items():
                    self.assertEqual(observed[field],value)
                self.assertEqual(observed["net"],baseline["proceeds"]-baseline["debits"])

    def test_large_uniform_clock_shifts_preserve_latency_and_ledger_verdict(self):
        """Fragment rebasing changes absolute edge labels without changing durations."""
        fixture = [self.buy,self.sell,self.reject,self.sell,self.buy,self.reject]
        labels = ["ALPHA","BETA","ALPHA","BETA","ALPHA","BETA"]
        trusted = plan_from_orders(fixture,8,labels)
        original = transactions(fixture,8,13)
        reference = reconcile(trusted,original)
        for increment in (1,256,2**31,2**47,2**61):
            with self.subTest(increment=increment):
                moved = tuple(shifted(event,increment) for event in original)
                self.assertEqual(reconcile(trusted,moved),reference)
                for index,item in enumerate(moved):
                    self.assertEqual(item.cycle,original[index].cycle+increment)
        original_fault = list(original)
        original_fault[-1] = replace(
            original_fault[-1],first_visible=original_fault[-1].first_visible-1,
        )
        faulty = reconcile(trusted,original_fault)
        moved = (shifted(event,2**31) for event in original_fault)
        repeated = reconcile(trusted,moved)
        first_issue, = faulty["diagnostics"]
        moved_issue, = repeated["diagnostics"]
        self.assertEqual(moved_issue["fields"],first_issue["fields"])
        self.assertEqual(moved_issue["cycle"],first_issue["cycle"]+2**31)
        self.assertEqual(moved_issue["sequence"],first_issue["sequence"])
        self.assertEqual(repeated["instruments"],faulty["instruments"])

    def test_json_diagnostics_keep_tag_upper_bits_and_unsigned_cash(self):
        """Ensure high unsigned numeric wire values survive a structured audit report."""
        maximum = (1 << 32)-1
        expected = Request(65535,maximum,maximum,10000,2,False)
        plan = plan_from_orders([expected],0)
        valid = transactions([expected],0)
        reply = valid[-1]
        wrong = replace(reply,reply=replace(reply.reply,tag=65534,cash=U64-1))
        result = reconcile(plan,(valid[0],wrong))
        issue, = json.loads(json.dumps(result))["diagnostics"]
        self.assertEqual(issue["fields"]["tag"],{"expected":65535,"observed":65534})
        self.assertEqual(issue["fields"]["cash"],{"expected":U64,"observed":U64-1})
        self.assertEqual(issue["cycle"],reply.cycle)
        self.assertEqual(result["instruments"]["<unmatched>"]["observed"]["rejected"],1)
        self.assertEqual(result["instruments"]["<unmatched>"]["observed"]["net"],0)

    def test_identical_orders_reusing_tag_complete_independently(self):
        """An identical succeeding response is valid when its new request is outstanding."""
        orders = [self.buy]*15
        identical = reconcile(plan_from_orders(orders,8),transactions(orders,8))
        self.assertEqual(identical["issues_by_code"],{})
        self.assertEqual(identical["observed_requests"],15)
        self.assertEqual(identical["fully_matched"],15)
        self.assertEqual(identical["instruments"]["TEST"]["observed"]["debits"],15*21)
        self.assertEqual(identical["instruments"]["TEST"]["observed"]["transactions"],15)

    def test_corrupted_reply_status_stays_separate_from_planned_rejection(self):
        """A false success response never changes the predicted accepted cash ledger."""
        original = self.good[-1]
        bogus = Reply(self.reject.tag,Kind.OK,False,110,9,119,True)
        result = reconcile(self.plan,self.good[:-1]+(replace(original,reply=bogus),))
        self.assertEqual(result["issues_by_code"],{"reply_mismatch":1})
        self.assertEqual(result["fully_matched"],2)
        stats = result["instruments"]["XYZ"]
        self.assertEqual(stats["planned"]["ok"],1)
        self.assertEqual(stats["observed"]["ok"],2)
        self.assertEqual(stats["difference"]["debits"],119)
        self.assertEqual(stats["difference"]["rejected"],-1)

    def test_illegal_early_and_late_samples_report_distinct_timing_deltas(self):
        """Preserve signed visibility errors instead of silently clamping invalid edges."""
        req, rsp = self.good[:2]
        early = replace(rsp,first_visible=req.cycle-1)
        beyond = replace(rsp,first_visible=rsp.cycle+1)
        for event, latency, hold in ((early,-1,86),(beyond,86,-1)):
            with self.subTest(event=event):
                checked = reconcile(self.plan.selected(0,1),(req,event))
                self.assertEqual(checked["issues_by_code"],{"timing_mismatch":1})
                self.assertEqual(checked["first_visible_edges"],{latency:1})
                self.assertEqual(checked["response_hold_edges"],{hold:1})

class FileAndCliTests(unittest.TestCase):
    """Check multi-file clock fragments and command-line evidence."""

    def setUp(self):
        """Install a small two-order CSV plan and canonical local trace targets."""
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = self.root/"plan.csv"
        self.part1 = self.root/"trace1.txt"
        self.part2 = self.root/"trace2.txt"
        self.report = self.root/"result.json"
        self.plan.write_text(book_csv([
            (7,"IF","buy","2",10,500,"floor",21),
            (7,"IF","sell","1",6,500,"ceil",4),
        ]))
        self.orders = [item.request for item in load_plan(self.plan).entries]
        self.expected = transactions(self.orders,8,118)

    def cli(self,*args):
        """Invoke the real parser with isolated stdout and error diagnostics."""
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main([str(part) for part in args])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_split_across_an_outstanding_request_preserves_timing(self):
        """A slice can end after admission, and the following file supplies completion."""
        save_trace(self.part1,self.expected[:1])
        saved = tuple(shifted(event,-100) for event in self.expected[1:])
        save_trace(self.part2,saved)
        activity = tuple(captured_events(segments_for([self.part1,self.part2],[0,100])))
        self.assertEqual(activity,self.expected)
        exit_code, out, err = self.cli(self.plan,self.part1,self.part2,"--offset",0,"--offset",100,
                                       "--output",self.report)
        self.assertEqual((exit_code,out,err),(0,"",""))
        decoded = json.loads(self.report.read_text())
        self.assertEqual(decoded["segments"][1]["cycle_offset"],100)
        self.assertEqual(decoded["instruments"]["IF"]["observed"]["net"],-16)
        self.assertEqual(decoded["fully_matched"],2)

    def test_local_cycle_overlap_and_offset_validation(self):
        """Reject inconsistent physical edge domains before matching order identity."""
        save_trace(self.part1,self.expected[:2])
        save_trace(self.part2,self.expected[:2])
        with self.assertRaisesRegex(ReconcileError, "adjusted cycle"):
            tuple(captured_events(segments_for([self.part1,self.part2])))
        with self.assertRaisesRegex(ReconcileError, "one offset"):
            segments_for([self.part1,self.part2],[0])
        with self.assertRaisesRegex(ReconcileError, "segment offset"):
            CaptureSegment(self.part1,-1)
        with self.assertRaisesRegex(ReconcileError, "provide"):
            segments_for([])
        with self.assertRaisesRegex(ReconcileError, "adjusted cycle"):
            shifted(self.expected[0],U64)

    def test_capture_parse_errors_include_file_and_row(self):
        """Surface malformed capture rows with their file name and exact line."""
        req = trace_row(self.expected[0])
        samples = [
            (req+"\nbogus junk\n","line 2: expected"),
            (req+"\n"+req+"\n","line 2: hardware capture"),
            (req+"\n"+"rsp 200 201 7 0 0 00 00 00 0\n","line 2: response visibility"),
            ("req 117 -7 01 02 0 0 0 0 0\n","line 1: hardware fields"),
            ("req 117 0007 01 02 0 0 0 0 a\n","line 1: disabled limit"),
            ("req 117 0007 01 02 0 0 0 2 0\n","line 1: limit enable"),
        ]
        for body, message in samples:
            with self.subTest(body=body):
                self.part1.write_text(body)
                code, out, err = self.cli(self.plan,self.part1)
                self.assertEqual((code,out),(2,""))
                self.assertIn(str(self.part1),err)
                self.assertIn(message,err)
                self.assertNotIn("Traceback",err)

    def test_cli_mismatch_prints_json_and_distinguishes_error(self):
        """Machine-readable mismatch is exit 1, while invalid input is exit 2."""
        save_trace(self.part1,self.expected[:2])
        code, stdout, error = self.cli(self.plan,self.part1)
        self.assertEqual((code,error),(1,""))
        json_data = json.loads(stdout)
        self.assertEqual(json_data["fully_matched"],1)
        self.assertEqual(json_data["issues_by_code"],{"missing_request":1})
        code, stdout, error = self.cli(self.plan,self.part1,"--sequence-start",0,"--sequence-count",1)
        self.assertEqual((code,error),(0,""))
        self.assertEqual(json.loads(stdout)["expected_requests"],1)
        code, stdout, error = self.cli(self.plan,self.part1,"--sequence-count",4)
        self.assertEqual((code,stdout),(2,""))
        self.assertIn("sequence count",error)

    def test_report_never_overwrites_capture_or_plan(self):
        """Report publication checks path aliases before replacing any file."""
        save_trace(self.part1,self.expected)
        saved = self.part1.read_text()
        alias = self.root/"alias.txt"
        alias.symlink_to(self.part1)
        for path in (self.plan,self.part1,alias):
            with self.subTest(path=path):
                code, stdout, error = self.cli(self.plan,self.part1,"--output",path)
                self.assertEqual((code,stdout),(2,""))
                self.assertIn("same",error)
                self.assertEqual(self.part1.read_text(),saved)

    def test_binary_with_independent_trace_checker_accepts_same_facts(self):
        """A hardware capture reconciles identically against quantized NOM1 requests."""
        image = self.root/"plan.nom1"
        with image.open("wb") as data:
            save_image(data,ImageHeader(8,len(self.orders)),self.orders)
        save_trace(self.part1,self.expected)
        code, stdout, error = self.cli(image,self.part1)
        self.assertEqual((code,error),(0,""))
        report = json.loads(stdout)
        self.assertEqual(report["source_format"],"NOM1")
        self.assertEqual(report["instruments"]["<unlabeled>"]["observed"]["net"],-16)
        self.assertEqual(report["fully_matched"],2)


    def test_atomic_output_preserves_previous_report_after_parse_fails(self):
        """Do not replace valid evidence when the newly supplied trace is malformed."""
        save_trace(self.part1,self.expected)
        previous_status, out, err = self.cli(self.plan,self.part1,"--output",self.report)
        self.assertEqual((previous_status,out,err),(0,"",""))
        previous = self.report.read_bytes()
        self.part1.write_text("not an event\n")
        code, out, error = self.cli(self.plan,self.part1,"--output",self.report)
        self.assertEqual((code,out),(2,""))
        self.assertIn("line 1",error)
        self.assertEqual(self.report.read_bytes(),previous)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         ["plan.csv","result.json","trace1.txt"])

    def test_reuse_fragment_with_shifted_clocks_and_recycled_tags(self):
        """One per-experiment capture can be reused for a correlated second replay."""
        save_trace(self.part1,self.expected)
        extended = self.root/"duplicate_orders.csv"
        text = self.plan.read_text()
        extended.write_text(text+text.split("\n",1)[1])
        offset = self.expected[-1].cycle + 9
        status, out, err = self.cli(extended,self.part1,self.part1,"--offset",0,"--offset",offset,
                                    "--output",self.report)
        self.assertEqual((status,out,err),(0,"",""))
        payload = json.loads(self.report.read_bytes())
        self.assertEqual(payload["fully_matched"],4)
        self.assertEqual(payload["segments"][0]["path"],payload["segments"][1]["path"])
        self.assertEqual(payload["segments"][1]["cycle_offset"],offset)
        self.assertEqual(payload["instruments"]["IF"]["observed"]["net"],-32)

    def test_natural_offset_bounds_and_text_layout_errors(self):
        """Reject invalid CLI coordinates with file- or field-specific help."""
        save_trace(self.part1,self.expected)
        for settings,message in [
            (("--offset",-1),"segment offset"),
            (("--offset",U64+1),"segment offset"),
            (("--fraction",32),"fraction width"),
            (("--sequence-start",-1),"sequence start"),
            (("--diagnostic-limit",-1),"diagnostic limit"),
        ]:
            with self.subTest(settings=settings):
                status, out, err = self.cli(self.plan,self.part1,*settings)
                self.assertEqual((status,out),(2,""))
                self.assertIn(message,err)

if __name__ == "__main__":
    unittest.main()
