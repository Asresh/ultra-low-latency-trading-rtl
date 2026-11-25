"""Bundle packing, path and corruption checks for the host segment planner."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from dma_protocol import FLAG_CRC, Opcode, crc32
from dma_segment_tool import Plan, export, inspect


SPEC = {
    "operation":"scatter", "table":"0x3ff8", "aperture":["0x3000","0x9000"], "cookie":"0x500",
    "verify":True, "irq":True, "segments":[{"address":"0x6ff8","length":16},{"address":"0x5000","length":3}],
}


class PlanTool(unittest.TestCase):
    def test_record_sizes_crc_and_report_offsets(self):
        data = bytes(range(19))
        plan = Plan.from_spec(SPEC,data)
        self.assertEqual(plan.descriptor.op,Opcode.SCATTER)
        self.assertEqual(plan.descriptor.expected_crc,crc32(data))
        self.assertTrue(plan.descriptor.flags & FLAG_CRC)
        self.assertEqual((len(plan.table),len(plan.descriptor.encode())),(32,32))
        report = plan.describe()
        self.assertEqual(report["segments"][0]["bursts"],[[0x6ff8,1],[0x7000,1]])
        self.assertEqual(report["segments"][1]["payload_offset"],16)
        self.assertEqual(report["segments"][1]["memory_bytes"],8)
        self.assertTrue(report["verify_write"])

    def test_explicit_zero_checksum_is_still_enabled(self):
        planning = deepcopy(SPEC)
        planning["expected_crc"] = 0
        plan = Plan.from_spec(planning)
        self.assertTrue(plan.descriptor.check_crc)
        self.assertEqual(plan.describe()["expected_crc"],0)

    def test_noncanonical_values_and_conflicting_fields_are_rejected(self):
        cases = (
            ({"max_segments":1},"BAD_DESC"),
            ({"operation":"digest"},"BAD_DESC"),
            ({"table":"0x8ff8"},"ADDRESS"),
            ({"verify":1},"true or false"),
            ({"cookie":True},"integer"),
            ({"random":9},"unexpected fields"),
        )
        for changes,error in cases:
            with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,error):
                Plan.from_spec({**SPEC,**changes})
        with self.assertRaisesRegex(ValueError,"exact segment total"):
            Plan.from_spec(SPEC,b"payload")
        with self.assertRaisesRegex(ValueError,"choose expected_crc"):
            Plan.from_spec({**SPEC,"expected_crc":0},bytes(range(19)))

    def test_export_round_trip_and_corrupted_valid_record_detection(self):
        plan = Plan.from_spec(SPEC)
        with TemporaryDirectory() as temp:
            bundle = Path(temp)/"bundle"
            export(plan,bundle)
            self.assertEqual(inspect(bundle),plan)
            with self.assertRaisesRegex(ValueError,"already exists"):
                export(plan,bundle)
            file = bundle/"segments.bin"
            modified = bytearray(file.read_bytes())
            modified[1] ^= 1
            file.write_bytes(modified)
            with self.assertRaisesRegex(ValueError,"differs from the recorded plan"):
                inspect(bundle)


if __name__ == "__main__":
    unittest.main()
