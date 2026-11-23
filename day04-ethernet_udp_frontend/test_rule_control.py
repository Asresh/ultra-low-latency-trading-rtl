import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from net_wire import Match, Result, Rule, classify, parse_frame
from net_scenarios import standard
from rule_control import (
    ABI_MAJOR, Access, Control, ControlError, CSR_CLEAR, CSR_COMMIT, CSR_EPOCH, CSR_ID, CSR_INDEX,
    CSR_PUSH, CSR_SNAPSHOT, CSR_STAT_HIGH, CSR_STAT_ID, CSR_STAT_INDEX, CSR_STAT_LOW, CSR_VERSION,
    CSR_WORD0, IDENTITY, access_read, access_write, load_policy, main, parse_policy, policy_plan,
    snapshot_plan,
)


class Registers:
    def __init__(self):
        self.epoch = 0
        self.active = 0
        self.bank = [[0]*8 for _ in range(2)]
        self.index = 0
        self.words = [0]*8
        self.count_index = 0
        self.live = [0]*32
        self.frozen = [0]*32
        self.snap_id = 0
        self.write_count = 0
        self.pushes = 0
        self.commits = 0
        self.fail_after = 0
        self.wrong_words = False

    def transact(self, command):
        address = command.address
        if not command.write:
            reads = {
                CSR_EPOCH:self.epoch, CSR_ID:IDENTITY, CSR_VERSION:ABI_MAJOR<<16,
                CSR_INDEX:self.index, CSR_STAT_INDEX:self.count_index, CSR_STAT_ID:self.snap_id,
                CSR_STAT_LOW:self.frozen[self.count_index] & 0xffffffff,
                CSR_STAT_HIGH:self.frozen[self.count_index] >> 32,
            }
            if CSR_WORD0 <= address <= CSR_WORD0+28:
                value = self.words[(address-CSR_WORD0)//4]
                return 0,value ^ int(self.wrong_words)
            if address not in reads:
                return 2,0
            return 0,reads[address]
        self.write_count += 1
        if self.fail_after and self.write_count >= self.fail_after:
            return 1,0
        if address == CSR_INDEX:
            self.index = command.value
        elif address == CSR_STAT_INDEX:
            self.count_index = command.value
        elif CSR_WORD0 <= address <= CSR_WORD0+28:
            self.words[(address-CSR_WORD0)//4] = command.value
        elif address == CSR_PUSH:
            self.bank[1-self.active][self.index] = sum(value << (32*i)
                                                     for i,value in enumerate(self.words))
            self.pushes += 1
        elif address == CSR_COMMIT:
            self.active ^= 1
            self.epoch = (self.epoch+1)&255
            self.commits += 1
        elif address == CSR_SNAPSHOT:
            self.frozen = self.live.copy()
            self.snap_id += 1
        elif address == CSR_CLEAR:
            self.live[:] = [0]*32
        else:
            return 1,0
        return 0,command.value


class PolicyParsingTests(unittest.TestCase):
    def test_parse_all_match_kinds_and_steer(self):
        document = {
            "rules":[
                {"dst_ip":"239.1.2.99/24","src_ip":"10.15.4.0/24",
                 "dst_port":"0x4650","src_port":{"value":"0x2ee0","mask":"0xfff0"},
                 "tci":{"value":"0x007","mask":"0xfff"},"tags":1,"queue":7},
                {"enabled":False,"action":"drop"},
                {"src_ip":"*","action":"drop"},
            ]
        }
        rules = parse_policy(document)
        first = rules[0]
        self.assertEqual(first.dst_ip.mask,0xffffff00)
        self.assertEqual(first.src_ip.value,0x0a0f0400)
        self.assertEqual(first.dst_port.value,18000)
        self.assertFalse(rules[1].enabled)
        data = parse_frame(standard(b"x",tags=((0x8100,0x2007),)).serialize())
        outcome = classify(data,rules,epoch=2)
        self.assertEqual((outcome.result,outcome.queue,outcome.epoch),(Result.OK,7,2))

    def test_invalid_schema_is_rejected(self):
        cases = (
            {}, {"rules":"ring"}, {"rules":[{}]*9}, {"rules":[42]},
            {"rules":[{"action":"mirror"}]}, {"rules":[{"enabled":"yes"}]},
            {"rules":[{"action":"drop","queue":1}]}, {"rules":[{"queue":8}]},
            {"rules":[{"src_port":-1}]}, {"rules":[{"tags":4}]},
            {"rules":[{"dst_ip":"2001:db8::/32"}]},
            {"rules":[{"dst_port":{"value":4}}]}, {"rules":[{"tci":True}]},
            {"rules":[{"ports":[12,13]}]},
        )
        for value in cases:
            with self.subTest(value=value),self.assertRaises(ValueError):
                parse_policy(value)

    def test_load_file_constraint_and_plan_json(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Path(directory)/"policy.json"
            cfg.write_text(json.dumps({"rules":[{"action":"pass","queue":3}]}))
            self.assertEqual(load_policy(cfg),(Rule(queue=3),))
            log = io.StringIO()
            with patch("sys.argv",["rule_control","rules",str(cfg),"--epoch","0xff",
                                   "--no-readback"]),redirect_stdout(log):
                main()
            plan = json.loads(log.getvalue())
            self.assertEqual(plan[2]["expect"],"0x000000ff")
            self.assertEqual(plan[-1]["expect"],"0x00000000")
            self.assertEqual(sum(op["address"]=="0x40" for op in plan),8)

    def test_invalid_transaction_word_alignment_and_numeric_width(self):
        for args in ((True,-4,0,15),(True,0x60,0,16),(False,0x21,0,0),(True,4,1<<32,15)):
            with self.subTest(args=args),self.assertRaises(ValueError):
                Access(*args)
        with self.assertRaises(ValueError):
            list(snapshot_plan([1,1]))
        with self.assertRaises(ValueError):
            list(snapshot_plan([32]))

    def test_explicit_plan_contains_prior_epoch_and_single_commit(self):
        sequence = list(policy_plan([Rule(queue=6)],expected_epoch=4))
        self.assertEqual(sequence[:3],[
            access_read(CSR_ID,IDENTITY),access_read(CSR_VERSION,1<<16),access_read(CSR_EPOCH,4),
        ])
        self.assertEqual(sequence[-2:],[
            access_write(CSR_COMMIT,1),access_read(CSR_EPOCH,5),
        ])
        self.assertEqual(sum(item.address==CSR_PUSH for item in sequence),8)
        self.assertEqual(sum(item.address==CSR_COMMIT for item in sequence),1)


class ControlExecutionTests(unittest.TestCase):
    def test_replace_defines_all_rows_before_swap(self):
        device = Registers()
        master = Control(device.transact)
        expected = (Rule(queue=3),Rule(tags=Match(2,3),drop=True))
        self.assertEqual(master.replace_policy(expected),1)
        self.assertEqual(device.bank[1][:2],[rule.encode() for rule in expected])
        self.assertEqual(device.bank[1][2:],[0]*6)
        self.assertEqual(device.pushes,8)
        self.assertEqual(master.replace_policy([Rule(queue=7)],verify_words=False),2)
        self.assertEqual(device.bank[0],[Rule(queue=7).encode()]+[0]*7)
        self.assertEqual(device.commits,2)

    def test_readback_error_never_triggers_commit(self):
        device = Registers()
        device.wrong_words = True
        with self.assertRaisesRegex(ControlError,"expected"):
            Control(device.transact).replace_policy([Rule(queue=1)])
        self.assertEqual((device.active,device.commits),(0,0))

    def test_command_status_never_partially_applies_active_rule(self):
        device = Registers()
        device.fail_after = 28
        with self.assertRaisesRegex(ControlError,"status"):
            Control(device.transact).replace_policy([Rule(queue=1)])
        self.assertGreater(device.pushes,0)
        self.assertEqual((device.active,device.commits),(0,0))

    def test_counter_combine_clear_and_later_snapshot(self):
        device = Registers()
        device.live[2] = 0xabcd0123456789
        device.live[31] = (1<<64)-1
        control = Control(device.transact)
        stamp,counters = control.snapshot([2,31],clear=True)
        self.assertEqual(stamp,1)
        self.assertEqual(counters,{2:0xabcd0123456789,31:(1<<64)-1})
        self.assertEqual(device.live,[0]*32)
        second,empty = control.snapshot([31])
        self.assertEqual((second,empty),(2,{31:0}))

    def test_changed_snapshot_id_prevents_incoherent_read_and_clear(self):
        device = Registers()
        device.live[2] = 7
        seen = 0

        def race(request):
            nonlocal seen
            if request.address == CSR_STAT_ID:
                seen += 1
                if seen == 2:
                    device.snap_id += 1
            return device.transact(request)

        with self.assertRaisesRegex(ControlError,"changed"):
            Control(race).snapshot([2],clear=True)
        self.assertEqual(device.live[2],7)


if __name__ == "__main__":
    unittest.main()
