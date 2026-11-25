"""Exercise durable recovery across real modular wire boundaries and bad captures."""

import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from mold_recovery_store import ArchiveError, HALF, Identity, Intake, RecoveryArchive
from wire_codec import HEADER, MASK64, decode_packet, make_packet


SESSION = b"REPLAY0001"
REUSED = b"REPLAY0002"
IDENTITY = Identity(0, 1)
WRAPPED = MASK64 - 1
CLI = Path(__file__).with_name("mold_recovery_cli.py")


class RecoveryArchiveTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.path = Path(self.work.name) / "market.sqlite"
        self.archive = RecoveryArchive(self.path, access="rwc")
        self.addCleanup(self.archive.connection.close)
        self.archive.register(IDENTITY, SESSION, WRAPPED, record_limit=20)

    def offset(self, ordinal):
        return (WRAPPED + ordinal) & MASK64

    def wire(self, ordinal, values=(), *, declared_count=None, session=SESSION):
        return make_packet(
            session, self.offset(ordinal), values, declared_count=declared_count,
        )

    def intake(self, ordinal, values=(), *, source="A", declared_count=None):
        return self.archive.intake(
            IDENTITY, self.wire(ordinal, values, declared_count=declared_count), source,
        )

    def recover(self, ordinal, count, **options):
        raw = HEADER.pack(SESSION, self.offset(ordinal), count)
        return self.archive.retrieve(IDENTITY, raw, **options)

    def cli(self, *args, input_text=None):
        return subprocess.run(
            [sys.executable, str(CLI), str(self.path), *args], input=input_text,
            text=True, capture_output=True, check=False,
        )

    def test_wrapped_packet_and_recovery_split_between_datagrams(self):
        result = self.intake(0, (b"abc", b"efgh", b"", b"ij", b"klm"))
        self.assertEqual(result, Intake("data", 5, 0, 0))
        response = self.recover(0, 5, max_bytes=30, max_blocks=2)
        self.assertEqual((response.kind, response.count, response.next_sequence), ("complete", 5, 3))
        decoded = [decode_packet(packet) for packet in response.datagrams]
        self.assertTrue(all(len(raw) <= 30 for raw in response.datagrams))
        self.assertTrue(all(len(packet.messages) <= 2 for packet in decoded))
        self.assertEqual(
            [item for packet in decoded for item in packet.numbered_messages()],
            [(WRAPPED, b"abc"), (MASK64, b"efgh"), (0, b""), (1, b"ij"), (2, b"klm")],
        )
        self.assertEqual(self.archive.gaps(IDENTITY), ([], False))

    def test_available_prefix_stops_at_first_hole(self):
        self.intake(0, (b"zero", b"one"))
        self.intake(3, (b"three",))
        self.intake(6)
        self.assertEqual(
            self.archive.gaps(IDENTITY), ([(self.offset(2), 1), (self.offset(4), 2)], False),
        )
        self.assertEqual(self.archive.gaps(IDENTITY, limit=1), ([(self.offset(2), 1)], True))
        response = self.recover(0, 8)
        self.assertEqual((response.kind, response.count, response.next_sequence), ("partial", 2, 0))
        self.assertEqual(decode_packet(response.datagrams[0]).messages, (b"zero", b"one"))
        self.assertEqual(self.recover(2, 8).kind, "missing")

    def test_first_arrival_wins_and_alternate_sample_is_bounded(self):
        self.intake(1, (b"original", b"untouched"))
        self.assertEqual(self.intake(1, (b"original",), source="B").identical, 1)
        self.assertEqual(self.intake(1, (b"first conflict",), source="B").conflicting, 1)
        self.assertEqual(self.intake(1, (b"second conflict",), source="A").conflicting, 1)
        response = self.recover(1, 2)
        self.assertEqual(decode_packet(response.datagrams[0]).messages, (b"original", b"untouched"))
        audit = self.archive.audit(IDENTITY)
        self.assertEqual((audit.stored, audit.duplicates, audit.conflicts), (2, 1, 2))
        row = self.archive.connection.execute("SELECT body, source FROM alternate").fetchone()
        self.assertEqual(tuple(row), (b"first conflict", "B"))

    def test_epoch_and_slot_prevent_stale_context_lookup(self):
        self.intake(0, (b"old",))
        new = Identity(0, 2)
        other = Identity(1, 1)
        self.archive.register(new, REUSED, WRAPPED)
        self.archive.register(other, SESSION, WRAPPED)
        self.archive.intake(new, self.wire(0, (b"fresh",), session=REUSED), "B")
        self.archive.intake(other, self.wire(0, (b"other slot",)), "A")
        old_request = HEADER.pack(SESSION, WRAPPED, 1)
        new_request = HEADER.pack(REUSED, WRAPPED, 1)
        with self.assertRaisesRegex(ArchiveError, "session differs"):
            self.archive.retrieve(new, old_request)
        for identity, request, expected in (
            (IDENTITY, old_request, b"old"),
            (new, new_request, b"fresh"),
            (other, old_request, b"other slot"),
        ):
            reply = self.archive.retrieve(identity, request)
            self.assertEqual(decode_packet(reply.datagrams[0]).messages, (expected,))
        with self.assertRaisesRegex(ArchiveError, "not registered"):
            self.archive.retrieve(Identity(1, 2), old_request)

    def test_registration_is_idempotent_but_cannot_change_bounds(self):
        same = self.archive.register(IDENTITY, SESSION, WRAPPED, record_limit=20)
        self.assertEqual(same.seed, WRAPPED)
        for token, seed, limit in ((REUSED, WRAPPED, 20), (SESSION, 1, 20), (SESSION, WRAPPED, 2)):
            with self.subTest(token=token, seed=seed, limit=limit):
                with self.assertRaisesRegex(ArchiveError, "registered epoch cannot change"):
                    self.archive.register(IDENTITY, token, seed, record_limit=limit)
        with self.assertRaisesRegex(ArchiveError, "ten-byte"):
            self.archive.register(Identity(0, 2), b"abc", WRAPPED)

    def test_ingest_rejects_truncated_final_block_without_side_effects(self):
        invalid = self.wire(2, (b"first looks valid", b"second"))[:-2]
        with self.assertRaisesRegex(ArchiveError, "truncated block"):
            self.archive.intake(IDENTITY, invalid, "B")
        after = self.archive.audit(IDENTITY)
        self.assertEqual((after.stored, after.packets, after.advertised), (0, 0, 0))
        self.assertEqual(self.recover(2, 1).kind, "missing")

    def test_capacity_failure_rolls_back_any_prior_inserts_or_alternates(self):
        small = Identity(2, 4)
        self.archive.register(small, SESSION, WRAPPED, record_limit=2)
        self.archive.intake(small, self.wire(0, (b"original",)), "A")
        try:
            self.archive.intake(small, self.wire(0, (b"alternate", b"fits", b"too many")), "B")
        except ArchiveError as exc:
            self.assertIn("record limit", str(exc))
        else:
            self.fail("exceeding the archive capacity unexpectedly succeeded")
        after = self.archive.audit(small)
        self.assertEqual((after.stored, after.packets, after.advertised, after.conflicts), (1, 1, 1, 0))
        self.assertEqual(self.archive.connection.execute("SELECT count(*) FROM alternate").fetchone()[0], 0)
        self.assertEqual(self.archive.intake(small, self.wire(1, (b"fits",)), "B").inserted, 1)

    def test_end_of_session_accepts_delayed_inside_data_only(self):
        self.intake(0, (b"zero",))
        with self.assertRaisesRegex(ArchiveError, "contradicts"):
            self.intake(0, declared_count=65535)
        self.assertEqual(self.intake(4, declared_count=65535).kind, "end")
        self.assertEqual(self.intake(4, declared_count=65535).kind, "end")
        self.assertEqual(self.intake(2, (b"delayed",)).inserted, 1)
        with self.assertRaisesRegex(ArchiveError, "past the observed end"):
            self.intake(4, (b"late and beyond ending",))
        with self.assertRaisesRegex(ArchiveError, "past the observed end"):
            self.intake(5)
        audit = self.archive.audit(IDENTITY)
        self.assertEqual((audit.ending, audit.advertised, audit.ends, audit.stored), (4, 4, 2, 2))

    def test_retention_fence_removes_alternates_and_preserves_next_data(self):
        self.intake(0, (b"zero", b"one", b"two"))
        self.intake(0, (b"different zero",), source="B")
        self.assertEqual(self.archive.prune(IDENTITY, self.offset(1)), 1)
        self.assertEqual(self.archive.connection.execute("SELECT count(*) FROM alternate").fetchone()[0], 0)
        self.assertEqual(self.recover(0, 3).kind, "pruned")
        self.assertEqual(self.recover(1, 2).kind, "complete")
        with self.assertRaisesRegex(ArchiveError, "retention fence"):
            self.intake(0, (b"zero", b"one"))
        with self.assertRaisesRegex(ArchiveError, "forward, observed"):
            self.archive.prune(IDENTITY, self.offset(0))
        self.assertEqual(self.archive.audit(IDENTITY).retained, 1)

    def test_archive_counters_persist_and_readonly_rejects_mutation(self):
        self.intake(0, (b"durable",))
        with RecoveryArchive(self.path) as reopened:
            audit = reopened.audit(IDENTITY)
            self.assertEqual((audit.packets, audit.stored), (1, 1))
            with self.assertRaisesRegex(ArchiveError, "read-only"):
                reopened.intake(IDENTITY, self.wire(1, (b"read-only",)), "A")
        unrelated = Path(self.work.name) / "other.sqlite"
        with sqlite3.connect(unrelated) as connection:
            connection.execute("CREATE TABLE private (id)")
        with self.assertRaisesRegex(ArchiveError, "unrelated schema"):
            RecoveryArchive(unrelated, access="rwc")

    def test_wire_horizon_and_invalid_request_limits_are_rejected(self):
        for raw in (b"", HEADER.pack(SESSION, WRAPPED, 0), self.wire(0, (b"garbage",))):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ArchiveError, "20 bytes|zero count"):
                    self.archive.retrieve(IDENTITY, raw)
        with self.assertRaisesRegex(ArchiveError, "outside the configured epoch horizon"):
            self.intake(HALF, (b"ambiguous",))
        with self.assertRaisesRegex(ArchiveError, "extends past"):
            self.intake(HALF - 1, (b"outside range",))
        with self.assertRaisesRegex(ArchiveError, "configured cap"):
            self.recover(0, 9, request_cap=8)
        with self.assertRaisesRegex(ArchiveError, "UDP bounds"):
            self.recover(0, 1, max_bytes=19)
        self.intake(0, (b"too many bytes for a tiny packet",))
        with self.assertRaisesRegex(ArchiveError, "cannot fit"):
            self.recover(0, 1, max_bytes=23)

    def test_audit_catches_metadata_mismatch(self):
        self.intake(2, (b"entry",))
        self.archive.connection.execute("UPDATE contexts SET stored=4")
        with self.assertRaisesRegex(ArchiveError, "stored messages disagree"):
            self.archive.audit(IDENTITY)

    def test_cli_streaming_ingest_reply_audit_and_bad_input(self):
        items = (
            {"slot": 0, "epoch": 1, "source": "A", "wire": self.wire(0, (b"via CLI",)).hex()},
            {"slot": 0, "epoch": 1, "source": "B", "wire": self.wire(3).hex()},
        )
        ingested = self.cli("ingest", "-", input_text="\n".join(map(json.dumps, items)))
        self.assertEqual(ingested.returncode, 0, ingested.stderr)
        self.assertEqual([json.loads(line)["kind"] for line in ingested.stdout.splitlines()], ["data", "heartbeat"])
        reply = self.cli("reply", HEADER.pack(SESSION, WRAPPED, 3).hex(), "--slot", "0", "--epoch", "1")
        self.assertEqual(reply.returncode, 0, reply.stderr)
        encoded = json.loads(reply.stdout)
        self.assertEqual((encoded["kind"], encoded["count"]), ("partial", 1))
        self.assertEqual(decode_packet(bytes.fromhex(encoded["datagrams"][0])).messages, (b"via CLI",))
        inspected = self.cli("audit", "--slot", "0", "--epoch", "1")
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout)["missing_runs"], [[MASK64, 2]])
        invalid = self.cli("ingest", "-", input_text="\n[]")
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("line 2", invalid.stderr)


