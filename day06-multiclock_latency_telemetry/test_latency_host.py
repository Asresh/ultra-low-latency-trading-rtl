"""Model, wire-format and measurement-tool checks using independent examples."""

import io
from fractions import Fraction
import unittest

from latency_capture import (
    CaptureError, histogram_frames, output_events, read_event, read_words, trace_frames,
)
from latency_profile import (
    PathBudget, Profile, empirical_gap, parse_path, profile_report, quantile,
)
from latency_reference import (
    LIMIT64, HALF64, CapturedTrace, ClassHistogram, Correlator, Geometry,
    Observation, Report, Status, bounded_trace, bucket_bounds, bucket_of, histogram_words,
)
from pps_calibrate import (
    CalibrationError, PPS, estimate_period, nearest_fixed, read_pps,
)
from verify_latency_capture import verify_histograms, verify_traces


def event_pair(tag, delay, group=0, timestamp=1000, probe_a=1, probe_b=2):
    a = Observation(200, tag, group, probe_a, False)
    b = Observation(200 + delay, tag, group, probe_b, True)
    return Report.pair(a, b, timestamp)


class ModelRules(unittest.TestCase):
    def test_group_is_part_of_hash_and_key(self):
        g = Geometry()
        self.assertEqual(g.key_bits(Observation(0, 0x587, 3, 1, False)), 0x3587)
        event = Observation(0, 0x543, 2, 1, False)
        self.assertEqual(g.set_of(event), 6)

    def test_out_of_order_and_counter_wrap(self):
        a = Observation(LIMIT64 - 1, 11, 0, 1, False)
        b = Observation(3, 11, 0, 2, True)
        report = Report.pair(b, a, 10)
        self.assertEqual(report.duration, 5)
        self.assertEqual(report.status, Status.MATCH)
        self.assertEqual((report.start_probe, report.end_probe), (1, 2))
        self.assertEqual(read_event(report.transport_word()), report)
        wrong = Observation(LIMIT64 - 3, 11, 0, 0, True)
        invalid = Report.pair(a, wrong, 10)
        self.assertEqual(invalid.status, Status.NEGATIVE)
        self.assertEqual(read_event(invalid.transport_word()), invalid)

    def test_same_endpoint_is_not_a_pair(self):
        a = Observation(1, 1, 0, 0, False)
        with self.assertRaisesRegex(ValueError, "opposite"):
            Report.pair(a, a, 10)
        with self.assertRaisesRegex(ValueError, "same key"):
            Report.pair(a, Observation(2, 2, 1, 1, True), 10)

    def test_held_model_report_and_flush_fence(self):
        g = Geometry(scan_log=2)
        f = Correlator(g)
        start = Observation(0, 3, 0, 0, False)
        end = Observation(1, 3, 0, 1, True)
        first = f.step(tick=1, timeout=20, observation=start, sink_ready=False, armed=True)
        self.assertTrue(first.ready)
        hold = f.step(tick=2, timeout=20, observation=end, sink_ready=False, armed=True)
        self.assertFalse(hold.ready)
        self.assertEqual(hold.pending, 1)
        self.assertEqual(hold.visible.status, Status.STORED)
        released = f.step(tick=3, timeout=20, observation=end, sink_ready=True, armed=True, flush=True)
        self.assertFalse(released.ready)
        self.assertEqual(released.visible, hold.visible)
        fresh = f.step(tick=4, timeout=20, observation=end, sink_ready=True, armed=True)
        self.assertTrue(fresh.ready)
        self.assertIsNone(fresh.visible)

    def test_bucket_range_edges_and_saturation(self):
        plain = [0, 1, 2, 3, 4, 7, 8, 15, 16, 31, 32, LIMIT64]
        self.assertEqual([bucket_of(x, 8) for x in plain], [0, 1, 2, 3, 4, 4, 5, 5, 6, 6, 7, 7])
        self.assertEqual(bucket_bounds(4, 8), (4, 7))
        self.assertEqual(bucket_bounds(7, 8), (32, LIMIT64))
        tallies = ClassHistogram(8, 2)
        for duration in [0, 8, 9, 9, 9, 9, LIMIT64, 1]:
            tallies.add(duration)
        self.assertTrue(tallies.overflow)
        self.assertEqual((tallies.minimum, tallies.maximum), (0, LIMIT64))
        self.assertEqual((tallies.count, tallies.total, tallies.buckets[5]), (3, LIMIT64, 3))


