"""Seeded market vectors, recovery fault tapes and latency workloads."""

from dataclasses import dataclass, replace
from random import Random
from typing import Dict, Iterable, List, Optional, Set

from market_reference import (
    Command, Config, Instrument, MarketMode, Operation, Statistic, Status,
)


@dataclass(frozen=True)
class Driver:
    reset: bool = False
    tick: bool = False
    response_ready: bool = True
    quote_ready: bool = True

    def line(self) -> str:
        """Format source control and command fields in one hardware driver row."""
        return " ".join(str(int(value)) for value in (
            self.reset, self.tick, self.response_ready, self.quote_ready,
        ))


@dataclass(frozen=True)
class Beat:
    command: Command
    gap: int = 0

    def line(self) -> str:
        """Format one bus command exactly as the scheduled SV bench consumes it."""
        return " ".join(str(value) for value in (*self.command.values(), self.gap))


@dataclass
class Scenario:
    name: str
    commands: List[Beat]
    schedule: List[Driver]
    coverage: Set[str]


class Tape:
    def __init__(self, config: Config, name: str):
        """Initialize a deterministic stimulus stream and campaign metadata."""
        self.config = config
        self.scenario = Scenario(name, [], [Driver(reset=True)] * 2, set())
        self.instruments = [Instrument(config) for _ in range(config.banks)]

    def append(self, cmd: Command, gap: int = 0, expected: Optional[Status] = None) -> Status:
        """Add a new scheduled drive cycle to the current scenario."""
        cfg = self.config
        cmd = replace(cmd, tag=(len(self.scenario.commands) & cfg.mask(cfg.tag_width)))
        cmd.validate_width(cfg)
        if gap < 0:
            raise ValueError("gap must be nonnegative")
        self.scenario.commands.append(Beat(cmd, gap))
        if cmd.instrument < cfg.banks:
            status = self.instruments[cmd.instrument].process(cmd).response.status
        else:
            status = Status.BANK
        if expected is not None and status != expected:
            raise AssertionError(f"directed vector unexpectedly got {status} instead of {expected}: {cmd}")
        return Status(status)

    def send(
        self, bank: int, op: int, *, order_id: int = 0, new_id: int = 0,
        side: bool = False, price: int = 0, quantity: int = 0, stat_index: int = 0,
        sequence: Optional[int] = None, gap: int = 0, expected: Optional[Status] = None,
    ) -> Status:
        """Issue a command with the requested sink behavior and optional idle gap."""
        if sequence is None:
            sequence = self.instruments[bank].expected if bank < self.config.banks else 0
        return self.append(
            Command(bank, op, sequence, order_id, new_id, side, price, quantity, stat_index),
            gap=gap, expected=expected,
        )

    def level(self, bank: int, side: bool, price: int) -> None:
        """Schedule a per-price query for checking aggregate depth."""
        self.send(bank, Operation.LEVEL, side=side, price=price, expected=Status.OK)

    def counters(self, bank: int) -> None:
        """Schedule statistic reads on the chosen instrument."""
        for counter in Statistic:
            self.send(bank, Operation.STAT, stat_index=counter, expected=Status.OK)

    def inspect(self, bank: int) -> None:
        """Add a read-only state probe to the current tape."""
        self.send(bank, Operation.BBO, expected=Status.OK)
        for order_id in range(self.config.slots):
            self.send(bank, Operation.ORDER, order_id=order_id, expected=Status.OK)
        for side in (False, True):
            for price in range(self.config.levels):
                self.level(bank, side, price)

    def start(self, bank: int, sequence: int = 0, gap: int = 0) -> None:
        """Begin a new cycle schedule from the prescribed reset condition."""
        self.send(bank, Operation.SNAP_BEGIN, sequence=sequence, gap=gap, expected=Status.OK)

    def seed_bank(
        self, bank: int, *, sequence: int = 0, gap: int = 0,
        bid_size: Optional[int] = None, ask_size: Optional[int] = None,
    ) -> None:
        """Load a test book through the same snapshot command path exposed to the DUT."""
        cfg = self.config
        self.start(bank, sequence=sequence, gap=gap)
        self.send(
            bank, Operation.SNAP_ADD, order_id=0, side=False, price=0,
            quantity=bid_size if bid_size is not None else min(cfg.quantity_max, 11), expected=Status.OK,
        )
        self.send(
            bank, Operation.SNAP_ADD, order_id=1, side=True, price=cfg.levels - 1,
            quantity=ask_size if ask_size is not None else min(cfg.quantity_max, 7), expected=Status.OK,
        )
        self.send(bank, Operation.SNAP_END, expected=Status.OK)

    def complete(self) -> Scenario:
        """Add enough idle cycles to let outstanding diagnostics and quotes drain."""
        return self.scenario


