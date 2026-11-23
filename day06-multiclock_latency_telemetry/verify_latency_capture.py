"""Check hardware trace and histogram files against the primary result stream."""

import argparse
from collections import Counter
from itertools import zip_longest
from pathlib import Path
import sys

from latency_capture import (
    CaptureError, histogram_frames, read_event, read_words, trace_frames,
)
from latency_reference import ClassHistogram, HALF64, Status, modular_delta


def parse_capture(directory, name, width):
    """Load one captured word stream at its expected width."""
    with (directory / name).open(encoding="ascii") as source:
        yield from read_words(source, width)


def verify(directory, groups):
    """Check captured data against the independent reference for this scenario."""
    event_words = parse_capture(directory, "events.hex", 320)
    transfer_ticks = parse_capture(directory, "transfers.hex", 64)
    transmitted = []
    status_counts = Counter()
    for position, (word, tick) in enumerate(zip_longest(event_words, transfer_ticks), 1):
        if word is None or tick is None:
            raise CaptureError("primary stream and transfer ticks have different lengths")
        event = read_event(word)
        if modular_delta(tick, event.observed_tick) >= HALF64:
            raise CaptureError(f"reply {position} precedes its processing timestamp")
        if transmitted and modular_delta(tick, transmitted[-1][0]) >= HALF64:
            raise CaptureError("primary transfer time moved backwards")
        transmitted.append((tick, event))
        status_counts[event.status] += 1
    if not transmitted:
        raise CaptureError("no primary records sampled")
    snapshots = list(histogram_frames(parse_capture(directory, "histogram.hex", 128), groups))
    traces = list(trace_frames(parse_capture(directory, "trace.hex", 320)))
    if not snapshots or not traces:
        raise CaptureError("an export stream was empty")
    verify_histograms(transmitted, snapshots)
    verify_traces([record for _, record in transmitted], traces)
    return status_counts, len(snapshots), len(traces)


def verify_histograms(transmitted, snapshots):
    """Compare snapshots with the sample history at each captured boundary."""
    offset = 0
    previous_cut = None
    for frame in snapshots:
        if previous_cut is not None and modular_delta(frame.cut_tick, previous_cut) >= HALF64:
            raise CaptureError("closed banks move backwards")
        samples = [
            ClassHistogram(len(group.bucket_counts), group.counter_bits) for group in frame.groups
        ]
        while offset < len(transmitted):
            received_at, event = transmitted[offset]
            if modular_delta(frame.cut_tick, received_at) >= HALF64:
                break
            if event.status == Status.MATCH:
                if event.event_class >= len(samples):
                    raise CaptureError("event class not present in reported geometry")
                samples[event.event_class].add(event.duration)
            offset += 1
        for index, (want, actual) in enumerate(zip(samples, frame.groups)):
            compact = (
                want.count, want.total, want.minimum, want.maximum,
                want.overflow, tuple(want.buckets),
            )
            decoded = (
                actual.count, actual.total, actual.minimum, actual.maximum,
                actual.overflow, actual.bucket_counts,
            )
            if compact != decoded:
                raise CaptureError(f"snapshot {frame.frame_id}, class {index}: {decoded} != {compact}")
        previous_cut = frame.cut_tick


def verify_traces(records, traces):
    """Compare trigger position and each retained trace record with the event stream."""
    next_start = 0
    for frame in traces:
        segment = list(frame.records)
        for start in range(next_start, len(records) - len(segment) + 1):
            if records[start:start + len(segment)] == segment:
                next_start = start + len(segment)
                break
        else:
            raise CaptureError(f"trace {frame.frame_id} not found as a contiguous received sequence")


def main():
    """Read and verify the simulation's event, histogram and trace captures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--classes", type=int, default=4)
    args = parser.parse_args()
    try:
        counts, snapshots, traces = verify(args.directory, args.classes)
        summary = ", ".join(f"{status.name}={amount}" for status, amount in sorted(counts.items()))
        print(f"PASS capture: {sum(counts.values())} reports, {snapshots} snapshots, "
              f"{traces} traces ({summary})")
    except (CaptureError, OSError, ValueError) as exc:
        print(f"verify_latency_capture: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
