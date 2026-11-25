"""Build and independently audit the combined Ethernet, ARP and UDP fixture."""

from argparse import ArgumentParser
from collections import Counter
from dataclasses import replace
from ipaddress import IPv4Address
from pathlib import Path
import json

from arp_reference import ArpFrame, LOCAL_IP, LOCAL_MAC, PEER_IP, PEER_MAC, Operation
from net_wire import MacBeat, Packet, Result, Rule, TxDescriptor, from_beats, parse_frame, to_beats


GATEWAY_25 = IPv4Address("192.0.2.30")
GATEWAY_MAC = bytes.fromhex("02000000001e")
REMOTE = IPv4Address("198.51.100.1")
BLOCKED = IPv4Address("198.51.100.99")
LOCAL_MISSING = IPv4Address("192.0.2.77")
GROUP = IPv4Address("239.129.2.3")
QINQ = ((0x88a8, 0x6018), (0x8100, 0x2007))


def integer_mac(data):
    return int.from_bytes(data, "big")


def multicast_mac(ip):
    """Ethernet mapping in RFC 1112 section 6.4."""
    return 0x01005E000000 + (int(ip) % (1 << 23))


def write_beats(path, data, bad=False):
    rows = to_beats(data, bad=bad) if data else []
    path.write_text("".join(row.row() + "\n" for row in rows))


def arp_reply(peer_ip, peer_mac, tci):
    return ArpFrame(LOCAL_MAC, peer_mac, Operation.REPLY, peer_mac,
                    peer_ip, LOCAL_MAC, LOCAL_IP, tci).encode()


def build(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "permit.rule").write_text(f"{Rule(queue=5).encode():064x}\n")
    ingress_packet = Packet(integer_mac(LOCAL_MAC), integer_mac(PEER_MAC), int(PEER_IP),
                            int(LOCAL_IP), 18000, 19000, b"book-fast", QINQ, 0x7001)
    stacked_arp = ArpFrame.request(PEER_MAC, PEER_IP, LOCAL_IP, 7).encode()
    peer_probe = ArpFrame.request(PEER_MAC, PEER_IP, LOCAL_IP, 0)

    inbound = {
        "wrong_vlan": arp_reply(PEER_IP, PEER_MAC, 25),
        "gateway_24": arp_reply(PEER_IP, PEER_MAC, 24),
        "gateway_25": arp_reply(GATEWAY_25, GATEWAY_MAC, 25),
        "peer_probe": peer_probe.encode(),
        "ipv4": ingress_packet.serialize(),
        "bad_ipv4": ingress_packet.serialize(),
        "other": b"\xff"*12 + bytes.fromhex("86dd") + b"\x12"*46,
        "short": bytes(range(13)),
        "stacked_arp": stacked_arp[:12] + bytes.fromhex("88a80018") + stacked_arp[12:],
        "triple_vlan": ingress_packet.serialize()[:12] + bytes.fromhex("81000002") +
                       ingress_packet.serialize()[12:],
    }
    for name, packet in inbound.items():
        write_beats(directory / f"{name}.rx", packet, bad=(name == "bad_ipv4"))
    write_beats(directory / "ipv4.expected", ingress_packet.payload)

    expected = [
        (0, ArpFrame.request(LOCAL_MAC, LOCAL_IP, PEER_IP, 24).encode()),
        (0, ArpFrame.request(LOCAL_MAC, LOCAL_IP, GATEWAY_25, 25).encode()),
        (0, peer_probe.answer(LOCAL_MAC).encode()),
        (0, ArpFrame.request(LOCAL_MAC, LOCAL_IP, LOCAL_MISSING, 24).encode()),
        (0, ArpFrame.request(LOCAL_MAC, LOCAL_IP, LOCAL_MISSING, 24).encode()),
    ]

    def descriptor(name, cookie, ip, payload, tags, accepted_on=None,
                   mac=None, ttl=64, length=None, ident=99):
        packet = Packet(0x123456789012, 0xFEDCBA987654, 0xDEADBEEF, int(ip),
                        5001, 5101, payload, tags, ident=ident, ttl=ttl, tos=0x18)
        encoded = TxDescriptor(packet, cookie, payload_size=length).encode()
        (directory / f"{name}.desc").write_text(f"{encoded:080x}\n")
        write_beats(directory / f"{name}.body", payload)
        if accepted_on is not None:
            wire = replace(packet, dest_mac=mac, src_mac=integer_mac(LOCAL_MAC),
                           src_ip=int(LOCAL_IP)).serialize()
            expected.append((accepted_on, wire))

    vlan24 = ((0x8100, 0xA018),)
    vlan25 = ((0x8100, 25),)
    descriptor("routed", 0x100, REMOTE, b"recovery-request", vlan24,
               accepted_on=1, mac=integer_mac(PEER_MAC), ident=0xBAD)
    descriptor("group", 0x100, GROUP, b"multicast reporting", (),
               accepted_on=2, mac=multicast_mac(GROUP), ident=0x482)
    descriptor("cached", 0x101, REMOTE, b"repeat", vlan24,
               accepted_on=1, mac=integer_mac(PEER_MAC), ident=0xB04)
    descriptor("epoch", 0x102, REMOTE, b"abandoned-epoch", vlan25)
    descriptor("blocked", 0x103, BLOCKED, b"never on wire", vlan24)
    descriptor("group_denied", 0x104, GROUP, b"", ())
    descriptor("miss", 0x105, REMOTE, b"", ())
    descriptor("bad_vid", 0x106, GROUP, b"", ((0x8100, 0xFFF),))
    descriptor("timeout", 0x107, LOCAL_MISSING, b"unreachable", vlan24)
    descriptor("bad_shape", 0x108, REMOTE, b"two tags not supported here", QINQ)
    descriptor("bad_body", 0x109, GROUP, b"short by one", (), length=13)

    # Expected complete frames form a multiset. Arbitration owns timing and
    # allowed inter-frame order, whereas each frame's source and bytes are exact.
    (directory / "wire_expect.json").write_text(json.dumps(
        [[lane, wire.hex()] for lane, wire in expected], indent=2) + "\n")


