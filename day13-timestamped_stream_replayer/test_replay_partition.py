"""Evaluate the host's split bundle against independent packet and record boundaries."""

from dataclasses import replace
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
import json
import unittest

from replay_partition import Partition, join_partitions, partition_tape, read_bundle, write_bundle
from replay_program import Packet, ReplayError, Tape, U32


def example():
    """Build five packets that exercise exact fits, partial fits, and equal-time boundaries."""
    return Tape.from_packets((
        Packet.paced(9, b"abcdefgh!", 9, 3),
        Packet.paced(10, b"XYZ", 12),
        Packet.paced(9, bytes(range(17)), 20, 2),
        Packet.paced(0, b"X", 25),
        Packet.paced(8, b"another tail", 30, 1),
    ))


class PartitionTests(unittest.TestCase):
    """Verify greedy complete-packet placement without changing payload or global time."""

    def test_greedy_bounded_roundtrip(self):
        """Restore exact input order, tags, and every offset across multiple banks."""
        source = example()
        parts = partition_tape(source, 3)
        self.assertEqual([part.index for part in parts], [0, 1, 2])
        self.assertEqual([part.origin for part in parts], [9,20,25])
        self.assertEqual([[event.tick for event in part.tape.events] for part in parts],
                         [[0,3,3], [0,2,4], [0,5,6]])
        self.assertEqual([part.basename for part in parts],
                         ["part-0000.trp", "part-0001.trp", "part-0002.trp"])
        self.assertEqual(join_partitions(parts), source)
        self.assertEqual([sum(beat.last for beat in part.tape.events) for part in parts], [2, 1, 2])

    def test_nonrebase_and_empty_are_explicit_modes(self):
        """Keep due times as programmed with rebasing disabled and label an empty tape."""
        source = example()
        parts = partition_tape(source, 6, shift=False)
        self.assertEqual([part.origin for part in parts], [0,0])
        self.assertEqual([part.tape.duration for part in parts], [24,31])
        self.assertEqual(join_partitions(parts), source)
        self.assertEqual(partition_tape(Tape(()), 3), ())
        self.assertEqual(join_partitions(()), Tape(()))

    def test_rejects_invalid_ranges_and_oversized_packets(self):
        """Splitting never creates a partially runnable packet."""
        for width in (0, -1, U32+1):
            with self.subTest(width=width):
                with self.assertRaisesRegex(ReplayError, "depth"):
                    partition_tape(example(), width)
        with self.assertRaisesRegex(ReplayError, "packet 2 needs 3"):
            partition_tape(example(), 2)
        with self.assertRaisesRegex(ReplayError, "Boolean"):
            partition_tape(example(), 4, shift=0)

    def test_out_of_sequence_or_corrupted_partition_fails(self):
        """Restore schedule only when filenames, indices, and original offsets agree."""
        a, b, c = partition_tape(example(), 3)
        with self.assertRaisesRegex(ReplayError, "expected part index 0"):
            join_partitions((b, a, c))
        with self.assertRaisesRegex(ReplayError, "chronological"):
            join_partitions((a, replace(b, origin=1)))
        with self.assertRaisesRegex(ReplayError, "empty"):
            join_partitions((Partition(0, 0, Tape(())),))
        with self.assertRaisesRegex(ReplayError, "tick"):
            join_partitions((a, replace(b, origin=U32)))

    def test_random_partitions_conserve_every_packet_and_fit_capacity(self):
        """Explore varied record depths and packet sizes, including many equal ticks."""
        rng = Random(13013)
        for check in range(100):
            depth = rng.randrange(1,24)
            clock = rng.randrange(20)
            packets = []
            for index in range(rng.randrange(0,25)):
                count = rng.randrange(1,depth*8+1)
                data = bytes(rng.randrange(256) for _ in range(count))
                packet = Packet.paced(index,data,clock,rng.randrange(0,4))
                clock = packet.end+rng.randrange(0,12)
                packets.append(packet)
            source = Tape.from_packets(packets)
            with self.subTest(case=check, depth=depth):
                partitions = partition_tape(source, depth, shift=bool(check%2))
                restored = join_partitions(partitions)
                self.assertEqual(tuple(restored.packets()), tuple(packets))
                for part in partitions:
                    self.assertTrue(0 < len(part.tape.events) <= depth)
                for first, second in zip(partitions, partitions[1:]):
                    first_next = next(second.tape.packets())
                    self.assertGreater(len(first.tape.events) + len(first_next.ticks), depth)

    def test_bundle_checks_files_and_frozen_totals(self):
        """A valid directory contains only canonical filenames and complete metadata."""
        with TemporaryDirectory() as temp:
            base = Path(temp) / "recording"
            report = write_bundle(base, example(), 3)
            self.assertEqual(report["total_beats"], 9)
            self.assertEqual(report["total_bytes"], 42)
            self.assertEqual(read_bundle(base), example())
            self.assertEqual(sorted(path.name for path in base.iterdir()),
                             ["manifest.json", "part-0000.trp", "part-0001.trp", "part-0002.trp"])
            with self.assertRaisesRegex(ReplayError, "new directory"):
                write_bundle(base, example(), 3)
            bytes_path = base / "part-0001.trp"
            bytes_path.write_bytes(bytes_path.read_bytes() + b"extra")
            with self.assertRaisesRegex(ReplayError, "SHA-256"):
                read_bundle(base)

    def test_manifest_path_length_offsets_and_version_are_validated(self):
        """Reject stale metadata and attempts to use another file as a tape part."""
        variations = (
            ("version", lambda doc: doc.update(version=3), "unsupported"),
            ("total_beats", lambda doc: doc.update(total_beats=3), "totals"),
            ("declared depth", lambda doc: doc.update(depth=1), "capacity"),
            ("part path", lambda doc: doc["records"][0].update(file="../else.trp"), "canonical"),
            ("part length", lambda doc: doc["records"][0].update(beats=99), "metadata"),
            ("part origin", lambda doc: doc["records"][0].update(origin=-5), "origin"),
            ("shift disagreement", lambda doc: doc.update(rebase=False), "origin zero"),
        )
        with TemporaryDirectory() as temp:
            base = Path(temp) / "recording"
            reference = write_bundle(base, example(), 3)
            for label, update, problem in variations:
                with self.subTest(label=label):
                    edit = json.loads(json.dumps(reference))
                    update(edit)
                    (base/"manifest.json").write_text(json.dumps(edit))
                    with self.assertRaisesRegex(ReplayError, problem):
                        read_bundle(base)

    def test_existing_symlink_and_failed_split_do_not_create_any_bundle(self):
        """Leave other paths unchanged when a destination aliases another directory."""
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            destination = folder/"bundle"
            with self.assertRaises(ReplayError):
                write_bundle(destination, example(), 2)
            self.assertFalse(destination.exists())
            destination.symlink_to(folder/"missing")
            with self.assertRaisesRegex(ReplayError, "new directory"):
                write_bundle(destination, example(), 3)
            self.assertEqual(destination.readlink(), folder/"missing")


    def test_empty_bundle_preserves_a_zero_record_manifest(self):
        """A verified empty program remains valid without allocating an empty part."""
        with TemporaryDirectory() as temp:
            base = Path(temp) / "empty"
            result = write_bundle(base, Tape(()), 2)
            self.assertEqual(result["records"], [])
            self.assertEqual(result["total_bytes"], 0)
            self.assertEqual(result["total_beats"], 0)
            self.assertEqual(read_bundle(base), Tape(()))
            self.assertEqual([path.name for path in base.iterdir()], ["manifest.json"])

    def test_partition_with_same_tick_boundary_preserves_full_packet_order(self):
        """Inclusive offsets allow equal tick across two adjacent image banks."""
        original = Tape.from_packets(
            (Packet.paced(5, b"x"*21, 6, 4), Packet.paced(5,b"y"*12,14,2))
        )
        parts = partition_tape(original, 3)
        self.assertEqual(parts[0].absolute().duration, 14)
        self.assertEqual(parts[1].absolute().events[0].tick, 14)
        self.assertEqual(parts[1].tape.events[0].tick, 0)
        self.assertTrue(parts[0].tape.events[-1].last)
        self.assertTrue(parts[1].tape.events[-1].last)
        self.assertEqual(join_partitions(parts), original)

if __name__ == "__main__":
    unittest.main()
