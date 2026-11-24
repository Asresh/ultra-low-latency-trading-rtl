"""Drive directed and randomized RTL workloads and compare every sampled pin."""

import argparse
from collections import Counter, deque
from dataclasses import replace
from pathlib import Path
import random

from drop_copy_model import Inputs, Lane, Key, Oracle, Payload, PIN_NAMES, Status


REFERENCE = Payload(0x405, 60, 125_050, 0)


class Scenario:
    """Build cycles and maintain expected pin and report records."""

    def __init__(self, sets: int, wait: int, history: int):
        """Start a fresh scenario with a modeled and seeded reset."""
        self.model = Oracle(sets, wait, history)
        self.inputs = []
        self.outputs = []
        self.generated = Counter()

    def cycle(self, **signals) -> tuple:
        """Record one cycle and account for a newly produced report."""
        inputs = Inputs(**signals)
        prior = self.model.report
        pins = self.model.advance(inputs)
        self.inputs.append(inputs.row())
        self.outputs.append(pins)
        if self.model.report is not None and self.model.report is not prior:
            self.generated[self.model.report.status] += 1
        return pins

    def idle(self, count: int = 1) -> None:
        """Accept reports and advance time without offering an input."""
        for _ in range(count):
            self.cycle()

    def send(self, lane: Lane, key: Key, payload: Payload = REFERENCE,
             *, expect: Status = None, stalls: int = 0):
        """Keep one lane asserted until accepted and optionally check its exact status."""
        field = "primary" if lane == Lane.PRIMARY else "copy"
        for _ in range(stalls):
            assert not self.cycle(ready=False, **{field: (key, payload)})[int(lane)]
        for _ in range(self.model.sets * 2 + 2):
            if self.cycle(**{field: (key, payload)})[int(lane)]:
                result = self.model.report
                if expect is not None:
                    assert result.status == expect, (expect, result)
                return result
        raise AssertionError("valid source failed to handshake after sweep drained")

    def epoch(self, number: int, expected: Status, flushed: int = 0) -> None:
        """Apply a restart and check both status and its abandoned pending count."""
        assert self.cycle(epoch=number)[2]
        assert self.model.report.status == expected
        assert self.model.report.flushed == flushed

    def write(self, output: Path) -> None:
        """Write input and expectation files with readable hex values."""
        output.mkdir(parents=True, exist_ok=True)
        write_rows(output / "inputs.hex", self.inputs)
        write_rows(output / "expected.hex", self.outputs)


def write_rows(path: Path, rows) -> None:
    """Serialize integer fields on one line per sample, with a row count header."""
    with path.open("w") as output:
        print(len(rows), file=output)
        for fields in rows:
            print(" ".join(f"{int(value):x}" for value in fields), file=output)


def read_rows(path: Path, columns: int) -> list:
    """Decode checked-size diagnostic files and report a precise malformed row."""
    with path.open() as source:
        count = int(source.readline())
        rows = []
        for line_number, line in enumerate(source, start=2):
            words = line.split()
            if len(words) != columns:
                raise ValueError(f"{path}:{line_number}: expected {columns} fields")
            try:
                rows.append(tuple(int(word, 16) for word in words))
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: invalid hexadecimal field") from exc
    if len(rows) != count:
        raise ValueError(f"{path}: header claims {count} samples, found {len(rows)}")
    return rows


def pick_collisions(epoch: int, sets: int) -> list:
    """Pick three distinct executions whose keys route to the same set."""
    candidates = [Key(epoch, 4, 9, number) for number in range(1, 1000)]
    target = candidates[0].bucket(sets)
    return [key for key in candidates if key.bucket(sets) == target][:3]


