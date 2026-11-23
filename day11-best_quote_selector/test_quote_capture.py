"""Check observed-transaction reconstruction and report calculations."""

from dataclasses import replace
import json
import unittest

from quote_analysis import MarketAnalyzer, analyze, compare_latency, cumulative_latency, render_markdown
from quote_capture import CaptureJoin, CommandCapture, Flush, QueryCapture, deserialize, join_cycles, serialize
from quote_model import Machine
from quote_schema import Ack, Code, Command, Drive, Op, PROFILES, Query, Result, Sample, State, Winner
from quote_verify import TraceError
from quote_workload import stall_tape, state_tape


class LogicalCapture(unittest.TestCase):
    def setUp(self):
        """Prepare test events with both delivered and reset-canceled transactions."""
        self.profile = PROFILES["default"]
        self.cmd = Command.replace(0, 0, 11, (5, 1), (8, 1), 81)
        self.query = Query(0, 20, 37)
        self.first = Drive(tick=1, command=self.cmd, query=self.query)
        self.store = Sample(1, 1, None, None)

    def full_capture(self, workload):
        """Build raw cycle observations and join them through the production capture path."""
        model = Machine(self.profile)
        return tuple(join_cycles(workload.drives, (model.step(row) for row in workload.drives)))

    def test_outputs_before_inputs_on_same_edge(self):
        """Appended tags never hide older transactions on the same channel."""
        join = CaptureJoin()
        self.assertEqual(join.step(0, self.first, self.store), ())
        second = Drive(tick=2, command=replace(self.cmd, tag=92))
        sample = Sample(1, 1, Ack(Code.OK, 81), None)
        actual = join.step(1, second, sample)
        self.assertEqual(actual, (CommandCapture(0, 1, 1, self.cmd, Ack(Code.OK, 81)),))
        last = Result(tag=37, tick=1)
        actual = join.step(2, Drive(tick=3), Sample(1, 1, Ack(Code.OK, 92), last))
        self.assertEqual(
            actual,
            (
                CommandCapture(1, 2, 2, second.command, Ack(Code.OK, 92)),
                QueryCapture(0, 2, 1, self.query, last),
            ),
        )
        join.finish()

    def test_duplicate_application_tags_pair_by_queue_order(self):
        """Preserve chronological pairing when clients reuse a tag."""
        join = CaptureJoin()
        join.step(0, self.first, self.store)
        later = Drive(tick=2, query=self.query)
        join.step(1, later, Sample(1, 1, Ack(Code.OK, 81), None))
        older = join.step(2, Drive(tick=3), Sample(1, 1, None, Result(tag=37, tick=1)))
        newer = join.step(3, Drive(tick=4), Sample(1, 1, None, Result(tag=37, tick=2)))
        self.assertEqual((older[0].accepted, newer[0].accepted), (0, 1))

    def test_holds_on_each_sink_do_not_emit_early(self):
        """A valid but stalled acknowledgement or result remains queued."""
        join = CaptureJoin()
        join.step(0, self.first, self.store)
        offer = Sample(0, 1, Ack(Code.OK, 81), None)
        self.assertEqual(join.step(1, Drive(2, ack_ready=0), offer), ())
        self.assertEqual(join.step(2, Drive(3, ack_ready=0), offer), ())
        self.assertEqual(join.step(3, Drive(4), offer)[0].latency, 3)
        stalled = Sample(1, 0, None, Result(tag=37, tick=1))
        self.assertEqual(join.step(4, Drive(5, result_ready=0), stalled), ())
        self.assertEqual(join.step(5, Drive(6), stalled)[0].latency, 5)
        join.finish()

    def test_reset_identifies_flushed_tags(self):
        """Reset emits a record that identifies both accepted but silently flushed messages."""
        join = CaptureJoin()
        join.step(0, self.first, self.store)
        actual = join.step(1, Drive(reset=1), Sample(0, 0, None, None))
        self.assertEqual(actual, (Flush(1, (81,), (37,)),))
        join.finish()

    def test_wrong_tags_and_ticks_are_rejected(self):
        """Fail separately for wrong output tag and wrong sampled tick."""
        bad = (
            (Sample(1, 1, Ack(Code.OK, 3), None), "expected acknowledgement tag"),
            (Sample(1, 1, None, Result(tag=38, tick=1)), "expected result tag"),
            (Sample(1, 1, None, Result(tag=37, tick=2)), "tick differs"),
        )
        for output, error in bad:
            join = CaptureJoin()
            join.step(0, self.first, self.store)
            with self.subTest(error=error), self.assertRaisesRegex(TraceError, error):
                join.step(1, Drive(tick=2), output)

    def test_missing_rows_and_undrained_capture_detected(self):
        """Distinguish an unresolved accepted result from truncated source or output rows."""
        with self.assertRaisesRegex(TraceError, "length mismatch"):
            tuple(join_cycles([self.first], []))
        with self.assertRaisesRegex(TraceError, "pending"):
            tuple(join_cycles([self.first], [self.store]))
        join = CaptureJoin()
        with self.assertRaisesRegex(TraceError, "expected edge 0"):
            join.step(1, self.first, self.store)

    def test_export_round_trip_for_stalls_and_missing_sides(self):
        """Preserve exactly the observed transactions across JSON encode and decode."""
        samples = self.full_capture(stall_tape(self.profile))
        recovered = [deserialize(json.loads(json.dumps(serialize(item))), self.profile) for item in samples]
        self.assertEqual(recovered, list(samples))
        last_query = next(item for item in reversed(samples) if isinstance(item, QueryCapture))
        obj = serialize(last_query)
        self.assertIsNone(obj["result"]["bid"])
        self.assertIsNone(obj["result"]["ask"])
        self.assertIsNone(obj["result"]["spread"])
        self.assertTrue(any(isinstance(item, Flush) and item.queries for item in samples))

    def test_export_detects_inconsistent_file_metadata(self):
        """Reject a changed latency or missing detail key before computing statistics."""
        actual = CommandCapture(0, 1, 1, self.cmd, Ack(Code.OK, 81))
        fields = serialize(actual)
        with self.assertRaisesRegex(ValueError, "delivery latency"):
            deserialize(dict(fields, latency=12), self.profile)
        with self.assertRaisesRegex(ValueError, "mismatched acknowledgement tag"):
            deserialize(dict(fields, ack={"code": "OK", "tag": 7}), self.profile)
        with self.assertRaisesRegex(ValueError, "unexpected command"):
            deserialize(dict(fields, extra=7), self.profile)
        with self.assertRaisesRegex(ValueError, "unknown capture kind"):
            deserialize({"kind": "packet"}, self.profile)
        bad = {"kind": "reset", "cycle": 7, "commands": "bad", "queries": ()}
        with self.assertRaisesRegex(ValueError, "tag lists"):
            deserialize(bad, self.profile)


