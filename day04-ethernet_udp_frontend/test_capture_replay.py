import io
from pathlib import Path
import tempfile
import unittest

from capture_fixtures import enhanced, interface, section
from capture_io import packets_from
from capture_replay import bundle, stable_rules, synthetic_input
from net_scenarios import standard
from net_wire import MacBeat, Result, Rule


def read_text(path):
    return [line.split() for line in path.read_text().splitlines()]


class ReplayBundleTests(unittest.TestCase):
    def test_synthetic_fixture_reproducible_and_exercises_dispositions(self):
        encoded = synthetic_input(4704)
        self.assertEqual(encoded,synthetic_input(4704))
        self.assertNotEqual(encoded,synthetic_input(4705))
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            report = bundle(packets_from(io.BytesIO(encoded)),root,stable_rules(),fcs="verify")
            self.assertEqual(report["total"]["packets"],152)
            self.assertEqual(report["total"]["emitted_packets"],144)
            self.assertEqual(report["total"]["disposition"]["capture_truncated"],8)
            self.assertEqual(len(report["interfaces"]),3)
            for reason in ("OK","RULE_DROP","RULE_MISS","MAC_BAD","UDP_CHECKSUM"):
                self.assertGreater(report["total"]["disposition"][reason],0)
            self.assertGreater(report["capture_timestamp_reorders"],0)

    def test_file_serials_and_pkt_body_agree(self):
        pkt1 = standard(b"approved")
        pkt2 = standard(b"denied",dst_port=1000)
        fixture = section()+interface(scale=9)
        fixture += enhanced("<",pkt1.serialize(),ticks=1)
        fixture += enhanced("<",pkt2.serialize(),ticks=4)
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            bundle(packets_from(io.BytesIO(fixture)),root,stable_rules(),idle=3)
            frames = read_text(root/"rt_mac.txt")
            info = read_text(root/"rt_info.txt")
            bodies = read_text(root/"rt_payload.txt")
            self.assertEqual(len(frames),16)
            self.assertEqual(frames[0][:2],["0","0"])
            self.assertTrue(MacBeat.parse(" ".join(frames[0][2:])).first)
            self.assertTrue(MacBeat.parse(" ".join(frames[7][2:])).last)
            self.assertEqual(frames[8][:2],["11","1"])
            self.assertEqual([int(row[1],16)>>376 for row in info],
                             [Result.OK,Result.RULE_DROP])
            self.assertEqual(len(bodies),1)
            self.assertEqual(bodies[0][0],"0")
            self.assertEqual(MacBeat.parse(" ".join(bodies[0][1:])).valid_bytes(),b"approved")
            self.assertEqual(read_text(root/"rt_counts.txt"),[["16","2","1"]])

    def test_capture_record_filter_keeps_original_serial_number(self):
        first = standard(b"drop from capture").serialize()
        second = standard(b"keep unicast").serialize()
        blob = section()+interface()+interface()+enhanced("<",first)+enhanced("<",second,iface=1)
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            bundle(packets_from(io.BytesIO(blob)),p,[Rule(queue=3)],selected_interface=(0,1))
            self.assertEqual(read_text(p/"rt_mac.txt")[0][:2],["0","1"])
            info, = read_text(p/"rt_info.txt")
            self.assertEqual(info[0],"1")

    def test_rules_machine_prefix_padding_and_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            cfg = stable_rules()
            bundle((),p,cfg)
            rules = [int(row[0],16) for row in read_text(p/"rt_rules.txt")]
            self.assertEqual(rules,[item.encode() for item in cfg]+[0]*(8-len(cfg)))
            with self.assertRaisesRegex(ValueError,"eight"):
                bundle((),p,[Rule()]*9)

    def test_zero_checksum_strict_model_matches_opcode_contract(self):
        raw = standard(b"disabled",udp_zero=True).serialize()
        data = section()+interface()+enhanced("<",raw)
        with tempfile.TemporaryDirectory() as output:
            p = Path(output)
            report = bundle(packets_from(io.BytesIO(data)),p,[Rule()],strict=True)
            self.assertEqual(report["total"]["disposition"],{"UDP_ZERO":1})
            self.assertEqual(read_text(p/"rt_payload.txt"),[])
            result, = read_text(p/"rt_info.txt")
            self.assertEqual(int(result[1],16)>>376,Result.UDP_ZERO)


if __name__ == "__main__":
    unittest.main()
