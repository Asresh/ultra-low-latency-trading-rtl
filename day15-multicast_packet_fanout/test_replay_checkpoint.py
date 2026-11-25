"""Known-answer tests for replay gaps, reset seeds and capture integrity."""

from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zlib import crc32

from replay_checkpoint import Archive, CRC, audit_window, avoid_alias, unpack_input
from replay_model import Beat, Configuration, ConfigStatus, Geometry, Input, Op, Packet, PROFILES
from replay_verify import Scenario, hexadecimal, singleton, worked


G = Geometry(2,3,3,2,5,3,7)


def worked_archive():
    g = PROFILES["default"]
    run = worked(g)
    with StringIO("".join(hexadecimal(row)+"\n" for row in run.stimulus)) as tape,          StringIO("".join(hexadecimal(row)+"\n" for row in run.expected)) as captured:
        return audit_window(tape,captured,g)


class ReplaySemantics(unittest.TestCase):
    def test_lagging_port_keeps_evicted_packet_and_reports_two_exact_gaps(self):
        work = worked(PROFILES["default"])
        fast,slow,*_ = work.events
        self.assertEqual([event.sequence for _,event in fast if event.last],list(range(12)))
        self.assertEqual([(ev.sequence,ev.resume) for _,ev in slow if ev.gap],[(1,5),(5,8)])
        self.assertEqual([ev.data for _,ev in slow if not ev.gap and ev.sequence==0],[0xface0031,0xbb32])
        self.assertEqual((work.model.subscribers[1].sent,work.model.subscribers[1].lost),(5,7))
        self.assertGreater(work.metrics["max_resident"],work.g.retain)
        self.assertGreater(work.metrics["held_gaps"],1)

    def test_reset_seed_near_epoch_exhaustion_and_sticky_sentinel(self):
        run = Scenario(G)
        run.edge(reset=True,epoch=4,seed=G.limit-2)
        run.configure(Configuration(Op.FOLLOW,0,4))
        run.publish((singleton(G,11),))
        run.publish((singleton(G,12),))
        self.assertFalse(run.edge(beat=singleton(G,13)).source_ready)
        run.drain()
        self.assertEqual([ev.sequence for _,ev in run.events[0] if ev.last],[G.limit-2,G.limit-1])
        self.assertEqual(run.model.next_sequence,G.limit)
        run.edge(reset=True,epoch=5,seed=0)
        self.assertEqual((run.model.earliest,run.model.next_sequence),(0,0))
        run.configure(Configuration(Op.FOLLOW,0,4))
        self.assertEqual(run.model.cfg_report.status,ConfigStatus.BAD_EPOCH)

    def test_change_during_active_last_transfer_returns_busy_and_keeps_order(self):
        run = Scenario(G)
        run.edge(reset=True,epoch=2)
        run.configure(Configuration(Op.FOLLOW,0,2))
        run.publish((singleton(G,3),),ready=0)
        run.idle(1,ready=0)
        run.configure(Configuration(Op.DETACH,0,2,tag=7),ready=1)
        self.assertEqual(run.model.cfg_report.status,ConfigStatus.BUSY)
        self.assertTrue(run.model.subscribers[0].enabled)
        run.configure(Configuration(Op.DETACH,0,2,tag=8),ready=1)
        self.assertFalse(run.model.subscribers[0].enabled)

    def test_follow_colliding_with_publication_receives_same_edge_sequence(self):
        run = Scenario(G)
        run.edge(reset=True,epoch=3,seed=12)
        outcome = run.edge(config=Configuration(Op.FOLLOW,0,3),beat=singleton(G,42),ready=1)
        self.assertTrue(outcome.cfg_ready and outcome.source_ready)
        run.drain()
        self.assertEqual([(r.sequence,r.tag) for _,r in run.events[0]],[(12,42)])


