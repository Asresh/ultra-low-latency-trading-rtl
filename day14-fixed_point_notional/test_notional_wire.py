"""Check binary request/reply artifacts and the separate hardware capture auditor."""

from dataclasses import replace
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest

from notional_batch import BatchError, ORDER_FIELDS
from notional_capture import Audit, ReplyEvent, RequestEvent, TraceError, read_events
from notional_model import Kind, Reply, Request, calculate
from notional_wire import (
    CHECK, HEADER, MAX_RECORDS, ORDER, REPLY, ImageError, ImageHeader, ImageRecord, Reader,
    compile_image, decode_order, decode_reply, encode_order, encode_reply, explain, image_to_tape,
    save_image,
)


def raw_image(header, requests):
    """Create bytes using the supported batch writer."""
    stream = BytesIO()
    save_image(stream, header, requests)
    return stream.getvalue()


def checked(data):
    """Return all complete, mathematically verified records."""
    return list(Reader(BytesIO(data)).records())


class NotionalSamples(unittest.TestCase):
    def test_header_is_explicitly_little_endian(self):
        """Fixed-point geometry and record count have exact known bytes."""
        header = ImageHeader(8, 0x1122)
        expected = bytes.fromhex("4e4f4d310800000022110000")
        self.assertEqual(header.bytes(), expected)
        self.assertEqual(ImageHeader.read(BytesIO(expected)), header)

    def test_input_zero_limit_stays_present(self):
        """Optional bit makes a present zero distinct from the no-limit wire value."""
        req = Request(4660, 0x11223344, 0x55AACC12, 77, 2, True, 0)
        with_limit = encode_order(req)
        raw = bytes.fromhex("34124433221112ccaa554d0002030000000000000000")
        self.assertEqual(with_limit, raw)
        self.assertEqual(decode_order(raw), req)
        self.assertEqual(decode_order(encode_order(replace(req, limit=None))), replace(req, limit=None))
        self.assertNotEqual(with_limit, encode_order(replace(req, limit=None)))

    def test_reply_maxima_and_error_codes_round_trip(self):
        """Both sides, all public status codes, and wide monetary values are represented."""
        u64 = (1 << 64) - 1
        for status in Kind:
            for sell in (False, True):
                result = Reply(65535, status, sell, u64, u64-5, 0xF001020304050607, True)
                self.assertEqual(decode_reply(encode_reply(result)), result)

    def test_declared_count_matches_exactly(self):
        """Reject streams shorter or longer than their advertised transaction count."""
        request = Request(1, 129, 6)
        contents = raw_image(ImageHeader(8, 2), (request, request))
        rows = checked(contents)
        self.assertEqual([item.sequence for item in rows], [0, 1])
        self.assertEqual([item.request for item in rows], [request, request])
        with self.assertRaisesRegex(ImageError, "extra bytes"):
            checked(contents + b"\x00")
        with self.assertRaisesRegex(ImageError, "record 1"):
            checked(contents[:-CHECK.size-1])
        with self.assertRaisesRegex(ImageError, "shorter"):
            raw_image(ImageHeader(8, 2), (request,))
        with self.assertRaisesRegex(ImageError, "exceeded"):
            raw_image(ImageHeader(8, 1), (request, request))

    def test_reordered_records_have_valid_crc_but_invalid_sequence(self):
        """Record identity is stronger than transport integrity of individual rows."""
        a = ImageRecord.predict(0, Request(5, 3, 4), 0)
        b = ImageRecord.predict(1, Request(5, 5, 4), 0)
        data = ImageHeader(0, 2).bytes() + b.encoded() + a.encoded()
        with self.assertRaisesRegex(ImageError, "sequence mismatch"):
            checked(data)

    def test_crc_and_arithmetic_fail_independently(self):
        """A malformed mathematical result can still carry a valid transport CRC."""
        row = ImageRecord.predict(0, Request(42, 500, 39), 8)
        incorrect = replace(row, reply=replace(row.reply, cash=row.reply.cash + 1))
        header = ImageHeader(8, 1).bytes()
        data = header + incorrect.encoded()
        self.assertEqual(list(Reader(BytesIO(data)).records(check_math=False)), [incorrect])
        with self.assertRaisesRegex(ImageError, "arithmetic reply differs"):
            checked(data)
        damaged = header + row.encoded()[:-1] + bytes([row.encoded()[-1] ^ 4])
        with self.assertRaisesRegex(ImageError, "CRC mismatch"):
            checked(damaged)

    def test_parse_rejects_reserved_and_malformed_fields(self):
        """Only assigned header and wire bits may be set."""
        for raw in (b"BAD2" + bytes(8), HEADER.pack(b"NOM1", 3, 1, 0, 2),
                    HEADER.pack(b"NOM1", 3, 0, 5, 2)):
            with self.subTest(raw=raw), self.assertRaises(ImageError):
                checked(raw)
        for flags, limit in ((4, 0), (1, 9)):
            with self.subTest(flags=flags), self.assertRaises(ImageError):
                decode_order(ORDER.pack(11, 7, 64, 5, 2, flags, limit))
        for status, flags in ((7, 0), (0, 4)):
            with self.subTest(status=status), self.assertRaises(ImageError):
                decode_reply(REPLY.pack(11, status, flags, 6, 4, 2))
        for shape in ((0, 0), (8, MAX_RECORDS+1), (32, 1)):
            with self.subTest(shape=shape), self.assertRaises(ImageError):
                ImageHeader(*shape)