class ReportRules(unittest.TestCase):
    def setUp(self):
        """Use a crossed best bid and offer from distinct venues and known edge timing."""
        self.profile = PROFILES["odd"]
        self.result = Result(
            1, Code.OK, State.CROSSED, 5, 3, 4,
            Winner(1, 0, 77, 8, 5, 1), Winner(1, 1, 72, 1, 4, 2), -5,
        )
        self.query = Query(0, 8, 1)

    def capture(self, accepted, reply, *, latency=2, instrument=0):
        """Build a tracked query for deterministic report arithmetic."""
        request = replace(self.query, instrument=instrument)
        return QueryCapture(accepted, accepted + latency, reply.tick, request, reply)

    def test_report_tracks_crosses_masks_winners_and_quantiles(self):
        """Compare complete per-venue and per-instrument reports to known counts."""
        def observed(cycle, spread):
            """Keep field values coherent while changing the crossed spread."""
            return self.capture(
                cycle, replace(self.result, ask=replace(self.result.ask, price=77 + spread), spread=spread)
            )

        report = analyze((observed(5, -5), observed(9, -1), observed(13, -9)), self.profile)
        book = report["instruments"][0]
        self.assertEqual(book["states"], {"CROSSED": 3})
        self.assertEqual(book["spreads"], {"count": 3, "min": -9, "p50": -5, "p95": -1, "max": -1})
        self.assertEqual(book["crossed_bid_ask_venue_pairs"], {"0:1": 3})
        self.assertEqual(book["venues"][0],
                         {"venue": 0, "active_observations": 3, "stale_observations": 0,
                          "bid_wins": 3, "ask_wins": 0})
        self.assertEqual(book["venues"][2]["stale_observations"], 3)
        self.assertEqual(book["state_runs"], [{
            "instrument": 0, "state": "CROSSED", "first": 5, "last": 13,
            "observations": 3, "low_spread": -9, "sampled_span": 8,
        }])
        self.assertIn("| 0 | 3 | 3 | 0 | 0 | -5 | 2 |", render_markdown(report))

    def test_error_and_reset_break_sampling_intervals(self):
        """Keep evidence on either side of missing data in separate duration runs."""
        error = Result(tag=1, code=Code.BAD_AGE)
        event = Flush(12, (15,), (1, 9))
        report = analyze((
            self.capture(5, self.result), self.capture(6, error),
            self.capture(9, self.result), event, self.capture(14, self.result),
        ), self.profile)
        book = report["instruments"][0]
        self.assertEqual([run["first"] for run in book["state_runs"]], [5, 9, 14])
        self.assertEqual([run["sampled_span"] for run in book["state_runs"]], [0, 0, 0])
        self.assertEqual(book["query_codes"], {"OK": 3, "BAD_AGE": 1})
        self.assertEqual(report["flushes"], {"resets": 1, "commands": 1, "queries": 2})

    def test_invalid_queries_are_not_book_activity(self):
        """Keep unaddressable IDs out of per-instrument metrics."""
        bad = self.capture(3, Result(code=Code.BAD_INST), instrument=3)
        data = analyze((bad,), self.profile)
        self.assertEqual(data["invalid_instrument_queries"], {3: 1})
        self.assertEqual(data["instruments"], [])
        self.assertIn("no successful instrument observations", render_markdown(data))

    def test_command_outcome_aggregation_and_order(self):
        """Maintain summaries by operation and reject reordered delivery."""
        state = MarketAnalyzer(self.profile)
        first = CommandCapture(1, 2, 4, Command(Op.ENABLE), Ack(Code.OK, 0))
        second = CommandCapture(2, 4, 8, Command(7), Ack(Code.BAD_OP, 0))
        state.record(first)
        state.record(second)
        self.assertEqual(state.summarize()["command_outcomes_by_op"], {"ENABLE:OK": 1, "7:BAD_OP": 1})
        self.assertEqual(state.summarize()["command_latency"]["max"], 2)
        with self.assertRaisesRegex(ValueError, "not ordered"):
            state.record(first)

    def test_distribution_helpers(self):
        """Use nearest-rank latency comparison and ordered cumulative boundaries."""
        self.assertEqual(cumulative_latency([4, 1, 4, 7], [0, 1, 4, 8]),
                         {0: 0, 1: 1, 4: 3, 8: 4})
        self.assertEqual(compare_latency([1, 2, 3], [3, 5, 7], 50), 3)
        self.assertEqual(compare_latency([1, 2, 3], [3, 5, 7]), 4)
        for limits in ((1, 1), (3, 2), (-1, 0), ("1",)):
            with self.assertRaises(ValueError):
                cumulative_latency([3], limits)
        with self.assertRaises(ValueError):
            compare_latency([], [1])

    def test_grouped_report_contains_all_directed_transfers(self):
        """Cross-check aggregate clocks and states against observed logical records."""
        driver = state_tape(self.profile).drives
        model = Machine(self.profile)
        joined = tuple(join_cycles(driver, (model.step(step) for step in driver)))
        report = analyze(joined, self.profile)
        for kind, key in ((CommandCapture, "command_latency"),):
            self.assertEqual(report[key]["count"], sum(isinstance(row, kind) for row in joined))
        self.assertEqual(sum(len(row["venues"]) for row in report["instruments"]), 10)
        self.assertEqual(report["instruments"][0]["states"]["CROSSED"], 2)


if __name__ == "__main__":
    unittest.main()