class ArchiveFormat(unittest.TestCase):
    def test_canonical_replay_and_new_epoch(self):
        archive = worked_archive()
        self.assertEqual((archive.earliest,archive.next_sequence,archive.epoch),(8,12,9))
        self.assertEqual(Archive.decode(archive.encode()),archive)
        restored = Scenario(archive.geometry)
        for request in archive.restore(12,0):
            restored.edge(request)
        self.assertEqual(restored.model.epoch,12)
        self.assertEqual(tuple(restored.model.packets),archive.packets)
        self.assertEqual([ev.sequence for _,ev in restored.events[0] if ev.last],[8,9,10,11])

    def test_reject_bad_format_crc_tail_and_width(self):
        data = bytearray(worked_archive().encode())
        mutated = bytearray(data)
        mutated[-1] ^= 1
        with self.assertRaisesRegex(ValueError,"CRC"):
            Archive.decode(mutated)
        trailer = data[:-4]+b"x"
        with self.assertRaisesRegex(ValueError,"trailing"):
            Archive.decode(trailer+CRC.pack(crc32(trailer)))
        data[0] = 35
        with self.assertRaisesRegex(ValueError,"unknown"):
            Archive.decode(data)

    def test_reject_noncontiguous_and_noncanonical_packet_images(self):
        frame = Packet(5,27,(Beat(1,1,True,27),))
        cases = (
            (replace(frame,sequence=4),"contiguous"),
            (replace(frame,beats=(Beat(1,0,True,27),)),"keep"),
            (replace(frame,beats=(Beat(0x800,1,True,27),)),"outside keep"),
            (replace(frame,beats=(Beat(1,1,False,27),)),"last"),
            (replace(frame,beats=(Beat(1,1,True,20),)),"tag"),
        )
        for packet,reason in cases:
            with self.subTest(packet=packet),self.assertRaisesRegex(ValueError,reason):
                Archive(G,2,6,(packet,))
        with self.assertRaisesRegex(ValueError,"retention"):
            Archive(G,2,0,(frame,))

    def test_incomplete_trace_and_an_overwritten_observation_fail(self):
        run = Scenario(G)
        run.edge(reset=True,epoch=1)
        run.edge(beat=Beat(123,G.full_mask,False,7))
        packed = [hexadecimal(row) for row in run.stimulus]
        observed = [hexadecimal(row) for row in run.expected]
        with self.assertRaisesRegex(ValueError,"ingress boundary"):
            audit_window(packed,observed,G)
        with self.assertRaisesRegex(ValueError,"lengths"):
            audit_window(packed,observed[:-1],G)
        with self.assertRaisesRegex(ValueError,"differs|disagrees"):
            audit_window(packed,[observed[0],"0"],G)

    def test_hold_protocol_violation_is_detected_before_archiving(self):
        run = Scenario(G)
        run.edge(reset=True,epoch=1)
        run.edge(beat=singleton(G,18))
        run.edge(beat=singleton(G,19),report_ready=False)
        run.edge(beat=singleton(G,20))
        with self.assertRaisesRegex(ValueError,"held beat"):
            audit_window(
                [hexadecimal(item) for item in run.stimulus],
                [hexadecimal(item) for item in run.expected],G,
            )

    def test_restore_and_file_alias_constraints(self):
        image = worked_archive()
        with self.assertRaisesRegex(ValueError,"different epoch"):
            list(image.restore(image.epoch))
        with self.assertRaisesRegex(ValueError,"subscriber"):
            list(image.restore(10,image.geometry.ports))
        with TemporaryDirectory() as directory:
            existing = Path(directory)/"archive"
            existing.write_bytes(image.encode())
            alias = existing.with_name("link")
            alias.symlink_to(existing)
            with self.assertRaisesRegex(ValueError,"aliases"):
                avoid_alias(alias,(existing,))
        fields = list(Input(reset=True).words())
        fields[9] = 2
        with self.assertRaisesRegex(ValueError,"scalar"):
            unpack_input(fields,G)


if __name__ == "__main__":
    unittest.main()
