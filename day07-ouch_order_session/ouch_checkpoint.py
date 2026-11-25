"""Independent OCP1 checkpoint codec for durable offline recovery."""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Iterable

from ouch_model import (
    COMMAND_WIDTHS,
    SLOT_WIDTHS,
    Command,
    CommandKind,
    Order,
    Pending,
    Session,
    SessionState,
    pack,
    unpack,
)
from ouch_protocol import MAX_PRICE, MAX_SEQUENCE, MAX_SHARES, valid_client, valid_session, valid_symbol


MAGIC = 0x3150434F
META_WIDTHS = (1, 1, 1, 32, 64, 80)
META_BITS = sum(META_WIDTHS)
ORDER_BITS = sum(SLOT_WIDTHS) + sum(COMMAND_WIDTHS) + 1
META_WORDS = (META_BITS + 31) // 32
ORDER_WORDS = (ORDER_BITS + 31) // 32
COMMAND_BITS = sum(COMMAND_WIDTHS)
WORD = struct.Struct("<I")


class CheckpointStatus(IntEnum):
    OK = 0
    UNSAFE = 1
    BAD_CMD = 2
    BAD_FRAME = 3
    BAD_FORMAT = 4
    BAD_CRC = 5
    BAD_STATE = 6
    ABORTED = 7
    BAD_ID = 8


class CheckpointError(ValueError):
    def __init__(self, status: CheckpointStatus, reason: str, slot: int | None = None):
        self.status = status
        self.slot = slot
        location = "" if slot is None else f" slot={slot}"
        super().__init__(f"{status.name}{location}: {reason}")


def crc_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        running = byte
        for _ in range(8):
            running = running >> 1 ^ (0x82F63B78 if running & 1 else 0)
        table.append(running)
    return tuple(table)


CRC_TABLE = crc_table()


def crc32c(data: bytes) -> int:
    """Compute Castagnoli checksum one byte at a time."""
    running = 0xFFFFFFFF
    for byte in data:
        running = CRC_TABLE[(running ^ byte) & 255] ^ (running >> 8)
    return running ^ 0xFFFFFFFF


def words(data: bytes) -> tuple[int, ...]:
    if len(data) % WORD.size:
        raise CheckpointError(CheckpointStatus.BAD_FRAME, "unaligned 32-bit stream")
    return tuple(value for value, in struct.iter_unpack("<I", data))


def wire_words(elements: Iterable[int]) -> bytes:
    return b"".join(WORD.pack(value) for value in elements)


def joined(fields: Iterable[int]) -> int:
    """Assemble low-order words first."""
    return sum(value << (32 * row) for row, value in enumerate(fields))


def expanded(value: int, amount: int) -> tuple[int, ...]:
    return tuple((value >> (index * 32)) & 0xFFFFFFFF for index in range(amount))


@dataclass(frozen=True)
class Metadata:
    ended: bool
    killed: bool
    market_open: bool
    high_water: int
    checkpoint: int
    session: bytes

    def raw(self) -> int:
        return pack((self.ended, self.killed, self.market_open, self.high_water,
                     self.checkpoint, self.session), META_WIDTHS)

    @classmethod
    def decode(cls, word: int) -> Metadata:
        ended, killed, market, highest, checkpoint, session = unpack(word, META_WIDTHS)
        return cls(bool(ended), bool(killed), bool(market), highest, checkpoint, session.to_bytes(10, "big"))


def command_from_word(value: int) -> Command:
    fields = unpack(value, COMMAND_WIDTHS)
    kind, slot, tag, ref, prev, qty, price, symbol, side, tif, display, ident = fields
    return Command(kind,slot,tag,ref,prev,qty,price,symbol.to_bytes(8,"big"),
                   chr(side),chr(tif),chr(display),ident.to_bytes(14,"big"))


def row_fields(value: int) -> tuple[tuple[int, ...], int, bool]:
    match_seen = bool(value & 1)
    command = (value >> 1) & ((1 << COMMAND_BITS) - 1)
    view = tuple(unpack(value >> (COMMAND_BITS + 1), SLOT_WIDTHS))
    return view, command, match_seen


