"""Check independent routed-link oracles against hand-worked examples."""
from contextlib import redirect_stdout
from io import StringIO
from ipaddress import IPv4Address
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from net_wire import to_beats
from next_hop_vectors import expected
from routed_link_fixture import audit, build, multicast_mac, read_trace


class NextHopTest(unittest.TestCase):
    def test_untagged_and_priority_tagged_have_distinct_routes(self):
        destination = IPv4Address("203.0.113.222")
        self.assertEqual(expected(-1, destination, False, 0)[0:2], (0, int(destination)))
        self.assertEqual(expected(-1, destination, False, 0)[3], 1)
        self.assertEqual(expected(0, destination, False, 0)[0], 1)

    def test_more_specific_reject_beats_default(self):
        blocked = expected(24, IPv4Address("198.51.100.64"), False, 0)
        permitted = expected(24, IPv4Address("198.51.100.128"), False, 0)
        self.assertEqual(blocked, (2, 0, 0, 3, False))
        self.assertEqual(permitted[:2], (0, 0xc0000214))

    def test_multicast_uses_low_23_bits_and_requires_opt_in(self):
        first = IPv4Address("224.1.2.3")
        second = IPv4Address("239.129.2.3")
        self.assertEqual(multicast_mac(first), 0x01005e010203)
        self.assertEqual(multicast_mac(second), multicast_mac(first))
        self.assertEqual(expected(-1, second, False, 0)[0], 7)
        self.assertEqual(expected(4095, second, True, 0)[0], 3)
        self.assertFalse(expected(24, second, True, 0)[-1])

    def test_arp_failure_preserves_hop_and_clears_mac(self):
        outcome = expected(-1, IPv4Address("198.51.100.2"), False, 1)
        self.assertEqual(outcome, (4, 0xc0000201, 0, 0, True))


class LinkTraceTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.trace = self.directory / "trace.txt"

    def test_lock_failure_detected_before_frame_reassembly(self):
        source = b"0123456789"
        first, tail = to_beats(source)
        self.trace.write_text(f"2 1 {first.row()}\n3 2 {tail.row()}\n")
        with self.assertRaisesRegex(ValueError, "ownership changed"):
            read_trace(self.trace)

    def test_unfinished_packet_is_not_a_result(self):
        beat = to_beats(b"0123456789")[0]
        self.trace.write_text(f"1 0 {beat.row()}\n")
        with self.assertRaisesRegex(ValueError, "open frame"):
            read_trace(self.trace)

    def test_fixture_round_trip_and_exact_byte_corruption(self):
        build(self.directory)
        expected_frames = json.loads((self.directory / "wire_expect.json").read_text())
        rows = []
        for lane, frame in expected_frames:
            for beat in to_beats(bytes.fromhex(frame)):
                rows.append(f"{len(rows)+1} {lane} {beat.row()}\n")
        self.trace.write_text("".join(rows))
        with redirect_stdout(StringIO()):
            audit(self.directory, self.trace)
        rows[2] = rows[2].replace("040600", "040601")
        self.trace.write_text("".join(rows))
        with self.assertRaisesRegex(ValueError, "wire frame mismatch"):
            audit(self.directory, self.trace)
