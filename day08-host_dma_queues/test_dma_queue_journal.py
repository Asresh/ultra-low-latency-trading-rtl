from dataclasses import replace
from pathlib import Path
from random import Random
import os
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from dma_protocol import Completion, Descriptor, Opcode, Status, U32_MASK, U64_MASK
from dma_queue_journal import Journal, JournalError, JournalStore, QueueIdentity, Snapshot


GENERATION = UUID("3996863f-7b1a-46be-b5ae-36ec12138eaf")
IDENTITY = QueueIdentity(GENERATION, 3, 0xfffffffe, 4, 0x100, 0x280)


def command(ordinal):
    return Descriptor(Opcode.FENCE, tag=ordinal&0xffff, cookie=ordinal).encode()


def result(ordinal, *, queue=3, status=Status.OK):
    return Completion.of_descriptor(
        Descriptor.decode(command(ordinal)), queue, status, ordinal + 7,
    ).encode()


def ring_image(identity, begin, records):
    data = bytearray(b"\xa5" * identity.depth * 32)
    for ordinal, record in enumerate(records, start=begin):
        offset = (identity.pointer(ordinal) & (identity.depth - 1)) * 32
        data[offset:offset + 32] = record
    return bytes(data)


def snapshot(identity=IDENTITY, *, retired=0, published=0, released=0, sq=None, cq=None):
    current_sq = [command(i) for i in range(retired, published)] if sq is None else sq
    current_cq = [result(i) for i in range(released, retired)] if cq is None else cq
    return Snapshot(
        identity, identity.pointer(retired), identity.pointer(published),
        identity.pointer(released), identity.pointer(retired),
        ring_image(identity, retired, current_sq), ring_image(identity, released, current_cq),
    )


