"""Predict, drive and check RTL schedules against configured periodic backpressure."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from random import Random

from replay_compare import ComparisonOptions, check_run
from replay_format import StampedPacket, read_tape, write_tape
from replay_metrics import load_runs
from replay_model import load_commands
from replay_planner import CompileOptions, PredictedBeat, compile_packets
from replay_program import Command, Cycle, ReplayError, U16, U32, U48, unsigned
from replay_vectors import write_cyclestream

PROFILES = ("dense", "sparse", "mixed")


@dataclass(frozen=True)
class PeriodicReady:
    """Finite pattern whose first bit drives the sink on virtual tick zero."""

    bits: tuple

    def __post_init__(self):
        """Require only Boolean flags and at least one open cycle per period."""
        if type(self.bits) is not tuple or not 1 <= len(self.bits) <= 4096:
            raise ReplayError("ready pattern needs 1–4096 bits")
        if any(type(bit) is not bool for bit in self.bits):
            raise ReplayError("ready pattern flags must be Boolean")
        if not any(self.bits):
            raise ReplayError("ready pattern never accepts output")

    @classmethod
    def parse(cls, text):
        """Read a compact ASCII pattern such as '100101'."""
        if type(text) is not str or set(text) - {"0", "1"}:
            raise ReplayError("ready pattern needs only characters 0 and 1")
        return cls(tuple(bit == "1" for bit in text))

    def ready(self, tick):
        """Return the repeated sink state at this virtual tick."""
        unsigned(tick, U48, "ready tick")
        return self.bits[tick % len(self.bits)]

    def next_ready(self, tick):
        """Find the next eligible transfer edge in at most one complete period."""
        for delay in range(len(self.bits)):
            candidate = tick + delay
            if candidate > U48:
                raise ReplayError("periodic forecast exceeds the virtual time range")
            if self.ready(candidate):
                return candidate
        raise AssertionError("a validated period must contain an open slot")

    def word(self):
        """Return a stable compact representation for an experiment manifest."""
        return "".join("1" if bit else "0" for bit in self.bits)


@dataclass(frozen=True)
class Experiment:
    """Program image plus the periodic sink and repeated-run policy."""

    tape: object
    ready: PeriodicReady
    repeats: int = 2
    gap: int = 3
    cycle_limit: int = 100000

    def __post_init__(self):
        """Validate repeat geometry and refuse a zero-capacity forecast."""
        unsigned(self.repeats, U16, "repeat count")
        unsigned(self.gap, U32, "repeat gap")
        unsigned(self.cycle_limit, U32, "cycle limit")
        if self.repeats == 0 or self.cycle_limit == 0 or not self.tape.events:
            raise ReplayError("periodic experiment requires work and positive bounds")

    def predict(self):
        """Forecast exact offers and accepted tick edges without running the DUT model."""
        base = 0
        for pass_number in range(1, self.repeats+1):
            free_at = base
            accepted = 0
            for index, event in enumerate(self.tape.events):
                due = base + event.tick
                offered = max(due, free_at)
                if offered >= self.cycle_limit:
                    raise ReplayError("experiment exceeds its virtual cycle budget")
                accepted = self.ready.next_ready(offered+1)
                if accepted >= self.cycle_limit:
                    raise ReplayError("ready pattern pushes experiment past its cycle budget")
                yield PredictedBeat(pass_number, index, event, due, offered, accepted)
                free_at = accepted
            base = accepted + self.gap + 1

    def pin_cycles(self):
        """Drive the planned image followed by independently scheduled readiness edges."""
        final_tick = 0
        for event in self.predict():
            final_tick = event.accepted
        initial = [Cycle(reset=True)]
        initial.extend(Cycle(cfg=request) for request in load_commands(self.tape))
        initial.append(Cycle(cfg=Command.start(self.repeats, self.gap)))
        initial.extend(Cycle(ready=self.ready.ready(tick)) for tick in range(final_tick+1))
        initial.extend((Cycle(), Cycle()))
        return tuple(initial)

    def expected_words(self):
        """Return a portable list of expected hardware handshakes in order."""
        return tuple(
            (
                row.pass_number, row.tape_index, row.event.tag, row.event.payload.hex(),
                int(row.event.last), row.due, row.offered, row.accepted,
            )
            for row in self.predict()
        )

    def verify(self, run):
        """Check the physical capture with exact analytic scheduling and payload results."""
        program = check_run(self.tape, run, ComparisonOptions(self.repeats, self.gap))
        expected = self.expected_words()
        measured = tuple(
            (
                row.pass_number, index % len(self.tape.events), row.tag, row.payload.hex(),
                int(row.last), row.due, row.offered, row.accepted,
            )
            for index, row in enumerate(run.deliveries)
        )
        for index, (target, actual) in enumerate(zip(expected, measured)):
            if actual != target:
                raise ReplayError(f"analytic check line {index} expects {target!r}, observed {actual!r}")
        result = program.record()
        result["expected_first_offset"] = expected[0][7]
        result["measured_first_offset"] = run.cycles_to_first
        result["physical_hold_cycles"] = run.output_hold_cycles
        result["program_passes"] = self.repeats
        return result

    def manifest(self):
        """Return timing options independently of generated stimulus or capture files."""
        return {
            "format": 1,
            "ready": self.ready.word(),
            "repeats": self.repeats,
            "gap": self.gap,
            "cycle_limit": self.cycle_limit,
        }

    @classmethod
    def read(cls, source, program):
        """Reconstruct an experiment from explicitly versioned settings."""
        options = json.load(source)
        if type(options) is not dict or set(options) != {"format", "ready", "repeats", "gap", "cycle_limit"}:
            raise ReplayError("experiment manifest fields differ from the supported schema")
        if options.pop("format") != 1:
            raise ReplayError("unsupported experiment manifest version")
        ready = PeriodicReady.parse(options.pop("ready"))
        return cls(program, ready, **options)


def packet_lengths(profile):
    """Select varied terminal masks and multi-beat sizes for each traffic shape."""
    cases = {
        "dense": (1, 2, 3, 4, 5, 6, 7, 8, 10, 13, 32),
        "sparse": (7, 12, 16, 19, 28, 3, 1),
        "mixed": (1, 16, 24, 4, 8, 9, 8, 7, 23, 31),
    }
    try:
        return cases[profile]
    except KeyError as exc:
        raise ReplayError(f"unsupported traffic profile {profile}") from exc


def capture_fixture(profile, seed):
    """Build exact input times and deterministic packet bytes for a traffic shape."""
    rng = Random(seed)
    time_ns = rng.randrange(10**9, 2*10**9)
    for index, length in enumerate(packet_lengths(profile)):
        if profile == "dense":
            interval = rng.randrange(0, 3)
        elif profile == "sparse":
            interval = rng.randrange(1000, 3001)
        else:
            interval = rng.choice((0, 3, 9, 12, 100, 1000))
        time_ns += interval
        payload = bytes(rng.getrandbits(8) for _ in range(length))
        yield StampedPacket(time_ns, 0x100+index, payload)


def benchmark_fixture(profile, seed, *, depth=32):
    """Create a checked finite tape using the host-clock compiler and chosen ready pattern."""
    timing = {
        "dense": (0, 0, "101", 2, 5),
        "sparse": (4, 3, "1", 3, 7),
        "mixed": (1, 2, "1001000001", 2, 1),
    }
    packets = tuple(capture_fixture(profile, seed))
    spacing, interpacket, mask, repeats, repeat_gap = timing[profile]
    program = compile_packets(
        packets, CompileOptions(100000000, beat_spacing=spacing, packet_gap=interpacket,
                                maximum_beats=depth)
    )
    return Experiment(program.tape, PeriodicReady.parse(mask), repeats, repeat_gap)


def save_experiment(folder, experiment):
    """Write binary program, simulator inputs, and compact independent expectations."""
    folder.mkdir(parents=True, exist_ok=True)
    write_tape(experiment.tape, folder/"image.trp")
    write_cyclestream(folder/"input.stim", experiment.pin_cycles())
    manifest = json.dumps(experiment.manifest(), sort_keys=True, indent=2)
    (folder/"settings.json").write_text(manifest+"\n", encoding="utf-8")
    outcomes = json.dumps(experiment.expected_words(), indent=2)
    (folder/"analytical.json").write_text(outcomes+"\n", encoding="utf-8")


def check_experiment(folder):
    """Read actual hardware observations and validate the one requested experiment."""
    tape = read_tape(folder/"image.trp")
    with (folder/"settings.json").open(encoding="utf-8") as source:
        experiment = Experiment.read(source, tape)
    runs = load_runs(folder/"input.stim", folder/"output.trace")
    if len(runs) != 1:
        raise ReplayError(f"experiment capture needs one run, found {len(runs)}")
    result = experiment.verify(runs[0])
    (folder/"result.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result


def main():
    """Build one deterministic traffic fixture or check its hardware transcript."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "check"))
    parser.add_argument("folder", type=Path)
    parser.add_argument("--profile", choices=PROFILES, default="dense")
    parser.add_argument("--seed", type=int, default=132132)
    args = parser.parse_args()
    try:
        if args.action == "build":
            save_experiment(args.folder, benchmark_fixture(args.profile, args.seed))
        else:
            result = check_experiment(args.folder)
            print(f"PASS analytic replay: {result['accepted_beats']} beats, "
                  f"{result['complete_passes']} passes")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
