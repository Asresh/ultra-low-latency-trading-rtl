"""Independent byte-level codec for the documented subset of OUCH 5 and SoupBinTCP 4.10."""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import BinaryIO, Iterable, Iterator


MAX_BODY = 72
MAX_SHARES = 999_999
MAX_PRICE = 1_999_999_900
MAX_SEQUENCE = (1 << 64) - 1
BLANK_SESSION = b" " * 10
BE16 = struct.Struct(">H")
BE32 = struct.Struct(">I")
BE64 = struct.Struct(">Q")


class Status(IntEnum):
    OK = 0
    BAD_KEEP = 1
    FRAME_LENGTH = 2
    SOUP_TYPE = 3
    OUCH_TYPE = 4
    OUCH_LENGTH = 5
    OUCH_APPENDAGE = 6
    LOGIN_TEXT = 7
    LOGIN_PHASE = 8
    SESSION_ID = 9
    SEQUENCE_GAP = 10
    SEQUENCE_OVERFLOW = 11
    NOT_ACTIVE = 12
    UNSUPPORTED_CMD = 13
    MARKET_CLOSED = 14
    BAD_SLOT = 15
    SLOT_BUSY = 16
    NO_ORDER = 17
    STALE_REFERENCE = 18
    BAD_FIELDS = 19
    NO_RETRY = 20
    UNKNOWN_REFERENCE = 21
    LIFECYCLE = 22
    ACK_ECHO = 23
    BAD_DELTA = 24
    DUP_MATCH = 25
    RX_TIMEOUT = 26
    LOGIN_REJECTED = 27
    KILLED = 28
    REPLAY_WINDOW = 29
    REPLAY_DIVERGED = 30
    REPLAY_FENCE = 31


class Direction(IntEnum):
    HOST_TO_PEER = 0
    PEER_TO_HOST = 1


class DecodeError(ValueError):
    """Carry a stable hardware-compatible error category and a host diagnostic."""

    def __init__(self, status: Status, message: str, offset: int | None = None):
        """Attach the protocol status and applicable byte offset to a decoding error."""
        self.status = status
        self.offset = offset
        location = f" at byte {offset}" if offset is not None else ""
        super().__init__(f"{status.name}{location}: {message}")


@dataclass(frozen=True)
class Envelope:
    kind: str
    payload: bytes

    @property
    def length(self) -> int:
        """Return the number of bytes in the envelope body, including its type byte."""
        return 1 + len(self.payload)

    def frame(self) -> bytes:
        """Add the two-byte wire length prefix and the packet type."""
        if len(self.kind) != 1 or ord(self.kind) >= 128:
            raise ValueError("Soup packet type must be one ASCII byte")
        if not 1 <= self.length <= MAX_BODY:
            raise ValueError(f"packet body length {self.length} is outside 1..{MAX_BODY}")
        return BE16.pack(self.length) + self.kind.encode("ascii") + self.payload


class StreamDecodeError(DecodeError):
    def __init__(self, status: Status, message: str, offset: int,
                 completed: list[tuple[int, Envelope]]):
        """Retain valid frames preceding the decoding fault and its offset."""
        super().__init__(status, message, offset)
        self.completed = completed


@dataclass(frozen=True)
class Login:
    username: bytes
    password: bytes
    session: bytes = BLANK_SESSION
    sequence: int = 1
    timeout: bytes = b"05000"

    def validate(self) -> None:
        """Check the record for inconsistent fields before it can be encoded or consumed."""
        if not credential_valid(self.username, 6):
            raise DecodeError(Status.LOGIN_TEXT, "username must be a 6-byte alphanumeric login field")
        if not credential_valid(self.password, 10):
            raise DecodeError(Status.LOGIN_TEXT, "password must be a 10-byte alphanumeric login field")
        if not valid_session(self.session, allow_blank=True):
            raise DecodeError(Status.LOGIN_TEXT, "invalid 10-byte session request")
        if len(self.timeout) != 5 or not self.timeout.isdigit() or int(self.timeout) == 0:
            raise DecodeError(Status.LOGIN_TEXT, "timeout must be five decimal ASCII digits and nonzero")
        if not 0 <= self.sequence <= MAX_SEQUENCE:
            raise DecodeError(Status.LOGIN_TEXT, "sequence is outside unsigned 64-bit range")

    def encode(self) -> bytes:
        """Serialize the validated protocol record into its exact wire layout."""
        self.validate()
        return Envelope("L", self.username + self.password + self.session +
                        print_decimal(self.sequence) + self.timeout).frame()


