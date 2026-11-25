"""Fault tests for the Day 03 portfolio capture, replay and atomic export."""
from dataclasses import asdict
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from portfolio_journal import (
    JournalError, atomic_text, canonical, check_journal, check_paths, hex_rows,
    pack_journal, snapshot, with_digest,
)
from portfolio_model import Exposure
from portfolio_vectors import Campaign, PROFILES
from portfolio_wire import Kind, decode_row
from risk_model import Key, Operation


class JournalTests(unittest.TestCase):
    def build(self):
        campaign = Campaign(PROFILES["narrow"], seed=13)
        campaign.reset()
        campaign.configure_all()
        campaign.order(Operation.NEW, Key(0, 0, 0), 0, price=30, qty=4)
        campaign.order(Operation.NEW, Key(1, 1, 1), 1, price=20, qty=5)
        campaign.order(Operation.FILL, Key(1, 1, 1), 1, price=19, qty=2, generation=1, event=1)
        campaign.read(None)
        sealed = StringIO()
        anchor = pack_journal("narrow", campaign.stimulus, campaign.gold, sealed)
        return campaign, sealed.getvalue(), anchor

    def change_last(self, journal: str, patch) -> str:
        rows = journal.splitlines(keepends=True)
        last = json.loads(rows.pop())
        last.pop("digest")
        patch(last)
        rows.append(canonical(with_digest(last)) + "\n")
        return "".join(rows)

    def test_recover_full_state_and_original_wire_rows(self):
        campaign, journal, anchor = self.build()
        replay = StringIO()
        report = check_journal(StringIO(journal), anchor, replay)
        profile, terminal, rows, receipts, restored = report
        self.assertEqual((profile, terminal, rows, receipts), ("narrow", anchor, len(campaign.stimulus), 4))
        self.assertEqual(list(hex_rows(StringIO(replay.getvalue()), "replay")), campaign.stimulus)
        self.assertEqual(restored.exposure(None), Exposure(180, 38, 2))
        saved = snapshot(*report)
        self.assertEqual(saved["terminal_digest"], anchor)
        self.assertEqual(saved["scopes"][-1]["exposure"], asdict(Exposure(180, 38, 2)))
        self.assertTrue(saved["slots"][1]["filled"])
        self.assertEqual(saved["slots"][1]["event"], 1)

    def test_reject_corrupted_payload_even_when_json_is_valid(self):
        _, journal, _ = self.build()
        changed = journal.replace('"input":[2,0,1,', '"input":[2,0,170,')
        self.assertNotEqual(changed, journal)
        with self.assertRaisesRegex(JournalError, "digest mismatch"):
            check_journal(StringIO(changed))

    def test_reject_recomputed_digest_around_false_rtl_result(self):
        _, journal, _ = self.build()
        altered = self.change_last(journal, lambda row: row["result"].__setitem__(-1, 123))
        with self.assertRaisesRegex(JournalError, "result disagrees"):
            check_journal(StringIO(altered))

    def test_anchor_detects_removed_suffix(self):
        _, journal, anchor = self.build()
        prefix = "".join(journal.splitlines(keepends=True)[:-1])
        self.assertNotEqual(check_journal(StringIO(prefix))[1], anchor)
        with self.assertRaisesRegex(JournalError, "terminal anchor mismatch"):
            check_journal(StringIO(prefix), anchor)

    def test_hash_chain_detects_insertion(self):
        _, journal, _ = self.build()
        rows = journal.splitlines(keepends=True)
        rows.insert(8, rows[7])
        with self.assertRaisesRegex(JournalError, "bad order or previous digest"):
            check_journal(StringIO("".join(rows)))

    def test_inconsistent_capture_never_seals_as_valid(self):
        campaign, _, _ = self.build()
        captures = list(campaign.gold)
        captures[0] = (*captures[0][:-1], 1)
        with self.assertRaisesRegex(JournalError, "capture disagrees"):
            pack_journal("narrow", campaign.stimulus, captures, StringIO())
        with self.assertRaisesRegex(JournalError, "unmatched capture"):
            pack_journal("narrow", campaign.stimulus, [*campaign.gold, captures[0]], StringIO())
        with self.assertRaisesRegex(JournalError, "first action must reset"):
            pack_journal("narrow", campaign.stimulus[1:], captures, StringIO())

    def test_reject_bool_and_noncanonical_json(self):
        _, journal, _ = self.build()
        altered = self.change_last(journal, lambda row: row["input"].__setitem__(0, False))
        with self.assertRaisesRegex(JournalError, "expected an unsigned integer"):
            check_journal(StringIO(altered))
        with self.assertRaisesRegex(JournalError, "record is not canonical"):
            check_journal(StringIO(journal.replace("{", "{ ", 1)))

    def test_wire_rejects_bits_that_would_truncate(self):
        profile = PROFILES["narrow"]
        with self.assertRaisesRegex(ValueError, "does not fit 8 bits"):
            decode_row(profile, (Kind.ORDER, 0, 1, 0, 0, 0, 0, 0, 0, 256, 1), 1)
        with self.assertRaisesRegex(ValueError, "padding"):
            decode_row(profile, (Kind.RESET, *([0] * 9), 1), 1)
        with self.assertRaisesRegex(ValueError, "unknown action"):
            decode_row(profile, (7, *([0] * 10)), 1)

    def test_atomic_output_stays_intact_when_replay_fails(self):
        with TemporaryDirectory() as root:
            directory = Path(root)
            target = directory / "existing"
            target.write_text("prior\n")
            with self.assertRaises(JournalError):
                with atomic_text(target) as output:
                    output.write("replacement\n")
                    raise JournalError("capture failed")
            self.assertEqual(target.read_text(), "prior\n")
            self.assertEqual(list(directory.iterdir()), [target])

    def test_reject_hard_link_to_input(self):
        with TemporaryDirectory() as root:
            directory = Path(root)
            source = directory / "original"
            source.write_text("input")
            alias = directory / "alias"
            os.link(source, alias)
            with self.assertRaisesRegex(JournalError, "output would replace input"):
                check_paths(alias, source)


if __name__ == "__main__":
    unittest.main()