class WireFailures(unittest.TestCase):
    def test_text_binary_and_csv(self):
        event = event_pair(2, 7)
        word = event.transport_word()
        text = io.StringIO(f"0x{word:080X} # row 1\n\n{word:080x}\n")
        raw = io.BytesIO(word.to_bytes(40, "little"))
        self.assertEqual(list(read_words(text, 320)), [word, word])
        self.assertEqual(list(read_words(raw, 320, True)), [word])
        out = io.StringIO()
        self.assertEqual(output_events(iter([word]), True, out), 1)
        self.assertIn("MATCH", out.getvalue())
        with self.assertRaisesRegex(CaptureError, "incomplete"):
            list(read_words(io.BytesIO(b"x" * 39), 320, True))
        with self.assertRaisesRegex(CaptureError, "line 1"):
            list(read_words(io.StringIO("0777"), 320))

    def test_corrupt_event_details(self):
        word = event_pair(33, 7).transport_word()
        with self.assertRaisesRegex(CaptureError, "reserved"):
            read_event(word | (1 << 260))
        with self.assertRaisesRegex(CaptureError, "impossible"):
            read_event(word ^ (1 << 70))
        with self.assertRaisesRegex(CaptureError, "non-match"):
            read_event(word ^ (1 << 317))
        damaged = (word & ~(((1 << 64)-1) << 64) & ~(7 << 317)) | (4 << 317)
        with self.assertRaises(CaptureError):
            read_event(damaged)

    def test_histogram_class_and_rank_validation(self):
        data = list(histogram_words([[1, 2, 4, 7, 33, 37], [], [1, 1]], 10, 0, 55))
        frame = list(histogram_frames(data, 3))[0]
        self.assertEqual((frame.frame_id, frame.cut_tick), (0, 55))
        group = frame.groups[0]
        self.assertEqual(group.percentile_interval(50), (4, 7))
        self.assertEqual(group.percentile_interval(100), (32, 37))
        self.assertEqual(group.mean(), Fraction(14))
        self.assertIsNone(frame.groups[1].mean())
        with self.assertRaisesRegex(CaptureError, "EOF"):
            list(histogram_frames(data[:-1], 3))
        with self.assertRaisesRegex(CaptureError, "expected class"):
            list(histogram_frames([data[0], data[1] ^ (1 << 116), *data[2:]], 3))
        with self.assertRaisesRegex(CaptureError, "reserved"):
            list(histogram_frames(data[:3] + [data[3] ^ (1 << 40)] + data[4:], 3))
        with self.assertRaisesRegex(CaptureError, "counter"):
            wrapped = list(histogram_words([[6]], 8, 0, 2, counter_width=2))
            wrapped[1] ^= 1 << 73
            list(histogram_frames(wrapped, 1))
        with self.assertRaisesRegex(CaptureError, "sequence"):
            list(histogram_frames(data + data, 3))

    def test_window_wrap_and_trace_restrictions(self):
        probe = [event_pair(i, i+1, timestamp=500+i) for i in range(12)]
        keep = bounded_trace(probe, 8, 2, 0, 10, frame_id=LIMIT64 & 0xFFFFFFFF)
        self.assertIsInstance(keep, CapturedTrace)
        r = list(trace_frames(keep.serialize(), first_id=None))[0]
        self.assertEqual((r.trigger_offset, r.trigger_tick), (5, 509))
        self.assertEqual([i.tag for i in r.records], list(range(4, 12)))
        second = CapturedTrace(0, keep.reports, keep.trigger_offset, keep.threshold_trigger)
        rollover = list(trace_frames([*keep.serialize(), *second.serialize()], first_id=None))
        self.assertEqual([item.frame_id for item in rollover], [0xFFFFFFFF, 0])
        self.assertIsNone(bounded_trace(probe, 8, 3, 0, 12))
        words = list(keep.serialize())
        with self.assertRaisesRegex(CaptureError, "offset"):
            list(trace_frames([words[0] | (0xFF << 16), *words[1:]], first_id=None))
        with self.assertRaisesRegex(CaptureError, "indexed record"):
            list(trace_frames([words[0] ^ (1 << 42), *words[1:]], first_id=None))

    def test_capture_cross_checks(self):
        reports = [event_pair(i, i+5, timestamp=20+i) for i in range(8)]
        raw = [(r.observed_tick+2, r) for r in reports]
        h = list(histogram_frames(
            histogram_words([[r.duration for r in reports[:3]]], 8, 0, 24), 1))
        verify_histograms(raw, h)
        with self.assertRaisesRegex(CaptureError, "snapshot 0"):
            verify_histograms(raw[1:], h)
        trace = bounded_trace(reports, 5, 2, 0, 9)
        decoded = list(trace_frames(trace.serialize()))
        verify_traces(reports, decoded)
        with self.assertRaisesRegex(CaptureError, "contiguous"):
            verify_traces([reports[0], *reports[3:]], decoded)


