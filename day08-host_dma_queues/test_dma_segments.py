"""Independent host contracts for preflight, ordered effects and segmented verification."""

from dataclasses import replace
from itertools import product
import unittest

from dma_model import AxiFault, Capture, Engine, Fabric, Fatal, Phase, SparseMemory
from dma_protocol import (
    AxiResponse, Descriptor, FLAG_CRC, FLAG_VERIFY, Opcode, QueueConfig, Region, Ring,
    Segment, Status, crc32, segment_plan,
)


BAND = Region(0x3000,0x9000)
Q = QueueConfig(Ring(0x1000,8),Ring(0x2000,8),BAND)
TABLE = 0x3ff8
PARTS = (Segment(0x6ff8,16),Segment(0x6000,8),Segment(0x7800,11))
BODY = bytes((67*index+13)&255 for index in range(35))
INPUT = Descriptor(Opcode.GATHER,length=len(BODY),address=TABLE,cookie=91,tag=13,reserved=len(PARTS))


def wire(plan):
    return b"".join(row.encode() for row in plan)


class SegmentProtocol(unittest.TestCase):
    def test_exact_little_endian_list_abi(self):
        row = Segment(0xff00112233445566,0x12345678,0xaa55f0f0)
        self.assertEqual(row.encode().hex(),"66554433221100ff78563412f0f055aa")
        self.assertEqual(Descriptor.decode(INPUT.encode()),INPUT)
        self.assertEqual(INPUT.encode()[-4:],b"\x03\x00\x00\x00")
        self.assertEqual(segment_plan(INPUT,BAND,wire(PARTS)),PARTS)

    def test_input_validation_and_count_bounds(self):
        malformed = [
            (replace(INPUT,reserved=0),Status.BAD_DESC),
            (replace(INPUT,reserved=9),Status.BAD_DESC),
            (replace(INPUT,flags=FLAG_VERIFY),Status.BAD_DESC),
            (replace(INPUT,address=0x8ff8),Status.ADDRESS),
            (replace(INPUT,address=TABLE+1),Status.ADDRESS),
            (replace(INPUT,length=0),Status.ADDRESS),
            (replace(INPUT,length=513),Status.ADDRESS),
            (replace(INPUT,op=Opcode.PULL),Status.BAD_DESC),
        ]
        for desc, status in malformed:
            with self.subTest(desc=desc):
                self.assertEqual(desc.validate(BAND),status)
        self.assertEqual(INPUT.validate(BAND,max_segments=2),Status.BAD_DESC)
        self.assertEqual(INPUT.validate(BAND,max_segments=3),Status.OK)

    def test_each_segment_is_checked_with_rounded_memory_boundaries(self):
        owners = list(PARTS)
        for index,changes in (
            (0,{"length":15}),(1,{"address":TABLE}),(2,{"address":0x8ff8}),
            (2,{"address":PARTS[0].address+8}),(2,{"reserved":1}),
            (2,{"length":13}),(2,{"length":0}),(1,{"address":0x6001}),
            (2,{"address":(1<<64)-8}),
        ):
            with self.subTest(index=index,changes=changes):
                trial = owners.copy()
                trial[index] = replace(trial[index],**changes)
                with self.assertRaises(ValueError):
                    segment_plan(INPUT,BAND,wire(trial))
        with self.assertRaisesRegex(ValueError,"table length"):
            segment_plan(INPUT,BAND,wire(PARTS)[:-1])

    def test_legal_adjacency_and_reverse_table_order(self):
        rows = (Segment(0x4028,32),Segment(0x8000,8),Segment(0x7ff8,7))
        request = replace(INPUT,reserved=3,length=47)
        self.assertEqual(segment_plan(request,BAND,wire(rows)),rows)
        for index, length in product(range(3),range(1,10)):
            altered = list(rows)
            altered[index] = replace(altered[index],length=length)
            request = replace(request,length=sum(row.length for row in altered))
            valid = index == 2 or length == 8
            if valid and index == 2 and length >= 9:
                valid = False
            with self.subTest(index=index,length=length):
                if valid:
                    self.assertEqual(segment_plan(request,BAND,wire(altered)),tuple(altered))
                else:
                    with self.assertRaises(ValueError):
                        segment_plan(request,BAND,wire(altered))


