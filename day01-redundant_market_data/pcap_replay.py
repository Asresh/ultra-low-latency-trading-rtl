"""Extract dual-feed UDP payloads from an offline classic Ethernet PCAP."""

import argparse
from collections import Counter
from dataclasses import dataclass
import heapq
import ipaddress
import json
from pathlib import Path
import struct
import sys
import tempfile
from typing import NamedTuple

MAGICS = {
    b"\xd4\xc3\xb2\xa1": ("<", 1000),
    b"\xa1\xb2\xc3\xd4": (">", 1000),
    b"\x4d\x3c\xb2\xa1": ("<", 1),
    b"\xa1\xb2\x3c\x4d": (">", 1),
}
ETHERNET = 1
IPV4 = 0x0800
VLAN = {0x8100, 0x88A8, 0x9100}
UDP = 17
MAX_CAPTURE_SIZE = 16 * 1024 * 1024
NANOSECONDS = 1_000_000_000


class PcapError(ValueError):
    """Invalid capture container."""


class PacketDrop(ValueError):
    """Frame that the UDP replay interface cannot consume."""


@dataclass(frozen=True)
class Endpoint:
    address: bytes
    port: int

    @classmethod
    def parse(cls, text):
        host, delimiter, port = text.rpartition(":")
        if not delimiter:
            raise argparse.ArgumentTypeError("use an IPv4 destination and port, e.g. 239.1.2.3:9000")
        try:
            packed = ipaddress.IPv4Address(host).packed
            number = int(port)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
        if not 1 <= number <= 65535:
            raise argparse.ArgumentTypeError("UDP destination port must be in 1..65535")
        return cls(packed, number)


class Datagram(NamedTuple):
    destination: Endpoint
    data: bytes


class Beat(NamedTuple):
    cycle: int
    lane: int
    data: int
    keep: int
    first: int
    last: int


def exact_read(file, size, label):
    part = file.read(size)
    if len(part) != size:
        raise PcapError(f"truncated {label}, expected {size} bytes and found {len(part)}")
    return part


def read_capture(file):
    header = exact_read(file, 24, "PCAP header")
    try:
        byteorder, fraction_scale = MAGICS[header[:4]]
    except KeyError as exc:
        raise PcapError("expected classic PCAP with microsecond or nanosecond timestamps") from exc
    major, minor, _, _, snap_length, link_field = struct.unpack(byteorder + "HHIIII", header[4:])
    if (major, minor) != (2, 4):
        raise PcapError(f"unsupported PCAP version {major}.{minor}")
    if (link_field & 0xFFFF) != ETHERNET:
        raise PcapError("Ethernet link type is required")
    if not 0 < snap_length <= MAX_CAPTURE_SIZE:
        raise PcapError(f"snap length {snap_length} outside 1..{MAX_CAPTURE_SIZE}")
    number = 0
    while True:
        raw_header = file.read(16)
        if not raw_header:
            return
        number += 1
        if len(raw_header) != 16:
            raise PcapError(f"truncated record header at packet {number}")
        seconds, fraction, captured, original = struct.unpack(byteorder + "IIII", raw_header)
        if fraction >= NANOSECONDS // fraction_scale:
            raise PcapError(f"bad fractional timestamp at packet {number}")
        if captured > snap_length or captured > original:
            raise PcapError(f"invalid captured/original length at packet {number}")
        packet = exact_read(file, captured, f"Ethernet record {number}")
        yield seconds * NANOSECONDS + fraction * fraction_scale, packet


def header_checksum_valid(header):
    words = struct.unpack(f">{len(header) // 2}H", header)
    total = sum(words)
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return total == 0xFFFF


def udp_datagram(frame, verify_ipv4=False):
    if len(frame) < 14:
        raise PacketDrop("short ethernet")
    offset = 14
    ether_type = int.from_bytes(frame[12:14], "big")
    for _ in range(2):
        if ether_type not in VLAN:
            break
        if len(frame) < offset + 4:
            raise PacketDrop("short vlan")
        ether_type = int.from_bytes(frame[offset + 2:offset + 4], "big")
        offset += 4
    if ether_type != IPV4:
        raise PacketDrop("not ipv4 or more than two vlan tags")

    ip = frame[offset:]
    if len(ip) < 20:
        raise PacketDrop("short ipv4")
    if ip[0] >> 4 != 4:
        raise PacketDrop("invalid ipv4 version")
    header_length = (ip[0] & 15) * 4
    total_length = int.from_bytes(ip[2:4], "big")
    if header_length < 20 or header_length + 8 > total_length or total_length > len(ip):
        raise PacketDrop("bad ipv4 length")
    if ip[9] != UDP:
        raise PacketDrop("not udp")
    if int.from_bytes(ip[6:8], "big") & 0x3FFF:
        raise PacketDrop("ipv4 fragment")
    if verify_ipv4 and not header_checksum_valid(ip[:header_length]):
        raise PacketDrop("bad ipv4 checksum")
    udp = ip[header_length:total_length]
    _, port, udp_length, _ = struct.unpack(">HHHH", udp[:8])
    if udp_length != len(udp):
        raise PacketDrop("bad udp length")
    if udp_length == 8:
        raise PacketDrop("empty udp payload")
    return Datagram(Endpoint(ip[16:20], port), udp[8:])


