"""Generate, replay, and audit reserve matcher transactions."""

import argparse
from collections import Counter, deque
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess

from reserve_model import Command, Event, Kind, PROFILES, Reject, ReserveMatcher, Result, unpack
from reserve_workload import campaign


@dataclass(frozen=True)
class PinSample:
    """One camera frame of both interfaces immediately before a rising edge."""

    cycle: int
    reset: bool
    input_valid: bool
    input_ready: bool
    output_valid: bool
    output_ready: bool
    input: object
    output: object
    lanes: int


def parse_bit(text):
    """Reject high-impedance or uninitialized control samples."""
    if text not in {"0", "1"}:
        raise ValueError(f"unknown control {text!r}")
    return text == "1"


def parse_trace(path, geometry):
    """Validate the on-disk format and decode complete accepted or held offers."""
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            parts = line.split()
            try:
                if len(parts) != 16:
                    raise ValueError("expected 16 columns")
                cycle = int(parts[0])
                if cycle != number - 1:
                    raise ValueError(f"sample {cycle} where {number - 1} was expected")
                reset, i_v, i_r, o_v, o_r = map(parse_bit, parts[1:6])
                command = (
                    Command.decode(geometry, *(int(word, 16) for word in parts[6:9]))
                    if i_v else None
                )
                event = None
                if o_v:
                    header = [int(word, 16) for word in parts[9:14]]
                    payload = int(parts[14], 16)
                    event = Event(*header, Result(**unpack(payload, geometry.result_layout())))
                yield PinSample(cycle, reset, i_v, i_r, o_v, o_r, command, event, int(parts[15], 16))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc


class CheckFailure(ValueError):
    """A protocol or transaction contradiction with a cycle annotation."""


