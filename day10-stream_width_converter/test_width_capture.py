"""Test WGC1 byte framing and cross-profile timing analysis using synthetic traces."""

from dataclasses import replace
from io import BytesIO, StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zlib import crc32

from width_audit import AuditError, TraceAudit, Transfer, capture
from width_capture import (
    BEAT_META, CHECKSUM, HEADER, RECORD, CaptureError, Geometry, convert_to_binary,
    convert_to_text, decode_value, encode_value, inspect, read_rows, text_record, write_capture,
)
from width_model import Beat, Report, Status, to_beats
from width_profile import compare, profile, quantile


def burst(iw=4, ow=8, seed=4, ready=0):
    """Create two clean packets and an explicit reject in a portable trace."""
    yield Transfer("reset", 0, None)
    cycle = 1
    for payload in (b"012345678", b"HELLO"):
        frames = tuple(to_beats(payload, iw, seed, 255))
        for beat in frames:
            yield Transfer("input", cycle, beat)
            cycle += 1
        yield Transfer("report", cycle, Report(seed, len(payload)))
        cycle += 1 + ready
        for beat in to_beats(payload, ow, seed):
            yield Transfer("output", cycle, beat)
            cycle += 1
    yield Transfer("input", cycle, Beat(tag=5, last=True))
    yield Transfer("report", cycle + 1, Report(5, 0, Status.BAD_KEEP))


def encoded(geometry, events):
    """Build a binary fixture with the public writer and return its exact bytes."""
    out = BytesIO()
    write_capture(out, geometry, events)
    return out.getvalue()


def decoded(raw):
    """Read all binary records using the header's physical dimensions."""
    file = BytesIO(raw)
    geo = Geometry.read(file)
    return geo, list(read_rows(file, geo))


def forge(clock, channel, value):
    """Construct a CRC-correct malformed record to exercise structural validation."""
    prefix = RECORD.pack(channel, clock, len(value))
    return prefix + value + CHECKSUM.pack(crc32(prefix + value))


class BinaryEncoding(unittest.TestCase):
    def test_exact_header_and_reset_bytes(self):
        """The initial exchange contains transparent big-independent field order."""
        geo = Geometry(4, 8, 128)
        event = Transfer("reset", 0, None)
        raw = encoded(geo, [event])
        expected_header = b"WGC1\x04\x08\x80\x00"
        self.assertEqual(raw[:HEADER.size], expected_header)
        self.assertEqual(raw[HEADER.size:HEADER.size + RECORD.size], bytes(RECORD.size))
        self.assertEqual(decoded(raw), (geo, [event]))

    def test_round_trip_at_many_lanes(self):
        """Every lane width and last-mask residue survives binary and text conversion."""
        for iw, ow in ((1, 1), (2, 3), (4, 8), (8, 4), (15, 16), (16, 1)):
            with self.subTest(iw=iw, ow=ow):
                expected = list(burst(iw, ow))
                geo = Geometry(iw, ow, 64)
                geometry, actual = decoded(encoded(geo, expected))
                self.assertEqual(geometry, geo)
                self.assertEqual(actual, expected)
                lines = [text_record(event) for event in actual]
                self.assertEqual(list(capture(lines, iw, ow)), expected)

    def test_binary_decoder_rejects_invalid_magic_and_geometry(self):
        """Never treat a mismatched format as compatible based on its extension."""
        for values, message in (
            ((b"BAD2", 4, 8, 20), "wrong magic"),
            ((b"WGC1", 0, 1, 20), "input width"),
            ((b"WGC1", 7, 17, 20), "output width"),
            ((b"WGC1", 4, 8, 0), "capacity"),
        ):
            with self.subTest(values=values), self.assertRaisesRegex(CaptureError, message):
                decoded(HEADER.pack(*values))

    def test_truncated_record_classes(self):
        """Require a complete header, prefix, body, and checksum."""
        geo = Geometry(3, 5, 8)
        raw = encoded(geo, [Transfer("input", 2, Beat(0x55, 1, True, 4))])
        crops = (
            (HEADER.size - 1, "capture header"),
            (HEADER.size + 2, "record prefix"),
            (HEADER.size + RECORD.size + 1, "record body"),
            (len(raw)-1, "record checksum"),
        )
        for count, message in crops:
            with self.subTest(count=count), self.assertRaisesRegex(CaptureError, message):
                decoded(raw[:count])
        self.assertEqual(decoded(raw[:HEADER.size]), (geo, []))

    def test_crc_detects_in_range_data_corruption(self):
        """Flipping a data bit leaves widths valid but changes the record checksum."""
        geo = Geometry(4, 8, 16)
        raw = bytearray(encoded(geo, list(burst())))
        raw[-1] ^= 1
        with self.assertRaisesRegex(CaptureError, "record CRC mismatch"):
            decoded(raw)

    def test_rejects_unknown_type_and_wrong_geometry_body(self):
        """An intact CRC cannot make a new type or inconsistent width legal."""
        geo = Geometry(4, 8, 12)
        with self.assertRaisesRegex(CaptureError, "unknown record type"):
            decoded(geo.header() + forge(64, 17, bytes(4)))
        with self.assertRaisesRegex(CaptureError, "declared geometry"):
            decoded(geo.header() + forge(2, 1, bytes(8)))

    def test_field_validation_after_crc(self):
        """The decoder checks flags, status codes, and masks even under a valid CRC."""
        geo = Geometry(4, 8, 16)
        wrong_flag = BEAT_META.pack(3, 5, 123) + bytes(geo.input_width)
        bad_keep = BEAT_META.pack(0x40, 1, 12) + bytes(geo.input_width)
        wrong_status = bytes((0x34, 0x12, 4, 0, 255))
        malformed = ((1, wrong_flag, "not binary"),
                     (1, bad_keep, "lane mask"),
                     (3, wrong_status, "valid Status"))
        for kind, value, message in malformed:
            with self.subTest(kind=kind), self.assertRaisesRegex(CaptureError, message):
                decoded(geo.header() + forge(1, kind, value))

    def test_duplicate_times_and_reset_collision(self):
        """Each edge can have one data transfer per channel with no reset overlap."""
        geo = Geometry(4, 8, 12)
        event = Transfer("input", 3, Beat(11, 1, True, 9))
        problems = (
            ([event, event], "repeated channel"),
            ([event, replace(event, cycle=2)], "backwards"),
            ([event, Transfer("reset", 3, None)], "reset shares"),
            ([Transfer("reset", 3, None), event], "reset shares"),
        )
        for sequence, reason in problems:
            with self.subTest(reason=reason), self.assertRaisesRegex(CaptureError, reason):
                encoded(geo, sequence)
        second = Transfer("output", 3, Beat(5, 1, True, 9))
        self.assertEqual(decoded(encoded(geo, [event, second]))[1], [event, second])

    def test_writer_rejects_content_of_wrong_type(self):
        """Binary encoding is deliberately typed by record kind."""
        geo = Geometry(2, 4, 8)
        for channel, value in (("reset", 123), ("input", None), ("output", Report()),
                               ("report", Beat()), ("unknown", None)):
            with self.subTest(channel=channel), self.assertRaises(CaptureError):
                encoded(geo, [Transfer(channel, 0, value)])
        with self.assertRaises(ValueError):
            encode_value(geo, "report", Report(-1, 0, Status.OK))
        with self.assertRaises(ValueError):
            decode_value(geo, "report", b"\xff\xff\xff\xff\x7f")


