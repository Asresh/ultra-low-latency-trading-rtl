"""Check packet semantics with byte-level golden cases and independent properties."""

from dataclasses import replace
from itertools import product
import random
import unittest

from width_model import Beat, Drive, Gearbox, Report, Status, full, to_beats, validate_packet
from width_stream import Edge, QueueOracle


class PacketEncoding(unittest.TestCase):
    def test_byte_order_is_little_endian(self):
        """Selected lane zero travels first even when the input word is displayed in hex."""
        first, second = to_beats(bytes.fromhex("01 23 45 67 89 ab"), 4, 79, fill=0xCC)
        self.assertEqual(first, Beat(0x67452301, 15, False, 79))
        self.assertEqual(second, Beat(0xCCCCAB89, 3, True, 79))
        self.assertEqual(first.bytes(4) + second.bytes(4), bytes.fromhex("0123456789ab"))

    def test_all_tail_masks(self):
        """Only nonempty prefixes are legal final masks at every used bus width."""
        for width in (1, 3, 4, 5, 8):
            for mask in range(1 << width):
                with self.subTest(width=width, mask=mask):
                    raw = bytes(range(1, width + 1))
                    beat = Beat(int.from_bytes(raw, "little"), mask, True, 13)
                    lanes = [index for index in range(width) if (mask >> index) % 2]
                    is_prefix = bool(lanes) and lanes == list(range(len(lanes)))
                    report, payload = validate_packet([beat], width, 32)
                    self.assertEqual(report.length, len(lanes))
                    self.assertEqual(report.status, Status.OK if is_prefix else Status.BAD_KEEP)
                    expected = bytes(raw[index] for index in lanes) if is_prefix else None
                    self.assertEqual(payload, expected)

    def test_every_width_and_packet_remainder(self):
        """Retain actual bytes across boundary sizes and poison-filled padding lanes."""
        for width, initial in product(range(1, 17), range(1, 55)):
            raw = bytes((initial + index * 17) % 256 for index in range(initial))
            beats = tuple(to_beats(raw, width, 5, 0xA0))
            amount = (initial + width - 1) // width
            self.assertEqual(len(beats), amount)
            self.assertEqual(sum(beat.last for beat in beats), 1)
            self.assertTrue(beats[-1].last)
            self.assertEqual(validate_packet(beats, width, 55), (Report(5, initial), raw))

    def test_empty_and_illegal_fields_rejected_in_constructor(self):
        """The host model catches values beyond the HDL ports."""
        with self.assertRaisesRegex(ValueError, "one payload byte"):
            tuple(to_beats(b"", 8, 0))
        for width in (0, 17):
            with self.assertRaisesRegex(ValueError, "lane width"):
                tuple(to_beats(b"A", width, 0))
        for tag in (-1, 65536):
            with self.assertRaisesRegex(ValueError, "packet tag"):
                tuple(to_beats(b"A", 4, tag))
        with self.assertRaisesRegex(ValueError, "padding"):
            tuple(to_beats(b"A", 1, 0, 256))
        for beat in (Beat(1 << 32, 1), Beat(-1, 1), Beat(0, 16), Beat(0, -1)):
            with self.assertRaises(ValueError):
                beat.validate(4)

    def test_whole_frame_boundaries_required(self):
        """The packet oracle has one final beat, with no embedded frame delimiters."""
        examples = (
            (),
            (Beat(1, 1, False, 2),),
            (Beat(1, 1, True, 2), Beat(2, 1, True, 2)),
            (Beat(1, 1, True, 2), Beat(2, 1, False, 2)),
        )
        for example in examples:
            with self.subTest(example=example), self.assertRaisesRegex(ValueError, "one complete frame"):
                validate_packet(example, 1, 16)

    def test_first_fault_beats_later_faults(self):
        """Keep the first bad reason while counting selected bytes to the real end."""
        prefix = [
            Beat(0x44332211, 15, False, 4),
            Beat(0x88776655, 15, False, 6),
            Beat(0xCCDDAABB, 0b1010, True, 4),
        ]
        report, output = validate_packet(prefix, 4, 12)
        self.assertEqual(report, Report(4, 10, Status.BAD_TAG))
        self.assertIsNone(output)
        report, _ = validate_packet(prefix, 4, 3)
        self.assertEqual(report, Report(4, 10, Status.TOO_LONG))
        prefix[1] = replace(prefix[1], keep=0b1101)
        report, _ = validate_packet(prefix, 4, 4)
        self.assertEqual(report, Report(4, 9, Status.BAD_KEEP))

    def test_maximum_size_and_tail_width(self):
        """A frame exactly at the byte limit succeeds with a partial last beat."""
        data = b"012345678"
        beats = tuple(to_beats(data, 4, 21))
        self.assertEqual(beats[-1].keep, 1)
        good = validate_packet(beats, 4, 9)
        bad = validate_packet(beats, 4, 8)
        self.assertEqual(good, (Report(21, 9), data))
        self.assertEqual(bad, (Report(21, 9, Status.TOO_LONG), None))

    def test_report_saturates_even_after_reject(self):
        """Count beyond a 16-bit diagnostic field without wrapping."""
        middle = Beat(0xAABBCCDDEEFF0011, 255, False, 4)
        tail = replace(middle, last=True)
        long_frame = (middle,) * 8192 + (tail,)
        report, data = validate_packet(long_frame, 8, 128)
        self.assertEqual(report, Report(4, 65535, Status.TOO_LONG))
        self.assertIsNone(data)


