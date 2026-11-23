"""Build independent, reproducible schedules for the RTL trace simulation."""


from __future__ import annotations
from dataclasses import dataclass, replace
from random import Random

from quote_model import Machine
from quote_schema import Code, Command, Drive, Op, Query, State, bounded, mask


@dataclass(frozen=True)
class Workload:
    name: str
    drives: tuple[Drive, ...]
    required_codes: frozenset[int] = frozenset()
    required_states: frozenset[int] = frozenset()
    minimum_cmd_stalls: int = 0
    minimum_query_stalls: int = 0
    minimum_parallel: int = 0
    exact_query_latency: int | None = None


class Builder:
    def __init__(self, profile):
        """Create a source scheduler with a modular tick and a reference for readiness."""
        self.profile = profile
        self.machine = Machine(profile)
        self.tick = 0
        self.command_tag = 0
        self.query_tag = 0
        self.tape = []

    def _tagged_command(self, command):
        """Assign tag values independently of payload sequence and instrument."""
        tagged = replace(command, tag=self.command_tag)
        self.command_tag = bounded(self.command_tag + 1, self.profile.tag_w)
        return tagged

    def _tagged_query(self, query):
        """Allocate query tags in a separate width-limited namespace."""
        tagged = replace(query, tag=self.query_tag)
        self.query_tag = bounded(self.query_tag + 1, self.profile.tag_w)
        return tagged

    def edge(self, *, command=None, query=None, ar=1, qr=1, reset=0, tick=None):
        """Append a physical edge, return expected handshake signals, and advance time."""
        if tick is not None:
            self.tick = bounded(tick, self.profile.time_w)
        driven = Drive(self.tick, reset, ar, qr, command, query)
        self.tape.append(driven)
        sample = self.machine.step(driven)
        self.tick = bounded(self.tick + 1, self.profile.time_w)
        return sample

    def startup(self, tick=0):
        """Initialize registers and command/query timing at an optional chosen clock tick."""
        self.edge(reset=1, tick=tick)
        self.edge(reset=1)

    def idle(self, cycles, *, ar=1, qr=1):
        """Advance a chosen number of clocks while retaining the requested sink readiness."""
        if cycles < 0:
            raise ValueError("idle cycles must not be negative")
        for _ in range(cycles):
            self.edge(ar=ar, qr=qr)

    def cmd(self, command, *, tick=None):
        """Offer a tagged command at a ready edge in a directed scenario."""
        sample = self.edge(command=self._tagged_command(command), tick=tick)
        if not sample.cmd_ready:
            raise RuntimeError("directed command issued at a busy edge")

    def query(self, query, *, tick=None):
        """Offer a tagged query at a ready edge in a directed scenario."""
        sample = self.edge(query=self._tagged_query(query), tick=tick)
        if not sample.query_ready:
            raise RuntimeError("directed query issued at a busy edge")

    def both(self, command, query, *, tick=None):
        """Consume both independent inputs on one edge and expose pre-update read behavior."""
        sample = self.edge(
            command=self._tagged_command(command), query=self._tagged_query(query),
            tick=tick,
        )
        if not (sample.cmd_ready and sample.query_ready):
            raise RuntimeError("both directed transactions should have transferred")

    def finish(self, name, **coverage):
        """Drain all outputs, then return immutable vectors and named coverage obligations."""
        for _ in range(3):
            self.edge()
        if not self.machine.drained():
            raise RuntimeError("unfinished outputs after directed drain")
        return Workload(name, tuple(self.tape), **coverage)


def _bid(price, qty=1):
    """Return a one-sided bid shaped as (bid tuple, absent ask tuple)."""
    return (price, qty), (0, 0)


def _ask(price, qty=1):
    """Return a one-sided ask shaped as (absent bid tuple, ask tuple)."""
    return (0, 0), (price, qty)


