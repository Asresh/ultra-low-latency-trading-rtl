"""Independent binary layouts for the market-data verification suite."""

from dataclasses import dataclass
import struct

MASK64 = (1 << 64) - 1
HEADER = struct.Struct(">10sQH")
LENGTH = struct.Struct(">H")
SIZES = {"A": 36, "F": 40, "E": 31, "C": 36, "X": 23, "D": 19, "U": 35}
COMMON = struct.Struct(">HH6sQ")
ADD = struct.Struct(">BI8sI")
EXECUTE = struct.Struct(">IQ")
EXECUTE_PRICE = struct.Struct(">BI")
REPLACE = struct.Struct(">QII")
QUANTITY = struct.Struct(">I")


# Encode a session, first sequence, and length-prefixed messages as one MoldUDP64 datagram.
def make_packet(session, first, messages=(), *, declared_count=None):
    """Build a downstream MoldUDP64 packet, allowing injected header errors."""
    if len(session) != 10:
        raise ValueError("session must contain ten bytes")
    count = len(messages) if declared_count is None else declared_count
    return (HEADER.pack(session, first & MASK64, count) +
            b"".join(LENGTH.pack(len(message)) + message for message in messages))


# Encode a recovery request for count consecutive messages starting at first.
def make_request(session, first, count):
    return int.from_bytes(HEADER.pack(session, first & MASK64, count), "big")


@dataclass(frozen=True)
class Downstream:
    session: bytes
    first: int
    block_count: int
    messages: tuple

    @property
    # Identify the zero-count datagram used to advance the advertised sequence.
    def is_heartbeat(self):
        return self.block_count == 0

    @property
    # Identify the reserved count that ends a session.
    def is_end(self):
        return self.block_count == 65535

    # Pair each payload with its sequence, wrapping at the wire's 64-bit boundary.
    def numbered_messages(self):
        for index, payload in enumerate(self.messages):
            yield (self.first + index) & MASK64, payload


# Validate the header, every block, and datagram bounds before exposing any message.
def decode_packet(packet, packet_limit=1536, message_limit=64, block_limit=64):
    if len(packet) < HEADER.size:
        raise ValueError("truncated header")
    if len(packet) > packet_limit:
        raise ValueError("packet limit")
    session, first, count = HEADER.unpack_from(packet)
    offset = HEADER.size
    messages = []
    if count not in (0, 65535):
        if count > block_limit:
            raise ValueError("block count limit")
        for _ in range(count):
            if offset + LENGTH.size > len(packet):
                raise ValueError("absent block length")
            size, = LENGTH.unpack_from(packet, offset)
            offset += LENGTH.size
            if offset + size > len(packet):
                raise ValueError("truncated block")
            if size > message_limit:
                raise ValueError("body limit")
            messages.append(packet[offset:offset+size])
            offset += size
    if offset != len(packet):
        raise ValueError("undeclared packet bytes")
    return Downstream(session, first, count, tuple(messages))


@dataclass(frozen=True)
class ItchRecord:
    result: int
    sequence: int
    kind: int
    locate: int = 0
    tracking: int = 0
    timestamp: int = 0
    reference: int = 0
    replacement: int = 0
    side: int = 0
    shares: int = 0
    stock: int = 0
    price: int = 0
    match: int = 0
    printable: int = 0
    mpid: int = 0

    # Pack normalized event fields in the order used by the receiver's RTL output.
    def bits(self):
        widths = [2, 2, 64, 64, 8, 16, 16, 48, 64, 64, 8, 32, 64, 32, 64, 8, 32]
        columns = [
            self.result, 0, self.sequence, self.sequence, self.kind, self.locate,
            self.tracking, self.timestamp, self.reference, self.replacement,
            self.side, self.shares, self.stock, self.price, self.match,
            self.printable, self.mpid,
        ]
        word = 0
        for width, column in zip(widths, columns):
            if not 0 <= column < (1 << width):
                raise ValueError(f"field {column} does not fit in {width} bits")
            word = word << width | column
        return word


# Convert one supported ITCH payload and its Mold sequence to a normalized event.
def decode_itch(sequence, payload):
    kind = payload[0] if payload else 0
    if not payload:
        return ItchRecord(2, sequence, kind)
    if chr(kind) not in SIZES:
        return ItchRecord(1, sequence, kind)
    if len(payload) != SIZES[chr(kind)]:
        return ItchRecord(2, sequence, kind)

    locate, tracking, timestamp, reference = COMMON.unpack_from(payload, 1)
    base = [0, sequence, kind, locate, tracking, int.from_bytes(timestamp, "big"), reference]
    start = 1 + COMMON.size
    if kind in b"AF":
        side, shares, stock, price = ADD.unpack_from(payload, start)
        if side not in b"BS":
            return ItchRecord(2, sequence, kind)
        suffix = payload[start + ADD.size:]
        return ItchRecord(*base, side=side, shares=shares, price=price,
                          stock=int.from_bytes(stock, "big"), mpid=int.from_bytes(suffix, "big"))
    if kind in b"EC":
        shares, match = EXECUTE.unpack_from(payload, start)
        if kind == ord("E"):
            return ItchRecord(*base, shares=shares, match=match)
        printable, price = EXECUTE_PRICE.unpack_from(payload, start + EXECUTE.size)
        if printable not in b"YN":
            return ItchRecord(2, sequence, kind)
        return ItchRecord(*base, shares=shares, match=match, printable=printable, price=price)
    if kind == ord("X"):
        shares, = QUANTITY.unpack_from(payload, start)
        return ItchRecord(*base, shares=shares)
    if kind == ord("U"):
        replacement, shares, price = REPLACE.unpack_from(payload, start)
        return ItchRecord(*base, replacement=replacement, shares=shares, price=price)
    return ItchRecord(*base)


# Build a supported ITCH payload with reproducible randomized field values.
def make_itch(kind, rng):
    common = COMMON.pack(
        rng.getrandbits(16),
        rng.getrandbits(16),
        rng.getrandbits(48).to_bytes(6, "big"),
        rng.getrandbits(64),
    )
    if kind in "AF":
        side = rng.choice(b"BS")
        shares = rng.getrandbits(32)
        stock = rng.choice((b"NVDA    ", b"LONGNAME", b"AMZN    "))
        price = rng.getrandbits(32)
        tail = ADD.pack(side, shares, stock, price)
        if kind == "F":
            tail += rng.choice((b"ABCD", b"XYZW"))
    elif kind in "EC":
        tail = EXECUTE.pack(rng.getrandbits(32), rng.getrandbits(64))
        if kind == "C":
            tail += EXECUTE_PRICE.pack(rng.choice(b"YN"), rng.getrandbits(32))
    elif kind == "X":
        tail = QUANTITY.pack(rng.getrandbits(32))
    elif kind == "U":
        tail = REPLACE.pack(rng.getrandbits(64), rng.getrandbits(32), rng.getrandbits(32))
    elif kind == "D":
        tail = b""
    else:
        raise ValueError(f"unknown fixture kind {kind}")
    return kind.encode("ascii") + common + tail


# Split a packet into little-endian bus beats with byte enables and frame markers.
def words(packet, rng):
    if not packet:
        raise ValueError("test input must contain a datagram byte")
    data = []
    for offset in range(0, len(packet), 8):
        chunk = packet[offset:offset+8]
        pad = bytes(rng.randrange(256) for _ in range(8-len(chunk)))
        data.append((int.from_bytes(chunk+pad, "little"), (1 << len(chunk))-1,
                     offset == 0, offset+len(chunk) == len(packet)))
    return data
