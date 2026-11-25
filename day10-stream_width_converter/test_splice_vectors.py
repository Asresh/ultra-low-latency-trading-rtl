"""Known-answer packet splices for the independent tape builder."""
import unittest

from splice_vectors import evaluate, input_words, lane_word


class SpliceVectorTest(unittest.TestCase):
    def test_known_crc_ignores_nonselected_lanes(self):
        data = b"123456789"
        desc = (250,9,0,0,0,0,0,0,0,True)
        report, wire = evaluate(desc, list(input_words(data, 4, 3)), 4, 12, 2)
        self.assertEqual(report, (250,9,13,0))
        self.assertEqual(wire, data+b"\x26\x39\xf4\xcb")

    def test_patch_coordinates_follow_trim_and_precede_prefix(self):
        data = b"abcdef"
        desc = (1,6,1,1,2,lane_word(b"XY"),1,5,lane_word(b"1234"),False)
        report, wire = evaluate(desc, list(input_words(data, 8, 1)), 8, 12, 2)
        self.assertEqual(report, (1,6,6,0))
        self.assertEqual(wire, b"XYb1d3")

    def test_zero_body_can_publish_crc_only(self):
        data = b"discard me"
        desc = (3,10,10,0,0,0,0,0,0,True)
        report, wire = evaluate(desc, list(input_words(data, 3, 1)), 3, 12, 2)
        self.assertEqual(report, (3,10,4,0))
        self.assertEqual(wire, b"\0"*4)

    def test_edited_length_checks_before_narrowing(self):
        words = list(input_words(b"abcd", 4, 0))
        for prefix_length, expected_count, status in (
                (65527, 65535, 0), (65528, 0, 4), (131071, 0, 4)):
            with self.subTest(prefix_length=prefix_length):
                desc = (4,4,0,0,prefix_length,0,0,0,0,True)
                report, wire = evaluate(desc, words, 4, 4, 131071)
                self.assertEqual(report, (4,4,expected_count,status))
                self.assertEqual(len(wire), expected_count)
