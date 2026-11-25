"""Semantic tests for deadline policy ordering and the host alert reader."""

from io import StringIO
import unittest

from deadline_alert import decode_alert, read_alerts
from deadline_model import (
    Command, Input, Monitor, Op, Policy, Report, Result, U16_MAX, U32_MAX, U64_MAX, pack,
)


class DeadlineTest(unittest.TestCase):
    def setUp(self):
        self.monitor = Monitor(2, 1, 3)

    def apply(self, **ports):
        return self.monitor.advance(Input(**ports))

    def program(self, policy, slot=0, revision=0):
        self.apply(command=Command(Op.PROGRAM, slot, revision, policy=policy))
        return self.monitor.reply

    def test_class_order_and_threshold_equality(self):
        self.program(Policy(True, 3, 1, 4, 0, 10, 2, 2))
        self.program(Policy(True, 0, 0, 0, 7, 5, 1, 1), slot=1)
        for duration in (10, 11, 12, 10, 10):
            self.apply(report=Report(duration=duration, tick=duration))
            if duration == 11:
                self.assertEqual(len(self.monitor.alerts), 0)
            if duration == 12:
                alert = self.monitor.alerts[0]
                fields = decode_alert(alert.slot, alert.revision, alert.data)
                self.assertEqual((fields.rule, fields.transition, fields.consecutive), (0, "trip", 2))
        alert = self.monitor.alerts[0]
        self.assertEqual(decode_alert(alert.slot, alert.revision, alert.data).transition, "recovery")
        self.apply(report=Report(stop=9, duration=6))
        alert = self.monitor.alerts[0]
        self.assertEqual(alert.slot, 1)
        self.assertEqual(self.monitor.rules[0].stats["matched"], 5)
        self.assertEqual(self.monitor.rules[1].stats["matched"], 1)

    def test_drop_does_not_hold_alarm_and_pop_frees_space(self):
        self.program(Policy(True, wild=7, limit=10, trip=1, recover=1))
        self.apply(report=Report(duration=11), alert_ready=False)
        held = self.monitor.alerts[0]
        self.apply(report=Report(duration=10), alert_ready=False)
        self.assertFalse(self.monitor.rules[0].alarm)
        self.assertEqual(self.monitor.global_stats["lost"], 1)
        self.assertEqual(self.monitor.rules[0].stats["recover"], 1)
        self.assertEqual(self.monitor.rules[0].stats["lost"], 1)
        self.assertEqual(self.monitor.alerts[0], held)
        self.apply(report=Report(tag=2, duration=12), alert_ready=True)
        self.assertNotEqual(self.monitor.alerts[0], held)
        self.assertEqual(self.monitor.global_stats["lost"], 1)

    def test_program_preserves_preedge_revision_and_clear_cut(self):
        self.program(Policy(True, wild=7, limit=10, trip=1, recover=1))
        self.apply(report=Report(duration=21), command=Command(
            Op.PROGRAM, revision=1, policy=Policy(True, wild=7, limit=20, trip=2, recover=2),
        ))
        self.assertEqual(self.monitor.alerts[0].revision, 1)
        self.assertEqual(self.monitor.reply.revision, 2)
        rule = self.monitor.rules[0]
        self.assertEqual((rule.alarm, rule.bad, rule.good), (False, 0, 0))
        self.assertEqual(rule.stats["breaches"], 1)
        old = rule.snapshot()
        self.apply(report=Report(duration=9), command=Command(Op.CLEAR, revision=2))
        self.assertEqual(self.monitor.reply.data, old)
        self.assertEqual((rule.stats["matched"], rule.stats["breaches"], rule.worst), (1, 0, 9))
        self.assertEqual(rule.good, 1)

    def test_reply_stays_held_and_revision_never_wraps(self):
        self.program(Policy())
        expected = self.monitor.reply
        self.apply(command=Command(Op.INSPECT, 1), reply_ready=False)
        self.assertEqual(self.monitor.reply, expected)
        self.assertEqual(self.program(Policy()).status, Result.STALE)
        self.assertEqual(self.program(Policy(enabled=True), revision=1).status, Result.BAD_POLICY)
        for revision in range(1, 7):
            self.assertEqual(self.program(Policy(), revision=revision).status, Result.DONE)
        exhausted = self.program(Policy(), revision=7)
        self.assertEqual((exhausted.status, exhausted.revision), (Result.EXHAUSTED, 7))
        self.apply(command=Command(Op.CLEAR, revision=7))
        self.assertEqual(self.monitor.reply.revision, 7)
        self.assertEqual(self.monitor.reply.status, Result.DONE)

    def test_counter_saturation_and_nonmatches_excluded(self):
        self.program(Policy(True, wild=7, limit=0, trip=U16_MAX, recover=U16_MAX))
        rule = self.monitor.rules[0]
        rule.stats.update(matched=U32_MAX, breaches=U32_MAX)
        self.monitor.global_stats["seen"] = U32_MAX
        rule.bad = U16_MAX - 1
        self.apply(report=Report(duration=U64_MAX))
        self.assertTrue(rule.alarm)
        self.apply(report=Report(duration=U64_MAX))
        self.apply(report=Report(status=2))
        self.assertEqual((rule.bad, rule.worst), (U16_MAX, U64_MAX))
        self.assertEqual((rule.stats["matched"], rule.stats["breaches"]), (U32_MAX, U32_MAX))
        self.assertEqual(self.monitor.global_stats["seen"], U32_MAX)
        self.assertEqual(self.monitor.global_stats["nonmatch"], 1)

    def test_reset_discards_transfers_and_policy(self):
        self.program(Policy(True, wild=7, limit=0, trip=1, recover=1))
        self.apply(report=Report(duration=1))
        before = self.apply(reset=True, report=Report(), command=Command(Op.CLEAR), alert_ready=True)
        self.assertEqual((before.ctl_ready, before.reply_valid, before.alert_valid), (0, 0, 0))
        self.assertEqual(len(self.monitor.alerts), 0)
        self.assertEqual(self.monitor.rules[0].revision, 0)


