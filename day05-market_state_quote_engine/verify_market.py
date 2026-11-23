"""Generate market workloads, run RTL, and verify every clock against the oracle."""

import argparse
from collections import Counter, deque
from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
from subprocess import run
from typing import Dict, Iterable, List, Optional, Tuple

from market_reference import (
    Command, Combinational, Config, CycleInput, Engine, MarketMode, MUTATIONS, Operation,
    PROFILES, Quote, Reason, Response, Statistic, Status,
)
from market_scenarios import Driver, Scenario, all_scenarios


@dataclass(frozen=True)
class Trace:
    cycle: int
    stimulus: CycleInput
    observed: Combinational

    @classmethod
    def parse(cls, row: str, cfg: Config) -> "Trace":
        try:
            word = [int(value) for value in row.split()]
        except ValueError as exc:
            raise AssertionError(f"unknown trace word in row {row.strip()!r}") from exc
        n_cmd = len(fields(Command))
        n_rsp = len(fields(Response))
        n_quote = len(fields(Quote))
        width = 7 + n_cmd + 1 + n_rsp + 1 + n_quote + 5 + cfg.banks
        if len(word) != width:
            raise AssertionError(f"trace has {len(word)} fields, expected {width}: {row}")
        cycle, reset, tick, inject, ready, rsp_ready, q_ready = word[:7]
        cmd_end = 7 + n_cmd
        cmd = Command(*word[7:cmd_end]) if inject else None
        rsp_end = cmd_end + 1 + n_rsp
        rsp = Response(*word[cmd_end+1:rsp_end]) if word[cmd_end] else None
        q_end = rsp_end + 1 + n_quote
        quote = Quote(*word[rsp_end+1:q_end]) if word[rsp_end] else None
        live, eligible, up, down = [unpack_mask(mask, cfg.banks) for mask in word[q_end:q_end+4]]

        return cls(
            cycle,
            CycleInput(bool(reset), bool(tick), cmd, bool(rsp_ready), bool(q_ready)),
            Combinational(
                bool(ready), rsp, quote, live, eligible, up, down,
                tuple(word[q_end+5:]), bool(word[q_end+4]),
            ),
        )


def unpack_mask(value: int, width: int) -> Tuple[bool, ...]:
    return tuple(bool((value >> bit) & 1) for bit in range(width))


def records(path: Path, cfg: Config) -> Iterable[Trace]:
    with path.open(encoding="utf-8") as data:
        for row in data:
            yield Trace.parse(row, cfg)