class StoreAndForward(unittest.TestCase):
    def send(self, dut, payload, tag, *, ready=True, report_ready=True):
        """Send a complete clean frame and require immediate ingress acceptance."""
        timeline = []
        for beat in to_beats(payload, dut.input_width, tag):
            sample = dut.advance(Drive(True, beat, ready, report_ready))
            self.assertTrue(sample.input_ready)
            timeline.append(sample)
        return timeline

    def consume(self, dut):
        """Return packets, reports and edge stats while the egress drains."""
        beats = []
        reports = []
        for _ in range(130):
            sample = dut.advance(Drive())
            if sample.output is not None:
                beats.append(sample.output)
            if sample.report is not None:
                reports.append(sample.report)
            if not dut.pending and dut.report is None:
                return beats, reports, sample.counters
        self.fail("timed out draining store-and-forward model")

    def test_no_early_output(self):
        """Hold a partially written frame until its final beat validates."""
        dut = Gearbox(4, 8, 30)
        payload = bytes(range(18))
        timeline = self.send(dut, payload, 35)
        self.assertTrue(all(sample.output is None for sample in timeline))
        self.assertTrue(all(sample.report is None for sample in timeline))
        outputs, reports, _ = self.consume(dut)
        self.assertEqual(reports, [Report(35, 18)])
        self.assertEqual(outputs, list(to_beats(payload, 8, 35)))

    def test_output_and_report_stall_independently(self):
        """No new frame starts until the report slot can eventually accept its result."""
        dut = Gearbox(4, 8, 15)
        self.send(dut, b"abc", 3, ready=False, report_ready=False)
        idle = Drive(ready=False, report_ready=False)
        held = dut.observe(idle)
        self.assertFalse(held.input_ready)
        for _ in range(10):
            self.assertEqual(dut.advance(idle), held)
        result = dut.advance(Drive(ready=True, report_ready=False))
        self.assertIsNotNone(result.output)
        result = dut.observe(idle)
        self.assertIsNone(result.output)
        self.assertEqual(result.report, Report(3, 3))
        self.assertFalse(result.input_ready)
        self.assertTrue(dut.observe(Drive(report_ready=True)).input_ready)

    def test_both_banks_occupied(self):
        """Capacity is two committed packets, including a partly emitted head."""
        dut = Gearbox(4, 4, 11)
        self.send(dut, b"ABCDEF", 1, ready=False)
        self.send(dut, b"gh", 2, ready=False)
        self.assertEqual(len(dut.pending), 2)
        self.assertFalse(dut.observe(Drive()).input_ready)
        sample = dut.advance(Drive())
        self.assertFalse(sample.output.last)
        self.assertFalse(dut.observe(Drive()).input_ready)
        sample = dut.advance(Drive())
        self.assertTrue(sample.output.last)
        self.assertTrue(dut.observe(Drive()).input_ready)
        output, _, _ = self.consume(dut)
        self.assertEqual(output, [Beat(0x6867, 3, True, 2)])

    def test_reset_invalidates_capture_and_counters(self):
        """A reset retires nothing and discards both committed and incomplete frames."""
        dut = Gearbox(4, 4, 11)
        self.send(dut, b"ABCDEF", 1, ready=False)
        partial = Beat(0x12345678, 15, False, 8)
        dut.advance(Drive(True, partial, False))
        self.assertTrue(dut.input_packet)
        self.assertTrue(dut.pending)
        dut.advance(Drive(reset=True))
        now = dut.observe(Drive())
        self.assertEqual(now.counters, (0, 0, 0, 0))
        self.assertIsNone(now.output)
        self.assertIsNone(now.report)
        self.assertTrue(now.input_ready)

    def test_counters_saturate(self):
        """Valid completion, invalid completion and output bytes never wrap."""
        dut = Gearbox(2, 4, 7)
        limit = (1 << 32) - 1
        dut.accepted = dut.rejected = dut.emitted = dut.output_bytes = limit
        self.send(dut, b"ABC", 19)
        dut.advance(Drive(True, Beat(0, 0, True, 7)))
        actual = dut.observe(Drive())
        self.assertEqual(actual.counters, (limit,) * 4)
        self.assertEqual(actual.report, Report(7, 0, Status.BAD_KEEP))

    def test_full_queue_acceptance_depends_on_pre_edge_capacity(self):
        """Pop and push share a clock only when the new input already had room."""
        dut = Gearbox(1, 1, 3)
        self.send(dut, b"A", 1, ready=False)
        self.send(dut, b"B", 2, ready=False)
        third = Beat(67, 1, True, 3)
        view = dut.advance(Drive(True, third))
        self.assertFalse(view.input_ready)
        self.assertEqual(view.output.data, 65)
        view = dut.advance(Drive(True, third))
        self.assertTrue(view.input_ready)
        self.assertEqual(view.output.data, 66)
        self.assertEqual(dut.observe(Drive()).output.data, 67)

    def test_report_counter_tracks_each_packet(self):
        """Run shuffled lengths and verify exact cumulative counters."""
        rng = random.Random(913)
        dut = Gearbox(3, 5, 31)
        valid_bytes = 0
        passed = 0
        dropped = 0
        for index in range(160):
            data = rng.getrandbits(8 * 39).to_bytes(39, "little")[:rng.randint(1, 39)]
            self.send(dut, data, index)
            self.consume(dut)
            if len(data) <= 31:
                passed += 1
                valid_bytes += len(data)
            else:
                dropped += 1
            self.assertEqual(dut.observe(Drive()).counters, (passed, dropped, passed, valid_bytes))


