"""Deterministic workloads for pin-level comparison and trace experiments."""

import random
from collections import Counter

from limiter_model import CycleModel
from limiter_protocol import Command, Drive, Operation


class Schedule:
    """Drive a legal ready/valid source and predict each sampled clock edge."""

    def __init__(self, geometry):
        """Keep full wire rows so failures can be replayed without RNG state."""
        self.model = CycleModel(geometry)
        self.inputs = []
        self.expected = []
        self.accepted = []
        self.next_tag = 0
        self.request_stalls = 0

    @property
    def geometry(self):
        """Expose the model's modular tick and field-size helpers."""
        return self.model.geometry

    def tag(self):
        """Allocate a bounded tag and permit reuse after modulo wrap."""
        value = self.next_tag
        self.next_tag = (value + 1) & self.geometry.tag_max
        return value

    def edge(self, drive):
        """Record a single edge, returning the predicted input handshake."""
        observation = self.model.step(drive)
        self.inputs.append(drive)
        self.expected.append(observation)
        if observation.output_valid and not drive.ready and not drive.reset:
            self.request_stalls += int(bool(drive.valid))
        accepted = bool(drive.valid and observation.input_ready)
        if accepted:
            self.accepted.append((len(self.inputs) - 1, drive.command))
        return accepted

    def reset(self, cycles=1, ready=0):
        """Cancel a pending response and clear every row for these ticks."""
        for _ in range(cycles):
            self.edge(Drive(reset=1, ready=ready))

    def idle(self, cycles=1, ready=1):
        """Tick the datapath without a new command, holding the sink if requested."""
        for _ in range(cycles):
            self.edge(Drive(ready=ready))

    def send(self, command, *, wait=0):
        """Hold input fields fixed until accepted under a finite sink stall."""
        for _ in range(wait):
            if self.edge(Drive(valid=1, ready=0, command=command)):
                self.idle(ready=0)
                return
        if not self.edge(Drive(valid=1, ready=1, command=command)):
            raise AssertionError("ready sink could not accept a command")

    def configure(self, key, tick, cap, rate, **timing):
        """Create a full bucket at a possibly wrapping reference tick."""
        self.send(Command.configure(key, self.geometry.wrapped(tick), cap, rate, self.tag()), **timing)

    def offer(self, key, tick, amount, **timing):
        """Send an offer with a deterministic tag for post-run correlation."""
        command = Command.offer(key, self.geometry.wrapped(tick), amount, self.tag())
        self.send(command, **timing)

    def inspect(self, key, tick, **timing):
        """Expose the matching key's balance at the selected logical tick."""
        self.send(Command.inspect(key, self.geometry.wrapped(tick), self.tag()), **timing)

    def clear(self, key, tick, **timing):
        """Reset counters at the next eligible per-key logical tick."""
        self.send(Command.clear(key, self.geometry.wrapped(tick), self.tag()), **timing)

    def finish(self):
        """Drain any last decision and verify the stream has fully quiesced."""
        self.idle(2)
        if self.model.outbox is not None:
            raise AssertionError("cannot finish a schedule with a pending response")
        return self


