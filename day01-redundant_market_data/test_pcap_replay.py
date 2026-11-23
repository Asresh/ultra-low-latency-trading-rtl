"""Construct byte-exact Ethernet/UDP captures for the offline replay tool."""

import io
import struct
import unittest

from pcap_replay import (
    Endpoint, MAGICS, PacketDrop, PcapError, header_checksum_valid,
    pack_beats, read_capture, schedule, udp_datagram,
)


A = Endpoint.parse("239.1.2.3:12000")
B = Endpoint.parse("239.1.2.4:12001")
OTHER = Endpoint.parse("239.1.2.5:12002")
PAYLOAD = b"0123456789ABC"


def ipv4_checksum(data):
    total = 0
    for index in range(0, len(data), 2):
        total += (data[index] << 8) | data[index + 1]
        total = (total >> 16) + (total & 65535)
    return (~total) & 65535


def packet(endpoint, payload=PAYLOAD, tags=(), opts=b"", flags=0, protocol=17, checksum=True, padding=0):
    udp = struct.pack(">HHHH", 8000, endpoint.port, 8 + len(payload), 0) + payload
    ip = struct.pack(
        ">BBHHHBBH4s4s", 0x45 + len(opts) // 4, 0, 20 + len(opts) + len(udp), 5,
        flags, 64, protocol, 0, b"\x7f\x00\x00\x01", endpoint.address,
    ) + opts
    if checksum:
        ip = ip[:10] + struct.pack(">H", ipv4_checksum(ip)) + ip[12:]
    ethernet = bytes(12) + b"".join(struct.pack(">HH", tag, 90) for tag in tags) + b"\x08\x00"
    return ethernet + ip + udp + bytes(padding)


def pcap(records, magic=b"\xd4\xc3\xb2\xa1", snap_length=65535, link=1):
    order, _ = MAGICS[magic]
    data = magic + struct.pack(order + "HHIIII", 2, 4, 0, 0, snap_length, link)
    for second, fraction, frame in records:
        data += struct.pack(order + "IIII", second, fraction, len(frame), len(frame)) + frame
    return data


class PcapInputTests(unittest.TestCase):
    def test_four_pcaps_convert_time(self):
        for magic, (_, scale) in MAGICS.items():
            with self.subTest(magic=magic.hex()):
                wire = packet(A)
                records = list(read_capture(io.BytesIO(pcap([(17, 65, wire)], magic))))
                self.assertEqual(records, [(17_000_000_000 + 65 * scale, wire)])

    def test_container_errors_are_distinguished(self):
        good = pcap([(0, 0, packet(A))])
        cases = {
            b"a": "truncated PCAP header",
            b"\x0a\x0d\x0d\x0a" + good[4:]: "classic PCAP",
            pcap([], link=101): "Ethernet link type",
            pcap([], snap_length=0): "snap length",
            good[:30]: "record header",
            good[:-1]: "Ethernet record",
            pcap([(0, 1_000_000, packet(A))]): "fractional timestamp",
            pcap([(0, 0, packet(A))], snap_length=12): "captured/original length",
        }
        for stream, message in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(PcapError, message):
                    list(read_capture(io.BytesIO(stream)))


class UdpExtractionTests(unittest.TestCase):
    def test_ip_options_double_vlan_and_padding(self):
        frame = packet(A, tags=(0x88A8, 0x8100), opts=b"\x01\x01\x00\x00", padding=9)
        self.assertTrue(header_checksum_valid(frame[22:46]))
        self.assertEqual(udp_datagram(frame, True), (A, PAYLOAD))

    def test_fragments_and_malformed_packets_do_not_emit_payload(self):
        bad_udp_len = bytearray(packet(A))
        bad_udp_len[38:40] = b"\x00\x08"
        bad_ip_len = bytearray(packet(A))
        bad_ip_len[16:18] = b"\xff\xff"
        wrong_version = bytearray(packet(A))
        wrong_version[14] = 0x65
        cases = [
            (packet(A, flags=0x2000), "fragment"),
            (packet(A, flags=1), "fragment"),
            (packet(A, protocol=6), "not udp"),
            (packet(A, tags=(0x88A8, 0x8100, 0x8100)), "vlan tags"),
            (packet(A, b""), "empty udp"),
            (packet(A)[:10], "short ethernet"),
            (packet(A)[:24], "short ipv4"),
            (packet(A, tags=(0x8100,))[:16], "short vlan"),
            (bad_udp_len, "bad udp length"),
            (bad_ip_len, "bad ipv4 length"),
            (wrong_version, "invalid ipv4 version"),
            (packet(A, checksum=False), "bad ipv4 checksum"),
        ]
        for frame, failure in cases:
            with self.subTest(failure=failure):
                with self.assertRaisesRegex(PacketDrop, failure):
                    udp_datagram(frame, True)
        self.assertEqual(udp_datagram(packet(A, checksum=False)), (A, PAYLOAD))

    def test_df_flag_does_not_indicate_fragment(self):
        self.assertEqual(udp_datagram(packet(B, flags=0x4000)), (B, PAYLOAD))


class SchedulerTests(unittest.TestCase):
    def test_data_lane_and_keep_alignment(self):
        beats = list(pack_beats(3, 1, PAYLOAD))
        self.assertEqual(beats[0], (3, 1, 0x3736353433323130, 0xFF, 1, 0))
        self.assertEqual(beats[1], (4, 1, 0x4342413938, 0x1F, 0, 1))
        self.assertEqual(list(pack_beats(8, 0, b"\x12")), [(8, 0, 0x12, 1, 1, 1)])

    def test_independent_lane_backlogs_merge_in_cycle_order(self):
        output = io.StringIO()
        records = [
            (100, packet(OTHER)), (103, packet(A)), (103, packet(A, b"Z")),
            (103, packet(B)), (104, packet(B, b"W")),
            (102, packet(B, b"B")), (110, packet(A, b"Q", flags=1)),
        ]
        stats = schedule(records, output, (A, B), 1_000_000_000, gap_cycles=1)
        trace = output.getvalue().splitlines()[1:]
        columns = [entry.split() for entry in trace]
        self.assertEqual([(row[0], row[1]) for row in columns],
                         [("0", "0"), ("0", "1"), ("1", "0"), ("1", "1"),
                          ("3", "0"), ("3", "1"), ("5", "1")])
        self.assertEqual(stats["drop:unselected endpoint"], 1)
        self.assertEqual(stats["drop:ipv4 fragment"], 1)
        self.assertEqual(stats["lane0_scheduled_late"], 1)
        self.assertEqual(stats["timestamp_before_origin"], 1)
        self.assertEqual(stats["lane0_payload_bytes"], len(PAYLOAD) + 1)

    def test_clock_rounds_up_to_next_available_edge(self):
        output = io.StringIO()
        schedule([(0, packet(A, b"A")), (1, packet(B, b"B"))],
                 output, (A, B), 156_250_000)
        self.assertTrue(output.getvalue().splitlines()[-1].startswith("1 1 "))

    def test_bad_scheduler_controls(self):
        with self.assertRaisesRegex(ValueError, "distinct"):
            schedule([], io.StringIO(), (A, A), 250_000_000)
        with self.assertRaisesRegex(ValueError, "clock"):
            schedule([], io.StringIO(), (A, B), 0)
        with self.assertRaisesRegex(ValueError, "gap"):
            schedule([], io.StringIO(), (A, B), 250_000_000, gap_cycles=-1)


if __name__ == "__main__":
    unittest.main()