class CutThrough(unittest.TestCase):
    def test_emits_before_final_input(self):
        """Four ingress bytes make a two-byte sink beat even before last appears."""
        dut = QueueOracle(4, 2, 6)
        dut.advance(Edge(Beat(0x44332211, 15, False, 7), True, False))
        view = dut.advance(Edge(Beat(), False, True))
        self.assertEqual(view.outgoing, Beat(0x2211, 3, False, 7))
        self.assertEqual(view.occupancy, 4)
        view = dut.advance(Edge(Beat(0x55, 1, True, 7), True, True))
        self.assertEqual(view.outgoing, Beat(0x4433, 3, False, 7))
        view = dut.advance(Edge(Beat(), False, True))
        self.assertEqual(view.outgoing, Beat(0x55, 1, True, 7))
        self.assertEqual(len(dut.data), 0)

    def test_does_not_combine_short_frames(self):
        """A terminal byte ends the output even when a different packet is queued."""
        dut = QueueOracle(4, 8, 12)
        dut.advance(Edge(Beat(0xBBAA, 3, True, 8), True, False))
        dut.advance(Edge(Beat(0x22, 1, True, 9), True, False))
        left = dut.advance(Edge(Beat(), False, True))
        right = dut.advance(Edge(Beat(), False, True))
        self.assertEqual(left.outgoing, Beat(0xBBAA, 3, True, 8))
        self.assertEqual(right.outgoing, Beat(0x22, 1, True, 9))

    def test_rejects_a_contract_violation(self):
        """The oracle detects invalid keep and packet tag changes."""
        dut = QueueOracle(4, 8, 12)
        with self.assertRaisesRegex(ValueError, "already be valid"):
            dut.advance(Edge(Beat(0, 5, True, 3), True, False))
        dut.advance(Edge(Beat(0, 15, False, 3), True, False))
        dut.advance(Edge(Beat(0, 15, True, 4), True, False))
        with self.assertRaisesRegex(ValueError, "changed tag"):
            dut.candidate()

    def test_invalid_geometry_fails_fast(self):
        """One input plus one output worth of elastic storage is required."""
        for geometry in ((0, 4, 8), (3, 0, 5), (8, 4, 11)):
            with self.subTest(geometry=geometry), self.assertRaises(ValueError):
                QueueOracle(*geometry)
