"""Test portable cropping at actual resets, including lost work and bad suffixes."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from width_audit import AuditError, Transfer
from width_capture import CaptureError, Geometry, inspect, read_rows, write_capture
from width_excerpt import ExcerptError, epoch_summaries, epochs, extract, range_bounds, shift_rows
from width_model import Beat, Report


def normal_edge(first, tag):
    """Provide one legal one-byte frame and two independent retirements."""
    return [
        Transfer("input", first, Beat(42, 1, True, tag)),
        Transfer("output", first+2, Beat(42, 1, True, tag)),
        Transfer("report", first+2, Report(tag, 1)),
    ]


def raw_capture(events):
    """Serialize the standard test width and exact source edges."""
    file = BytesIO()
    write_capture(file, Geometry(4, 8, 128), events)
    return file.getvalue()


class EpochBoundaries(unittest.TestCase):
    def sample(self):
        """Make epochs with an abandoned frame, an empty reset, and complete traffic."""
        return [
            Transfer("reset", 12, None),
            *normal_edge(16, 37),
            Transfer("input", 26, Beat(0xABCD, 15, False, 6)),
            Transfer("reset", 30, None),
            Transfer("reset", 32, None),
            *normal_edge(35, 37),
        ]

    def test_split_rebase_and_preserve_closing_discard(self):
        """An excerpt keeps the reset needed to explain an abandoned input."""
        with TemporaryDirectory() as name:
            source = Path(name) / "all.wgc"
            output = Path(name) / "clip.wgc"
            source.write_bytes(raw_capture(self.sample()))
            summary = epoch_summaries(source)
            segments = summary["epochs"]
            self.assertEqual([row["first_cycle"] for row in segments], [12, 30, 32])
            self.assertEqual([row["packets"] for row in segments], [1, 0, 1])
            self.assertEqual(segments[0]["abandoned_by_reset"], 1)
            plan = extract(source, output, first=0, count=1)
            self.assertEqual(plan["cycles_subtracted"], 12)
            self.assertTrue(plan["closing_reset_included"])
            self.assertEqual(inspect(output)["abandoned_by_reset"], 1)
            with output.open("rb") as file:
                geo = Geometry.read(file)
                transferred = list(read_rows(file, geo))
            self.assertEqual(transferred[0], Transfer("reset", 0, None))
            self.assertEqual(transferred[-1], Transfer("reset", 18, None))

    def test_preserve_absolute_cycles_and_nonunique_tags(self):
        """Selection uses epoch numbers, so repeated tags do not confuse clipping."""
        with TemporaryDirectory() as name:
            source = Path(name) / "all.wgc"
            output = Path(name) / "clip.wgc"
            source.write_bytes(raw_capture(self.sample()))
            plan = extract(source, output, 1, 2, rebase=False)
            self.assertEqual(plan["cycles_subtracted"], 0)
            self.assertFalse(plan["closing_reset_included"])
            self.assertEqual(inspect(output)["packets"], 1)
            self.assertEqual(epoch_summaries(output)["epochs"][0]["first_cycle"], 30)

    def test_validate_later_epochs_before_writing_clipped_output(self):
        """A valid prefix cannot mask corrupted records after the selected range."""
        with TemporaryDirectory() as name:
            source = Path(name) / "all.wgc"
            output = Path(name) / "clip.wgc"
            malformed = bytearray(raw_capture(self.sample()))
            malformed[-1] ^= 0xFF
            source.write_bytes(malformed)
            output.write_bytes(b"prior")
            with self.assertRaisesRegex(CaptureError, "CRC mismatch"):
                extract(source, output, 0, 1)
            self.assertEqual(output.read_bytes(), b"prior")
            incomplete = self.sample() + [Transfer("input", 99, Beat(0x1, 1, True, 80))]
            source.write_bytes(raw_capture(incomplete))
            with self.assertRaisesRegex(AuditError, "still outstanding"):
                extract(source, output, 0, 1)

    def test_implicit_first_epoch_and_empty_capture(self):
        """A legacy capture without an initial reset still has an epoch-zero view."""
        initial = normal_edge(10, 5)
        parsed = list(epochs(initial))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].number, 0)
        self.assertEqual(parsed[0].first, 10)
        self.assertEqual(parsed[0].last, 12)
        self.assertEqual(list(epochs([])), [])
        with self.assertRaisesRegex(ExcerptError, "before"):
            list(shift_rows(initial, 11))

    def test_bad_range_and_alias_are_precise(self):
        """Reject overwriting the source and overly broad or impossible epoch requests."""
        for first, count in ((True,1), (-1,1), (1,0), (0,257)):
            with self.subTest(first=first, count=count), self.assertRaises(ExcerptError):
                range_bounds(first,count)
        with TemporaryDirectory() as name:
            source = Path(name) / "all.wgc"
            output = Path(name) / "clip.wgc"
            source.write_bytes(raw_capture(self.sample()))
            with self.assertRaisesRegex(CaptureError, "same path"):
                extract(source, source, 0, 1)
            with self.assertRaisesRegex(ExcerptError, "extends past"):
                extract(source, output, 2, 2)
            self.assertFalse(output.exists())
