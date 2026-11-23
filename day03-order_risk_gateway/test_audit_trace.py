"""Host transport tests independent of the SystemVerilog audit transmitter."""

import io
import struct
import unittest
import zlib

from audit_trace import CaptureError, check_trace, expected_rows, file_words, frame_records
from risk_model import Decision, PROFILES, encode_reply


PROFILE = PROFILES["narrow"]
ORDER = Decision(0, 95, 2, 1, 1, 3, 1, 0, 0, 1, 8, 11, 88, 124, 500, 2)


def encode_frame(decision=ORDER, frame_id=0, version=1, pad=0):
    """Assemble a test audit frame with a matching header, payload, and trailing CRC."""
    n = PROFILE.response_bits
    payload = encode_reply(PROFILE, decision) | pad << n
    data = b"RSK1" + struct.pack("<II", (version << 24) | n, frame_id)
    data += payload.to_bytes(((n + 31) // 32) * 4, "little")
    return data + struct.pack("<I", zlib.crc32(data))


class AuditTraceTests(unittest.TestCase):
    def binary_records(self, data, first_id=0):
        """Pack test capture words in the binary byte order consumed by the parser."""
        return list(frame_records(file_words(io.BytesIO(data), True), PROFILE, first_id))

    def test_valid_binary_and_hex_agree(self):
        """Check that valid binary and hex agree."""
        frame = encode_frame()
        words = struct.unpack(f"<{len(frame) // 4}I", frame)
        text = "\n".join(f"{word:08x}" for word in words)
        encoded = list(frame_records(file_words(io.StringIO(text), False), PROFILE))
        self.assertEqual(encoded, self.binary_records(frame))
        self.assertEqual(encoded[0].decision, ORDER)

    def test_frame_sequence_wrap(self):
        """Check that frame sequence wrap."""
        stream = encode_frame(frame_id=0xFFFF_FFFF) + encode_frame(frame_id=0)
        self.assertEqual([frame.frame_id for frame in self.binary_records(stream, None)],
                         [0xFFFF_FFFF, 0])

    def test_duplicate_frame_id_is_rejected(self):
        """Check that duplicate frame id is rejected."""
        with self.assertRaisesRegex(CaptureError, "frame id"):
            self.binary_records(encode_frame() * 2)

    def test_crc_detects_a_corrupted_payload(self):
        """Check that CRC detects a corrupted payload."""
        data = bytearray(encode_frame())
        data[13] ^= 1
        with self.assertRaisesRegex(CaptureError, "CRC"):
            self.binary_records(data)

    def test_unexpected_magic_version_and_profile(self):
        """Check that unexpected magic version and profile."""
        corrupt = b"NOPE" + encode_frame()[4:]
        with self.assertRaisesRegex(CaptureError, "magic"):
            self.binary_records(corrupt)
        with self.assertRaisesRegex(CaptureError, "version"):
            self.binary_records(encode_frame(version=2))
        words = file_words(io.BytesIO(encode_frame()), True)
        with self.assertRaisesRegex(CaptureError, "width"):
            list(frame_records(words, PROFILES["wide"]))

    def test_crc_correct_padding_is_still_invalid(self):
        """Check that CRC correct padding is still invalid."""
        with self.assertRaisesRegex(CaptureError, "padding"):
            self.binary_records(encode_frame(pad=1))

    def test_partial_word_and_part_header(self):
        """Check that partial word and part header."""
        frame = encode_frame()
        with self.assertRaisesRegex(CaptureError, "partial"):
            self.binary_records(frame[:-1])
        with self.assertRaisesRegex(CaptureError, "truncated"):
            self.binary_records(frame[:-4])

    def test_invalid_hex_words_are_diagnostic(self):
        """Check that invalid hex words are diagnostic."""
        with self.assertRaisesRegex(CaptureError, "line 2"):
            list(file_words(io.StringIO("314B5352\nnope"), False))

    def test_golden_model_correlates_values_and_length(self):
        """Check that golden model correlates values and length."""
        frames = self.binary_records(encode_frame())
        count, coverage = check_trace(iter(frames), iter([ORDER]))
        self.assertEqual(count, 1)
        self.assertEqual(sum(coverage.values()), 1)
        with self.assertRaisesRegex(CaptureError, "missing"):
            check_trace(iter(frames), iter([ORDER, ORDER]))
        with self.assertRaisesRegex(CaptureError, "unexpected"):
            check_trace(iter(frames), iter([]))
        wrong = Decision(0, 96, 2, 1, 1, 3, 1, 0, 0, 1, 8, 11, 88, 124, 500, 2)
        with self.assertRaisesRegex(CaptureError, "tag 95 != 96"):
            check_trace(iter(frames), iter([wrong]))

    def test_invalid_reference_row_is_diagnostic(self):
        """Check that invalid reference row is diagnostic."""
        with self.assertRaisesRegex(CaptureError, "line 1"):
            list(expected_rows(io.StringIO("1 2 3\n")))


if __name__ == "__main__":
    unittest.main()
