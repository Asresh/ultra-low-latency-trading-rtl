"""Byte-queue oracle and traces for the cut-through teaching variant."""

import argparse
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import random

from width_model import Beat, full, to_beats
from width_workload import encode, payload


@dataclass(frozen=True)
class Edge:
    incoming: Beat
    valid: bool
    ready: bool
    reset: bool = False


@dataclass(frozen=True)
class View:
    ready: bool
    outgoing: Beat
    valid: bool
    occupancy: int


class QueueOracle:
    """An ordered byte queue that retains packet identity and boundaries."""

    def __init__(self, iw, ow, capacity):
        """Require room for both a maximum input beat and a partial output."""
        if min(iw, ow) < 1 or capacity < iw + ow:
            raise ValueError("streaming capacity must cover both complete widths")
        self.iw = iw
        self.ow = ow
        self.capacity = capacity
        self.data = deque()

    def candidate(self):
        """Form a complete beat if enough bytes or a visible packet end exist."""
        ready = []
        ending = False
        for byte, tag, last in self.data:
            ready.append((byte, tag))
            if last or len(ready) == self.ow:
                ending = last
                break
        if not ready or (len(ready) < self.ow and not ending):
            return Beat()
        if any(tag != ready[0][1] for byte, tag in ready):
            raise ValueError("upstream changed tag before ending packet")
        packed = int.from_bytes(bytes(byte for byte, tag in ready), "little")
        return Beat(packed, full(len(ready)), ending, ready[0][1])

    def advance(self, edge):
        """Check pre-edge occupancy before committing push and pop atomically."""
        value = edge.incoming
        selected = value.bytes(self.iw)
        output = Beat() if edge.reset else self.candidate()
        ready = not edge.reset and 0 < len(selected) <= self.capacity - len(self.data)
        view = View(ready, output, bool(output.keep), len(self.data))
        if edge.reset:
            self.data.clear()
            return view
        if edge.ready and view.valid:
            for _ in range(len(output.bytes(self.ow))):
                self.data.popleft()
        if edge.valid and ready:
            if value.keep != full(len(selected)) or (not value.last and len(selected) < self.iw):
                raise ValueError("cut-through input must already be valid")
            for index, byte in enumerate(selected):
                self.data.append((byte, value.tag, value.last and index == len(selected)-1))
        return view


def scenario(iw, ow, capacity, seed, packets):
    """Drive accepted beats continuously while covering resets and bank stalls."""
    rng = random.Random(seed)
    fifo = QueueOracle(iw, ow, capacity)
    yield Edge(Beat(), False, False, True), View(False, Beat(), False, 0)
    groups = [list(to_beats(payload(index, rng.randint(1, 80)), iw, index, 255))
              for index in range(packets)]
    beats = deque(beat for group in groups for beat in group)
    hold = None
    for cycle in range(200000):
        if hold is None and beats and rng.randrange(9):
            hold = beats.popleft()
        active = hold is not None
        edge = Edge(hold or Beat(keep=full(iw)), active, cycle > 20 and rng.randrange(5) < 3)
        view = fifo.advance(edge)
        yield edge, view
        if active and view.ready:
            hold = None
        if hold is None and not beats and not fifo.data:
            return
    raise RuntimeError("test never drained its finite stream")


def write_plan(directory, profile, iw, ow, capacity, seed, packets=160):
    """Persist the independent cycle schedule in host-readable hexadecimal."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"stream_{profile}.txt"
    total = 0
    with path.open("w", encoding="ascii") as file:
        for edge, view in scenario(iw, ow, capacity, seed, packets):
            offered = edge.incoming
            expected = view.outgoing
            words = (
                edge.reset, edge.valid, offered.data, offered.keep, offered.last, offered.tag,
                edge.ready, view.ready, view.valid, expected.data, expected.keep,
                expected.last, expected.tag, view.occupancy,
            )
            file.write(encode(words) + "\n")
            total += 1
    return total


def main():
    """Create the standard and incommensurate lane-width cut-through tapes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    for name, iw, ow, depth, seed in (("up", 4, 8, 12, 8801), ("odd", 5, 3, 11, 11005)):
        print(f"{name} streaming edges: {write_plan(args.directory, name, iw, ow, depth, seed)}")


if __name__ == "__main__":
    main()