def basic_changes(tape: Tape, bank: int) -> None:
    """Exercise additions, replacements, cancels, and executions in a coherent book."""
    cfg = tape.config
    max_price = cfg.levels - 1
    tape.seed_bank(bank, sequence=cfg.sequence_max - 1)
    tape.send(bank, Operation.HEARTBEAT, expected=Status.OK)
    tape.send(bank, Operation.BBO, expected=Status.OK)
    tape.send(bank, Operation.CANCEL, order_id=0, quantity=2, expected=Status.OK)
    tape.send(bank, Operation.EXECUTE, order_id=1, quantity=3, expected=Status.OK)
    tape.level(bank, True, max_price)

    tape.send(
        bank, Operation.REPLACE, order_id=0, new_id=2, side=True, price=0,
        quantity=min(cfg.quantity_max, 8), expected=Status.OK,
    )
    tape.level(bank, False, 0)
    tape.send(bank, Operation.ORDER, order_id=0, expected=Status.OK)
    tape.send(bank, Operation.ORDER, order_id=2, expected=Status.OK)
    tape.send(
        bank, Operation.REPLACE, order_id=2, new_id=0, side=True, price=max_price,
        quantity=3, expected=Status.OK,
    )
    tape.send(bank, Operation.BBO, expected=Status.OK)
    tape.send(
        bank, Operation.REPLACE, order_id=0, new_id=2, price=max_price - 1,
        quantity=2, expected=Status.OK,
    )
    tape.send(bank, Operation.EXECUTE, order_id=2, quantity=2, expected=Status.OK)
    tape.send(bank, Operation.DELETE, order_id=1, expected=Status.OK)
    tape.send(bank, Operation.BBO, expected=Status.OK)

    for order_id in range(cfg.slots):
        tape.send(
            bank, Operation.ADD, order_id=order_id, side=False, price=max_price,
            quantity=cfg.quantity_max, expected=Status.OK,
        )
    tape.level(bank, False, max_price)
    for order_id in reversed(range(cfg.slots)):
        tape.send(bank, Operation.DELETE, order_id=order_id, expected=Status.OK)
    tape.level(bank, False, max_price)
    tape.counters(bank)


