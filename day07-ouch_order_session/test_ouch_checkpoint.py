"""Verify archive semantics and transaction crash recovery with synthetic state."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from gen_checkpoint_vectors import IMAGE_ID, image_mutations, interrupted_command, open_session
from gen_ouch_vectors import DEFAULT_LOGIN, SESSION, Scenario
from ouch_checkpoint import Archive, CheckpointError, CheckpointStatus, WORD, crc32c
from ouch_checkpoint_store import Anchor, CheckpointStore, StoreError, atomic_image
from ouch_model import Command, CommandKind, NoticeKind, Pending, SessionState
from ouch_protocol import Status, login_accepted, unframe


def sample_archive(slots: int = 4) -> Archive:
    campaign = Scenario(slots,replay_depth=5)
    open_session(campaign)
    interrupted_command(campaign)
    return Archive.from_session(IMAGE_ID,campaign.device)


class CodecTests(unittest.TestCase):
    def test_crc32c_reference(self):
        self.assertEqual(crc32c(b"123456789"),0xE3069283)

    def test_complete_image_restores_command_identity(self):
        expected = sample_archive()
        archive = Archive.decode(expected.encode(),IMAGE_ID)
        self.assertEqual(archive,expected)
        session = archive.restore(replay_depth=5)
        pending = session.store.view(0)
        self.assertEqual(session.store.high_water,3)
        self.assertEqual(session.oldest_verifiable,6)
        self.assertEqual(pending.pending,Pending.REPLACE)
        self.assertEqual((pending.ref,pending.next_ref,pending.leaves,pending.total_filled),(1,3,44,11))
        session.link(True)
        notice,wire = session.request_login(replace(DEFAULT_LOGIN,session=SESSION,sequence=5))
        self.assertEqual((notice.status,wire),(Status.REPLAY_WINDOW,b""))
        notice,wire = session.request_login(replace(DEFAULT_LOGIN,session=SESSION,sequence=6))
        self.assertIsNone(notice)
        self.assertTrue(wire)
        accepted = session.frame(unframe(login_accepted(SESSION,6)))
        self.assertEqual(accepted.kind,NoticeKind.LOGIN)
        retry_notice,exact = session.command(Command(CommandKind.RETRY,0,tag=42,ref=3))
        self.assertEqual(retry_notice.status,Status.OK)
        self.assertEqual(exact,pending.outstanding.client_message().encode())

    def test_cancel_journal_restores_canonical_reference(self):
        order = sample_archive().restore(replay_depth=3).store.view(1)
        self.assertEqual(order.pending,Pending.CANCEL)
        self.assertEqual(order.outstanding.ref,order.outstanding.prev)
        self.assertEqual(order.leaves,23)

    def test_official_geometry_requires_whole_words(self):
        raw = sample_archive().encode()
        for payload in (raw[:-1],raw[:-4],raw + WORD.pack(0)):
            with self.subTest(length=len(payload)):
                with self.assertRaises(CheckpointError) as error:
                    Archive.decode(payload)
                self.assertEqual(error.exception.status,CheckpointStatus.BAD_FRAME)

    def test_corrupt_but_checksummed_images_are_rejected(self):
        archive = sample_archive()
        variants = image_mutations(archive)
        for number,(raw,status,slot) in variants.items():
            with self.subTest(number=number):
                with self.assertRaises(CheckpointError) as caught:
                    Archive.decode(raw,IMAGE_ID)
                self.assertEqual(caught.exception.status,status)
                if slot != 255:
                    self.assertEqual(caught.exception.slot,slot)

    def test_ended_killed_state_roundtrips(self):
        archive = sample_archive(slots=1)
        metadata = replace(archive.metadata,ended=True,killed=True,market_open=False)
        result = replace(archive,metadata=metadata)
        restored = Archive.decode(result.encode()).restore(replay_depth=1)
        self.assertTrue(restored.killed)
        self.assertFalse(restored.market)
        self.assertEqual(restored.state,SessionState.ENDED)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "ledger.sqlite"
        self.store = CheckpointStore(self.path)
        self.archive = sample_archive()

    def test_monotonic_published_pointer_and_exact_retry(self):
        old = self.store.publish(self.archive.encode())
        self.assertEqual(self.store.publish(self.archive.encode()),old)
        different = replace(self.archive,metadata=replace(self.archive.metadata,killed=True))
        with self.assertRaisesRegex(StoreError,"same image ID"):
            self.store.publish(different.encode())
        newer = replace(self.archive,image_id=self.archive.image_id+1)
        active = self.store.publish(newer.encode())
        self.assertEqual(self.store.load(active).archive,newer)
        with self.assertRaisesRegex(StoreError,"regressed"):
            self.store.publish(self.archive.encode())
        with self.assertRaisesRegex(StoreError,"independent anchor"):
            self.store.load(old)
        self.assertEqual(Anchor.parse(str(active)),active)

    def test_sequences_and_kill_may_not_roll_back_under_new_id(self):
        killed = replace(self.archive,metadata=replace(self.archive.metadata,killed=True))
        old = self.store.publish(killed.encode())
        for metadata in (
            self.archive.metadata,
            replace(killed.metadata,checkpoint=killed.metadata.checkpoint-1),
            replace(killed.metadata,session=b"   OTHER01"),
        ):
            with self.subTest(metadata=metadata):
                with self.assertRaises(StoreError):
                    self.store.publish(replace(killed,image_id=IMAGE_ID+1,metadata=metadata).encode())
        self.assertEqual(self.store.load().anchor,old)

    def test_corruption_and_atomic_extraction(self):
        first = self.store.publish(self.archive.encode())
        path = self.root / "restore.bin"
        path.write_bytes(b"pending temporary image")
        atomic_image(path,self.store.load(first).image)
        self.assertEqual(path.read_bytes(),self.archive.encode())
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE ouch_images SET record=?",(b"disk corruption",))
        with self.assertRaisesRegex(StoreError,"digest changed"):
            self.store.load()

    def test_process_cut_during_wal_publication_keeps_prior_image(self):
        original = self.store.publish(self.archive.encode())
        candidate = replace(self.archive,image_id=IMAGE_ID+1).encode()
        image_path = self.root / "uncommitted.bin"
        image_path.write_bytes(candidate)
        script = """
from pathlib import Path
import hashlib
import os
import sqlite3
import sys
database, path, key = sys.argv[1:]
record = Path(path).read_bytes()
connection = sqlite3.connect(database, isolation_level=None)
connection.execute("PRAGMA synchronous=FULL")
connection.execute("BEGIN IMMEDIATE")
connection.execute("INSERT INTO ouch_images VALUES (?,?,?)",
                   (key, hashlib.sha256(record).digest(), record))
connection.execute("UPDATE ouch_latest SET id=? WHERE singleton=1", (key,))
os._exit(71)
"""
        process = subprocess.run([sys.executable,"-B","-c",script,str(self.path),str(image_path),
                                  f"{IMAGE_ID+1:016x}"],check=False)
        self.assertEqual(process.returncode,71)
        self.assertEqual(self.store.load(original).image,self.archive.encode())
        active = self.store.publish(candidate)
        self.assertEqual(active.digest,hashlib.sha256(candidate).digest())
        self.assertEqual(self.store.load(active).image,candidate)


if __name__ == "__main__":
    unittest.main()
