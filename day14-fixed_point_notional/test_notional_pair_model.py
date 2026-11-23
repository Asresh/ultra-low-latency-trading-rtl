"""Check admission order, timing classes, and raw pair capture error handling."""

from __future__ import annotations

import csv
import json
import random
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from notional_model import Kind, Reply, Request, calculate
from notional_pair_campaign import (
    Campaign, TRANSACTION_FIELDS, check_sustained, clean_request, directed, hex_rows, make_campaign,
    load_markers, malformed_request, offsets, require_campaign, save_hex, shipment, tool_words, verify_pair_capture,
    write_transfers,
)
from notional_pair_model import (
    DRIVE_FIELDS, PIN_FIELDS, Drive, PairCounters, PairModel, PairTraceError, PinAudit, Pins,
    audit_rows, calculation_delay, decode_reply, decode_request, differences,
    describe_phases, predict_saturated,
)


class PairTiming(unittest.TestCase):
    """Check exact handshakes and ordering without using campaign timing helpers."""

    def setUp(self):
        """Create a Q24.8 pair with separate valid and malformed requests."""
        self.model = PairModel(8)
        self.slow = Request(13, 256, 4, 100)
        self.short = Request(13, 256, 0)
        self.other = Request(14, 123, 330, 97)

    def advance_to(self, when, *, ready=True):
        """Tick empty source edges until the selected pre-edge cycle."""
        while self.model.cycle < when:
            self.model.advance(Drive(ready=ready))

    def issue_two(self):
        """Admit a slow older request and a malformed younger request."""
        left = self.model.advance(Drive(request=self.slow)).admitted
        right = self.model.advance(Drive(request=self.short)).admitted
        self.assertEqual((left.lane, right.lane), (0, 1))
        self.assertEqual((left.admitted, right.admitted), (0, 1))
        self.assertEqual((left.available, right.available), (85, 2))
        return left, right

    def test_pair_warmup_caps_at_two_outstanding(self):
        """A final acceptance pin transition respects the old queue capacity."""
        self.assertEqual(self.model.pins(Drive()), Pins(True, None, 0))
        self.issue_two()
        pins = self.model.pins(Drive())
        self.assertEqual(pins, Pins(False, None, 2))
        self.assertIsNone(self.model.available_lane())
        edge = self.model.advance(Drive(request=self.other))
        self.assertIsNone(edge.admitted)
        self.assertIsNone(edge.completed)
        self.assertEqual(len(self.model.pending), 2)

    def test_fast_tail_must_wait_behind_slow_head_for_admission_order(self):
        """A ready younger lane is hidden until the older lane transfers."""
        head, tail = self.issue_two()
        self.assertTrue(self.model.head_of_line())
        for index in range(2, 85):
            self.assertEqual(self.model.cycle, index)
            unready = self.model.advance(Drive())
            self.assertIsNone(unready.completed)
            self.assertFalse(unready.pins.output_valid)
        boundary = self.model.advance(Drive())
        self.assertEqual(boundary.completed.job.ordinal, head.ordinal)
        self.assertEqual(boundary.completed.service, 85)
        self.assertEqual(boundary.completed.order_wait, 0)
        second = self.model.advance(Drive())
        self.assertEqual(second.completed.job.ordinal, tail.ordinal)
        self.assertEqual(second.completed.service, 1)
        self.assertEqual(second.completed.order_wait, 84)
        self.assertEqual(second.completed.delivered, 86)
        self.assertEqual(second.completed.job.reply.status, Kind.BAD_QUANTITY)
        self.assertFalse(self.model.pending)

    def test_full_queue_takes_one_preevaluation_cycle_to_release_capacity(self):
        """Ready stays low on retirement from two occupied lanes."""
        self.issue_two()
        self.advance_to(85)
        head = self.model.advance(Drive(request=self.other))
        self.assertFalse(head.pins.input_ready)
        self.assertEqual(head.pins.outstanding, 2)
        self.assertIsNotNone(head.completed)
        self.assertIsNone(head.admitted)
        following = self.model.advance(Drive(request=self.other))
        self.assertTrue(following.pins.input_ready)
        self.assertEqual(following.pins.outstanding, 1)
        self.assertIsNotNone(following.admitted)
        self.assertIsNotNone(following.completed)
        self.assertEqual((following.admitted.lane, following.completed.job.lane), (0, 1))

    def test_hold_effects_on_compute_wait_and_sink_wait_are_distinct(self):
        """A stalled head remains visible and holds the younger ready lane."""
        self.issue_two()
        self.advance_to(85)
        self.assertFalse(self.model.head_of_line())
        self.assertTrue(self.model.ordered_hold())
        frozen = self.model.pins(Drive(ready=False))
        for _ in range(7):
            self.assertEqual(self.model.advance(Drive(ready=False)).pins, frozen)
        first = self.model.advance(Drive()).completed
        second = self.model.advance(Drive()).completed
        self.assertEqual(first.order_wait, 0)
        self.assertEqual(first.downstream_wait, 7)
        self.assertEqual(second.order_wait, 91)
        self.assertEqual(second.downstream_wait, 0)
        self.assertEqual(second.job.first_visible, 93)

    def test_reset_fences_full_pair_even_if_input_and_output_are_ready(self):
        """A synchronous reset keeps the old count on its sample, then clears both lanes."""
        prior = self.issue_two()
        self.advance_to(85, ready=False)
        reset = self.model.advance(Drive(reset=True, request=self.other, ready=True))
        self.assertEqual(reset.pins, Pins(False, None, 2))
        self.assertIsNone(reset.admitted)
        self.assertIsNone(reset.completed)
        self.assertEqual(tuple(item.ordinal for item in reset.discarded), (prior[0].ordinal, prior[1].ordinal))
        self.assertEqual(self.model.pins(Drive()), Pins(True, None, 0))
        next_item = self.model.advance(Drive(request=self.other)).admitted
        self.assertEqual(next_item.lane, 0)
        self.assertEqual(next_item.ordinal, 2)
        self.advance_to(next_item.available)
        self.assertEqual(self.model.advance(Drive()).completed.job.request, self.other)

    def test_malformed_shortcuts_are_per_lane_and_still_consume_queue_slots(self):
        """Every malformed reason obeys the same structural output timing."""
        for case, kind in enumerate((Kind.BAD_QUANTITY, Kind.BAD_PRICE, Kind.BAD_FEE, Kind.BAD_MODE)):
            with self.subTest(case=case):
                current = malformed_request(case, case)
                admitted = self.model.advance(Drive(request=current))
                ready = self.model.advance(Drive())
                self.assertEqual(ready.completed.job.reply.status, kind)
                self.assertEqual(ready.completed.job.admitted, admitted.admitted.admitted)
                self.assertEqual(ready.completed.service, 1)
                self.assertEqual(ready.completed.order_wait, 0)
                self.assertEqual(ready.completed.downstream_wait, 0)
                self.assertEqual(ready.pins.outstanding, 1)
                self.assertEqual(ready.completed.job.lane, case % 2)

    def test_cash_limit_or_overflow_still_takes_the_full_arithmetic_path(self):
        """Error at arithmetic completion takes the same clocks as an accepted amount."""
        carry = Request(1, 0xffffffff, 0xffffffff, 10_000)
        limited = Request(2, 31, 20, limit=0)
        zero_fee = Request(4, 1, 1, fee_bps=0)
        for request, desired in ((carry, Kind.OVERFLOW), (limited, Kind.LIMIT), (zero_fee, Kind.OK)):
            reply = calculate(request, 0)
            self.assertEqual(reply.status, desired)
            self.assertEqual(calculation_delay(reply), 85)

    def test_model_requires_supported_profile_and_exact_control_types(self):
        """Reject adapter data the hardware driver cannot sample meaningfully."""
        for value in (-1, 32, True, "8", None):
            with self.assertRaises(ValueError):
                PairModel(value)
        with self.assertRaisesRegex(ValueError, "Boolean"):
            Drive(reset=1)
        with self.assertRaisesRegex(ValueError, "Boolean"):
            Drive(ready=0)
        with self.assertRaises(TypeError):
            Drive(request="tag")
        with self.assertRaises(TypeError):
            self.model.advance({})


