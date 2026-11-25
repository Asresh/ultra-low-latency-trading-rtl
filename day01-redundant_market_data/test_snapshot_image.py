"""Exercise the independent snapshot parser, image integrity and book semantics."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zlib import crc32

from mold_snapshot_image import CRC, HEADER, ImageError, Order, Snapshot, control_fixture
from mold_snapshot_image import decode_orders, read_descriptor, save_image
from snapshot_cutover_demo import Book, decode_normalized, fixtures
from wire_codec import decode_itch


class SnapshotImageTest(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.groups = fixtures()

    def test_canonical_round_trip_and_row_bytes(self):
        image = Snapshot.decode(self.snapshot.encode())
        self.assertEqual(image, self.snapshot)
        self.assertEqual(crc32(image.payload()), image.crc)
        self.assertEqual(decode_orders(image.payload()), image.orders)
        commands = tuple(control_fixture(image, 31, 7, False))
        sent = b"".join(int(line.split()[0], 16).to_bytes(16, "little") for line in commands[1:])
        self.assertEqual(sent, image.payload())
        self.assertEqual([line.split()[1] for line in commands[1:]], ["0", "0", "0", "1"])

    def test_checksum_and_truncation(self):
        original = bytearray(self.snapshot.encode())
        for offset, expected in ((5, "header checksum"), (len(original)-1, "row checksum")):
            data = bytearray(original)
            data[offset] ^= 0x80
            with self.subTest(offset=offset):
                with self.assertRaisesRegex(ImageError, expected):
                    Snapshot.decode(data)
        for data in (original[:-1], original+b"\0"):
            with self.assertRaisesRegex(ImageError, "length"):
                Snapshot.decode(data)

    def test_rejects_reserved_bits_even_with_valid_crc(self):
        src = self.snapshot.encode()
        body = bytearray(src[HEADER.size+CRC.size:])
        body[11] = 1
        fields = HEADER.unpack_from(src)
        header = HEADER.pack(*fields[:-1], crc32(body))
        with self.assertRaisesRegex(ImageError, "reserved"):
            Snapshot.decode(header+CRC.pack(crc32(header))+body)

    def test_scope_validation_and_empty_image(self):
        restored = replace(self.snapshot, orders=())
        self.assertEqual(restored.crc, 0)
        self.assertIs(restored.check_hardware(1, 3, 1), restored)
        self.assertEqual(Snapshot.decode(restored.encode()), restored)
        for desc, slots, epoch, body in (
            (self.snapshot, 3, 3, 3),
            (replace(self.snapshot, expected_epoch=7), 3, 3, 16),
            (replace(self.snapshot, slot=3), 3, 3, 16),
        ):
            with self.assertRaises(ImageError):
                desc.check_hardware(slots, epoch, body)

    def test_duplicate_and_noncanonical_references(self):
        for records in (reversed(self.snapshot.orders), self.snapshot.orders*2):
            with self.assertRaisesRegex(ImageError, "distinct and ascending"):
                replace(self.snapshot, orders=tuple(records))
        with self.assertRaises(ImageError):
            Order(3, 17, "S", "0xff", 1)

    def test_image_writes_and_duplicate_json_fields(self):
        with TemporaryDirectory() as temporary:
            descriptor = Path(temporary) / "book.json"
            descriptor.write_text('{"slot":0,"slot":1}', encoding="ascii")
            with self.assertRaisesRegex(ImageError, "duplicate JSON field"):
                read_descriptor(descriptor)
            path = Path(temporary) / "book.msn"
            save_image(path, self.snapshot)
            self.assertEqual(Snapshot.decode(path.read_bytes()), self.snapshot)


class SnapshotBookTest(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.groups = fixtures()
        self.book = Book(self.snapshot)

    def test_wrap_and_rebuild_through_real_event_bits(self):
        for _, first, bodies in self.groups[2:]:
            for offset, payload in enumerate(bodies):
                record = decode_itch((first+offset) & ((1<<64)-1), payload)
                self.book.apply(decode_normalized(record.bits()))
        self.assertEqual(self.book.sequence, 3)
        self.assertEqual(tuple(sorted(self.book.orders)), (9001, 9010))
        self.assertEqual(self.book.orders[9010].shares, 22)

    def test_rejects_noncontiguous_events_and_keeps_order_book(self):
        _, first, bodies = self.groups[2]
        record = decode_itch(first, bodies[0])
        with self.assertRaisesRegex(ImageError, "wrong sequence"):
            self.book.apply(replace(record, sequence=5))
        with self.assertRaisesRegex(ImageError, "absent order"):
            self.book.apply(replace(record, reference=50))
        self.assertEqual(self.book.sequence, first)
        self.assertEqual(tuple(self.book.orders.values()), self.snapshot.orders)


if __name__ == "__main__":
    unittest.main()