class TextAndAudit(unittest.TestCase):
    def test_friendly_diagnostics_for_malformed_text(self):
        """Bad text rows identify the original line and reject ambiguous cycle order."""
        for payload, reason in (
            ("", "not enough"),
            ("input 6 f 100 0 3", "lane mask"),
            ("output 6 1 1 2 3", "one bit"),
            ("report 7 1 1 88", "valid Status"),
            ("reset -1", "nonnegative"),
            ("reset 4 extra", "malformed reset"),
            ("jam 5 1", "malformed jam"),
            ("input 2 4 1 1 3\ninput 2 8 1 1 4", "same channel"),
            ("reset 3\noutput 3 1 1 1 2", "same channel"),
            ("reset 3\noutput 2 1 1 1 2", "monotonic"),
        ):
            lines = payload.splitlines() or [payload]
            with self.subTest(reason=reason), self.assertRaisesRegex(AuditError, reason):
                list(capture(lines, 4, 8))

    def test_audit_rejects_payload_changes_and_nonzero_padding(self):
        """A valid-format output can still differ from the wanted packet."""
        rows = list(burst())
        index = next(index for index, event in enumerate(rows)
                     if event.channel == "output" and event.value.last)
        event = rows[index]
        changes = (
            (replace(event.value, data=event.value.data ^ 1), "payload"),
            (replace(event.value, data=event.value.data | (1 << 63)), "unused"),
            (replace(event.value, tag=3), "identity"),
            (replace(event.value, keep=0), "low-lane prefix"),
            (replace(event.value, last=False), "non-final"),
        )
        for override, reason in changes:
            with self.subTest(reason=reason):
                audit = TraceAudit(4, 8, 128)
                altered = rows[:index] + [replace(event, value=override)] + rows[index+1:]
                with self.assertRaisesRegex(AuditError, reason):
                    for row in altered:
                        audit.apply(row)

    def test_stall_latency_and_reset_accounting(self):
        """Independent capture checks retain visible packets and discard abandoned work."""
        rows = list(burst(4, 8, ready=3))
        audit = TraceAudit(4, 8, 128)
        audit.apply(Transfer("input", 0, Beat(5, 15, False, 3)))
        for row in rows:
            audit.apply(row)
        report = audit.finish()
        self.assertEqual(report["packets"], 2)
        self.assertEqual(report["bytes"], 14)
        self.assertEqual(report["abandoned_by_reset"], 1)
        self.assertEqual(report["reports"], {"OK": 2, "BAD_KEEP": 1})
        self.assertEqual(report["first_beat_min"], 5)
        self.assertEqual(report["final_beat_max"], 6)

    def test_incomplete_captures_do_not_claim_success(self):
        """Stopping after input leaves both a report and packet in flight."""
        audit = TraceAudit(4, 8, 16)
        audit.apply(Transfer("input", 1, Beat(22, 1, True, 18)))
        with self.assertRaisesRegex(AuditError, "still outstanding"):
            audit.finish()
        with self.assertRaisesRegex(AuditError, "report.*differs"):
            audit.apply(Transfer("report", 2, Report(19, 1)))