class PairWireContract(unittest.TestCase):
    """Check cycle vector column meanings independently of the simulator file."""

    def test_optional_limit_zero_is_not_lost_during_decoding(self):
        """The limit-enable bit retains a real limit even when its value is zero."""
        request = Request(0xAB, 91, 15, 500, 1, True, 0)
        drive = Drive(request=request, ready=False)
        self.assertEqual(offsets(drive.wire()), "0 1 0 ab 5b f 1f4 1 1 1 0")
        self.assertEqual(Drive.read(drive.wire()), drive)
        self.assertEqual(decode_request(request.wire()), request)
        self.assertIsNone(decode_request(Request(1, 3, 2).wire()).limit)

    def test_result_fields_preserve_64_bit_values_and_status(self):
        """Do not truncate cash or lose the inexact indicator when round-tripping."""
        reply = Reply(600, Kind.LIMIT, False, 1 << 63, (1 << 62) + 2, (1 << 64) - 1, True)
        raw = Pins(False, reply, 2)
        self.assertEqual(Pins.read(raw.wire()), raw)
        self.assertEqual(decode_reply(reply.wire()), reply)
        self.assertEqual(raw.wire()[:3], (0, 1, 2))
        self.assertEqual(tuple(PIN_FIELDS[-3:]), ("fee", "cash", "inexact"))

    def test_wire_rejects_inactive_payloads_and_impossible_counts(self):
        """The checker must refuse states that hide a visible result or command."""
        with self.assertRaisesRegex(PairTraceError, "count exceeds"):
            Pins.read((0, 0, 3, 0, 0, 0, 0, 0, 0, 0))
        with self.assertRaisesRegex(PairTraceError, "outstanding"):
            Pins.read((0, 1, 0, 0, 0, 0, 0, 0, 0, 0))
        with self.assertRaisesRegex(PairTraceError, "inactive reply"):
            Pins.read((0, 0, 0, 0, 0, 0, 0, 0, 0, 1))
        with self.assertRaisesRegex(PairTraceError, "request payload"):
            Drive.read((0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1))
        with self.assertRaisesRegex(PairTraceError, "disabled limit"):
            decode_request((0, 1, 1, 0, 0, 0, 0, 1))
        with self.assertRaisesRegex(ValueError, "sink ready"):
            Drive.read((0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0))
        with self.assertRaisesRegex(PairTraceError, "fields"):
            Pins.read((1,))
        with self.assertRaisesRegex(ValueError, "valid Kind"):
            decode_reply((0, 7, 0, 0, 0, 0, 0))

    def test_diff_explains_changed_fields_instead_of_tag_only(self):
        """A reused tag still exposes differences in arithmetic and result timing."""
        expected = Pins(False, Reply(83, Kind.OK, True, 8, 2, 6), 2)
        bad = Pins(True, replace(expected.reply, fee=3), 1)
        self.assertEqual(differences(expected, bad), (
            "input_ready expected=0x0 actual=0x1, outstanding expected=0x2 actual=0x1, "
            "fee expected=0x2 actual=0x3"
        ))


