"""Corruption, sparse replay, cutoff and independent-seal tests."""

from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from audit_investigator_model import Drive, InvestigatorModel
from audit_tape import Seal, WORD, crc32c, decode_frames, frame, publish_seal, verify
from check_investigator import Scenario, incident, seal
from drop_copy_audit import AuditError, Profile, Record, Replay
from check_drop_copy import REFERENCE
from drop_copy_model import Key, Status
from drop_copy_partition_model import Input, Observation


def generated(profile: Profile) -> list[Record]:
    fixture = Scenario(profile)
    incident(fixture)
    seal(fixture)
    return fixture.model.journal


class CodecTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile(history=96)
        self.rows = generated(self.profile)

    def stream(self,rows=None):
        return b"".join(frame(record.encode(self.profile),self.profile)
                        for record in (self.rows if rows is None else rows))

    def test_castagnoli_and_exact_roundtrip(self):
        self.assertEqual(crc32c(b"123456789"),0xE3069283)
        raw = BytesIO(self.stream())
        restored = [record for _,record in decode_frames(raw,self.profile)]
        self.assertEqual(restored,self.rows)

    def test_corrupt_crc_and_torn_frame(self):
        actual = bytearray(self.stream())
        actual[28] ^= 0x20
        with self.assertRaisesRegex(AuditError,"bad frame checksum"):
            list(decode_frames(BytesIO(actual),self.profile))
        for cutoff in (1,9,53):
            with self.subTest(cutoff=cutoff):
                with self.assertRaisesRegex(AuditError,"torn|truncated"):
                    list(decode_frames(BytesIO(actual[:cutoff]),self.profile))

    def test_nonzero_high_padding_is_rejected_even_with_correct_crc(self):
        sample = bytearray(frame(self.rows[0].encode(self.profile),self.profile))
        sample[-5] |= 128
        sample[-4:] = WORD.pack(crc32c(sample[:-4]))
        with self.assertRaisesRegex(AuditError,"nonzero high padding"):
            list(decode_frames(BytesIO(sample),self.profile))

    def test_resealed_false_diagnostic_cannot_replay(self):
        altered = list(self.rows)
        index = next(i for i,item in enumerate(altered) if item.report and item.report[1]==Status.LATE)
        original = altered[index]
        fields = list(original.report)
        fields[7] += 1  # change accepted age only, then regenerate every necessary CRC
        altered[index] = replace(original,report=tuple(fields))
        machine = Replay(self.profile)
        with self.assertRaisesRegex(AuditError,"differs from exact reconciliation"):
            for _,record in decode_frames(BytesIO(self.stream(altered)),self.profile):
                machine.advance(record)

    def test_marker_replay_catches_invented_epoch(self):
        machine = Replay(self.profile)
        mark = next(i for i,item in enumerate(self.rows) if item.marker)
        for row in self.rows[:mark]:
            machine.advance(row)
        sample = self.rows[mark]
        false = replace(sample,marker=replace(sample.marker,epochs=sample.marker.epochs^1))
        with self.assertRaisesRegex(AuditError,"marker state differs"):
            machine.advance(false)

    def test_run_and_sequence_cannot_be_spliced(self):
        for changed in (replace(self.rows[1],run=self.rows[1].run^64),
                        replace(self.rows[1],index=self.rows[1].index+1)):
            with self.subTest(changed=changed):
                machine = Replay(self.profile)
                machine.advance(self.rows[0])
                with self.assertRaisesRegex(AuditError,"not contiguous"):
                    machine.advance(changed)

    def test_incomplete_or_extended_terminal_tape_is_rejected(self):
        machine = Replay(self.profile)
        for record in self.rows[:-1]:
            machine.advance(record)
        with self.assertRaisesRegex(AuditError,"no terminal"):
            machine.summary()
        machine.advance(self.rows[-1])
        extra = replace(self.rows[-1],index=self.rows[-1].index+1,cycle=self.rows[-1].cycle+1)
        with self.assertRaisesRegex(AuditError,"follows the terminal"):
            machine.advance(extra)

    def test_cycle_budget_and_repeated_edge_are_rejected(self):
        machine = Replay(self.profile,max_cycles=300)
        too_late = replace(self.rows[0],cycle=2000)
        with self.assertRaisesRegex(AuditError,"configured cycle"):
            machine.advance(too_late)
        machine.advance(self.rows[0])
        with self.assertRaisesRegex(AuditError,"cycle repeated"):
            machine.advance(replace(self.rows[1],cycle=self.rows[0].cycle))

    def test_fast_forward_matches_every_silent_edge_around_timeouts(self):
        current = Replay(self.profile)
        for number,row in enumerate(self.rows):
            if number % 3 == 1:
                slow = deepcopy(current)
                limit = 40 + number
                for _ in range(limit):
                    slow.machine.advance(Input(ready=False))
                fast = deepcopy(current)
                fast.skip_idle(current.cycle+limit)
                self.assertEqual(fast.machine.held,slow.machine.held)
                self.assertEqual(fast.machine.incidents,slow.machine.incidents)
                for fast_ledger,slow_ledger in zip(fast.machine.ledgers,slow.machine.ledgers):
                    self.assertEqual(fast_ledger.tick,slow_ledger.tick)
                    self.assertEqual(fast_ledger.report,slow_ledger.report)
                    self.assertEqual(fast_ledger.counts,slow_ledger.counts)
                    self.assertEqual(fast_ledger.entries,slow_ledger.entries)
            current.advance(row)

    def test_quiet_span_beyond_tick_wrap_retires_closed_tombstone(self):
        replay = Replay(self.profile,max_cycles=1<<40)
        one = Observation(0,(Key(0,1,2,17),REFERENCE))
        replay.machine.advance(Input(primary=one,ready=True))
        replay.machine.advance(Input(copy=one,ready=True))
        replay.cycle = 2
        self.assertTrue(replay.machine.ledgers[0].entries[one.event[0]].closed)
        replay.skip_idle((1<<32)+8)
        self.assertNotIn(one.event[0],replay.machine.ledgers[0].entries)
        self.assertEqual(replay.machine.ledgers[0].tick,(self.profile.reset_tick+8)&0xFFFFFFFF)

    def test_reset_without_run_id_never_opened_the_trace_port(self):
        machine = InvestigatorModel(self.profile)
        machine.step(Drive(reset=True,run_id=0))
        seen = machine.step(Drive(mark=12,terminal=True))
        self.assertFalse(seen.mark_ready)
        self.assertFalse(machine.seal)

    def test_detached_seal_detects_semantically_valid_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/"capture.dca"
            source.write_bytes(self.stream())
            summary,anchor = verify(source,self.profile,namespace=0,execution=0x917)
            self.assertTrue(any(row["status"]=="LATE" for row in summary["selected_reports"]))
            seal_path = root/"anchor.json"
            publish_seal(seal_path,anchor)
            self.assertEqual(Seal.read(seal_path),anchor)
            edited = replace(self.rows[-1],marker=replace(self.rows[-1].marker,tag=0))
            source.write_bytes(self.stream([*self.rows[:-1],edited]))
            self.assertEqual(verify(source,self.profile)[0]["records"],summary["records"])
            with self.assertRaisesRegex(AuditError,"independent terminal seal"):
                verify(source,self.profile,expected=Seal.read(seal_path))


if __name__ == "__main__":
    unittest.main()
