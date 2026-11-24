"""Generate legal ready/valid stimuli and compare every cycle with SystemVerilog."""

import argparse
from collections import Counter
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
import random
import subprocess

from fanout_model import Beat, Diagnostic, Fanout, Inputs, PROFILES, Route, Status


TRACE_FIELDS = (
    "cfg_ready", "s_ready", "m_valid", "m_last", "m_data", "m_keep", "m_tag",
    "report_valid", "report_status", "report_tag", "report_group", "report_destinations",
    "report_bytes", "forwarded_packets", "filtered_packets", "congested_packets",
    "invalid_packets", "delivered_packets", "port_occupancy", "resident_slots",
)


@dataclass(frozen=True)
class PacketPlan:
    """One ingress packet encoded as a stable series of offered beats."""

    group: int
    tag: int
    length: int
    error: str = ""


class Scenario:
    """Stimulus builder that retries an offer until the model observes acceptance."""

    def __init__(self, profile):
        """Store geometry and start from the reset state before cycle zero."""
        self.p = profile
        self.model = Fanout(profile)
        self.inputs = []
        self.samples = []
        self.stats = Counter()
        self.open_outputs = [None] * profile.ports
        self.pending_offer = None
        self.reports = []
        self.transferred = [[] for _ in range(profile.ports)]

    def cycle(self, inputs=Inputs()):
        """Score one edge and verify source, sink, and report ready/valid stability."""
        if not inputs.rst and self.pending_offer is not None:
            assert inputs.offer == self.pending_offer, "source changed an unaccepted offer"
        sample = self.model.step(inputs)
        prior = self.inputs[-1] if self.inputs else None
        last = self.samples[-1] if self.samples else None
        if not inputs.rst:
            if last is not None and not prior.rst:
                if last.report is not None and not prior.report_ready:
                    assert sample.report == last.report, "report changed while held"
                for port, beat in enumerate(last.outputs):
                    if beat is not None and not (prior.ready >> port & 1):
                        assert sample.outputs[port] == beat, "port changed while held"
            for port, beat in enumerate(sample.outputs):
                if beat is not None and inputs.ready >> port & 1:
                    self.transfer(port, beat)
                elif beat is not None:
                    self.stats["egress stalls"] += 1
            if sample.report is not None and inputs.report_ready:
                self.reports.append(sample.report)
                self.stats[sample.report.status.name] += 1
            elif sample.report is not None:
                self.stats["report stalls"] += 1
            if inputs.offer is not None:
                self.stats["input transfers" if sample.s_ready else "ingress stalls"] += 1
        else:
            self.open_outputs = [None] * self.p.ports
            self.stats["reset edges"] += 1
        self.pending_offer = inputs.offer if not inputs.rst and not sample.s_ready else None
        self.stats["max resident"] = max(self.stats["max resident"], sample.resident)
        self.inputs.append(inputs)
        self.samples.append(sample)
        return sample

    def transfer(self, port, beat):
        """Reconstruct every independently handshaken egress packet as tag and bytes."""
        tag, payload = self.open_outputs[port] or (beat.tag, bytearray())
        assert tag == beat.tag, "egress packets interleaved"
        for lane in range(self.p.data_bytes):
            if beat.keep >> lane & 1:
                payload.append(beat.data >> (lane * 8) & 255)
            else:
                assert beat.data >> (lane * 8) & 255 == 0, "unused byte lane was not cleared"
        if beat.last:
            self.transferred[port].append((tag, bytes(payload)))
            self.open_outputs[port] = None
        else:
            self.open_outputs[port] = tag, payload

    def forward(self, beat, driving):
        """Retry one offer with cycle-specific surrounding traffic and a finite watchdog."""
        for _ in range(400):
            inputs = driving()
            if self.cycle(replace(inputs, offer=beat)).s_ready:
                return
        raise AssertionError("input did not make progress within 400 cycles")

    def drain(self):
        """Give each endpoint credit for enough cycles to release every descriptor."""
        assert self.pending_offer is None
        for _ in range(self.p.depth * self.p.max_beats + 3):
            self.cycle(Inputs(ready=self.p.all_ports))
            if self.model.resident == 0 and self.model.report is None:
                return
        raise AssertionError("queues or report did not drain")


