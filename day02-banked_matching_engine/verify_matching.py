"""Differential trace checker, coverage collection and timing report."""

import argparse
from collections import Counter, deque
from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
from subprocess import run
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from matching_model import Command, Event, Kind, Opcode, PROFILES, Profile, Reference, Reject
from matching_workload import Scenario, scenarios


@dataclass(frozen=True)
class Sample:
    cycle: int
    reset: bool
    source_valid: bool
    source_ready: bool
    command: Command
    sink_valid: bool
    sink_ready: bool
    event: Event
    available: int
    grant: int
    starts: int
    commits: int
    command_used: Tuple[int, ...]
    event_used: Tuple[int, ...]

    @classmethod
    # Decode one trace row and unpack its masks, occupancies, command, and event using profile widths.
    def parse(cls, line: str, profile: Profile) -> "Sample":
        try:
            words = tuple(map(int, line.split()))
        except ValueError as exc:
            raise AssertionError(f"trace contains an unknown value: {line.strip()}") from exc
        expected_width = 34 + profile.banks * 2
        if len(words) != expected_width:
            raise AssertionError(f"trace width {len(words)} expected {expected_width}")

        command_start = 4
        command_stop = command_start + len(fields(Command))
        event_start = command_stop + 2
        event_stop = event_start + len(fields(Event))
        tail = event_stop + 4
        return cls(
            cycle=words[0],
            reset=bool(words[1]),
            source_valid=bool(words[2]),
            source_ready=bool(words[3]),
            command=Command(*words[command_start:command_stop]),
            sink_valid=bool(words[command_stop]),
            sink_ready=bool(words[command_stop+1]),
            event=Event(*words[event_start:event_stop]),
            available=words[event_stop],
            grant=words[event_stop+1],
            starts=words[event_stop+2],
            commits=words[event_stop+3],
            command_used=words[tail:tail+profile.banks],
            event_used=words[tail+profile.banks:],
        )

    @property
    # Indicate that this sampled edge transferred an input command.
    def source_fire(self) -> bool:
        return self.source_valid and self.source_ready

    @property
    # Indicate that this sampled edge transferred an output event.
    def sink_fire(self) -> bool:
        return self.sink_valid and self.sink_ready


# Extract one lane's bit from a packed mask.
def bit_at(mask: int, index: int) -> int:
    return (mask >> index) & 1


# Read trace rows in order and convert them to typed cycle samples.
def sample_trace(path: Path, profile: Profile) -> Iterable[Sample]:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            yield Sample.parse(line, profile)


# Describe precisely which fields differ between hardware and reference events.
def differing_event_fields(got: Event, expected: Event) -> str:
    return ", ".join(
        f"{f.name}={getattr(got, f.name)} (expected {getattr(expected, f.name)})"
        for f in fields(Event)
        if getattr(got, f.name) != getattr(expected, f.name)
    )


