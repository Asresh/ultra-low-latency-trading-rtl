"""Cross-queue address ownership tests for the optional isolation mode."""

from itertools import product
import unittest

from dma_model import Engine, Phase
from dma_protocol import Descriptor, Opcode, QueueConfig, Region, Ring, Status


DEPTH = 4


def layout(sq, cq, low, high):
    return QueueConfig(Ring(sq, DEPTH), Ring(cq, DEPTH), Region(low, high))


class QueueIsolation(unittest.TestCase):
    def setUp(self):
        self.owner = layout(0x1000, 0x2000, 0x3000, 0x3800)
        self.foreign = layout(0x4000, 0x5000, 0x6000, 0x6800)
        self.model = Engine(queues=3, isolate_queues=True)
        self.assertTrue(self.model.configure(1, self.owner, seed=0xffffffff))
        self.assertTrue(self.model.configure(2, self.foreign, seed=12))

    def test_all_nine_conflicts_preserve_installed_geometry_and_cursor(self):
        for region, replacement in product(self.owner.regions, range(3)):
            values = [0x8000, 0x9000, 0xA000, 0xA800]
            if replacement == 2:
                values[2:] = [region.stop-8, region.stop+8]
            else:
                values[replacement] = region.stop-32
            attempted = layout(*values)
            with self.subTest(region=region, replacement=replacement):
                self.assertFalse(self.model.configure(2, attempted, seed=55))
                self.assertEqual(self.model.queues[2].config, self.foreign)
                self.assertEqual(self.model.queues[2].sq_head, 12)

    def test_adjacency_is_legal_and_own_claim_is_replaced(self):
        adjacent = layout(0x1080, 0x2080, 0x3800, 0x3900)
        self.assertTrue(self.model.configure(2, adjacent, seed=55))
        self.assertTrue(self.model.configure(2, adjacent, seed=56))
        self.assertEqual(self.model.queues[2].sq_head, 56)

    def test_outstanding_sq_or_cq_blocks_release_even_while_worker_idle(self):
        self.assertTrue(self.model.host_post(1, Descriptor(Opcode.FENCE, tag=77)))
        self.assertFalse(self.model.configure(1))
        self.assertFalse(self.model.configure(0, self.owner))
        self.assertEqual(self.model.start().queue, 1)
        self.assertFalse(self.model.configure(1))
        self.model.ack_completion()
        self.assertFalse(self.model.configure(1))
        self.assertFalse(self.model.configure(0, self.owner))
        self.assertEqual([record.tag for record in self.model.reap(1)], [77])
        self.assertTrue(self.model.configure(1))
        self.assertTrue(self.model.configure(0, self.owner))

    def test_peer_aperture_reads_are_rejected_before_memory_transfer(self):
        request = Descriptor(Opcode.READBACK, 16, self.owner.aperture.start, tag=1)
        self.assertTrue(self.model.host_post(2, request))
        result = self.model.start().completion
        self.assertEqual(result.status, Status.ADDRESS)
        self.assertNotIn(Phase.READBACK, {access.phase for access in self.model.fabric.trace})

    def test_third_peer_and_no_queue_order_dependency(self):
        self.assertTrue(self.model.configure(0, layout(0x8000, 0x9000, 0xA000, 0xA800)))
        cross_peer = layout(0x1800, 0x2800, 0x5000, 0x5100)
        self.assertFalse(self.model.configure(0, cross_peer))
        self.assertTrue(self.model.configure(2))
        self.assertTrue(self.model.configure(0, cross_peer))

    def test_default_mode_continues_to_accept_a_shared_payload_aperture(self):
        model = Engine()
        shared = layout(0x8000, 0x9000, self.owner.aperture.start, self.owner.aperture.stop)
        self.assertTrue(model.configure(0, self.owner))
        self.assertTrue(model.configure(1, shared))
        self.assertTrue(model.queues[0].config.overlaps(model.queues[1].config))


if __name__ == "__main__":
    unittest.main()
