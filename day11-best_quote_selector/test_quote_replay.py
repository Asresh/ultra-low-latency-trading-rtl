"""Exercise normalization, bounded scheduling, and reproducible file conversion."""

from dataclasses import asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from quote_model import Machine
from quote_replay import (
    Accepted, Canceled, Event, ReplayError, SinkWindow,
    check_order, decode_event, encode_event, event_channel, merge_windows,
    parse_window, read_events, ready_at, schedule, write_events,
)
from quote_schema import Command, Op, PROFILES, Profile, Query
from quote_verify import compare_trace, read_drives, write_drives


class JsonInput(unittest.TestCase):
    def setUp(self):
        """Use a single venue and a reset followed by overlapping tested events."""
        self.p = PROFILES["solo"]
        self.cmd = Command.replace(0, 0, 3, (19, 4), (29, 6), 71)
        self.query = Query(0, 8, 72)
        self.events = (Event(0, reset=True), Event(2, self.cmd, self.query))

    def test_round_trip(self):
        """Retain normalized operations, side fields, tags, and reset boundaries."""
        lines = [encode_event(event) for event in self.events]
        self.assertIn('"op":"replace"', lines[1])
        actual = tuple(decode_event(line, self.p) for line in lines)
        self.assertEqual(actual, self.events)

    def test_unknown_opcode_is_preserved_when_numeric(self):
        """Pass intentionally malformed numeric opcodes to the RTL validation channel."""
        command = replace(self.cmd, op=7)
        event = Event(2, command, self.query)
        actual = decode_event(encode_event(event), self.p)
        self.assertEqual(actual.command.op, 7)

    def test_unknown_opcode_name_rejected(self):
        """Reject unrecognized strings before opening a driver output."""
        with self.assertRaisesRegex(ReplayError, "unknown operation"):
            decode_event(
                '{"cycle":1,"command":{"op":"BUY","instrument":0,"venue":0}}', self.p
            )

    def test_sequence_required_for_market_data(self):
        """Reject normalized market commands missing mandatory serial fields."""
        for name in ("replace", "withdraw"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ReplayError, "require sequence"):
                    decode_event(
                        '{"cycle":1,"command":{"op":"' + name + '","instrument":0,"venue":0}}',
                        self.p,
                    )

    def test_enable_and_disable_ignore_market_fields(self):
        """Controls require addressing alone and use zero optional payloads."""
        for symbol, wanted in (("enable", Op.ENABLE), ("disable", Op.DISABLE)):
            line = '{"cycle":2,"command":{"op":"' + symbol + '","instrument":0,"venue":0}}'
            self.assertEqual(decode_event(line, self.p).command, Command(wanted))

    def test_duplicate_nested_keys(self):
        """Reject duplicate values before any one copy can silently replace another."""
        for line in (
            '{"cycle":0,"cycle":5,"reset":true}',
            '{"cycle":0,"reset":false,"reset":true}',
            '{"cycle":0,"query":{"instrument":0,"instrument":1,"max_age":0}}',
        ):
            with self.subTest(line=line):
                with self.assertRaisesRegex(ReplayError, "duplicate field"):
                    decode_event(line, self.p)

    def test_boolean_is_not_cycle_size_or_tag(self):
        """Distinguish JSON booleans from decimal integer fields at the file boundary."""
        for line in (
            '{"cycle":false,"reset":true}',
            '{"cycle":3,"query":{"instrument":0,"max_age":true}}',
            '{"cycle":3,"query":{"instrument":0,"max_age":1,"tag":false}}',
        ):
            with self.subTest(line=line):
                with self.assertRaisesRegex(ReplayError, "must be an integer"):
                    decode_event(line, self.p)

    def test_invalid_record_shapes(self):
        """Diagnose malformed, empty, extra, and conflicting event records."""
        cases = (
            ("[", "invalid JSON"),
            ("{}", "missing event fields"),
            ("[]", "must be an object"),
            ('{"cycle":1,"extra":5}', "unknown event fields"),
            ('{"cycle":1}', "needs a command"),
            ('{"cycle":-1,"reset":true}', "nonnegative"),
            ('{"cycle":1,"reset":1}', "must be Boolean"),
            ('{"cycle":1,"reset":true,"query":{"instrument":0,"max_age":0}}', "cannot carry"),
            ('{"cycle":1,"query":[]}', "query must be an object"),
            ('{"cycle":1,"query":{"instrument":0}}', "missing query fields"),
            ('{"cycle":1,"query":{"instrument":2,"max_age":0}}', "does not fit"),
        )
        for text, error in cases:
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, error):
                    decode_event(text, self.p)

    def test_file_io_keeps_existing_blank_lines_readable(self):
        """Read both normalized events after introducing harmless line whitespace."""
        with TemporaryDirectory() as folder:
            filename = Path(folder) / "source.jsonl"
            write_events(filename, self.events, self.p)
            original = filename.read_text()
            filename.write_text("\n  \n" + original + "\n")
            self.assertEqual(tuple(read_events(filename, self.p)), self.events)

    def test_file_error_mentions_physical_line(self):
        """Attribute a syntax failure to the physical line even when blank lines appear."""
        with TemporaryDirectory() as folder:
            filename = Path(folder) / "events.jsonl"
            filename.write_text('\n{"cycle":0,"reset":true}\n\n{\n')
            with self.assertRaisesRegex(ReplayError, "events.jsonl:4: invalid JSON"):
                tuple(read_events(filename, self.p))


