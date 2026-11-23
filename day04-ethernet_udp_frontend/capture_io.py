"""Read bounded classic pcap and pcapng without third-party libraries."""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import struct


NANOSECOND = 1_000_000_000
DLT_ETHERNET = 1
DEFAULT_LIMIT = 16 << 20
NG_SHB = b"\x0a\x0d\x0d\x0a"
NG_MAGIC = {b"\x1a\x2b\x3c\x4d": ">", b"\x4d\x3c\x2b\x1a": "<"}
CLASSIC_MAGIC = {
    b"\xa1\xb2\xc3\xd4": (">",1_000_000),
    b"\xd4\xc3\xb2\xa1": ("<",1_000_000),
    b"\xa1\xb2\x3c\x4d": (">",NANOSECOND),
    b"\x4d\x3c\xb2\xa1": ("<",NANOSECOND),
}


class CaptureError(ValueError):
    pass


def check_output_paths(inputs, outputs):
    """Reject aliases between input and writable paths before a tool opens output files."""
    occupied = [path for path in inputs if path is not None]
    for output in outputs:
        if output is None:
            continue
        for path in occupied:
            if output.resolve() == path.resolve() or (
                output.exists() and path.exists() and output.samefile(path)
            ):
                raise ValueError(f"output path {output} aliases {path}")
        occupied.append(output)


@dataclass(frozen=True)
class CapturePacket:
    serial: int
    section: int
    interface: int
    name: str
    link_type: int
    capture_time_ns: int
    wire_length: int
    captured: bytes
    fcs_bytes: int = 0

    @property
    def truncated(self):
        """Report when the capture saved fewer bytes than the original packet length."""
        return self.wire_length != len(self.captured)

    @property
    def key(self):
        """Identify an interface by both its section and interface number."""
        return (self.section, self.interface)


@dataclass(frozen=True)
class Interface:
    link_type: int
    snaplen: int
    name: str = ""
    resolution: int = 1_000_000
    time_offset: int = 0
    fcs_bytes: int = 0

    def time_in_ns(self, ticks):
        """Convert device ticks to an exact nanosecond fraction with the interface offset."""
        return (ticks*NANOSECOND)//self.resolution + self.time_offset*NANOSECOND


def read_exact(handle, length, label):
    """Read a complete field or raise a diagnostic on premature end of file."""
    data = handle.read(length)
    if len(data) != length:
        raise CaptureError(f"truncated {label}: need {length} bytes, got {len(data)}")
    return data


def read_optional(handle, length, label):
    """Distinguish a clean record boundary at EOF from a partially read field."""
    data = handle.read(length)
    if not data:
        return None
    if len(data) != length:
        raise CaptureError(f"truncated {label}: need {length} bytes, got {len(data)}")
    return data


def aligned_length(size):
    """Round a data size up to its four-byte storage boundary."""
    return (size+3) & ~3


def decode_options(blob, endian):
    """Decode and check option headers, padding, terminator, and values."""
    offset = 0
    while offset < len(blob):
        if offset+4 > len(blob):
            raise CaptureError("truncated pcapng option header")
        code, length = struct.unpack_from(endian+"HH", blob, offset)
        offset += 4
        if offset+aligned_length(length) > len(blob):
            raise CaptureError("truncated pcapng option value")
        if code == 0:
            if length != 0:
                raise CaptureError("end-of-options must have zero data length")
            if any(blob[offset:]):
                raise CaptureError("nonzero bytes after pcapng end-of-options")
            return
        yield code, blob[offset:offset+length]
        offset += aligned_length(length)


def decode_interface(blob, endian):
    """Parse the declared link type and clock properties for one interface."""
    if len(blob) < 8:
        raise CaptureError("truncated pcapng interface description")
    link_type, _, snaplen = struct.unpack_from(endian+"HHI", blob)
    values = dict(link_type=link_type, snaplen=snaplen)
    seen = set()
    for option, value in decode_options(blob[8:], endian):
        if option not in (2, 9, 13, 14):
            continue
        if option in seen:
            raise CaptureError(f"repeated interface option {option}")
        seen.add(option)
        if option == 2:
            values["name"] = value.decode("utf-8", errors="replace")
        elif option == 9:
            if len(value) != 1:
                raise CaptureError("if_tsresol expects one octet")
            exponent = value[0] & 127
            radix = 2 if value[0] & 128 else 10
            values["resolution"] = radix**exponent
        elif option == 13:
            if len(value) != 1 or value[0] % 8:
                raise CaptureError("if_fcslen must specify whole bytes")
            values["fcs_bytes"] = value[0]//8
        elif option == 14:
            if len(value) != 8:
                raise CaptureError("if_tsoffset expects a signed 64-bit integer")
            values["time_offset"], = struct.unpack(endian+"q", value)
    return Interface(**values)


def decode_enhanced(blob, endian, interfaces, serial, section):
    """Read an enhanced packet using its section's interface metadata."""
    if len(blob) < 20:
        raise CaptureError("truncated pcapng enhanced packet fields")
    iface, high, low, caplen, wirelen = struct.unpack_from(endian+"5I", blob)
    if iface >= len(interfaces):
        raise CaptureError("pcapng packet references unknown interface")
    config = interfaces[iface]
    if caplen > wirelen or (config.snaplen and caplen > config.snaplen):
        raise CaptureError("pcapng captured length exceeds reported limit")
    padded_size = aligned_length(caplen)
    if 20+padded_size > len(blob):
        raise CaptureError("pcapng data is shorter than the recorded captured length")
    options = list(decode_options(blob[20+padded_size:], endian))
    fcs_bytes = config.fcs_bytes
    for code, value in options:
        if code == 2:
            if len(value) != 4:
                raise CaptureError("pcapng EPB flags expect four bytes")
            epb_flags, = struct.unpack(endian+"I", value)
            packet_fcs = (epb_flags >> 5) & 15
            if packet_fcs:
                fcs_bytes = packet_fcs
    timing = config.time_in_ns((high << 32) | low)
    return CapturePacket(serial, section, iface, config.name, config.link_type,
                         timing, wirelen, blob[20:20+caplen], fcs_bytes)