class SeededReconciliationTest(unittest.TestCase):
    def test_archive_replay_prefixes_match_an_independent_map(self):
        rng = random.Random(0x8ee00b)
        expected = {}
        duplicate = conflict = 0
        with tempfile.TemporaryDirectory() as directory:
            with RecoveryArchive(Path(directory) / "seeded.sqlite", access="rwc") as archive:
                seed = MASK64 - 4
                archive.register(IDENTITY, SESSION, seed)
                for _ in range(280):
                    offset = rng.randrange(130)
                    bodies = [
                        bytes(rng.randrange(256) for _ in range(rng.randrange(8)))
                        for _ in range(rng.randrange(1, 5))
                    ]
                    if (existing := expected.get(offset)) is not None and rng.randrange(2):
                        bodies[0] = existing
                    outcome = archive.intake(
                        IDENTITY, make_packet(SESSION, seed + offset, bodies), rng.choice(("A", "B")),
                    )
                    reference = [0, 0, 0]
                    for ordinal, body in enumerate(bodies, offset):
                        if ordinal not in expected:
                            expected[ordinal] = body
                            reference[0] += 1
                        elif expected[ordinal] == body:
                            reference[1] += 1
                        else:
                            reference[2] += 1
                    self.assertEqual((outcome.inserted, outcome.identical, outcome.conflicting), tuple(reference))
                    duplicate += reference[1]
                    conflict += reference[2]
                for _ in range(100):
                    offset, count = rng.randrange(134), rng.randrange(1, 15)
                    wanted = []
                    for ordinal in range(offset, offset + count):
                        if ordinal not in expected:
                            break
                        wanted.append(((seed + ordinal) & MASK64, expected[ordinal]))
                    reply = archive.retrieve(IDENTITY, HEADER.pack(SESSION, (seed+offset)&MASK64, count),
                                             max_bytes=42, max_blocks=3)
                    packets = [decode_packet(wire) for wire in reply.datagrams]
                    actual = [entry for packet in packets for entry in packet.numbered_messages()]
                    self.assertEqual(actual, wanted)
                    self.assertTrue(all(len(wire) <= 42 for wire in reply.datagrams))
                audit = archive.audit(IDENTITY)
                self.assertEqual((audit.stored, audit.duplicates, audit.conflicts),
                                 (len(expected), duplicate, conflict))


if __name__ == "__main__":
    unittest.main()