class DeadlineAlertTest(unittest.TestCase):
    def word(self, code=1, consecutive=1, duration=10):
        return pack(
            (255, 8), (0xFA01, 16), (4, 8), (9, 8), (code, 8), (consecutive, 16),
            (9, 64), (duration, 64), (U64_MAX, 64),
        )

    def test_decode_network_fields_and_max_ticks(self):
        data = f"# captured alerts\n\n2 12 {self.word():064x} # trip\n"
        alert, = read_alerts(StringIO(data))
        self.assertEqual((alert.rule, alert.revision), (2, 12))
        self.assertEqual((alert.event_class, alert.tag, alert.start_probe, alert.stop_probe), (255, 0xFA01, 4, 9))
        self.assertEqual((alert.limit_ticks, alert.duration_ticks, alert.processing_tick), (9, 10, U64_MAX))
        self.assertEqual(alert.transition, "trip")

    def test_recovery_at_exact_limit(self):
        alert = decode_alert(0, 1, self.word(code=2, duration=9))
        self.assertEqual(alert.transition, "recovery")

    def test_rejects_invalid_code_streak_width_and_revision(self):
        for rule, revision, word in (
            (0, 0, self.word()), (-1, 1, self.word()), (0, 1, self.word(code=3)),
            (0, 1, self.word(consecutive=0)), (0, 1, self.word(duration=9)), (0, 1, 1 << 256),
        ):
            with self.subTest(rule=rule, revision=revision, word=word):
                with self.assertRaises(ValueError):
                    decode_alert(rule, revision, word)
        with self.assertRaisesRegex(ValueError, "line 2"):
            list(read_alerts(["# header\n", "0 1 xyz\n"]))


if __name__ == "__main__":
    unittest.main()