def directed(scenario: Scenario) -> None:
    """Hit every report type with directed assertions, including clock wrap and timeout."""
    s = scenario
    s.cycle(reset=True)
    s.idle()
    first, second, third = pick_collisions(0, s.model.sets)
    s.send(Lane.PRIMARY, first, expect=Status.STAGED)
    result = s.send(Lane.COPY, first, expect=Status.MATCH, stalls=3)
    assert result.age == 4
    s.send(Lane.PRIMARY, first, expect=Status.DUPLICATE)
    changed = replace(REFERENCE, quantity=61, price=125_100)
    result = s.send(Lane.COPY, first, changed, expect=Status.DUP_CONFLICT)
    assert result.diff == 6
    s.send(Lane.COPY, second, expect=Status.STAGED)
    result = s.send(Lane.PRIMARY, second, changed, expect=Status.PAIR_CONFLICT)
    assert (result.expected, result.observed, result.diff) == (REFERENCE, changed, 6)
    s.send(Lane.PRIMARY, second, changed, expect=Status.DUPLICATE)
    s.send(Lane.PRIMARY, third, expect=Status.FULL)
    s.epoch(1, Status.RESTART)
    s.send(Lane.COPY, first, expect=Status.STALE_EPOCH)
    s.send(Lane.COPY, replace(first, epoch=1, execution=0), expect=Status.BAD_EVENT)
    s.send(Lane.COPY, replace(first, epoch=1, kind=3), expect=Status.BAD_EVENT)
    s.send(Lane.COPY, replace(first, epoch=1), replace(REFERENCE, quantity=0),
           expect=Status.BAD_EVENT)
    s.epoch(1, Status.BAD_RESTART)

    primary_only, copy_only, fresh_key = pick_collisions(1, s.model.sets)
    s.send(Lane.PRIMARY, primary_only, expect=Status.STAGED)
    s.send(Lane.COPY, copy_only, expect=Status.STAGED)
    assert s.model.tick < 40
    for _ in range(s.model.wait - 2):
        s.cycle(ready=False)
    assert s.model.due()[0] == primary_only
    pending_copy = (primary_only, REFERENCE)
    assert not s.cycle(copy=pending_copy, ready=False)[Lane.COPY]
    assert not s.cycle(copy=pending_copy, ready=True)[Lane.COPY]
    assert s.model.report.status == Status.MISSING_COPY
    assert s.model.report.age == s.model.wait + 1
    assert not s.cycle(copy=pending_copy)[Lane.COPY]
    assert s.model.report.status == Status.MISSING_PRIMARY
    assert s.cycle(copy=pending_copy)[Lane.COPY]
    assert s.model.report.status == Status.LATE
    s.send(Lane.PRIMARY, fresh_key, expect=Status.FULL)
    s.idle(s.model.history)
    s.send(Lane.PRIMARY, fresh_key, expect=Status.STAGED)
    s.cycle(epoch=2, ready=False)
    s.cycle(epoch=2, ready=False)
    s.epoch(2, Status.RESTART, flushed=1)
    s.send(Lane.COPY, fresh_key, expect=Status.STALE_EPOCH)
    s.epoch(1, Status.BAD_RESTART)

    # Simultaneous valid lanes should alternate; neither producer may change until accepted.
    a = (replace(first, epoch=2, kind=1), REFERENCE)
    b = (replace(first, epoch=2, session=10), REFERENCE)
    assert s.cycle(primary=a, copy=b)[:2] == (True, False)
    assert s.cycle(copy=b)[:2] == (False, True)
    s.send(Lane.COPY, a[0], expect=Status.MATCH)
    s.send(Lane.PRIMARY, b[0], expect=Status.MATCH)
    s.epoch(0xFFFF, Status.RESTART)
    s.epoch(0, Status.BAD_RESTART)
    s.idle()
    assert set(s.generated) == set(Status), s.generated
    s.cycle(reset=True)
    s.idle(14)
    assert s.model.tick == 0xFFFFFFFE
    s.send(Lane.PRIMARY, first, expect=Status.STAGED)
    s.idle()
    result = s.send(Lane.COPY, first, expect=Status.MATCH)
    assert result.age == 2 and s.model.tick == 1
    s.idle(s.model.history - 2)
    result = s.send(Lane.COPY, first, expect=Status.DUPLICATE)
    assert result.age == s.model.history - 1
    s.send(Lane.COPY, first, expect=Status.STAGED)
    s.epoch(1, Status.RESTART, flushed=1)
    s.idle()


