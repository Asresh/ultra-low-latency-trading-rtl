"""Independent Ethernet II, IPv4, and UDP wire formats for models and tools."""

from dataclasses import dataclass, replace
from enum import IntEnum
from ipaddress import IPv4Address
import struct


class Result(IntEnum):
    OK = 0
    BAD_AXIS = 1
    MAC_BAD = 2
    FRAME_LIMIT = 3
    L2_TYPE = 4
    VLAN_LIMIT = 5
    L2_SHORT = 6
    IP_SHAPE = 7
    IP_LENGTH = 8
    IP_FRAGMENT = 9
    IP_CHECKSUM = 10
    IP_PROTOCOL = 11
    UDP_LENGTH = 12
    UDP_CHECKSUM = 13
    UDP_ZERO = 14
    RULE_MISS = 15
    RULE_DROP = 16
    TX_DESCRIPTOR = 17
    TX_LENGTH = 18


BE16 = struct.Struct(">H")
ETHER_PREFIX = struct.Struct(">6s6sH")
IPV4_BASE = struct.Struct(">BBHHHBBHII")
UDP_HEAD = struct.Struct(">HHHH")
UDP_PSEUDO = struct.Struct(">IIBBH")
VLAN_TYPES = (0x8100, 0x88A8)
ETH_IP = 0x0800
ETH_MIN_NO_FCS = 60
VLAN_OUTER_TYPE = 0x88A8
PROTOCOL_UDP = 17
RX_META_BITS = 384
TX_DESC_BITS = 320
RULE_BITS = 256


def as_mac(value):
    if isinstance(value, str):
        octets = value.split(":")
        if len(octets) != 6 or any(len(octet) != 2 for octet in octets):
            raise ValueError(f"bad Ethernet address {value!r}")
        value = int("".join(octets), 16)
    if not isinstance(value, int) or not 0 <= value < (1 << 48):
        raise ValueError(f"Ethernet address out of range: {value!r}")
    return value


def as_ipv4(value):
    return int(IPv4Address(value))


def bounded(value, width, label):
    if not isinstance(value, int) or not 0 <= value < (1 << width):
        raise ValueError(f"{label} must fit in {width} bits")
    return value


def bit_fields(*fields):
    value = 0
    for number, width in fields:
        value = (value << width) | bounded(number, width, "packed field")
    return value


def checksum(data):
    """RFC 1071 sum with carry folded until no carry remains, then inverted."""
    if len(data) & 1:
        data += b"\x00"
    total = sum(value[0] for value in struct.iter_unpack(">H", data))
    while total > 0xffff:
        total = (total & 0xffff) + (total >> 16)
    return 0xffff ^ total


def udp_checksum(source, destination, segment):
    pseudo = UDP_PSEUDO.pack(source, destination, 0, PROTOCOL_UDP, len(segment))
    return checksum(pseudo + segment)


@dataclass(frozen=True)
class MacBeat:
    data: int
    keep: int
    first: bool
    last: bool
    bad: bool = False

    def valid_bytes(self):
        masks = tuple((1 << count) - 1 for count in range(1, 9))
        if self.keep not in masks:
            return b""
        return self.data.to_bytes(8, "little")[:bin(self.keep).count("1")]

    def row(self):
        return f"{self.data:016x} {self.keep:02x} {int(self.first)} {int(self.last)} {int(self.bad)}"

    @classmethod
    def parse(cls, row):
        data, keep, first, last, bad = row.split()
        flags = tuple(int(x) for x in (first, last, bad))
        if any(flag not in (0, 1) for flag in flags):
            raise ValueError("stream markers must be zero or one")
        return cls(bounded(int(data, 16), 64, "stream data"),
                   bounded(int(keep, 16), 8, "keep"), *(bool(x) for x in flags))


def to_beats(payload, *, rng=None, bad=False):
    if not payload:
        raise ValueError("zero-byte frame has no MAC representation")
    beats = []
    for offset in range(0, len(payload), 8):
        chunk = payload[offset:offset+8]
        pad = bytes(rng.randrange(256) for _ in range(8-len(chunk))) if rng else bytes(8-len(chunk))
        end = (offset+len(chunk) == len(payload))
        beats.append(MacBeat(int.from_bytes(chunk+pad, "little"),
                            (1 << len(chunk))-1, offset == 0, end, bad and end))
    return beats


def from_beats(beats):
    chunks = []
    fault = False
    bad = False
    for index, beat in enumerate(beats):
        data = beat.valid_bytes()
        last = index == len(beats)-1
        if beat.first != (index == 0) or beat.last != last:
            fault = True
        if not data or (not last and beat.keep != 255):
            fault = True
        bad |= beat.bad
        chunks.append(data)
    if not beats or fault:
        return b"".join(chunks), Result.BAD_AXIS
    if bad:
        return b"".join(chunks), Result.MAC_BAD
    return b"".join(chunks), Result.OK


