"""Encode and audit Ethernet II ARP at the MAC-client boundary."""

import argparse
from dataclasses import dataclass
from enum import IntEnum
from ipaddress import IPv4Address
from pathlib import Path
from struct import Struct
from typing import Optional


ARP = 0x0806
VLAN = 0x8100
ETHERNET_IPV4_ARP = Struct("!HHBBH6s4s6s4s")
BROADCAST = bytes.fromhex("ffffffffffff")
ZERO_MAC = bytes(6)
LOCAL_MAC = bytes.fromhex("02000000000a")
LOCAL_IP = IPv4Address("192.0.2.10")
PEER_MAC = bytes.fromhex("020000000014")
PEER_IP = IPv4Address("192.0.2.20")
NEXT_IP = IPv4Address("192.0.2.30")
THIRD_IP = IPv4Address("192.0.2.40")


class Operation(IntEnum):
    REQUEST = 1
    REPLY = 2


def require_mac(address):
    """Keep binary MAC addresses exactly six bytes wide."""
    if not isinstance(address, bytes) or len(address) != 6:
        raise ValueError("MAC address must be six bytes")


@dataclass(frozen=True)
class ArpFrame:
    """Represent the wire fields and preserve a full VLAN TCI for a response."""

    destination: bytes
    source: bytes
    operation: Operation
    sender_mac: bytes
    sender_ip: IPv4Address
    target_mac: bytes
    target_ip: IPv4Address
    tci: Optional[int] = None

    def __post_init__(self):
        for address in (self.destination, self.source, self.sender_mac, self.target_mac):
            require_mac(address)
        if not isinstance(self.operation, Operation):
            raise ValueError("ARP operation must be request or reply")
        if any(not isinstance(address, IPv4Address) for address in (self.sender_ip, self.target_ip)):
            raise ValueError("ARP IP addresses must be IPv4")
        if self.tci is not None and (
            type(self.tci) is not int or not 0 <= self.tci <= 0xFFFF or self.tci & 0xFFF == 0xFFF
        ):
            raise ValueError("TCI must fit in 16 bits and carry a usable VLAN ID")

    @property
    def vid(self):
        """Represent untagged traffic distinctly from a priority-tagged frame."""
        return None if self.tci is None else self.tci & 0xFFF

    @classmethod
    def request(cls, source, sender, target, vid=None):
        if vid is not None and (type(vid) is not int or not 0 <= vid < 0xFFF):
            raise ValueError("probe VLAN ID must be in 0..4094")
        return cls(BROADCAST, source, Operation.REQUEST, source, sender, ZERO_MAC, target, vid)

    def answer(self, local_mac):
        """Build the response to a previously validated request."""
        if self.operation != Operation.REQUEST:
            raise ValueError("only a request has an ARP answer")
        return ArpFrame(
            self.sender_mac, local_mac, Operation.REPLY, local_mac, self.target_ip,
            self.sender_mac, self.sender_ip, self.tci,
        )

    def encode(self, pad=True):
        header = self.destination + self.source
        if self.tci is not None:
            header += VLAN.to_bytes(2, "big") + self.tci.to_bytes(2, "big")
        header += ARP.to_bytes(2, "big")
        payload = ETHERNET_IPV4_ARP.pack(
            1, 0x0800, 6, 4, self.operation, self.sender_mac, self.sender_ip.packed,
            self.target_mac, self.target_ip.packed,
        )
        frame = header + payload
        return frame.ljust(60, b"\0") if pad else frame

    @classmethod
    def decode(cls, frame):
        if len(frame) < 14:
            raise ValueError("Ethernet header truncated")
        ether_type = int.from_bytes(frame[12:14], "big")
        tci = None
        offset = 14
        if ether_type == VLAN:
            if len(frame) < 18:
                raise ValueError("VLAN header truncated")
            tci = int.from_bytes(frame[14:16], "big")
            ether_type = int.from_bytes(frame[16:18], "big")
            offset = 18
        if ether_type != ARP:
            raise ValueError("EtherType is not ARP")
        limit = offset + ETHERNET_IPV4_ARP.size
        if len(frame) < limit:
            raise ValueError("ARP payload truncated")
        hw_type, proto_type, hw_len, proto_len, op, sha, spa, tha, tpa = ETHERNET_IPV4_ARP.unpack(
            frame[offset:limit]
        )
        if (hw_type, proto_type, hw_len, proto_len) != (1, 0x0800, 6, 4):
            raise ValueError("unexpected ARP protocol or address geometry")
        return cls(
            frame[:6], frame[6:12], Operation(op), sha, IPv4Address(spa), tha, IPv4Address(tpa), tci,
        )


def directed_frames():
    """Return the expected frame sequence for the integration testbench."""
    tagged = ArpFrame.request(LOCAL_MAC, LOCAL_IP, PEER_IP, 42)
    untagged = ArpFrame.request(LOCAL_MAC, LOCAL_IP, PEER_IP)
    tagged_input = ArpFrame(
        BROADCAST, PEER_MAC, Operation.REQUEST, PEER_MAC, PEER_IP, ZERO_MAC, LOCAL_IP, 0xA02A,
    )
    second_target = ArpFrame.request(LOCAL_MAC, LOCAL_IP, NEXT_IP, 12)
    third_target = ArpFrame.request(LOCAL_MAC, LOCAL_IP, THIRD_IP)
    return (
        tagged, untagged, tagged_input.answer(LOCAL_MAC), untagged, second_target,
        untagged, second_target, third_target, third_target,
    )


def audit_capture(lines, expected):
    """Compare exact wire bytes while providing a useful first mismatch report."""
    captures = tuple(lines)
    if len(captures) != len(expected):
        raise ValueError(f"captured {len(captures)} packets, expected {len(expected)}")
    for pos, (line, packet) in enumerate(zip(captures, expected), 1):
        try:
            actual = bytes.fromhex(line)
            decoded = ArpFrame.decode(actual)
        except ValueError as exc:
            raise ValueError(f"packet {pos}: {exc}") from exc
        wanted = packet.encode()
        if actual == wanted:
            continue
        if len(actual) != len(wanted):
            raise ValueError(f"packet {pos}: wire length {len(actual)}, expected {len(wanted)}")
        first_change = next(index for index, (a, b) in enumerate(zip(actual, wanted)) if a != b)
        raise ValueError(
            f"packet {pos}: byte {first_change} differs ({actual[first_change]:02x} vs "
            f"{wanted[first_change]:02x}), decoded={decoded}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="one hexadecimal MAC frame per line")
    args = parser.parse_args()
    try:
        frames = directed_frames()
        audit_capture(args.capture.read_text(encoding="ascii").splitlines(), frames)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"ARP capture: {exc}\n")
    print(f"PASS {len(frames)} byte-exact ARP request/reply frames and zero Ethernet padding")


if __name__ == "__main__":
    main()
