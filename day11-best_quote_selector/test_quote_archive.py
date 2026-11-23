"""Check packed profile widths, signed fields, corrupt rows, and atomic CLI I/O."""

import io
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest

from quote_archive import (
    ArchiveError, Field, FRAME, HEADER, Kind, MAGIC, VERSION,
    format_for, header_bytes, read_archive, read_header, row_checksum, write_archive,
)
from quote_archive_tool import pack_file, unpack_file
from quote_model import Machine
from quote_schema import PROFILES
from quote_verify import read_drives, write_drives
from quote_workload import state_tape


class PackAtoms(unittest.TestCase):
    def test_signed_field_extrema(self):
        """Round trip every exactly representable integer for a small signed field."""
        field = Field("spread", 5, signed=True)
        for number in range(-16, 16):
            self.assertEqual(field.decode(field.encode(number)), number)
        self.assertEqual(field.encode(-1), 31)
        self.assertEqual(field.encode(-16), 16)
        for number in (-17, 16, "4"):
            with self.assertRaisesRegex(ArchiveError, "spread"):
                field.encode(number)

    def test_unsigned_and_field_mask(self):
        """Ensure decoding ignores unrelated higher bits in an individual field."""
        field = Field("venue", 3)
        self.assertEqual(field.decode(0b111010), 2)
        self.assertEqual(field.decode(field.encode(7)), 7)
        with self.assertRaises(ArchiveError):
            field.encode(-1)

    def test_header_known_profile_layout(self):
        """Check stable magic, version, and exact byte order for each profile value."""
        fmt = format_for(Kind.TRACE, PROFILES["odd"])
        expected = b"BQ11" + bytes((1, 2, 3, 5, 12, 9, 11, 8, 12))
        self.assertEqual(header_bytes(fmt), expected)
        self.assertEqual(read_header(io.BytesIO(expected)), fmt)

    def test_compact_width_changes_with_geometry(self):
        """Check field totals rather than storing the same wasteful row in every profile."""
        solo = format_for(Kind.DRIVES, PROFILES["solo"])
        wide = format_for(Kind.DRIVES, PROFILES["wide"])
        self.assertLess(solo.bytes, wide.bytes)
        self.assertLessEqual(solo.bits, solo.bytes * 8)
        self.assertEqual(len(solo.fields), 18)
        self.assertEqual(len(format_for(Kind.TRACE, PROFILES["default"]).fields), 25)

    def test_inactive_fields_cannot_hide_payloads(self):
        """Treat invalid-field garbage as a noncanonical archive row."""
        fmt = format_for(Kind.DRIVES, PROFILES["solo"])
        raw = [0] * len(fmt.fields)
        raw[10] = 4
        with self.assertRaisesRegex(ArchiveError, "payload"):
            fmt.decode(fmt.pack(raw))

    def test_padding_bits_and_extra_columns(self):
        """Reject nonzero high padding and ambiguous field widths."""
        fmt = format_for(Kind.DRIVES, PROFILES["solo"])
        with self.assertRaisesRegex(ArchiveError, "columns"):
            fmt.pack((1, 2))
        self.assertLess(fmt.bits, fmt.bytes * 8)
        malformed = (1 << fmt.bits).to_bytes(fmt.bytes, "little")
        with self.assertRaisesRegex(ArchiveError, "padding"):
            fmt.unpack(malformed)


