"""Integration and corruption fixtures for the offline journal and exact RTL replay."""

from copy import deepcopy
from dataclasses import asdict, replace
from io import StringIO
import json
import unittest

from market_cosim import ReplayVector, audit_and_export, compare, expected_pins, validate_simulator_config
from market_reference import Command, Config, Operation, PROFILES, Status
from market_replay import CSV_FIELDS, FeedRow, JOURNAL_SCHEMA, ReplaySession, fixture, read_rows, write_rows
from market_trace_tools import JournalError, audit_stream, export_rows, read_config, read_entries
from verify_market import Trace


def convert(rows, cfg=None):
    stream = StringIO()
    write_rows(stream, rows)
    stream.seek(0)
    return list(read_rows(stream, cfg or Config()))


def draw(raw, cfg=None):
    stream = StringIO(",".join(CSV_FIELDS) + "\n" + raw + "\n")
    return list(read_rows(stream, cfg or Config()))


def journal_source(cfg, results):
    header = {"schema": JOURNAL_SCHEMA, "profile": "fixture", "config": asdict(cfg)}
    rows = [json.dumps(header), *(json.dumps(row) for row in results)]
    return StringIO("\n".join(rows) + "\n")


def rendered(cfg, feed):
    runner = ReplaySession(cfg)
    rows = [row.mapping() for row in runner.expand(feed)]
    return json.loads(json.dumps(rows))


def seed_bank(cfg):
    return [
        FeedRow(0, reset=True),
        FeedRow(2, command=Command(op=Operation.SNAP_BEGIN, sequence=1, tag=1)),
        FeedRow(3, command=Command(op=Operation.SNAP_ADD, sequence=2, quantity=10, tag=2)),
        FeedRow(4, command=Command(op=Operation.SNAP_ADD, sequence=3, side=True, order_id=1,
                                   price=cfg.levels-1, quantity=8, tag=3)),
        FeedRow(5, command=Command(op=Operation.SNAP_END, sequence=4, tag=4)),
    ]


class CSVCase(unittest.TestCase):
    def test_fixture_round_trips_all_profiles(self):
        for profile, cfg in PROFILES.items():
            with self.subTest(profile=profile):
                original = list(fixture(cfg))
                self.assertEqual(convert(original, cfg), original)

    def test_accepts_numeric_reserved_opcode(self):
        row = FeedRow(1, command=Command(op=14, order_id=2, tag=3))
        self.assertEqual(convert([row]), [row])

    def test_rejects_duplicate_or_out_of_order_cycle(self):
        for rows in ([FeedRow(4), FeedRow(4)], [FeedRow(7), FeedRow(6)]):
            with self.assertRaisesRegex(ValueError, "strictly increasing"):
                convert(rows)

    def test_rejects_boolean_other_than_zero_or_one(self):
        row = "0,0,2,1,1,,,,,,,,,,"
        with self.assertRaisesRegex(ValueError, "tick must be"):
            draw(row)

    def test_rejects_command_without_operation(self):
        row = "0,0,0,1,1,1,,4,,,,,,,"
        with self.assertRaisesRegex(ValueError, "fields require"):
            draw(row)

    def test_rejects_overflow_instead_of_silently_truncating(self):
        row = FeedRow(2, command=Command(op=Operation.ADD, quantity=Config().quantity_max+1))
        with self.assertRaisesRegex(ValueError, "quantity.*outside"):
            convert([row])

    def test_rejects_arrival_on_reset(self):
        row = FeedRow(0, reset=True, command=Command())
        with self.assertRaisesRegex(ValueError, "combined with reset"):
            convert([row])


