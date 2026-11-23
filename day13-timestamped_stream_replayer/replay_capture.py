"""Offline classic-PCAP ingestion and packet extraction for scheduled replay."""

from dataclasses import dataclass
from struct import Struct
from typing import Optional

from replay_format import StampedPacket
from replay_program import ReplayError, U16, U32, unsigned

# Magic bytes select byte order and units used by the fractional timestamp word.
MAGICS = {
    bytes.fromhex("d4c3b2a1"): ("<", 1000),
    bytes.fromhex("a1b2c3d4"): (">", 1000),
    bytes.fromhex("4d3cb2a1"): ("<", 1),
    bytes.fromhex("a1b23c4d"): (">", 1),
}
ETHERNET = 1
VLAN_TYPES = frozenset((0x8100, 0x88A8, 0x9100))


def read_exact(source, length, description):
    """Read one complete PCAP field or raise a labeled truncation error."""
    data = source.read(length)
    if len(data) != length:
        raise ReplayError(f"truncated {description}: expected {length} bytes, got {len(data)}")
    return data


@dataclass(frozen=True)
class CaptureHeader:
    """Version, record byte order and snap limit of a supported Ethernet capture."""

    order: str
    tick_ns: int
    limit: int
    link_type: int

    @property
    def divisor(self):
        """Return the number of fractional timestamp units per second."""
        return 10**9 // self.tick_ns

    @classmethod
    def read(cls, source, hard_limit=1 << 20):
        """Decode the fixed-size global header and reject unsafe allocation geometry."""
        unsigned(hard_limit, U32, "maximum capture packet")
        magic = read_exact(source, 4, "PCAP magic")
        try:
            order, unit = MAGICS[magic]
        except KeyError as exc:
            raise ReplayError("unsupported PCAP header; use classic PCAP with Ethernet link type") from exc
        profile = Struct(order+"HHIIII")
        major, minor, _, _, captured, link_type = profile.unpack(
            read_exact(source, profile.size, "PCAP global header")
        )
        if (major, minor) != (2, 4):
            raise ReplayError(f"unsupported PCAP version {major}.{minor}")
        if not 0 < captured <= hard_limit:
            raise ReplayError("PCAP snap length is zero or exceeds the configured limit")
        if link_type != ETHERNET:
            raise ReplayError(f"PCAP link type {link_type} is not Ethernet")
        return cls(order, unit, captured, link_type)


@dataclass(frozen=True)
class CapturePacket:
    """One full record from the capture file, including any truncated payload."""

    position: int
    time_ns: int
    original_length: int
    payload: bytes

    @property
    def truncated(self):
        """Check whether packet capture stopped before the original frame ended."""
        return len(self.payload) != self.original_length


def packet_records(source, *, hard_limit=1 << 20, require_monotonic=True):
    """Yield records with integer nanoseconds and ensure every captured byte is present."""
    header = CaptureHeader.read(source, hard_limit)
    layout = Struct(header.order + "IIII")
    prior_tick = None
    index = 0
    while True:
        first = source.read(layout.size)
        if not first:
            break
        if len(first) != layout.size:
            raise ReplayError(f"record {index}: truncated PCAP packet header")
        seconds, fractions, captured, original = layout.unpack(first)
        if fractions >= header.divisor:
            raise ReplayError(f"record {index}: fractional timestamp exceeds one second")
        if not 0 < captured <= min(header.limit, original):
            raise ReplayError(f"record {index}: impossible captured length")
        absolute = seconds * 10**9 + fractions * header.tick_ns
        if require_monotonic and prior_tick is not None and absolute < prior_tick:
            raise ReplayError(f"record {index}: timestamps move backwards")
        data = read_exact(source, captured, f"record {index} packet payload")
        yield CapturePacket(index, absolute, original, data)
        prior_tick = absolute
        index += 1


@dataclass(frozen=True)
class LinkHeader:
    """EtherType and optional VLAN numbers from the outer Ethernet bytes."""

    ethertype: int
    vlans: tuple
    payload_offset: int


