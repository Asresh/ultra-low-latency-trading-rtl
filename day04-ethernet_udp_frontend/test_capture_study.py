from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

from capture_fixtures import enhanced, interface, pcap, section
from capture_io import CapturePacket, DLT_ETHERNET, packets_from
from capture_study import CycleScheduler, Study, interface_id, main, row_outputs, wire_without_fcs
from net_scenarios import DST, standard
from net_wire import Match, Result, Rule


def one_capture(data, *, serial=0, time=0, original=None, fcs=0, link=DLT_ETHERNET, lane=0):
    return CapturePacket(serial,0,lane,f"lane-{lane}",link,time,
                         len(data) if original is None else original,data,fcs)


def with_fcs(data, corrupt=False):
    return data+(zlib.crc32(data)^int(corrupt)).to_bytes(4,"little")


class ReplayTimingTests(unittest.TestCase):
    def test_schedule_clamps_overlapping_packets_without_reordering(self):
        clock = CycleScheduler(156250000,interframe_idle=2)
        self.assertEqual(clock.start(100,8),0)
        self.assertEqual(clock.start(105,3),10)
        self.assertEqual(clock.start(90,3),15)
        self.assertEqual(clock.reordered_time,1)
        self.assertEqual(clock.start(None,1),20)
        self.assertEqual(clock.start(901,1),126)

    def test_rational_edge_ceil(self):
        clock = CycleScheduler(156250000)
        self.assertEqual(clock.start(100,1),0)
        self.assertEqual(clock.start(107,1),2)
        self.assertEqual(clock.start(125,1),4)
        with self.assertRaises(ValueError):
            clock.start(200,0)

    def test_schedule_invalid_time_inputs(self):
        with self.assertRaises(ValueError):
            CycleScheduler(0)
        with self.assertRaises(ValueError):
            CycleScheduler(125000000,interframe_idle=-1)


class FcsSemanticsTests(unittest.TestCase):
    def test_capture_metadata_may_strip_checksum_bytes_without_validating(self):
        wire = standard(b"fcs").serialize()
        record = one_capture(with_fcs(wire,corrupt=True),fcs=4)
        self.assertEqual(wire_without_fcs(record,"metadata"),(wire,False))
        self.assertEqual(wire_without_fcs(record,"verify"),(wire,True))
        self.assertEqual(wire_without_fcs(record,"none"),(record.captured,False))
        self.assertEqual(wire_without_fcs(record,"force"),(wire,False))

    def test_fcs_status_only_on_terminal_beat(self):
        wire = standard(b"payload").serialize()
        study = Study([Rule()],fcs_mode="verify")
        rows = study.add(one_capture(with_fcs(wire,corrupt=True)))
        self.assertEqual(rows[-1].result.result,Result.MAC_BAD)
        self.assertEqual([r.beat.bad for r in rows],[False]*7+[True])
        self.assertEqual(study.report()["total"]["disposition"],{"MAC_BAD":1})

    def test_bad_fcs_length_and_empty_frame_are_not_emitted(self):
        study = Study([Rule()])
        self.assertFalse(study.add(one_capture(b"abc",fcs=4)))
        self.assertFalse(study.add(one_capture(b"a"*72,fcs=8)))
        self.assertFalse(study.add(one_capture(b"")))
        self.assertEqual(study.report()["total"]["disposition"],{
            "unsupported_capture_fcs":2,"empty_capture_record":1,
        })