@dataclass(frozen=True)
class ClientOrder:
    kind: str
    ref: int
    quantity: int
    price: int = 0
    prev: int = 0
    side: str = ""
    symbol: bytes = b""
    tif: str = ""
    display: str = ""
    client_id: bytes = b""

    def encode(self) -> bytes:
        """Serialize the validated protocol record into its exact wire layout."""
        if self.kind == "O":
            sub = ENTER.pack(
                b"O", self.ref, self.side.encode("ascii"), self.quantity, self.symbol,
                self.price, self.tif.encode("ascii"), self.display.encode("ascii"),
                b"P", b"N", b"N", self.client_id,
            ) + b"\0\0"
        elif self.kind == "U":
            sub = REPLACE_REQUEST.pack(
                b"U", self.prev, self.ref, self.quantity, self.price, self.tif.encode("ascii"),
                self.display.encode("ascii"), b"N", self.client_id,
            ) + b"\0\0"
        elif self.kind == "X":
            sub = CANCEL_REQUEST.pack(b"X", self.ref, self.quantity)
        else:
            raise ValueError(f"unsupported outbound OUCH order type {self.kind!r}")
        return Envelope("U", sub).frame()


@dataclass(frozen=True)
class VenueEvent:
    kind: str
    time: int
    ref: int = 0
    prev: int = 0
    quantity: int = 0
    price: int = 0
    symbol: bytes = b""
    client_id: bytes = b""
    side: str = ""
    state: str = ""
    venue: int = 0
    reason: int = 0
    explicit_zero_appendage: bool = False

    def payload(self) -> bytes:
        """Build synthetic exchange messages without using FPGA-side shifts."""
        if self.kind == "S":
            return SYSTEM_EVENT.pack(b"S", self.time, bytes([self.reason]))
        if self.kind in {"A", "U"}:
            common = (
                self.ref, self.side.encode("ascii"), self.quantity, self.symbol, self.price,
                b"0", b"Y", self.venue, b"P", b"N", b"N", self.state.encode("ascii"), self.client_id,
            )
            data = (
                ACCEPTED.pack(b"A", self.time, *common)
                if self.kind == "A" else
                REPLACED.pack(b"U", self.time, self.prev, *common)
            )
            return data + b"\0\0"
        if self.kind == "E":
            return EXECUTED.pack(
                b"E", self.time, self.ref, self.quantity, self.price, bytes([self.reason]), self.venue
            ) + b"\0\0"
        if self.kind == "C":
            data = CANCELED.pack(b"C", self.time, self.ref, self.quantity, bytes([self.reason]))
        elif self.kind == "J":
            data = REJECTED.pack(b"J", self.time, self.ref, self.reason, self.client_id)
        elif self.kind in {"P", "I"}:
            data = CANCEL_STATUS.pack(self.kind.encode("ascii"), self.time, self.ref)
        else:
            raise ValueError(f"unsupported venue event {self.kind!r}")
        return data + b"\0\0" if self.explicit_zero_appendage else data

    def frame(self) -> bytes:
        """Encode a sequenced venue message and its SoupBinTCP envelope."""
        return Envelope("S", self.payload()).frame()


ENTER = struct.Struct(">cIcI8sQ5c14s")
REPLACE_REQUEST = struct.Struct(">cIIIQ3c14s")
CANCEL_REQUEST = struct.Struct(">cII")
SYSTEM_EVENT = struct.Struct(">cQc")
ACCEPTED = struct.Struct(">cQIcI8sQ2cQ4c14s")
REPLACED = struct.Struct(">cQIIcI8sQ2cQ4c14s")
CANCELED = struct.Struct(">cQIIc")
EXECUTED = struct.Struct(">cQIIQcQ")
REJECTED = struct.Struct(">cQIH14s")
CANCEL_STATUS = struct.Struct(">cQI")