def mixed(s: Scenario, seed: int, cycles: int) -> None:
    """Exercise seeded valid-hold traffic with mixed pair delays, duplicate and bad rows."""
    rng = random.Random(seed)
    queues = [deque(), deque()]
    history = []
    cursor = 400
    config = None
    held = [None, None]
    ready_burst = 0
    for count in range(cycles):
        epoch = s.model.epoch
        if config is None and rng.randrange(250) == 0:
            config = epoch + 1
        if len(queues[0]) + len(queues[1]) < 256 and rng.randrange(5) == 0:
            key = Key(epoch, rng.randrange(8), rng.randrange(8), cursor, rng.randrange(2))
            payload = Payload(rng.getrandbits(32), rng.randrange(1, 1 << 24),
                              rng.getrandbits(32), rng.randrange(2))
            cursor += 1
            first_lane = rng.randrange(2)
            queues[first_lane].append((count, (key, payload)))
            if rng.randrange(7):
                second = (key, payload if rng.randrange(4) else replace(
                    payload, quantity=1, side=1 - payload.side))
                queues[1 - first_lane].append((count + rng.randrange(s.model.wait + 6), second))
            history.append((key, payload))
        if history and rng.randrange(9) == 0:
            # Prefer recent events but retain a few stale epochs across restarts.
            event = rng.choice(history[-24:])
            lane = rng.randrange(2)
            if len(queues[lane]) < 180:
                queues[lane].append((count, event))
        for lane in range(2):
            if held[lane] is None and queues[lane] and queues[lane][0][0] <= count:
                _, held[lane] = queues[lane].popleft()
        if ready_burst == 0 and rng.randrange(30) == 0:
            ready_burst = rng.randrange(1, 12)
        ready = ready_burst == 0
        ready_burst = max(0, ready_burst - 1)
        p_ready, c_ready, config_ready, *_ = s.cycle(
            primary=held[0], copy=held[1], epoch=config, ready=ready,
        )
        if p_ready:
            held[0] = None
        if c_ready:
            held[1] = None
        if config_ready:
            config = None
    last_due = max((at for queue in queues for at, _ in queue), default=cycles)
    for count in range(cycles, last_due + 512):
        for lane in range(2):
            if held[lane] is None and queues[lane] and queues[lane][0][0] <= count:
                _, held[lane] = queues[lane].popleft()
        if not any(held) and not any(queues) and config is None:
            break
        p_ready, c_ready, config_ready, *_ = s.cycle(
            primary=held[0], copy=held[1], epoch=config,
        )
        if p_ready:
            held[0] = None
        if c_ready:
            held[1] = None
        if config_ready:
            config = None
    else:
        raise AssertionError("random source queues did not drain")
    s.idle(s.model.wait + 2 * s.model.sets + 1)


def compare(output: Path) -> int:
    """Compare all observed pins and print a compact status coverage summary."""
    expected = read_rows(output / "expected.hex", len(PIN_NAMES))
    seen = read_rows(output / "observed.hex", len(PIN_NAMES))
    if len(seen) != len(expected):
        raise AssertionError(f"expected {len(expected)} samples; simulation emitted {len(seen)}")
    for cycle, (actual, reference) in enumerate(zip(seen, expected)):
        failures = [f"{name}=0x{a:x} expected 0x{b:x}" for name, a, b
                    in zip(PIN_NAMES, actual, reference) if a != b]
        if failures:
            raise AssertionError(f"cycle {cycle}: " + ", ".join(failures))
    stimulus = read_rows(output / "inputs.hex", 10)
    reports = Counter(Status(row[4]).name for inputs, row in zip(stimulus, seen)
                      if row[3] and inputs[-1] and not inputs[0])
    print(f"PASS {len(seen)} exact pin samples, {sum(reports.values())} accepted reports")
    print("  " + ", ".join(f"{status}={count}" for status, count in sorted(reports.items())))
    return len(seen)


def main() -> None:
    """Generate a deterministic scenario or compare a completed simulation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "check"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--sets", type=int, default=4)
    parser.add_argument("--cycles", type=int, default=6000)
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=0xD20C)
    args = parser.parse_args()
    if args.action == "generate":
        scenario = Scenario(args.sets, 24, 40)
        directed(scenario)
        mixed(scenario, args.seed, args.cycles)
        scenario.write(args.output)
    else:
        compare(args.output)


if __name__ == "__main__":
    main()
