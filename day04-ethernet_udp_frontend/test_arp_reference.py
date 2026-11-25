"""Known-answer and negative tests for the ARP capture auditor."""

from dataclasses import replace
from ipaddress import IPv4Address
import unittest

from arp_reference import ArpFrame, LOCAL_IP, LOCAL_MAC, PEER_IP, audit_capture, directed_frames


class ArpTests(unittest.TestCase):
    def test_untagged_rfc_fields(self):
        request = ArpFrame.request(LOCAL_MAC, LOCAL_IP, PEER_IP)
        literal = (
            "ffffffffffff02000000000a0806"
            "0001080006040001"
            "02000000000ac000020a000000000000c0000214"
        )
        self.assertEqual(request.encode(pad=False).hex(), literal)
        self.assertEqual(request.encode().hex(), literal + "00" * 18)
        self.assertEqual(ArpFrame.decode(bytes.fromhex(literal)), request)

    def test_tci_is_reflected_in_reply(self):
        response = directed_frames()[2]
        actual = response.encode()
        self.assertEqual(actual[12:18].hex(), "8100a02a0806")
        self.assertEqual(actual[24:26], b"\x00\x02")
        self.assertEqual(actual[46:], bytes(14))
        self.assertEqual(ArpFrame.decode(actual), response)

    def test_priority_tagged_zero_is_distinct_from_untagged(self):
        request = ArpFrame.request(LOCAL_MAC, LOCAL_IP, PEER_IP, 0)
        self.assertEqual(ArpFrame.decode(request.encode()).vid, 0)
        self.assertIsNone(ArpFrame.decode(replace(request, tci=None).encode()).vid)

    def test_reserved_vid_and_addr_validation(self):
        for value in (True, -1, 4095, 4096, 65536):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "VLAN ID"):
                ArpFrame.request(LOCAL_MAC, LOCAL_IP, PEER_IP, value)
        with self.assertRaisesRegex(ValueError, "MAC"):
            ArpFrame.request(b"", LOCAL_IP, PEER_IP)
        with self.assertRaisesRegex(ValueError, "IPv4"):
            ArpFrame.request(LOCAL_MAC, LOCAL_IP, "192.0.2.20")

    def test_all_incomplete_prefixes_fail(self):
        packet = directed_frames()[0].encode(pad=False)
        for length in range(len(packet)):
            with self.subTest(length=length), self.assertRaises(ValueError):
                ArpFrame.decode(packet[:length])

    def test_wire_geometry_and_type_are_validated(self):
        packet = bytearray(directed_frames()[1].encode())
        for offset, value in ((12, 0x86), (15, 2), (18, 5), (21, 9)):
            corrupted = bytearray(packet)
            corrupted[offset] = value
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                ArpFrame.decode(corrupted)

    def test_audit_exact_order_width_and_padding(self):
        expected = directed_frames()
        golden = [packet.encode().hex() for packet in expected]
        audit_capture(golden, expected)
        with self.assertRaisesRegex(ValueError, "expected 9"):
            audit_capture(golden[:-1], expected)
        with self.assertRaisesRegex(ValueError, "wire length"):
            audit_capture([golden[0][:-2], *golden[1:]], expected)
        with self.assertRaisesRegex(ValueError, "byte 59 differs"):
            audit_capture([golden[0][:-2] + "ff", *golden[1:]], expected)
        with self.assertRaisesRegex(ValueError, "packet 1"):
            audit_capture([golden[1], golden[0], *golden[2:]], expected)

    def test_audit_catches_wrong_neighbor(self):
        expected = directed_frames()
        frames = [packet.encode().hex() for packet in expected]
        frames[5] = replace(expected[5], target_ip=IPv4Address("192.0.2.21")).encode().hex()
        with self.assertRaisesRegex(ValueError, "packet 6"):
            audit_capture(frames, expected)


if __name__ == "__main__":
    unittest.main()
