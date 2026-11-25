"""Exercise host and fabric readback contracts without relying on RTL timing."""

from dataclasses import replace
import unittest

from dma_model import AxiFault, Capture, Engine, Fabric, Fatal, Phase, SparseMemory
from dma_protocol import (
    AxiResponse, Descriptor, FLAG_CRC, FLAG_IRQ, FLAG_VERIFY, Opcode, QueueConfig,
    Region, Ring, Status, crc32,
)


class DmaReadback(unittest.TestCase):
    def setUp(self):
        self.address = 0x3ff8
        self.aperture = Region(0x3000, 0x6000)
        self.payload = bytes(range(73))
        self.geometry = QueueConfig(Ring(0x1000, 4), Ring(0x2000, 4), self.aperture)
        self.readback = Descriptor(
            Opcode.READBACK, len(self.payload), self.address, 0xfeed, FLAG_CRC|FLAG_IRQ,
            0x3a, crc32(self.payload),
        )
        self.push = replace(self.readback, op=Opcode.PUSH, flags=FLAG_VERIFY|FLAG_IRQ, expected_crc=0)

    def engine(self, fault=None):
        """Build a queue with independently filled byte-addressed fabric memory."""
        memory = SparseMemory(0xa5)
        rules = () if fault is None else (fault,)
        model = Engine(Fabric(memory, rules))
        self.assertTrue(model.configure(0, self.geometry, seed=0xffffffff))
        return model

    def start(self, model, descriptor, capture=None):
        self.assertTrue(model.host_post(0, descriptor, capture))
        return model.start()

    def test_standalone_readback_samples_exact_bytes_without_a_packet(self):
        model = self.engine()
        model.fabric.memory.write(self.address, self.payload + b"padding")
        pending = self.start(model, self.readback)
        self.assertEqual(
            (pending.completion.status, pending.completion.length, pending.completion.crc),
            (Status.OK, len(self.payload), crc32(self.payload)),
        )
        reads = [
            (access.address, access.data) for access in model.fabric.trace
            if access.phase == Phase.READBACK and access.channel == "AR"
        ]
        self.assertEqual(reads, [(0x3ff8, 0), (0x4000, 8)])
        self.assertEqual(model.reap(0), [])
        self.assertEqual(model.emissions, [])
        self.assertEqual(model.queues[0].sq_head, 0xffffffff)
        retirement = model.ack_completion()
        self.assertEqual(model.queues[0].sq_head, 0)
        self.assertEqual(model.queues[0].cq_tail, 0)
        self.assertEqual(model.reap(0), [retirement])
        self.assertEqual((model.pull_bytes, model.push_bytes), (0, 0))
        self.assertTrue(model.queues[0].irq.request)

    def test_verify_flag_is_limited_to_push_and_crc_is_independent(self):
        for op in Opcode:
            with self.subTest(op=op):
                descriptor = replace(self.push, op=op)
                self.assertEqual(
                    descriptor.validate(self.aperture),
                    Status.OK if op == Opcode.PUSH else Status.BAD_DESC,
                )
        raw = self.readback.encode()
        self.assertEqual(Descriptor.decode(raw), self.readback)
        naked = replace(self.readback, flags=0)
        self.assertEqual(naked.validate(self.aperture), Status.BAD_DESC)

    def test_verified_push_checks_memory_after_every_payload_response(self):
        model = self.engine()
        pending = self.start(model, self.push, Capture.body(self.payload))
        trace = model.fabric.trace
        phases = [entry.phase for entry in trace]
        write_ack = [
            index for index, entry in enumerate(trace)
            if entry.phase == Phase.PUSH and entry.channel == "B"
        ]
        self.assertEqual(len(write_ack), 2)
        self.assertLess(write_ack[-1], phases.index(Phase.VERIFY))
        self.assertLess(
            max(index for index, phase in enumerate(phases) if phase == Phase.VERIFY),
            phases.index(Phase.COMPLETION),
        )
        self.assertEqual(pending.completion.status, Status.OK)
        self.assertEqual(model.fabric.memory.read(self.address, 80), self.payload + b"\xa5"*7)
        self.assertEqual(model.push_bytes, 0)
        model.ack_completion()
        self.assertEqual(model.push_bytes, len(self.payload))

    def test_read_xor_distinguishes_read_data_from_committed_capture(self):
        model = self.engine(AxiFault(Phase.VERIFY, address=0x4000, read_xor=0xff00))
        pending = self.start(model, self.push, Capture.body(self.payload))
        corrupted = bytearray(self.payload)
        corrupted[9] ^= 0xff
        self.assertEqual(pending.completion.crc, crc32(corrupted))
        self.assertEqual(pending.completion.status, Status.VERIFY_ERROR)
        self.assertEqual(pending.completion.length, 0)
        self.assertEqual(model.fabric.memory.read(self.address, len(self.payload)), self.payload)
        model.ack_completion()
        self.assertEqual((model.rejected, model.push_bytes), (1, 0))

    def test_final_beat_padding_changes_leave_digest_unchanged(self):
        # A transfer of 73 bytes includes one byte from its final aligned word.
        model = self.engine(AxiFault(Phase.VERIFY, address=self.address+72, read_xor=0xff00))
        result = self.start(model, self.push, Capture.body(self.payload)).completion
        self.assertEqual((result.status, result.crc), (Status.OK, crc32(self.payload)))

    def test_verify_transport_errors_and_protocol_errors_are_distinct(self):
        for last in (None, True):
            with self.subTest(last=last):
                fault = AxiFault(Phase.VERIFY, AxiResponse.SLVERR, 0x4000, last=last)
                model = self.engine(fault)
                result = self.start(model, self.push, Capture.body(self.payload))
                if last is None:
                    self.assertEqual(result.completion.status, Status.READ_ERROR)
                    self.assertEqual(result.completion.crc, 0)
                    self.assertEqual(model.fatal, Fatal.NONE)
                else:
                    self.assertIsNone(result)
                    self.assertEqual(model.fatal, Fatal.AXI_READ_PROTOCOL)
                    phases = [entry.phase for entry in model.fabric.trace]
                    self.assertNotIn(Phase.COMPLETION, phases)
                self.assertEqual(model.fabric.memory.read(self.address, len(self.payload)), self.payload)
                self.assertEqual(model.reap(0), [])

    def test_prewrite_checksum_or_write_response_error_suppresses_verify_read(self):
        wrong_crc = replace(self.push, flags=FLAG_VERIFY|FLAG_CRC, expected_crc=1)
        b_error = AxiFault(Phase.PUSH, AxiResponse.DECERR, 0x4000)
        cases = ((wrong_crc, None, Status.CRC_ERROR), (self.push, b_error, Status.WRITE_ERROR))
        for desc, fault, expected in cases:
            with self.subTest(status=expected):
                model = self.engine(fault)
                self.assertEqual(self.start(model, desc, Capture.body(self.payload)).completion.status, expected)
                self.assertFalse(any(event.phase == Phase.VERIFY for event in model.fabric.trace))


if __name__ == "__main__":
    unittest.main()
