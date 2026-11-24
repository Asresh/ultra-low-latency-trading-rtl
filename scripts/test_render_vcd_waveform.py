"""Known-answer checks for the snapshot parser and SVG renderer."""

from decimal import Decimal
from io import StringIO
import unittest
from xml.etree import ElementTree

from render_vcd_waveform import grid_times, read_vcd, render


FIXTURE = """$timescale
  10 ps
$end
$scope module tb $end
$var wire 1 ! ready $end
$var wire 4 A tag [3:0] $end
$var wire 4 a other_tag [3:0] $end
$scope module dut $end
$var wire 1 ! ready $end
$upscope $end
$upscope $end
$enddefinitions $end
$dumpvars
x!
bx A
b11 a
$end
#100
0!
b01 A
#150
1!
b10 A
#200
0!
bz A
#225
1!
0!
#300
b1111 A
"""


class WaveformTests(unittest.TestCase):
    """Check crop boundaries, aliases, identifier case and SVG validity."""

    def test_crop_includes_state_on_the_left(self):
        """Retain the last value through the inclusive left boundary."""
        channels = read_vcd(
            StringIO(FIXTURE), ["tb.ready=Ready", "tb.dut.ready", "tb.tag"],
            Decimal("1.5"), Decimal("2.5"),
        )
        self.assertEqual(channels[0].label, "Ready")
        self.assertEqual(channels[0].events, channels[1].events)
        self.assertEqual(channels[0].events, [(Decimal("1.5"), "1"), (Decimal(2), "0")])
        self.assertEqual(channels[2].events, [(Decimal("1.5"), "10"), (Decimal(2), "z")])

    def test_vector_codes_are_case_sensitive_and_svg_text_is_escaped(self):
        """Separate vector codes A and a and preserve user-provided diagram labels."""
        start, end = Decimal(0), Decimal(3)
        channels = read_vcd(
            StringIO(FIXTURE), ["tb.tag=Offer <tag>", "tb.other_tag"], start, end,
        )
        self.assertEqual(channels[0].events[0][1], "x")
        self.assertEqual(channels[1].events, [(start, "11")])
        report = render(channels, start, end, "RTL & test")
        root = ElementTree.fromstring(report)
        self.assertEqual(root.attrib["role"], "img")
        self.assertIn("0x3", report)
        self.assertIn("Offer &lt;tag&gt;", report)
        self.assertIn("RTL &amp; test", report)

    def test_unknown_signal_reports_its_full_path(self):
        """Reject a misspelled hierarchical signal rather than drawing an empty lane."""
        with self.assertRaisesRegex(ValueError, r"'tb\.missing'"):
            read_vcd(StringIO(FIXTURE), ["tb.missing"], Decimal(0), Decimal(3))

    def test_grid_keeps_edges_in_a_subnanosecond_window(self):
        """Keep fractional nanosecond boundaries and ordered intermediate ticks."""
        start, end = Decimal("0.011"), Decimal("0.097")
        ticks = list(grid_times(start, end))
        self.assertGreater(len(ticks), 3)
        self.assertEqual((ticks[0], ticks[-1]), (start, end))
        self.assertEqual(ticks, sorted(set(ticks)))


if __name__ == "__main__":
    unittest.main()