class ArchiveRecords(unittest.TestCase):
    def encode(self, name, stream_type):
        """Produce representative bytes and their typed inputs for one selected profile."""
        profile = PROFILES[name]
        driver = state_tape(profile).drives
        if stream_type == Kind.DRIVES:
            rows = driver
        else:
            model = Machine(profile)
            rows = tuple(model.step(row) for row in driver)
        fmt = format_for(stream_type, profile)
        stream = io.BytesIO()
        measured = write_archive(stream, fmt, rows)
        self.assertEqual(measured, len(rows))
        return stream.getvalue(), fmt, rows

    def test_all_profiles_and_channels(self):
        """Round trip complete driver and capture streams, including crossed spreads."""
        for name in PROFILES:
            for kind in Kind:
                with self.subTest(name=name, kind=kind):
                    raw, source, rows = self.encode(name, kind)
                    fmt, records = read_archive(io.BytesIO(raw), source)
                    self.assertEqual(fmt, source)
                    self.assertEqual(tuple(records), rows)

    def test_detect_independent_header_corruption(self):
        """Reject wrong signatures, versions, dimensions, and truncated headers."""
        base = bytearray(header_bytes(format_for(Kind.TRACE, PROFILES["solo"])))
        cases = (
            (0, ord("H"), "magic"), (4, VERSION + 1, "version"),
            (5, 91, "kind"), (6, 0, "instruments"), (9, 64, "qty_w"),
        )
        for index, val, error in cases:
            bad = bytearray(base)
            bad[index] = val
            with self.subTest(error=error), self.assertRaisesRegex(ArchiveError, error):
                read_header(io.BytesIO(bad))
        for size in range(HEADER.size):
            with self.subTest(size=size), self.assertRaisesRegex(ArchiveError, "truncated"):
                read_header(io.BytesIO(base[:size]))

    def test_payload_crc_binds_row_order(self):
        """Catch both data corruption and swapping two intact records."""
        raw, fmt, rows = self.encode("default", Kind.TRACE)
        changed = bytearray(raw)
        changed[HEADER.size + fmt.bytes - 1] ^= 1
        _, records = read_archive(io.BytesIO(changed))
        with self.assertRaisesRegex(ArchiveError, "checksum mismatch at row 1"):
            next(records)
        span = fmt.bytes + FRAME.size
        cut = HEADER.size
        reversed_rows = raw[:cut] + raw[cut+span:cut+span*2] + raw[cut:cut+span] + raw[cut+span*2:]
        _, records = read_archive(io.BytesIO(reversed_rows))
        with self.assertRaisesRegex(ArchiveError, "checksum mismatch at row 1"):
            next(records)

    def test_detect_eof_mid_payload_or_checksum(self):
        """Differentiate a physically incomplete row from a corrupted complete row."""
        raw, fmt, _ = self.encode("solo", Kind.DRIVES)
        _, records = read_archive(io.BytesIO(raw[:HEADER.size + fmt.bytes - 1]))
        with self.assertRaisesRegex(ArchiveError, "truncated payload"):
            next(records)
        _, records = read_archive(io.BytesIO(raw[:HEADER.size + fmt.bytes + 1]))
        with self.assertRaisesRegex(ArchiveError, "truncated checksum"):
            next(records)

    def test_expected_layout_mismatch_is_detected_before_rows(self):
        """Reject captures from the wrong stream or geometry before interpretation."""
        raw, _, _ = self.encode("wide", Kind.DRIVES)
        for layout, error in (
            (format_for(Kind.TRACE, PROFILES["wide"]), "stream type"),
            (format_for(Kind.DRIVES, PROFILES["solo"]), "profile"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ArchiveError, error):
                read_archive(io.BytesIO(raw), layout)

    def test_corrupt_archive_never_overwrites_existing_text(self):
        """Abort atomically when a formerly valid archive has a broken final checksum."""
        raw, _, _ = self.encode("solo", Kind.DRIVES)
        with TemporaryDirectory() as folder:
            source = Path(folder) / "corrupt.bq"
            target = Path(folder) / "known-good.txt"
            source.write_bytes(raw[:-1] + bytes((raw[-1] ^ 3,)))
            target.write_text("keep me\n")
            with self.assertRaises(ArchiveError):
                unpack_file(source, target, None)
            self.assertEqual(target.read_text(), "keep me\n")
            self.assertEqual(sorted(file.name for file in target.parent.iterdir()),
                             ["corrupt.bq", "known-good.txt"])

    def test_decimal_file_and_archive_round_trip_exactly(self):
        """Retain the simulator's raw input bytes after compression and reconstruction."""
        profile = PROFILES["odd"]
        with TemporaryDirectory() as folder:
            source = Path(folder) / "driver.txt"
            target = Path(folder) / "capture.bq"
            copied = Path(folder) / "copy.txt"
            rows = state_tape(profile).drives
            write_drives(source, rows, profile)
            self.assertEqual(pack_file(source, target, Kind.DRIVES, profile), len(rows))
            fmt, count = unpack_file(target, copied, None)
            self.assertEqual(fmt.profile, profile)
            self.assertEqual(count, len(rows))
            self.assertEqual(source.read_bytes(), copied.read_bytes())
            self.assertEqual(tuple(read_drives(copied, profile)), rows)


if __name__ == "__main__":
    unittest.main()
