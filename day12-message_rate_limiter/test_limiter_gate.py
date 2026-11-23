"""Test the forwarding wrapper independently of the main pin-model matrix."""

import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from limiter_gate_model import AUDIT_NAMES, GateDrive, GateModel, GatePins
from limiter_protocol import Command, Operation, PROFILES, Status, write_rows
from run_gate_matrix import GateSchedule, compare_capture, gate_cases


class GateModelTest(unittest.TestCase):
    """Exercise both consumption orders, replacements, rejections, and reset."""

    def setUp(self):
        """Create a default-width gate with no pending payload."""
        self.geometry = PROFILES["default"]
        self.gate = GateModel(self.geometry)

    def send(self, command, *, data=0xFE123456, audit_ready=1, output_ready=1):
        """Offer one command with independently configured consumers."""
        return self.gate.step(GateDrive(
            valid=1, audit_ready=audit_ready, output_ready=output_ready, data=data, command=command,
        ))

    def configure(self, cap=8, rate=1):
        """Enable key zero and arrange that its configuration is acknowledged."""
        self.send(Command.configure(0, 0, cap, rate, 20))
        response = self.gate.step(GateDrive())
        self.assertEqual(response.audit[:4], (int(Operation.CONFIG), 0, 20, int(Status.OK)))
        self.assertIsNone(response.output)

    def test_acknowledge_audit_then_read_payload_with_stable_tag(self):
        """The earlier audit transfer must disappear while the later data stays."""
        self.configure()
        self.send(Command.offer(0, 0, 2, 9))
        first = self.gate.step(GateDrive(audit_ready=1, output_ready=0))
        self.assertEqual(first.input_ready, 0)
        self.assertEqual(first.audit[:4], (int(Operation.OFFER), 0, 9, int(Status.PASS)))
        self.assertEqual(first.output, (0, 9, 0xFE123456))
        held = self.gate.step(GateDrive(audit_ready=0, output_ready=0))
        self.assertIsNone(held.audit)
        self.assertEqual(held.output, first.output)
        done = self.gate.step(GateDrive(audit_ready=0, output_ready=1))
        self.assertEqual(done, GatePins(1, None, first.output))
        self.assertEqual(self.gate.step(GateDrive()), GatePins(1, None, None))

    def test_acknowledge_data_then_audit_stays_ready_independent(self):
        """The still-pending audit remains stable with no duplicate data transfer."""
        self.configure()
        self.send(Command.offer(0, 0, 8, 3), data=0x4567)
        first = self.gate.step(GateDrive(audit_ready=0, output_ready=1))
        self.assertIsNotNone(first.audit)
        self.assertEqual(first.output, (0, 3, 0x4567))
        for _ in range(4):
            waiting = self.gate.step(GateDrive(audit_ready=0, output_ready=1))
            self.assertEqual(waiting, GatePins(0, first.audit, None))
        clear = self.gate.step(GateDrive(audit_ready=1, output_ready=0))
        self.assertEqual(clear, GatePins(1, first.audit, None))
        self.assertEqual(self.gate.step(GateDrive()), GatePins(1, None, None))

    def test_limited_result_never_creates_a_payload_transfer(self):
        """Quotas affect forwarding without hiding the reason from audit."""
        self.configure(1, 0)
        rejected = Command.offer(0, 0, 2, 21)
        accepted = self.send(rejected, output_ready=0)
        self.assertEqual(accepted.input_ready, 1)
        later = self.gate.step(GateDrive(audit_ready=1, output_ready=0))
        self.assertEqual(later.input_ready, 1)
        self.assertEqual(later.audit[:4], (int(Operation.OFFER), 0, 21, int(Status.LIMITED)))
        self.assertIsNone(later.output)
        self.assertEqual(later.audit[6], 1)

    def test_in_place_accepted_payload_is_replaced_only_after_old_transfer(self):
        """The old pair transfers before next-cycle pins reveal the new payload."""
        self.configure()
        self.send(Command.offer(0, 0, 2, 1), data=0xAAAAAAAA)
        sample = self.send(Command.offer(0, 1, 4, 2), data=0xBBBBBBBB)
        self.assertEqual(sample.output, (0, 1, 0xAAAAAAAA))
        self.assertEqual(sample.audit[2], 1)
        self.assertEqual(sample.input_ready, 1)
        following = self.gate.step(GateDrive())
        self.assertEqual(following.output, (0, 2, 0xBBBBBBBB))
        self.assertEqual(following.audit[4], 3)
        self.assertIsNone(self.gate.step(GateDrive()).output)

    def test_unaccepted_source_cannot_modify_saved_payload_or_consumed_tokens(self):
        """A fully stalled slot rejects even a ready-looking new offer."""
        self.configure()
        self.send(Command.offer(0, 0, 1, 1), data=17)
        for data in range(4):
            pending = self.send(
                Command.offer(0, 1, 8, 2), data=data, audit_ready=0, output_ready=0
            )
            self.assertEqual(pending.input_ready, 0)
            self.assertEqual(pending.output[-1], 17)
            self.assertEqual(self.gate.implementation.bucket(0).balance, 7)

    def test_reset_fences_the_other_half_even_when_consumer_is_ready(self):
        """After data ack, reset may cancel audit without replaying prior data."""
        self.configure()
        self.send(Command.offer(0, 0, 4, 1), data=10)
        self.gate.step(GateDrive(audit_ready=0, output_ready=1))
        fenced = self.gate.step(GateDrive(reset=1, audit_ready=1, output_ready=1))
        self.assertEqual(fenced, GatePins(0, None, None))
        self.assertEqual(self.gate.step(GateDrive()), GatePins(1, None, None))
        self.send(Command.offer(0, 0, 1, 2))
        denied = self.gate.step(GateDrive())
        self.assertEqual(denied.audit[3], int(Status.DISABLED))
        self.assertIsNone(denied.output)

    def test_drive_and_snapshot_wire_order(self):
        """Pin fixtures must distinguish each sink and zero only inactive fields."""
        cmd = Command.offer(3, 8, 9, 10)
        drive = GateDrive(1, 1, 0, 1, 0x11223344, cmd)
        self.assertEqual(drive.words(), (1, 1, 0, 1, 0x11223344, 1, 3, 8, 9, 0, 0, 10))
        for name in ("reset", "valid", "audit_ready", "output_ready"):
            with self.assertRaisesRegex(ValueError, name):
                replace(drive, **{name: 4}).validate(self.geometry, 32)
        with self.assertRaisesRegex(ValueError, "data"):
            replace(drive, data=1 << 32).validate(self.geometry, 32)
        self.assertEqual(GatePins(1, None, (3, 10, 12)).words(), (
            1, 0, 1, *([0] * len(AUDIT_NAMES)), 3, 10, 12,
        ))


