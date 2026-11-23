"""Focused model tests and a separate token-by-token chronological oracle."""

import random
import unittest
from dataclasses import replace

from limiter_model import Bucket, CycleModel, Limiter, ReferenceVerifier, Statistics, legal_fields
from limiter_protocol import Command, Drive, Geometry, Operation, Status, ZERO_RESPONSE
from limiter_scenarios import Schedule, accepted_statuses, boundary_cases, parallel_burst, random_traffic


def slow_refill(capacity, amount, rate, gap):
    """Refill one unit of time at a time, independent of the multiplier equation."""
    for _ in range(gap):
        amount = min(capacity, amount + rate)
    return amount


def simple_sink(geometry, capacity, rate, actions):
    """Evaluate offers in absolute time without production status handling."""
    balance = capacity
    previous = 0
    passes = 0
    denies = 0
    charged = 0
    lost = 0
    answers = []
    for tick, cost in actions:
        balance = slow_refill(capacity, balance, rate, tick - previous)
        before = balance
        if cost <= balance:
            balance -= cost
            passes += 1
            charged += cost
            matched = Status.PASS
        else:
            denies += 1
            matched = Status.LIMITED
        for bit, counter in enumerate((passes, denies, charged)):
            if counter > geometry.count_max:
                lost |= 1 << bit
        answers.append((
            matched,
            before,
            balance,
            min(geometry.count_max, passes),
            min(geometry.count_max, denies),
            min(geometry.count_max, charged),
            lost,
        ))
        previous = tick
    return answers


def reported_offer(response):
    """Select direct admission data for comparison with the slow oracle."""
    return (
        response.status,
        response.before,
        response.balance,
        response.passed,
        response.limited,
        response.charged,
        response.saturated,
    )


