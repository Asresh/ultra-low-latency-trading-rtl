"""Audit a pin trace against a stand-alone arithmetic and handshake model."""

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path

from limiter_coverage import DecisionCoverage
from limiter_model import CycleModel, ReferenceVerifier
from limiter_protocol import (
    COMMAND_COLUMNS,
    RESPONSE_COLUMNS,
    Drive,
    Manifest,
    Observation,
    read_rows,
    write_rows,
)


class CaptureMismatch(AssertionError):
    """A row or request failed the comparison with the executable contract."""


@dataclass(frozen=True)
class Transfer:
    """A delivered decision paired with its accepted command and cycle."""

    accepted: int
    delivered: int
    command: object
    response: object

    @property
    def latency(self):
        """Count cycles from input handshake to output handshake."""
        return self.delivered - self.accepted


@dataclass
class CaptureReport:
    """Cycle and decision statistics used by the command-line audit tool."""

    cycles: int = 0
    resets: int = 0
    requests: int = 0
    transfers: int = 0
    discarded: int = 0
    input_stalled: int = 0
    output_stalled: int = 0
    replaced: int = 0
    coverage: dict = field(default_factory=dict)
    statuses: Counter = field(default_factory=Counter)
    latencies: Counter = field(default_factory=Counter)
    key_statuses: dict = field(default_factory=lambda: defaultdict(Counter))

    def to_record(self):
        """Produce stable JSON-friendly diagnostic data without enum keys."""
        return {
            "cycles": self.cycles,
            "resets": self.resets,
            "requests": self.requests,
            "transfers": self.transfers,
            "discarded": self.discarded,
            "input_stalled": self.input_stalled,
            "output_stalled": self.output_stalled,
            "replaced": self.replaced,
            "coverage": dict(self.coverage),
            "statuses": {status.name: count for status, count in sorted(self.statuses.items())},
            "latencies": {str(delay): count for delay, count in sorted(self.latencies.items())},
            "keys": {
                str(key): {status.name: count for status, count in sorted(codes.items())}
                for key, codes in sorted(self.key_statuses.items())
            },
        }

    def summary(self):
        """Render a compact test result that can be searched in CI logs."""
        details = " ".join(
            f"{status.name}={count}" for status, count in sorted(self.statuses.items())
        )
        return (
            f"{self.cycles} cycles, {self.requests} accepted, {self.transfers} checked, "
            f"{self.discarded} reset discards, {self.output_stalled} output stalls"
            f" [{details}]"
        )