class RecoveryWindows(unittest.TestCase):
    def setUp(self):
        self.journal = Journal(IDENTITY).prepare(0, [command(i) for i in range(12)])

    def test_fresh_plan_only_uses_ring_credit_and_preserves_order_at_wrap(self):
        updated, plan = self.journal.reconcile(snapshot())
        self.assertEqual(updated, self.journal)
        self.assertEqual((plan.submit_tail, plan.release_head), (2, IDENTITY.seed))
        self.assertEqual([item.address for item in plan.writes], [0x140, 0x160, 0x100, 0x120])
        self.assertEqual([item.record for item in plan.writes], [command(i) for i in range(4)])

    def test_posted_sq_is_not_reissued_and_only_retired_cq_is_visible(self):
        capture = snapshot(published=4)
        speculative = ring_image(IDENTITY, 0, (result(0),))
        updated, plan = self.journal.reconcile(replace(capture,cq_image=speculative))
        self.assertEqual((updated.published, len(updated.completions)), (4, 0))
        self.assertFalse(plan.has_doorbell)
        self.assertEqual(plan.writes, ())

    def test_joint_credit_recovery_archives_before_released(self):
        updated, plan = self.journal.reconcile(snapshot(retired=2, published=4))
        self.assertEqual(updated.completions, (result(0), result(1)))
        self.assertEqual(plan.release_head, 0)
        self.assertEqual([item.ordinal for item in plan.writes], [4, 5])
        next_state, next_plan = updated.reconcile(snapshot(retired=3,published=6,released=2))
        self.assertEqual((next_state.published, next_state.released, len(next_state.completions)), (6, 2, 3))
        self.assertEqual([item.ordinal for item in next_plan.writes], [6])

    def test_release_cannot_cross_the_previous_durable_archive(self):
        with self.assertRaisesRegex(JournalError,"cross durable"):
            self.journal.reconcile(snapshot(retired=2,published=2,released=2))

    def test_published_sq_must_have_durable_intent(self):
        with self.assertRaisesRegex(JournalError,"cross durable"):
            Journal(IDENTITY).reconcile(snapshot(published=1))

    def test_previously_published_sq_cannot_regress(self):
        updated, _ = self.journal.reconcile(snapshot(published=2))
        with self.assertRaisesRegex(JournalError,"SQ tail moved backward"):
            updated.reconcile(snapshot(published=1))

    def test_stale_cq_capture_cannot_regress_even_after_native_wrap(self):
        updated, _ = self.journal.reconcile(snapshot(retired=2,published=2))
        with self.assertRaisesRegex(JournalError,"CQ tail moved backward"):
            updated.reconcile(snapshot(retired=1,published=2))

    def test_capture_must_hold_equal_engine_owned_commit_cursors(self):
        with self.assertRaisesRegex(JournalError,"same edge"):
            self.journal.reconcile(replace(snapshot(),sq_head=U32_MASK))

    def test_pending_sq_bytes_must_equal_prepared_records(self):
        with self.assertRaisesRegex(JournalError,"SQ bytes differ at ordinal 0"):
            self.journal.reconcile(snapshot(published=2,sq=[command(1),command(0)]))

    def test_previously_archived_but_unreleased_cq_cannot_change(self):
        updated, _ = self.journal.reconcile(snapshot(retired=1,published=1))
        with self.assertRaisesRegex(JournalError,"archived CQ bytes differ at ordinal 0"):
            updated.reconcile(snapshot(retired=1,published=1,cq=[result(0,status=Status.BAD_DESC)]))

    def test_new_cq_rejects_wrong_queue_and_wrong_identity(self):
        for record in (result(0,queue=0),result(1)):
            with self.subTest(record=record), self.assertRaisesRegex(JournalError,"does not identify"):
                self.journal.reconcile(snapshot(retired=1,published=1,cq=[record]))

    def test_torn_or_invalid_cq_is_not_released(self):
        damaged = bytearray(result(0))
        damaged[16] = 1
        with self.assertRaisesRegex(JournalError,"reserved completion bits"):
            self.journal.reconcile(snapshot(retired=1,published=1,cq=[bytes(damaged)]))

    def test_canonical_failed_descriptor_fetch_can_archive(self):
        journal = Journal(IDENTITY).prepare(0,(command(77),))
        data = Completion(Status.READ_ERROR,0,0,0,0,0,3,5).encode()
        updated, _ = journal.reconcile(snapshot(retired=1,published=1,cq=[data]))
        self.assertEqual(updated.completions, (data,))

    def test_stopped_and_reconfigured_queue_fail_closed(self):
        bad_captures = [
            replace(snapshot(),fatal=1),
            replace(snapshot(),enabled=False),
            snapshot(replace(IDENTITY,generation=UUID(int=5))),
            snapshot(replace(IDENTITY,sq_base=0x400)),
        ]
        for capture in bad_captures:
            with self.subTest(capture=capture), self.assertRaises(JournalError):
                self.journal.reconcile(capture)

    def test_serialized_ordinal_cannot_wrap(self):
        with self.assertRaisesRegex(JournalError,"one ring window"):
            self.journal.advance(U64_MASK,IDENTITY.pointer(U64_MASK+1),"SQ tail")

    def test_randomized_credit_and_retry_schedule(self):
        rng = Random(35842)
        for depth in (2,4,8,32):
            identity = replace(IDENTITY,depth=depth,sq_base=0x1000,cq_base=0x3000)
            count = 293
            journal = Journal(identity).prepare(0,[command(i) for i in range(count)])
            published = retired = released = 0
            refused = stalls = 0
            for _ in range(4000):
                cq_free = depth - (retired - released)
                retired += rng.randrange(1 + min(cq_free,published-retired))
                journal, plan = journal.reconcile(
                    snapshot(identity,retired=retired,published=published,released=released),
                )
                self.assertEqual(journal.completions,tuple(result(i) for i in range(retired)))
                if plan.has_doorbell and rng.randrange(3):
                    count_written = len(plan.writes)
                    want = list(range(published,min(count,retired+depth)))
                    self.assertEqual([write.ordinal for write in plan.writes],want)
                    self.assertEqual(plan.submit_tail,identity.pointer(published+count_written))
                    self.assertEqual(plan.release_head,identity.pointer(retired))
                    published += count_written
                    released = retired
                elif plan.has_doorbell:
                    refused += 1
                else:
                    stalls += 1
                if released == count and not plan.has_doorbell:
                    break
            self.assertGreater(refused,0)
            self.assertGreater(stalls,0)
            self.assertEqual((published,retired,released),(count,count,count))


