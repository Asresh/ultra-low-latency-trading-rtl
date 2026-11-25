"""Durable submission and completion journal for one live Day 08 queue."""

from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
from hashlib import sha256
from hmac import compare_digest
import os
from pathlib import Path
from struct import Struct
import tempfile
from uuid import UUID

from dma_protocol import (
    Completion, Descriptor, Opcode, RECORD_BYTES, Ring, Status, U64_MASK,
    add_pointer, bounded, distance,
)


JOURNAL_MAGIC = b"DMAJNL1\0"
CAPTURE_MAGIC = b"DMACAP1\0"
IDENTITY = Struct("<16sB3xIIQQ")
COUNTERS = Struct("<QQQQ")  # prepared, observed publication, observed release, archived
CURSORS = Struct("<IIIIBB")
HASH_BYTES = sha256().digest_size
ARCHIVE_DIGESTS = (Status.OK, Status.CRC_ERROR, Status.WRITE_ERROR, Status.VERIFY_ERROR)


class JournalError(ValueError):
    """The on-disk image or observed live queue is inconsistent."""


@dataclass(frozen=True)
class QueueIdentity:
    generation: UUID
    number: int
    seed: int
    depth: int
    sq_base: int
    cq_base: int

    def __post_init__(self):
        if not isinstance(self.generation, UUID) or self.generation.int == 0:
            raise JournalError("queue requires a nonzero incarnation UUID")
        bounded(self.number, 8, "queue number")
        bounded(self.seed, 32, "queue seed")
        if self.sq.region.overlaps(self.cq.region):
            raise JournalError("submission and completion regions overlap")

    @property
    def sq(self):
        return Ring(self.sq_base, self.depth)

    @property
    def cq(self):
        return Ring(self.cq_base, self.depth)

    def pointer(self, ordinal):
        return add_pointer(self.seed, ordinal)

    def encode(self):
        return IDENTITY.pack(
            self.generation.bytes, self.number, self.seed,
            self.depth, self.sq_base, self.cq_base,
        )

    @classmethod
    def decode(cls, data):
        raw, *fields = IDENTITY.unpack(data)
        value = cls(UUID(bytes=raw), *fields)
        if value.encode() != data:
            raise JournalError("reserved identity fields must be zero")
        return value


def seal(magic, contents):
    image = magic + contents
    return image + sha256(image).digest()


def unseal(magic, data, minimum):
    if len(data) < len(magic) + minimum + HASH_BYTES or not data.startswith(magic):
        raise JournalError("invalid or truncated image header")
    image, digest = data[:-HASH_BYTES], data[-HASH_BYTES:]
    if not compare_digest(sha256(image).digest(), digest):
        raise JournalError("image digest mismatch")
    return image[len(magic):]


@dataclass(frozen=True)
class Snapshot:
    identity: QueueIdentity
    sq_head: int
    sq_tail: int
    cq_head: int
    cq_tail: int
    sq_image: bytes
    cq_image: bytes
    fatal: int = 0
    enabled: bool = True

    def __post_init__(self):
        for value in (self.sq_head, self.sq_tail, self.cq_head, self.cq_tail):
            bounded(value, 32, "live ring cursor")
        bounded(self.fatal, 2, "live fatal code")
        if not isinstance(self.enabled, bool):
            raise JournalError("enabled flag must be boolean")
        size = self.identity.depth * RECORD_BYTES
        if len(self.sq_image) != size or len(self.cq_image) != size:
            raise JournalError("each ring image must contain every physical slot")

    def encode(self):
        cursors = CURSORS.pack(
            self.sq_head, self.sq_tail, self.cq_head, self.cq_tail, self.fatal, self.enabled,
        )
        return seal(CAPTURE_MAGIC, self.identity.encode() + cursors + self.sq_image + self.cq_image)

    @classmethod
    def decode(cls, data):
        start = IDENTITY.size
        ring_start = start + CURSORS.size
        image = unseal(CAPTURE_MAGIC, data, ring_start)
        identity = QueueIdentity.decode(image[:start])
        sq_end = ring_start + identity.depth * RECORD_BYTES
        sq_head, sq_tail, cq_head, cq_tail, fatal, enabled = CURSORS.unpack(image[start:ring_start])
        if enabled not in (0, 1):
            raise JournalError("enabled flag must be boolean")
        return cls(
            identity, sq_head, sq_tail, cq_head, cq_tail,
            image[ring_start:sq_end], image[sq_end:], fatal, bool(enabled),
        )

    def record(self, ring_image, ordinal):
        slot = self.identity.pointer(ordinal) & (self.identity.depth - 1)
        begin = slot * RECORD_BYTES
        return ring_image[begin:begin + RECORD_BYTES]


