from dataclasses import replace
import unittest

from dma_model import (
    AxiFault, Capture, Engine, Fabric, Fatal, Irq, Phase, SparseMemory,
)
from dma_protocol import (
    AxiResponse, Completion, Descriptor, FLAG_CRC, FLAG_IRQ, Opcode, QueueConfig, Region, Ring,
    Status, U32_MASK, add_pointer, crc32, packet_beats,
)


def geometry(index=0, depth=8):
    """Build the selected queue's nonoverlapping ring and payload regions."""
    base = index*0x10000
    return QueueConfig(Ring(base+0x1000,depth),Ring(base+0x2000,depth),
                       Region(base+0x3000,base+0x8000))


def make_engine(faults=(), fill=0xa5, depth=8, timeout=6, seed=0, batch=0, delay=0):
    """Build a test engine with configurable memory, DMA, and interrupt parameters."""
    model = Engine(Fabric(SparseMemory(fill),faults),capture_timeout=timeout)
    for i in (0,1):
        if not model.configure(i,geometry(i,depth),seed=seed,irq_batch=batch,irq_delay=delay):
            raise AssertionError("failed to configure test queue")
    return model


class PublicationTests(unittest.TestCase):
    def test_cq_publication_waits_for_last_b_and_wraps_both_rings(self):
        """Cover the CQ publication waits for last b and wraps both rings regression case."""
        model = make_engine(seed=U32_MASK)
        descriptor = Descriptor(Opcode.FENCE,tag=3,flags=FLAG_IRQ,cookie=0x55)
        self.assertTrue(model.host_post(0,descriptor))
        state = model.queues[0]
        before = model.start()
        self.assertEqual(state.sq_head,U32_MASK)
        self.assertEqual(state.cq_tail,U32_MASK)
        self.assertEqual(model.reap(0),[])
        self.assertFalse(state.irq.request)
        cq_memory = model.fabric.memory.read(state.config.cq.address(U32_MASK),32)
        self.assertEqual(Completion.decode(cq_memory),before.completion)
        self.assertIsNone(model.start())
        self.assertEqual(model.completed,0)
        self.assertFalse(model.configure(1,geometry(1)))
        self.assertEqual(model.ack_completion(),before.completion)
        self.assertEqual((state.sq_head,state.cq_tail),(0,0))
        self.assertEqual((state.submission_count,state.completion_count),(0,1))
        self.assertTrue(state.irq.request)
        self.assertEqual(model.reap(0),[before.completion])

    def test_reap_and_submit_doorbell_is_atomic_when_one_field_illegal(self):
        """Cover the reap and submit doorbell is atomic when one field illegal regression case."""
        model = make_engine(seed=U32_MASK-3,depth=4)
        queue = model.queues[0]
        for _ in range(3):
            self.assertTrue(model.host_post(0,Descriptor(Opcode.FENCE)))
        self.assertTrue(model.start())
        self.assertTrue(model.ack_completion())
        before = queue.sq_tail,queue.cq_head
        self.assertFalse(queue.doorbell(add_pointer(before[0],3),add_pointer(before[1])))
        self.assertEqual((queue.sq_tail,queue.cq_head),before)
        self.assertFalse(queue.doorbell(add_pointer(before[0]),add_pointer(before[1],2)))
        self.assertEqual((queue.sq_tail,queue.cq_head),before)
        self.assertTrue(queue.doorbell(add_pointer(before[0]),add_pointer(before[1])))
        self.assertEqual(queue.submission_count,3)
        self.assertEqual(queue.completion_count,0)

    def test_cq_full_blocks_one_lane_and_preserves_other_lane_progress(self):
        """Cover the CQ full blocks one lane and preserves other lane progress regression case."""
        model = make_engine(depth=2)
        for index in (0,1,2):
            self.assertTrue(model.host_post(0,Descriptor(Opcode.FENCE,tag=index)))
            if index < 2:
                self.assertTrue(model.start())
                model.ack_completion()
        self.assertEqual(model.eligible(),())
        self.assertIsNone(model.start())
        self.assertFalse(model.configure(0,geometry(depth=2)))
        self.assertTrue(model.host_post(1,Descriptor(Opcode.FENCE,tag=99)))
        self.assertEqual(model.eligible(),(1,))
        self.assertEqual(model.start().queue,1)
        model.ack_completion()
        self.assertEqual([item.tag for item in model.reap(0,count=1)],[0])
        self.assertEqual(model.start().completion.tag,2)
        model.ack_completion()
        self.assertEqual([item.tag for item in model.reap(0)],[1,2])
        self.assertEqual([item.tag for item in model.reap(1)],[99])

    def test_round_robin_alternates_eligible_queues(self):
        """Cover the round robin alternates eligible queues regression case."""
        model = make_engine()
        for index in range(5):
            for lane in (0,1):
                model.host_post(lane,Descriptor(Opcode.FENCE,tag=index))
        observed = []
        while model.start():
            done = model.ack_completion()
            observed.append((done.queue,done.tag))
        self.assertEqual(observed,[(lane,index) for index in range(5) for lane in (0,1)])