def words_for(plan, geometry, rng):
    """Generate one packet, optionally placing a specific protocol error on a beat."""
    data = [rng.getrandbits(8 * geometry.data_bytes) for _ in range(plan.length)]
    final_keep = (1 << rng.randint(1, geometry.data_bytes)) - 1
    tag_mask = (1 << geometry.tag_width) - 1
    for index, value in enumerate(data):
        last = index == len(data) - 1
        tag = plan.tag
        group = plan.group if index == 0 else rng.randint(0, geometry.groups)
        keep = final_keep if last else geometry.full_keep
        if plan.error == "keep" and last:
            keep = 0 if geometry.data_bytes < 3 else 5
        if plan.error == "tag" and index == 1:
            tag = (tag + 1) & tag_mask
        yield Beat(value, keep, last, group, tag)


def assemble_demo():
    """Build a known-answer default-geometry example for documentation and replay."""
    run = Scenario(PROFILES["default"])
    run.cycle(Inputs(rst=True))
    run.cycle(Inputs(cfg=(0, Route(0b011))))
    run.cycle(Inputs(cfg=(1, Route(0b010, True))))
    # Packet A1 snapshots ports 0 and 1 on the same edge that config changes to port 2.
    run.cycle(Inputs(cfg=(0, Route(0b100)), offer=Beat(0x44332211, 0xf, False, 0, 0xa1), ready=1))
    run.cycle(Inputs(offer=Beat(0xfeed6655, 3, True, 3, 0xa1), ready=1))
    run.cycle(Inputs(ready=1, report_ready=False))
    run.cycle(Inputs(ready=1, report_ready=False))
    run.cycle(Inputs(offer=Beat(0xffaa8171, 3, True, 1, 0xb2), ready=1))
    # Port 1 is full. Its configured drop policy cannot leak a partial copy.
    run.cycle(Inputs(offer=Beat(0xdeaf85ee, 1, True, 1, 0xc3), ready=1))
    run.cycle(Inputs(offer=Beat(0xfadecc99, 1, True, 0, 0xd4), ready=5))
    run.cycle(Inputs(ready=5))
    run.cycle(Inputs(ready=7))
    run.drain()
    assert run.reports == [
        Diagnostic(Status.FORWARDED, 0xa1, 0, 3, 6),
        Diagnostic(Status.FORWARDED, 0xb2, 1, 2, 2),
        Diagnostic(Status.CONGESTED, 0xc3, 1, 2, 1),
        Diagnostic(Status.FORWARDED, 0xd4, 0, 4, 1),
    ]
    assert run.transferred == [
        [(0xa1, bytes.fromhex("112233445566"))],
        [(0xa1, bytes.fromhex("112233445566")), (0xb2, bytes.fromhex("7181"))],
        [(0xd4, bytes.fromhex("99"))],
    ]
    return run


