from dataclasses import replace
import random
import unittest

from dma_protocol import (
    Beat, Burst, Completion, Descriptor, FLAG_CRC, FLAG_IRQ, Opcode, QueueConfig, Region,
    Ring, Status, U32_MASK, add_pointer, burst_plan, collect_packet, crc32, distance,
    packet_beats, rounded, write_beat_payload,
)


class WireFormat(unittest.TestCase):
    def test_descriptor_known_words_and_exact_little_endian_layout(self):
        """Cover the descriptor known words and exact little endian layout regression case."""
        entry = Descriptor(
            Opcode.PULL,257,0x0123456789abcdef,0xfedcba9876543210,
            FLAG_CRC|FLAG_IRQ,0x1234,0xcafecafe,0x44556677,
        )
        image = bytes.fromhex(
            "0101000034120301 efcdab8967452301 1032547698badcfe fecafeca77665544"
        )
        self.assertEqual(entry.encode(),image)
        self.assertEqual(Descriptor.decode(image),entry)
        self.assertEqual(entry.words()[0],0x0103123400000101)

    def test_completion_known_record_error_bytes_and_reserved_bits(self):
        """Cover the completion known record error bytes and reserved bits regression case."""
        record = Completion(Status.CRC_ERROR,2,0xbeef,0,0x0011223344556677,0xcbf43926,1,0x5678)
        image = bytes.fromhex(
            "00000000efbe0206 7766554433221100 000000012639f4cb 7856000000000000"
        )
        self.assertEqual(record.encode(),image)
        self.assertEqual(Completion.decode(image),record)
        with self.assertRaisesRegex(ValueError,"reserved"):
            Completion.decode(image[:16]+b"x"+image[17:])
        with self.assertRaisesRegex(ValueError,"nonzero byte count"):
            replace(record,length=2)

    def test_control_rejections_precede_opcode_and_operands(self):
        """Cover the control rejections precede opcode and operands regression case."""
        valid = Descriptor(Opcode.PULL,length=7,address=0x400,tag=33)
        aperture = Region(0x400,0x500)
        cases = [
            (replace(valid,op=255,flags=8),Status.BAD_DESC),
            (replace(valid,op=255,expected_crc=55),Status.BAD_DESC),
            (replace(valid,op=255,reserved=8),Status.BAD_DESC),
            (replace(valid,op=255),Status.BAD_OP),
            (replace(valid,length=0),Status.ADDRESS),
            (replace(valid,length=513),Status.ADDRESS),
            (replace(valid,address=0x401),Status.ADDRESS),
            (replace(valid,address=0x3f8),Status.ADDRESS),
            (replace(valid,address=0x4f8,length=9),Status.ADDRESS),
            (replace(valid,address=0x4f8,length=8),Status.OK),
            (Descriptor(Opcode.FENCE,flags=FLAG_IRQ),Status.OK),
            (Descriptor(Opcode.FENCE,flags=FLAG_CRC),Status.BAD_DESC),
            (Descriptor(Opcode.FENCE,address=0x400),Status.BAD_DESC),
            (Descriptor(Opcode.FENCE,length=1),Status.BAD_DESC),
        ]
        for record,status in cases:
            with self.subTest(record=record):
                self.assertEqual(record.validate(aperture),status)

    def test_error_completion_crc_is_only_available_after_collection(self):
        """Cover the error completion CRC is only available after collection regression case."""
        entry = Descriptor(Opcode.PUSH,17,0x400,19,FLAG_CRC,23,29)
        for code in Status:
            record = Completion.of_descriptor(entry,1,code,88,0xabcdef)
            expected = 0xabcdef if code in (Status.OK,Status.CRC_ERROR,Status.WRITE_ERROR) else 0
            self.assertEqual(record.crc,expected)
            self.assertEqual(record.length,17 if code==Status.OK else 0)
            self.assertEqual(record.cookie,19)

    def test_codec_widths_and_sizes_rejected(self):
        """Cover the codec widths and sizes rejected regression case."""
        with self.assertRaisesRegex(ValueError,"32 bytes"):
            Descriptor.decode(bytes(31))
        with self.assertRaisesRegex(ValueError,"32 bytes"):
            Completion.decode(bytes(33))
        with self.assertRaisesRegex(ValueError,"flags"):
            Descriptor(1,flags=256)
        with self.assertRaisesRegex(ValueError,"tag"):
            Descriptor(1,tag=-1)
        with self.assertRaisesRegex(ValueError,"DMA address"):
            Descriptor(1,address=1<<64)


