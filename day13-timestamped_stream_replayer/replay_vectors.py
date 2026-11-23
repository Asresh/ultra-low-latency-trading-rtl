"""Generate directed and randomized ready/valid workloads for the RTL oracle."""

import argparse
from pathlib import Path
from random import Random

from replay_model import ReplayModel, load_commands
from replay_program import Beat, Command, Cycle, Operation, Packet, Status, Tape, U16, U32, U64


def command_words(command):
    """Return raw request fields in the simulator's fixed column order."""
    if command is None:
        return (0,) * 8
    return (
        command.op,
        command.tick,
        command.data,
        command.keep,
        int(command.last),
        command.tag,
        command.repeats,
        command.gap,
    )


def encode_cycle(cycle):
    """Encode one edge's reset, channel controls and request as hexadecimal fields."""
    words = (
        int(cycle.reset),
        int(cycle.cfg is not None),
        *command_words(cycle.cfg),
        int(cycle.rsp_ready),
        int(cycle.ready),
    )
    return " ".join(f"{word:x}" for word in words)


def decode_cycle(line):
    """Rebuild a simulator input cycle and reject noncanonical unused fields."""
    try:
        reset, valid, op, tick, data, keep, last, tag, repeats, gap, rr, mr = (
            int(word, 16) for word in line.split()
        )
    except ValueError as exc:
        raise ValueError("cycle row needs twelve hexadecimal fields") from exc
    if any(bit not in (0, 1) for bit in (reset, valid, last, rr, mr)):
        raise ValueError("cycle controls must be single bits")
    if not valid and any((op, tick, data, keep, last, tag, repeats, gap)):
        raise ValueError("idle configuration fields must be zero")
    command = Command(op, tick, data, keep, bool(last), tag, repeats, gap) if valid else None
    return Cycle(bool(reset), command, bool(rr), bool(mr))


def write_cyclestream(path, cycles):
    """Persist one deterministic simulator stimulus tape with a length header."""
    with path.open("w", encoding="ascii") as output:
        output.write(f"{len(cycles)}\n")
        for cycle in cycles:
            output.write(encode_cycle(cycle) + "\n")