class FileOperations(unittest.TestCase):
    def setUp(self):
        """Create independent trace files in an ephemeral test directory."""
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "gearbox.txt"
        self.target = self.root / "gearbox.wgc"
        self.decoded = self.root / "unpacked.txt"

    def test_cli_functions_round_trip_and_report(self):
        """The file functions retain canonical transfers and exact payload metrics."""
        expected = list(burst())
        self.source.write_text("\n".join(map(text_record, expected))+"\n", encoding="ascii")
        geo = Geometry(4, 8, 128)
        self.assertEqual(convert_to_binary(self.source, self.target, geo), len(expected))
        self.assertEqual(convert_to_text(self.target, self.decoded), len(expected))
        self.assertEqual(self.decoded.read_text(), self.source.read_text())
        result = inspect(self.target)
        self.assertEqual(result["geometry"], vars(geo))
        self.assertEqual(result["packets"], 2)
        self.assertEqual(result["bytes"], 14)

    def test_does_not_replace_original_on_bad_input_or_alias(self):
        """Ensure CLI failures preserve a previously valid artifact and incoming trace."""
        self.source.write_text("bad row\n", encoding="ascii")
        self.target.write_bytes(b"previous")
        with self.assertRaises(AuditError):
            convert_to_binary(self.source, self.target, Geometry(4, 8, 128))
        self.assertEqual(self.target.read_bytes(), b"previous")
        copy = self.root / "copy.txt"
        os.link(self.source, copy)
        with self.assertRaisesRegex(CaptureError, "same file"):
            convert_to_binary(self.source, copy, Geometry(4, 8, 128))
        with self.assertRaisesRegex(CaptureError, "same path"):
            convert_to_text(self.target, self.target)

    def test_compare_accepts_repacked_payloads(self):
        """Two different width paths can be compared without matching clock edges."""
        a = Geometry(4, 8, 16)
        b = Geometry(2, 3, 16)
        self.source.write_bytes(encoded(a, burst(a.input_width, a.output_width)))
        self.target.write_bytes(encoded(b, burst(b.input_width, b.output_width, ready=7)))
        report = profile(self.target, example_limit=1)
        self.assertEqual(report["packets"], 2)
        self.assertEqual(report["output_payload_bytes"], 14)
        self.assertEqual(report["first_beat_latency_percentiles"][100], 9)
        self.assertEqual(report["accepted_edges"]["output"], 5)
        self.assertEqual(len(report["examples"]), 1)
        example = report["examples"][0]
        self.assertEqual((example["length"], example["output_beats"]), (9, 3))
        self.assertEqual(compare(self.source, self.target)["same_packet_payloads"], 2)
        self.target.write_bytes(encoded(b, burst(b.input_width, b.output_width, seed=11)))
        with self.assertRaisesRegex(AuditError, "ordered output packet 1"):
            compare(self.source, self.target)

    def test_nearest_rank_percentile_contract(self):
        """Use a simple exact order statistic instead of interpolating cycle counts."""
        self.assertIsNone(quantile([], 99))
        self.assertEqual(quantile([10, 8, 20], 50), 10)
        self.assertEqual(quantile([10, 8, 20], 90), 20)
        with self.assertRaises(ValueError):
            quantile([1, 2, 3], 101)

    def test_profile_rejects_bad_keep_and_padding(self):
        """Transport integrity alone cannot validate an output beat's physical lanes."""
        geometry = Geometry(4, 8, 16)
        events = list(burst())
        index = next(i for i, row in enumerate(events) if row.channel == "output" and row.value.last)
        original = events[index]
        bad_width = replace(original.value, keep=2)
        bad_padding = replace(original.value, data=original.value.data | (1 << 63))
        for fault, error in ((bad_width, "keep"), (bad_padding, "padding")):
            with self.subTest(error=error):
                events[index] = replace(original, value=fault)
                self.target.write_bytes(encoded(geometry, events))
                with self.assertRaisesRegex(AuditError, error):
                    profile(self.target)