def inspect_link(data):
    """Decode an Ethernet header and any complete 802.1Q or 802.1ad tags."""
    if len(data) < 14:
        raise ReplayError("capture does not include a full Ethernet header")
    offset = 12
    kind = int.from_bytes(data[offset:offset+2], "big")
    vlans = []
    while kind in VLAN_TYPES:
        if len(data) < offset + 6:
            raise ReplayError("capture stopped in the middle of a VLAN tag")
        vlans.append(int.from_bytes(data[offset+2:offset+4], "big") & 0xFFF)
        offset += 4
        kind = int.from_bytes(data[offset:offset+2], "big")
    return LinkHeader(kind, tuple(vlans), offset+2)


@dataclass(frozen=True)
class CaptureSelection:
    """Select a chronological record subset before programming the tape."""

    ethertype: Optional[int] = None
    vlan: Optional[int] = None
    start_ns: Optional[int] = None
    end_ns: Optional[int] = None
    max_packets: Optional[int] = None
    truncated: str = "error"
    tag: str = "record"
    first_tag: int = 0

    def __post_init__(self):
        """Validate optional link/time filters, tag source and truncation policy."""
        if self.ethertype is not None:
            unsigned(self.ethertype, U16, "EtherType")
        if self.vlan is not None:
            unsigned(self.vlan, 4095, "VLAN")
        for boundary in (self.start_ns, self.end_ns):
            if boundary is not None and (type(boundary) is not int or boundary < 0):
                raise ReplayError("capture time bounds must be nonnegative integers")
        if self.end_ns is not None and self.start_ns is not None and self.end_ns < self.start_ns:
            raise ReplayError("capture end precedes its start")
        if self.max_packets is not None:
            unsigned(self.max_packets, U32, "max packets")
        if self.truncated not in ("reject", "error", "keep"):
            raise ReplayError("truncated records must be rejected, treated as errors, or retained")
        if self.tag not in ("record", "sequential", "outer-vlan"):
            raise ReplayError("invalid capture tag source")
        unsigned(self.first_tag, U16, "first tag")

    def time_matches(self, packet):
        """Check that the record is inside the inclusive requested time window."""
        return ((self.start_ns is None or packet.time_ns >= self.start_ns)
                and (self.end_ns is None or packet.time_ns <= self.end_ns))

    def needs_header(self):
        """Return whether tagging or filtering requires parsing Ethernet bytes."""
        return self.ethertype is not None or self.vlan is not None or self.tag == "outer-vlan"

    def header_matches(self, header):
        """Check EtherType and VLAN without requiring a transport-layer decoder."""
        return ((self.ethertype is None or header.ethertype == self.ethertype)
                and (self.vlan is None or self.vlan in header.vlans))

    def choose_tag(self, index, selected, header):
        """Return a checked 16-bit record, selected-position, or outer-VLAN tag."""
        if self.tag == "outer-vlan":
            return header.vlans[0] if header.vlans else self.first_tag
        origin = index if self.tag == "record" else selected
        return unsigned(self.first_tag+origin, U16, "derived capture tag")

    def select(self, packets):
        """Yield selected and tagged packets without changing their byte contents."""
        emitted = 0
        if self.max_packets == 0:
            return
        for packet in packets:
            if not self.time_matches(packet):
                continue
            if packet.truncated:
                if self.truncated == "error":
                    raise ReplayError(f"record {packet.position}: capture truncated the original frame")
                if self.truncated == "reject":
                    continue
            header = inspect_link(packet.payload) if self.needs_header() else None
            if header and not self.header_matches(header):
                continue
            tag = self.choose_tag(packet.position, emitted, header)
            yield StampedPacket(packet.time_ns, tag, packet.payload)
            emitted += 1
            if self.max_packets is not None and emitted >= self.max_packets:
                return


def import_pcap(source, selection=CaptureSelection(), *, hard_limit=1 << 20):
    """Return an iterator of replayable packets with exact capture timestamps."""
    return selection.select(packet_records(source, hard_limit=hard_limit))
