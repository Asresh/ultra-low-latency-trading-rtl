"""Compare pin transcripts with an independently executed request and tape model."""

import argparse
from collections import Counter
from dataclasses import dataclass, fields
from itertools import zip_longest
from pathlib import Path

from replay_model import ReplayModel
from replay_program import ReplayError, Response, Statistics, Status, U16, U32, U48, U64
from replay_vectors import read_cyclestream


@dataclass(frozen=True)
class CapturedOffer:
    """Wire output fields captured while valid, independent of acceptance."""

    data: int
    keep: int
    last: bool
    tag: int
    due: int
    offered: int
    pass_number: int

    @classmethod
    def from_model(cls, offer):
        """Convert a reference offer into the physical output fields."""
        event = offer.event
        return cls(event.data, event.keep, event.last, event.tag,
                   offer.due, offer.offered, offer.pass_number)


@dataclass(frozen=True)
class PinSample:
    """One externally observable snapshot of the replay engine."""

    index: int
    cfg_ready: bool
    response: object
    offer: object
    active: bool
    paused: bool
    done: bool
    now: int
    statistics: Statistics

    @classmethod
    def decode(cls, row):
        """Parse exact simulator field widths, ignoring payloads on invalid channels."""
        words = row.split()
        if len(words) != 21:
            raise ReplayError(f"need 21 fields, found {len(words)}")
        try:
            index = int(words[0], 10)
            raw = tuple(int(word, 16) for word in words[1:])
        except ValueError as exc:
            raise ReplayError("cycle or pin value is not an integer") from exc
        if index < 0:
            raise ReplayError("cycle index cannot be negative")
        names = (
            "cfg_ready", "rsp_valid", "status", "length", "m_valid", "data", "keep", "last",
            "tag", "due", "offered", "pass_number", "active", "paused", "done", "now",
            "beats", "packets", "late", "passes",
        )
        bounds = (
            1, 1, 15, U32, 1, U64, 255, 1, U16, U48, U48, U16, 1, 1, 1,
            U48, U32, U32, U32, U32,
        )
        for key, value, bound in zip(names, raw, bounds):
            if not 0 <= value <= bound:
                raise ReplayError(f"{key} exceeds the recorded pin width")
        cr, rv, status, length, mv, data, keep, last, tag, due, offered, number, active, paused, done, now, *stats = raw
        try:
            response = Response(Status(status), length) if rv else None
        except ValueError as exc:
            raise ReplayError(f"unknown valid response status {status}") from exc
        offer = CapturedOffer(data, keep, bool(last), tag, due, offered, number) if mv else None
        return cls(index, bool(cr), response, offer, bool(active), bool(paused), bool(done),
                   now, Statistics(*stats))

    @classmethod
    def expected(cls, index, observation):
        """Build the matching physical snapshot from a reference clock observation."""
        return cls(index, observation.cfg_ready, observation.response,
                   CapturedOffer.from_model(observation.offer) if observation.offer else None,
                   observation.active, observation.paused, observation.done,
                   observation.now, observation.statistics)


def read_transcript(path):
    """Yield samples, annotate parser failures, and enforce consecutive cycle indexes."""
    with path.open(encoding="ascii") as source:
        for index, row in enumerate(source):
            try:
                sample = PinSample.decode(row)
            except ReplayError as exc:
                raise ReplayError(f"{path}:{index+1}: {exc}") from exc
            if sample.index != index:
                raise ReplayError(f"{path}:{index+1}: saw cycle {sample.index}, expected {index}")
            yield sample


def first_difference(expected, current):
    """Describe only the first invalid field to keep differential errors actionable."""
    for field in fields(PinSample):
        want = getattr(expected, field.name)
        got = getattr(current, field.name)
        if want != got:
            return f"{field.name}: expected {want!r}, observed {got!r}"
    return None


