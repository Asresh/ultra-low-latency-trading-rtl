"""Generate and compare partitioned drop-copy incidents and recovery traces."""

from __future__ import annotations

import argparse
import random
from collections import Counter, deque
from dataclasses import replace
from itertools import zip_longest
from pathlib import Path

from check_drop_copy import REFERENCE, read_rows, write_rows
from drop_copy_model import Key, Lane, Payload, Status
from drop_copy_partition_model import BAD_NAMESPACE, PORTS, Input, Observation, PartitionModel, Restart


class Scenario:
    def __init__(self, namespaces: int, sets: int):
        self.model = PartitionModel(namespaces, sets)
        self.inputs = []
        self.outputs = []
        self.coverage = Counter()

    def step(self, **signals) -> tuple:
        request = Input(**signals)
        pins = self.model.advance(request)
        self.inputs.append(request.row())
        self.outputs.append(pins)
        if request.reset:
            self.coverage["reset"] += 1
        if pins[3] and request.ready:
            self.coverage[f"status_{status_name(pins[5])}"] += 1
        if pins[3] and not request.ready:
            self.coverage["held_report"] += 1
        fires = [bool(data and ready) for data, ready in
                 zip((request.primary, request.copy, request.restart), pins[:3])]
        valid_parallel = fires[0] and fires[1] and (
            request.primary.namespace < self.model.count and request.copy.namespace < self.model.count
        )
        if valid_parallel:
            self.coverage["parallel_lanes"] += 1
        if valid_parallel and fires[2] and request.restart.namespace < self.model.count:
            self.coverage["parallel_lanes_and_restart"] += 1
        if pins[-2]:
            self.coverage["incident_visible"] += 1
        return pins

    def idle(self, count: int = 1, ready: bool = True) -> None:
        for _ in range(count):
            self.step(ready=ready)

    def wait_accept(self, port: str, payload) -> tuple:
        ready_at = {"primary": 0, "copy": 1, "restart": 2}[port]
        for _ in range(64):
            pins = self.step(**{port: payload})
            if pins[ready_at]:
                return pins
        raise AssertionError(f"held source {port} did not drain")

    def write(self, output: Path) -> None:
        output.mkdir(exist_ok=True, parents=True)
        write_rows(output / "inputs.hex", self.inputs)
        write_rows(output / "expected.hex", self.outputs)


def status_name(value: int) -> str:
    return "BAD_NAMESPACE" if value == BAD_NAMESPACE else Status(value).name


def incident(s: Scenario) -> None:
    """Prove identical normalized keys can be in separate recovery domains."""
    n = s.model.count
    original = Key(0, 4, 7, 28)
    revised = replace(REFERENCE, quantity=61)
    s.step(reset=True)
    s.idle()

    if n == 1:
        s.step(primary=Observation(0, (original, REFERENCE)), ready=False)
    else:
        result = s.step(primary=Observation(0, (original, REFERENCE)),
                        copy=Observation(1, (original, REFERENCE)), ready=False)
        assert result[:2] == (True, True)
        assert all(s.model.ledgers[i].pending() == 1 for i in (0, 1))

    if n >= 3:
        s.step(primary=Observation(2, (original, REFERENCE)), ready=False)
    s.idle(3, ready=False)
    held = s.step(copy=Observation(0, (original, revised)))
    assert (held[1], *held[3:6]) == (True, 1, 0, Status.STAGED)
    assert s.model.ledgers[0].report.status == Status.PAIR_CONFLICT
    if n > 1:
        next_report = s.step(primary=Observation(1, (original, REFERENCE)))
        assert (next_report[0], *next_report[3:6], next_report[-2]) == (
            True, 1, 1, Status.STAGED, 1,
        )
        assert s.model.ledgers[1].report.status == Status.MATCH
    s.idle(n + 2)
    held_incident = s.wait_accept("restart", Restart(0, 0))
    assert held_incident[-2] == 1
    s.idle()
    new_epoch = s.wait_accept("restart", Restart(0, 2))
    assert new_epoch[-2] == 1
    dropped = s.step()
    assert (dropped[4], dropped[5], dropped[13], dropped[-2]) == (0, Status.RESTART, 2, 0)

    s.wait_accept("primary", Observation(0, (original, REFERENCE)))
    s.idle()
    assert s.model.ledgers[0].counts["rejected"] == 2
    if n > 1:
        s.wait_accept("copy", Observation(1, (original, REFERENCE)))
        s.idle()
        assert s.model.ledgers[1].counts["duplicate"] == 1
        assert s.model.ledgers[1].epoch == 0
    if n >= 3:
        # The control flushes only namespace 2. Other namespaces accept on
        # the very same edge with independently checked epoch identities.
        distinct = s.step(
            primary=Observation(0, (replace(original, epoch=2, execution=100), REFERENCE)),
            copy=Observation(1, (replace(original, execution=101), REFERENCE)),
            restart=Restart(2, 1),
        )
        assert distinct[:3] == (True, True, True)
        assert s.model.ledgers[2].report.status == Status.RESTART
        assert s.model.ledgers[2].report.flushed == 1
        assert s.model.ledgers[0].pending() == s.model.ledgers[1].pending() == 1
        s.idle(n + 3)
    s.idle()