def print_decimal(value: int) -> bytes:
    """Format a sequence integer in the fixed-width SoupBinTCP ASCII field."""
    if not 0 <= value <= MAX_SEQUENCE:
        raise ValueError("number is outside unsigned 64-bit range")
    return f"{value:20d}".encode("ascii")


def parse_decimal(raw: bytes) -> int:
    """Parse a padded ASCII decimal sequence and reject overflow."""
    digits = raw.lstrip(b" ")
    if len(raw) != 20 or not digits or not digits.isdigit():
        raise DecodeError(Status.LOGIN_TEXT, "invalid right-aligned ASCII sequence")
    value = int(digits)
    if value > MAX_SEQUENCE:
        raise DecodeError(Status.LOGIN_TEXT, "sequence overflows unsigned 64-bit range")
    return value


def ascii_alnum(raw: bytes) -> bool:
    """Return whether every byte belongs to the ASCII letter or digit set."""
    return bool(raw) and all(
        48 <= c <= 57 or 65 <= c <= 90 or 97 <= c <= 122 for c in raw
    )


def credential_valid(raw: bytes, length: int) -> bool:
    """Validate the width, character set and trailing padding of a credential."""
    return len(raw) == length and ascii_alnum(raw.rstrip(b" "))


def valid_session(raw: bytes, allow_blank: bool = False) -> bool:
    """Check the session identifier with the caller's optional blank allowance."""
    if len(raw) != 10:
        return False
    significant = raw.lstrip(b" ")
    return (allow_blank and not significant) or ascii_alnum(significant)


def valid_symbol(raw: bytes) -> bool:
    """Check a fixed-width symbol before accepting order entry."""
    significant = raw.rstrip(b" ")
    return len(raw) == 8 and bool(significant) and all(
        48 <= c <= 57 or 65 <= c <= 90 or c == 46 for c in significant
    )


def valid_client(raw: bytes) -> bool:
    """Check a fixed-width client identifier and its padding."""
    return len(raw) == 14 and ascii_alnum(raw.replace(b" ", b""))


def decode_login_request(raw: bytes) -> Login:
    """Decode exact login request fields and reject invalid lengths."""
    if len(raw) != 51:
        raise DecodeError(Status.FRAME_LENGTH, "login request requires 51 payload bytes")
    user, secret, session, sequence, timeout = struct.unpack(">6s10s10s20s5s", raw)
    login = Login(user, secret, session, parse_decimal(sequence), timeout)
    login.validate()
    return login


def login_accepted(session: bytes, sequence: int | bytes) -> bytes:
    """Build a venue login-accepted envelope for a session and sequence."""
    if len(session) != 10:
        raise ValueError("login accepted requires a 10-byte session ID")
    digits = sequence if isinstance(sequence, bytes) else print_decimal(sequence)
    if len(digits) != 20:
        raise ValueError("login accepted requires 20 sequence bytes")
    return Envelope("A", session + digits).frame()


def control(kind: str, reason: bytes = b"") -> bytes:
    """Construct a wire control message with an optional reason byte."""
    return Envelope(kind, reason).frame()


def _check_appendage(raw: bytes, fixed: int, optional: bool = False) -> bytes:
    """Require the mandatory message fields and a supported appendage length."""
    if optional and len(raw) == fixed:
        return raw
    if len(raw) != fixed + 2:
        raise DecodeError(Status.OUCH_LENGTH, f"expected OUCH payload length {fixed + 2}")
    if raw[fixed:] != b"\0\0":
        raise DecodeError(Status.OUCH_APPENDAGE, "this implementation permits no optional tags")
    return raw[:fixed]


