import io
from pathlib import Path
import struct
import tempfile
import unittest

from capture_fixtures import (
    corrupt_tail, enhanced, interface, ng_block, ng_option, ng_options, pcap, section, simple,
)
from capture_io import CaptureError, NANOSECOND, packets, packets_from, tick_period_ns


class ClassicCaptureTests(unittest.TestCase):
    def test_endian_and_time_precision(self):
        for endian in ("<",">"):
            for rate in (1_000_000,NANOSECOND):
                with self.subTest(endian=endian,rate=rate):
                    data = pcap([(5,21,b"a",1),(6,73,b"xyz",5)],endian,rate)
                    first,second = packets_from(io.BytesIO(data))
                    self.assertEqual(first.capture_time_ns,5*NANOSECOND+21*(NANOSECOND//rate))
                    self.assertEqual(second.capture_time_ns,6*NANOSECOND+73*(NANOSECOND//rate))
                    self.assertEqual((first.serial,second.serial),(0,1))
                    self.assertFalse(first.truncated)
                    self.assertTrue(second.truncated)
                    self.assertEqual(second.captured,b"xyz")

    def test_fcs_extension_leaves_dlt(self):
        data = pcap([(0,0,b"x"*60,60)],network=1+(1<<26)+(2<<28))
        pkt, = packets_from(io.BytesIO(data))
        self.assertEqual((pkt.fcs_bytes,pkt.link_type),(4,1))

    def test_bounded_read_errors(self):
        good_data = pcap([(0,0,bytes(100),100)])
        cases = [
            (b"abcd","signature"),
            (good_data[:15],"file header"),
            (good_data[:35],"record header"),
            (good_data[:-1],"packet bytes"),
            (pcap([(0,1_000_000,b"x",1)]),"timestamp"),
            (pcap([(0,0,b"abc",2)]),"captured length"),
            (pcap([(0,0,b"abc",3)],snap=2),"captured length"),
        ]
        for wire,reason in cases:
            with self.subTest(reason=reason),self.assertRaisesRegex(CaptureError,reason):
                list(packets_from(io.BytesIO(wire)))
        with self.assertRaisesRegex(CaptureError,"limit"):
            list(packets_from(io.BytesIO(good_data),limit=64))

    def test_files_are_closed_after_iteration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"fixture.pcap"
            path.write_bytes(pcap([(7,123,b"data",4)]))
            (record,) = packets(path)
            self.assertEqual(record.captured,b"data")


class NextGenerationTests(unittest.TestCase):
    def test_sections_reset_interfaces_and_byte_order(self):
        little = section("<")+interface("<",name="lane-A")+enhanced("<",b"alpha",ticks=2_000_000)
        big = section(">")+interface(">",name="lane-B",scale=9)+enhanced(">",b"beta",ticks=19)
        records = list(packets_from(io.BytesIO(little+big)))
        self.assertEqual([record.key for record in records],[(0,0),(1,0)])
        self.assertEqual([record.name for record in records],["lane-A","lane-B"])
        self.assertEqual([record.capture_time_ns for record in records],[2*NANOSECOND,19])

    def test_multiple_interfaces_binary_scale_and_signed_offset(self):
        first = interface("<",name="book",scale=0x80+10,offset=-3)
        second = interface("<",name="orders",scale=9,offset=3)
        enhanced_a = enhanced("<",b"1234",iface=0,ticks=874)
        enhanced_b = enhanced("<",b"ab",iface=1,ticks=(1<<33)+16)
        a,b = packets_from(io.BytesIO(section()+first+second+enhanced_a+enhanced_b))
        self.assertEqual(a.capture_time_ns,874*NANOSECOND//1024-3*NANOSECOND)
        self.assertEqual(b.capture_time_ns,(1<<33)+16+3*NANOSECOND)
        self.assertEqual((a.name,b.name),("book","orders"))

    def test_simple_packet_saves_slice_and_has_no_time(self):
        fixture = section()+interface(snap=5)+simple("<",b"abcde",wirelen=7)
        record, = packets_from(io.BytesIO(fixture))
        self.assertEqual(record.capture_time_ns,None)
        self.assertEqual(record.captured,b"abcde")
        self.assertTrue(record.truncated)

    def test_interface_fcs_option_and_record_override(self):
        base = section()+interface(fcs=32,arbitrary=((199,b"unknown"),))
        a = enhanced("<",b"first")
        b = enhanced("<",b"second",flags=8<<5)
        records = list(packets_from(io.BytesIO(base+a+b)))
        self.assertEqual([record.fcs_bytes for record in records],[4,8])

    def test_unknown_blocks_skipped(self):
        fixture = section()+ng_block("<",937,b"opaque!!")+interface()+enhanced("<",b"stay")
        record, = packets_from(io.BytesIO(fixture))
        self.assertEqual(record.captured,b"stay")

    def test_container_faults_are_local_and_bounded(self):
        prefix = section()+interface()
        one_record = enhanced("<",b"abcd")
        cases = (
            (prefix+enhanced("<",b"x",iface=9),"unknown interface"),
            (prefix+enhanced("<",b"long",wirelen=1),"captured length"),
            (section()+interface(snap=1)+enhanced("<",b"too long"),"captured length"),
            (prefix+simple("<",b"x",wirelen=9),"simple packet size"),
            (section()+simple("<",b"empty"),"interface zero"),
            (prefix+corrupt_tail(one_record),"leading and trailing"),
            (section(version=2)+interface(),"section version"),
            (prefix+one_record[:-3],"truncated"),
            (prefix+struct.pack("<II",6,14)+bytes(6),"invalid"),
            (prefix+struct.pack("<II",6,4*1024*1024*1024-4),"invalid"),
        )
        for contents,error in cases:
            with self.subTest(reason=error), self.assertRaisesRegex(CaptureError,error):
                list(packets_from(io.BytesIO(contents)))

    def test_option_errors_include_invalid_lengths_and_repeated_keys(self):
        raw_idb = struct.pack("<HHI",1,0,65535)
        raw_epb = struct.pack("<5I",0,0,0,0,0)
        cases = (
            (section()+interface(scale=6,arbitrary=((9,b"\x06"),)),"repeated"),
            (section()+interface(fcs=11),"whole bytes"),
            (section()+interface(arbitrary=((14,b"short"),)),"signed 64-bit"),
            (section()+interface(arbitrary=((9,b"\x06\x02"),)),"one octet"),
            (section()+ng_block("<",1,raw_idb+struct.pack("<HH",2,9)),"truncated pcapng option"),
            (section()+ng_block("<",1,raw_idb+ng_option("<",0,b"x")),"zero data length"),
            (section()+ng_block("<",1,raw_idb+b"\x00"*4+b"junk"),"nonzero bytes"),
            (section()+interface()+ng_block("<",6,raw_epb+ng_options("<",(2,b"x"))),
             "flags expect four bytes"),
        )
        for data,reason in cases:
            with self.subTest(reason=reason),self.assertRaisesRegex(CaptureError,reason):
                list(packets_from(io.BytesIO(data)))


class ClockArithmeticTests(unittest.TestCase):
    def test_period_is_rational_without_float_rounding(self):
        period = tick_period_ns(156_250_000)
        self.assertEqual((period.numerator,period.denominator),(32,5))
        with self.assertRaises(ValueError):
            tick_period_ns(0)


if __name__ == "__main__":
    unittest.main()