class AxiFaultSemantics(unittest.TestCase):
    def test_failing_descriptor_read_completes_with_zero_identifier(self):
        """Cover the failing descriptor read completes with zero identifier regression case."""
        error = AxiFault(Phase.DESCRIPTOR,AxiResponse.SLVERR,address=0x1010,queue=0)
        model = make_engine([error])
        model.host_post(0,Descriptor(Opcode.PULL,16,0x3000,0xbbbb,tag=0xaaaa))
        pending = model.start()
        self.assertEqual((pending.completion.op,pending.completion.cookie),(0,0))
        self.assertEqual(pending.completion.status,Status.READ_ERROR)
        self.assertEqual([r.channel for r in model.fabric.trace if r.phase==Phase.DESCRIPTOR],
                         ["AR","R","R","R","R"])
        self.assertEqual(model.fatal,Fatal.NONE)
        model.ack_completion()
        self.assertEqual(model.rejected,1)

    def test_malformed_rlast_is_fatal_in_descriptor_and_payload_phases(self):
        """Cover the malformed RLAST is fatal in descriptor and payload phases regression case."""
        for phase,address in ((Phase.DESCRIPTOR,0x1000),(Phase.PULL,0x4000)):
            with self.subTest(phase=phase):
                model = make_engine([AxiFault(phase,last=True,address=address)])
                model.host_post(0,Descriptor(Opcode.PULL,17,0x4000,cookie=321))
                self.assertIsNone(model.start())
                self.assertEqual(model.fatal,Fatal.AXI_READ_PROTOCOL)
                self.assertEqual(model.queues[0].submission_count,1)
                self.assertEqual(model.reap(0),[])
                self.assertIsNone(model.start())
                self.assertFalse(any(item.phase == Phase.COMPLETION for item in model.fabric.trace))

    def test_mid_data_rlast_absence_preempts_read_response_error(self):
        """Cover the mid data RLAST absence preempts read response error regression case."""
        model = make_engine([AxiFault(Phase.PULL,AxiResponse.DECERR,address=0x4008,last=False)])
        model.host_post(0,Descriptor(Opcode.PULL,9,0x4000,cookie=1))
        self.assertIsNone(model.start())
        self.assertEqual(model.fatal,Fatal.AXI_READ_PROTOCOL)
        self.assertEqual(model.completed,0)

    def test_failed_second_push_b_keeps_prior_writes_and_reports_crc(self):
        """Cover the failed second push b keeps prior writes and reports CRC regression case."""
        payload = bytes(index&255 for index in range(257))
        fault = AxiFault(Phase.PUSH,AxiResponse.SLVERR,address=0x4000)
        model = make_engine([fault])
        addr = 0x3ff8
        model.host_post(0,Descriptor(Opcode.PUSH,len(payload),addr),Capture.body(payload))
        completion = model.start().completion
        self.assertEqual(completion.status,Status.WRITE_ERROR)
        self.assertEqual(completion.length,0)
        self.assertEqual(completion.crc,crc32(payload))
        self.assertEqual(model.fabric.memory.read(addr,len(payload)),
                         payload[:8]+bytes((0xa5,))*(len(payload)-8))
        issued = [x.address for x in model.fabric.trace if x.phase==Phase.PUSH and x.channel=="AW"]
        self.assertEqual(issued,[0x3ff8,0x4000])
        model.ack_completion()
        self.assertEqual(model.push_bytes,0)

    def test_cq_b_error_latches_fault_and_never_publishes_visibility(self):
        """Cover the CQ b error latches fault and never publishes visibility regression case."""
        fault = AxiFault(Phase.COMPLETION,AxiResponse.DECERR,queue=1,apply_write=True)
        model = make_engine([fault],batch=1)
        model.host_post(1,Descriptor(Opcode.FENCE,flags=FLAG_IRQ,tag=33))
        pending = model.start()
        self.assertIsNotNone(pending)
        self.assertEqual(model.reap(1),[])
        self.assertIsNone(model.ack_completion())
        self.assertEqual(model.fatal,Fatal.COMPLETION_WRITE)
        state = model.queues[1]
        self.assertEqual((state.sq_head,state.cq_tail),(0,0))
        self.assertFalse(state.irq.request)
        self.assertIsNone(model.start())
        self.assertFalse(model.configure(1,geometry(1)))


