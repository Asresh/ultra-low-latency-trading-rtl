"""Check PPS arithmetic, retained audit identity, and exact epoch integration."""

from dataclasses import replace
from fractions import Fraction
from io import StringIO
import unittest

from latency_reference import LIMIT64
from pps_discipline_audit import Audit, Code, Mode, decode, rate_epochs, read_audits
from pps_discipline_model import Command, Discipline, INITIAL, Inputs, Op, Policy, Q32
from pps_discipline_model import measured_period


class PeriodTest(unittest.TestCase):
    def test_fixed_point_ties_even_both_directions(self):
        ticks = 1 << 33
        self.assertEqual(tuple(measured_period(i, ticks) for i in (1, 3, 5, 7)), (0, 2, 2, 4))

    def test_wide_input_and_integer_boundary(self):
        self.assertEqual(measured_period(LIMIT64, 1 << 32), LIMIT64)
        self.assertGreater(measured_period(LIMIT64, (1 << 32) - 1), LIMIT64)
        with self.assertRaisesRegex(ValueError, "positive"):
            measured_period(1, 0)


class AuditTest(unittest.TestCase):
    def test_unpack_all_fields_at_wire_limits(self):
        audit = Audit(8, Code.APPLIED, Mode.HOLDOVER, LIMIT64 - 5, LIMIT64 - 100,
                      (1 << 63) - 1, 9*Q32, 8*Q32+1, 17)
        text = f"# capture\n17 8 {audit.word():082x} # accepted\n"
        self.assertEqual(tuple(read_audits(StringIO(text))), (audit,))

    def test_reject_reserved_codes_and_missing_measurements(self):
        invalid = Audit(0, Code.APPLIED, Mode.ACQUIRE, 5, 4, 0, 9*Q32, 9*Q32, 0)
        with self.assertRaisesRegex(ValueError, "measurement"):
            decode(0, 0, invalid.word())
        with self.assertRaises(ValueError):
            decode(0, 0, 14 << 322)
        with self.assertRaisesRegex(ValueError, "line 2"):
            tuple(read_audits(["# header\n", "1 xyz\n"]))

    def test_rate_history_uses_effective_tick_and_handles_wrap(self):
        actual = 8*Q32+11
        first = LIMIT64 - 2
        audits = (Audit(1, Code.APPLIED, Mode.ACQUIRE, 0, LIMIT64 - 1, 3,
                        9*Q32, actual, 0),)
        rate_map = rate_epochs(audits, first, INITIAL, 0, 1)
        self.assertEqual(rate_map.elapsed(first, 0), Fraction(24))
        self.assertEqual(rate_map.elapsed(0, 3), Fraction(3*actual, Q32))
        with self.assertRaisesRegex(ValueError, "did not change"):
            rate_epochs((replace(audits[0], period=INITIAL),), first, INITIAL, 0, 1)

    def test_sequence_gaps_and_tail_marker_are_independent_of_loss_metrics(self):
        seed = Audit(1, Code.SEED, Mode.ACQUIRE, 1, 1, 0, 0, INITIAL, 0)
        rate = Audit(1, Code.APPLIED, Mode.ACQUIRE, 99, 3, 120, 9*Q32, 9*Q32, 2)
        with self.assertRaisesRegex(ValueError, "dropped audit"):
            rate_epochs((seed, rate), 0, INITIAL, 1, 3)
        with self.assertRaisesRegex(ValueError, "sequence 1"):
            rate_epochs((seed, rate), 0, INITIAL, 0, 3)
        with self.assertRaisesRegex(ValueError, "cursor 2"):
            rate_epochs((seed,), 0, INITIAL, 0, 2)
        with self.assertRaisesRegex(ValueError, "sequence 1"):
            rate_epochs((seed, seed), 0, INITIAL, 0, 2)


class DisciplineTest(unittest.TestCase):
    def setUp(self):
        self.model = Discipline(1, 3)
        self.policy = Policy(True, 1000, 90, 150, 501, 12, 2, 5*Q32, 12*Q32, 1<<20, 1)
        self.model.advance(Inputs(control=Command(Op.PROGRAM, 0, self.policy)))

    def test_early_edge_stale_sample_and_holdover(self):
        self.model.advance(Inputs(now=5, sample=5))
        self.model.advance(Inputs(now=10, sample=10))
        self.assertEqual(self.model.audits[0].code, Code.EARLY)
        self.assertEqual(self.model.anchor, 5)
        self.model.advance(Inputs(now=110, sample=95))
        self.assertEqual(self.model.audits[0].code, Code.STALE_SAMPLE)
        self.assertEqual(self.model.anchor, 5)
        self.model.advance(Inputs(now=506))
        self.assertEqual(self.model.mode, Mode.HOLDOVER)
        self.assertEqual(self.model.period, INITIAL)

    def test_counter_clear_preserves_audit_sequence(self):
        self.model.advance(Inputs(now=5, sample=5, audit_ready=False))
        self.model.advance(Inputs(now=7, sample=7, audit_ready=False))
        self.assertEqual(self.model.counts["lost"], 1)
        self.model.advance(Inputs(now=8, control=Command(Op.CLEAR, 1)))
        self.assertEqual(self.model.counts["lost"], 0)
        self.assertEqual(self.model.audit_cursor, 2)
        self.model.advance(Inputs(now=9, sample=9))
        self.assertEqual(self.model.audits[0].sequence, 2)


if __name__ == "__main__":
    unittest.main()