def rows(data):
    if len(data) % RECORD_BYTES:
        raise JournalError("torn ring record")
    return tuple(data[i:i + RECORD_BYTES] for i in range(0, len(data), RECORD_BYTES))


def validate_completion(submission, completion, queue):
    descriptor = Descriptor.decode(submission)
    result = Completion.decode(completion)
    expected = (descriptor.op, descriptor.tag, descriptor.cookie)
    observed = (result.op, result.tag, result.cookie)
    # A failed descriptor fetch retires with the decoded identity cleared.
    zero_fetch = result.status == Status.READ_ERROR and observed == (0, 0, 0)
    if result.queue != queue or (observed != expected and not zero_fetch):
        raise JournalError("completion does not identify its prepared submission")
    if result.status == Status.OK:
        if result.length != descriptor.length:
            raise JournalError("success completion length differs from its submission")
        if descriptor.op == Opcode.FENCE and result.crc:
            raise JournalError("FENCE completion contains a payload digest")
        if descriptor.check_crc and result.crc != descriptor.expected_crc:
            raise JournalError("success completion digest differs from its checked submission")
    if result.status not in ARCHIVE_DIGESTS and result.crc:
        raise JournalError("completion status cannot carry a digest")
    return result


@dataclass(frozen=True)
class SubmissionWrite:
    ordinal: int
    address: int
    record: bytes


@dataclass(frozen=True)
class ResumePlan:
    identity: QueueIdentity
    observed_sq_tail: int
    observed_cq_head: int
    submit_tail: int
    release_head: int
    writes: tuple[SubmissionWrite, ...]
    completed_begin: int
    completed_end: int

    @property
    def has_doorbell(self):
        return (self.observed_sq_tail, self.observed_cq_head) != (self.submit_tail, self.release_head)