class Scoreboard:
    """Check complete output offers and pending work in bank-local order."""

    def __init__(self, geometry):
        """Create one expected queue per bank plus the reject lane."""
        self.geometry = geometry
        self.reference = ReserveMatcher(geometry)
        self.expected = [deque() for _ in range(geometry.banks + 1)]
        self.kinds = Counter()
        self.reasons = Counter()
        self.coverage = Counter()
        self.sink_held = None
        self.source_held = None
        self.round_robin_start = 0
        self.pinned_lane = None
        self.command_latencies = Counter()
        self.max_backlog = 0

    @staticmethod
    def _require(pred, sample, problem):
        """Include the first discrepant clock in every error."""
        if not pred:
            raise CheckFailure(f"cycle {sample.cycle}: {problem}")

    def _lane(self, bank):
        """Map invalid bank encodings to the single reject lane."""
        return min(bank, self.geometry.banks)

    def _reset(self, sample):
        """Flush all expected transactions and restore fairness state."""
        self._require(not sample.input_ready and not sample.output_valid, sample,
                      "reset did not suppress interface handshakes")
        self.coverage["reset_edges"] += 1
        self.coverage["flushed_results"] += sum(len(queue) for queue in self.expected)
        self.reference.reset()
        for queue in self.expected:
            queue.clear()
        self.sink_held = None
        self.source_held = None
        self.round_robin_start = 0
        self.pinned_lane = None

    def _grant(self, sample):
        """Select the current held lane, or the next round-robin competitor."""
        if self.pinned_lane is not None:
            self._require(sample.lanes & (1 << self.pinned_lane), sample, "held lane disappeared")
            return self.pinned_lane
        ports = len(self.expected)
        for offset in range(ports):
            lane = (self.round_robin_start + offset) % ports
            if sample.lanes & (1 << lane):
                return lane
        return None

    def _egress(self, sample):
        """Validate a presented payload before retiring or pinning the lane."""
        grant = self._grant(sample)
        self._require(sample.output_valid == (grant is not None), sample, "bad output availability")
        if self.sink_held is not None:
            self._require(sample.output == self.sink_held, sample, "stalled output changed")
        if grant is None:
            return
        event = sample.output
        self._require(self._lane(event.bank) == grant, sample,
                      f"arbiter granted bank {event.bank}, expected lane {grant}")
        queue = self.expected[grant]
        self._require(queue, sample, f"unsolicited output for lane {grant}")
        expected_event, accepted_at = queue[0]
        self._require(expected_event == event, sample,
                      f"expected {expected_event}, received {event}")
        if not sample.output_ready:
            self.sink_held = event
            self.pinned_lane = grant
            self.coverage["sink_stalls"] += 1
            return
        self.sink_held = None
        self.pinned_lane = None
        self.round_robin_start = (grant + 1) % len(self.expected)
        queue.popleft()
        self.kinds[Kind(event.result.kind).name] += 1
        if event.result.reason:
            self.reasons[Reject(event.result.reason).name] += 1
        self.coverage["accepted_results"] += 1
        if event.result.refreshed:
            self.coverage["reserve_refreshes"] += 1
        if event.event_sequence == self.geometry.sequence_mask:
            self.coverage["event_wrap_boundaries"] += 1
        if event.result.last:
            self.command_latencies[sample.cycle - accepted_at] += 1

    def _ingress(self, sample):
        """Track source stability and produce expected events for accepted requests."""
        if self.source_held is not None:
            self._require(sample.input == self.source_held, sample, "stalled input changed")
        if sample.input_valid and not sample.input_ready:
            self.source_held = sample.input
            self.coverage["source_stalls"] += 1
            return
        self.source_held = None
        if not sample.input_valid:
            return
        command = sample.input
        lane = self._lane(command.bank)
        for event in self.reference.execute(command):
            self.expected[lane].append((event, sample.cycle))
        self.coverage["accepted_inputs"] += 1
        self.max_backlog = max(self.max_backlog, sum(len(queue) for queue in self.expected))

    def step(self, sample):
        """Observe transfers in pre-edge order, retiring output before admitting input."""
        self.coverage["cycles"] += 1
        if sample.reset:
            self._reset(sample)
            return
        self._egress(sample)
        self._ingress(sample)

    def summary(self):
        """Require a clean end and summarize absolute-cycle latencies."""
        remaining = sum(len(queue) for queue in self.expected)
        if remaining:
            raise CheckFailure(f"capture ended with {remaining} results undelivered")
        completed = sum(self.command_latencies.values())
        latency = None
        if completed:
            ordered = sorted(self.command_latencies.elements())
            latency = {"min": ordered[0], "p50": ordered[completed // 2], "max": ordered[-1]}
        return {
            "coverage": dict(self.coverage),
            "event_kinds": dict(self.kinds),
            "rejections": dict(self.reasons),
            "terminal_latency_cycles": latency,
            "maximum_queued_expected_results": self.max_backlog,
        }


def audit(path, geometry):
    """Audit a trace against both state and ready/valid sequence requirements."""
    checker = Scoreboard(geometry)
    for sample in parse_trace(path, geometry):
        checker.step(sample)
    if not checker.coverage["cycles"]:
        raise CheckFailure("empty trace")
    return checker.summary()


def write_tape(path, geometry, seed, commands):
    """Serialize a complete workload and report the number of real inputs."""
    count = 0
    with path.open("w", encoding="ascii") as stream:
        for item in campaign(geometry, seed, commands):
            if isinstance(item, Command):
                payload = item.payload(geometry)
                print(f"0 {item.bank:x} {item.tag:x} {payload:x}", file=stream)
                count += 1
            else:
                action, duration = item
                print(f"{action} 0 0 {duration:x}", file=stream)
    return count


def require_coverage(summary, command_count, random_commands):
    """Fail campaigns that silently skipped the workload or directed features."""
    coverage = summary["coverage"]
    if coverage["accepted_inputs"] != command_count:
        raise CheckFailure(f"accepted {coverage['accepted_inputs']} of {command_count} inputs")
    for required in (
        Kind.STP_MAKER, Kind.STP_TAKER, Kind.FILL, Kind.ORDER, Kind.LEVEL,
        Kind.SNAPSHOT, Kind.IMPORT,
    ):
        if not summary["event_kinds"].get(required.name):
            raise CheckFailure(f"no transfer covered {required.name}")
    for required in (Reject.FOK, Reject.OWNER, Reject.POLICY, Reject.BANK, Reject.CROSSED):
        if not summary["rejections"].get(required.name):
            raise CheckFailure(f"no transfer covered rejection {required.name}")
    for key in ("sink_stalls", "source_stalls", "reserve_refreshes"):
        if not coverage.get(key):
            raise CheckFailure(f"no samples for {key}")
    if random_commands > 600 and not coverage.get("flushed_results"):
        raise CheckFailure("resets failed to exercise pending work")


def run(args):
    """Render, run and independently audit the simulation for one geometry."""
    geometry = PROFILES[args.profile]
    args.build.mkdir(parents=True, exist_ok=True)
    tape = args.build / "reserve_tape.txt"
    trace = args.build / "reserve_capture.txt"
    counts = write_tape(tape, geometry, args.seed, args.commands)
    command = [args.vvp, str(args.sim), f"+tape={tape}", f"+trace={trace}"]
    if args.waveform:
        command.append(f"+waveform={args.waveform}")
    subprocess.run(command, check=True)
    summary = audit(trace, geometry)
    require_coverage(summary, counts, args.commands)
    metrics = args.build / "reserve_metrics.json"
    metrics.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    cover = summary["coverage"]
    print(
        f"Reserve {args.profile}: {cover['cycles']} cycles, "
        f"{cover['accepted_inputs']} commands, {cover['accepted_results']} results, "
        f"{cover['reserve_refreshes']} reserve refreshes"
    )


def main():
    """Accept independent run/audit commands and standardized profile flags."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("run", "audit", "params"):
        command = commands.add_parser(name)
        command.add_argument("--profile", choices=PROFILES, default="default")
        if name == "run":
            command.add_argument("--seed", type=int, default=7201)
            command.add_argument("--commands", type=int, default=2400)
            command.add_argument("--build", type=Path, required=True)
            command.add_argument("--sim", type=Path, required=True)
            command.add_argument("--vvp", default="vvp")
            command.add_argument("--waveform", type=Path)
        elif name == "audit":
            command.add_argument("trace", type=Path)
    args = parser.parse_args()
    if args.action == "params":
        print(" ".join(
            f"-P tb_reserve_matching_engine.{name}={value}"
            for name, value in PROFILES[args.profile].arguments().items()
        ))
    elif args.action == "audit":
        print(json.dumps(audit(args.trace, PROFILES[args.profile]), indent=2, sort_keys=True))
    else:
        run(args)


if __name__ == "__main__":
    main()