class HostReplayCase(unittest.TestCase):
    def setUp(self):
        self.cfg = PROFILES["edge"]

    def test_buffer_preserves_order_while_diagnostic_stalls(self):
        arrival = [
            FeedRow(0, reset=True),
            FeedRow(1, command=Command(op=Operation.BBO, tag=1)),
            FeedRow(2, response_ready=False, command=Command(op=Operation.BBO, tag=2)),
            FeedRow(3, response_ready=False, command=Command(op=Operation.BBO, tag=3)),
            FeedRow(4, response_ready=False),
            FeedRow(5),
        ]
        session = ReplaySession(self.cfg, capacity=2)
        rows = list(session.expand(arrival))
        offered = [row.offered["tag"] for row in rows if row.offered]
        accepted = [row.offered["tag"] for row in rows if row.accepted]
        responses = [row.response["tag"] for row in rows if row.response and row.response_ready]
        self.assertEqual(offered, [1, 2, 2, 2, 2, 3])
        self.assertEqual((accepted, responses), ([1, 2, 3], [1, 2, 3]))
        self.assertEqual(session.counters["peak_backlog"], 2)

    def test_bounded_host_backlog_signals_overflow(self):
        book = ReplaySession(self.cfg, capacity=1)
        book.step(FeedRow(0, command=Command(op=Operation.BBO)))
        book.step(FeedRow(1, command=Command(op=Operation.BBO, tag=1), response_ready=False))
        with self.assertRaisesRegex(OverflowError, "cycle 2"):
            book.step(FeedRow(2, command=Command(op=Operation.BBO, tag=2), response_ready=False))

    def test_reset_discards_stalled_ingress_not_future_inputs(self):
        tape = [
            FeedRow(0, reset=True),
            FeedRow(1, command=Command(op=Operation.BBO, tag=1)),
            FeedRow(2, response_ready=False, command=Command(op=Operation.BBO, tag=2)),
            FeedRow(3, reset=True),
            FeedRow(4, command=Command(op=Operation.BBO, tag=3)),
        ]
        r = ReplaySession(self.cfg)
        cycles = list(r.expand(tape))
        self.assertEqual([row.dropped_ingress for row in cycles if row.reset], [0, 1])
        self.assertEqual([row.response["tag"] for row in cycles if row.response and row.response_ready], [3])
        self.assertEqual(r.counters["accepts"], 2)

    def test_stale_quote_crosses_transport_but_consumer_drops_it(self):
        commands = seed_bank(self.cfg)
        commands += [
            FeedRow(6, quote_ready=False),
            FeedRow(7, quote_ready=False),
            FeedRow(8, quote_ready=False, tick=True),
            FeedRow(9, quote_ready=False),
            FeedRow(10),
        ]
        ledger = list(ReplaySession(self.cfg).expand(commands))
        old = next(row for row in ledger if row.cycle == 8)
        held = next(row for row in ledger if row.cycle == 9)
        self.assertEqual(old.quote, held.quote)
        self.assertTrue(old.quote_current)
        self.assertFalse(held.quote_current)
        self.assertEqual(next(row.quote_action for row in ledger if row.cycle == 10), "drop_ineligible")
        self.assertEqual(ledger[11].quote_action, "withdraw_intent")

    def test_same_epoch_changed_size_drops_superseded_offer(self):
        commands = seed_bank(self.cfg)
        commands += [
            FeedRow(6, quote_ready=False),
            FeedRow(7, quote_ready=False),
            FeedRow(8, quote_ready=False, command=Command(op=Operation.CANCEL, sequence=5,
                                                        order_id=1, quantity=7, tag=5)),
            FeedRow(9, quote_ready=False),
            FeedRow(10),
        ]
        ledger = list(ReplaySession(self.cfg).expand(commands))
        self.assertEqual(ledger[10].quote_action, "drop_superseded")
        self.assertEqual(ledger[11].quote_action, "publish_intent")
        self.assertEqual(ledger[11].quote["size"], 1)

    def test_inventory_exposes_aggregates_and_counter_values(self):
        session = ReplaySession(self.cfg)
        list(session.expand(seed_bank(self.cfg)))
        view, = session.inventory()
        self.assertEqual((view["expected_sequence"], view["mode"], view["epoch"]), (5, "LIVE", 1))
        self.assertEqual(view["statistics"]["LIVE_ORDERS"], 2)
        self.assertEqual([(level["price"], level["size"]) for level in view["levels"]], [(0, 10), (2, 8)])
        self.assertEqual(view["installed_offline_intent"]["size"], self.cfg.quote_size)