def fault_and_recover(tape: Tape, bank: int) -> None:
    """Trigger a sequence fault and restore the instrument from a new snapshot."""
    cfg = tape.config
    tape.send(bank, Operation.HEARTBEAT, expected=Status.MODE)
    tape.start(bank, cfg.sequence_max)
    tape.send(bank, Operation.ADD, order_id=0, quantity=1, expected=Status.MODE)
    tape.send(bank, Operation.SNAP_ADD, sequence=1, order_id=0, quantity=1, expected=Status.GAP)
    tape.send(bank, Operation.BBO, expected=Status.OK)
    tape.seed_bank(bank, sequence=3)
    tape.send(bank, Operation.SNAP_END, expected=Status.MODE)
    tape.send(bank, Operation.HEARTBEAT, expected=Status.OK)

    errors = (
        (Operation.ADD, Status.ID, dict(order_id=cfg.slots, quantity=1)),
        (Operation.ADD, Status.PRICE, dict(order_id=2, price=cfg.levels, quantity=1)),
        (Operation.ADD, Status.QTY, dict(order_id=2, quantity=0)),
        (Operation.ADD, Status.DUPLICATE, dict(order_id=0, quantity=1)),
        (Operation.DELETE, Status.ABSENT, dict(order_id=2)),
        (Operation.CANCEL, Status.QTY, dict(order_id=0, quantity=11)),
        (Operation.EXECUTE, Status.QTY, dict(order_id=1, quantity=8)),
        (Operation.REPLACE, Status.REPLACE_ID, dict(order_id=0, new_id=0, quantity=2)),
        (Operation.REPLACE, Status.DUPLICATE, dict(order_id=0, new_id=1, quantity=2)),
        (Operation.REPLACE, Status.ID, dict(order_id=0, new_id=cfg.slots, quantity=2)),
        (14, Status.CODE, {}),
    )
    for index, (op, error, payload) in enumerate(errors):
        tape.send(bank, op, expected=error, **payload)
        tape.send(bank, Operation.HEARTBEAT, expected=Status.MODE)
        tape.send(bank, Operation.BBO, expected=Status.OK)
        tape.seed_bank(bank, sequence=(index * 17 + 1) & cfg.sequence_max)

    tape.send(bank, Operation.LEVEL, price=cfg.levels, expected=Status.PRICE)
    tape.send(bank, Operation.ORDER, order_id=cfg.slots, expected=Status.ID)
    tape.send(bank, Operation.STAT, stat_index=15, expected=Status.STAT)
    tape.send(bank, Operation.HEARTBEAT, expected=Status.OK)
    tape.counters(bank)


def semantic_faults(cfg: Config) -> Scenario:
    """Send illegal order mutations that should fence the affected instrument."""
    tape = Tape(cfg, "faults")
    for bank in range(cfg.banks):
        fault_and_recover(tape, bank)
        basic_changes(tape, bank)
    for bank in range(cfg.banks, cfg.instrument_max + 1):
        tape.send(bank, Operation.SNAP_BEGIN, expected=Status.BANK)
        tape.send(bank, Operation.BBO, expected=Status.BANK)
    tape.scenario.coverage.update(("all_ops", "all_statuses", "boot_wrap", "crossed", "read_counters"))
    if cfg.quote_size:
        tape.scenario.coverage.add("quotes")
    return tape.complete()


def remaining_legal_ops(tape: Tape, bank: int) -> Dict[str, List[int]]:
    """Cover optional valid operations omitted by the basic scenario."""
    instrument = tape.instruments[bank]
    live = list(instrument.book.orders)
    available = [key for key in range(tape.config.slots) if key not in instrument.book.orders]
    return {
        "live": live,
        "free": available,
        "smaller": [key for key in live if instrument.book.orders[key].size > 1],
    }


def random_query(tape: Tape, rng: Random, bank: int) -> None:
    """Pick a diagnostic command and index from the legal test geometry."""
    cfg = tape.config
    op = rng.choice((Operation.ORDER, Operation.LEVEL, Operation.BBO, Operation.STAT))
    tape.send(
        bank, op, order_id=rng.randint(0, cfg.order_id_max),
        side=bool(rng.getrandbits(1)), price=rng.randint(0, cfg.price_max),
        stat_index=rng.randrange(16), gap=int(rng.randrange(20) == 0),
    )


