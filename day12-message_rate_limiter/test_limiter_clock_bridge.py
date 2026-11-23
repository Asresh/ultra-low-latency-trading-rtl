"""Check safe timestamp extension against an unwrapped independent reference."""

import json
import random
import tempfile
import unittest
from pathlib import Path

from limiter_clock_bridge import (
    AbsoluteOffer, compile_absolute, policies_from_records, read_absolute, read_policies,
)
from limiter_model import Limiter
from limiter_protocol import Geometry, Operation, PROFILES, Status
from limiter_tuning import Demand, Policy, evaluate
from limiter_workload import compile_program


class ClockBridgeTest(unittest.TestCase):
    """Check the minimum number of safe refills for a short timestamp port."""

    def setUp(self):
        """Make several wraps cheap to reach even in a small fixture."""
        self.geometry = Geometry(keys=3, tick_w=4, token_w=6, count_w=10, tag_w=4)
        self.policies = {0: Policy(10, 2), 1: Policy(7, 0), 2: Policy(6, 1)}

    def test_single_key_refresh_is_inserted_exactly_at_ambiguity(self):
        """Tick delta seven is legal while eight needs an intermediate anchor."""
        geometry = self.geometry
        policy = {0: self.policies[0]}
        direct = compile_absolute(geometry, [AbsoluteOffer(7, 0, 1)], policy)
        self.assertEqual(direct.inspections, 0)
        self.assertEqual([row.arrival.command.op for row in direct.entries], [
            Operation.CONFIG, Operation.OFFER,
        ])
        adjusted = compile_absolute(geometry, [AbsoluteOffer(8, 0, 1)], policy)
        self.assertEqual(adjusted.inspections, 1)
        self.assertEqual([row.absolute_tick for row in adjusted.entries], [0, 7, 8])
        self.assertEqual([row.source_row for row in adjusted.entries], [-1, 0, 0])
        self.assertEqual(adjusted.entries[-1].arrival.command.tick, 8)

    def test_exact_multiples_of_half_range_use_last_offer_as_anchor(self):
        """The offer itself covers the final segment when the gap is a multiple."""
        for distance in (0, 7, 14, 21, 28, 35, 36):
            plan = compile_absolute(
                self.geometry, (AbsoluteOffer(distance, 0, 1),), {0: self.policies[0]}
            )
            expected = max(0, (distance + 6) // 7 - 1)
            self.assertEqual(plan.inspections, expected)
            self.assertEqual(plan.entries[-1].absolute_tick, distance)
            self.assertEqual(plan.entries[-1].arrival.command.tick, distance & 15)

    def test_inactive_keys_receive_no_helpers_until_needed(self):
        """Avoid side effects on unrelated instrument time anchors."""
        plan = compile_absolute(
            self.geometry, [AbsoluteOffer(18, 0, 2), AbsoluteOffer(21, 0, 2)], self.policies
        )
        config = [item for item in plan.entries if item.purpose == "configure"]
        changes = [item for item in plan.entries if item.purpose == "refresh"]
        self.assertEqual([item.arrival.command.key for item in config], [0, 1, 2])
        self.assertEqual([item.absolute_tick for item in changes], [7, 14])
        self.assertEqual({item.arrival.command.key for item in changes}, {0})
        self.assertEqual([item.source_row for item in changes], [0, 0])

    def test_interleaved_keys_do_not_need_globally_ordered_generated_ticks(self):
        """Housekeeping uses per-key anchors while original event order is preserved."""
        demanded = (AbsoluteOffer(10, 0, 5), AbsoluteOffer(12, 1, 1), AbsoluteOffer(14, 0, 6))
        plan = compile_absolute(self.geometry, demanded, self.policies)
        recorded_offers = [item for item in plan.entries if item.purpose == "offer"]
        self.assertEqual([item.source_row for item in recorded_offers], [0, 1, 2])
        self.assertEqual(recorded_offers[1].absolute_tick, 12)
        refreshes = [item for item in plan.entries if item.purpose == "refresh"]
        self.assertEqual([item.absolute_tick for item in refreshes], [7, 7])
        self.assertEqual([item.arrival.command.key for item in refreshes], [0, 1])
        self.assertEqual([item.arrival.cycle for item in plan.entries], list(range(len(plan.entries))))
        scheduled = compile_program(self.geometry, plan.arrivals, [])
        self.assertEqual(scheduled.submitted, len(plan.entries))
        self.assertEqual(scheduled.accepted, len(plan.entries))

    def test_result_agrees_with_absolute_demand_even_with_missed_wraps(self):
        """Reconstruct passes and charged tokens across many independent tick wraps."""
        for seed in range(10):
            rng = random.Random(seed)
            demands = []
            now = 27
            for _ in range(120):
                now += rng.randrange(45)
                demands.append(AbsoluteOffer(now, rng.randrange(3), 1 + rng.randrange(11)))
            plan = compile_absolute(self.geometry, demands, self.policies, start_tick=20)
            model = Limiter(self.geometry)
            by_key = {key: [] for key in self.policies}
            for record in plan.entries:
                response = model.decide(record.arrival.command)
                if record.purpose in ("refresh", "configure"):
                    self.assertEqual(response.status, Status.OK)
                else:
                    self.assertIn(response.status, (Status.PASS, Status.LIMITED))
                    by_key[response.key].append((record, response.status))
            self.assertGreater(plan.inspections, 100)
            self.assertTrue(any(row.arrival.command.tag == 0 for row in plan.entries[1:]))
            for key, config in self.policies.items():
                expected = evaluate(
                    config, [Demand(offer.tick - 20, offer.cost) for offer in demands if offer.key == key],
                )
                delivered = by_key[key]
                self.assertEqual(len(delivered), expected.offered)
                accepted = [record for record, status in delivered if status == Status.PASS]
                self.assertEqual(len(accepted), expected.passed)
                self.assertEqual(sum(record.arrival.command.amount for record in accepted), expected.charged)

    def test_large_age_is_rejected_before_it_can_expand_into_unbounded_output(self):
        """An inappropriate capture can never create millions of housekeeping commands."""
        with self.assertRaisesRegex(ValueError, "refreshes exceed 3"):
            compile_absolute(
                self.geometry,
                [AbsoluteOffer(1_000_000_000, 0, 1)],
                self.policies,
                max_inspections=3,
            )

    def test_missing_policy_or_time_domain_is_reported_with_source_row(self):
        """Fail rather than wrapping an impossible event or attaching the wrong key."""
        cases = (
            ([AbsoluteOffer(0, 3, 1)], self.policies, {}, "missing policy"),
            ([AbsoluteOffer(5, 0, 64)], self.policies, {}, "cost exceeds"),
            ([AbsoluteOffer(1, 0, 1)], self.policies, {"start_tick": 2}, "backwards"),
            ([AbsoluteOffer(4, 1, 1), AbsoluteOffer(3, 0, 1)], self.policies, {}, "backwards at row 1"),
            ([], {3: Policy(1, 0)}, {}, "not implemented"),
            ([], {0: Policy(64, 0)}, {}, "unsupported"),
            ([], self.policies, {"start_tick": -1}, "start tick"),
            ([], self.policies, {"max_inspections": -1}, "maximum inspections"),
        )
        for demands, policies, constraints, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                with self.assertRaisesRegex(ValueError, diagnostic):
                    compile_absolute(self.geometry, demands, policies, **constraints)

    def test_empty_plan_leaves_empty_source_configuration(self):
        """An empty policy and traffic input produces no synthetic clock work."""
        self.assertEqual(compile_absolute(self.geometry, [], {}).to_record(), {
            "inspections": 0, "offers": 0, "rows": [],
        })


class ClockBridgeFilesTest(unittest.TestCase):
    """Read strict adapters before any generated program reaches the simulator."""

    def test_json_policy_and_unsigned_absolute_rows(self):
        """Read the declared run metadata with precise field spelling and location."""
        geometry = PROFILES["small"]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            absolute = folder / "absolute.csv"
            configs = folder / "keys.json"
            absolute.write_text("tick,key,cost\n3,0,1\n82,1,4\n", encoding="utf-8")
            configs.write_text(json.dumps([
                {"key": 0, "capacity": 5, "rate": 1},
                {"key": 1, "capacity": 6, "rate": 0},
            ]), encoding="utf-8")
            traffic = read_absolute(absolute)
            policies = read_policies(configs, geometry)
            self.assertEqual(traffic, (AbsoluteOffer(3, 0, 1), AbsoluteOffer(82, 1, 4)))
            self.assertEqual(policies, {0: Policy(5, 1), 1: Policy(6, 0)})
            absolute.write_text("tick,key,cost\n0,2,1\n1,2,-4\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "absolute.csv:3: cost"):
                read_absolute(absolute)

    def test_policy_fields_and_keys_are_validated_before_scheduling(self):
        """No implicit key assignment, aliases, or oversized capacity are accepted."""
        geo = PROFILES["small"]
        valid = {"key": 0, "capacity": 3, "rate": 1}
        rejected = (
            (valid, "array"),
            ([{"key": 0, "capacity": 1}], "needs key"),
            ([valid, valid], "duplicate"),
            ([{**valid, "capacity": 64}], "token port"),
            ([{**valid, "key": True}], "invalid key"),
            ([{**valid, "key": 3}], "invalid key"),
            ([{**valid, "rate": 4}], "between zero and capacity"),
        )
        for configs, diagnostic in rejected:
            with self.assertRaisesRegex(ValueError, diagnostic):
                policies_from_records(configs, geo)


if __name__ == "__main__":
    unittest.main()