class ImageFiles(unittest.TestCase):
    def setUp(self):
        """Create an isolated directory for failed conversions and round trips."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.orders = root / "orders.csv"
        self.image = root / "orders.nom"
        self.tape = root / "tape.txt"

    def prepare(self):
        """Write one valid limit and one deliberate hardware validation failure."""
        header = ",".join(ORDER_FIELDS)
        self.orders.write_text(header + "\n3,AAA,buy,8,8,0,floor,65\n3,AAA,sell,3,0,50,ceil,\n",
                               encoding="utf-8")

    def test_round_trip_emits_normal_tape_and_diagnostics(self):
        """Binary conversion independently preserves tags and replays an invalid order."""
        self.prepare()
        self.assertEqual(compile_image(self.orders, self.image, 8), 2)
        self.assertEqual(image_to_tape(self.image, self.tape), 2)
        rows = self.tape.read_text().splitlines()
        self.assertEqual(rows[0], "3 800 8 0 0 0 1 41 3 0 0 40 0 40 0 55")
        self.assertEqual(rows[1], "3 300 0 32 1 1 0 0 3 1 1 0 0 0 0 1")
        report = explain(self.image, limit=1)
        self.assertEqual(report["outcomes"]["BAD_QUANTITY"], 1)
        self.assertEqual(report["outcomes"]["OK"], 1)
        example, = report["examples"]
        self.assertEqual(example["result"]["cash"], 64)

    def test_failed_validation_cannot_overwrite_artifacts(self):
        """Validate any existing image and detect filename aliases before writing a tape."""
        request = Request(1, 5, 17)
        self.image.write_bytes(raw_image(ImageHeader(0, 1), [request])[:-1])
        self.tape.write_bytes(b"prior")
        with self.assertRaisesRegex(ImageError, "CRC"):
            image_to_tape(self.image, self.tape)
        self.assertEqual(self.tape.read_bytes(), b"prior")
        self.prepare()
        self.image.unlink()
        os.link(self.orders, self.image)
        with self.assertRaisesRegex(BatchError, "same file"):
            compile_image(self.orders, self.image, 8)


class CaptureChecking(unittest.TestCase):
    def test_transaction_timeline_uses_cycle_and_signal_times(self):
        """An output hold contributes delay without changing compute latency."""
        events = read_events(("req 5 7 a 2 0 0 0 0 0", "rsp 94 90 7 0 0 14 0 14 0"))
        checker = Audit(0)
        for event in events:
            checker.apply(event)
        result = checker.finish()
        self.assertEqual(result["first_visible_latency"], {85: 1})
        self.assertEqual(result["output_hold_cycles"], 4)
        self.assertEqual(result["statuses"], {"OK": 1})

    def test_reject_invalid_edges_and_negative_flags(self):
        """Validate row lengths and fields before they can change scoreboard state."""
        bad = (
            ("", "empty"),
            ("req 1 7 a 0 0 0 1 0 4", "disabled"),
            ("req 1 7 a 0 0 0 2 0 0", "1 unsigned"),
            ("rsp 2 3 7 0 0 0 0 0 0", "visibility"),
            ("req 1 7 a 0 0 0 1 -1 0", "unsigned hex"),
            ("rsp 1 0 7 e 0 0 0 0 0", "valid Kind"),
            ("req 1 7 a 0 0 0 0 0 0\nreq 1 7 a 0 0 0 0 0 0", "advance"),
        )
        for rows, error in bad:
            with self.subTest(rows=rows), self.assertRaisesRegex(TraceError, error):
                list(read_events(rows.splitlines() or [""]))

    def test_stale_unmatched_and_corrupted_responses(self):
        """Result data still must fit the active request and expected response cycle."""
        req = Request(3, 15, 1)
        predicted = calculate(req, 0)
        audit = Audit(0)
        with self.assertRaisesRegex(TraceError, "associated request"):
            audit.apply(ReplyEvent(90, 90, predicted))
        audit.apply(RequestEvent(4, req))
        with self.assertRaisesRegex(TraceError, "already in flight"):
            audit.apply(RequestEvent(5, req))
        with self.assertRaisesRegex(TraceError, "final response"):
            audit.finish()
        with self.assertRaisesRegex(TraceError, "unexpected first-visible"):
            audit.apply(ReplyEvent(91, 88, predicted))
        with self.assertRaisesRegex(TraceError, "observed"):
            audit.apply(ReplyEvent(90, 89, replace(predicted, cash=16)))