def exercise_capacity(run):
    """Force all-or-none admission, release-edge retry, and independent slot growth."""
    geometry = run.p
    run.cycle(Inputs(rst=True))
    run.cycle(Inputs(cfg=(0, Route(geometry.all_ports))))
    tag_mask = (1 << geometry.tag_width) - 1
    for index in range(min(geometry.depth, geometry.slots)):
        beat = Beat(12 + index, 1, True, 0, index & tag_mask)
        assert run.cycle(Inputs(offer=beat)).s_ready
    waiting = Beat(97, 1, True, 0, 0x1e & tag_mask)
    blocked = run.cycle(Inputs(offer=waiting))
    assert not blocked.s_ready and blocked.resident == min(geometry.depth, geometry.slots)
    # Simultaneous terminal transfers release resources for the next edge only.
    releasing = run.cycle(Inputs(offer=waiting, ready=geometry.all_ports))
    assert not releasing.s_ready
    assert all(beat is not None and beat.last for beat in releasing.outputs)
    assert run.cycle(Inputs(offer=waiting, ready=geometry.all_ports)).s_ready
    run.drain()
    assert all((waiting.tag, b"a") in sink for sink in run.transferred)
    if geometry.slots > geometry.depth and geometry.groups > 1:
        run.cycle(Inputs(rst=True))
        for slot in range(geometry.slots):
            group = (slot // geometry.depth) % geometry.groups
            selected = 1 << (slot // geometry.depth)
            run.cycle(Inputs(cfg=(group, Route(selected))))
            assert run.cycle(Inputs(offer=Beat(slot, 1, True, group, slot))).s_ready
        assert run.model.resident == geometry.slots
        # A port with no descriptors can still be denied by shared memory exhaustion.
        free_port = (geometry.slots + geometry.depth - 1) // geometry.depth
        if free_port < geometry.ports:
            unused_group = geometry.groups - 1
            run.cycle(Inputs(cfg=(unused_group, Route(1 << free_port, True))))
            denied = Beat(0x55, 1, True, unused_group, 0x1d)
            assert run.cycle(Inputs(offer=denied)).s_ready
            assert run.model.report.status == Status.CONGESTED
            assert not run.model.queues[free_port]
        run.drain()


def exercise_errors(run, rng):
    """Use fixed statuses covering bad-group, bad-beat, overlength and sticky faults."""
    geometry = run.p
    run.cycle(Inputs(rst=True))
    run.cycle(Inputs(cfg=(geometry.groups, Route(geometry.all_ports))))
    packets = [
        (PacketPlan(geometry.groups, 7, 1, "keep"), Status.BAD_GROUP),
        (PacketPlan(0, 9, 1), Status.FILTERED),
        (PacketPlan(0, 10, 1, "keep"), Status.BAD_BEAT),
        (PacketPlan(0, 11, geometry.max_beats + 1), Status.TOO_LONG),
        (PacketPlan(0, 12, 2, "tag"), Status.BAD_TAG),
    ]
    for plan, status in packets:
        start_at = len(run.reports)
        for beat in words_for(plan, geometry, rng):
            assert run.cycle(Inputs(offer=beat, ready=geometry.all_ports)).s_ready
        run.drain()
        assert [report.status for report in run.reports[start_at:]] == [status]
    run.cycle(Inputs(cfg=(0, Route(geometry.all_ports))))
    for _ in range(256):
        assert run.cycle(Inputs(offer=Beat(255, geometry.full_keep, False, 0, 1))).s_ready
    assert run.model.resident == 1
    run.cycle(Inputs(rst=True, ready=geometry.all_ports))
    after = run.cycle(Inputs(ready=geometry.all_ports))
    assert after.resident == 0 and after.report is None and after.outcomes == (0, 0, 0, 0)


def stress(profile, seed, packets):
    """Score seeded traffic with mutable routing, unequal receiver rates, and resets."""
    rng = random.Random(seed)
    run = Scenario(profile)
    exercise_capacity(run)
    exercise_errors(run, rng)
    run.cycle(Inputs(rst=True))

    def blank():
        """Generate a cycle without an offered beat but with independently changing peers."""
        ready = sum((rng.randrange(10) < max(2, 8 - 2 * port)) << port for port in range(profile.ports))
        cfg = None
        if rng.randrange(7) == 0:
            cfg = (rng.randrange(profile.groups + 1),
                   Route(rng.randrange(profile.all_ports + 1), bool(rng.getrandbits(1))))
        return Inputs(ready=ready, cfg=cfg, report_ready=rng.randrange(5) != 0)

    for group in range(profile.groups):
        run.cycle(Inputs(cfg=(group, Route(profile.all_ports, group % 2 == 0))))
    for index in range(packets):
        if index and index % 257 == 0:
            run.cycle(Inputs(rst=True, ready=profile.all_ports))
        group = rng.randrange(profile.groups + 1)
        tag = rng.getrandbits(profile.tag_width)
        length = rng.randint(1, profile.max_beats)
        kind = rng.randrange(16)
        error = ""
        if kind == 0:
            length = profile.max_beats + rng.randint(1, 4)
        if kind == 1:
            error = "keep"
        if kind == 2 and length > 1:
            error = "tag"
        for beat in words_for(PacketPlan(group, tag, length, error), profile, rng):
            if rng.randrange(5) == 0:
                run.cycle(blank())
            run.forward(beat, blank)
    run.drain()
    return run


def compare(run, simulator, image, directory, waveform=None):
    """Write stimuli, execute RTL, and report the first field difference between traces."""
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "inputs.hex"
    capture = directory / "observed.hex"
    expected = directory / "expected.hex"
    paths = [source, capture, expected] + ([waveform] if waveform is not None else [])
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("waveform path must differ from stimulus and trace artifacts")
    source.write_text("".join(format_row(values.words()) for values in run.inputs))
    expected.write_text("".join(format_row((cycle, *sample.words(run.p)))
                                for cycle, sample in enumerate(run.samples)))
    command = [simulator, str(image), f"+input={source}", f"+output={capture}"]
    if waveform is not None:
        waveform.parent.mkdir(parents=True, exist_ok=True)
        command.append(f"+waveform={waveform}")
    subprocess.run(command, check=True)
    rows = capture.read_text().splitlines()
    if len(rows) != len(run.samples):
        raise AssertionError(f"expected {len(run.samples)} RTL cycles, observed {len(rows)}")
    for cycle, (row, model) in enumerate(zip(rows, run.samples)):
        fields = row.split()
        try:
            actual = tuple(int(field, 16) for field in fields)
        except ValueError as error:
            raise AssertionError(f"cycle {cycle} contains unknown RTL state: {row}") from error
        wanted = (cycle, *model.words(run.p))
        if actual != wanted:
            if len(actual) != len(wanted):
                raise AssertionError(f"cycle {cycle} expected {len(wanted)} trace fields: {row}")
            differences = [f"{name}: expected {a:#x}, got {b:#x}"
                           for name, a, b in zip(("cycle", *TRACE_FIELDS), wanted, actual)
                           if a != b]
            raise AssertionError(f"cycle {cycle}, stimulus {run.inputs[cycle]}: " + "; ".join(differences))
    print(f"PASS {len(run.samples)} cycles, {len(TRACE_FIELDS)} monitored fields. {dict(run.stats)}")


def format_row(words):
    """Format a fixed-order testbench row using unsigned hexadecimal words."""
    return " ".join(f"{int(word):x}" for word in words) + "\n"


def main():
    """Provide profile parameters and run reproducible Icarus comparisons."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("parameters", "check", "example"))
    parser.add_argument("--profile", choices=PROFILES, default="default")
    parser.add_argument("--simulator", default="vvp")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--directory", type=Path, default=Path("build/check"))
    parser.add_argument("--waveform", type=Path)
    parser.add_argument("--seed", type=int, default=27115)
    parser.add_argument("--packets", type=int, default=1200)
    options = parser.parse_args()
    if options.action == "parameters":
        for key, value in PROFILES[options.profile].defines().items():
            print(f"-Ptb_multicast_packet_fanout.{key}={value}")
        return
    if options.image is None:
        parser.error("supply --image for simulation")
    if options.packets < 0:
        parser.error("--packets must be nonnegative")
    if options.action == "example" and options.profile != "default":
        parser.error("the example uses the default profile")
    run = (assemble_demo() if options.action == "example" else
           stress(PROFILES[options.profile], options.seed, options.packets))
    compare(run, options.simulator, options.image, options.directory, options.waveform)


if __name__ == "__main__":
    main()
