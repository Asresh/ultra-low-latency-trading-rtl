"""Transactional, offline MoldUDP64 recovery storage for a slot and epoch."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Optional

from wire_codec import HEADER, LENGTH, MASK64, decode_packet, make_packet


APPLICATION_ID = 0x4D4F4C44
VERSION = 1
HALF = 1 << 63
MAX_DOWNSTREAM_BLOCKS = 65534
MAX_RECOVERY_REQUEST = 65535
UDP_PAYLOAD_MAX = 65507
SCHEMA = (
    """CREATE TABLE contexts (
        slot INTEGER NOT NULL, epoch INTEGER NOT NULL, session BLOB NOT NULL,
        seed BLOB NOT NULL, record_limit INTEGER NOT NULL,
        retained INTEGER NOT NULL DEFAULT 0, advertised INTEGER NOT NULL DEFAULT 0,
        ending INTEGER, stored INTEGER NOT NULL DEFAULT 0,
        packets INTEGER NOT NULL DEFAULT 0, heartbeats INTEGER NOT NULL DEFAULT 0,
        ends INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
        conflicts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (slot, epoch)
    ) WITHOUT ROWID""",
    """CREATE TABLE messages (
        slot INTEGER NOT NULL, epoch INTEGER NOT NULL, ordinal INTEGER NOT NULL,
        body BLOB NOT NULL, source TEXT NOT NULL,
        PRIMARY KEY (slot, epoch, ordinal),
        FOREIGN KEY (slot, epoch) REFERENCES contexts (slot, epoch)
    ) WITHOUT ROWID""",
    """CREATE TABLE alternate (
        slot INTEGER NOT NULL, epoch INTEGER NOT NULL, ordinal INTEGER NOT NULL,
        body BLOB NOT NULL, source TEXT NOT NULL,
        PRIMARY KEY (slot, epoch, ordinal),
        FOREIGN KEY (slot, epoch, ordinal) REFERENCES messages (slot, epoch, ordinal)
            ON DELETE CASCADE
    ) WITHOUT ROWID""",
)


class ArchiveError(ValueError):
    """An operation is incompatible with the archive or its selected epoch."""


def unsigned(value, maximum, label):
    if not isinstance(value, int) or not 0 <= value <= maximum:
        raise ArchiveError(f"{label} must be between 0 and {maximum}")
    return value


@dataclass(frozen=True)
class Identity:
    slot: int
    epoch: int

    def __post_init__(self):
        unsigned(self.slot, 65535, "slot")
        unsigned(self.epoch, (1 << 32) - 1, "epoch")

    def sql(self):
        return self.slot, self.epoch


@dataclass(frozen=True)
class Request:
    session: bytes
    first: int
    count: int

    @classmethod
    def from_bytes(cls, data):
        if len(data) != HEADER.size:
            raise ArchiveError("recovery request is not exactly 20 bytes")
        session, first, count = HEADER.unpack(data)
        if count == 0:
            raise ArchiveError("recovery request has zero count")
        return cls(session, first, count)


@dataclass(frozen=True)
class Context:
    identity: Identity
    session: bytes
    seed: int
    record_limit: int
    retained: int
    advertised: int
    ending: Optional[int]
    stored: int
    packets: int
    heartbeats: int
    ends: int
    duplicates: int
    conflicts: int

    @classmethod
    def from_row(cls, identity, row):
        return cls(
            identity, row["session"], int.from_bytes(row["seed"], "big"), row["record_limit"],
            row["retained"], row["advertised"], row["ending"], row["stored"],
            row["packets"], row["heartbeats"], row["ends"], row["duplicates"], row["conflicts"],
        )

    def sequence(self, ordinal):
        return (self.seed + ordinal) & MASK64

    def ordinal(self, sequence):
        """Map the declared epoch onto the unambiguous half of the sequence ring."""
        unsigned(sequence, MASK64, "wire sequence")
        delta = (sequence - self.seed) & MASK64
        if delta >= HALF:
            raise ArchiveError("wire sequence is outside the configured epoch horizon")
        return delta


@dataclass(frozen=True)
class Intake:
    kind: str
    inserted: int
    identical: int
    conflicting: int


@dataclass(frozen=True)
class Recovery:
    kind: str
    count: int
    next_sequence: int
    datagrams: tuple


def valid_session(session):
    if not isinstance(session, bytes) or len(session) != 10:
        raise ArchiveError("session must be a ten-byte wire token")


def packet_limit(size):
    if not isinstance(size, int) or not HEADER.size + LENGTH.size <= size <= UDP_PAYLOAD_MAX:
        raise ArchiveError("datagram byte limit is outside the supported UDP bounds")
    return size


def reply_limits(max_bytes, max_blocks):
    """Validate the bounds used to form independently valid data packets."""
    packet_limit(max_bytes)
    unsigned(max_blocks, MAX_DOWNSTREAM_BLOCKS, "downstream block limit")
    if not max_blocks:
        raise ArchiveError("downstream block limit must be positive")


def packet_groups(records, max_bytes, max_blocks):
    """Place a contiguous message prefix into independently valid data packets."""
    reply_limits(max_bytes, max_blocks)
    packet, used = [], HEADER.size
    for body in records:
        needed = LENGTH.size + len(body)
        if needed + HEADER.size > max_bytes:
            raise ArchiveError("an archived message cannot fit the requested datagram size")
        if packet and (used + needed > max_bytes or len(packet) == max_blocks):
            yield tuple(packet)
            packet, used = [], HEADER.size
        packet.append(body)
        used += needed
    if packet:
        yield tuple(packet)


class RecoveryArchive:
    """A database where accepted packets, retained rows and telemetry commit together."""

    def __init__(self, path, *, access="ro"):
        if access not in ("ro", "rw", "rwc"):
            raise ValueError("invalid archive access mode")
        self.writeable = access != "ro"
        uri = Path(path).absolute().as_uri() + f"?mode={access}"
        self.connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            # Existing databases cannot silently become an archive. Initialize
            # while holding the write lock so concurrent first opens agree.
            with self.transaction(write=access == "rwc"):
                app = self.connection.execute("PRAGMA application_id").fetchone()[0]
                version = self.connection.execute("PRAGMA user_version").fetchone()[0]
                if app == APPLICATION_ID and version == VERSION:
                    return
                if app != 0 or version != 0 or access != "rwc":
                    raise ArchiveError("file is not a compatible MoldUDP64 archive")
                if self.connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                    raise ArchiveError("file is an existing database with an unrelated schema")
                for ddl in SCHEMA:
                    self.connection.execute(ddl)
                self.connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                self.connection.execute(f"PRAGMA user_version = {VERSION}")
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.connection.close()

    @contextmanager
    def transaction(self, *, write=False):
        if write and not self.writeable:
            raise ArchiveError("archive is read-only")
        self.connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def context(self, identity):
        row = self.connection.execute(
            "SELECT * FROM contexts WHERE slot = ? AND epoch = ?", identity.sql(),
        ).fetchone()
        if row is None:
            raise ArchiveError(f"slot {identity.slot}, epoch {identity.epoch} is not registered")
        return Context.from_row(identity, row)

    def register(self, identity, session, seed, *, record_limit=1_000_000):
        valid_session(session)
        unsigned(seed, MASK64, "seed")
        unsigned(record_limit, HALF - 1, "record limit")
        if not record_limit:
            raise ArchiveError("record limit must be positive")
        with self.transaction(write=True):
            self.connection.execute(
                """INSERT INTO contexts (slot, epoch, session, seed, record_limit)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                (*identity.sql(), session, seed.to_bytes(8, "big"), record_limit),
            )
            context = self.context(identity)
            if (context.session, context.seed, context.record_limit) != (session, seed, record_limit):
                raise ArchiveError("registered epoch cannot change session, seed or record limit")
            return context

    @staticmethod
    def decode_datagram(data, max_bytes, max_message, max_blocks):
        if not isinstance(data, bytes):
            raise ArchiveError("captured datagram must be a byte string")
        packet_limit(max_bytes)
        unsigned(max_message, 65535, "message limit")
        unsigned(max_blocks, MAX_DOWNSTREAM_BLOCKS, "block limit")
        if not max_blocks:
            raise ArchiveError("block limit must be positive")
        try:
            return decode_packet(data, max_bytes, max_message, max_blocks)
        except ValueError as exc:
            raise ArchiveError(f"invalid downstream packet: {exc}") from exc

    def intake(
        self, identity, data, source, *, max_bytes=1536, max_message=64, max_blocks=64,
    ):
        """Validate complete captured bytes before modifying a registered epoch."""
        try:
            source_length = len(source.encode("utf-8")) if isinstance(source, str) else 0
        except UnicodeError as exc:
            raise ArchiveError("capture source must be valid UTF-8") from exc
        if not 1 <= source_length <= 64:
            raise ArchiveError("capture source must be one to 64 UTF-8 bytes")
        packet = self.decode_datagram(data, max_bytes, max_message, max_blocks)
        with self.transaction(write=True):
            context = self.context(identity)
            if packet.session != context.session:
                raise ArchiveError("captured packet session differs from the registered token")
            start = context.ordinal(packet.first)
            exclusive = start + len(packet.messages)
            if exclusive >= HALF:
                raise ArchiveError("datagram extends past the configured epoch horizon")
            if start < context.retained:
                raise ArchiveError("packet begins before the archive's retention fence")
            if context.ending is not None and exclusive > context.ending:
                raise ArchiveError("packet extends past the observed end-of-session")
            if packet.is_end and (start < context.advertised or
                                  (context.ending is not None and start != context.ending)):
                raise ArchiveError("end-of-session contradicts existing archive evidence")
            outcome = self._store_packet(identity, context, start, packet, source)
            advertised = max(exclusive, context.advertised)
            ending = start if packet.is_end else context.ending
            self.connection.execute(
                """UPDATE contexts SET advertised=?, ending=?, stored=?,
                          packets=?, heartbeats=?, ends=?, duplicates=?, conflicts=?
                   WHERE slot=? AND epoch=?""",
                (
                    advertised, ending, context.stored + outcome.inserted,
                    self._add_count(context.packets, 1),
                    self._add_count(context.heartbeats, packet.is_heartbeat),
                    self._add_count(context.ends, packet.is_end),
                    self._add_count(context.duplicates, outcome.identical),
                    self._add_count(context.conflicts, outcome.conflicting), *identity.sql(),
                ),
            )
            return outcome

    @staticmethod
    def _add_count(value, amount):
        return min(HALF - 1, value + amount)

    def _store_packet(self, identity, context, start, packet, source):
        inserted = identical = conflicting = 0
        for ordinal, body in enumerate(packet.messages, start):
            key = (*identity.sql(), ordinal)
            original = self.connection.execute(
                "SELECT body FROM messages WHERE slot = ? AND epoch = ? AND ordinal = ?", key,
            ).fetchone()
            if original is None:
                if context.stored + inserted >= context.record_limit:
                    raise ArchiveError("record limit would be exceeded; packet not archived")
                self.connection.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", (*key, body, source))
                inserted += 1
            elif original["body"] == body:
                identical += 1
            else:
                # Keep one alternate sample per sequence. Conflicts cannot
                # multiply the retained archive's row count without bound.
                self.connection.execute(
                    "INSERT INTO alternate VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (*key, body, source),
                )
                conflicting += 1
        kind = "end" if packet.is_end else "heartbeat" if packet.is_heartbeat else "data"
        return Intake(kind, inserted, identical, conflicting)

    def retrieve(self, identity, data, *, max_bytes=1536, max_blocks=64, request_cap=1024):
        """Return only the available prefix of the requested sequence interval."""
        reply_limits(max_bytes, max_blocks)
        unsigned(request_cap, MAX_RECOVERY_REQUEST, "request cap")
        request = Request.from_bytes(data)
        if request.count > request_cap:
            raise ArchiveError("request exceeds the configured cap")
        with self.transaction():
            context = self.context(identity)
            if request.session != context.session:
                raise ArchiveError("request session differs from the registered token")
            start = context.ordinal(request.first)
            if start + request.count >= HALF:
                raise ArchiveError("requested interval exceeds the epoch horizon")
            if start < context.retained:
                return Recovery("pruned", 0, request.first, ())
            offset, records = start, []
            for ordinal, body in self.connection.execute(
                """SELECT ordinal, body FROM messages
                   WHERE slot=? AND epoch=? AND ordinal >= ? AND ordinal < ?
                   ORDER BY ordinal""",
                (*identity.sql(), start, start + request.count),
            ):
                if ordinal != offset:
                    break
                offset += 1
                records.append(body)
            if not records:
                return Recovery("missing", 0, request.first, ())
            next_packet, datagrams = start, []
            for group in packet_groups(records, max_bytes, max_blocks):
                datagrams.append(make_packet(context.session, context.sequence(next_packet), group))
                next_packet += len(group)
            status = "complete" if offset == start + request.count else "partial"
            return Recovery(status, len(records), context.sequence(offset), tuple(datagrams))

    def prune(self, identity, keep_from):
        """Advance an irrevocable retention boundary and delete earlier observations."""
        with self.transaction(write=True):
            context = self.context(identity)
            floor = context.ordinal(keep_from)
            if not context.retained <= floor <= context.advertised:
                raise ArchiveError("retention boundary is not a forward, observed position")
            removed = self.connection.execute(
                "DELETE FROM messages WHERE slot=? AND epoch=? AND ordinal < ?",
                (*identity.sql(), floor),
            ).rowcount
            self.connection.execute(
                "UPDATE contexts SET retained=?, stored=? WHERE slot=? AND epoch=?",
                (floor, context.stored - removed, *identity.sql()),
            )
            return removed

    def gaps(self, identity, *, limit=100):
        """Summarize missing runs relative to the observed exclusive watermark."""
        with self.transaction():
            return self._missing_runs(self.context(identity), limit)

    def _missing_runs(self, context, limit):
        unsigned(limit, 100000, "gap limit")
        if limit == 0:
            raise ArchiveError("gap limit must be positive")
        offset, missing = context.retained, []
        for ordinal, in self.connection.execute(
            "SELECT ordinal FROM messages WHERE slot = ? AND epoch = ? ORDER BY ordinal",
            context.identity.sql(),
        ):
            if offset < ordinal:
                if len(missing) == limit:
                    return missing, True
                missing.append((context.sequence(offset), ordinal - offset))
            offset = ordinal + 1
        if offset < context.advertised:
            if len(missing) == limit:
                return missing, True
            missing.append((context.sequence(offset), context.advertised - offset))
        return missing, False

    def audit(self, identity):
        """Verify storage integrity, provenance bounds and each context's accounting."""
        with self.transaction():
            context = self.context(identity)
            self._check_integrity(context)
            return context

    def inspect(self, identity, *, limit=100):
        """Return validated metadata and gaps from one coherent database snapshot."""
        with self.transaction():
            context = self.context(identity)
            self._check_integrity(context)
            gaps, truncated = self._missing_runs(context, limit)
            return context, gaps, truncated

    def _check_integrity(self, context):
        integrity = self.connection.execute("PRAGMA quick_check").fetchall()
        if [row[0] for row in integrity] != ["ok"]:
            raise ArchiveError("SQLite quick_check detected archive corruption")
        if self.connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ArchiveError("archive contains an orphaned record")
        stats = self.connection.execute(
            "SELECT count(*), min(ordinal), max(ordinal) FROM messages WHERE slot=? AND epoch=?",
            context.identity.sql(),
        ).fetchone()
        count, minimum, maximum = stats
        if (count != context.stored or count > context.record_limit or
            (count > 0 and not context.retained <= minimum <= maximum < context.advertised) or
            not context.retained <= context.advertised < HALF or
            (context.ending is not None and context.ending != context.advertised)):
            raise ArchiveError("archive boundaries and stored messages disagree")