class Coverage:
    """Observe transfers, stall patterns, protocol responses, and scheduling lateness."""

    def __init__(self):
        """Initialize counters used across multiple reset-separated campaigns."""
        self.events = Counter()
        self.statuses = Counter()
        self.ops = Counter()
        self.maximum_lateness = 0
        self.max_output_hold = 0
        self.current_hold = 0

    def observe(self, cycle, sample, step):
        """Count only completed actions outside reset, plus explicit stall cycles."""
        self.events["cycles"] += 1
        if cycle.reset:
            self.events["reset"] += 1
            self.events["reset_with_output"] += int(sample.offer is not None)
            self.current_hold = 0
            return
        if step.request is not None:
            self.ops[int(step.request.op)] += 1
            self.events["requests"] += 1
        if cycle.cfg is not None and not sample.cfg_ready:
            self.events["request_stalled"] += 1
        if step.response is not None:
            self.statuses[step.response.status.name] += 1
        elif sample.response is not None:
            self.events["response_stalled"] += 1
        if step.output is not None:
            lateness = step.output.late_by
            self.events["beats"] += 1
            self.events["packets"] += int(step.output.event.last)
            self.events["bytes"] += len(step.output.event.payload)
            self.events["late"] += int(lateness > 0)
            self.events["replayed"] += int(step.output.pass_number > 1)
            self.maximum_lateness = max(self.maximum_lateness, lateness)
        if sample.offer is not None and not cycle.ready:
            self.current_hold += 1
            self.max_output_hold = max(self.max_output_hold, self.current_hold)
            self.events["output_stalled"] += 1
        else:
            self.current_hold = 0
        if sample.paused:
            self.events["paused"] += 1
        if sample.done:
            self.events["done"] += 1

    def require_directed(self, depth):
        """Reject an RTL campaign that skipped an expected directed test path."""
        statuses = set(Status)
        if depth == 1:
            statuses.difference_update({Status.BAD_TIME, Status.BAD_TAG})
        missing_statuses = sorted(status.name for status in statuses if not self.statuses[status.name])
        required = (
            "reset_with_output", "request_stalled", "response_stalled", "output_stalled", "beats",
            "packets", "done", "paused", "replayed",
        )
        missing_paths = [item for item in required if not self.events[item]]
        if missing_statuses or missing_paths:
            raise ReplayError(f"required coverage missing: status={missing_statuses}, path={missing_paths}")

    def summary(self):
        """Return a deterministic one-line report for local or CI logs."""
        return (
            f"{self.events['cycles']} sampled edges, {self.events['requests']} commands, "
            f"{self.events['beats']} output beats, {self.events['packets']} packet ends, "
            f"{self.events['late']} late offers, max lateness={self.maximum_lateness}, "
            f"longest output hold={self.max_output_hold}, statuses={len(self.statuses)}"
        )


def verify(cycles, transcript, depth, directed=False):
    """Cross-check every observable pin and ensure stimulus and capture end together."""
    model = ReplayModel(depth)
    coverage = Coverage()
    missing = object()
    for index, (cycle, sample) in enumerate(zip_longest(cycles, transcript, fillvalue=missing)):
        if cycle is missing or sample is missing:
            raise ReplayError(f"stimulus/capture lengths differ at cycle {index}")
        step = model.step(cycle)
        difference = first_difference(PinSample.expected(index, step.observation), sample)
        if difference is not None:
            raise ReplayError(f"cycle {index}: {difference}")
        coverage.observe(cycle, sample, step)
    if not coverage.events["cycles"]:
        raise ReplayError("capture is empty")
    if directed:
        coverage.require_directed(depth)
    return coverage


def main():
    """Load input and trace text, run the oracle, and print checked coverage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stimulus", type=Path)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--depth", type=int, default=16)
    parser.add_argument("--directed", action="store_true")
    args = parser.parse_args()
    try:
        report = verify(read_cyclestream(args.stimulus), read_transcript(args.transcript),
                        args.depth, args.directed)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"PASS replay differential: {report.summary()}")


if __name__ == "__main__":
    main()