# Summarize a latency sample with count and percentile measurements.
def describe_samples(values: List[int]) -> Dict[str, int]:
    if not values:
        return {}
    ordered = sorted(values)
    last = len(ordered) - 1
    return {
        "count": len(values),
        "min": ordered[0],
        "p50": ordered[last * 50 // 100],
        "p95": ordered[last * 95 // 100],
        "max": ordered[-1],
    }


class TraceChecker:
    # Initialize the reference model, expected lane queues, and independent coverage counters.
    def __init__(self, profile: Profile, scenario: Scenario):
        self.profile = profile
        self.scenario = scenario
        self.lanes = profile.banks + 1
        self.reference = Reference(profile)
        self.pending: List[Deque[Tuple[Event, int]]] = [deque() for _ in range(self.lanes)]
        self.expected_commands = [0] * profile.banks
        self.expected_events = [0] * self.lanes
        self.command_watermark = [0] * profile.banks
        self.event_watermark = [0] * self.lanes
        self.round_robin = 0
        self.held_lane: Optional[int] = None
        self.held_command: Optional[Command] = None
        self.held_event: Optional[Event] = None
        self.counters: Counter = Counter()
        self.kinds: Counter = Counter()
        self.rejects: Counter = Counter()
        self.commands_by_lane: Counter = Counter()
        self.events_by_lane: Counter = Counter()
        self.opcodes: Counter = Counter()
        self.tifs: Counter = Counter()
        self.fills_by_command: Counter = Counter()
        self.accept_cycles: List[int] = []
        self.emission_cycles: List[int] = []
        self.all_latency: List[int] = []
        self.terminal_latency: List[int] = []
        self.first_cycle_latency: Optional[int] = None
        self.max_wait = 0
        self.lane_wait = [0] * self.lanes
        self.prev_event_seq: List[Optional[int]] = [None] * self.lanes
        self.prev_cmd_seq: List[Optional[int]] = [None] * self.lanes
        self.in_reset = False
        self.cycle = -1

    # Raise a diagnostic tied to the scenario and cycle, formatting expensive details only on failure.
    def require(self, condition: bool, failure) -> None:
        if not condition:
            message = failure() if callable(failure) else failure
            raise AssertionError(f"{self.scenario.name} cycle {self.cycle}: {message}")

    # Check the reset interface and clear all reference state and in-flight tracking.
    def on_reset(self, sample: Sample) -> None:
        self.require(not sample.source_ready and not sample.sink_valid,
                     "interface allowed transfers during reset")
        self.require(not (sample.available or sample.grant or sample.starts or sample.commits),
                     "an internal lane transferred during reset")
        self.counters["reset_cycles"] += 1
        if not self.in_reset:
            self.counters["reset_epochs"] += 1
            self.counters["reset_flushed_events"] += sum(map(len, self.pending))
            self.counters["reset_flushed_ingress"] += sum(sample.command_used)
            self.counters["reset_flushed_egress"] += sum(sample.event_used)
            self.counters["reset_while_output_stalled"] += int(self.held_event is not None)
        self.in_reset = True
        self.reference.reset()
        for queue in self.pending:
            queue.clear()
        self.expected_commands = [0] * self.profile.banks
        self.expected_events = [0] * self.lanes
        self.round_robin = 0
        self.held_lane = None
        self.held_command = None
        self.held_event = None
        self.lane_wait = [0] * self.lanes
        self.prev_event_seq = [None] * self.lanes
        self.prev_cmd_seq = [None] * self.lanes

    # Check readiness from configured FIFO capacity and require stable stalled payloads.
    def check_flow_control(self, sample: Sample, source_lane: int) -> None:
        self.require(sample.command_used == tuple(self.expected_commands),
                     lambda: f"command FIFO levels {sample.command_used} != {self.expected_commands}")
        self.require(sample.event_used == tuple(self.expected_events),
                     lambda: f"event FIFO levels {sample.event_used} != {self.expected_events}")
        actual_ready = (
            sample.command_used[source_lane] < self.profile.command_depth
            if source_lane < self.profile.banks
            else sample.event_used[source_lane] < self.profile.event_depth
        )
        self.require(sample.source_ready == actual_ready, "source was blocked by the wrong queue")
        available = sum((used > 0) << lane for lane, used in enumerate(sample.event_used))
        self.require(available == sample.available, "egress lane-valid bits disagree with occupied queues")
        if self.held_command is not None:
            self.require(sample.source_valid and sample.command == self.held_command,
                         "test source changed a stalled command")
        self.held_command = (
            sample.command if sample.source_valid and not sample.source_ready else None
        )
        if self.held_event is not None:
            self.require(sample.sink_valid, "stalled event lost valid")
            self.require(sample.event == self.held_event, "stalled event payload changed")
        self.held_event = sample.event if sample.sink_valid and not sample.sink_ready else None
        if sample.source_valid and not sample.source_ready:
            self.counters["source_stalls"] += 1
        if sample.sink_valid and not sample.sink_ready:
            self.counters["sink_stalls"] += 1

    # Recompute the round-robin grant, lock selection under stalls, and bound lane wait.
    def check_arbiter(self, sample: Sample) -> Optional[int]:
        selected = self.held_lane
        if selected is None:
            rank = ((self.round_robin + offset) % self.lanes for offset in range(self.lanes))
            selected = next((lane for lane in rank if bit_at(sample.available, lane)), None)
        self.require(sample.sink_valid == (selected is not None), "arbiter dropped or invented a valid lane")
        if selected is None:
            self.require(sample.grant == 0, "idle arbiter popped an event")
            return None
        actual = self.reference.lane_for(sample.event.instrument)
        self.require(actual == selected,
                     lambda: f"round robin selected lane {actual}, expected {selected}")
        self.require(sample.grant == ((1 << selected) if sample.sink_ready else 0),
                     "arbiter granted wrong lane or popped while stalled")
        if sample.sink_ready:
            self.held_lane = None
            self.round_robin = (selected + 1) % self.lanes
        else:
            self.held_lane = selected
        for lane in range(self.lanes):
            if not bit_at(sample.available, lane) or (sample.sink_fire and lane == selected):
                self.lane_wait[lane] = 0
            elif sample.sink_fire:
                self.lane_wait[lane] += 1
                self.max_wait = max(self.max_wait, self.lane_wait[lane])
                self.require(self.lane_wait[lane] < self.lanes,
                             lambda: f"continuously eligible lane {lane} exceeded fairness bound")
        return selected

    # Confirm the accepted stimulus and queue reference events for the target lane.
    def handle_input(self, sample: Sample, lane: int) -> None:
        source_index = len(self.accept_cycles)
        self.require(source_index < len(self.scenario.beats), "RTL accepted an extra command")
        expected = self.scenario.beats[source_index].command
        self.require(sample.command == expected,
                     lambda: f"source order {sample.command} differs from vector {expected}")
        self.pending[lane].extend(
            (event, sample.cycle) for event in self.reference.execute(sample.command)
        )
        self.accept_cycles.append(sample.cycle)
        self.commands_by_lane[lane] += 1
        self.counters["accepted"] += 1
        if lane < self.profile.banks:
            opcode = Opcode(sample.command.opcode)
            self.opcodes[opcode.name] += 1
            if opcode == Opcode.NEW:
                tif = sample.command.tif
                self.tifs[tif] += 1
        else:
            self.counters["invalid_instruments"] += 1

    # Compare the head event and record delivery latency and sequence-wrap metrics on transfer.
    def handle_output(self, sample: Sample, lane: int) -> None:
        queue = self.pending[lane]
        self.require(bool(queue), lambda: f"lane {lane} emitted unexpected {sample.event}")
        expected, accepted_at = queue[0]
        actual = sample.event
        self.require(actual == expected, lambda: differing_event_fields(actual, expected))
        if not sample.sink_fire:
            return
        queue.popleft()
        latency = sample.cycle - accepted_at
        earliest = 1 if lane == self.profile.banks else 3
        self.require(latency >= earliest, lambda: f"lane {lane} event bypassed a stored queue")
        self.all_latency.append(latency)
        if self.first_cycle_latency is None:
            self.first_cycle_latency = latency
        if actual.last:
            self.terminal_latency.append(latency)
        self.emission_cycles.append(sample.cycle)
        self.events_by_lane[lane] += 1
        self.counters["emitted"] += 1
        self.kinds[Kind(actual.kind).name] += 1
        if actual.reject:
            self.rejects[Reject(actual.reject).name] += 1
        if actual.kind == Kind.FILL:
            self.fills_by_command[actual.tag] += 1
        if self.prev_event_seq[lane] == self.profile.count_mask and actual.event_sequence == 0:
            self.counters["event_wraps"] += 1
        if self.prev_cmd_seq[lane] == self.profile.count_mask and actual.command_sequence == 0:
            self.counters["command_wraps"] += 1
        self.prev_event_seq[lane] = actual.event_sequence
        self.prev_cmd_seq[lane] = actual.command_sequence

    # Calculate the next ingress and egress FIFO occupancy from observed lane handshakes.
    def update_occupancy(self, sample: Sample, source_lane: int) -> None:
        for lane, occupied in enumerate(sample.command_used):
            self.command_watermark[lane] = max(self.command_watermark[lane], occupied)
            new = occupied + int(sample.source_fire and source_lane == lane) - bit_at(sample.starts, lane)
            self.require(0 <= new <= self.profile.command_depth,
                         lambda: f"invalid ingress traffic count for lane {lane}")
            self.expected_commands[lane] = new
        for lane, occupied in enumerate(sample.event_used):
            self.event_watermark[lane] = max(self.event_watermark[lane], occupied)
            push = (
                int(sample.source_fire and source_lane == lane)
                if lane == self.profile.banks else bit_at(sample.commits, lane)
            )
            new = occupied + push - bit_at(sample.grant, lane)
            self.require(0 <= new <= self.profile.event_depth,
                         lambda: f"invalid egress traffic count for lane {lane}")
            self.require(not push or occupied < self.profile.event_depth,
                         lambda: f"lane {lane} committed into a full event queue")
            self.expected_events[lane] = new

    # Replay one sampled edge in protocol order, comparing output before applying new input.
    def verify_cycle(self, sample: Sample) -> None:
        self.require(sample.cycle == self.cycle + 1, "missing or repeated trace lines")
        self.cycle = sample.cycle
        if sample.reset:
            self.on_reset(sample)
            return
        self.in_reset = False
        source_lane = self.reference.lane_for(sample.command.instrument)
        self.check_flow_control(sample, source_lane)

        # The oldest lane event is already visible before today's input edge.
        lane = self.check_arbiter(sample)
        if lane is not None:
            self.handle_output(sample, lane)
        if sample.source_fire:
            self.handle_input(sample, source_lane)

        if sample.commits and sample.commits & (sample.commits - 1):
            self.counters["parallel_commit_cycles"] += 1
        if sample.sink_valid and not sample.sink_ready and lane is not None:
            other = sample.commits & ~(1 << lane)
            self.counters["other_bank_progress_during_stall"] += int(bool(other))
        self.update_occupancy(sample, source_lane)

    # Require any expected traffic to drain, check scenario coverage, and summarize counters and latency.
    def finish(self) -> Dict:
        self.require(len(self.accept_cycles) == len(self.scenario.beats),
                     "simulation ended before all source commands were accepted")
        self.require(not any(self.pending), lambda: f"missing events in {[len(q) for q in self.pending]}")
        self.require(not any(self.expected_commands + self.expected_events),
                     "simulation ended before all queues drained")
        if self.scenario.name == "stress":
            self.require(set(self.kinds) == {kind.name for kind in Kind}, "missed an event kind")
            self.require(set(self.rejects) == {reason.name for reason in Reject if reason != Reject.NONE},
                         "missed a documented rejection reason")
            self.require(self.counters["source_stalls"] > 0 and self.counters["sink_stalls"] > 0,
                         "stress never exercised both interfaces under backpressure")
            if self.profile.banks > 1:
                self.require(self.counters["other_bank_progress_during_stall"] > 0,
                             "test did not show lane progress while a different lane was stalled")
                self.require(self.counters["parallel_commit_cycles"] > 0,
                             "test never committed two bank events in parallel")
        elif self.scenario.name == "reset":
            self.require(self.counters["reset_while_output_stalled"] >= 1,
                         "test did not reset a stalled output")
            self.require(self.counters["reset_flushed_events"] > 0, "reset did not flush pending events")
        elif self.scenario.name.startswith("benchmark_"):
            self.require(self.first_cycle_latency == 3, "empty-engine latency must be three cycles")
            self.require(not self.counters["sink_stalls"], "benchmark used sink backpressure")
            self.require(self.counters["emitted"] == self.counters["accepted"],
                         "benchmark expected one result per query")
        interval = self.emission_cycles[-1] - self.emission_cycles[0] + 1
        return {
            "name": self.scenario.name,
            "cycles": self.cycle + 1,
            "counters": dict(self.counters),
            "kinds": dict(self.kinds),
            "rejects": dict(self.rejects),
            "commands_by_lane": dict(self.commands_by_lane),
            "events_by_lane": dict(self.events_by_lane),
            "opcodes": dict(self.opcodes),
            "new_tifs": dict(self.tifs),
            "maximum_fills_for_one_command": max(self.fills_by_command.values(), default=0),
            "command_high_watermark": self.command_watermark,
            "event_high_watermark": self.event_watermark,
            "max_eligible_bypass_count": self.max_wait,
            "first_output_latency_cycles": self.first_cycle_latency,
            "event_latency_cycles": describe_samples(self.all_latency),
            "terminal_latency_cycles": describe_samples(self.terminal_latency),
            "cycles_first_to_last_output": interval,
            "events_per_cycle_in_output_window": len(self.emission_cycles) / interval,
        }


# Write one scenario, run the simulator, then replay its measured trace through the scoreboard.
def verify_scenario(
    scenario: Scenario, profile: Profile, simulator: Path, vvp: str, build: Path,
    waveform: Optional[Path] = None,
) -> Dict:
    name = scenario.name
    commands_path = build / f"{name}.commands.txt"
    schedule_path = build / f"{name}.schedule.txt"
    trace_path = build / f"{name}.trace.txt"
    commands_path.write_text("\n".join(beat.line() for beat in scenario.beats) + "\n", encoding="utf-8")
    schedule_path.write_text(
        "".join(f"{int(reset)} {int(ready)}\n" for reset, ready in scenario.schedule),
        encoding="utf-8",
    )
    run(
        [vvp, str(simulator), f"+commands={commands_path}",
         f"+schedule={schedule_path}", f"+trace={trace_path}",
         *([f"+waveform={waveform}"] if waveform is not None else [])],
        check=True,
    )
    checker = TraceChecker(profile, scenario)
    for sample in sample_trace(trace_path, profile):
        checker.verify_cycle(sample)
    return checker.finish()


# Format ordered scenario measurements as a concise Markdown report.
def metrics_table(report: Dict) -> str:
    lines = [
        f"# Matching engine measurements for {report['profile']}",
        "",
        f"Seed {report['seed']}; generated random commands {report['random_commands']}.",
        "",
        "| Scenario | Accepts | Events | Source stalls | Sink stalls | Latency p50/p95 (cycles) | Events/cycle |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for metric in report["scenarios"]:
        count = metric["counters"]
        latency = metric["event_latency_cycles"]
        lines.append(
            f"| {metric['name']} | {count['accepted']} | {count['emitted']} | "
            f"{count.get('source_stalls', 0)} | {count.get('sink_stalls', 0)} | "
            f"{latency['p50']}/{latency['p95']} | {metric['events_per_cycle_in_output_window']:.4f} |"
        )
    stress = report["scenarios"][0]
    counts = stress["counters"]
    lines += [
        "",
        f"- Parallel commit cycles: {counts.get('parallel_commit_cycles', 0)}.",
        f"- Other-bank commit cycles under output stall: {counts.get('other_bank_progress_during_stall', 0)}.",
        f"- Largest sweep: {stress['maximum_fills_for_one_command']} fills.",
        f"- Command FIFO high water marks: {stress['command_high_watermark']}.",
        f"- Egress FIFO high water marks: {stress['event_high_watermark']} (last is reject lane).",
        f"- Maximum consecutive transfers bypassing an eligible lane: {stress['max_eligible_bypass_count']}.",
        f"- Sequence counter wraps: {counts.get('command_wraps', 0)} command and {counts.get('event_wraps', 0)} event.",
    ]
    return "\n".join(lines)


# Run all requested scenarios for a profile and save structured and human-readable results.
def run_report(args: argparse.Namespace) -> None:
    if not 0 <= args.commands <= 25000:
        raise SystemExit("--commands must be between 0 and 25000")
    build = args.build.resolve()
    build.mkdir(parents=True, exist_ok=True)
    profile = PROFILES[args.profile]
    results = [
        verify_scenario(scenario, profile, args.sim.resolve(), args.vvp, build,
                        args.waveform if scenario.name == "stress" else None)
        for scenario in scenarios(profile, args.commands, args.seed)
    ]
    report = {
        "profile": args.profile,
        "dimensions": asdict(profile),
        "seed": args.seed,
        "random_commands": args.commands,
        "scenarios": results,
    }
    (build / "metrics.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (build / "metrics.md").write_text(metrics_table(report) + "\n", encoding="utf-8")
    print(metrics_table(report))


# Define the CLI for profile parameters and for test execution and reporting.
def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    verify = commands.add_parser("run", help="generate vectors, run simulation, and check the trace")
    verify.add_argument("--profile", choices=tuple(PROFILES), required=True)
    verify.add_argument("--seed", type=int, required=True)
    verify.add_argument("--commands", type=int, required=True)
    verify.add_argument("--vvp", default="vvp")
    verify.add_argument("--sim", type=Path, required=True)
    verify.add_argument("--build", type=Path, required=True)
    verify.add_argument("--waveform", type=Path, help="optional VCD for the checked stress tape")
    report = commands.add_parser("report", help="print Markdown results from a previous run")
    report.add_argument("metrics", type=Path)
    return parser.parse_args()


# Dispatch the requested verification mode.
def main() -> None:
    args = arguments()
    if args.operation == "run":
        run_report(args)
    else:
        print(metrics_table(json.loads(args.metrics.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    main()