def percentiles(samples: List[int]) -> Dict[str, int]:
    if not samples:
        return {}
    samples = sorted(samples)
    last = len(samples) - 1
    return {
        "samples": len(samples),
        "min": samples[0],
        "p50": samples[last // 2],
        "p95": samples[last * 95 // 100],
        "max": samples[-1],
    }


def print_differences(got: Combinational, wanted: Combinational) -> str:
    differences = []
    for field in fields(got):
        actual = getattr(got, field.name)
        expected = getattr(wanted, field.name)
        if actual == expected:
            continue
        if field.name in ("response", "quote") and actual is not None and expected is not None:
            for detail in fields(actual):
                a = getattr(actual, detail.name)
                b = getattr(expected, detail.name)
                if a != b:
                    differences.append(f"{field.name}.{detail.name}={a}, expected {b}")
        else:
            differences.append(f"{field.name}={actual}, expected {expected}")
    return "; ".join(differences)


class Checker:
    def __init__(self, cfg: Config, scenario: Scenario):
        self.cfg = cfg
        self.scenario = scenario
        self.oracle = Engine(cfg)
        self.cycle = -1
        self.reset_last = False
        self.pending_responses: deque = deque()
        self.accepted = 0
        self.held_command: Optional[Command] = None
        self.held_response: Optional[Response] = None
        self.held_quote: Optional[Quote] = None
        self.last_commit = -2
        self.streak = 0
        self.longest_streak = 0
        self.update_streak = 0
        self.longest_update_streak = 0
        self.last_update = -2
        self.last_desired = [None] * cfg.banks
        self.unpublished_key = [None] * cfg.banks
        self.scheduled_at: Optional[int] = None
        self.response_cycles: List[int] = []
        self.response_latency: List[int] = []
        self.quote_latency: List[int] = []
        self.metrics: Counter = Counter()
        self.ops: Counter = Counter()
        self.statuses: Counter = Counter()
        self.reasons: Counter = Counter()
        self.desired_reasons: Counter = Counter()
        self.mode_samples: Counter = Counter()
        self.per_bank: List[Counter] = [Counter() for _ in range(cfg.banks)]

    def error(self, message: str) -> AssertionError:
        return AssertionError(f"{self.scenario.name} cycle {self.cycle}: {message}")

    def require(self, value: bool, message: str) -> None:
        if not value:
            raise self.error(message)

    def check_stimulus(self, trace: Trace) -> None:
        stimulus = trace.stimulus
        knobs = (
            self.scenario.schedule[self.cycle]
            if self.cycle < len(self.scenario.schedule) else Driver()
        )
        self.require(
            (stimulus.reset, stimulus.tick, stimulus.response_ready, stimulus.quote_ready)
            == (knobs.reset, knobs.tick, knobs.response_ready, knobs.quote_ready),
            "testbench did not follow the schedule",
        )
        if stimulus.reset:
            self.require(stimulus.command is None, "testbench asserted input valid during reset")
            return
        if self.held_command is not None:
            self.require(stimulus.command == self.held_command, "test source changed a held command")
        if stimulus.command is not None:
            self.require(self.accepted < len(self.scenario.commands), "extra input command")
            self.require(
                stimulus.command == self.scenario.commands[self.accepted].command,
                "test source did not present the next vector",
            )

    def observe_reset(self) -> None:
        self.metrics["reset_cycles"] += 1
        if not self.reset_last:
            self.metrics["reset_episodes"] += 1
            if self.held_quote is not None:
                self.metrics["reset_dropped_quote_slot"] += 1
            if self.pending_responses:
                self.metrics["reset_dropped_responses"] += len(self.pending_responses)
            self.pending_responses.clear()
            self.last_desired = [None] * self.cfg.banks
            self.unpublished_key = [None] * self.cfg.banks
        self.held_command = None
        self.held_response = None
        self.held_quote = None
        self.scheduled_at = None

    def observe_policy_changes(self) -> None:
        for bank, instrument in enumerate(self.oracle.instruments):
            mode = instrument.mode
            desired = instrument.desired()
            self.mode_samples[mode.name] += 1
            self.desired_reasons[Reason(desired.reason).name] += 1
            prior = self.last_desired[bank]
            if prior is not None and prior.key() != desired.key() and (prior.active or desired.active):
                self.per_bank[bank]["desired_transitions"] += 1
            self.last_desired[bank] = desired
            if desired.active:
                self.metrics["eligible_bank_cycles"] += 1
            if desired.pressure_bid:
                self.metrics["positive_imbalance_cycles"] += 1
            if desired.pressure_ask:
                self.metrics["negative_imbalance_cycles"] += 1

            dispatched = self.oracle.publisher.last[bank]
            pending = (
                desired.key() if (desired.active or dispatched.active) and desired.key() != dispatched.key()
                else None
            )
            previous_pending = self.unpublished_key[bank]
            if previous_pending is not None and pending != previous_pending:
                self.per_bank[bank]["coalesced_dirty_transitions"] += 1
                self.metrics["coalesced_dirty_transitions"] += 1
            self.unpublished_key[bank] = pending

    def observe_source(self, trace: Trace) -> None:
        command = trace.stimulus.command
        if command is None:
            self.held_command = None
            return
        if not trace.observed.source_ready:
            self.metrics["source_stall_cycles"] += 1
            self.held_command = command
            return
        self.held_command = None
        self.ops[operation_name(command.op)] += 1
        self.pending_responses.append((command, self.cycle))
        self.metrics["accepts"] += 1
        self.accepted += 1
        if command.instrument < self.cfg.banks:
            self.per_bank[command.instrument]["accepts"] += 1
            bank = self.oracle.instruments[command.instrument]
            if trace.stimulus.tick and command.op == Operation.HEARTBEAT and (
                bank.mode == MarketMode.LIVE and command.sequence == bank.expected
            ):
                self.metrics["refresh_at_tick"] += 1
            if command.op == Operation.SNAP_BEGIN and bank.mode == MarketMode.FENCED:
                self.metrics["fence_restarts"] += 1

    def observe_response(self, trace: Trace) -> None:
        response = trace.observed.response
        if self.held_response is not None:
            self.require(response == self.held_response, "diagnostic changed under stall")
        self.held_response = None
        if response is None:
            return
        if not trace.stimulus.response_ready:
            self.metrics["response_stall_cycles"] += 1
            self.held_response = response
            return
        self.require(bool(self.pending_responses), "response without an accepted command")
        command, accepted = self.pending_responses.popleft()
        self.require(
            (response.tag, response.op, response.sequence, response.order_id, response.instrument)
            == (command.tag, command.op, command.sequence, command.order_id, command.instrument),
            "response echo does not correspond to the oldest command",
        )
        latency = self.cycle - accepted
        self.require(latency >= 1, "response bypassed its register")
        self.response_latency.append(latency)
        self.response_cycles.append(self.cycle)
        self.streak = self.streak + 1 if self.cycle == self.last_commit + 1 else 1
        self.last_commit = self.cycle
        self.longest_streak = max(self.longest_streak, self.streak)
        self.metrics["responses"] += 1
        self.statuses[Status(response.status).name] += 1
        if response.status == Status.OK and response.op in MUTATIONS:
            self.update_streak = self.update_streak + 1 if self.cycle == self.last_update + 1 else 1
            self.last_update = self.cycle
            self.longest_update_streak = max(self.longest_update_streak, self.update_streak)
        if (
            response.status == Status.OK and response.op == Operation.SNAP_BEGIN
            and response.sequence == self.cfg.sequence_max and response.expected == 0
        ):
            self.metrics["sequence_wrap_begin"] += 1
        if response.status == Status.OK and response.op == Operation.STAT:
            counter = Statistic(command.stat_index)
            self.metrics[f"stat_read_{counter.name.lower()}"] += 1
            if counter == Statistic.TIMEOUTS:
                self.metrics["observed_timeouts"] += response.statistic

    def observe_quote(self, trace: Trace) -> None:
        quote = trace.observed.quote
        if self.held_quote is not None:
            self.require(quote == self.held_quote, "quote changed under stall")
        self.held_quote = None
        if quote is None:
            return
        bank = quote.instrument
        self.require(0 <= bank < self.cfg.banks, "quote from invalid instrument")
        blocked_by_fence = quote.active and (
            not trace.observed.eligible[bank] or quote.epoch != trace.observed.epochs[bank]
        )
        if blocked_by_fence:
            self.metrics["stale_held_quote_cycles"] += 1
        if not trace.observed.quote_current:
            self.metrics["superseded_quote_cycles"] += 1
        if not trace.stimulus.quote_ready:
            self.metrics["quote_stall_cycles"] += 1
            self.held_quote = quote
            return
        self.require(self.scheduled_at is not None, "quote popped without scheduling")
        self.quote_latency.append(self.cycle - self.scheduled_at)
        self.metrics["quotes"] += 1
        self.per_bank[bank]["quotes"] += 1
        reason = Reason(quote.reason)
        self.reasons[reason.name] += 1
        if not trace.observed.quote_current:
            self.metrics["consumer_must_drop"] += 1
        if quote.active:
            self.metrics["quote_active"] += 1
            self.require(quote.bid < quote.ask < self.cfg.levels and quote.size > 0,
                         "active quote has invalid limit price or size")
        else:
            self.metrics["quote_withdrawals"] += 1
            self.require(not (quote.bid or quote.ask or quote.size), "withdrawal carries executable price/size")

    def check_cycle(self, trace: Trace) -> None:
        self.require(trace.cycle == self.cycle + 1, "nonconsecutive trace cycle")
        self.cycle = trace.cycle
        self.check_stimulus(trace)
        if trace.stimulus.reset:
            self.observe_reset()
        else:
            self.observe_response(trace)
            self.observe_quote(trace)
            self.observe_source(trace)
            self.observe_policy_changes()
            if trace.observed.quote is not None and trace.stimulus.quote_ready:
                self.scheduled_at = None
        result = self.oracle.cycle(trace.stimulus)
        if trace.observed != result.pins:
            raise self.error(print_differences(trace.observed, result.pins))
        if result.scheduled is not None:
            self.scheduled_at = self.cycle
            self.unpublished_key[result.scheduled] = None
            self.metrics["scheduled_quotes"] += 1
        self.reset_last = trace.stimulus.reset

    def coverage(self) -> None:
        need = self.scenario.coverage
        routes = {
            "all_ops": all(self.ops[op.name] for op in Operation),
            "all_statuses": set(self.statuses) == {status.name for status in Status},
            "boot_wrap": self.metrics["sequence_wrap_begin"] > 0,
            "crossed": self.desired_reasons[Reason.CROSSED.name] > 0,
            "read_counters": all(self.metrics[f"stat_read_{stat.name.lower()}"] for stat in Statistic),
            "quotes": self.metrics["quote_active"] > 0,
            "response_stalls": self.metrics["response_stall_cycles"] > 0,
            "quote_stalls": self.metrics["quote_stall_cycles"] > 0,
            "source_stalls": self.metrics["source_stall_cycles"] > 0,
            "timeout": self.metrics["observed_timeouts"] > 0,
            "restart_after_fence": self.metrics["fence_restarts"] > 0,
            "refresh_with_tick": self.metrics["refresh_at_tick"] > 0,
            "reset_flush": (
                self.metrics["reset_dropped_responses"] > 0 and (
                    self.cfg.quote_size == 0 or self.metrics["reset_dropped_quote_slot"] > 0
                )
            ),
            "stale_quote_blocked": self.metrics["stale_held_quote_cycles"] > 0,
            "coalesced": self.metrics["coalesced_dirty_transitions"] > 0,
            "line_rate": self.longest_streak == self.accepted and all(
                elapsed == 1 for elapsed in self.response_latency
            ),
            "hot_bank_update_burst": self.longest_update_streak >= 672,
            "unchanged_intent_suppressed": (
                self.metrics["quote_active"] == self.cfg.banks and self.metrics["quote_withdrawals"] == 0
            ),
        }
        bad_coverage = sorted(name for name in need if not routes[name])
        if bad_coverage:
            raise self.error(f"missing required coverage: {', '.join(bad_coverage)}")

    def finish(self) -> Dict:
        self.require(self.accepted == len(self.scenario.commands), "simulation did not accept every command")
        self.require(not self.pending_responses, "simulation ended with unobserved responses")
        self.require(self.oracle.publisher.output is None, "publisher did not drain")
        unfinished = self.oracle.publisher.choose([bank.desired() for bank in self.oracle.instruments])
        self.require(unfinished is None, "publisher failed to converge to latest desired state")
        self.coverage()
        first = self.response_cycles[0]
        last = self.response_cycles[-1]
        return {
            "scenario": self.scenario.name,
            "cycles": self.cycle + 1,
            "metrics": dict(self.metrics),
            "commands_by_op": dict(self.ops),
            "responses_by_status": dict(self.statuses),
            "quote_reasons": dict(self.reasons),
            "desired_reasons": dict(self.desired_reasons),
            "mode_samples": dict(self.mode_samples),
            "per_bank": [dict(values) for values in self.per_bank],
            "response_latency": percentiles(self.response_latency),
            "quote_slot_latency": percentiles(self.quote_latency),
            "longest_response_burst": self.longest_streak,
            "longest_mutation_burst": self.longest_update_streak,
            "responses_per_cycle": len(self.response_cycles) / (last - first + 1),
        }


def operation_name(op: int) -> str:
    try:
        return Operation(op).name
    except ValueError:
        return f"RESERVED_{op}"


def verify(cfg: Config, scenario: Scenario, args: argparse.Namespace) -> Dict:
    base = args.build / scenario.name
    base.mkdir(parents=True, exist_ok=True)
    cmd = base / "commands.txt"
    schedule = base / "schedule.txt"
    trace = base / "signals.txt"
    cmd.write_text("".join(beat.line() + "\n" for beat in scenario.commands), encoding="utf-8")
    schedule.write_text("".join(row.line() + "\n" for row in scenario.schedule), encoding="utf-8")
    run(
        [args.vvp, str(args.sim), f"+commands={cmd}", f"+schedule={schedule}", f"+trace={trace}"],
        check=True,
    )
    checker = Checker(cfg, scenario)
    for sample in records(trace, cfg):
        checker.check_cycle(sample)
    return checker.finish()


def report(data: Dict) -> str:
    lines = [
        f"# Day 05 verification profile {data['profile']}",
        "",
        f"Seed {data['seed']}, random traffic iterations {data['iterations']}.",
        "",
        "| Scenario | Inputs | ACKs | Quotes | Withdrawals | ACK p50/p95 cycles | ACKs/cycle |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in data["scenarios"]:
        metric = row["metrics"]
        p = row["response_latency"]
        lines.append(
            f"| {row['scenario']} | {metric['accepts']} | {metric['responses']} | "
            f"{metric.get('quotes', 0)} | {metric.get('quote_withdrawals', 0)} | "
            f"{p['p50']}/{p['p95']} | {row['responses_per_cycle']:.4f} |"
        )
    return "\n".join(lines)


def run_verification(args: argparse.Namespace) -> None:
    if not 100 <= args.iterations <= 30000:
        raise SystemExit("--iterations must be between 100 and 30000")
    cfg = PROFILES[args.profile]
    selected = set(args.scenario or ())
    results = [
        verify(cfg, scenario, args) for scenario in all_scenarios(cfg, args.iterations, args.seed)
        if not selected or scenario.name in selected
    ]
    if selected != {row["scenario"] for row in results} and selected:
        raise SystemExit("unknown scenario selection")
    result = {
        "profile": args.profile, "seed": args.seed, "iterations": args.iterations,
        "config": asdict(cfg), "scenarios": results,
    }
    args.build.mkdir(parents=True, exist_ok=True)
    (args.build / "metrics.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (args.build / "metrics.md").write_text(report(result) + "\n", encoding="utf-8")
    print(report(result))


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run_cmd = sub.add_parser("run", help="generate, simulate, check and report")
    run_cmd.add_argument("--profile", choices=PROFILES, default="default")
    run_cmd.add_argument("--iterations", type=int, default=5000)
    run_cmd.add_argument("--seed", type=int, default=7201)
    run_cmd.add_argument("--scenario", action="append")
    run_cmd.add_argument("--sim", type=Path, required=True)
    run_cmd.add_argument("--vvp", default="vvp")
    run_cmd.add_argument("--build", type=Path, required=True)
    params = sub.add_parser("params", help="emit Icarus bench profile overrides")
    params.add_argument("profile", choices=PROFILES)
    summary = sub.add_parser("report", help="print JSON results as a Markdown table")
    summary.add_argument("metrics", type=Path)
    return parser.parse_args()


def main() -> None:
    args = arguments()
    if args.action == "run":
        run_verification(args)
    elif args.action == "params":
        values = PROFILES[args.profile].rtl_parameters()
        print(" ".join(f"-P tb_market_state_quote_engine.{name}={value}" for name, value in values.items()))
    else:
        print(report(json.loads(args.metrics.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    main()