def read_trace(path):
    """Read handshaken 64-bit beats, rejecting interleaved or torn packets."""
    frames = []
    previous_cycle = -1
    frame_lane = None
    beats = []
    with path.open() as stream:
        for line in stream:
            cycle_raw, lane_raw, row = line.split(maxsplit=2)
            cycle, lane = int(cycle_raw), int(lane_raw)
            if cycle <= previous_cycle:
                raise ValueError("accepted MAC transfers must have strictly increasing cycles")
            if lane not in (0, 1, 2):
                raise ValueError("unrecognized physical source lane")
            previous_cycle = cycle
            beat = MacBeat.parse(row)
            if beat.first:
                if beats:
                    raise ValueError("a new physical packet interrupted an unfinished frame")
                frame_lane = lane
            if lane != frame_lane or not beats and not beat.first:
                raise ValueError("MAC ownership changed mid-frame")
            beats.append(beat)
            if beat.last:
                frame, result = from_beats(beats)
                if result != Result.OK:
                    raise ValueError(f"invalid MAC output framing: {result}")
                frames.append((lane, frame.hex()))
                beats.clear()
                frame_lane = None
    if beats:
        raise ValueError("trace ended with an open frame")
    return frames


def audit(directory, trace):
    expected = Counter(tuple(row) for row in json.loads(
        (directory / "wire_expect.json").read_text()))
    observed = Counter(read_trace(trace))
    if expected != observed:
        raise ValueError(f"wire frame mismatch, missing={expected-observed}, extra={observed-expected}")
    for lane, wire in observed:
        frame = bytes.fromhex(wire)
        if lane == 0:
            ArpFrame.decode(frame)
        elif parse_frame(frame).result != Result.OK:
            raise ValueError("expected UDP capture must independently parse with a valid checksum")
    print(f"PASS routed wire audit: {sum(observed.values())} frames, "
          f"{sum(count for (lane, _), count in observed.items() if lane == 0)} ARP")


def main():
    parser = ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_command = commands.add_parser("build")
    build_command.add_argument("directory", type=Path)
    audit_command = commands.add_parser("audit")
    audit_command.add_argument("directory", type=Path)
    audit_command.add_argument("trace", type=Path)
    options = parser.parse_args()
    if options.command == "build":
        build(options.directory)
    else:
        audit(options.directory, options.trace)


if __name__ == "__main__":
    main()