class PayloadSemantics(unittest.TestCase):
    def test_tail_read_is_trimmed_before_crc_and_packet(self):
        """Cover the tail read is trimmed before CRC and packet regression case."""
        payload = b"123456789"
        dma = make_engine()
        dma.fabric.memory.write(0x3ff8,payload+bytes([222])*7)
        dma.host_post(0,Descriptor(Opcode.PULL,9,0x3ff8,0x44,FLAG_CRC,7,crc32(payload)))
        pending = dma.start()
        pkt, = dma.emissions
        self.assertEqual((pkt.queue,pkt.tag,pkt.cookie,pkt.bytes),(0,7,0x44,payload))
        self.assertEqual(pkt.beats()[-1].data,ord("9"))
        self.assertEqual(pending.completion.crc,crc32(payload))
        self.assertEqual(list(r.data for r in dma.fabric.trace if r.channel=="AR" and r.phase==Phase.PULL),
                         [0,0])

    def test_crc_errors_prevent_reads_from_crossing_into_endpoint_output_and_push_commits(self):
        """Cover the CRC errors prevent reads from crossing into endpoint output and push commits regression case."""
        data = b"0123456789abcdefghijklm"
        model = make_engine()
        model.fabric.memory.write(0x3000,data)
        for opcode,address,capture in (
            (Opcode.PULL,0x3000,None),(Opcode.PUSH,0x3100,Capture.body(data))
        ):
            desc = Descriptor(opcode,len(data),address,3,FLAG_CRC,7,crc32(data)^1)
            model.host_post(0,desc,capture)
            pending = model.start()
            self.assertEqual(pending.completion.status,Status.CRC_ERROR)
            self.assertEqual(pending.completion.crc,crc32(data))
            model.ack_completion()
        self.assertFalse(model.emissions)
        self.assertFalse(any(row.phase==Phase.PUSH for row in model.fabric.trace))
        self.assertEqual(model.fabric.memory.read(0x3100,len(data)),bytes((0xa5,))*len(data))

    def test_capture_drains_bad_packet_or_times_out_without_writes(self):
        """Cover the capture drains bad packet or times out without writes regression case."""
        good = tuple(packet_beats(b"0123456789abcdefg"))
        malformed = replace(good[0],keep=3)
        shapes = (
            (Capture((malformed,good[1],good[2]),(0,0,2)),Status.STREAM_FORMAT),
            (Capture((malformed,good[1],good[2]),(0,3,3)),Status.CAPTURE_TIMEOUT),
            (Capture(good,(0,5,0)),Status.OK),
            (Capture(good,(0,6,0)),Status.CAPTURE_TIMEOUT),
            (Capture(()),Status.CAPTURE_TIMEOUT),
        )
        for capture,expected in shapes:
            with self.subTest(expected=expected,capture=capture):
                model = make_engine(timeout=6)
                model.host_post(0,Descriptor(Opcode.PUSH,17,0x4000),capture)
                result = model.start().completion
                self.assertEqual(result.status,expected)
                writes = [item for item in model.fabric.trace if item.phase==Phase.PUSH]
                self.assertEqual(bool(writes),expected==Status.OK)


class InterruptPolicy(unittest.TestCase):
    def test_batch_timer_and_immediate_modes(self):
        """Cover the batch timer and immediate modes regression case."""
        policy = Irq(minimum_batch=3,maximum_delay=6)
        policy.edge(completion=True)
        self.assertEqual((policy.backlog,policy.elapsed,policy.request),(1,1,False))
        policy.idle(4)
        self.assertFalse(policy.request)
        policy.idle(1)
        self.assertTrue(policy.request)
        policy.edge(acknowledge=True)
        self.assertEqual((policy.backlog,policy.elapsed,policy.request),(0,0,False))
        for i in range(3):
            policy.edge(completion=True)
            self.assertEqual(policy.request,i==2)
        policy.edge(acknowledge=True,completion=True,immediate=True)
        self.assertEqual((policy.backlog,policy.elapsed,policy.request),(1,1,True))

    def test_saturation_and_ack_independent_of_cq_head(self):
        """Cover the saturation and ack independent of CQ head regression case."""
        model = make_engine()
        state = model.queues[0]
        for _ in range(350):
            state.irq.edge(completion=True,immediate=True)
        state.irq.idle(2*U32_MASK)
        self.assertEqual(state.irq.backlog,255)
        self.assertEqual(state.irq.elapsed,U32_MASK)
        self.assertEqual(state.irq.request,True)
        state.irq.edge(acknowledge=True)
        self.assertEqual((state.cq_head,state.cq_tail),(0,0))
        self.assertEqual((state.irq.backlog,state.irq.elapsed,state.irq.request),(0,0,False))


if __name__ == "__main__":
    unittest.main()
