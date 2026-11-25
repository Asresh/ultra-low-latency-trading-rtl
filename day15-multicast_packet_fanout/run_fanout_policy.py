"""Compare dynamic subscriber policy, credits and packet copies with the RTL."""

import argparse
from collections import Counter
from dataclasses import replace
from itertools import zip_longest
from pathlib import Path
import random
import subprocess

from fanout_model import Beat, PROFILES, Status
from fanout_policy_model import PARTIAL, Policy, PolicyFanout, PolicyInputs


def hexadecimal(values):
    return " ".join(f"{int(value):x}" for value in values)


class Scenario:
    def __init__(self, profile):
        self.profile = profile
        self.model = PolicyFanout(profile)
        self.stimulus = []
        self.expected = []
        self.metrics = Counter()

    def edge(self, inputs=None, **fields):
        row = inputs or PolicyInputs(**fields)
        expected = self.model.step(row)
        self.stimulus.append(row.words(self.profile))
        self.expected.append(expected)
        if row.beat:
            self.metrics["accepted_beats" if expected[3] else "blocked_beats"] += 1
        if expected[9]:
            if row.report_ready:
                self.metrics[f"status_{expected[10]}"] += 1
                self.metrics["slot_pressure"] += expected[17]
                self.metrics["epochs_wrapped"] += expected[13] == 0 and expected[14] != 0
            else:
                self.metrics["held_reports"] += 1
        self.metrics["output_beats"] += sum(
            (expected[4] & row.ready) >> port & 1 for port in range(self.profile.ports)
        )
        return expected[3]

    def send(self, packet, controls):
        """Hold one input beat unchanged while subscriber or report credit is absent."""
        for beat in packet:
            for _ in range(250):
                if self.edge(controls(beat)):
                    break
            else:
                raise AssertionError("test fixture starved an input beat")

    def drain(self, ready=None):
        """Retire any remaining reports and all enabled subscriber references."""
        ready = self.profile.all_ports if ready is None else ready
        for _ in range(self.profile.max_beats*self.profile.depth + 6):
            self.edge(ready=ready)


def policy_for(profile, members, mandatory, mark, may_drop=False):
    optional = members & ~mandatory
    counts = tuple(mark if optional >> port & 1 else 0 for port in range(profile.ports))
    return Policy(members, mandatory, counts, may_drop)


def example(profile):
    """A blocked optional recorder is omitted while required trading copies continue."""
    run = Scenario(profile)
    p = profile
    assert p.ports >= 2 and p.groups >= 2
    both, required = 3, 1
    primary = policy_for(p, both, required, 1)
    recorder_only = policy_for(p, 2, 2, 0)
    newer = policy_for(p, both, required, 1, may_drop=True)
    run.edge(rst=True)
    run.edge(config=(0, primary))
    run.edge(config=(1, recorder_only))
    packet_a = Beat(0x1234, p.full_keep, False, 0, 0xA1)
    assert run.edge(beat=packet_a, config=(0, newer), ready=required)
    run.send([replace(packet_a, data=0xabba, last=True, keep=1)], lambda beat: PolicyInputs(beat=beat, ready=required))
    for tag, group in ((0xB2, 0), (0xC3, 1), (0xD4, 0)):
        run.send([Beat(tag, 1, True, group, tag)], lambda beat: PolicyInputs(beat=beat, ready=required))
    run.drain()
    reports = [
        (row[10], row[11], row[13], row[16], row[18])
        for stimulus,row in zip(run.stimulus,run.expected) if row[9] and stimulus[-1]
    ]
    assert reports == [
        (Status.FORWARDED, 0xA1, 1, 0, 3), (PARTIAL, 0xB2, 2, 2, 1),
        (Status.FORWARDED, 0xC3, 1, 0, 2), (PARTIAL, 0xD4, 2, 2, 1),
    ], reports
    return run


def make_packet(profile, rng, index, group):
    length = rng.randrange(1, profile.max_beats+1)
    variant = rng.random()
    tag = index & ((1 << profile.tag_width)-1)
    if variant < 0.1:
        length = profile.max_beats+1
    beat_list = []
    for beat in range(length):
        last = beat == length-1
        keep = (1 << rng.randint(1,profile.data_bytes))-1 if last else profile.full_keep
        beat_tag = tag
        if variant >= 0.10 and variant < 0.17 and last:
            keep = 0
        if variant >= 0.17 and variant < 0.25 and length > 1 and last:
            beat_tag ^= 1
        beat_list.append(Beat(rng.getrandbits(profile.data_bytes*8), keep, last, group, beat_tag))
    return beat_list