class CaptureAudit:
    """Validate pin behavior and retain complete records for offline analysis."""

    def __init__(self, geometry):
        """Start from reset and allocate independent input/output orders."""
        self.model = CycleModel(geometry)
        self.invariants = ReferenceVerifier(geometry)
        self.coverage = DecisionCoverage(geometry)
        self.report = CaptureReport()
        self.outstanding = deque()
        self.delivered = []
        self.held_request = None
        self.held_response = None

    def cross_check(self, expected, actual, cycle):
        """Report exact fields if the two implementations disagree."""
        if expected.input_ready != actual.input_ready:
            raise CaptureMismatch(
                f"cycle {cycle}: expected s_ready {expected.input_ready}, got {actual.input_ready}"
            )
        if expected.output_valid != actual.output_valid:
            raise CaptureMismatch(
                f"cycle {cycle}: expected m_valid {expected.output_valid}, got {actual.output_valid}"
            )
        if expected.response is not None:
            difference = expected.response.differences(actual.response)
            if difference:
                message = ", ".join(
                    f"{name} expected {want!s} got {got!s}"
                    for name, (want, got) in difference.items()
                )
                raise CaptureMismatch(f"cycle {cycle}: {message}")

    def check_stability(self, drive, sample, cycle):
        """Make stalled request and response stability explicit in audit output."""
        if drive.reset:
            self.held_request = None
            self.held_response = None
            return
        if self.held_request is not None:
            if not drive.valid or drive.command != self.held_request:
                raise CaptureMismatch(f"cycle {cycle}: source changed a stalled request")
        if self.held_response is not None:
            if not sample.output_valid or sample.response != self.held_response:
                raise CaptureMismatch(f"cycle {cycle}: sink saw a stalled result change")
        self.held_request = drive.command if drive.valid and not sample.input_ready else None
        self.held_response = sample.response if sample.output_valid and not drive.ready else None

    def check_cycle(self, drive, actual):
        """Consume one cycle in order and pair accepted and delivered boundaries."""
        cycle = self.report.cycles
        self.report.cycles += 1
        expected = self.model.step(drive)
        self.check_stability(drive, actual, cycle)
        self.cross_check(expected, actual, cycle)
        if drive.reset:
            self.coverage.reset(self.outstanding)
            self.report.resets += 1
            self.report.discarded += len(self.outstanding)
            self.outstanding.clear()
            return
        if actual.output_valid and not drive.ready:
            self.report.output_stalled += 1
        if drive.valid and not actual.input_ready:
            self.report.input_stalled += 1
        transferred = bool(actual.output_valid and drive.ready)
        accepted = bool(drive.valid and actual.input_ready)
        if transferred:
            if not self.outstanding:
                raise CaptureMismatch(f"cycle {cycle}: completed without a request")
            start, command = self.outstanding.popleft()
            self.invariants.observe_decision(command, actual.response)
            delivery = Transfer(start, cycle, command, actual.response)
            self.delivered.append(delivery)
            self.coverage.transfer(command, actual.response)
            self.report.transfers += 1
            self.report.statuses[actual.response.status] += 1
            self.report.key_statuses[actual.response.key][actual.response.status] += 1
            self.report.latencies[delivery.latency] += 1
        if accepted:
            self.report.requests += 1
            self.outstanding.append((cycle, drive.command))
        if accepted and transferred:
            self.report.replaced += 1
        if len(self.outstanding) > 1:
            raise CaptureMismatch(f"cycle {cycle}: exceeded one-response capacity")

    def finish(self):
        """Confirm end-of-capture checks and return the complete audit report."""
        if self.outstanding:
            raise CaptureMismatch("capture ended without draining the last response")
        if self.held_request is not None:
            raise CaptureMismatch("capture ended with a request stuck at the source")
        if self.report.transfers + self.report.discarded != self.report.requests:
            raise CaptureMismatch("accepted requests do not balance against completions")
        self.report.coverage = self.coverage.export()
        return self.report


def prepare(directory, manifest, inputs):
    """Write a reproducible simulation schedule before the simulator starts."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest.write(directory / "manifest.json")
    write_rows(directory / "inputs.hex", (drive.words() for drive in inputs))


def audit_directory(directory):
    """Audit exactly one RTL capture directory and retain its command stream."""
    directory = Path(directory)
    manifest = Manifest.read(directory / "manifest.json")
    geometry = manifest.geometry
    checker = CaptureAudit(geometry)
    drivers = read_rows(directory / "inputs.hex", COMMAND_COLUMNS + 3)
    samples = read_rows(directory / "capture.hex", RESPONSE_COLUMNS + 2)
    sentinel = object()
    for cycle, (drive_words, sample_words) in enumerate(
        zip_longest(drivers, samples, fillvalue=sentinel)
    ):
        if drive_words is sentinel:
            raise CaptureMismatch(f"cycle {cycle}: unexpected capture row")
        if sample_words is sentinel:
            raise CaptureMismatch(f"cycle {cycle}: missing capture row")
        try:
            drive = Drive.from_words(drive_words, geometry)
            observed = Observation.from_words(sample_words, geometry)
        except ValueError as exc:
            raise CaptureMismatch(f"cycle {cycle}: {exc}") from exc
        checker.check_cycle(drive, observed)
    result = checker.finish()
    if manifest.cycles != result.cycles:
        raise CaptureMismatch(
            f"manifest expected {manifest.cycles} cycles, read {result.cycles}"
        )
    return manifest, result, checker.delivered