def random_boot(tape: Tape, rng: Random, bank: int) -> None:
    """Build one reproducible initial snapshot for the selected bank."""
    cfg = tape.config
    tape.start(bank, rng.randint(0, cfg.sequence_max))
    chosen = rng.sample(range(cfg.slots), rng.randint(2, min(cfg.slots, 5)))
    for index, order_id in enumerate(chosen):
        side = bool(index % 2)
        price = rng.randrange(cfg.levels)
        if index < 2:
            price = cfg.levels - 1 if side else 0
        tape.send(
            bank, Operation.SNAP_ADD, order_id=order_id, side=side, price=price,
            quantity=rng.randint(1, cfg.quantity_max), expected=Status.OK,
        )
    tape.send(bank, Operation.SNAP_END, expected=Status.OK)


def random_update(tape: Tape, rng: Random, bank: int) -> None:
    """Pick a mix of legal and deliberately faulting operations for a live book."""
    cfg = tape.config
    state = tape.instruments[bank]
    options = remaining_legal_ops(tape, bank)
    live, free = options["live"], options["free"]
    candidates = [Operation.HEARTBEAT]
    if free:
        candidates.extend([Operation.ADD] * 5)
    if live:
        candidates.extend([Operation.DELETE, Operation.EXECUTE, Operation.EXECUTE])
    if free and live:
        candidates.extend([Operation.REPLACE] * 3)
    if options["smaller"]:
        candidates.extend([Operation.CANCEL] * 2)
    op = rng.choice(candidates)
    order_id = rng.choice(live) if live else 0
    quantity = 0
    price = 0
    new_id = 0
    if op == Operation.ADD:
        order_id = rng.choice(free)
        quantity = rng.randint(1, cfg.quantity_max)
        price = rng.randrange(cfg.levels)
    elif op == Operation.CANCEL:
        order_id = rng.choice(options["smaller"])
        quantity = rng.randrange(1, state.book.orders[order_id].size)
    elif op == Operation.EXECUTE:
        resting_size = state.book.orders[order_id].size
        quantity = resting_size if rng.randrange(3) == 0 else rng.randint(1, resting_size)
    elif op == Operation.REPLACE:
        old = state.book.orders[order_id]
        new_id = rng.choice(free)
        price = old.price if rng.randrange(3) == 0 else rng.randrange(cfg.levels)
        quantity = rng.randint(1, cfg.quantity_max)
    tape.send(
        bank, op, order_id=order_id, new_id=new_id, price=price, quantity=quantity,
        side=bool(rng.getrandbits(1)), expected=Status.OK,
    )


def randomized(cfg: Config, count: int, seed: int) -> Scenario:
    """Interleave independent instrument traffic with pseudo-random backpressure."""
    tape = Tape(cfg, "stress")
    rng = Random(seed)
    for bank in range(cfg.banks):
        tape.seed_bank(bank, sequence=(cfg.sequence_max - bank) & cfg.sequence_max)
    for index in range(count):
        bank = rng.randrange(cfg.banks)
        state = tape.instruments[bank]
        draw = rng.randrange(100)
        if draw < 3:
            tape.send(rng.randint(cfg.banks, cfg.instrument_max), Operation.BBO, expected=Status.BANK)
        elif draw < 28:
            random_query(tape, rng, bank)
        elif state.mode != MarketMode.LIVE:
            random_boot(tape, rng, bank)
        elif draw < 31:
            tape.send(
                bank, Operation.HEARTBEAT, sequence=(state.expected + 1) & cfg.sequence_max,
                expected=Status.GAP,
            )
        elif draw < 35:
            tape.send(bank, Operation.ADD, order_id=cfg.slots, quantity=1, expected=Status.ID)
        elif draw < 38:
            random_boot(tape, rng, bank)
        else:
            random_update(tape, rng, bank)

        if index % 59 == 0:
            tape.counters(bank)

    for bank in range(cfg.banks):
        tape.inspect(bank)
        tape.counters(bank)
    pacing = Random(seed ^ 0xD814)
    for cycle in range(2 * len(tape.scenario.commands) + 50):
        response_ready = cycle % 163 not in range(54, 73) and pacing.randrange(7) != 0
        quote_ready = cycle % 89 not in range(31, 65) and pacing.randrange(5) != 0
        tape.scenario.schedule.append(Driver(response_ready=response_ready, quote_ready=quote_ready))
    tape.scenario.coverage.update(("response_stalls", "source_stalls"))
    if cfg.quote_size:
        tape.scenario.coverage.update(("quotes", "quote_stalls"))
    return tape.complete()