class FieldRulesTest(unittest.TestCase):
    """Validate operation-specific zero fields and rejection precedence."""

    def setUp(self):
        """Start with one enabled key, one disabled key, and one invalid encoding."""
        self.geo = Geometry(keys=3, tick_w=5, token_w=5, count_w=4, tag_w=8)
        self.model = Limiter(self.geo)
        self.model.decide(Command.configure(0, 7, 10, 2, 1))

    def test_exact_command_validation(self):
        """Check every operation for both allowed and forbidden field combinations."""
        cases = (
            (Command.configure(0, 0, 5, 0), True),
            (Command.configure(0, 0, 0, 0), False),
            (Command.configure(0, 0, 5, 6), False),
            (Command.disable(0, 0), True),
            (Command(Operation.CONFIG, 0, 0, 5), False),
            (Command(Operation.CONFIG, 0, 0, rate=1), False),
            (Command.offer(0, 0, 0), False),
            (Command.offer(0, 0, 1), True),
            (Command(Operation.OFFER, 0, 0, 1, 1), False),
            (Command(Operation.OFFER, 0, 0, 1, enable=1), False),
            (Command.inspect(0, 0), True),
            (Command(Operation.INSPECT, 0, 0, 1), False),
            (Command(Operation.INSPECT, 0, 0, rate=1), False),
            (Command(Operation.INSPECT, 0, 0, enable=1), False),
            (Command.clear(0, 0), True),
            (Command(Operation.CLEAR, 0, 0, amount=1), False),
            (Command(Operation.CLEAR, 0, 0, rate=1), False),
            (Command(Operation.CLEAR, 0, 0, enable=1), False),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(legal_fields(command), expected)

    def test_rejection_precedence(self):
        """Check invalid key, fields, enabled state, and chronology in that order."""
        cases = (
            (Command.offer(3, 5, 0), Status.BAD_KEY),
            (Command.offer(1, 5, 0), Status.BAD_FIELDS),
            (Command.offer(1, 5, 1), Status.DISABLED),
            (Command.offer(0, 5, 0), Status.BAD_FIELDS),
            (Command.offer(0, 5, 1), Status.BAD_TICK),
        )
        baseline = self.model.rows.copy()
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(self.model.decide(command).status, expected)
                self.assertEqual(self.model.rows, baseline)

    def test_configure_replaces_stats_and_keeps_prior_before(self):
        """Reconfiguration begins full while reporting the old pre-command balance."""
        self.model.decide(Command.offer(0, 8, 4))
        reply = self.model.decide(Command.configure(0, 31, 4, 4, 21))
        self.assertEqual(reply.status, Status.OK)
        self.assertEqual(reply.tag, 21)
        self.assertEqual(reply.before, 6)
        self.assertEqual(reply.balance, 4)
        self.assertEqual(reply.anchor, 31)
        self.assertEqual(reply.capacity, 4)
        self.assertEqual(reply.rate, 4)
        self.assertEqual(reply.charged, 0)
        self.assertEqual(reply.passed, 0)
        self.assertEqual(reply.saturated, 0)

    def test_clear_preserves_newly_refilled_balance(self):
        """Counter clearing must advance time without refilling beyond policy."""
        self.model.decide(Command.offer(0, 7, 10))
        self.model.decide(Command.offer(0, 7, 1))
        reply = self.model.decide(Command.clear(0, 9))
        self.assertEqual(reply.status, Status.OK)
        self.assertEqual(reply.anchor, 9)
        self.assertEqual(reply.before, 4)
        self.assertEqual(reply.balance, 4)
        self.assertEqual(reply.capacity, 10)
        self.assertEqual(reply.rate, 2)
        self.assertEqual((reply.passed, reply.limited, reply.charged, reply.saturated), (0, 0, 0, 0))

    def test_half_range_and_near_wrap(self):
        """Distinguish forward wrap from the exactly half-range old timestamp."""
        self.model.decide(Command.configure(0, 30, 4, 1))
        self.model.decide(Command.offer(0, 30, 4))
        recent = self.model.decide(Command.inspect(0, 0))
        self.assertEqual(recent.before, 2)
        self.assertEqual(recent.anchor, 0)
        stale = self.model.decide(Command.inspect(0, 16))
        self.assertEqual(stale.status, Status.BAD_TICK)
        self.assertEqual(stale.anchor, recent.anchor)
        self.assertEqual(stale.balance, recent.balance)
        valid = self.model.decide(Command.inspect(0, 15))
        self.assertEqual(valid.status, Status.OK)
        self.assertEqual(valid.balance, 4)

    def test_invalid_config_does_not_disarm(self):
        """A malformed policy leaves the previous capacity and clock anchor intact."""
        expected = self.model.bucket(0)
        invalid = Command.configure(0, 3, 0, 2)
        self.assertEqual(self.model.decide(invalid).status, Status.BAD_FIELDS)
        self.assertEqual(self.model.bucket(0), expected)

    def test_disable_and_read(self):
        """A disabled row has no credit but still returns inspect and clear results."""
        disabled = self.model.decide(Command.disable(0, 2))
        inspected = self.model.decide(Command.inspect(0, 31))
        cleared = self.model.decide(Command.clear(0, 9))
        self.assertEqual(disabled.enabled, 0)
        self.assertEqual(disabled.before, 10)
        for reply in (inspected, cleared):
            self.assertEqual(reply.status, Status.OK)
            self.assertEqual(reply.anchor, 2)
            self.assertEqual(reply.capacity, 0)
            self.assertEqual(reply.before, 0)
        self.assertEqual(self.model.decide(Command.offer(0, 31, 1)).status, Status.DISABLED)

    def test_reject_python_objects_and_unrepresentable_input(self):
        """The oracle cannot accidentally compare a truncated vector field."""
        with self.assertRaises(TypeError):
            self.model.decide({"op": "offer"})
        with self.assertRaisesRegex(ValueError, "key"):
            self.model.decide(Command.offer(4, 0, 1))
        with self.assertRaisesRegex(ValueError, "tick"):
            self.model.decide(Command.offer(0, 32, 1))

    def test_stats_flags_do_not_light_at_exact_maximum(self):
        """Sticky saturation only records a counter increment that was lost."""
        stats = Statistics(5, 6, 0)
        exact = stats.add(7, passed=2, limited=1, charged=7)
        self.assertEqual(exact, Statistics(7, 7, 7, 0))
        after = exact.add(7, limited=1, charged=1)
        self.assertEqual(after, Statistics(7, 7, 7, 6))
        all_lost = after.add(7, passed=1)
        self.assertEqual(all_lost, Statistics(7, 7, 7, 7))
        unchanged = all_lost.add(7)
        self.assertEqual(unchanged, all_lost)


class PolicyPropertiesTest(unittest.TestCase):
    """Use independent per-tick arithmetic and trace relations as properties."""

    def test_slow_refill_across_all_small_states(self):
        """Check every small balance, capacity, rate, and forward interval."""
        geometry = Geometry(keys=3, tick_w=3, token_w=3, count_w=3)
        for capacity in range(1, geometry.token_max + 1):
            for rate in range(capacity + 1):
                for balance in range(capacity + 1):
                    for anchor in (0, geometry.tick_mask):
                        bucket = Bucket(True, anchor, capacity, rate, balance)
                        for delta in range(geometry.half):
                            tick = geometry.wrapped(anchor + delta)
                            actual = bucket.advance(geometry, tick)
                            self.assertEqual(actual.balance, slow_refill(capacity, balance, rate, delta))
                            self.assertEqual(actual.anchor, tick)
                            self.assertEqual(actual.statistics, bucket.statistics)
                        self.assertIsNone(bucket.advance(geometry, geometry.wrapped(anchor + geometry.half)))

    def test_randomized_overflow_near_wrap_matches_unbounded_accounting(self):
        """Compare modular hardware-sized offers with an independent absolute clock."""
        geometry = Geometry(keys=3, tick_w=5, token_w=6, count_w=4)
        generator = random.Random(4408)
        for trial in range(40):
            cap = generator.randint(1, geometry.token_max)
            rate = generator.randrange(cap + 1)
            relative = 0
            actions = []
            for _ in range(120):
                relative += generator.randrange(geometry.half)
                actions.append((relative, generator.randint(1, geometry.token_max)))
            values = simple_sink(geometry, cap, rate, actions)
            offset = geometry.tick_mask - trial % 8
            model = Limiter(geometry)
            model.decide(Command.configure(0, offset, cap, rate))
            for (tick, cost), expected in zip(actions, values):
                command = Command.offer(0, geometry.wrapped(offset + tick), cost)
                self.assertEqual(reported_offer(model.decide(command)), expected)

    def test_independent_keys_match_restricted_replay(self):
        """Removing all other keys from a run leaves the selected key's results intact."""
        geometry = Geometry(keys=5, tick_w=6, token_w=5, count_w=4)
        schedule = random_traffic(geometry, 955, 140)
        models = {key: Limiter(geometry) for key in range(geometry.keys)}
        parallel = Limiter(geometry)
        for drive, sample in zip(schedule.inputs, schedule.expected):
            if drive.reset:
                parallel.reset()
                for mirror in models.values():
                    mirror.reset()
                continue
            if not (drive.valid and sample.input_ready):
                continue
            decision = parallel.decide(drive.command)
            if drive.command.key in models:
                single = models[drive.command.key].decide(drive.command)
                self.assertEqual(single, decision)

    def test_refill_is_associative_until_cap_and_in_forward_range(self):
        """Splitting a chronologically legal interval preserves final credits."""
        geometry = Geometry(keys=1, tick_w=6, token_w=6)
        for initial in range(0, 11):
            for refill in range(5):
                for first in range(5):
                    for second in range(5):
                        row = Bucket(True, 60, 10, refill, initial)
                        middle = geometry.wrapped(row.anchor + first)
                        end = geometry.wrapped(row.anchor + first + second)
                        staged = row.advance(geometry, middle).advance(geometry, end)
                        self.assertEqual(staged, row.advance(geometry, end))


class HandshakeModelTest(unittest.TestCase):
    """Check acceptance, response ordering, and reset with exact cycle indexes."""

    def setUp(self):
        """Create a model with an empty response slot at the first edge."""
        self.geometry = Geometry(keys=3)
        self.pipeline = CycleModel(self.geometry)

    def test_replace_visible_entry_and_accept_new_command(self):
        """Consume one response and accept the next request on the same edge."""
        cfg = Command.configure(0, 0, 8, 0, 1)
        offer = Command.offer(0, 0, 4, 2)
        first = self.pipeline.step(Drive(valid=1, command=cfg))
        self.assertEqual((first.input_ready, first.output_valid), (1, 0))
        second = self.pipeline.step(Drive(valid=1, command=offer))
        self.assertEqual((second.input_ready, second.output_valid), (1, 1))
        self.assertEqual(second.response.tag, 1)
        done = self.pipeline.step(Drive())
        self.assertEqual(done.response.tag, 2)
        self.assertEqual(done.response.balance, 4)
        self.assertIsNone(self.pipeline.step(Drive()).response)

    def test_stalled_request_never_refills_or_changes_key(self):
        """A waiting query observes state only after its actual handshake."""
        cfg = Command.configure(0, 0, 10, 1)
        self.pipeline.step(Drive(valid=1, command=cfg))
        waiting = Command.inspect(0, 5)
        for _ in range(4):
            hold = self.pipeline.step(Drive(valid=1, ready=0, command=waiting))
            self.assertEqual(hold.input_ready, 0)
            self.assertEqual(hold.response.anchor, 0)
            self.assertEqual(self.pipeline.policy.bucket(0).anchor, 0)
        self.pipeline.step(Drive(valid=1, command=waiting))
        result = self.pipeline.step(Drive()).response
        self.assertEqual(result.anchor, 5)

    def test_reset_does_not_count_an_unchanged_visible_pin_as_transfer(self):
        """Synchronous reset cancels a result after sampling its old valid pin."""
        self.pipeline.step(Drive(valid=1, command=Command.configure(0, 1, 6, 0)))
        during = self.pipeline.step(Drive(reset=1, ready=1))
        self.assertEqual((during.input_ready, during.output_valid), (0, 1))
        self.assertIsNone(self.pipeline.step(Drive()).response)
        self.assertFalse(self.pipeline.policy.bucket(0).enabled)

    def test_first_empty_slot_can_accept_even_with_blocked_sink(self):
        """Initial pipeline capacity remains usable before any result occupies it."""
        before = self.pipeline.step(Drive(valid=1, ready=0, command=Command.inspect(0, 0)))
        self.assertEqual((before.input_ready, before.output_valid), (1, 0))
        after = self.pipeline.step(Drive(ready=0))
        self.assertEqual((after.input_ready, after.output_valid), (0, 1))

    def test_schedules_reproducible_and_in_order(self):
        """Equal seeds preserve exact drive words while different seeds diversify traffic."""
        first = random_traffic(self.geometry, 47, 90)
        replica = random_traffic(self.geometry, 47, 90)
        variant = random_traffic(self.geometry, 48, 90)
        self.assertEqual(first.inputs, replica.inputs)
        self.assertEqual(first.expected, replica.expected)
        self.assertNotEqual(first.inputs, variant.inputs)
        self.assertEqual(list(first.accepted), sorted(first.accepted))

    def test_continuous_sink_has_one_decision_per_edge_after_first(self):
        """A burst's sampled response stays one cycle behind every issued request."""
        plan = parallel_burst(self.geometry, 128)
        acceptance = [cycle for cycle, _ in plan.accepted]
        delivery = [
            cycle for cycle, (drive, observation) in enumerate(zip(plan.inputs, plan.expected))
            if not drive.reset and drive.ready and observation.output_valid
        ]
        self.assertEqual(delivery, [cycle + 1 for cycle in acceptance])
        self.assertEqual(acceptance, list(range(len(acceptance))))

    def test_boundary_scenario_has_explicit_status_coverage(self):
        """Every protocol decision is exercised by the hand-written short schedule."""
        counts = accepted_statuses(boundary_cases(self.geometry))
        self.assertEqual(set(counts), set(Status))

    def test_verifier_flags_fabricated_pass(self):
        """A response marked PASS must account for the exact consumed amount."""
        verifier = ReferenceVerifier(self.geometry)
        command = Command.offer(0, 0, 2)
        false_pass = replace(ZERO_RESPONSE, op=Operation.OFFER, status=Status.PASS)
        with self.assertRaisesRegex(AssertionError, "charged"):
            verifier.observe_decision(command, false_pass)

    def test_schedule_drains_after_waiting_source(self):
        """A delayed sink changes cycle timing while preserving exactly two accepted commands."""
        plan = Schedule(self.geometry)
        plan.configure(0, 0, 9, 0)
        plan.offer(0, 0, 2, wait=3)
        plan.finish()
        self.assertEqual(len(plan.accepted), 2)
        self.assertEqual(plan.request_stalls, 3)
        self.assertIsNone(plan.model.outbox)


if __name__ == "__main__":
    unittest.main()