@dataclass(frozen=True)
class Packet:
    dest_mac: int
    src_mac: int
    src_ip: int
    dst_ip: int
    src_port: int
    dst_port: int
    payload: bytes = b""
    tags: tuple = ()
    ident: int = 0
    ttl: int = 64
    tos: int = 0
    df: bool = True
    udp_zero: bool = False
    options: bytes = b""
    protocol: int = PROTOCOL_UDP
    frag_bits: int = 0

    def l2(self):
        addresses = self.dest_mac.to_bytes(6, "big") + self.src_mac.to_bytes(6, "big")
        return addresses + b"".join(struct.pack(">HH", tag_type, tci)
                                    for tag_type, tci in self.tags) + BE16.pack(ETH_IP)

    def segment(self):
        length = len(self.payload)+UDP_HEAD.size
        segment = UDP_HEAD.pack(self.src_port, self.dst_port, length, 0) + self.payload
        if self.udp_zero:
            return segment
        value = udp_checksum(self.src_ip, self.dst_ip, segment) or 0xffff
        return segment[:6] + BE16.pack(value) + segment[8:]

    def ip_header(self, data_len):
        if len(self.options) % 4 or len(self.options) > 40:
            raise ValueError("IPv4 options must occupy four to forty aligned bytes, or be empty")
        header_size = IPV4_BASE.size + len(self.options)
        flags = self.frag_bits | (0x4000 if self.df else 0)
        blank = IPV4_BASE.pack(
            0x40 | (header_size//4), self.tos, header_size+data_len, self.ident,
            flags, self.ttl, self.protocol, 0, self.src_ip, self.dst_ip,
        ) + self.options
        return blank[:10] + BE16.pack(checksum(blank)) + blank[12:]

    def serialize(self, *, include_pad=True, fill=0):
        segment = self.segment()
        frame = self.l2() + self.ip_header(len(segment)) + segment
        if include_pad and len(frame) < ETH_MIN_NO_FCS:
            return frame + bytes((fill,))*(ETH_MIN_NO_FCS-len(frame))
        return frame


@dataclass(frozen=True)
class Decoded:
    result: Result
    frame_length: int
    packet: Packet = None
    l2_length: int = 0
    queue: int = 0
    epoch: int = 0

    def rx_info(self):
        if not self.packet:
            return bit_fields((self.result,8),(0,376)), ((1 << 8)-1) << 376
        p = self.packet
        tags = tuple(item[1] for item in p.tags)
        outer = tags[0] if tags else 0
        inner = tags[1] if len(tags) == 2 else 0
        body = bit_fields(
            (self.result,8),(self.queue,8),(self.epoch,8),(self.l2_length,8),(0,64),
            (p.dest_mac,48),(p.src_mac,48),(p.src_ip,32),(p.dst_ip,32),
            (p.src_port,16),(p.dst_port,16),(len(p.payload),16),(self.frame_length,16),
            (outer,16),(inner,16),(p.ident,16),(p.ttl,8),(len(tags),2),
            (p.udp_zero,1),(p.df,1),(5+len(p.options)//4,4),
        )
        mask = ((1 << RX_META_BITS)-1) ^ (((1 << 64)-1) << 288)
        return body, mask


def parse_frame(frame, *, max_frame=1536, allow_zero=True):
    size = len(frame)

    def bad(code):
        return Decoded(code, size)

    if size > max_frame:
        return bad(Result.FRAME_LIMIT)
    if size < ETHER_PREFIX.size:
        return bad(Result.L2_SHORT)

    dest, source, ether_type = ETHER_PREFIX.unpack_from(frame)
    tags = []
    cursor = ETHER_PREFIX.size
    while ether_type in VLAN_TYPES:
        if len(tags) == 2:
            return bad(Result.VLAN_LIMIT)
        if cursor + 4 > size:
            return bad(Result.L2_SHORT)
        tci, next_type = struct.unpack_from(">HH", frame, cursor)
        tags.append((ether_type, tci))
        ether_type = next_type
        cursor += 4
    if ether_type != ETH_IP:
        return bad(Result.L2_TYPE)
    if cursor + IPV4_BASE.size > size:
        return bad(Result.IP_LENGTH)

    fields = IPV4_BASE.unpack_from(frame, cursor)
    version_ihl, tos, total, ident, flags, ttl, proto, _, src_ip, dst_ip = fields
    if version_ihl >> 4 != 4 or (version_ihl & 15) < 5:
        return bad(Result.IP_SHAPE)
    ip_header_size = (version_ihl & 15)*4
    if total < ip_header_size:
        return bad(Result.IP_LENGTH)
    if flags & 0xBFFF:
        return bad(Result.IP_FRAGMENT)
    if ttl == 0:
        return bad(Result.IP_SHAPE)

    end = cursor + total
    if not end <= size <= max(end, ETH_MIN_NO_FCS):
        return bad(Result.IP_LENGTH)
    ip_header = frame[cursor:cursor+ip_header_size]
    if checksum(ip_header):
        return bad(Result.IP_CHECKSUM)
    if proto != PROTOCOL_UDP:
        return bad(Result.IP_PROTOCOL)
    udp_segment = frame[cursor+ip_header_size:end]
    if len(udp_segment) < UDP_HEAD.size:
        return bad(Result.UDP_LENGTH)
    source_port, dest_port, udp_length, wire_sum = UDP_HEAD.unpack_from(udp_segment)
    if udp_length != len(udp_segment):
        return bad(Result.UDP_LENGTH)
    if wire_sum == 0:
        if not allow_zero:
            return bad(Result.UDP_ZERO)
    elif udp_checksum(src_ip, dst_ip, udp_segment):
        return bad(Result.UDP_CHECKSUM)

    packet = Packet(
        int.from_bytes(dest, "big"), int.from_bytes(source, "big"), src_ip, dst_ip,
        source_port, dest_port, udp_segment[UDP_HEAD.size:], tuple(tags), ident, ttl, tos,
        bool(flags & 0x4000), wire_sum == 0, ip_header[IPV4_BASE.size:], proto,
    )
    return Decoded(Result.OK, size, packet, cursor)


@dataclass(frozen=True)
class Match:
    value: int = 0
    mask: int = 0

    def matches(self, observed):
        return (observed & self.mask) == (self.value & self.mask)


@dataclass(frozen=True)
class Rule:
    src_ip: Match = Match()
    dst_ip: Match = Match()
    src_port: Match = Match()
    dst_port: Match = Match()
    tci: Match = Match()
    tags: Match = Match()
    enabled: bool = True
    drop: bool = False
    queue: int = 0

    def matches(self, packet):
        tci = packet.tags[0][1] if packet.tags else 0
        return self.enabled and all((
            self.src_ip.matches(packet.src_ip), self.dst_ip.matches(packet.dst_ip),
            self.src_port.matches(packet.src_port), self.dst_port.matches(packet.dst_port),
            self.tci.matches(tci), self.tags.matches(len(packet.tags)),
        ))

    def encode(self):
        return bit_fields(
            (self.src_ip.value,32),(self.src_ip.mask,32),(self.enabled,1),(self.drop,1),
            (self.queue,3),(self.tags.value,2),(self.tags.mask,2),(0,7),
            (self.dst_ip.value,32),(self.dst_ip.mask,32),(self.dst_port.value,16),
            (self.dst_port.mask,16),(self.tci.value,16),(self.tci.mask,16),
            (self.src_port.value,16),(self.src_port.mask,16),(0,16),
        )


def classify(decoded, rules, epoch=0):
    if decoded.result != Result.OK:
        return replace(decoded, epoch=epoch)
    for entry in rules:
        if entry.matches(decoded.packet):
            if entry.drop:
                return replace(decoded, result=Result.RULE_DROP, epoch=epoch)
            return replace(decoded, queue=entry.queue, epoch=epoch)
    return replace(decoded, result=Result.RULE_MISS, epoch=epoch)


@dataclass(frozen=True)
class TxDescriptor:
    packet: Packet
    cookie: int
    payload_size: int = None
    tags_override: int = None
    reserved: int = 0

    @property
    def length(self):
        return len(self.packet.payload) if self.payload_size is None else self.payload_size

    @property
    def tags(self):
        return len(self.packet.tags) if self.tags_override is None else self.tags_override

    def encode(self):
        p = self.packet
        outer = p.tags[0][1] if p.tags else 0
        inner = p.tags[1][1] if len(p.tags) > 1 else 0
        return bit_fields(
            (self.cookie,32),(p.dest_mac,48),(p.src_mac,48),(p.src_ip,32),(p.dst_ip,32),
            (p.src_port,16),(p.dst_port,16),(self.length,16),(outer,16),(inner,16),
            (p.ident,16),(p.ttl,8),(p.tos,8),(0,8),(self.tags,2),(p.udp_zero,1),
            (p.df,1),(self.reserved,4),
        )

    def expected(self, beats, *, max_payload=1472):
        if self.tags > 2 or not self.packet.ttl or self.length > max_payload or self.reserved:
            return Result.TX_DESCRIPTOR, b""
        if self.length:
            payload, status = from_beats(beats)
            if status != Result.OK:
                return Result.BAD_AXIS, b""
            if len(payload) != self.length:
                return Result.TX_LENGTH, b""
        else:
            payload = b""
        tags = tuple((VLAN_OUTER_TYPE if self.tags == 2 and i == 0 else 0x8100, value)
                     for i, (_, value) in enumerate(self.packet.tags[:self.tags]))
        p = replace(self.packet, payload=payload, tags=tags, options=b"", protocol=17, frag_bits=0)
        return Result.OK, p.serialize()
