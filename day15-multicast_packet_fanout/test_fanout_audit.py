"""Known answers and mutation tests for the independent waveform capture audit."""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fanout_audit import audit
from fanout_model import PROFILES


EXAMPLE = Path("build/example")


class ExampleAuditTests(unittest.TestCase):
    """Exercise the audit against the checked introductory RTL capture."""

    def setUp(self):
        """Use copies of the test capture so that a mutation cannot change originals."""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / "inputs.hex"
        self.observed = Path(directory.name) / "observed.hex"
        self.source.write_bytes((EXAMPLE / self.source.name).read_bytes())
        self.observed.write_bytes((EXAMPLE / self.observed.name).read_bytes())

    def run_audit(self, allow_pending=False):
        """Audit temporary trace files using default project geometry."""
        return audit(PROFILES["default"], self.source, self.observed, allow_pending)

    def mutate(self, cycle, field, value):
        """Overwrite one observation field while preserving the trace grammar."""
        lines = self.observed.read_text().splitlines()
        fields = lines[cycle].split()
        fields[field] = f"{value:x}"
        lines[cycle] = " ".join(fields)
        self.observed.write_text("\n".join(lines) + "\n")

    def test_multicast_counts_and_reconstruction(self):
        """Identify copies by ordinal, and check the packet byte hash and release order."""
        result = self.run_audit()
        self.assertEqual(result["cycles"], 14)
        self.assertEqual(result["statuses"], {"FORWARDED": 3, "CONGESTED": 1})
        packets = {(entry["tag"], entry["port"]): entry for entry in result["deliveries"]}
        self.assertEqual(set(packets), {(0xa1, 0), (0xa1, 1), (0xb2, 1), (0xd4, 2)})
        first, second = packets[0xa1, 0], packets[0xa1, 1]
        self.assertEqual(first["ordinal"], second["ordinal"])
        self.assertEqual(first["sha256"], hashlib.sha256(bytes.fromhex("112233445566")).hexdigest())
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["first_after_input"], 1)
        self.assertEqual(second["first_after_input"], 7)
        self.assertLess(first["last_cycle"], second["first_cycle"])

    def test_bad_payload(self):
        """Reject a replica whose byte payload differs from the accepted source."""
        self.mutate(5, 5, 0)
        with self.assertRaisesRegex(ValueError, "data or cleared padding"):
            self.run_audit()

    def test_bad_mask(self):
        """Reject a route that follows the simultaneous write instead of the old mapping."""
        self.mutate(5, 12, 4)
        with self.assertRaisesRegex(ValueError, "snapshot mask"):
            self.run_audit()

    def test_missing_valid(self):
        """Reject a silently lost subscriber even if other ports transfer."""
        self.mutate(5, 3, 1)
        with self.assertRaisesRegex(ValueError, "port 1 output valid"):
            self.run_audit()

    def test_unstable_report(self):
        """Reject altered metadata while the report consumer keeps ready low."""
        self.mutate(6, 10, 0xbb)
        with self.assertRaisesRegex(ValueError, "held report changed"):
            self.run_audit()

    def test_bad_boundary(self):
        """Reject an early last beat even when data and tag are correct."""
        self.mutate(5, 4, 1)
        with self.assertRaisesRegex(ValueError, "packet boundary"):
            self.run_audit()

    def test_prefix_and_mismatched_length(self):
        """Accept an intentional prefix and reject accidental asymmetric truncation."""
        files = (self.source, self.observed)
        for path in files:
            path.write_text("\n".join(path.read_text().splitlines()[:7]) + "\n")
        with self.assertRaisesRegex(ValueError, "ended with live state"):
            self.run_audit()
        summary = self.run_audit(allow_pending=True)
        self.assertEqual(summary["pending"], {"input_frame": False, "report": True, "subscribers": 1})
        self.source.write_text(self.source.read_text() + self.source.read_text().splitlines()[0] + "\n")
        with self.assertRaisesRegex(ValueError, "trace lengths differ"):
            self.run_audit(allow_pending=True)


if __name__ == "__main__":
    unittest.main()
