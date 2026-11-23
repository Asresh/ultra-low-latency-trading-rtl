"""Generate adversarial cycle vectors scored by the independent reference model."""

import argparse
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
import random

from latency_reference import Correlator, Geometry, HALF64, LIMIT64, Observation, Status, histogram_words


@dataclass(frozen=True)
class Drive:
    observation: object = None
    sink_ready: bool = True
    armed: bool = True
    reset: bool = False
    flush: bool = False
    delta: int = 1
    timeout: int = 60


class Campaign:
    def __init__(self, geometry, sink):
        self.geometry = geometry
        self.model = Correlator(geometry)
        self.sink = sink
        self.tick = LIMIT64 - 24
        self.counts = Counter()
        self.stall_count = 0
        self.timeout = 60
        self.cycles = 0

    def cycle(self, drive=Drive()):
        self.tick = (self.tick + drive.delta) & LIMIT64
        visible = self.model.step(
            tick=self.tick,
            timeout=drive.timeout,
            observation=drive.observation,
            sink_ready=drive.sink_ready,
            armed=drive.armed,
            reset=drive.reset,
            flush=drive.flush,
        )
        m = visible.visible
        if m and drive.sink_ready and not drive.flush:
            self.counts[m.status] += 1
        if m and not drive.sink_ready:
            self.stall_count += 1
        rows = (
            int(drive.reset), int(drive.flush), int(drive.armed), self.tick,
            drive.timeout, int(drive.observation is not None),
            self.geometry.encode(drive.observation) if drive.observation else 0,
            int(drive.sink_ready), int(visible.ready), int(m is not None),
            m.unit_word(self.geometry) if m else 0, visible.pending, visible.evicted,
        )
        print(" ".join(f"{value:x}" for value in rows), file=self.sink)
        self.cycles += 1
        return visible

    def endpoint(self, tag, start, lag=1, group=0, probe=0):
        return Observation((self.tick - lag) & LIMIT64, tag, group, probe, is_end=not start)

    def send(self, event, *, timeout=60, armed=True, sink_ready=True):
        held = Drive(event, sink_ready=sink_ready, armed=armed, timeout=timeout)
        for _ in range(256):
            if self.cycle(held).ready:
                return
        raise AssertionError("synthetic writer could not advance")

    def idle(self, cycles, *, timeout=60, sink_ready=True):
        for _ in range(cycles):
            self.cycle(Drive(sink_ready=sink_ready, timeout=timeout))

    def collide(self, group, amount):
        found = {}
        limit = 1 << self.geometry.tag_width
        for tag in range(limit):
            probe = self.endpoint(tag, start=True, group=group)
            target = self.geometry.set_of(probe)
            tags = found.setdefault(target, [])
            tags.append(tag)
            if len(tags) == amount:
                return tags
        raise ValueError("tag width is too small for requested collisions")

    def directed(self):
        self.cycle(Drive(reset=True))
        self.send(self.endpoint(14, start=True), armed=False)
        self.send(self.endpoint(15, start=True, lag=-100))
        self.send(self.endpoint(2, start=False, lag=1, probe=2, group=1))
        self.send(self.endpoint(2, start=True, lag=9, probe=1, group=1))
        self.send(self.endpoint(5, start=True, lag=1))
        self.send(self.endpoint(5, start=True, lag=12))
        self.send(self.endpoint(5, start=False, lag=25, probe=2))
        self.send(self.endpoint(31, start=True, probe=2))
        self.idle(9)
        self.send(self.endpoint(31, start=False, probe=1))
        self.cycle(Drive(flush=True))
        colliding = self.collide(2, 3)
        for tag in colliding:
            self.send(self.endpoint(tag, start=True, group=2), timeout=1024)
        self.send(self.endpoint(colliding[1], start=False, group=2, probe=2), timeout=1024)
        self.cycle(Drive(delta=200, timeout=1024))
        self.send(self.endpoint(colliding[2], start=True, group=2), timeout=5)
        self.idle(self.geometry.slots * self.geometry.sweep_period + 20, timeout=5)
        self.send(self.endpoint(81, start=True), timeout=2048)
        self.idle(12, timeout=2048, sink_ready=False)
        self.cycle(Drive(flush=True, sink_ready=False, timeout=2048))
        self.send(self.endpoint(81, start=False), timeout=2048)
        self.cycle(Drive(reset=True, sink_ready=False))

    def randomized(self, rng, steps):
        queue = deque()
        sequence = rng.randrange(1 << self.geometry.tag_width)
        scenarios = (
            (2, 10, 43),
            (1, 8, 128),
            (3, 14, 235),
        )
        for turn in range(steps):
            burst_limit, empty_limit, ttl = scenarios[(turn * len(scenarios)) // steps]
            if len(queue) < 18 and rng.randrange(12) < empty_limit:
                next_tag = sequence
                sequence = (sequence + 1) & ((1 << self.geometry.tag_width) - 1)
                group = rng.randrange(1 << self.geometry.class_width)
                start = self.endpoint(next_tag, start=True, group=group, lag=rng.randrange(1, 25),
                                      probe=rng.randrange(1 << self.geometry.probe_width))
                delta = rng.randrange(170)
                if rng.randrange(8) == 0:
                    delta = -delta
                stop = Observation(
                    (start.tick + delta) & LIMIT64, start.tag, start.event_class,
                    rng.randrange(1 << self.geometry.probe_width), True,
                )
                if modular_prior(self.tick, stop.tick):
                    stop = Observation(self.tick, stop.tag, stop.event_class, stop.probe, True)
                if rng.randrange(4) == 0:
                    start, stop = stop, start
                queue.append(start)
                if rng.randrange(12) >= burst_limit:
                    queue.append(stop)
                if rng.randrange(20) == 0:
                    queue.append(start)

            flush = turn % 811 == 0
            reset = turn % 2281 == 0 and turn > 0
            drive = Drive(
                observation=queue[0] if queue else None,
                sink_ready=rng.randrange(5) != 0,
                armed=rng.randrange(35) != 0,
                reset=reset,
                flush=flush,
                timeout=ttl,
                delta=rng.choice((1, 1, 1, 2, 4)),
            )
            ready = self.cycle(drive).ready
            if ready and queue:
                queue.popleft()

        while queue:
            self.send(queue.popleft(), timeout=128)
        self.idle(self.geometry.slots * self.geometry.sweep_period + 160, timeout=15)


def modular_prior(a, b):
    return ((a - b) & LIMIT64) >= HALF64


def run_campaign(args):
    profile = Geometry(index_width=args.index_width, scan_log=args.scan_log)
    with args.output.open("w", encoding="ascii") as output:
        campaign = Campaign(profile, output)
        campaign.directed()
        campaign.randomized(random.Random(args.seed), args.cycles)
    missing = set(Status) - campaign.counts.keys()
    if missing:
        raise RuntimeError(f"missing directed statuses: {sorted(status.name for status in missing)}")
    if not campaign.stall_count:
        raise RuntimeError("regression did not exercise output backpressure")
    print(f"Reference: {campaign.cycles} cycles, {campaign.stall_count} blocked replies")
    for status, count in sorted(campaign.counts.items()):
        print(f"  {status.name:12} {count:5}")


def histogram_campaign(args):
    rng = random.Random(args.seed)
    classes = 4
    bin_count = 12
    counter_bits = 6
    cases = [
        [[0, 1, 2, 3, 4, 7, 8, 15, 16, 31, 32, LIMIT64], [], [17, 20, 42], [6] * 70],
        [[(1 << power) - 1 for power in range(17)], [4, 4], [1, 2], []],
        [[rng.randrange(1 << rng.randrange(1, 14)) for _ in range(rng.randrange(16, 60))]
         for _ in range(classes)],
        [[], [], [], []],
    ]
    with args.output.open("w", encoding="ascii") as output:
        for event_no, batch in enumerate(cases):
            tick = (17 * event_no) + 65
            samples = [(group, value) for group, delays in enumerate(batch) for value in delays]
            rng.shuffle(samples)
            words = list(histogram_words(batch, bin_count, event_no, tick, counter_bits))
            print(len(samples), len(words), f"{tick:x}", file=output)
            for group, value in samples:
                print(group, f"{value:x}", file=output)
            for word in words:
                print(f"{word:032x}", file=output)
    print(f"Histogram reference: {len(cases)} snapshots, {sum(len(l) for b in cases for l in b)} samples")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("correlator", "histogram"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=11279)
    parser.add_argument("--cycles", type=int, default=2500)
    parser.add_argument("--index-width", type=int, default=3)
    parser.add_argument("--scan-log", type=int, default=3)
    args = parser.parse_args()
    if args.cycles < 1 or not 1 <= args.index_width <= 8 or not 1 <= args.scan_log <= 7:
        parser.error("invalid cycle, geometry or scan budget")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.suite == "histogram":
        histogram_campaign(args)
    else:
        run_campaign(args)


if __name__ == "__main__":
    main()