def rejection_tape(profile):
    """Put every documented rejection through an independently shaped wire update."""
    bench = Builder(profile)
    bench.startup()
    age = profile.time_half - 1
    bench.query(Query(0, age))
    bench.cmd(Command(7, profile.instruments, profile.venues))
    bench.cmd(Command(7, 0, profile.venues))
    bench.cmd(Command(7, 0, 0))
    bench.cmd(Command.venue_enabled(0, 0, False))
    bench.cmd(Command.replace(0, 0, 0, (7, 0), (0, 0)))
    bench.cmd(Command.venue_enabled(0, 0, True))
    bench.cmd(Command.replace(0, 0, 0, (7, 0), (0, 0)))
    bench.cmd(Command.replace(0, 0, 0, (0, 2), (0, 0)))
    bench.cmd(Command.replace(0, 0, 0, (0, 0), (0, 0)))
    bench.cmd(Command.replace(0, 0, 0, (8, 2), (7, 1)))
    bench.cmd(Command.replace(0, 0, 0, (9, 2), (9, 1)))
    bench.cmd(Command.withdraw(0, 0, 0))
    bench.cmd(Command.withdraw(0, 0, profile.seq_half))
    bench.query(Query(0, age))
    bench.query(Query(profile.instruments, profile.time_half))
    bench.query(Query(0, profile.time_half))
    bench.cmd(Command.withdraw(0, 0, 1))
    bench.query(Query(0, age))
    return bench.finish(
        "reject", required_codes=frozenset(Code),
        required_states=frozenset((State.EMPTY, State.LOCKED)),
    )


def state_tape(profile):
    """Exercise every achievable market state plus switches between equal-price venues."""
    bench = Builder(profile)
    bench.startup()
    age = profile.time_half - 1
    q = Query(0, age)
    bench.query(q)
    bench.cmd(Command.replace(0, 0, 1, *_bid(10)))
    bench.query(q)
    bench.cmd(Command.replace(0, 0, 2, *_ask(14)))
    bench.query(q)
    bench.cmd(Command.replace(0, 0, 3, (10, 1), (14, 4)))
    bench.query(q)
    required = {State.EMPTY, State.BID_ONLY, State.ASK_ONLY, State.OPEN, State.LOCKED}
    if profile.venues > 1:
        bench.cmd(Command.replace(0, 1, 1, (10, min(9, profile.qty_max)), (14, 6)))
        bench.query(q)
        bench.cmd(Command.replace(0, 1, 2, *_bid(14, 9)))
        bench.query(q)
        bench.cmd(Command.replace(0, 1, 3, *_bid(15, 9)))
        bench.query(q)
        bench.cmd(Command.venue_enabled(0, 1, False))
        bench.query(q)
        bench.cmd(Command.venue_enabled(0, 1, True))
        bench.cmd(Command.replace(0, 1, 3, *_bid(16)))
        bench.query(q)
        bench.cmd(Command.replace(0, 1, 4, *_bid(16)))
        bench.query(q)
        bench.cmd(Command.withdraw(0, 1, 5))
        bench.query(q)
        required.add(State.CROSSED)
    else:
        bench.cmd(Command.replace(0, 0, 4, (10, 2), (10, 8)))
        bench.query(q)
    if profile.instruments > 1:
        bench.query(Query(1, age))
        bench.cmd(Command.replace(1, 0, 3, *_bid(19)))
        bench.query(Query(1, age))
        bench.query(q)
    return bench.finish(
        "states", required_states=frozenset(required),
        required_codes=frozenset({Code.OK, Code.BAD_SEQ} if profile.venues > 1 else {Code.OK}),
    )


def wrap_tape(profile):
    """Pin inclusive freshness at the boundary and test modular sequence and clock wrap."""
    bench = Builder(profile)
    tick_max = mask(profile.time_w)
    seq_max = mask(profile.seq_w)
    bench.startup(tick_max - 5)
    bench.cmd(Command.replace(0, 0, seq_max - 1, (9, 2), (30, 4)), tick=tick_max - 2)
    bench.query(Query(0, 2), tick=tick_max)
    bench.query(Query(0, 3), tick=0)
    bench.query(Query(0, 3), tick=1)
    bench.query(Query(0, profile.time_half - 1), tick=profile.time_half - 2)
    bench.cmd(Command.replace(0, 0, seq_max, (10, 2), (21, 4)))
    bench.cmd(Command.replace(0, 0, 0, (11, 2), (22, 4)))
    bench.cmd(Command.replace(0, 0, seq_max, (5, 1), (6, 1)))
    bench.cmd(Command.withdraw(0, 0, profile.seq_half))
    bench.query(Query(0, 0))
    bench.both(Command.replace(0, 0, 1, (8, 2), (13, 4)), Query(0, 0))
    bench.query(Query(0, 1))
    bench.cmd(Command.withdraw(0, 0, 2))
    bench.query(Query(0, 7))
    bench.cmd(Command.replace(0, 0, 1, (15, 2), (29, 4)))
    bench.edge(reset=1, tick=0)
    bench.cmd(Command.replace(0, 0, 1, (15, 2), (29, 4)))
    bench.query(Query(0, 1))
    return bench.finish(
        "wrap", required_codes=frozenset((Code.OK, Code.BAD_SEQ)),
        required_states=frozenset((State.EMPTY, State.OPEN)), minimum_parallel=1,
    )


