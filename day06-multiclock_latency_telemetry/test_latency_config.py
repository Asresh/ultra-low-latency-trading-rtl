"""Host-side bus programming plans exercise fixed-point and configuration edge cases."""

import unittest

from latency_config import (
    Bus, COMMAND, DEVICE_SIGNATURE, ID, RAW_HIGH, STATUS, TRACE_CONTROL,
    ConfigError, synthesize_plan,
)


def writes(plan):
    return [operation for operation in plan if operation.operation == "write"]


class Configuration(unittest.TestCase):
    def test_plan_has_probe_then_quiesce(self):
        p = synthesize_plan({})
        self.assertEqual(p[0], Bus.poll(ID, 0xFFFFFFFF, DEVICE_SIGNATURE))
        self.assertEqual(p[2], Bus.write(COMMAND, 0, 1))
        self.assertEqual(p[3], Bus.poll(STATUS, 1, 0))

    def test_exact_32_32_program_order_and_snapshot_fence(self):
        p = synthesize_plan({
            "period_ns": "6.5", "timeout_ticks": 0x123456789,
            "armed": True, "snapshot": True, "latch_clock": True,
            "trace": {
                "enable": True, "post": 2, "threshold_ticks": 80,
                "statuses": ["DUPLICATE", "SET_FULL"], "arm": True,
            },
        })
        result = writes(p)
        self.assertEqual(
            [(x.offset, x.value) for x in result[:5]],
            [(8, 0), (16, 6), (20, 0x80000000), (24, 0x23456789), (28, 1)],
        )
        self.assertEqual(
            [(x.offset, x.value) for x in result[5:]],
            [(32, 80), (36, 0), (TRACE_CONTROL, 0x1020C), (COMMAND, 0xF3)],
        )
        reads = [x.offset for x in p if x.operation == "read"]
        self.assertIn(RAW_HIGH, reads)
        self.assertIn(0x050, reads)
        self.assertIn(0x054, reads)
        command_index = p.index(Bus.write(COMMAND, 0xF3, 1))
        self.assertLess(p.index(Bus.poll(STATUS, 0x80, 0)), command_index)
        self.assertIn(Bus.poll(STATUS, 0x41, 1), p[command_index+1:])

    def test_byte_strobes_and_timeout_partition(self):
        operation = Bus.write(16, 100, 2).json()
        self.assertEqual(operation, {"op": "write", "addr": "0x010", "data": "0x00000064", "wstrb": "0x2"})
        p = synthesize_plan({"timeout_ticks": (1 << 63) - 1, "quiesce": False})
        self.assertIn(Bus.write(24, 0xFFFFFFFF), p)
        self.assertIn(Bus.write(28, 0x7FFFFFFF), p)
        self.assertNotIn(Bus.write(COMMAND, 0, 1), p)

    def test_missing_or_invalid_atoms(self):
        cases = (
            ({"timeout_ticks": 0}, "nonzero"),
            ({"timeout_ticks": 2 ** 63}, "unsigned"),
            ({"period_ns": "0"}, "positive"),
            ({"period_ns": "-1"}, "positive"),
            ({"period_ns": str(1 << 32)}, "32.32"),
            ({"period_ns": True}, "decimal string"),
            ({"period_ns": "6/0"}, "syntax"),
            ({"period_ns": 6.5}, "decimal string"),
            ({"arm": True}, "unrecognized"),
            ({"armed": 1}, "boolean"),
            ({"trace": {"statuses": "MATCH"}}, "list"),
            ({"trace": {"statuses": ["BOGUS"]}}, "unrecognized trace"),
            ({"trace": {"statuses": ["MATCH", "match"]}}, "duplicate"),
            ({"trace": {"arm": True}}, "at least one"),
            ({"trace": {"enable": False, "arm": True, "threshold_ticks": 1}}, "capture enabled"),
        )
        for item, message in cases:
            with self.subTest(item=item):
                with self.assertRaisesRegex(ConfigError, message):
                    synthesize_plan(item)

    def test_depth_applies_to_pretrigger_retention(self):
        p = synthesize_plan({"trace": {"post": 3}}, trace_depth=4)
        self.assertIn(Bus.write(TRACE_CONTROL, 0x10300), p)
        with self.assertRaisesRegex(ConfigError, "power"):
            synthesize_plan({}, trace_depth=9)
        with self.assertRaisesRegex(ConfigError, "unsigned"):
            synthesize_plan({"trace": {"post": 4}}, trace_depth=4)

    def test_bus_error_domain(self):
        with self.assertRaises(ConfigError):
            Bus.write(3, 0)
        with self.assertRaises(ConfigError):
            Bus.write(16, 2 ** 32)
        with self.assertRaises(ConfigError):
            Bus.write(16, 2, 0)
        with self.assertRaises(ConfigError):
            Bus.poll(COMMAND, 1, 2)
        with self.assertRaises(ConfigError):
            Bus.read(127)


if __name__ == "__main__":
    unittest.main()