class SegmentedLifecycle(unittest.TestCase):
    def engine(self, *faults, parts=PARTS):
        memory = SparseMemory(0xa5)
        memory.write(TABLE,wire(parts))
        model = Engine(Fabric(memory,faults),isolate_queues=True,max_segments=3)
        self.assertTrue(model.configure(0,Q,seed=0xffffffff))
        return model

    def post(self, model, descriptor=INPUT, capture=None):
        self.assertTrue(model.host_post(0,descriptor,capture))
        return model.start()

    def phases(self,model,phase,channel):
        return [access.address for access in model.fabric.trace if access.phase == phase and access.channel == channel]

    def initialize(self,model):
        offset = 0
        for row in PARTS:
            model.fabric.memory.write(row.address,BODY[offset:offset+row.length])
            offset += row.length

    def test_gather_reassembles_a_single_ordered_frame(self):
        model = self.engine()
        self.initialize(model)
        request = replace(INPUT,flags=FLAG_CRC,expected_crc=crc32(BODY))
        pending = self.post(model,request)
        self.assertEqual((pending.completion.status,pending.completion.crc),(Status.OK,crc32(BODY)))
        self.assertEqual(self.phases(model,Phase.TABLE,"AR"),[0x3ff8,0x4000])
        self.assertEqual(self.phases(model,Phase.PULL,"AR"),[0x6ff8,0x7000,0x6000,0x7800])
        self.assertEqual([emission.bytes for emission in model.emissions],[BODY])
        self.assertEqual((model.pull_bytes,model.reap(0)),(0,[]))
        model.ack_completion()
        self.assertEqual((model.pull_bytes,model.queues[0].cq_tail),(len(BODY),0))

    def test_list_fault_prevents_payload_and_capture_resolution(self):
        invalid = (PARTS[0],PARTS[1],replace(PARTS[2],address=PARTS[1].address))
        model = self.engine(parts=invalid)
        response = self.post(model,replace(INPUT,op=Opcode.SCATTER)).completion
        self.assertEqual((response.status,response.crc),(Status.SEGMENT_ERROR,0))
        self.assertEqual({event.phase for event in model.fabric.trace},
                         {Phase.DESCRIPTOR,Phase.TABLE,Phase.COMPLETION})
        self.assertEqual(model.fabric.memory.read(PARTS[0].address,16),b"\xa5"*16)

    def test_list_fault_response_and_rlast_failure_have_distinct_retirement(self):
        for early_last in (None,True):
            with self.subTest(early_last=early_last):
                fault = AxiFault(Phase.TABLE,AxiResponse.SLVERR,0x4000,last=early_last)
                model = self.engine(fault)
                response = self.post(model)
                if early_last:
                    self.assertIsNone(response)
                    self.assertEqual(model.fatal,Fatal.AXI_READ_PROTOCOL)
                else:
                    self.assertEqual(response.completion.status,Status.READ_ERROR)
                    self.assertEqual(response.completion.cookie,91)
                self.assertFalse(self.phases(model,Phase.PULL,"AR"))

    def test_crc_and_read_faults_never_emit_partial_gather(self):
        failed_crc = self.engine()
        self.initialize(failed_crc)
        response = self.post(failed_crc,replace(INPUT,flags=FLAG_CRC,expected_crc=crc32(BODY)^1))
        self.assertEqual(response.completion.status,Status.CRC_ERROR)
        self.assertFalse(failed_crc.emissions)
        broken = self.engine(AxiFault(Phase.PULL,AxiResponse.SLVERR,PARTS[1].address))
        self.initialize(broken)
        response = self.post(broken)
        self.assertEqual(response.completion.status,Status.READ_ERROR)
        self.assertFalse(broken.emissions)
        self.assertNotIn(PARTS[2].address,self.phases(broken,Phase.PULL,"AR"))

    def test_segment_digest_emits_neither_packet_nor_pull_bytes(self):
        model = self.engine()
        self.initialize(model)
        request = replace(INPUT,op=Opcode.SEGMENT_DIGEST,flags=FLAG_CRC,expected_crc=crc32(BODY))
        result = self.post(model,request).completion
        self.assertEqual((result.status,result.length,result.crc),(Status.OK,len(BODY),crc32(BODY)))
        model.ack_completion()
        self.assertFalse(model.emissions)
        self.assertEqual(model.pull_bytes,0)

    def test_verified_scatter_reads_back_in_list_order_after_all_writes(self):
        model = self.engine()
        request = replace(INPUT,op=Opcode.SCATTER,flags=FLAG_VERIFY)
        result = self.post(model,request,Capture.body(BODY)).completion
        self.assertEqual((result.status,result.crc),(Status.OK,crc32(BODY)))
        accesses = model.fabric.trace
        final_payload_b = max(i for i,v in enumerate(accesses) if v.phase == Phase.PUSH and v.channel=="B")
        first_verify = next(i for i,v in enumerate(accesses) if v.phase == Phase.VERIFY)
        self.assertLess(final_payload_b,first_verify)
        self.assertEqual(self.phases(model,Phase.PUSH,"AW"),self.phases(model,Phase.VERIFY,"AR"))
        self.assertEqual(model.fabric.memory.read(PARTS[2].address,16),BODY[24:]+b"\xa5"*5)
        self.assertEqual(model.push_bytes,0)
        model.ack_completion()
        self.assertEqual(model.push_bytes,len(BODY))

    def test_late_write_error_stops_later_segments_and_readback(self):
        failed = self.engine(AxiFault(Phase.PUSH,AxiResponse.SLVERR,PARTS[1].address,apply_write=True))
        request = replace(INPUT,op=Opcode.SCATTER,flags=FLAG_VERIFY)
        result = self.post(failed,request,Capture.body(BODY)).completion
        self.assertEqual((result.status,result.length,result.crc),(Status.WRITE_ERROR,0,crc32(BODY)))
        self.assertEqual(failed.fabric.memory.read(PARTS[0].address,16),BODY[:16])
        self.assertEqual(failed.fabric.memory.read(PARTS[1].address,8),BODY[16:24])
        self.assertEqual(failed.fabric.memory.read(PARTS[2].address,11),b"\xa5"*11)
        self.assertFalse(self.phases(failed,Phase.VERIFY,"AR"))

    def test_capture_crc_rejection_preempts_each_scatter_write(self):
        model = self.engine()
        request = replace(INPUT,op=Opcode.SCATTER,flags=FLAG_VERIFY|FLAG_CRC,expected_crc=crc32(BODY)^1)
        result = self.post(model,request,Capture.body(BODY)).completion
        self.assertEqual((result.status,result.crc),(Status.CRC_ERROR,crc32(BODY)))
        self.assertFalse(self.phases(model,Phase.PUSH,"AW"))

    def test_corrupt_second_segment_changes_whole_frame_crc(self):
        model = self.engine(AxiFault(Phase.VERIFY,address=PARTS[1].address,read_xor=0xff00))
        request = replace(INPUT,op=Opcode.SCATTER,flags=FLAG_VERIFY)
        result = self.post(model,request,Capture.body(BODY)).completion
        corrupted = bytearray(BODY)
        corrupted[17] ^= 0xff
        self.assertEqual((result.status,result.crc),(Status.VERIFY_ERROR,crc32(corrupted)))


if __name__ == "__main__":
    unittest.main()
