"""Round-trip, corruption, and boundary tests for offline replay file formats."""

from fractions import Fraction
from io import BytesIO, StringIO
from pathlib import Path
from random import Random
from struct import pack
from tempfile import TemporaryDirectory
import unittest
from zlib import crc32

from replay_capture import (
    CaptureHeader, CapturePacket, CaptureSelection, LinkHeader, import_pcap,
    inspect_link, packet_records,
)
from replay_format import (
    HEADER, MAGIC, RECORD, VERSION, StampedPacket, atomic_output, canonical_fraction,
    decode_tape, dump_packet_csv, dump_tape_csv, encode_tape, load_packet_csv, load_tape_csv,
    packet_offsets, parse_decimal, parse_integer, parse_last, parse_payload, read_tape,
    same_path, write_tape,
)
from replay_program import Beat, Packet, ReplayError, Tape, U16, U32


def fixture_tape():
    """Return a tape with same-tick packets, one partial last beat, and high tags."""
    return Tape.from_packets((
        Packet.paced(0x1294, bytes.fromhex("0102030405060708090a0b"), 7, 3),
        Packet.paced(U16, b"ABC", 10, 0),
    ))


def header(order="<", ns=False, *, snap=256, link=1, version=(2, 4)):
    """Build a fixed PCAP header independently of the capture parser."""
    marker = 0xA1B23C4D if ns else 0xA1B2C3D4
    return pack(order+"IHHIIII", marker, *version, 0, 0, snap, link)


def frame(order, seconds, fraction, payload, *, included=None, original=None):
    """Build one PCAP record with optionally altered header lengths."""
    captured = len(payload) if included is None else included
    wire = captured if original is None else original
    return pack(order+"IIII", seconds, fraction, captured, wire) + payload


def ethernet(kind=0x0800, vlans=(), body=b"abcd"):
    """Construct test MAC headers with nested VLAN tags and known payload bytes."""
    raw = bytes.fromhex("020000000001020000000002")
    for index, vlan in enumerate(vlans):
        tag_type = 0x8100 if index else 0x88A8
        raw += pack(">HH", tag_type, 0xA000 | vlan)
    return raw + pack(">H", kind) + body