def bad_addresses(s: Scenario) -> None:
    """Merge invalid primary/copy and management reports without table writes."""
    n = s.model.count
    key = Key(23, 2, 4, 9)
    malformed = Observation(n, (key, REFERENCE))
    restart = Restart(n, 91)
    start_epochs = [row.epoch for row in s.model.ledgers]
    accepted = s.step(primary=malformed, copy=malformed, restart=restart, ready=False)
    assert accepted[:3] == (False, False, True)
    for _ in range(2):
        assert s.step(primary=malformed, copy=malformed, ready=False)[:2] == (False, False)
    accepted_both = [False, False]
    for _ in range(n + 4):
        pins = s.step(primary=None if accepted_both[0] else malformed,
                      copy=None if accepted_both[1] else malformed)
        if pins[3] and pins[5] == BAD_NAMESPACE:
            assert pins[4] == n
            if pins[6] == Lane.EPOCH:
                assert pins[7] == Key(restart.epoch, 0, 0, 0).packed()
                assert pins[9] == 0
            else:
                assert pins[7] == key.packed() and pins[9] == REFERENCE.packed()
        for index in range(2):
            accepted_both[index] = accepted_both[index] or pins[index]
    assert all(accepted_both)
    assert s.model.bad_count == 3
    assert [row.epoch for row in s.model.ledgers] == start_epochs


def random_workload(s: Scenario, seed: int, cycles: int) -> None:
    """Offer paired and stale events while sink stalls and resets are interleaved."""
    rng = random.Random(seed)
    s.step(reset=True)
    n = s.model.count
    queues = [deque(), deque()]
    held = [None, None]
    control = None
    records = deque(maxlen=60)
    number = 500
    for cycle in range(cycles + 512):
        producing = cycle < cycles
        if producing and cycle and cycle % 401 == 0:
            s.step(reset=True)
            for queue in queues:
                queue.clear()
            records.clear()
            held = [None, None]
            control = None
            continue

        if producing and rng.randrange(4) == 0:
            namespace = rng.randrange(n + 1) if rng.randrange(10) == 0 else rng.randrange(n)
            epoch = s.model.ledgers[namespace].epoch if namespace < n else rng.randrange(1 << 16)
            key = Key(epoch, rng.randrange(8), rng.getrandbits(16), number, rng.randrange(2))
            number += 1
            payload = Payload(rng.getrandbits(32), rng.randrange(1, 1 << 24),
                              rng.getrandbits(32), rng.randrange(2))
            lane = rng.randrange(2)
            first = Observation(namespace, (key, payload))
            queues[lane].append((cycle, first))
            records.append(first)
            if rng.randrange(7):
                mate = Observation(namespace, (key, payload if rng.randrange(4) else
                                               replace(payload, side=1 - payload.side)))
                queues[1-lane].append((cycle + rng.randrange(30), mate))
        if producing and records and rng.randrange(8) == 0:
            queues[rng.randrange(2)].append((cycle, rng.choice(records)))
        if producing and control is None and rng.randrange(37) == 0:
            namespace = rng.randrange(n+1)
            epoch = s.model.ledgers[namespace].epoch if namespace < n else 0
            control = Restart(namespace, epoch + rng.choice((0, 1, 1, 1)))

        for lane, queue in enumerate(queues):
            if held[lane] is None and queue and queue[0][0] <= cycle:
                _, held[lane] = queue.popleft()
        if not producing and not any(held) and not any(queues) and control is None:
            break
        pins = s.step(primary=held[0], copy=held[1], restart=control,
                      ready=not producing or (cycle % 48 < 36 and rng.randrange(6) != 0))
        for lane in range(2):
            if pins[lane]:
                held[lane] = None
        if pins[2]:
            control = None
    else:
        raise AssertionError("queued partition input did not drain")
    s.idle(24 + (2 * s.model.sets + 1) * s.model.count)


def compare(output: Path) -> None:
    expected = read_rows(output / "expected.hex", len(PORTS))
    observed = read_rows(output / "observed.hex", len(PORTS))
    drive = read_rows(output / "inputs.hex", 13)
    reports = Counter()
    for cycle, (want, actual, inp) in enumerate(zip_longest(expected, observed, drive)):
        if want is None or actual is None or inp is None:
            raise AssertionError("trace length disagrees with driver")
        if want != actual:
            details = ", ".join(f"{pin}=0x{a:x}/0x{b:x}" for pin,a,b in zip(PORTS,actual,want) if a != b)
            raise AssertionError(f"cycle {cycle} actual/expected {details}; driver={inp}")
        if actual[3] and inp[1]:
            reports[status_name(actual[5])] += 1
    print(f"PASS {len(observed)} exact partition samples, {sum(reports.values())} transferred reports")
    print("  " + " ".join(f"{name}={count}" for name, count in sorted(reports.items())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("output", type=Path)
    gen.add_argument("--namespaces", type=int, default=3)
    gen.add_argument("--sets", type=int, default=4)
    gen.add_argument("--seed", type=int, default=0xDCC017)
    gen.add_argument("--cycles", type=int, default=2400)
    sub.add_parser("check").add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "check":
        compare(args.output)
        return
    if not 1 <= args.namespaces <= 8 or not 0 <= args.cycles <= 100_000:
        parser.error("require 1..8 namespaces and 0..100,000 random cycles")
    s = Scenario(args.namespaces, args.sets)
    incident(s)
    bad_addresses(s)
    if args.cycles:
        random_workload(s, args.seed, args.cycles)
        required = {"held_report", "incident_visible", "status_BAD_NAMESPACE",
                    "status_MATCH", "status_PAIR_CONFLICT", "status_MISSING_COPY",
                    "status_MISSING_PRIMARY", "status_RESTART", "status_STALE_EPOCH"}
        if args.namespaces > 1:
            required.add("parallel_lanes")
        if args.namespaces > 2:
            required.add("parallel_lanes_and_restart")
        if args.cycles >= 2400:
            missed = sorted(key for key in required if s.coverage[key] == 0)
            if missed:
                raise AssertionError(f"missing coverage {missed}")
    s.write(args.output)
    print("  " + " ".join(f"{key}={value}" for key, value in sorted(s.coverage.items())))


if __name__ == "__main__":
    main()