def stall_tape(profile):
    """Fill both paths, mutate the book while outputs wait, then exercise reset cancellation."""
    bench = Builder(profile)
    bench.startup()
    profile_age = profile.time_half - 1
    q = Query(0, profile_age)
    bench.cmd(Command.replace(0, 0, 0, (7, 1), (16, 3)))
    bench.edge(query=bench._tagged_query(q), qr=0)
    bench.edge(query=bench._tagged_query(q), qr=0)
    hold_q = bench._tagged_query(q)
    new = bench._tagged_command(Command.replace(0, 0, 1, (9, 2), (16, 3)))
    bench.edge(command=new, query=hold_q, ar=0, qr=0)
    hold_cmd = bench._tagged_command(Command.withdraw(0, 0, 2))
    for _ in range(9):
        bench.edge(command=hold_cmd, query=hold_q, ar=0, qr=0)
    bench.edge(command=hold_cmd, query=hold_q, ar=1, qr=1)
    bench.query(q)
    bench.idle(3)
    bench.edge(command=bench._tagged_command(new), query=bench._tagged_query(q), ar=0, qr=0)
    bench.idle(2, ar=0, qr=0)
    bench.edge(reset=1)
    bench.query(q)
    return bench.finish(
        "stall", required_codes=frozenset((Code.OK,)),
        required_states=frozenset((State.EMPTY, State.OPEN)),
        minimum_cmd_stalls=9, minimum_query_stalls=9, minimum_parallel=2,
    )



def price_extremes_tape(profile):
    """Preserve the two largest signed spread magnitudes and maximum forwarded quantities."""
    bench = Builder(profile)
    bench.startup()
    full = profile.price_max
    bulk = profile.qty_max
    request = Query(0, profile.time_half - 1)
    bench.cmd(Command.replace(0, 0, 0, (1, bulk), (full, bulk)))
    bench.query(request)
    bench.cmd(Command.replace(0, 0, 1, (full, bulk), (full, bulk)))
    bench.query(request)
    requirements = {State.OPEN, State.LOCKED, State.EMPTY}
    if profile.venues > 1:
        bench.cmd(Command.replace(0, 1, 0, *_ask(1, bulk)))
        bench.query(request)
        bench.cmd(Command.withdraw(0, 1, 1))
        bench.query(request)
        requirements.add(State.CROSSED)
    bench.cmd(Command.withdraw(0, 0, 2))
    bench.query(request)
    return bench.finish(
        "extremes", required_codes=frozenset((Code.OK,)),
        required_states=frozenset(requirements),
    )


