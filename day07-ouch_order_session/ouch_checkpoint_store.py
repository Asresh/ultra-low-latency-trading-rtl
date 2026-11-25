"""Store and recover validated OUCH images in a transactional local archive."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from ouch_checkpoint import Archive, CheckpointError, order_from_word
from ouch_protocol import MAX_SEQUENCE


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class Anchor:
    image_id: int
    digest: bytes

    def __post_init__(self) -> None:
        if not 0 < self.image_id <= MAX_SEQUENCE or len(self.digest) != 32:
            raise ValueError("invalid image ID or SHA-256 digest")

    @property
    def key(self) -> str:
        return f"{self.image_id:016x}"

    def __str__(self) -> str:
        return f"{self.key}:{self.digest.hex()}"

    @classmethod
    def parse(cls, raw: str) -> Anchor:
        identifier, separator, digest = raw.partition(":")
        if not separator or len(identifier) != 16 or len(digest) != 64:
            raise ValueError("anchor requires 16 hex ID digits, a colon and 64 hex digest digits")
        permitted = set("0123456789abcdef")
        if set(identifier.lower()) - permitted or set(digest.lower()) - permitted:
            raise ValueError("anchor contains non-hexadecimal characters")
        return cls(int(identifier,16),bytes.fromhex(digest))


SCHEMA = """
CREATE TABLE IF NOT EXISTS ouch_images (
    id TEXT PRIMARY KEY CHECK(length(id) = 16),
    digest BLOB NOT NULL CHECK(length(digest) = 32),
    record BLOB NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ouch_latest (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    id TEXT NOT NULL REFERENCES ouch_images(id)
);
"""

LATEST_ROW = """
SELECT image.id, image.digest, image.record
FROM ouch_latest AS active
JOIN ouch_images AS image ON image.id = active.id
WHERE active.singleton = 1
"""


@dataclass(frozen=True)
class Stored:
    anchor: Anchor
    archive: Archive
    image: bytes

    @classmethod
    def validate(cls, row: tuple[str, bytes, bytes], trusted: Anchor | None = None) -> Stored:
        identifier,stored_digest,payload = row
        try:
            anchor = Anchor.parse(f"{identifier}:{stored_digest.hex()}")
        except (TypeError,ValueError,AttributeError) as error:
            raise StoreError("invalid archive index fields") from error
        computed = hashlib.sha256(payload).digest()
        if not hmac.compare_digest(computed,anchor.digest):
            raise StoreError("persistent image digest changed")
        if trusted is not None and (trusted.image_id != anchor.image_id or
                                    not hmac.compare_digest(trusted.digest,computed)):
            raise StoreError("persistent image does not match the independent anchor")
        try:
            archive = Archive.decode(payload,anchor.image_id)
        except CheckpointError as error:
            raise StoreError(f"stored image failed validation: {error}") from error
        return cls(anchor,archive,payload)


@contextmanager
def publish_transaction(connection: sqlite3.Connection):
    """Make the image bytes and the published ID advance one durable transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


class CheckpointStore:
    def __init__(self,path: Path):
        self.path = path

    def _writer(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path,timeout=5,isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode != ("wal",):
                raise StoreError(f"database failed to enter WAL mode: {mode}")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(SCHEMA)
            return connection
        except BaseException:
            connection.close()
            raise

    def publish(self,payload: bytes) -> Anchor:
        """Reject corrupted, stale, or conflicting images before replacing the pointer."""
        image = Archive.decode(payload)
        anchor = Anchor(image.image_id,hashlib.sha256(payload).digest())
        with closing(self._writer()) as connection:
            with publish_transaction(connection):
                newest = connection.execute(LATEST_ROW).fetchone()
                if newest is not None:
                    prior = Stored.validate(newest)
                    if prior.anchor.image_id == anchor.image_id:
                        if not hmac.compare_digest(prior.anchor.digest,anchor.digest):
                            raise StoreError("same image ID names different bytes")
                        return prior.anchor
                    if anchor.image_id < prior.anchor.image_id:
                        raise StoreError("checkpoint ID regressed")
                    previous = prior.archive.metadata
                    updated = image.metadata
                    if updated.session != previous.session or len(image.records) != len(prior.archive.records):
                        raise StoreError("store is bound to one pinned session and slot geometry")
                    if (updated.checkpoint < previous.checkpoint or updated.high_water < previous.high_water or
                            (previous.killed and not updated.killed) or (previous.ended and not updated.ended)):
                        raise StoreError("checkpoint state regressed")
                connection.execute(
                    "INSERT INTO ouch_images (id,digest,record) VALUES (?,?,?)",
                    (anchor.key,anchor.digest,payload),
                )
                connection.execute(
                    """INSERT INTO ouch_latest (singleton,id) VALUES (1,?)
                       ON CONFLICT(singleton) DO UPDATE SET id=excluded.id""",
                    (anchor.key,),
                )
        return anchor

    def load(self,trusted: Anchor | None = None) -> Stored:
        """Read the current committed image and verify bytes, structure and optional anchor."""
        url = self.path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(url,uri=True,timeout=5,isolation_level=None)) as connection:
            row = connection.execute(LATEST_ROW).fetchone()
        if row is None:
            raise StoreError("the store has no committed checkpoint")
        return Stored.validate(row,trusted)


def atomic_image(path: Path,payload: bytes) -> None:
    """Do not leave an apparently complete partial image at the restore path."""
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent,prefix=path.name+".",delete=False) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary,path)
        temporary = None
        descriptor = os.open(path.parent,os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def report(stored: Stored) -> dict:
    meta = stored.archive.metadata
    active = []
    for index,value in enumerate(stored.archive.records):
        order = order_from_word(value)
        if not order.ref:
            continue
        active.append({
            "slot": index,
            "order_ref": order.ref,
            "live": order.live,
            "leaves": order.leaves,
            "filled": order.total_filled,
            "pending_kind": order.pending.name,
            "pending_reference": order.outstanding.ref if order.outstanding else None,
        })
    return {
        "anchor": str(stored.anchor),
        "session": meta.session.decode("ascii").strip(),
        "next_sequence": meta.checkpoint,
        "highest_order_ref": meta.high_water,
        "ended": meta.ended,
        "killed": meta.killed,
        "market_open": meta.market_open,
        "slots": len(stored.archive.records),
        "orders": active,
    }


def same_file(first: Path,second: Path) -> bool:
    if first.resolve() == second.resolve():
        return True
    return first.exists() and second.exists() and first.samefile(second)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action",required=True)
    save = actions.add_parser("publish",help="validate and durably publish an OCP1 image")
    save.add_argument("database",type=Path)
    save.add_argument("image",type=Path)
    for name in ("inspect","extract"):
        read = actions.add_parser(name)
        read.add_argument("database",type=Path)
        read.add_argument("--trusted-anchor",type=Anchor.parse)
        if name == "extract":
            read.add_argument("image",type=Path)
    args = parser.parse_args()
    store = CheckpointStore(args.database)
    try:
        if args.action == "publish":
            if same_file(args.database,args.image):
                raise StoreError("database and image paths alias")
            print(store.publish(args.image.read_bytes()))
        else:
            stored = store.load(args.trusted_anchor)
            if args.action == "extract":
                alias_paths = (args.database,Path(str(args.database)+"-wal"),Path(str(args.database)+"-shm"))
                if any(same_file(path,args.image) for path in alias_paths):
                    raise StoreError("extraction would replace a database file")
                atomic_image(args.image,stored.image)
            print(json.dumps(report(stored),indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, StoreError) as error:
        print(f"checkpoint: {error}",file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