def read_cyclestream(path):
    """Yield simulator input edges and check that the count header matches."""
    with path.open(encoding="ascii") as source:
        try:
            count = int(source.readline())
        except ValueError as exc:
            raise ValueError(f"{path}: invalid cycle count") from exc
        if count <= 0:
            raise ValueError(f"{path}: invalid cycle count")
        number = 0
        for number, line in enumerate(source, 1):
            if number > count:
                raise ValueError(f"{path}: more cycles than declared")
            try:
                yield decode_cycle(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{number+1}: {exc}") from exc
        if number != count:
            raise ValueError(f"{path}: short cycle tape")


class Campaign:
    """Append edges with a legal held request protocol and predictable coverage."""

    def __init__(self, depth, seed):
        """Reset the independent model and pin scheduler, starting with a reset edge."""
        self.model = ReplayModel(depth)
        self.rng = Random(seed)
        self.cycles = []
        self.results = []
        self.responses = set()
        self.edge(reset=True)

    def edge(self, command=None, *, reset=False, ready=True, rsp_ready=True):
        """Append the pins for one clock and record the transaction disposition."""
        cycle = Cycle(reset, command, rsp_ready, ready)
        trans = self.model.step(cycle)
        self.cycles.append(cycle)
        self.results.append(trans)
        if trans.response is not None:
            self.responses.add(trans.response.status)
        return trans

    def send(self, command, expected=None, *, ready=True, rsp_ready=True):
        """Keep a command asserted until accepted, then verify the oracle status."""
        result = self.edge(command, ready=ready, rsp_ready=rsp_ready)
        if result.request is None:
            for _ in range(20):
                result = self.edge(command, ready=ready, rsp_ready=True)
                if result.request is not None:
                    break
            else:
                raise AssertionError("request permanently blocked")
        if expected is not None and self.model.response.status != expected:
            raise AssertionError(f"{command}: {self.model.response.status.name} != {expected.name}")

    def idle(self, count=1, *, ready=True, rsp_ready=True):
        """Insert deterministic bubbles, optionally holding one or both output slots."""
        for _ in range(count):
            self.edge(ready=ready, rsp_ready=rsp_ready)

    def hold_output(self):
        """Wait until one beat is offered while keeping the sink blocked."""
        for _ in range(1000):
            if self.model.output is not None:
                return
            self.edge(ready=False)
        raise AssertionError("waiting for replay output exceeded watchdog")

    def drain(self):
        """Accept all offered beats until the run and final configuration response complete."""
        for _ in range(2000):
            if not self.model.active and self.model.response is None:
                return
            self.edge()
        raise AssertionError("replay did not drain within the vector watchdog")

    def load(self, tape):
        """Clear the program and append all validated events in chronological order."""
        tape.checked_depth(self.model.depth)
        for command in load_commands(tape):
            self.send(command, Status.OK)

    def simple_tape(self, distance):
        """Build a packet with a partial last beat plus a second packet when space permits."""
        depth = self.model.depth
        if depth == 1:
            return Tape.from_packets((Packet.paced(10, b"A", 0),))
        first = Packet.paced(10, b"Hello trace", 1, distance)
        packets = [first]
        if depth >= 3:
            packets.append(Packet.paced(11, b"next", first.end))
        return Tape.from_packets(packets)

    def invalid_programs(self):
        """Exercise command validation priority while keeping accepted tape words intact."""
        self.send(Command(7), Status.BAD_OP, rsp_ready=False)
        self.send(Command.start(), Status.EMPTY, rsp_ready=False)
        self.send(Command(Operation.PAUSE), Status.BAD_STATE)
        self.send(Command(Operation.RESUME), Status.BAD_STATE)
        self.send(Command(Operation.STOP), Status.BAD_STATE)
        self.send(Command(Operation.APPEND, 0, U64, 0, True, 1), Status.BAD_KEEP)
        self.send(Command(Operation.APPEND, 0, U64, 0x55, True, 1), Status.BAD_KEEP)
        self.send(Command(Operation.APPEND, 0, U64, 7, False, 1), Status.BAD_KEEP)
        self.send(Command(Operation.APPEND, 4, 0x24, 255, False, 1), Status.OK)
        self.send(Command.start(), Status.OPEN_PACKET)
        if self.model.depth >= 2:
            self.send(Command(Operation.APPEND, 3, U64, 1, True, 99), Status.BAD_TIME)
            self.send(Command(Operation.APPEND, 4, U64, 3, True, 99), Status.BAD_TAG)
            self.send(Command(Operation.APPEND, 4, U64, 1, True, 1), Status.OK)
        self.send(Command(Operation.CLEAR), Status.OK)
        for index in range(self.model.depth):
            self.send(Command.append(Beat.from_payload(index, bytes([index % 256]), U16-index, True)),
                      Status.OK)
        self.send(Command.append(Beat.from_payload(U32, b"!?", 1, True)), Status.FULL)
        self.send(Command.start(repeats=0), Status.BAD_REPEAT)
        self.send(Command.start(), Status.OK, ready=False)
        self.send(Command(Operation.CLEAR), Status.BUSY, ready=False)
        self.send(Command(Operation.APPEND, 0, 1, 1, True), Status.BUSY, ready=False)
        self.send(Command.start(), Status.BUSY, ready=False)
        self.drain()

    def pause_and_stop(self):
        """Pause a held offer, resume time, then stop without retracting a valid beat."""
        self.load(self.simple_tape(2))
        self.send(Command.start(repeats=4, gap=3), Status.OK, ready=False)
        self.hold_output()
        self.send(Command(Operation.PAUSE), Status.OK, ready=False)
        self.idle(9, ready=False, rsp_ready=False)
        self.send(Command(Operation.PAUSE), Status.BAD_STATE, ready=False, rsp_ready=False)
        self.send(Command(Operation.RESUME), Status.OK, ready=False)
        self.hold_output()
        self.send(Command(Operation.STOP), Status.OK, ready=False)
        self.send(Command(Operation.RESUME), Status.BAD_STATE, ready=False)
        self.idle(16, ready=False)
        self.drain()
        self.send(Command.start(repeats=3), Status.OK)
        self.drain()

    def extreme_ticks(self):
        """Program the full unsigned range and stop a run waiting for a far-off event."""
        self.load(Tape.from_packets([Packet.paced(11, b"later", U32)]))
        self.send(Command.start(repeats=U16, gap=U32), Status.OK)
        self.idle(19)
        self.send(Command(Operation.PAUSE), Status.OK)
        self.idle(19)
        self.send(Command(Operation.STOP), Status.OK)
        self.drain()

    def reset_in_flight(self):
        """Reset while output and configuration status are both held, then reuse the tape."""
        self.load(self.simple_tape(0))
        self.send(Command.start(repeats=4), Status.OK, ready=False)
        self.hold_output()
        self.send(Command(6), Status.BAD_OP, ready=False, rsp_ready=False)
        self.edge(reset=True, ready=False, rsp_ready=False)
        self.send(Command.start(), Status.EMPTY)
        self.load(Tape.from_packets([Packet.paced(50, b"new", 0)]))
        self.send(Command.start(), Status.OK)
        self.drain()

    def random_tape(self):
        """Generate complete packets until the next random packet would exhaust capacity."""
        remaining = self.rng.randrange(1, self.model.depth + 1)
        packets = []
        tick = self.rng.randrange(3)
        while remaining:
            beats = self.rng.randrange(1, min(remaining, 5) + 1)
            length = (beats - 1) * 8 + self.rng.randrange(1, 9)
            tag = self.rng.randrange(U16 + 1)
            payload = bytes(self.rng.getrandbits(8) for _ in range(length))
            offsets = []
            for _ in range(beats):
                tick += self.rng.choices((0, 1, 2, 4), (10, 5, 3, 1))[0]
                offsets.append(tick)
            packets.append(Packet(tag, payload, tuple(offsets)))
            remaining -= beats
        return Tape.from_packets(packets)

    def random_run(self):
        """Mix ready patterns, pause/resume, stop and bad programming during active playback."""
        self.load(self.random_tape())
        self.send(Command.start(self.rng.randrange(1, 5), self.rng.randrange(9)), Status.OK)
        pending = None
        for _ in range(350):
            if not self.model.active:
                break
            if pending is None:
                die = self.rng.randrange(100)
                if die < 5:
                    pending = Command(Operation.RESUME if self.model.paused else Operation.PAUSE)
                elif die < 6:
                    pending = Command(Operation.STOP)
                elif die < 10:
                    pending = Command(Operation.CLEAR)
                elif die < 13:
                    pending = Command(7)
            trans = self.edge(
                pending, ready=self.rng.randrange(4) != 0, rsp_ready=self.rng.randrange(5) != 0
            )
            if trans.request is not None:
                pending = None
        if pending is not None:
            self.send(pending)
        if self.model.paused:
            self.send(Command(Operation.RESUME))
        self.drain()

    def build(self, random_runs):
        """Run all directed campaigns and add the requested randomized runs."""
        self.invalid_programs()
        self.pause_and_stop()
        self.extreme_ticks()
        self.reset_in_flight()
        for _ in range(random_runs):
            self.random_run()
        self.idle(12)
        return tuple(self.cycles)


def main():
    """Generate a depth-specific pin-level stimulus tape."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--depth", type=int, default=16)
    parser.add_argument("--seed", type=int, default=13073)
    parser.add_argument("--random-runs", type=int, default=150)
    args = parser.parse_args()
    campaign = Campaign(args.depth, args.seed)
    cycles = campaign.build(args.random_runs)
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    write_cyclestream(args.destination, cycles)
    print(f"Wrote {len(cycles)} cycles at depth {args.depth} for seed {args.seed}.")


if __name__ == "__main__":
    main()