def order_from_word(value: int) -> Order:
    if not value:
        return Order()
    view, payload, match_seen = row_fields(value)
    live,pending,ref,_next,leaves,filled,limit,symbol,venue,last,ident,side,tag = view
    return Order(bool(live),ref,leaves,filled,limit,symbol.to_bytes(8,"big"),venue,
                 last if match_seen else None,ident.to_bytes(14,"big"),chr(side),tag,
                 command_from_word(payload) if pending else None)


def order_to_word(order: Order) -> int:
    command = order.outstanding
    # The native table canonicalizes a cancel's previous reference when
    # building its retry journal. Its wire cancel uses the current reference.
    if command is not None and command.kind == CommandKind.CANCEL:
        command = replace(command, prev=command.ref)
    packed = command.pack() if command is not None else 0
    return (pack(order.slot_fields(), SLOT_WIDTHS) << (COMMAND_BITS + 1) |
            packed << 1 | int(order.last_match is not None))


def validate_row(value: int, index: int, highest: int) -> tuple[int, ...]:
    if not 0 <= value < (1 << ORDER_BITS):
        raise CheckpointError(CheckpointStatus.BAD_FORMAT, "nonzero slot padding", index)
    if not value:
        return ()
    view, encoded, match_seen = row_fields(value)
    live,pending,ref,next_ref,leaves,filled,limit,symbol,venue,last,ident,side,tag = view
    if not 0 < ref <= highest or not 0 < limit <= MAX_PRICE:
        raise CheckpointError(CheckpointStatus.BAD_STATE, "reference or limit outside range", index)
    if (leaves + filled > MAX_SHARES or bool(live) != bool(leaves) or
            (not match_seen and last) or chr(side) not in "BS" or
            not valid_symbol(symbol.to_bytes(8,"big")) or not valid_client(ident.to_bytes(14,"big"))):
        raise CheckpointError(CheckpointStatus.BAD_STATE, "impossible order state", index)

    if pending == Pending.NONE:
        if encoded or next_ref:
            raise CheckpointError(CheckpointStatus.BAD_STATE, "orphaned request fields", index)
        return (ref,)

    request = command_from_word(encoded)
    if request.slot != index:
        raise CheckpointError(CheckpointStatus.BAD_STATE, "pending request names another slot", index)

    if pending == Pending.CANCEL:
        if (not live or next_ref or request.kind != CommandKind.CANCEL or
                request.ref != ref or request.prev != ref or request.quantity > MAX_SHARES):
            raise CheckpointError(CheckpointStatus.BAD_STATE, "invalid cancel journal", index)
        return (ref,)

    if (not 0 < request.quantity <= MAX_SHARES or not 0 < request.price <= MAX_PRICE or
            request.display not in ("Y", "N") or request.tif not in ("0", "3", "5") or
            not valid_client(request.client_id)):
        raise CheckpointError(CheckpointStatus.BAD_STATE, "invalid pending payload", index)

    if pending == Pending.ENTER:
        if (live or match_seen or filled or venue or next_ref or request.kind != CommandKind.ENTER or
                request.prev or request.ref != ref or request.price != limit or request.tag != tag or
                request.symbol != symbol.to_bytes(8,"big") or request.side != chr(side) or
                request.client_id != ident.to_bytes(14,"big")):
            raise CheckpointError(CheckpointStatus.BAD_STATE, "entry journal diverged from slot", index)
        return (ref,)

    assert pending == Pending.REPLACE
    if (not live or request.kind != CommandKind.REPLACE or request.prev != ref or
            request.ref != next_ref or not ref < next_ref <= highest):
        raise CheckpointError(CheckpointStatus.BAD_STATE, "invalid replacement reference", index)
    return ref, next_ref