@dataclass(frozen=True)
class Journal:
    identity: QueueIdentity
    submissions: tuple[bytes, ...] = ()
    completions: tuple[bytes, ...] = ()
    published: int = 0
    released: int = 0

    def __post_init__(self):
        archived = len(self.completions)
        if not 0 <= self.released <= archived <= self.published <= len(self.submissions) <= U64_MASK:
            raise JournalError("journal cursor ordering is impossible")
        depth = self.identity.depth
        if archived - self.released > depth or self.published - archived > depth:
            raise JournalError("journal window exceeds physical ring depth")

    def prepare(self, begin, records):
        if begin < 0:
            raise JournalError("prepare ordinal must be nonnegative")
        batch = tuple(bytes(record) for record in records)
        for record in batch:
            Descriptor.decode(record)
        size = len(self.submissions)
        if begin == size:
            return replace(self, submissions=self.submissions + batch)
        if begin < size and begin + len(batch) <= size:
            if self.submissions[begin:begin + len(batch)] == batch:
                return self
            raise JournalError("prepare replay conflicts with existing durable bytes")
        raise JournalError("prepare must append contiguously or retry an exact prior range")

    def encode(self):
        counts = COUNTERS.pack(len(self.submissions), self.published, self.released, len(self.completions))
        records = b"".join((*self.submissions, *self.completions))
        return seal(JOURNAL_MAGIC, self.identity.encode() + counts + records)

    @classmethod
    def decode(cls, payload):
        start = IDENTITY.size
        records_start = start + COUNTERS.size
        data = unseal(JOURNAL_MAGIC, payload, records_start)
        identity = QueueIdentity.decode(data[:start])
        prepared, published, released, archived = COUNTERS.unpack(data[start:records_start])
        if len(data) != records_start + (prepared + archived) * RECORD_BYTES:
            raise JournalError("journal image record count differs from its header")
        records = rows(data[records_start:])
        value = cls(identity, records[:prepared], records[prepared:], published, released)
        for submitted, completed in zip(value.submissions, value.completions):
            validate_completion(submitted, completed, identity.number)
        return value

    def advance(self, ordinal, cursor, name):
        increment = distance(cursor, self.identity.pointer(ordinal))
        if increment > self.identity.depth or ordinal + increment > U64_MASK:
            raise JournalError(f"{name} moved backward or more than one ring window")
        return ordinal + increment

    def reconcile(self, snapshot):
        """Verify a coherent live sample and propose actions from recoverable state."""
        if snapshot.identity != self.identity:
            raise JournalError("snapshot names a different queue incarnation or memory mapping")
        if not snapshot.enabled:
            raise JournalError("queue is disabled")
        if snapshot.fatal:
            raise JournalError("device reported a fatal stop, inspect side effects before recovery")
        if snapshot.sq_head != snapshot.cq_tail:
            raise JournalError("SQ head and CQ tail did not commit on the same edge")

        archived = len(self.completions)
        published = self.advance(self.published, snapshot.sq_tail, "SQ tail")
        retired = self.advance(archived, snapshot.cq_tail, "CQ tail")
        released = self.advance(self.released, snapshot.cq_head, "CQ head")
        depth = self.identity.depth
        if not released <= archived <= retired <= published <= len(self.submissions):
            raise JournalError("live cursors cross durable preparation or completion")
        if published - retired > depth or retired - released > depth:
            raise JournalError("observed live ring exceeds its physical depth")

        for ordinal in range(retired, published):
            if snapshot.record(snapshot.sq_image, ordinal) != self.submissions[ordinal]:
                raise JournalError(f"pending SQ bytes differ at ordinal {ordinal}")

        for ordinal in range(released, archived):
            if snapshot.record(snapshot.cq_image, ordinal) != self.completions[ordinal]:
                raise JournalError(f"visible archived CQ bytes differ at ordinal {ordinal}")

        additions = []
        for ordinal in range(archived, retired):
            record = snapshot.record(snapshot.cq_image, ordinal)
            try:
                validate_completion(self.submissions[ordinal], record, self.identity.number)
            except ValueError as exc:
                raise JournalError(f"invalid CQ record at ordinal {ordinal}: {exc}") from exc
            additions.append(record)

        updated = replace(
            self, completions=self.completions + tuple(additions),
            published=published, released=released,
        )
        sub_end = min(len(self.submissions), retired + depth)
        writes = tuple(
            SubmissionWrite(ordinal, self.identity.sq.address(self.identity.pointer(ordinal)), self.submissions[ordinal])
            for ordinal in range(published, sub_end)
        )
        plan = ResumePlan(
            self.identity, snapshot.sq_tail, snapshot.cq_head, self.identity.pointer(sub_end),
            snapshot.cq_tail, writes, archived, retired,
        )
        return updated, plan


def replace_synced(path, data):
    """Atomically replace one durable file in its own already-created directory."""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


class JournalStore:
    """Serialize tools using one path and return plans only after the file is synced."""

    def __init__(self, path):
        self.path = Path(path)
        self.lockfile = self.path.with_name(f".{self.path.name}.lock")

    @contextmanager
    def lock(self):
        with self.lockfile.open("ab") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield

    def read(self):
        return Journal.decode(self.path.read_bytes())

    def create(self, identity):
        with self.lock():
            journal = Journal(identity)
            if self.path.exists():
                journal = self.read()
                if journal.identity != identity:
                    raise JournalError("journal already exists for a different queue incarnation")
            replace_synced(self.path, journal.encode())
            return journal

    def prepare(self, begin, records):
        with self.lock():
            updated = self.read().prepare(begin, records)
            replace_synced(self.path, updated.encode())
            return updated

    def reconcile(self, snapshot):
        with self.lock():
            journal = self.read()
            updated, plan = journal.reconcile(snapshot)
            # Re-sync an identical image too. A previous replacement might
            # have become visible before its directory sync could complete.
            replace_synced(self.path, updated.encode())
            return plan