class StudyReportTests(unittest.TestCase):
    def test_report_groups_rules_queues_vlans_and_time(self):
        rules = [Rule(dst_ip=Match(DST,0xffffffff),dst_port=Match(18000,65535),queue=1),
                 Rule(dst_port=Match(1,65535),drop=True)]
        study = Study(rules)
        a = standard(b"data",tags=((0x8100,0x1007),)).serialize()
        b = standard(b"rejected",dst_port=1).serialize()
        c = standard(b"miss",dst_port=400).serialize()
        for raw,index,time,lane in ((a,0,100,0),(b,1,105,1),(c,2,500,1)):
            study.add(one_capture(raw,serial=index,time=time,lane=lane))
        summary = study.report()
        self.assertEqual(summary["total"]["disposition"],{"OK":1,"RULE_DROP":1,"RULE_MISS":1})
        self.assertEqual(summary["total"]["queues"],{"1":1})
        self.assertEqual(summary["total"]["vlans"],{"7":1,"untagged":2})
        self.assertEqual(summary["total"]["payload_bytes"],4)
        self.assertEqual(summary["total"]["span_ns"],400)
        self.assertEqual(summary["interfaces"]["0:0"]["packets"],1)
        self.assertEqual(summary["interfaces"]["0:1"]["packets"],2)

    def test_skipped_interfaces_truncation_and_noneth(self):
        logging = Study([Rule()],interface=(0,1))
        records = (
            one_capture(b"x"*90,lane=0),
            one_capture(b"x"*90,lane=1,original=100),
            one_capture(b"x"*90,lane=1,link=113),
            one_capture(b"",lane=1),
        )
        for record in records:
            self.assertFalse(logging.add(record))
        self.assertEqual(logging.report()["total"]["packets"],3)
        self.assertEqual(logging.report()["total"]["emitted_packets"],0)
        self.assertEqual(logging.report()["total"]["disposition"],
                         {"capture_truncated":1,"non_ethernet_capture":1,"empty_capture_record":1})

    def test_timed_rows_align_reference_frames(self):
        a = standard(b"fixed").serialize()
        b = standard(b"longer",udp_zero=True).serialize()
        input_pcap = pcap([(1,20,a,len(a)),(1,30,b,len(b))],rate=1_000_000_000)
        out = io.StringIO()
        expectations = io.StringIO()
        study = Study([Rule(queue=2)],allow_zero=False)
        row_outputs(study,packets_from(io.BytesIO(input_pcap)),out,expectations)
        stream = [line.split() for line in out.getvalue().splitlines()]
        expected = [line.split() for line in expectations.getvalue().splitlines()]
        self.assertEqual(len(stream),16)
        self.assertEqual(stream[0][:2],["0","0"])
        self.assertEqual(stream[8][:2],["8","1"])
        self.assertEqual(expected[0][-1],b"fixed".hex())
        self.assertEqual(int(expected[1][1],16)>>376,Result.UDP_ZERO)
        self.assertEqual(expected[1][-1],"-")

    def test_cli_writes_big_endian_multiinterface_fixture(self):
        raw = standard(b"through").serialize()
        encoded = section(">")+interface(">",name="data",scale=9)
        encoded += enhanced(">",raw,ticks=1530)
        with tempfile.TemporaryDirectory() as folder:
            capture = Path(folder)/"network.pcapng"
            capture.write_bytes(encoded)
            vectors = Path(folder)/"wire.txt"
            oracle = Path(folder)/"oracle.txt"
            output = io.StringIO()
            arguments = ["capture_study",str(capture),"--interface","0:0","--fcs","none",
                         "--output",str(vectors),"--expected",str(oracle)]
            with patch("sys.argv",arguments),redirect_stdout(output):
                main()
            report = json.loads(output.getvalue())
            self.assertEqual(report["interfaces"]["0:0"]["name"],"data")
            self.assertEqual(report["total"]["disposition"],{"OK":1})
            self.assertEqual(len(vectors.read_text().splitlines()),8)
            self.assertTrue(oracle.read_text().endswith(b"through".hex()+"\n"))

    def test_cli_rejects_input_and_output_path_collisions_before_opening(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            capture = root / "wire.pcap"
            original = pcap([])
            capture.write_bytes(original)
            symlink = root / "capture-link"
            symlink.symlink_to(capture)
            hardlink = root / "capture-copy"
            os.link(capture, hardlink)
            output = root / "vectors.txt"
            for paths in (
                ("--output", capture),
                ("--output", symlink),
                ("--expected", hardlink),
                ("--output", output, "--expected", output),
            ):
                with self.subTest(paths=paths):
                    arguments = ["capture_study", str(capture), *map(str, paths)]
                    stderr = io.StringIO()
                    with patch("sys.argv", arguments), redirect_stderr(stderr), self.assertRaises(SystemExit):
                        main()
                    self.assertIn("aliases", stderr.getvalue())
                    self.assertEqual(capture.read_bytes(), original)
                    self.assertFalse(output.exists())

    def test_interface_selector_validation(self):
        self.assertEqual(interface_id("2:19"),(2,19))
        for value in ("-1:0","2:blarg","2",":"):
            with self.assertRaisesRegex(Exception,"section:index"):
                interface_id(value)


if __name__ == "__main__":
    unittest.main()