def decode_outbound(payload: bytes) -> ClientOrder:
    """Decode an outbound order body into typed fields."""
    if not payload:
        raise DecodeError(Status.OUCH_LENGTH, "empty client data payload")
    kind = payload[:1]
    if kind == b"O":
        raw = _check_appendage(payload, ENTER.size)
        _, ref, side, quantity, symbol, price, tif, display, cap, iso, cross, ident = ENTER.unpack(raw)
        if (cap, iso, cross) != (b"P", b"N", b"N"):
            raise DecodeError(Status.BAD_FIELDS, "entry requires principal, non-ISO, continuous-market constants")
        return ClientOrder("O", ref, quantity, price, 0, side.decode("ascii"), symbol,
                           tif.decode("ascii"), display.decode("ascii"), ident)
    if kind == b"U":
        raw = _check_appendage(payload, REPLACE_REQUEST.size)
        _, prev, ref, quantity, price, tif, display, iso, ident = REPLACE_REQUEST.unpack(raw)
        if iso != b"N":
            raise DecodeError(Status.BAD_FIELDS, "replace requires non-ISO")
        return ClientOrder("U", ref, quantity, price, prev, "", b"",
                           tif.decode("ascii"), display.decode("ascii"), ident)
    if kind == b"X":
        raw = _check_appendage(payload, CANCEL_REQUEST.size, optional=True)
        _, ref, quantity = CANCEL_REQUEST.unpack(raw)
        return ClientOrder("X", ref, quantity)
    raise DecodeError(Status.OUCH_TYPE, f"unsupported client message {kind!r}")


def _acknowledgment(payload: bytes, replacement: bool) -> VenueEvent:
    """Decode both initial and replacement acknowledgment layouts."""
    schema = REPLACED if replacement else ACCEPTED
    fields = schema.unpack(_check_appendage(payload, schema.size))
    kind = fields[0].decode("ascii")
    time = fields[1]
    if replacement:
        prev = fields[2]
        fields = fields[3:]
    else:
        prev = 0
        fields = fields[2:]
    ref, side, quantity, symbol, price, _, _, venue, _, _, _, state, ident = fields
    return VenueEvent(kind, time, ref, prev, quantity, price, symbol, ident,
                      side.decode("latin1"), state.decode("latin1"), venue)


def decode_venue(payload: bytes) -> VenueEvent:
    """Decode a sequenced venue payload using its type and exact field widths."""
    if not payload:
        raise DecodeError(Status.OUCH_LENGTH, "empty sequenced data payload")
    kind = payload[:1]
    if kind in {b"A", b"U"}:
        return _acknowledgment(payload, replacement=kind == b"U")
    if kind == b"S":
        if len(payload) != SYSTEM_EVENT.size:
            raise DecodeError(Status.OUCH_LENGTH, "system event length mismatch")
        _, time, event = SYSTEM_EVENT.unpack(payload)
        return VenueEvent("S", time, reason=event[0])
    if kind == b"E":
        raw = _check_appendage(payload, EXECUTED.size)
        _, time, ref, qty, price, liquidity, match = EXECUTED.unpack(raw)
        return VenueEvent("E", time, ref, quantity=qty, price=price, venue=match,
                          reason=liquidity[0])
    if kind == b"C":
        raw = _check_appendage(payload, CANCELED.size, optional=True)
        _, time, ref, qty, reason = CANCELED.unpack(raw)
        return VenueEvent("C", time, ref, quantity=qty, reason=reason[0],
                          explicit_zero_appendage=len(raw) != len(payload))
    if kind == b"J":
        raw = _check_appendage(payload, REJECTED.size, optional=True)
        _, time, ref, reason, ident = REJECTED.unpack(raw)
        return VenueEvent("J", time, ref, client_id=ident, reason=reason,
                          explicit_zero_appendage=len(raw) != len(payload))
    if kind in {b"P", b"I"}:
        raw = _check_appendage(payload, CANCEL_STATUS.size, optional=True)
        _, time, ref = CANCEL_STATUS.unpack(raw)
        return VenueEvent(kind.decode("ascii"), time, ref,
                          explicit_zero_appendage=len(raw) != len(payload))
    raise DecodeError(Status.OUCH_TYPE, f"unsupported venue message {kind!r}")