def benchmark_tape(profile, cycles=160):
    """Offer one legal command and one query on every unstalled measurement edge."""
    bench = Builder(profile)
    bench.startup()
    age = profile.time_half - 1
    sequences = {}
    for step in range(cycles):
        instrument = step % profile.instruments
        venue = (step // profile.instruments) % profile.venues
        key = (instrument, venue)
        sequence = sequences.get(key, -1) + 1
        sequences[key] = sequence
        lower = 8 + (step % 10)
        upper = 28 + (step % 8)
        bench.both(
            Command.replace(
                instrument, venue, bounded(sequence, profile.seq_w),
                (lower, 1), (upper, 2),
            ), Query(instrument, age),
        )
    return bench.finish(
        "benchmark", minimum_parallel=cycles, exact_query_latency=2,
        required_codes=frozenset((Code.OK,)), required_states=frozenset((State.EMPTY, State.OPEN)),
    )


def _shape(rng, profile):
    """Generate valid or faulty side tuples emphasizing representable edge prices."""
    mode = rng.randrange(19)
    width = profile.price_max
    qty = rng.choice((1, profile.qty_max, rng.randrange(1, profile.qty_max + 1)))
    if mode == 0:
        return (0, 0), (0, 0)
    if mode == 1:
        return (rng.randint(1, width), 0), (0, 0)
    if mode == 2:
        return (0, qty), (0, 0)
    if mode == 3:
        return (width, qty), (1, qty)
    if mode <= 5:
        return _bid(rng.choice((1, width, rng.randint(1, width))), qty)
    if mode <= 7:
        return _ask(rng.choice((1, width, rng.randint(1, width))), qty)
    bid = rng.choice((1, width, rng.randint(1, width), rng.randint(95, min(110, width))))
    ask = rng.randrange(bid, width + 1)
    if mode <= 12:
        ask = bid
    return (bid, qty), (ask, rng.randint(1, profile.qty_max))


def _next_command(rng, bench):
    """Choose a real or invalid address and bias serial data toward the latest stored value."""
    profile = bench.profile
    instrument = rng.randrange(profile.instruments)
    venue = rng.randrange(profile.venues)
    last = bench.machine.book.status(instrument, venue).last_sequence
    if last is None:
        raw_sequence = rng.randrange(mask(profile.seq_w) + 1)
    else:
        step = rng.choice((-2, -1, 0, 1, 1, 1, 2, profile.seq_half - 1, profile.seq_half))
        raw_sequence = bounded(last + step, profile.seq_w)
    op = rng.choice((0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 4))
    if rng.randrange(21) == 0:
        instrument = profile.instruments
    if rng.randrange(19) == 0:
        venue = profile.venues
    bid, ask = _shape(rng, profile)
    update = Command.replace(instrument, venue, raw_sequence, bid, ask)
    return bench._tagged_command(replace(update, op=op))


def _next_query(rng, bench):
    """Generate request ages at zero, common values, complete half range, and invalid bounds."""
    profile = bench.profile
    instrument = rng.randrange(profile.instruments + int(rng.randrange(12) == 0))
    age = rng.choice((0, 1, 2, 4, 7, profile.time_half - 1, profile.time_half))
    return bench._tagged_query(Query(instrument, age))


def random_tape(profile, cycles, seed):
    """Mix continuous read/write traffic with bursts of stalls and intermittent reset."""
    rng = Random(seed)
    bench = Builder(profile)
    bench.startup(mask(profile.time_w) - 3)
    command = None
    query = None
    for step in range(cycles):
        if step % 127 == 126:
            bench.edge(reset=1, command=command, query=query)
            command = None
            query = None
            continue
        if command is None and rng.randrange(8) != 0:
            command = _next_command(rng, bench)
        if query is None and rng.randrange(9) != 0:
            query = _next_query(rng, bench)
        ar = int(step % 27 >= 10 and rng.randrange(10) != 0)
        qr = int(step % 33 >= 13 and rng.randrange(10) != 0)
        tick = None
        if step % 87 == 0:
            tick = bench.tick + profile.time_half
        sample = bench.edge(command=command, query=query, ar=ar, qr=qr, tick=tick)
        if sample.cmd_ready:
            command = None
        if sample.query_ready:
            query = None
    while command is not None or query is not None:
        sample = bench.edge(command=command, query=query)
        if sample.cmd_ready:
            command = None
        if sample.query_ready:
            query = None
    return bench.finish(
        f"random-{seed}", minimum_cmd_stalls=100, minimum_query_stalls=100,
        minimum_parallel=30,
    )


def all_workloads(profile, cycles=1800, seeds=(11, 71)):
    """Generate named directed workloads and a deterministic tape for each seed."""
    yield rejection_tape(profile)
    yield state_tape(profile)
    yield wrap_tape(profile)
    yield stall_tape(profile)
    yield price_extremes_tape(profile)
    yield benchmark_tape(profile)
    for seed in seeds:
        yield random_tape(profile, cycles, seed)