class ClockAndProfiling(unittest.TestCase):
    def test_precise_q32_rounding(self):
        self.assertEqual(nearest_fixed(Fraction(1, 2), 1), 0)
        self.assertEqual(nearest_fixed(Fraction(3, 2), 1), 2)
        self.assertEqual(nearest_fixed(Fraction(1, 3), 8), 3)

    def test_pps_wrap_loss_and_noisy_point(self):
        samples = []
        start = LIMIT64 - 2 * 125000000
        for i in range(30):
            if i in (1, 14, 17):
                continue
            glitch = 8000 if i == 10 else 0
            samples.append(PPS(i, (start + i * 125000000 + glitch) & LIMIT64, i * 10**9))
        estimate = estimate_period(samples)
        self.assertEqual(estimate.period, 8)
        self.assertGreater(estimate.excluded_windows, 0)
        self.assertEqual(estimate.phase_at_anchor, 0)
        self.assertEqual(estimate.rate_word, 8 << 32)
        self.assertEqual(estimate.as_json(armed=True)["writes"][-1]["value"], "0x00000011")

    def test_nominal_clock_reject_and_bad_input(self):
        rows = io.StringIO("second,tick,reference_ns\n1,0x40,1000\n2,0x50,2000\n")
        points = list(read_pps(rows))
        self.assertEqual(points, [PPS(1, 64, 1000), PPS(2, 80, 2000)])
        with self.assertRaisesRegex(CalibrationError, "three"):
            estimate_period(points)
        with self.assertRaisesRegex(CalibrationError, "strictly"):
            estimate_period([*points, *points])
        with self.assertRaisesRegex(CalibrationError, "drift"):
            estimate_period([PPS(i, i * 10**7, i * 10**9) for i in range(10)])
        with self.assertRaisesRegex(CalibrationError, "columns"):
            list(read_pps(io.StringIO("issued,time\n10,30\n")))

    def test_paths_are_independent_under_budget(self):
        a, baseline = Profile(), Profile()
        for i, delay in enumerate((4, 8, 15, 20, 42)):
            a.add(event_pair(i, delay, timestamp=100+i*3))
            baseline.add(event_pair(i, delay//2, timestamp=100+i*4))
        a.add(event_pair(18, 99, group=1, timestamp=122))
        key = parse_path("0:1:2")
        budgets = [PathBudget(*key, Fraction(1, 2), 16, 3), PathBudget(1, 1, 2, Fraction(1), 80, 3)]
        p = profile_report(a, [Fraction(1, 2)], None, baseline, 8, budgets)
        self.assertEqual(p["observations"], 6)
        self.assertEqual(p["by_path"][0]["quantiles"], {"50.0": 15})
        self.assertEqual(p["by_path"][0]["p99_change_ticks"], 21)
        self.assertEqual(sum(w["samples"] for w in p["by_path"][0]["windows"]), 5)
        self.assertEqual(p["budgets"][0]["verdict"], "met")
        self.assertEqual(p["budgets"][1]["verdict"], "insufficient_samples")
        self.assertEqual(a.meets(PathBudget(*key, Fraction(1), 40, 1))["verdict"], "over_budget")

    def test_rank_and_cdf_exact_examples(self):
        self.assertEqual(quantile([3, 4], Fraction(1, 2)).value, 3)
        self.assertEqual(quantile([3, 4], Fraction(51, 100)).value, 4)
        self.assertIsNone(quantile([], Fraction(1)))
        self.assertEqual(empirical_gap([1, 1, 2, 3], [1, 3, 4, 5]),
                         {"largest_cdf_gap": 0.5, "cutoff_ticks": 2})
        self.assertIsNone(empirical_gap([], [1]))
        p = Profile()
        p.add(event_pair(33, 7, timestamp=100))
        with self.assertRaisesRegex(CaptureError, "decreasing"):
            p.add(event_pair(34, 7, timestamp=99))


if __name__ == "__main__":
    unittest.main()