def decode_peer(envelope: Envelope) -> tuple[str, VenueEvent | bytes | None]:
    """Interpret a framed message received from the venue endpoint."""
    kind = envelope.kind
    payload = envelope.payload
    if kind == "S":
        return kind, decode_venue(payload)
    if kind == "A" and len(payload) == 30:
        return kind, payload
    if kind == "J" and len(payload) == 1:
        return kind, payload
    if kind in {"H", "Z"} and not payload:
        return kind, None
    if kind == "+":
        return kind, payload
    if kind in {"A", "J", "H", "Z"}:
        raise DecodeError(Status.FRAME_LENGTH, f"unexpected payload length for Soup {kind}")
    raise DecodeError(Status.SOUP_TYPE, f"unsupported peer packet type {kind!r}")


def decode_client(envelope: Envelope) -> tuple[str, ClientOrder | Login | None]:
    """Interpret a framed message sent by the client endpoint."""
    kind = envelope.kind
    if kind == "U":
        return kind, decode_outbound(envelope.payload)
    if kind == "L":
        return kind, decode_login_request(envelope.payload)
    if kind in {"R", "O"}:
        if envelope.payload:
            raise DecodeError(Status.FRAME_LENGTH, f"unexpected payload for client control {kind}")
        return kind, None
    raise DecodeError(Status.SOUP_TYPE, f"unsupported client packet type {kind!r}")


class SoupStream:
    """Incrementally split a direction of a byte capture; offsets survive fragmentation."""

    def __init__(self, limit: int = MAX_BODY):
        """Create an empty receive buffer with a maximum permitted packet length."""
        if not 1 <= limit <= 65535:
            raise ValueError("Soup frame limit must fit its two byte prefix")
        self.limit = limit
        self.offset = 0
        self.partial = bytearray()
        self.stopped = False

    def ingest(self, fragment: bytes) -> list[tuple[int, Envelope]]:
        """Append a byte fragment and yield all newly complete valid frames."""
        if self.stopped:
            raise RuntimeError("malformed stream is closed")
        self.partial.extend(fragment)
        completed = []
        take = 0
        while len(self.partial) - take >= 2:
            offset = self.offset + take
            body_length = BE16.unpack_from(self.partial, take)[0]
            if not 1 <= body_length <= self.limit:
                self.stopped = True
                self.offset += take
                del self.partial[:take]
                raise StreamDecodeError(Status.FRAME_LENGTH, f"invalid body length {body_length}",
                                        offset, completed)
            end = take + body_length + 2
            if end > len(self.partial):
                break
            type_char = chr(self.partial[take + 2])
            completed.append((offset, Envelope(type_char, bytes(self.partial[take + 3:end]))))
            take = end
        if take:
            self.offset += take
            del self.partial[:take]
        return completed

    def finish(self) -> None:
        """Verify the stream has no truncated envelope remaining at EOF."""
        if self.partial:
            raise DecodeError(Status.FRAME_LENGTH, "truncated frame at EOF", self.offset)


def take_frames(stream: BinaryIO, chunk_size: int = 4096) -> Iterator[tuple[int, Envelope]]:
    """Feed binary chunks to the framer and return all complete envelopes."""
    if chunk_size < 1:
        raise ValueError("chunk size must be positive")
    parser = SoupStream()
    while block := stream.read(chunk_size):
        try:
            yield from parser.ingest(block)
        except StreamDecodeError as error:
            yield from error.completed
            raise
    parser.finish()


def unframe(frame: bytes) -> Envelope:
    """Decode exactly one complete frame and reject trailing bytes."""
    items = list(take_frames(io.BytesIO(frame)))
    if len(items) != 1:
        raise DecodeError(Status.FRAME_LENGTH, f"expected one complete frame, got {len(items)}")
    return items[0][1]


def multiplex(frames: Iterable[Envelope]) -> bytes:
    """Concatenate framed messages into one byte stream in order."""
    return b"".join(frame.frame() for frame in frames)
