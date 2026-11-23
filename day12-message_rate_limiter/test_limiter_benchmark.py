"""Keep steady-state throughput distinct from full-trace bus utilization."""

import tempfile
import unittest
from pathlib import Path

from limiter_benchmark import Run, benchmark, run_summary, uninterrupted
from limiter_capture import prepare
from limiter_protocol import Manifest, PROFILES, write_rows
from limiter_scenarios import boundary_cases, parallel_burst


class BenchmarkTest(unittest.TestCase):
    """Validate spans, exact ratios, and data from recognized trace campaigns."""

    def fixture(self, pattern, name):
        """Export one reference fixture with all information needed to audit it."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        manifest = Manifest("default", name, 27, len(pattern.inputs), pattern.geometry)
        prepare(path, manifest, pattern.inputs)
        write_rows(path / "capture.hex", (item.words() for item in pattern.expected))
        return path

    def test_uninterrupted_detector_keeps_first_and_last_edges(self):
        """Check separated runs, singleton delivery, and empty capture behavior."""
        self.assertEqual(list(uninterrupted((1, 2, 4, 7, 8, 9))), [
            Run(1, 2), Run(4, 4), Run(7, 9),
        ])
        self.assertEqual(run_summary((1, 2, 4, 7, 8, 9), 16), {
            "transfers": 6,
            "first_active": 1,
            "last_active": 9,
            "runs": 3,
            "longest_run": {"first": 7, "last": 9, "size": 3},
            "active_decisions_per_cycle": "2/3",
            "capture_decisions_per_cycle": "3/8",
        })
        self.assertIsNone(run_summary((), 0)["longest_run"])
        self.assertEqual(run_summary((), 9)["capture_decisions_per_cycle"], "0")

    def test_unordered_or_out_of_bounds_edges_are_rejected(self):
        """Avoid recording imaginary throughput from corrupted cycle values."""
        for sequence, total in (((2, 1), 5), ((2, 2), 5), ((-1,), 3), ((3,), 3)):
            with self.subTest(sequence=sequence):
                with self.assertRaises(ValueError):
                    run_summary(sequence, total)

    def test_unblocked_burst_is_one_per_cycle_after_warmup(self):
        """Separate one-cycle decision latency from warmup and drain edges."""
        plan = parallel_burst(PROFILES["default"], 256)
        record = benchmark(self.fixture(plan, "burst"))
        self.assertEqual(record["latencies"]["p50"], 1)
        self.assertEqual(record["latencies"]["p95"], 1)
        self.assertEqual(record["successful_issues"]["first_active"], 0)
        self.assertEqual(record["deliveries"]["first_active"], 1)
        self.assertEqual(record["deliveries"]["active_decisions_per_cycle"], "1")
        self.assertNotEqual(record["deliveries"]["capture_decisions_per_cycle"], "1")
        self.assertEqual(record["deliveries"]["longest_run"]["size"], 266)

    def test_reset_discards_dont_raise_the_delivery_throughput(self):
        """Only complete handshakes contribute to response-rate estimates."""
        plan = boundary_cases(PROFILES["default"])
        record = benchmark(self.fixture(plan, "boundary"))
        self.assertEqual(record["reset_discarded"], 1)
        self.assertGreater(record["sink_stalls"], 0)
        self.assertEqual(
            record["deliveries"]["transfers"] + record["reset_discarded"], len(plan.accepted)
        )
        self.assertGreater(record["latencies"]["max"], record["latencies"]["min"])


if __name__ == "__main__":
    unittest.main()
