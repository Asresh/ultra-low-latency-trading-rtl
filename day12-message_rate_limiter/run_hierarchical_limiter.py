"""Generate checked multi-scope admission tapes and run the real RTL."""

import argparse
from collections import Counter, deque
from dataclasses import replace
from pathlib import Path
import random
import subprocess

from hierarchical_model import Command, Geometry, Hierarchy, Op, Status


PROFILES = {
    "default": Geometry(),
    "odd": Geometry(5, 9, 9, 7, 5, 16),
    "tiny": Geometry(1, 2, 4, 5, 2, 10),
}


def setup(g, tick):
    """Install independent scopes and alternate risk-control and ordinary sessions."""
    rows = [Command(Op.CONFIG_PORT, tick=tick, amount=25, rate=3, reserve=9, enable=True)]
    rows.extend(
        Command(Op.CONFIG_GROUP, group=group, tick=tick, amount=17, rate=2, reserve=6, enable=True)
        for group in range(g.groups)
    )
    rows.extend(
        Command(
            Op.CONFIG_SESSION, session=key, group=(key//2) % g.groups, tick=tick,
            amount=14, rate=2, enable=True, critical=key % 2 != 0,
        )
        for key in range(g.sessions)
    )
    return rows


def directed(g, tick):
    """Pin reserve isolation, policy rotation, timestamp rollover, and error precedence."""
    mask = (1 << g.tick_w)-1
    commands = setup(g, tick)
    commands.extend((
        Command(Op.OFFER, tick=tick, amount=11),
        Command(Op.OFFER, tick=tick, amount=1),
        Command(Op.OFFER, session=1, tick=tick, amount=6),
        Command(Op.OFFER, session=1, tick=tick, amount=1),
        Command(Op.INSPECT_PORT, tick=tick),
        Command(Op.INSPECT_GROUP, tick=tick, group=0),
        Command(Op.INSPECT_SESSION, tick=tick, session=1),
        Command(Op.OFFER, tick=(tick+3) & mask, amount=5),
        Command(Op.INSPECT_GROUP, tick=tick, group=0),
        Command(Op.OFFER, tick=tick, amount=1),
        Command(Op.OFFER, session=g.sessions, tick=tick, amount=0),
        Command(Op.CONFIG_GROUP, group=g.groups, tick=tick, amount=1, enable=True),
        Command(Op.CONFIG_SESSION, session=0, group=g.groups, tick=tick, amount=1, enable=True),
        Command(Op.OFFER, tick=(tick+3) & mask, amount=1, critical=True),
        Command(Op.CONFIG_GROUP, tick=(tick+4) & mask),
        Command(Op.OFFER, session=1, tick=(tick+4) & mask, amount=1),
        Command(Op.CONFIG_GROUP, tick=(tick+4) & mask, amount=31, rate=0, reserve=0, enable=True),
        Command(Op.CONFIG_PORT, tick=(tick+4) & mask, amount=31, rate=0, reserve=0, enable=True),
        Command(Op.CONFIG_SESSION, tick=(tick+4) & mask, amount=31, rate=0, enable=True, critical=True),
    ))
    # Force would-be overflow in both admission and rejection counters in the tiny profile.
    width = min(g.count_w, 5)
    if width == g.count_w:
        commands.extend(
            Command(Op.OFFER, tick=(tick+4) & mask, amount=1)
            for _ in range((1 << width)+2)
        )
        commands.extend(
            Command(Op.OFFER, tick=(tick+4) & mask, amount=31)
            for _ in range((1 << width)+2)
        )
    return commands


def workload(g, rng, count, tick):
    """Mix valid traffic, time faults, malformed fields, and policy changes."""
    last = tick
    tick_mask, token_mask = (1 << g.tick_w)-1, (1 << g.token_w)-1
    for _ in range(count):
        last = (last+rng.choices((0,1,2,3,7), (45,20,15,10,10))[0]) & tick_mask
        op = rng.choices(tuple(Op), (2,4,6,3,3,4,78))[0]
        command = Command(op, tick=last)
        if op in (Op.OFFER, Op.CONFIG_SESSION, Op.INSPECT_SESSION):
            command = replace(command, session=rng.randrange(g.sessions))
        if op in (Op.CONFIG_GROUP, Op.INSPECT_GROUP):
            command = replace(command, group=rng.randrange(g.groups))
        if op in (Op.CONFIG_PORT, Op.CONFIG_GROUP, Op.CONFIG_SESSION) and rng.randrange(6):
            capacity = rng.randrange(1, token_mask+1)
            command = replace(command, enable=True, amount=capacity, rate=rng.randrange(capacity+1))
            if op == Op.CONFIG_SESSION:
                command = replace(command, group=rng.randrange(g.groups), critical=bool(rng.randrange(2)))
            else:
                command = replace(command, reserve=rng.randrange(capacity+1))
        if op == Op.OFFER:
            command = replace(command, amount=rng.randint(1,min(token_mask,24)))
        if rng.randrange(9) == 0:
            command = replace(command, tick=(last-(1 << (g.tick_w-1))) & tick_mask)
        if rng.randrange(16) == 0:
            command = replace(command, reserve=rng.randint(1,token_mask), critical=True)
        yield command


def build_tape(g, seed, count, destination):
    """Predict every handshake from an independent software response slot."""
    rng = random.Random(seed)
    model = Hierarchy(g)
    tick_start = (1 << g.tick_w)-2
    chunks = [directed(g, tick_start)]
    for index in range(4):
        tick = (11*index) & ((1 << g.tick_w)-1)
        chunks.append(setup(g, tick)+list(workload(g, rng, count//4, tick)))
    counters = Counter()
    active = None
    sequence = 0

    def edge(tape, reset=False, ready=True, offer=None):
        nonlocal active, model
        valid = active is not None and not reset
        can_accept = not reset and (not valid or ready)
        bits = active.pack(g) if valid else 0
        tape.write(
            f"{int(reset)} {int(ready)} {int(offer is not None)} "
            f"{offer.pack(g) if offer else 0:x} {int(can_accept)} {int(valid)} {bits:x}\n"
        )
        if reset:
            counters["reset_flush"] += active is not None
            active = None
            model = Hierarchy(g)
        elif can_accept:
            active = model.apply(offer) if offer else None
            if active:
                counters[active.status.name] += 1
                if active.status == Status.LIMITED:
                    counters[f"limited_{active.detail:03b}"] += 1
        if valid and not ready:
            counters["stall"] += 1
        counters["cycle"] += 1
        return can_accept

    with destination.open("w", encoding="ascii") as tape:
        for index, chunk in enumerate(chunks if count else chunks[:1]):
            edge(tape, reset=True)
            ready_queue = deque(chunk)
            while ready_queue:
                command = replace(ready_queue[0], tag=sequence & ((1 << g.tag_w)-1))
                # Leave the beginning deterministic to make the demo VCD useful.
                ready = True if index == 0 else rng.randrange(10) > 2
                if edge(tape, ready=ready, offer=command):
                    ready_queue.popleft()
                    sequence += 1
            edge(tape)
            edge(tape)
        if count:
            command = Command(Op.INSPECT_PORT, tick=0, tag=sequence & ((1 << g.tag_w)-1))
            edge(tape, offer=command)
            edge(tape, ready=False, offer=replace(command, op=Op.INSPECT_SESSION))
            edge(tape, reset=True)
            edge(tape)
    if set(Status.__members__) - set(counters):
        raise AssertionError(f"missing required status coverage: {counters}")
    return counters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iverilog", default="iverilog")
    parser.add_argument("--vvp", default="vvp")
    parser.add_argument("--seed", type=int, default=12091)
    parser.add_argument("--commands", type=int, default=1800)
    parser.add_argument("--profile", choices=PROFILES, action="append")
    parser.add_argument("--wave", type=Path)
    args = parser.parse_args()
    profiles = args.profile or PROFILES
    if args.commands < 0:
        parser.error("--commands cannot be negative")
    if args.wave and len(profiles) != 1:
        parser.error("a waveform requires exactly one profile")
    for profile in profiles:
        geometry = PROFILES[profile]
        target = Path("build") / "hierarchy" / profile
        target.mkdir(parents=True, exist_ok=True)
        tape, binary = target / "cycles.txt", target / "hierarchy.vvp"
        metrics = build_tape(geometry, args.seed, args.commands, tape)
        top = "tb_hierarchical_rate_limiter"
        command = [args.iverilog, "-g2012", "-s", top, "-o", str(binary)]
        command.extend(f"-P{top}.{name.upper()}={value}" for name,value in vars(geometry).items())
        command.extend(("-f", "rtl_hierarchical.f", f"{top}.sv"))
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode:
            raise SystemExit(result.stderr or result.stdout)
        command = [args.vvp, str(binary), f"+tape={tape}"]
        if args.wave:
            args.wave.parent.mkdir(parents=True, exist_ok=True)
            command.append(f"+wave={args.wave}")
        subprocess.run(command, check=True)
        print(f"{profile}: {dict(metrics)}", flush=True)


if __name__ == "__main__":
    main()