class GateCampaignTest(unittest.TestCase):
    """Correlate the two consumed streams across directed and randomized traffic."""

    def test_wrapper_seed_and_explicit_coverage(self):
        """Each sink ordering and reset fence occurs in the deterministic preamble."""
        first = gate_cases(315, 0)
        again = gate_cases(315, 0)
        self.assertEqual(first.drives, again.drives)
        self.assertEqual(first.expect, again.expect)
        for event in (
            "audit_before_data", "data_before_audit", "held_audit", "held_data", "held_request",
            "reset_discarded_audit", "reset_discarded_data", "in_place_replacement",
        ):
            self.assertGreater(first.covered[event], 0, event)
        self.assertEqual(first.covered["accepted"], first.covered["audit"] + first.covered["reset_discarded_audit"])

    def test_audit_and_forwarded_sequences_preserve_input_identity(self):
        """Independent raw-pin scans prove data conservation across different sink stalls."""
        for seed in (2, 3, 17, 315, 4412):
            plan = gate_cases(seed, 800)
            inputs = {}
            consumed = set()
            audited = set()
            permitted = set()
            for drive, pins in zip(plan.drives, plan.expect):
                self.assertEqual(len(drive), 12)
                self.assertEqual(len(pins), 15)
                if drive[0]:
                    self.assertEqual(pins[:3], (0, 0, 0))
                    inputs.clear()
                    continue
                if drive[1] and pins[0]:
                    inputs[drive[-1]] = (drive[6], drive[4])
                if drive[2] and pins[1]:
                    tag = pins[5]
                    self.assertIn(tag, inputs)
                    self.assertNotIn(tag, audited)
                    audited.add(tag)
                    if pins[6] == Status.PASS:
                        permitted.add(tag)
                if drive[3] and pins[2]:
                    key, tag, payload = pins[-3:]
                    self.assertIn(tag, inputs)
                    self.assertEqual(inputs[tag], (key, payload))
                    self.assertNotIn(tag, consumed)
                    consumed.add(tag)
            self.assertEqual(len(consumed), plan.covered["forwarded"])
            self.assertEqual(len(audited), plan.covered["audit"])
            self.assertEqual(len(permitted), plan.covered["PASS"])
            self.assertLessEqual(len(consumed - permitted), plan.covered["reset_discarded_audit"])
            self.assertLessEqual(len(permitted - consumed), plan.covered["reset_discarded_data"])

    def test_capture_length_and_pin_error_are_actionable(self):
        """Identify the exact cycle and pin when a simulated result is corrupted."""
        plan = gate_cases(315, 8)
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "gate.hex"
            write_rows(file, plan.expect)
            compare_capture(plan, file)
            write_rows(file, plan.expect[:-1])
            with self.assertRaisesRegex(AssertionError, "trace length"):
                compare_capture(plan, file)
            changed = list(plan.expect)
            altered = list(changed[7])
            altered[-1] ^= 0xFF
            changed[7] = tuple(altered)
            write_rows(file, changed)
            with self.assertRaisesRegex(AssertionError, "cycle 7: pin\[14\]"):
                compare_capture(plan, file)

    def test_output_stalls_do_not_change_offer_decisions_without_reset(self):
        """Backpressure shifts delivery cycles without changing admitted tags."""
        rng = random.Random(33)
        tight = GateSchedule()
        delayed = GateSchedule()
        for plan in (tight, delayed):
            for key in range(plan.geometry.keys):
                plan.config(key, 22, 12, key)
        for number in range(216):
            key = number % tight.geometry.keys
            tick = 22 + number // tight.geometry.keys
            cost = 1 + number % 15
            pattern = tuple((rng.randrange(2), rng.randrange(2)) for _ in range(8))
            tight.offer(key, tick, cost, number)
            delayed.offer(key, tick, cost, number, pattern)
        for plan in (tight, delayed):
            plan.idle()
        streams = []
        for plan in (tight, delayed):
            delivered = [
                (pins[5], pins[6], pins[7])
                for drive, pins in zip(plan.drives, plan.expect)
                if drive[2] and pins[1]
            ]
            streams.append(delivered)
        self.assertEqual(streams[0], streams[1])
        self.assertGreater(len(delayed.drives), len(tight.drives))


if __name__ == "__main__":
    unittest.main()