def decode_simple(blob, endian, interfaces, serial, section):
    """Recover captured bytes for the first declared interface without a timestamp."""
    if len(blob) < 4 or not interfaces:
        raise CaptureError("pcapng simple packet requires interface zero and original length")
    wirelen, = struct.unpack_from(endian+"I", blob)
    config = interfaces[0]
    caplen = min(wirelen, config.snaplen) if config.snaplen else wirelen
    if len(blob) != 4+aligned_length(caplen):
        raise CaptureError("pcapng simple packet size disagrees with interface snap length")
    return CapturePacket(serial, section, 0, config.name, config.link_type,
                         None, wirelen, blob[4:4+caplen], config.fcs_bytes)


def ng_block_body(handle, endian, raw_total, label, *, already_read=0, limit=DEFAULT_LIMIT):
    """Read a bounded PCAPng body and verify the trailing repeated size."""
    total, = struct.unpack(endian+"I", raw_total)
    if total < 12+already_read or total % 4 or total > limit:
        raise CaptureError(f"invalid {label} block length {total}")
    rest = read_exact(handle, total-8-already_read, label)
    if rest[-4:] != raw_total:
        raise CaptureError(f"{label} leading and trailing block lengths disagree")
    return rest[:-4]


def pcapng_packets(handle, block_type, limit):
    """Iterate sections, switch byte order, and resolve each packet's interface."""
    endian = ""
    interfaces = []
    section = -1
    serial = 0
    while block_type is not None:
        raw_total = read_exact(handle, 4, "pcapng block length")
        if block_type == NG_SHB:
            marker = read_exact(handle, 4, "pcapng byte order marker")
            if marker not in NG_MAGIC:
                raise CaptureError("unknown pcapng byte order marker")
            endian = NG_MAGIC[marker]
            fields = ng_block_body(handle, endian, raw_total, "section header",
                                   already_read=4, limit=limit)
            if len(fields) < 12:
                raise CaptureError("truncated pcapng section version or length")
            major, _, section_length = struct.unpack_from(endian+"HHq", fields)
            if major != 1 or section_length < -1:
                raise CaptureError("unsupported pcapng section version or length field")
            list(decode_options(fields[12:], endian))
            interfaces = []
            section += 1
        else:
            if not endian:
                raise CaptureError("pcapng block precedes section header")
            numeric_type, = struct.unpack(endian+"I", block_type)
            data = ng_block_body(handle, endian, raw_total, f"block {numeric_type}", limit=limit)
            if numeric_type == 1:
                interfaces.append(decode_interface(data, endian))
            elif numeric_type == 6:
                yield decode_enhanced(data, endian, interfaces, serial, section)
                serial += 1
            elif numeric_type == 3:
                yield decode_simple(data, endian, interfaces, serial, section)
                serial += 1
        block_type = read_optional(handle, 4, "pcapng block type")


def pcap_packets(handle, magic, limit):
    """Decode classic PCAP packet records in their recorded time unit and endian."""
    endian, fractions_per_second = CLASSIC_MAGIC[magic]
    header = read_exact(handle, 20, "pcap file header")
    version_major, version_minor, _, _, snaplen, network = struct.unpack(endian+"HHiiii", header)
    if (version_major,version_minor) != (2,4) or snaplen <= 0:
        raise CaptureError("unsupported pcap version or snapshot length")
    # Bits [15:0] contain the link type. The pcap FCS extension indicates
    # the length in 16-bit words when its presence bit is set.
    fcs_bytes = ((network >> 28) & 15)*2 if (network >> 26) & 1 else 0
    packet_header = struct.Struct(endian+"4I")
    serial = 0
    while True:
        entry = read_optional(handle, packet_header.size, "pcap record header")
        if entry is None:
            break
        seconds, fraction, captured, original = packet_header.unpack(entry)
        if fraction >= fractions_per_second:
            raise CaptureError("pcap timestamp fraction exceeds one second")
        if captured > min(snaplen, original, limit):
            raise CaptureError("pcap captured length exceeds reported limit")
        payload = read_exact(handle, captured, "pcap packet bytes")
        timestamp = seconds*NANOSECOND + (fraction*NANOSECOND)//fractions_per_second
        yield CapturePacket(serial,0,0,"",network & 65535,timestamp,original,payload,fcs_bytes)
        serial += 1


def packets_from(handle, limit=DEFAULT_LIMIT):
    """Detect the capture container from its initial magic before decoding it."""
    if limit < 64:
        raise ValueError("reader limit must be at least 64 bytes")
    magic = read_exact(handle, 4, "capture signature")
    if magic == NG_SHB:
        yield from pcapng_packets(handle, magic, limit)
    elif magic in CLASSIC_MAGIC:
        yield from pcap_packets(handle, magic, limit)
    else:
        raise CaptureError("unsupported capture signature")


def packets(path, limit=DEFAULT_LIMIT):
    """Open a capture path and yield packet records while owning the file lifetime."""
    with Path(path).open("rb") as handle:
        yield from packets_from(handle, limit=limit)


def tick_period_ns(frequency_hz):
    """Express the replay clock period as an exact rational number of nanoseconds."""
    if frequency_hz <= 0:
        raise ValueError("frequency must be positive")
    return Fraction(NANOSECOND, frequency_hz)