class CsvTests(unittest.TestCase):
    """Exercise canonical CSV bytes and contextual validation errors."""

    def test_tape_csv_known_bytes(self):
        """Write deterministic text and recover all valid bytes, tags, and tick fields."""
        tape = fixture_tape()
        stream = StringIO()
        dump_tape_csv(tape, stream)
        self.assertEqual(stream.getvalue(), (
            "tick,tag,last,payload\n"
            "7,4756,0,0102030405060708\n"
            "10,4756,1,090a0b\n"
            "10,65535,1,414243\n"
        ))
        self.assertEqual(load_tape_csv(StringIO(stream.getvalue())), tape)
        self.assertEqual(packet_offsets(next(tape.packets())), ((7, 0, 8), (10, 8, 3)))

    def test_tape_csv_refuses_headers_and_partial_packets(self):
        """Distinguish incompatible columns from malformed logical record geometry."""
        examples = [
            ("tag,tick,last,payload\n1,1,1,4142\n", "header"),
            ("tick,tag,last,payload\n1,1,1,aa,extra\n", "line 2"),
            ("tick,tag,last,payload\n1,1,1\n", "line 2"),
            ("tick,tag,last,payload\n1,1,2,aa\n", "last"),
            ("tick,tag,last,payload\n1,1,0,aa\n", "all eight"),
            ("tick,tag,last,payload\n1,1,0,aa01020304050607\n", "partial packet"),
            ("tick,tag,last,payload\n1,1,1,000102030405060708\n", "more than 8"),
        ]
        for text, message in examples:
            with self.subTest(text=text):
                with self.assertRaisesRegex(ReplayError, message):
                    load_tape_csv(StringIO(text))

    def test_packet_csv_preserves_fractional_nanoseconds_exactly(self):
        """Keep decimal values exact through both import and export."""
        source = StringIO(
            "time_ns,tag,payload\n"
            "1.025,0x0f,1234567890abcdef\n"
            "2e-2,0x10,03\n"
        )
        packets = tuple(load_packet_csv(source))
        self.assertEqual(packets, (
            StampedPacket(Fraction(41, 40), 15, bytes.fromhex("1234567890abcdef")),
            StampedPacket(Fraction(1, 50), 16, b"\x03"),
        ))
        out = StringIO()
        dump_packet_csv(packets, out)
        self.assertEqual(out.getvalue(), "time_ns,tag,payload\n1.025,15,1234567890abcdef\n0.02,16,03\n")
        self.assertEqual(tuple(load_packet_csv(StringIO(out.getvalue()))), packets)

    def test_packet_csv_exposes_invalid_row_and_field(self):
        """Reject missing cells, impossible numbers, and malformed hex with row context."""
        for row, message in [
            ("NaN,1,ff", "finite"),
            ("-1,1,ff", "nonnegative"),
            ("Infinity,1,ff", "finite"),
            ("2,65536,ff", "tag"),
            ("2,1,123", "complete"),
            ("2,1,0xff", "complete"),
            ("2,1,xx", "complete"),
            ("2,1,", "nonempty"),
            ("2,,ff", "tag"),
        ]:
            with self.subTest(row=row):
                body = "time_ns,tag,payload\n" + row + "\n"
                with self.assertRaisesRegex(ReplayError, f"line 2: .*{message}"):
                    tuple(load_packet_csv(StringIO(body)))

    def test_number_exact_bounds(self):
        """Distinguish parsing from range checks and preserve exact finite signed fractions."""
        self.assertEqual(parse_integer("0xFF", 255, "byte"), 255)
        self.assertEqual(parse_integer("00009", 9, "count"), 9)
        self.assertEqual(parse_decimal("0.00000000001", "period"), Fraction(1, 10**11))
        self.assertEqual(canonical_fraction(Fraction(-1, 125)), "-0.008")
        self.assertEqual(canonical_fraction(Fraction(640, 10)), "64")
        self.assertEqual(parse_last("0"), False)
        self.assertEqual(parse_last("1"), True)
        self.assertEqual(parse_payload("Aa0f"), bytes.fromhex("aa0f"))
        with self.assertRaises(ReplayError):
            canonical_fraction(Fraction(2, 7))

    def test_packet_payload_must_be_nonempty_bytes(self):
        """Refuse payloads that would not encode as a terminating beat."""
        for bad in (b"", bytearray(b"a"), [1], "0102"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ReplayError, "nonempty"):
                    StampedPacket(1, 2, bad)


