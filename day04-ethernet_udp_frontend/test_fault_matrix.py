from collections import Counter
import random
import unittest

from fault_matrix import EXPECTED, Fault, mutate, observe, sample
from net_scenarios import standard
from net_wire import Result, parse_frame


class FaultMatrixTests(unittest.TestCase):
    def test_each_mutation_across_vlan_options_and_odd_payload(self):
        for tag_depth in range(3):
            tags=tuple((0x8100,i+1) for i in range(tag_depth))
            for options in (b"",b"\x01"*4,b"\x01"*40):
                for size in (0,1,8,17,120):
                    original=standard(bytes(size),tags=tags,options=options)
                    for fault in Fault:
                        with self.subTest(depth=tag_depth,ihl=len(options),size=size,fault=fault):
                            output=mutate(original,fault,random.Random(size))
                            self.assertEqual(observe(output,256),EXPECTED[fault])
                            self.assertEqual(parse_frame(output.original.serialize()).result,Result.OK)

    def test_sample_constructs_repeatable_coverage_and_trims_to_bound(self):
        packet=standard(bytes(300),tags=((0x88a8,33),(0x8100,45)),options=b"\x01"*40)
        def cases(seed):
            return list(sample(random.Random(seed),lambda:packet,80,160))
        a=cases(17)
        b=cases(29)
        self.assertEqual(a,cases(17))
        self.assertNotEqual(a,b)
        self.assertEqual({case.kind for case in a},set(Fault))
        self.assertLessEqual(max(len(case.wire) for case in a),160)
        histogram=Counter(observe(case,160) for case in a)
        for expected in EXPECTED.values():
            self.assertGreater(histogram[expected],0)

    def test_lower_layer_first_marker_fault_preserves_original_wire(self):
        packet=standard(b"a unique payload")
        result=mutate(packet,Fault.RX_FIRST,random.Random(33))
        self.assertEqual(result.wire,packet.serialize(include_pad=False))
        self.assertEqual(parse_frame(result.wire).result,Result.OK)
        self.assertEqual(observe(result,256),Result.BAD_AXIS)

    def test_empty_sample_cannot_claim_coverage(self):
        with self.assertRaisesRegex(ValueError,"at least"):
            list(sample(random.Random(0),lambda:standard(),1,256))


if __name__ == "__main__":
    unittest.main()