def watchdog(cfg: Config) -> Scenario:
    """Test that quote eligibility ends when the instrument receives no timely refresh."""
    tape = Tape(cfg, "watchdog")
    for bank in range(cfg.banks):
        tape.seed_bank(bank, sequence=cfg.sequence_max)
    tape.send(0, Operation.BBO, gap=31, expected=Status.OK)
    for bank in range(cfg.banks):
        tape.send(bank, Operation.STAT, stat_index=Statistic.TIMEOUTS)
        tape.send(bank, Operation.HEARTBEAT)
        tape.seed_bank(bank, sequence=2 + bank)
        tape.send(bank, Operation.STAT, stat_index=Statistic.AGE)
    # Pulse the external watchdog while both output interfaces can be held.
    prefix = 6 + cfg.banks * 4
    tape.scenario.schedule.extend(Driver(quote_ready=(n < 2)) for n in range(prefix))
    tape.scenario.schedule.extend(
        Driver(tick=True, response_ready=False, quote_ready=False)
        for _ in range(cfg.idle_ticks + 4)
    )
    tape.scenario.schedule.extend(Driver(quote_ready=False) for _ in range(8))
    tape.scenario.schedule.extend(Driver() for _ in range(35))
    tape.scenario.coverage.update(("timeout", "restart_after_fence"))
    return tape.complete()


def same_edge_refresh(cfg: Config) -> Scenario:
    """Test refresh and timeout competing at the same simulated edge."""
    tape = Tape(cfg, "same_edge_refresh")
    tape.seed_bank(0, sequence=1)
    tape.send(0, Operation.HEARTBEAT)
    for bank in range(1, cfg.banks):
        tape.seed_bank(bank)
    tape.send(0, Operation.STAT, stat_index=Statistic.AGE)
    tape.send(0, Operation.HEARTBEAT, gap=2)
    tape.send(0, Operation.BBO)
    tape.scenario.schedule.extend([Driver()] * 4)
    tape.scenario.schedule.append(Driver(tick=True))
    tape.scenario.schedule.extend([Driver()] * max(7, 4 * cfg.banks))
    tape.scenario.coverage.add("refresh_with_tick")
    return tape.complete()


def global_reset(cfg: Config) -> Scenario:
    """Clear in-flight activity and verify the next session begins with empty state."""
    tape = Tape(cfg, "reset")
    for bank in range(cfg.banks):
        tape.seed_bank(bank, sequence=bank)
    for index in range(14):
        bank = index % cfg.banks
        tape.send(bank, Operation.STAT, stat_index=Statistic.COMMANDS)
    for bank in range(cfg.banks):
        tape.seed_bank(bank, sequence=5 + bank)
        tape.inspect(bank)
    tape.scenario.schedule.extend(Driver(quote_ready=False) for _ in range(cfg.banks * 4 + 2))
    tape.scenario.schedule.extend(Driver(response_ready=False, quote_ready=False) for _ in range(12))
    tape.scenario.schedule.extend([Driver(reset=True)] * 2)
    tape.scenario.schedule.extend([Driver()] * 32)
    tape.scenario.coverage.add("reset_flush")
    return tape.complete()