class DurableImages(unittest.TestCase):
    def setUp(self):
        self.scratch = TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / "queue.journal"
        self.store = JournalStore(self.path)
        self.store.create(IDENTITY)

    def test_reopen_preserves_byte_exact_results_and_immutable_intent(self):
        self.store.prepare(0,(command(0),command(1)))
        self.store.prepare(1,(command(1),))
        with self.assertRaisesRegex(JournalError,"conflicts"):
            self.store.prepare(1,(command(7),))
        with self.assertRaisesRegex(JournalError,"contiguously"):
            self.store.prepare(1,(command(1),command(2)))
        JournalStore(self.path).reconcile(snapshot(retired=1,published=2))
        after = JournalStore(self.path).read()
        self.assertEqual(after.submissions,(command(0),command(1)))
        self.assertEqual(after.completions,(result(0),))

    def test_wrong_generation_cannot_replace_an_existing_file(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(JournalError,"different queue incarnation"):
            self.store.create(replace(IDENTITY,generation=UUID(int=2)))
        self.assertEqual(self.path.read_bytes(),before)

    def test_formats_detect_corruption_and_truncation_in_both_images(self):
        images = [(Snapshot.decode,snapshot().encode()),(Journal.decode,self.path.read_bytes())]
        for decode, image in images:
            damaged = bytearray(image)
            damaged[19] ^= 1
            for variant in (image[:12],image[:-1],bytes(damaged),image+b"\0"):
                with self.subTest(decode=decode, size=len(variant)), self.assertRaises(JournalError):
                    decode(variant)

    def test_failed_file_sync_leaves_previous_image_byte_exact(self):
        before = self.path.read_bytes()
        with patch("dma_queue_journal.os.fsync",side_effect=OSError("file sync failed")):
            with self.assertRaisesRegex(OSError,"file sync failed"):
                self.store.prepare(0,(command(0),))
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual({path.name for path in self.path.parent.iterdir()},{".queue.journal.lock","queue.journal"})

    def test_ambiguous_directory_sync_can_be_retried_with_same_batch(self):
        real_sync = os.fsync

        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
            real_sync(fd)

        with patch("dma_queue_journal.os.fsync",side_effect=fail_directory):
            with self.assertRaisesRegex(OSError,"directory sync failed"):
                self.store.prepare(0,(command(0),))
        with patch("dma_queue_journal.os.fsync", wraps=real_sync) as synced:
            updated = self.store.prepare(0,(command(0),))
            self.assertEqual(synced.call_count,2)
        self.assertEqual(updated.submissions,(command(0),))

    def test_failed_archive_sync_does_not_return_a_release_plan(self):
        self.store.prepare(0,(command(0),))
        with patch("dma_queue_journal.os.fsync",side_effect=OSError("archive sync failed")):
            with self.assertRaisesRegex(OSError,"archive sync failed"):
                self.store.reconcile(snapshot(retired=1,published=1))
        self.assertEqual(self.store.read().completions,())
        plan = self.store.reconcile(snapshot(retired=1,published=1))
        self.assertEqual(plan.release_head,U32_MASK)

    def test_conflicting_prepare_from_two_processes_leaves_one_durable_record(self):
        inputs = [self.path.with_name(f"submission-{i}.bin") for i in range(2)]
        for i, path in enumerate(inputs):
            path.write_bytes(command(i))
        driver = Path(__file__).with_name("dma_checkpoint.py")
        children = [
            subprocess.Popen(
                [sys.executable,"-B",str(driver),str(self.path),"prepare","0",str(path)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            )
            for path in inputs
        ]
        for child in children:
            child.communicate(timeout=10)
        self.assertEqual(sorted(child.returncode for child in children),[0,1])
        winner = next(i for i,child in enumerate(children) if child.returncode == 0)
        self.assertEqual(self.store.read().submissions,(command(winner),))


if __name__ == "__main__":
    unittest.main()