def boundary_cases(geometry):
    """Cover modular wrap, zero rate, each error class, and mid-stream reset."""
    schedule = Schedule(geometry)
    schedule.reset(2)
    capacity = min(7, geometry.token_max)
    rate = 1
    tick = geometry.tick_mask - 2
    schedule.inspect(0, tick)
    schedule.offer(0, tick, 1, wait=4)
    schedule.configure(0, tick, capacity, rate)
    schedule.offer(0, tick, 0)
    schedule.offer(0, tick, capacity)
    schedule.offer(0, tick, 1, wait=3)
    schedule.offer(0, tick + 1, 1)
    schedule.inspect(0, tick + 3)
    schedule.offer(0, tick + 4, 3)
    schedule.offer(0, tick + 3, 1)
    schedule.inspect(0, tick + 4 + geometry.half)
    schedule.inspect(0, tick + 4 + geometry.half - 1)
    schedule.clear(0, tick + 4 + geometry.half - 1)
    schedule.configure(0, 0, capacity, 0)
    schedule.offer(0, 0, capacity)
    schedule.offer(0, geometry.half - 1, 1)
    schedule.send(Command.disable(0, geometry.tick_mask, schedule.tag()))
    schedule.offer(0, geometry.tick_mask, 1)
    schedule.clear(0, 0)
    if geometry.keys < (1 << geometry.key_w):
        schedule.offer(geometry.keys, 0, 1)
    schedule.send(Command(Operation.CONFIG, 0, 0, 0, 1, 1, schedule.tag()))
    schedule.send(Command(Operation.CONFIG, 0, 0, 1, 0, 0, schedule.tag()))
    schedule.send(Command(Operation.INSPECT, 0, 0, 1, 0, 0, schedule.tag()))
    schedule.send(Command(Operation.CLEAR, 0, 0, 0, 0, 1, schedule.tag()))
    schedule.configure(0, 2, capacity, capacity)
    if geometry.keys > 1:
        schedule.configure(1, 15, capacity, 0)
        schedule.offer(1, 15, capacity // 2)
    schedule.offer(0, 2, capacity)
    schedule.reset(1, ready=1)
    schedule.offer(0, 3, 1)
    schedule.inspect(0, 4)
    return schedule.finish()


def saturated_counters(geometry):
    """Drive each counter to its exact maximum and then a lossy update."""
    schedule = Schedule(geometry)
    # Running more than 65,000 rows just to saturate a wide profile is
    # unnecessary. The small profile explicitly covers overflow of all counters.
    n = min(geometry.count_max, 511)
    large_cost = min(geometry.token_max, 111)
    schedule.configure(0, 0, large_cost, large_cost)
    tick = 0
    for item in range(n + 2):
        schedule.offer(0, tick, large_cost, wait=item % 9 == 0)
        schedule.offer(0, tick, 1)
        tick += 1
    schedule.inspect(0, tick)
    schedule.clear(0, tick)
    schedule.offer(0, tick, 1)
    schedule.inspect(0, tick)
    schedule.send(Command.disable(0, geometry.wrapped(tick), schedule.tag()))
    schedule.inspect(0, tick)
    return schedule.finish()


def parallel_burst(geometry, length=256):
    """Sustain one request a cycle while interleaving independent keys."""
    schedule = Schedule(geometry)
    cap = min(geometry.token_max, 32)
    for key in range(geometry.keys):
        schedule.configure(key, 0, cap, min(key + 1, cap))
    for number in range(length):
        key = number % geometry.keys
        tick = number // geometry.keys
        amount = (number % cap) + 1
        schedule.offer(key, tick, amount)
    for key in reversed(range(geometry.keys)):
        schedule.inspect(key, length // geometry.keys)
    return schedule.finish()


def mixed_command(schedule, generator, key):
    """Choose one valid or malformed operation near the key's time boundary."""
    geometry = schedule.geometry
    previous = schedule.model.policy.bucket(key)
    selector = generator.randrange(100)
    if selector < 8:
        capacity = generator.randint(1, geometry.token_max)
        rate = generator.randint(0, capacity)
        return Command.configure(
            key, generator.randrange(1 << geometry.tick_w), capacity, rate, schedule.tag()
        )
    if selector < 12:
        tick = generator.randrange(1 << geometry.tick_w)
        return Command.disable(key, tick, schedule.tag())
    if selector < 16:
        tick = generator.randrange(1 << geometry.tick_w)
        return Command(Operation.OFFER, key, tick, tag=schedule.tag())
    if selector < 22:
        delta = generator.choice((geometry.half, geometry.half + 1, geometry.tick_mask))
    else:
        delta = generator.choice((0, 0, 1, min(5, geometry.half - 1), geometry.half - 1))
    tick = geometry.wrapped(previous.anchor + delta)
    if selector < 29:
        return Command.clear(key, tick, schedule.tag())
    if selector < 40:
        return Command.inspect(key, tick, schedule.tag())
    cost = generator.choice(
        (1, geometry.token_max, max(1, previous.balance),
         min(geometry.token_max, previous.balance + 1),
         generator.randint(1, geometry.token_max))
    )
    return Command.offer(key, tick, cost, schedule.tag())


def random_traffic(geometry, seed, operations):
    """Exercise many legal and deliberately invalid updates with source stalls."""
    generator = random.Random(seed)
    schedule = Schedule(geometry)
    schedule.reset()
    for key in range(geometry.keys):
        cap = generator.randint(1, geometry.token_max)
        schedule.configure(key, generator.randrange(geometry.tick_mask + 1), cap, cap // 3)
    for index in range(operations):
        if index and generator.randrange(200) == 0:
            schedule.reset(cycles=generator.randint(1, 2), ready=generator.randrange(2))
        if generator.randrange(4) == 0:
            schedule.idle(cycles=generator.randrange(3), ready=generator.randrange(2))
        possible_keys = (1 << geometry.key_w) if index % 19 == 0 else geometry.keys
        key = generator.randrange(possible_keys)
        command = mixed_command(schedule, generator, key)
        wait = generator.randrange(7) if generator.randrange(3) == 0 else 0
        schedule.send(command, wait=wait)
    for key in range(geometry.keys):
        schedule.inspect(key, schedule.model.policy.bucket(key).anchor)
    return schedule.finish()


def scenarios(geometry, seed, operations):
    """Yield named runs across arithmetic boundaries and mixed bus traffic."""
    for name, schedule in (
        ("boundary", boundary_cases(geometry)),
        ("saturation", saturated_counters(geometry)),
        ("burst", parallel_burst(geometry)),
        ("random", random_traffic(geometry, seed, operations)),
    ):
        yield name, schedule


def accepted_statuses(schedule):
    """Count all newly formed results, including ones intentionally reset away."""
    counts = Counter()
    replay = CycleModel(schedule.geometry)
    for drive in schedule.inputs:
        observation = replay.step(drive)
        if drive.valid and observation.input_ready:
            counts[replay.outbox.status] += 1
    return counts