class AuditCase(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.output = rendered(self.cfg, fixture(self.cfg))

    def audit(self, data=None):
        return audit_stream(journal_source(self.cfg, data if data is not None else self.output))

    def test_journal_covers_gaps_and_watchdogs(self):
        result = self.audit()
        self.assertEqual(result["status_counts"], {"OK": 12, "GAP": 1})
        self.assertEqual(result["quote_actions"]["publish_intent"], 2)
        self.assertEqual(result["quote_actions"]["withdraw_intent"], 2)
        self.assertEqual(result["counters"]["gate_revokes"], 2)
        self.assertEqual(result["installed_offline"], {})

    def test_audit_rejects_nonconsecutive_cycles(self):
        rows = self.output[:5] + self.output[6:]
        with self.assertRaisesRegex(JournalError, "nonconsecutive"):
            self.audit(rows)

    def test_audit_rejects_changed_response_echo(self):
        rows = deepcopy(self.output)
        row = next(row for row in rows if row["response"] is not None)
        row["response"]["tag"] = 900
        with self.assertRaisesRegex(JournalError, "oldest accepted"):
            self.audit(rows)

    def test_audit_rejects_current_quote_with_an_old_epoch(self):
        rows = deepcopy(self.output)
        row = next(row for row in rows if row["quote_current"])
        row["quote"]["epoch"] = 9
        row["quote_action"] = "held" if not row["quote_ready"] else "drop_epoch"
        with self.assertRaisesRegex(JournalError, "wrong epoch"):
            self.audit(rows)

    def test_audit_rejects_an_invented_simultaneous_pressure(self):
        rows = deepcopy(self.output)
        row = next(row for row in rows if row["live"][0])
        row["pressure_bid"][0] = row["pressure_ask"][0] = True
        with self.assertRaisesRegex(JournalError, "simultaneous"):
            self.audit(rows)

    def test_audit_rejects_quote_revision_discontinuity(self):
        rows = deepcopy(self.output)
        row = next(row for row in rows if row["quote"] is not None)
        row["quote"]["revision"] = 2
        if not row["quote_ready"]:
            following = rows[row["cycle"]+1]
            while following["quote"] is not None:
                following["quote"]["revision"] = 2
                if following["quote_ready"]:
                    break
                following = rows[following["cycle"]+1]
        with self.assertRaisesRegex(JournalError, "revision.*expected"):
            self.audit(rows)

    def test_timeline_filters_interval_and_instrument(self):
        source = journal_source(self.cfg, self.output)
        cfg, _ = read_config(source)
        result = list(export_rows(read_entries(source, cfg), bank=1, first=0, last=18))
        self.assertTrue(result)
        self.assertTrue(all(row["instrument"] == 1 and row["cycle"] <= 18 for row in result))
        self.assertTrue(any(row["entity"] == "quote" for row in result))


class ExactCosimCase(unittest.TestCase):
    def setUp(self):
        self.cfg = PROFILES["edge"]
        self.output = rendered(self.cfg, fixture(self.cfg))

    def entries(self):
        stream = journal_source(self.cfg, self.output)
        read_config(stream)
        return list(read_entries(stream, self.cfg))

    def test_vectors_match_input_lengths_and_reset_order(self):
        out = StringIO()
        cfg, count, summary = audit_and_export(journal_source(self.cfg, self.output), out)
        vectors = out.getvalue().splitlines()
        self.assertEqual(cfg, self.cfg)
        self.assertEqual(vectors[0].split()[:6], ["0", "1", "0", "1", "1", "0"])
        self.assertEqual(count, len(vectors))
        self.assertEqual(summary["cycles"], count)
        self.assertTrue(all(len(vector.split()) == 16 for vector in vectors))

    def test_compare_finds_a_missing_trace_tail(self):
        entries = self.entries()
        trace = [
            Trace(entry.register.cycle, ReplayVector.from_entry(entry).stimulus, expected_pins(entry))
            for entry in entries
        ]
        stats = compare(entries, trace)
        self.assertEqual(stats.max_response_latency, 1)
        with self.assertRaisesRegex(JournalError, "ended before"):
            compare(entries, trace[:-1])

    def test_compare_explains_signal_and_recent_context(self):
        entries = self.entries()
        trace = [
            Trace(entry.register.cycle, ReplayVector.from_entry(entry).stimulus, expected_pins(entry))
            for entry in entries
        ]
        fault = next(n for n, item in enumerate(trace) if item.observed.response is not None)
        pins = trace[fault].observed
        altered = replace(pins, response=replace(pins.response, expected=7))
        trace[fault] = replace(trace[fault], observed=altered)
        with self.assertRaisesRegex(JournalError, "response.expected=7"):
            compare(entries, trace)

    def test_signal_vectors_require_initial_hardware_reset(self):
        rows = rendered(self.cfg, [FeedRow(1, command=Command(op=Operation.BBO))])
        with self.assertRaisesRegex(JournalError, "begin with an explicit"):
            audit_and_export(journal_source(self.cfg, rows), StringIO())

    def test_sim_config_rejects_unsafe_oversized_width(self):
        with self.assertRaisesRegex(JournalError, "sequence_width"):
            validate_simulator_config(replace(self.cfg, sequence_width=32))
        with self.assertRaisesRegex(JournalError, "outside the legal"):
            validate_simulator_config(replace(self.cfg, imbalance_q8=257))


if __name__ == "__main__":
    unittest.main()