class BinaryTests(unittest.TestCase):
    """Check byte-exact binary layout, CRC and malformed file handling."""

    def test_binary_known_layout(self):
        """Build the file layout manually and compare exact little-endian bytes."""
        beat = Beat.from_payload(0x44332211, b"ABC", 0x6655, True)
        body = bytes.fromhex("11223344414243000000000055660701")
        expected = b"TRP1" + bytes.fromhex("0100100001000000") + pack("<I", crc32(body)) + body
        self.assertEqual(encode_tape(Tape((beat,))), expected)
        self.assertEqual(decode_tape(expected), Tape((beat,)))

    def test_binary_roundtrip_every_residue(self):
        """Round-trip varied packet sizes, equal scheduled ticks, and nonuniform tags."""
        rng = Random(197)
        for count in range(1, 75):
            payload = bytes(rng.getrandbits(8) for _ in range(count))
            first = Packet.paced(rng.randrange(1 << 16), payload, count, count % 4)
            second = Packet.paced(0, b"last", first.end)
            tape = Tape.from_packets((first, second))
            result = decode_tape(encode_tape(tape))
            self.assertEqual(tape, result)
            self.assertEqual(tuple(result.packets()), (first, second))

    def test_binary_rejects_header_corruption(self):
        """Do not process payloads under invalid magic, version, size, count or CRC."""
        body = encode_tape(fixture_tape())[HEADER.size:]
        cases = (
            (b"bad!", VERSION, 16, 3, crc32(body), "magic"),
            (MAGIC, VERSION+1, 16, 3, crc32(body), "version"),
            (MAGIC, VERSION, 20, 3, crc32(body), "geometry"),
            (MAGIC, VERSION, 16, 4, crc32(body), "length"),
            (MAGIC, VERSION, 16, 3, crc32(body)^0x20, "CRC"),
        )
        for magic, version, size, count, checksum, message in cases:
            with self.subTest(message=message):
                raw = HEADER.pack(magic, version, size, count, checksum) + body
                with self.assertRaisesRegex(ReplayError, message):
                    decode_tape(raw)
        with self.assertRaisesRegex(ReplayError, "truncated"):
            decode_tape(b"TRP1")
        with self.assertRaisesRegex(ReplayError, "capacity"):
            decode_tape(encode_tape(fixture_tape()), 2)

    def test_binary_crc_covers_payload_and_timestamp(self):
        """Mutate each stored byte independently and require checksum rejection."""
        original = encode_tape(fixture_tape())
        for index in range(HEADER.size, len(original)):
            with self.subTest(index=index):
                broken = bytearray(original)
                broken[index] ^= 1
                with self.assertRaisesRegex(ReplayError, "CRC"):
                    decode_tape(broken)

    def test_binary_rejects_semantic_corruption_with_valid_crc(self):
        """A matching CRC cannot make reserved flags, inactive data or bad framing legal."""
        records = (
            (RECORD.pack(3, 7, 2, 1, 5), "flag"),
            (RECORD.pack(3, 0x100, 2, 1, 1), "inactive"),
            (RECORD.pack(3, 7, 2, 2, 1), "contiguous"),
            (RECORD.pack(3, 7, 2, 255, 0), "partial"),
        )
        for body, message in records:
            with self.subTest(message=message):
                candidate = HEADER.pack(MAGIC, VERSION, RECORD.size, 1, crc32(body)) + body
                with self.assertRaisesRegex(ReplayError, message):
                    decode_tape(candidate)

    def test_atomic_file_io_respects_alias_and_failure(self):
        """Keep source files intact and remove a temporary write that fails midway."""
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            binary = directory / "timed.trp"
            text = directory / "steps.csv"
            target = directory / "protected"
            target.write_text("original", encoding="utf-8")
            tape = fixture_tape()
            write_tape(tape, binary)
            write_tape(tape, text)
            self.assertEqual(read_tape(binary, 3), tape)
            self.assertEqual(read_tape(text, 3), tape)
            with self.assertRaises(ReplayError):
                read_tape(text, 2)
            link = directory / "link"
            link.symlink_to(text)
            self.assertTrue(same_path(text, link))
            self.assertFalse(same_path(binary, text))
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                atomic_output(target, "w", self.partial_writer)
            self.assertEqual(target.read_text(), "original")
            self.assertEqual(sorted(path.name for path in directory.iterdir()),
                             ["link", "protected", "steps.csv", "timed.trp"])

    @staticmethod
    def partial_writer(stream):
        """Write a prefix then fail to test atomic cleanup and previous output preservation."""
        stream.write("candidate")
        raise RuntimeError("writer stopped")