def coalescing(cfg: Config) -> Scenario:
    """Change desired quote fields while the previous output is stalled."""
    tape = Tape(cfg, "coalescing")
    for bank in range(cfg.banks):
        tape.seed_bank(bank, sequence=bank)
    for index in range(15):
        bank = index % cfg.banks
        selected = tape.instruments[bank].book.orders[1]
        spare = 2
        price = selected.price
        volume = 1 + (index % cfg.quantity_max)
        tape.send(
            bank, Operation.REPLACE, order_id=1, new_id=spare, price=price,
            quantity=volume, expected=Status.OK,
        )
        tape.send(
            bank, Operation.REPLACE, order_id=spare, new_id=1, price=price,
            quantity=min(cfg.quantity_max, volume + 1), expected=Status.OK,
        )
        tape.send(bank, Operation.BBO, expected=Status.OK)
    tape.send(0, Operation.HEARTBEAT, sequence=(tape.instruments[0].expected + 1) & cfg.sequence_max)
    tape.send(0, Operation.BBO, gap=12)
    tape.seed_bank(0, sequence=7)
    tape.send(0, Operation.BBO, gap=15)
    tape.scenario.schedule.extend(Driver(quote_ready=False) for _ in range(cfg.banks * 4 + 57))
    tape.scenario.schedule.extend([Driver()] * 64)
    if cfg.quote_size:
        tape.scenario.coverage.update(("quote_stalls", "stale_quote_blocked", "coalesced"))
    return tape.complete()


def line_rate(cfg: Config, count: int = 512) -> Scenario:
    """Check diagnostic throughput under continuous ready and valid."""
    tape = Tape(cfg, "line_rate")
    for bank in range(cfg.banks):
        tape.seed_bank(bank, sequence=bank)
    for index in range(count):
        bank = index % cfg.banks
        if index % 3:
            tape.send(bank, Operation.HEARTBEAT, expected=Status.OK)
        else:
            tape.send(bank, Operation.BBO, expected=Status.OK)
    tape.scenario.coverage.add("line_rate")
    if cfg.quote_size:
        tape.scenario.coverage.add("unchanged_intent_suppressed")
    return tape.complete()


def line_rate_updates(cfg: Config, groups: int = 96) -> Scenario:
    """Exercise changing book state with one update issued on each available cycle."""
    tape = Tape(cfg, "line_rate_updates")
    tape.seed_bank(0, sequence=cfg.sequence_max)
    # Reuse the same three slots, including the source and destination of
    # successive price-level edits, without a single cycle of source delay.
    for index in range(groups):
        low = index % max(1, cfg.levels - 1)
        high = cfg.levels - 1
        max_qty = cfg.quantity_max
        tape.send(0, Operation.REPLACE, order_id=0, new_id=2, price=low,
                  quantity=max_qty, expected=Status.OK)
        tape.send(0, Operation.CANCEL, order_id=2, quantity=1, expected=Status.OK)
        tape.send(0, Operation.EXECUTE, order_id=2, quantity=2, expected=Status.OK)
        tape.send(0, Operation.REPLACE, order_id=2, new_id=0, price=0,
                  quantity=max_qty, expected=Status.OK)
        tape.send(0, Operation.REPLACE, order_id=1, new_id=2, price=high,
                  quantity=max_qty, expected=Status.OK)
        tape.send(0, Operation.EXECUTE, order_id=2, quantity=3, expected=Status.OK)
        tape.send(0, Operation.REPLACE, order_id=2, new_id=1, price=high,
                  quantity=max_qty, expected=Status.OK)
    tape.send(0, Operation.BBO, expected=Status.OK)
    tape.level(0, False, 0)
    tape.level(0, True, cfg.levels-1)
    tape.scenario.coverage.update(("line_rate", "hot_bank_update_burst"))
    return tape.complete()


def all_scenarios(cfg: Config, count: int, seed: int) -> Iterable[Scenario]:
    """Build and name every directed and seeded campaign for this profile."""
    yield semantic_faults(cfg)
    yield watchdog(cfg)
    yield same_edge_refresh(cfg)
    yield global_reset(cfg)
    yield coalescing(cfg)
    yield line_rate(cfg)
    yield line_rate_updates(cfg)
    yield randomized(cfg, count, seed)