def randomized(profile, count, seed):
    p = profile
    run = Scenario(p)
    rng = random.Random(seed)

    def change(group=None):
        group = rng.randrange(p.groups+1) if group is None else group
        members = rng.randrange(p.all_ports+1)
        mandatory = rng.randrange(p.all_ports+1) & members
        policy = policy_for(p, members, mandatory, rng.randrange(p.depth+1), rng.randrange(2) == 1)
        if rng.randrange(10) == 0:
            policy = replace(policy, marks=(p.depth+1,) * p.ports)
            if p.depth+1 >= 1 << p.depth.bit_length():
                policy = replace(policy, marks=(p.depth,) * p.ports, members=0, mandatory=0)
        return group, policy

    def provide(beat):
        ready = sum((rng.randrange(10) > (7 if port == 0 else 3)) << port for port in range(p.ports))
        config = change() if rng.randrange(12) == 0 else ()
        return PolicyInputs(config=config, beat=beat, ready=ready, report_ready=rng.randrange(6) != 0)

    for index in range(count):
        if index % 127 == 0:
            run.edge(rst=True)
            for group in range(p.groups):
                run.edge(config=change(group))
        elif index % 5 == 0:
            run.edge(config=change())
        group = p.groups if index % 41 == 0 else rng.randrange(p.groups)
        packet = make_packet(p, rng, index, group)
        run.send(packet, provide)
    run.drain()
    if run.model.core.resident or run.model.stage != "first":
        raise AssertionError("fixture did not fully release ownership")
    return run


def compare(run, paths, args):
    profile = run.profile
    paths.mkdir(parents=True, exist_ok=True)
    simulator = paths / "policy.vvp"
    input_path = paths / "inputs.hex"
    observed_path = paths / "observed.hex"
    expected_path = paths / "expected.hex"
    input_path.write_text("".join(hexadecimal(row)+"\n" for row in run.stimulus), encoding="ascii")
    expected_path.write_text("".join(hexadecimal(row)+"\n" for row in run.expected), encoding="ascii")
    bench = "tb_multicast_policy_fanout"
    compile_cmd = [args.iverilog, "-g2012", "-s", bench, "-o", str(simulator)]
    compile_cmd.extend(f"-P{bench}.{parameter}={value}" for parameter,value in profile.defines().items())
    compile_cmd.extend(("-f", "rtl_policy.f", f"{bench}.sv"))
    subprocess.run(compile_cmd, check=True)
    sim_cmd = [args.vvp, str(simulator), f"+input={input_path}", f"+output={observed_path}"]
    if args.wave:
        args.wave.parent.mkdir(parents=True, exist_ok=True)
        sim_cmd.append(f"+waveform={args.wave}")
    subprocess.run(sim_cmd, check=True)
    with observed_path.open(encoding="ascii") as observed:
        for index, (wanted, actual) in enumerate(zip_longest(run.expected, observed)):
            if actual is None or wanted is None:
                raise AssertionError("simulator and oracle differ in sample counts")
            got = tuple(int(token,16) for token in actual.split())
            if got != wanted:
                changed = [i for i,(x,y) in enumerate(zip(wanted,got)) if x != y]
                raise AssertionError(f"Cycle {index}, columns {changed}: expected {wanted} and got {got}")
    print(f"{paths.name}: {len(run.expected)} cycles matched, {dict(run.metrics)}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--example", action="store_true")
    parser.add_argument("--wave", type=Path)
    parser.add_argument("--packets", type=int, default=850)
    parser.add_argument("--seed", type=int, default=15395)
    args = parser.parse_args()
    if args.wave and not args.example:
        parser.error("wave export uses the deterministic example")
    if args.packets < 1:
        parser.error("packets must be positive")
    base = Path("build") / "policy"
    if args.example:
        compare(example(PROFILES["default"]), base / "example", args)
    else:
        for label, profile in PROFILES.items():
            compare(randomized(profile,args.packets,args.seed), base / label, args)


if __name__ == "__main__":
    main()