class PcapTests(unittest.TestCase):
    """Exercise both byte orders, clock resolutions, framing and selection policies."""

    def test_every_classic_byte_order_and_resolution(self):
        """Decode source timestamps and payload unchanged in all supported variants."""
        payload = ethernet(body=b"unique")
        for order in ("<", ">"):
            for ns in (False, True):
                with self.subTest(order=order, ns=ns):
                    raw = header(order, ns) + frame(order, 5, 123, payload)
                    records = list(packet_records(BytesIO(raw)))
                    self.assertEqual(records, [CapturePacket(
                        0, 5_000_000_000 + 123 * (1 if ns else 1000), len(payload), payload,
                    )])

    def test_bad_global_header_is_rejected_before_packet_read(self):
        """Fail early on unsupported geometry, link type, or broken header."""
        for raw, message in [
            (b"bad!", "unsupported"),
            (header(version=(3, 5)), "version"),
            (header(snap=0), "snap"),
            (header(snap=(1 << 20)+1), "snap"),
            (header(link=101), "Ethernet"),
            (header()[:-1], "truncated"),
        ]:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ReplayError, message):
                    CaptureHeader.read(BytesIO(raw))

    def test_invalid_record_headers_and_truncation(self):
        """Refuse lengths or timestamps that cannot match the preceding profile."""
        a = header(snap=16)
        for raw, message in [
            (a + b"short", "packet header"),
            (a + frame("<", 1, 9, b"abc", included=4), "payload"),
            (a + frame("<", 1, 9, b"abc", original=2), "impossible"),
            (a + frame("<", 1, 1_000_000, b"abc"), "fractional"),
            (a + frame("<", 1, 0, b"x"*18), "impossible"),
            (a + frame("<", 1, 0, b""), "impossible"),
            (a + frame("<", 3, 4, b"a") + frame("<", 3, 3, b"b"), "backwards"),
        ]:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ReplayError, message):
                    list(packet_records(BytesIO(raw)))

    def test_optional_ordering_fence_for_offline_diagnosis(self):
        """Allow raw record examination without reordering capture time."""
        raw = header() + frame("<", 4, 0, b"a") + frame("<", 1, 0, b"b")
        packets = tuple(packet_records(BytesIO(raw), require_monotonic=False))
        self.assertEqual([p.position for p in packets], [0, 1])
        self.assertEqual([p.time_ns for p in packets], [4_000_000_000, 1_000_000_000])

    def test_vlan_parse_and_selection(self):
        """Filter on an inner VLAN but derive the replay tag from the outer one."""
        plain = ethernet(0x0800, body=b"X")
        udp4 = ethernet(0x0800, vlans=(15, 11), body=b"Y")
        ipv6 = ethernet(0x86DD, vlans=(15, 11), body=b"Z")
        raw = header() + b"".join(frame("<", 2, i, payload)
                                  for i, payload in enumerate((plain, udp4, ipv6)))
        self.assertEqual(inspect_link(plain), LinkHeader(0x0800, (), 14))
        self.assertEqual(inspect_link(udp4), LinkHeader(0x0800, (15, 11), 22))
        choices = CaptureSelection(ethertype=0x0800, vlan=11, tag="outer-vlan")
        selected = tuple(import_pcap(BytesIO(raw), choices))
        self.assertEqual(selected, (StampedPacket(2_000_001_000, 15, udp4),))

    def test_link_parser_rejects_short_and_incomplete_tag(self):
        """Differentiate a missing outer header from an interrupted VLAN header."""
        with self.assertRaisesRegex(ReplayError, "Ethernet"):
            inspect_link(b"bad")
        with self.assertRaisesRegex(ReplayError, "VLAN"):
            inspect_link(ethernet(vlans=(1,))[:-6])

    def test_truncation_and_timestamp_filters(self):
        """Check independent keep, reject, and error decisions for clipped capture records."""
        payload = ethernet()
        raw = header() + frame("<", 1, 0, payload, original=len(payload)+10)
        raw += frame("<", 2, 1, payload) + frame("<", 4, 0, payload)
        with self.assertRaisesRegex(ReplayError, "original frame"):
            tuple(import_pcap(BytesIO(raw)))
        rejected = tuple(import_pcap(BytesIO(raw), CaptureSelection(
            truncated="reject", tag="sequential", first_tag=20, end_ns=2_000_001_000,
        )))
        self.assertEqual(rejected, (StampedPacket(2_000_001_000, 20, payload),))
        kept = tuple(import_pcap(BytesIO(raw), CaptureSelection(truncated="keep", max_packets=2)))
        self.assertEqual([packet.tag for packet in kept], [0, 1])
        self.assertEqual([packet.time_ns for packet in kept], [1_000_000_000, 2_000_001_000])

    def test_selection_limit_does_not_read_a_malformed_tail(self):
        """A requested record subset may end before unrelated trailing damage."""
        raw = header() + frame("<", 1, 0, ethernet()) + b"bad"
        self.assertEqual(tuple(import_pcap(BytesIO(raw), CaptureSelection(max_packets=0))), ())
        selected = tuple(import_pcap(BytesIO(raw), CaptureSelection(max_packets=1)))
        self.assertEqual(len(selected), 1)
        with self.assertRaisesRegex(ReplayError, "packet header"):
            tuple(import_pcap(BytesIO(raw)))

    def test_tag_derivation_and_options_are_bounded(self):
        """Refuse a derived tag that would alias an earlier replay identity."""
        first = CapturePacket(9, 12, 4, b"abcd")
        with self.assertRaisesRegex(ReplayError, "derived"):
            tuple(CaptureSelection(first_tag=65530).select([first]))
        for opts in ({"ethertype": 65536}, {"vlan": 4096}, {"start_ns": -1},
                     {"end_ns": 1, "start_ns": 2}, {"tag": "mystery"}, {"max_packets": -1},
                     {"truncated": "shave"}):
            with self.subTest(opts=opts):
                with self.assertRaises(ReplayError):
                    CaptureSelection(**opts)


if __name__ == "__main__":
    unittest.main()
