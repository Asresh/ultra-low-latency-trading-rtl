"""Boundary tests for the partition-to-banked-replay simulation adapter."""

import csv
from pathlib import Path
import tempfile
import unittest

from replay_banked_bundle import HEADER, build, check, fixture
from replay_partition import read_bundle_parts
from replay_program import ReplayError


class BankedBundleTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.bundle = self.root / "bundle"
        fixture(self.bundle)
        self.capture = self.root / "capture.csv"
        self.vectors = self.root / "vectors.hex"

    def capture_rows(self):
        wall = 0
        for part in read_bundle_parts(self.bundle):
            for event in part.tape.events:
                yield [wall, part.index + 1, part.index % 2, f"{event.data:x}",
                       f"{event.keep:x}", int(event.last), event.tag, event.tick, event.tick, 1]
                wall += 1

    def write_capture(self, rows, header=HEADER):
        with self.capture.open("w", encoding="ascii", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(header)
            writer.writerows(rows)

    def test_build_and_accept_all_partition_beats(self):
        build(self.bundle, self.vectors, 3, 3, 100)
        self.assertGreater(len(self.vectors.read_text().splitlines()), 40)
        self.write_capture(self.capture_rows())
        self.assertEqual(check(self.bundle, self.capture), (6, 12))

    def test_geometry_and_simulation_bounds_fail_before_output(self):
        for depth, revision, cycles, message in (
            (1, 3, 100, "device holds 1"),
            (3, 2, 100, "more run IDs"),
            (3, 3, 1, "cycle budget"),
        ):
            with self.subTest(depth=depth, revision=revision, cycles=cycles):
                with self.assertRaisesRegex(ReplayError, message):
                    build(self.bundle, self.vectors, depth, revision, cycles)
                self.assertFalse(self.vectors.exists())

    def test_rejects_wrong_run_identity_and_missing_tail(self):
        rows = list(self.capture_rows())
        rows[2][1] = 1
        self.write_capture(rows)
        with self.assertRaisesRegex(ReplayError, "transfer 3"):
            check(self.bundle, self.capture)
        self.write_capture(list(self.capture_rows())[:-1])
        with self.assertRaisesRegex(ReplayError, "wrong number of transferred beats"):
            check(self.bundle, self.capture)

    def test_rejects_incomplete_capture_schema(self):
        rows = list(self.capture_rows())
        self.write_capture(rows, HEADER[:-1])
        with self.assertRaisesRegex(ReplayError, "capture header"):
            check(self.bundle, self.capture)
        rows[0].pop()
        self.write_capture(rows)
        with self.assertRaisesRegex(ReplayError, "number of fields"):
            check(self.bundle, self.capture)


if __name__ == "__main__":
    unittest.main()