class PairAuditing(unittest.TestCase):
    """Validate the contract when raw cycle records are changed or truncated."""

    def setUp(self):
        """Generate a directed fixture and an isolated staging directory."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.plan = Campaign(8)
        directed(self.plan)
        self.plan.write(self.folder)
        self.drive = self.folder / "drive.hex"
        self.capture = self.folder / "capture.hex"
        save_hex(self.capture, (sample.wire() for sample in self.plan.expected))

    def read(self):
        """Read the current fixture through the actual offline verification path."""
        return verify_pair_capture(8, self.drive, self.capture)

    def test_every_cancellation_is_accounted_for_without_reusing_old_results(self):
        """Match admission order and result identity independently of reused tags."""
        stats, transfers, discards = self.read()
        require_campaign(stats, transfers)
        self.assertEqual(stats.accepted, stats.completed + stats.discarded)
        self.assertEqual(stats.discarded, 3)
        self.assertEqual(len(discards), 3)
        self.assertGreater(stats.compute_order_stalls, 75)
        self.assertGreater(stats.downstream_order_stalls, 0)
        self.assertGreater(stats.simultaneous, 0)
        reused = [item for item in transfers if item.job.reply.tag == 20]
        self.assertEqual([item.job.reply.status for item in reused], [Kind.OK, Kind.BAD_PRICE])
        self.assertNotEqual(reused[0].job.ordinal, reused[1].job.ordinal)
        delivered = {record.job.ordinal for record in transfers}
        removed = {record.ordinal for record in discards}
        self.assertFalse(delivered & removed)
        self.assertEqual(delivered | removed, set(range(stats.accepted)))

    def test_stalled_source_or_target_matches_its_original_wire_fields(self):
        """Verify source and sink hold violations as distinct protocol errors."""
        cycle = next(i for i, (drive, pins) in enumerate(zip(self.plan.inputs, self.plan.expected))
                     if drive.valid and not drive.reset and not pins.input_ready)
        altered = list(self.plan.inputs)
        request = altered[cycle + 1].request
        altered[cycle + 1] = replace(altered[cycle + 1], request=replace(request, tag=request.tag ^ 1))
        save_hex(self.drive, (row.wire() for row in altered))
        with self.assertRaisesRegex(PairTraceError, "stalled request changed"):
            self.read()
        save_hex(self.drive, (row.wire() for row in self.plan.inputs))
        index = next(i for i, (drive, pins) in enumerate(zip(self.plan.inputs, self.plan.expected))
                     if pins.output_valid and not drive.ready and not drive.reset)
        pins = list(self.plan.expected)
        next_pin = pins[index + 1]
        pins[index + 1] = replace(next_pin, reply=replace(next_pin.reply, cash=next_pin.reply.cash ^ 1))
        save_hex(self.capture, (sample.wire() for sample in pins))
        with self.assertRaisesRegex(PairTraceError, "stalled reply changed"):
            self.read()

    def test_missing_and_extra_capture_rows_are_errors(self):
        """Report index mismatches even when earlier transactions validated."""
        initial = self.capture.read_text(encoding="ascii").splitlines(keepends=True)
        for lines, label in ((initial[:-1], "missing capture"), (initial + initial[-1:], "additional capture")):
            self.capture.write_text("".join(lines), encoding="ascii")
            with self.assertRaisesRegex(PairTraceError, label):
                self.read()

    def test_wrong_lane_count_is_found_even_on_inactive_edge(self):
        """The occupancy signal is part of the observable scheduler contract."""
        self.assertEqual(self.plan.expected[0].outstanding, 0)
        pins = [row.wire() for row in self.plan.expected]
        altered = list(pins[0])
        altered[2] = 1
        pins[0] = tuple(altered)
        save_hex(self.capture, pins)
        with self.assertRaisesRegex(PairTraceError, "outstanding expected=0x0 actual=0x1"):
            self.read()

    def test_atomic_cycle_completion_is_required_before_reporting_statistics(self):
        """Incomplete inputs leave a pending job rather than a misleading report."""
        audit = PinAudit(8)
        input_event = Drive(request=clean_request(1, 8))
        audit.check(input_event, Pins(True, None, 0))
        with self.assertRaisesRegex(PairTraceError, "without draining"):
            audit.finish()
        self.capture.write_text("", encoding="ascii")
        self.drive.write_text("", encoding="ascii")
        with self.assertRaisesRegex(PairTraceError, "capture is empty"):
            self.read()

    def test_transaction_csv_preserves_all_four_timestamps(self):
        """Persist a report that explains when a result was computed and seen."""
        _, transfers, _ = self.read()
        file = self.folder / "transactions.csv"
        write_transfers(file, transfers)
        with file.open(newline="") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(tuple(reader.fieldnames), TRANSACTION_FIELDS)
            rows = list(reader)
        self.assertEqual(len(rows), len(transfers))
        for raw, event in zip(rows, transfers):
            self.assertEqual(int(raw["admitted"]), event.job.admitted)
            self.assertEqual(int(raw["available"]), event.job.available)
            self.assertEqual(int(raw["visible"]), event.job.first_visible)
            self.assertEqual(int(raw["delivered"]), event.delivered)
            self.assertEqual(int(raw["order_wait"]), event.order_wait)
            self.assertEqual(int(raw["downstream_wait"]), event.downstream_wait)

    def test_hex_reader_rejects_bad_words_with_file_context(self):
        """Validate columns before interpretation and reject non-wire numeric forms."""
        path = self.folder / "rows.hex"
        for row, diagnostic in (
            ("1 1\n", "expected"),
            ("-1 0 0\n", "invalid hexadecimal"),
            ("+1 0 0\n", "invalid hexadecimal"),
            ("0x1 0 0\n", "invalid hexadecimal"),
            ("g 0 0\n", "invalid hexadecimal"),
        ):
            path.write_text("1 0 0\n" + row, encoding="ascii")
            values = hex_rows(path, 3)
            self.assertEqual(next(values), (1, 0, 0))
            with self.assertRaisesRegex(PairTraceError, f"rows.hex:2: {diagnostic}"):
                next(values)


class PairCampaignProperties(unittest.TestCase):
    """Exercise backpressure metamorphically and check public campaign options."""

    def preview(self, frac, requests, delays):
        """Build all requests in the same order under a chosen sink-ready policy."""
        model = Campaign(frac)
        model.mark("ordered")
        for request in requests:
            model.issue(request, delays)
        model.drain()
        return audit_rows(frac, zip(model.inputs, model.expected))

    def test_stalls_change_elapsed_time_but_never_admission_order_or_results(self):
        """Same requests with different stall patterns yield the same ordered arithmetic."""
        rng = random.Random(817)
        for frac in (0, 8, 31):
            traffic = []
            for index in range(50):
                if index % 3 == 0:
                    item = malformed_request(index % 3, index % 4)
                else:
                    item = clean_request(index % 3, frac, fee=rng.randrange(10_001), quantity=1 + index)
                traffic.append(item)
            _, open_sink, _ = self.preview(frac, traffic, None)
            _, misaligned, _ = self.preview(frac, traffic, lambda cycle: cycle % 13 > 4)
            self.assertEqual([item.job.reply for item in open_sink], [item.job.reply for item in misaligned])
            self.assertEqual([item.job.request for item in open_sink], traffic)
            self.assertEqual([item.job.ordinal for item in misaligned], list(range(len(traffic))))
            self.assertGreater(misaligned[-1].delivered, open_sink[-1].delivered)

    def test_markers_and_seeded_random_traffic_are_reproducible(self):
        """Compare the tape and named timing checkpoints across separate builds."""
        first = make_campaign(8, 55, 21)
        replica = make_campaign(8, 55, 21)
        variant = make_campaign(8, 56, 21)
        self.assertEqual(first.markers, replica.markers)
        self.assertEqual(first.inputs, replica.inputs)
        self.assertEqual(first.expected, replica.expected)
        self.assertNotEqual(first.inputs, variant.inputs)
        self.assertEqual(set(first.markers), {
            "warm_up", "slow_head_fast_tail", "full_queue_bubble", "one_in_one_out",
            "reset_busy_and_ready", "sustained_pair", "mixed_seed",
        })

    def test_empty_or_uncovered_campaign_reports_actionable_limits(self):
        """Reject silently timed-out or low-coverage campaigns."""
        model = Campaign(8, maximum_cycles=2)
        model.mark("limited")
        with self.assertRaisesRegex(RuntimeError, "deadline exceeded during limited"):
            model.idle(3)
        with self.assertRaisesRegex(ValueError, "duplicate campaign"):
            model.mark("limited")
        with self.assertRaisesRegex(PairTraceError, "campaign missed"):
            require_campaign(PairCounters(), [])
        with self.assertRaisesRegex(ValueError, "seed"):
            make_campaign(8, -1, 0)
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            make_campaign(8, 0, -1)

    def test_generate_only_writes_profile_tape_without_a_compiler(self):
        """The checked-in command can be used with another simulator if needed."""
        script = Path(__file__).with_name("notional_pair_campaign.py")
        with tempfile.TemporaryDirectory() as folder:
            run = subprocess.run(
                [sys.executable, "-B", str(script), "--profile", "8", "--output", folder,
                 "--operations", "3", "--generate-only", "--iverilog", "unused"],
                text=True,
                capture_output=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            path = Path(folder)
            summary = json.loads((path / "summary.json").read_text())
            rows = list(hex_rows(path / "q8" / "drive.hex", len(DRIVE_FIELDS)))
            self.assertEqual(summary["8"]["cycles"], len(rows))
            self.assertFalse((path / "q8" / "capture.hex").exists())
            self.assertEqual(tool_words('"/a b/tool" -I.'), ["/a b/tool", "-I."])
            with self.assertRaisesRegex(ValueError, "blank"):
                tool_words(" ")


class SchedulingBoundsTest(unittest.TestCase):
    """Cross-check the cycle model with independent recurrence constraints."""

    def test_uniform_valid_sustained_rate_and_full_queue_bubble(self):
        """Two long operations issue per 86 cycles after warm-up."""
        forecasts = predict_saturated([85] * 8)
        self.assertEqual([job.issue for job in forecasts], [0, 1, 86, 87, 172, 173, 258, 259])
        self.assertEqual([job.delivery for job in forecasts], [85, 86, 171, 172, 257, 258, 343, 344])
        self.assertEqual(forecasts[4].to_record(), {
            "ordinal": 4, "issue": 172, "ready": 257, "delivery": 257,
        })

    def test_all_shortcuts_yield_one_reply_per_cycle(self):
        """One-cycle validation failures can turn over without occupying two slots."""
        forecasts = predict_saturated([1] * 250)
        self.assertEqual([job.issue for job in forecasts], list(range(250)))
        self.assertEqual([job.delivery for job in forecasts], list(range(1, 251)))

    def test_mixed_service_schedule_preserves_fifo_transfer_limit(self):
        """An early computed reply may have to wait at least one more edge."""
        forecasts = predict_saturated([85, 1, 1, 85, 1])
        self.assertEqual([job.issue for job in forecasts], [0, 1, 86, 87, 88])
        self.assertEqual([job.ready for job in forecasts], [85, 2, 87, 172, 89])
        self.assertEqual([job.delivery for job in forecasts], [85, 86, 87, 172, 173])
        self.assertEqual(predict_saturated([]), [])
        for delay in (0, -1, False, 1.5):
            with self.assertRaisesRegex(ValueError, "positive"):
                predict_saturated([delay])

    def test_closed_form_matches_abstract_lane_simulation_many_mixes(self):
        """The order equations predict pressure independently of the rotating lane pointer."""
        for seed in range(12):
            rng = random.Random(seed)
            width = (0, 8, 31)[seed % 3]
            choices = [rng.randrange(4) != 0 for _ in range(50)]
            expected = predict_saturated(85 if full else 1 for full in choices)
            tape = Campaign(width)
            tape.mark("issue")
            for tag, full in enumerate(choices):
                if full:
                    request = clean_request(tag, width, fee=rng.randrange(10_001))
                else:
                    request = malformed_request(tag, tag % 4)
                tape.issue(request)
            tape.drain()
            _, compared, _ = audit_rows(width, zip(tape.inputs, tape.expected))
            self.assertEqual(
                [(item.issue, item.ready, item.delivery) for item in expected],
                [(item.job.admitted, item.job.available, item.delivered) for item in compared],
            )

    def test_campaign_measures_distinct_stalls_and_sustained_period(self):
        """Check that the independent phase report accounts for every transaction."""
        plan = Campaign(8)
        directed(plan)
        plan.mark("mixed_seed")
        plan.idle()
        stats, transactions, cancelled = audit_rows(8, zip(plan.inputs, plan.expected))
        report = describe_phases(plan.markers, stats.cycles, transactions, cancelled)
        lookup = {phase["name"]: phase for phase in report}
        self.assertEqual(sum(row["accepted"] for row in report), stats.accepted)
        self.assertEqual(sum(row["delivered"] for row in report), stats.completed)
        self.assertEqual(sum(row["discarded"] for row in report), stats.discarded)
        self.assertEqual(lookup["warm_up"]["delivered"], 1)
        self.assertGreater(lookup["slow_head_fast_tail"]["peak_order_wait"], 80)
        self.assertEqual(lookup["slow_head_fast_tail"]["peak_downstream_wait"], 5)
        self.assertEqual(lookup["reset_busy_and_ready"]["discarded"], 3)
        self.assertEqual(lookup["sustained_pair"]["total_service"], 16 * 85)
        measured = check_sustained(plan.markers, transactions)
        self.assertEqual(measured["warm_issue_period"], 86)
        self.assertEqual(measured["requests_per_period"], 2)

    def test_phase_assignment_follows_issue_when_delivery_crosses_boundary(self):
        """A late delivery remains attributed to its original scenario."""
        tape = Campaign(8)
        tape.mark("early")
        tape.issue(clean_request(10, 8))
        tape.mark("late")
        tape.issue(malformed_request(11, 2))
        tape.drain()
        stats, actual, cancelled = audit_rows(8, zip(tape.inputs, tape.expected))
        groups = describe_phases(tape.markers, stats.cycles, actual, cancelled)
        self.assertEqual([group["delivered"] for group in groups], [1, 1])
        self.assertEqual(groups[0]["last_delivery"], 85)
        self.assertEqual(groups[1]["first_admission"], 1)
        self.assertGreater(groups[1]["peak_order_wait"], 80)

    def test_malformed_phase_metadata_cannot_move_admissions_between_reports(self):
        """Reject missing start, duplicate boundary, or past-end markers."""
        cases = (
            ({"late": 1}, 2, "start of capture"),
            ({"a": 0, "b": 0}, 2, "share an edge"),
            ({"a": 0, "b": 2}, 2, "outside capture"),
            ({"": 0}, 1, "nonempty"),
            ({"a": 0}, -1, "nonnegative"),
        )
        for bounds, cycles, diagnostic in cases:
            with self.assertRaisesRegex(ValueError, diagnostic):
                describe_phases(bounds, cycles, [], [])


class OfflineCaptureTest(unittest.TestCase):
    """Reuse already recorded hardware data while focusing the exported report."""

    def setUp(self):
        """Save a compact synthetic capture and retain its profile metadata."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name)
        self.profile = self.output / "q8"
        campaign = Campaign(8)
        directed(campaign)
        campaign.mark("mixed_seed")
        campaign.idle(2)
        campaign.write(self.profile)
        save_hex(self.profile / "capture.hex", (row.wire() for row in campaign.expected))
        self.driver = Path(__file__).with_name("notional_pair_campaign.py")

    def audited(self, *arguments):
        """Call the offline mode while deliberately making both tool names unusable."""
        return subprocess.run(
            [sys.executable, "-B", str(self.driver), "--audit-only", "--output", str(self.output),
             "--profile", "8", "--iverilog", "unused", "--vvp", "unused", *arguments],
            text=True,
            capture_output=True,
        )

    def test_export_reused_tag_by_marker_without_rebuilding_or_hiding_results(self):
        """Keep total check counts while narrowing CSV output to a reused tag."""
        path = self.profile / "drive.hex"
        prior = path.read_bytes()
        saved_time = path.stat().st_mtime_ns
        completed = self.audited("--select-phase", "slow_head_fast_tail", "--select-tag", "20")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(path.read_bytes(), prior)
        self.assertEqual(path.stat().st_mtime_ns, saved_time)
        self.assertFalse((self.profile / "pair.vvp").exists())
        summary = json.loads((self.output / "summary.json").read_text())["8"]
        self.assertGreater(summary["completed"], summary["exported_transactions"])
        self.assertEqual(summary["exported_transactions"], 2)
        with (self.profile / "transactions.csv").open(newline="") as file:
            exported = list(csv.DictReader(file))
        self.assertEqual([row["tag"] for row in exported], ["20", "20"])
        self.assertEqual([row["status"] for row in exported], ["OK", "BAD_PRICE"])

    def test_filter_by_status_does_not_remove_other_unvalidated_cycles(self):
        """A corrupt cycle still fails when its eventual result would be hidden."""
        captured = self.profile / "capture.hex"
        rows = list(hex_rows(captured, len(PIN_FIELDS)))
        first = list(rows[0])
        first[2] = 1
        rows[0] = tuple(first)
        save_hex(captured, rows)
        result = self.audited("--select-status", "BAD_MODE", "--select-tag", "999")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outstanding expected", result.stderr)
        self.assertFalse((self.profile / "transactions.csv").exists())

    def test_unknown_phase_and_invalid_status_produce_cli_diagnostics(self):
        """Report misspelled filters instead of presenting misleading empty rows."""
        for options, diagnostic in (
            (("--select-phase", "step_27"), "unknown phase"),
            (("--select-status", "wrong"), "status must be one of"),
            (("--select-tag", "65536"), "sixteen bits"),
        ):
            executed = self.audited(*options)
            self.assertNotEqual(executed.returncode, 0)
            self.assertIn(diagnostic, executed.stderr)

    def test_phase_metadata_can_be_corrupted_without_changing_trace_pins(self):
        """Ensure marked scenario ranges come from a complete metadata record."""
        self.assertIn("warm_up", load_markers(self.profile))
        markers = self.profile / "markers.json"
        for broken, message in (
            ("{unbalanced", "invalid phase metadata"),
            ('["warm_up","mixed_seed"]', "missing campaign phase"),
            ('{"warm_up":0}', "missing campaign phase"),
        ):
            markers.write_text(broken, encoding="utf-8")
            with self.assertRaisesRegex(PairTraceError, message):
                load_markers(self.profile)

    def test_shipment_can_return_empty_or_preserve_entire_transfer_list(self):
        """Range, status, and tag constraints apply only at the export boundary."""
        stats, results, cancelled = verify_pair_capture(
            8, self.profile / "drive.hex", self.profile / "capture.hex",
        )
        phases = describe_phases(load_markers(self.profile), stats.cycles, results, cancelled)
        self.assertEqual(shipment(results, phases), results)
        self.assertEqual(shipment(results, phases, tag=65535), [])
        selected = shipment(results, phases, phase="slow_head_fast_tail",
                            tag=20, status=Kind.BAD_PRICE)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].job.request.tag, 20)
        self.assertGreater(selected[0].order_wait, 80)


if __name__ == "__main__":
    unittest.main()