@dataclass(frozen=True)
class Archive:
    image_id: int
    metadata: Metadata
    records: tuple[int, ...]

    def validate(self) -> None:
        if not 0 < self.image_id <= MAX_SEQUENCE or not 1 <= len(self.records) <= 255:
            raise CheckpointError(CheckpointStatus.BAD_FORMAT, "unsupported image ID or geometry")
        m = self.metadata
        if (not valid_session(m.session) or not 0 < m.checkpoint <= MAX_SEQUENCE or
                not 0 <= m.high_water < (1 << 32) or (m.ended and m.market_open)):
            raise CheckpointError(CheckpointStatus.BAD_STATE, "invalid pinned session and cursor")
        seen: dict[int, int] = {}
        for index, row in enumerate(self.records):
            for ref in validate_row(row, index, m.high_water):
                if ref in seen:
                    raise CheckpointError(CheckpointStatus.BAD_STATE,
                                          f"reference {ref} also appears in slot {seen[ref]}", index)
                seen[ref] = index

    def encode(self) -> bytes:
        self.validate()
        return self.encode_unchecked()

    def encode_unchecked(self) -> bytes:
        """Seal synthetic adversarial contents so structural checks are exercised."""
        geometry = ORDER_WORDS << 16 | META_WORDS << 8 | len(self.records)
        content = wire_words((MAGIC, geometry, *expanded(self.image_id,2),
                              *expanded(self.metadata.raw(),META_WORDS)))
        content += b"".join(wire_words(expanded(row,ORDER_WORDS)) for row in self.records)
        return content + WORD.pack(crc32c(content))

    @classmethod
    def decode(cls, payload: bytes, expected_image_id: int | None = None) -> Archive:
        raw = words(payload)
        if len(raw) < 4+META_WORDS+ORDER_WORDS+1:
            raise CheckpointError(CheckpointStatus.BAD_FRAME, "missing fixed fields or a complete row")
        geometry = raw[1]
        count = geometry & 255
        if raw[0] != MAGIC or (geometry >> 8) != (ORDER_WORDS << 8 | META_WORDS) or not count:
            raise CheckpointError(CheckpointStatus.BAD_FORMAT, "unknown OCP1 geometry")
        if len(raw) != 5+META_WORDS+count*ORDER_WORDS:
            raise CheckpointError(CheckpointStatus.BAD_FRAME, "wrong image length for geometry")
        image_id = joined(raw[2:4])
        if not image_id or (expected_image_id is not None and image_id != expected_image_id):
            raise CheckpointError(CheckpointStatus.BAD_ID, "image ID differs from anchor")
        raw_meta = joined(raw[4:4+META_WORDS])
        if raw_meta >= 1 << META_BITS:
            raise CheckpointError(CheckpointStatus.BAD_FORMAT, "nonzero metadata padding")
        first = 4+META_WORDS
        rows = tuple(joined(raw[first+i*ORDER_WORDS:first+(i+1)*ORDER_WORDS]) for i in range(count))
        if any(value >= 1 << ORDER_BITS for value in rows):
            raise CheckpointError(CheckpointStatus.BAD_FORMAT, "nonzero slot padding")
        if crc32c(payload[:-4]) != raw[-1]:
            raise CheckpointError(CheckpointStatus.BAD_CRC, "checksum changed")
        archive = cls(image_id, Metadata.decode(raw_meta), rows)
        archive.validate()
        return archive

    @classmethod
    def from_session(cls, image_id: int, session: Session) -> Archive:
        if session.up or not session.login_seen or session.unverified is not None:
            raise CheckpointError(CheckpointStatus.UNSAFE, "session is open or replay is unverified")
        meta = Metadata(session.state == SessionState.ENDED, session.killed, session.market,
                        session.store.high_water, session.checkpoint, session.session)
        result = cls(image_id, meta, tuple(order_to_word(session.store.view(slot))
                                           for slot in range(session.capacity)))
        result.validate()
        return result

    def restore(self, replay_depth: int, tx_ticks: int = 4, rx_ticks: int = 7) -> Session:
        """Start an offline oracle from the validated pinned identity and journal."""
        self.validate()
        if replay_depth < 1:
            raise ValueError("restore requires exact replay")
        session = Session(slots=len(self.records), replay_depth=replay_depth,
                          tx_ticks=tx_ticks, rx_ticks=rx_ticks)
        session.login_seen = True
        session.session = self.metadata.session
        session.checkpoint = self.metadata.checkpoint
        session.socket_cursor = self.metadata.checkpoint
        session.killed = self.metadata.killed
        session.market = self.metadata.market_open
        session.state = SessionState.ENDED if self.metadata.ended else SessionState.OFFLINE
        session.store.high_water = self.metadata.high_water
        session.store.orders = {slot: order_from_word(value)
                                for slot, value in enumerate(self.records) if value}
        return session