class SchedulerChecks(unittest.TestCase):
    def setUp(self):
        """Prepare normalized requests and a reset that can start a replay."""
        self.p = Profile(1, 1, 8, 4, 7, 5, 8)
        self.cmd = Command.replace(0, 0, 0, (13, 2), (21, 12), 4)
        self.query = Query(0, 16, 5)
        self.reset = Event(0, reset=True)

    def test_parallel_buses_same_cycle(self):
        """Accept one update and one query on the same idle edge."""
        plan = schedule(self.p, [self.reset, Event(1, self.cmd, self.query)])
        self.assertEqual(
            plan.accepted,
            (Accepted("command", 1, 1, 4, 1), Accepted("query", 1, 1, 5, 1)),
        )
        self.assertEqual(len(plan.drives), 4)
        self.assertEqual(plan.canceled, ())
        self.assertEqual(plan.drives[1].query, self.query)
        self.assertEqual(plan.drives[1].command, self.cmd)

    def test_same_edge_overfill_response_queue(self):
        """Queue multiple dated lookups and preserve the head through a full pipeline."""
        plan = schedule(
            self.p,
            [self.reset] + [Event(1, query=replace(self.query, tag=i)) for i in range(4)],
            windows=[SinkWindow(1, 9, result=True)],
        )
        self.assertEqual([entry.actual for entry in plan.accepted], [1, 2, 9, 10])
        self.assertEqual([entry.tag for entry in plan.accepted], [0, 1, 2, 3])
        self.assertEqual([entry.delay for entry in plan.accepted], [0, 1, 8, 9])
        self.assertTrue(all(row.query.tag == 2 for row in plan.drives[3:9]))

    def test_reset_discards_pending_and_flushes_accepted_separately(self):
        """Report canceled source inputs independently of already accepted outputs."""
        pending = [Event(1, command=replace(self.cmd, tag=tag)) for tag in range(3)]
        plan = schedule(
            self.p, [self.reset, *pending, Event(4, reset=True), Event(5, query=self.query)],
            windows=[SinkWindow(1, 7, ack=True)],
        )
        self.assertEqual(plan.canceled, (Canceled("command", 1, 4, 1), Canceled("command", 1, 4, 2)))
        self.assertEqual([entry.tag for entry in plan.accepted], [0, 5])
        self.assertEqual(plan.drives[4].reset, 1)
        self.assertIsNone(plan.drives[4].command)
        model = Machine(self.p)
        report = compare_trace(plan.drives, (model.step(row) for row in plan.drives), self.p)
        self.assertEqual(report["edges"]["reset_flushed_commands"], 1)

    def test_tick_origin_wraps_independently_of_schedule_edges(self):
        """Keep absolute reporting monotonic while hardware timestamps wrap."""
        plan = schedule(
            self.p, [self.reset, Event(1, self.cmd), Event(3, query=self.query)],
            tick_origin=125,
        )
        self.assertEqual([entry.actual for entry in plan.accepted], [1, 3])
        self.assertEqual([entry.tick for entry in plan.accepted], [126, 0])
        self.assertEqual([drive.tick for drive in plan.drives], [125, 126, 127, 0, 1, 2])

    def test_rejects_unsorted_and_conflicting_input(self):
        """Fail if playback order or reset precedence would otherwise be ambiguous."""
        cases = (
            ([Event(3, self.cmd), self.reset], "sorted"),
            ([self.reset, self.reset], "duplicate reset"),
            ([self.reset, Event(0, self.cmd)], "reset and data"),
            ([Event(0, self.cmd)], "explicit reset"),
            ([self.reset, Event(6, self.cmd)], "exceeds schedule limit"),
        )
        for events, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ReplayError, reason):
                    check_order(events, self.p, 6)

    def test_limit_accounts_for_drain(self):
        """Reject an otherwise valid replay that leaves a blocked output at the end."""
        for limit in (4, 7, 11):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(ReplayError, "ran out of cycles"):
                    schedule(
                        self.p, [self.reset, Event(1, query=self.query)],
                        [SinkWindow(1, limit, result=True)], max_cycles=limit,
                    )

    def test_replay_empty_after_reset_is_a_valid_capture(self):
        """Emit one reset row when the replay has no source transactions."""
        plan = schedule(self.p, [self.reset])
        self.assertEqual(plan.drives[0].reset, 1)
        self.assertEqual(plan.canceled, ())
        self.assertEqual(plan.accepted, ())
        self.assertEqual(len(plan.drives), 1)

    def test_rejects_bad_configuration_values(self):
        """Refuse capacities, origins, and windows that have no valid hardware mapping."""
        with self.assertRaisesRegex(ReplayError, "tick_origin"):
            schedule(self.p, [self.reset], tick_origin=128)
        with self.assertRaisesRegex(ReplayError, "at least four"):
            schedule(self.p, [self.reset], max_cycles=3)
        with self.assertRaisesRegex(ReplayError, "window exceeds"):
            schedule(self.p, [self.reset], [SinkWindow(0, 11, ack=True)], max_cycles=10)

    def test_window_merging_is_per_channel(self):
        """Merge overlapping or touching pauses without coupling the two sinks."""
        windows = [
            SinkWindow(4, 8, ack=True), SinkWindow(1, 5, ack=True),
            SinkWindow(8, 12, True, True), SinkWindow(0, 2, result=True),
        ]
        a = merge_windows(windows, "ack")
        q = merge_windows(windows, "result")
        self.assertEqual(list(a), [(1, 12)])
        self.assertEqual(list(q), [(0, 2), (8, 12)])
        self.assertEqual([ready_at(a, edge) for edge in (0, 1, 7, 11, 12)], [1, 0, 0, 0, 1])
        self.assertEqual([ready_at(q, edge) for edge in (0, 1, 2, 8, 11, 12)], [0, 0, 1, 0, 0, 1])

    def test_window_argument_validation(self):
        """Parse each accepted channel and reject fragments and nonpositive intervals."""
        wanted = {"ack": (True, False), "result": (False, True), "both": (True, True)}
        for name, pair in wanted.items():
            self.assertEqual(parse_window(f"2:7:{name}"), SinkWindow(2, 7, *pair))
        for value in ("2:2:ack", "-2:3:both", "frog:3:both", "0:7:foo", "9:7"):
            with self.subTest(value=value):
                with self.assertRaises(ReplayError):
                    parse_window(value)

    def test_schedule_text_round_trip(self):
        """Preserve every offered payload and readiness bit through the decimal file ABI."""
        plan = schedule(
            self.p,
            [self.reset, Event(1, self.cmd, self.query), Event(2, query=self.query)],
            windows=[SinkWindow(1, 9, True, True)],
        )
        with TemporaryDirectory() as folder:
            output = Path(folder) / "replay.txt"
            write_drives(output, plan.drives, self.p)
            self.assertEqual(tuple(read_drives(output, self.p)), plan.drives)

    def test_bad_channel_selector_is_not_silently_interpreted(self):
        """Fail when application callers request an unrecognized replay channel."""
        with self.assertRaises(ReplayError):
            merge_windows([], "other")
        with self.assertRaises(ReplayError):
            event_channel(Event(4, self.cmd), "other")


if __name__ == "__main__":
    unittest.main()