class AddressMath(unittest.TestCase):
    def test_ring_counter_wrap_and_full_empty_distinction(self):
        """Cover the ring counter wrap and full empty distinction regression case."""
        ring = Ring(0x400,8)
        self.assertEqual(add_pointer(U32_MASK-2,4),1)
        self.assertEqual(distance(1,U32_MASK-2),4)
        self.assertEqual(ring.live(2,U32_MASK-5),8)
        self.assertEqual(ring.live(U32_MASK,U32_MASK),0)
        self.assertEqual(ring.spans(U32_MASK-1,4),(0x4c0,0x4e0,0x400,0x420))
        with self.assertRaisesRegex(ValueError,"exceeds"):
            ring.live(3,U32_MASK-5)

    def test_configuration_overlap_and_overflow_rejected(self):
        """Cover the configuration overlap and overflow rejected regression case."""
        sq,cq = Ring(0x400,8),Ring(0x800,8)
        QueueConfig(sq,cq,Region(0x1000,0x2000))
        for args in ((sq,sq,Region(0x1000,0x2000)),
                     (sq,cq,Region(0x3f8,0x4f8)),
                     (sq,cq,Region(0x800,0x900)),
                     (sq,cq,Region(0x1001,0x2000))):
            with self.subTest(args=args),self.assertRaises(ValueError):
                QueueConfig(*args)
        with self.assertRaisesRegex(ValueError,"wraps"):
            Ring(0xffffffffffffff00,8)
        with self.assertRaisesRegex(ValueError,"power of two"):
            Ring(0x400,6)

    def test_page_and_burst_boundaries_cover_exact_rounded_footprint(self):
        """Cover the page and burst boundaries cover exact rounded footprint regression case."""
        self.assertEqual(list(burst_plan(0xff8,257)),[Burst(0xff8,1),Burst(0x1000,16),Burst(0x1080,16)])
        for alignment in range(0,4096,8):
            for length in (1,7,8,9,111,128,129,511,512):
                pieces = list(burst_plan(0x10000+alignment,length))
                self.assertEqual(sum(x.byte_count for x in pieces),rounded(length))
                self.assertEqual(pieces[0].address,0x10000+alignment)
                for a,b in zip(pieces,pieces[1:]):
                    self.assertEqual(a.address+a.byte_count,b.address)
                for piece in pieces:
                    self.assertLessEqual(piece.beats,16)
                    self.assertEqual(piece.address//4096,(piece.address+piece.byte_count-1)//4096)

    def test_lane_orders_and_padding_byte_strobes(self):
        """Cover the lane orders and padding byte strobes regression case."""
        for length in (1,3,7,8,9,17,63,128,129,512):
            start = 0xff8
            data = bytes(i&255 for i in range(length))
            accepted = bytearray()
            for burst in burst_plan(start,length):
                for address,value,mask,last in write_beat_payload(data,burst,burst.address-start):
                    self.assertEqual(address,start+len(accepted))
                    chunk = value.to_bytes(8,"little")
                    accepted.extend(chunk[:len(bin(mask))-2])
                    if not last:
                        self.assertEqual(mask,255)
            self.assertEqual(bytes(accepted),data)


class PacketContract(unittest.TestCase):
    def test_crc32_ieee_known_vector(self):
        """Cover the CRC-32 ieee known vector regression case."""
        self.assertEqual(crc32(b"123456789"),0xcbf43926)
        self.assertEqual(crc32(b""),0)

    def test_random_packet_roundtrip_every_byte_count(self):
        """Cover the random packet roundtrip every byte count regression case."""
        rng = random.Random(21)
        for length in range(1,513):
            data = bytes(rng.randrange(256) for _ in range(length))
            beats = tuple(packet_beats(data))
            self.assertEqual(collect_packet(beats,length),(Status.OK,data))
            self.assertTrue(beats[0].first)
            self.assertTrue(beats[-1].last)
            self.assertEqual(beats[-1].keep,(1<<((length-1)%8+1))-1)

    def test_bad_keeps_first_last_and_timeout(self):
        """Cover the bad keeps first last and timeout regression case."""
        result = tuple(packet_beats(bytes(9)))
        for mutation in (
            (replace(result[0],first=False),result[1]),
            (replace(result[0],last=True),result[1]),
            (replace(result[0],keep=0),result[1]),
            (result[0],replace(result[1],keep=0x05)),
            (result[0],replace(result[1],keep=3)),
        ):
            self.assertEqual(collect_packet(mutation,9),(Status.STREAM_FORMAT,b""))
        self.assertEqual(collect_packet(result[:1],9,timed_out=True),(Status.CAPTURE_TIMEOUT,b""))
        with self.assertRaises(ValueError):
            Beat(0,0xf0,True,True).payload


if __name__ == "__main__":
    unittest.main()