def pack_beats(cycle, lane, payload):
    for offset in range(0, len(payload), 8):
        data = payload[offset:offset + 8]
        yield Beat(
            cycle + offset // 8, lane, int.from_bytes(data, "little"),
            (1 << len(data)) - 1, int(offset == 0), int(offset + 8 >= len(payload)),
        )


def render(beat):
    return f"{beat.cycle} {beat.lane} {beat.data:016x} {beat.keep:02x} {beat.first} {beat.last}\n"


def read_staging(file):
    # Each lane is already sorted. Only two records need to be held for the merge.
    for row in file:
        cycle, lane, *_ = row.split()
        yield int(cycle), int(lane), row


def schedule(packet_records, output, endpoints, clock_hz, gap_cycles=0, verify_ipv4=False):
    endpoints = tuple(endpoints)
    if len(endpoints) != 2 or endpoints[0] == endpoints[1]:
        raise ValueError("line A and B must have distinct destination endpoints")
    lanes = {endpoint: lane for lane, endpoint in enumerate(endpoints)}
    if clock_hz <= 0 or gap_cycles < 0:
        raise ValueError("clock must be positive and lane gap must be nonnegative")
    available = [0, 0]
    first_timestamp = None
    stats = Counter()
    with tempfile.SpooledTemporaryFile(mode="w+t", max_size=1 << 20, encoding="ascii") as a, \
         tempfile.SpooledTemporaryFile(mode="w+t", max_size=1 << 20, encoding="ascii") as b:
        staging = (a, b)
        for timestamp, packet in packet_records:
            stats["pcap_packets"] += 1
            try:
                endpoint, payload = udp_datagram(packet, verify_ipv4)
            except PacketDrop as exc:
                stats[f"drop:{exc}"] += 1
                continue
            if endpoint not in lanes:
                stats["drop:unselected endpoint"] += 1
                continue
            if first_timestamp is None:
                first_timestamp = timestamp
            lane = lanes[endpoint]
            delta = timestamp - first_timestamp
            if delta < 0:
                stats["timestamp_before_origin"] += 1
                delta = 0
            ideal_cycle = (delta * clock_hz + NANOSECONDS - 1) // NANOSECONDS
            cycle = max(ideal_cycle, available[lane])
            if cycle > ideal_cycle:
                stats[f"lane{lane}_scheduled_late"] += 1
            stats[f"lane{lane}_packets"] += 1
            stats[f"lane{lane}_payload_bytes"] += len(payload)
            for beat in pack_beats(cycle, lane, payload):
                staging[lane].write(render(beat))
                stats[f"lane{lane}_beats"] += 1
                available[lane] = beat.cycle + 1 + gap_cycles
        for file in staging:
            file.seek(0)
        output.write("# cycle lane data_hex keep_hex first last\n")
        for _, _, row in heapq.merge(*map(read_staging, staging)):
            output.write(row)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--lane-a", type=Endpoint.parse, required=True, metavar="IP:PORT")
    parser.add_argument("--lane-b", type=Endpoint.parse, required=True, metavar="IP:PORT")
    parser.add_argument("--clock-hz", type=int, default=156_250_000)
    parser.add_argument("--gap-cycles", type=int, default=0)
    parser.add_argument("--verify-ipv4", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    endpoints = (args.lane_a, args.lane_b)
    try:
        if args.output and (
            args.output.resolve() == args.capture.resolve() or
            (args.output.exists() and args.capture.exists() and args.output.samefile(args.capture))
        ):
            parser.error("capture and output must be different files")
        with args.capture.open("rb") as capture:
            if args.output is None:
                stats = schedule(read_capture(capture), sys.stdout, endpoints,
                                 args.clock_hz, args.gap_cycles, args.verify_ipv4)
            else:
                with args.output.open("w", encoding="ascii") as output:
                    stats = schedule(read_capture(capture), output, endpoints,
                                     args.clock_hz, args.gap_cycles, args.verify_ipv4)
    except (OSError, PcapError, ValueError) as exc:
        print(f"pcap_replay: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(dict(stats), sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
