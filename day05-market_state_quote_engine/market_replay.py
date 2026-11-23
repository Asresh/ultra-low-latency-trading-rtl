"""Replay synthetic normalized feed events and export a cycle journal."""

import argparse
import csv
from collections import Counter, deque
from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
from typing import Deque, Dict, Iterable, Iterator, List, Optional, TextIO, Tuple

from market_reference import (
    Command, Config, CycleInput, Engine, Operation, PROFILES, Quote, Statistic,
)


INPUT_FIELDS = ("cycle", "reset", "tick", "response_ready", "quote_ready")
COMMAND_FIELDS = tuple(field.name for field in fields(Command))
CSV_FIELDS = INPUT_FIELDS + COMMAND_FIELDS
MAX_REPLAY_CYCLES = 1_000_000
JOURNAL_SCHEMA = "day05-market-journal-v1"


@dataclass(frozen=True)
class FeedRow:
    cycle: int
    reset: bool = False
    tick: bool = False
    response_ready: bool = True
    quote_ready: bool = True
    command: Optional[Command] = None

    def mapping(self) -> Dict:
        """Serialize host arrival and ready controls to the CSV field layout."""
        row = {name: int(getattr(self, name)) for name in INPUT_FIELDS}
        if self.command:
            row.update({
                name: value for name, value in zip(COMMAND_FIELDS, self.command.values())
            })
            try:
                row["op"] = Operation(self.command.op).name
            except ValueError:
                pass
        return row


def as_int(mapping: Dict[str, str], field: str, default: Optional[int] = None) -> int:
    """Convert one named integer using the declared fallback for a blank field."""
    raw = mapping[field]
    if raw is None or not raw.strip():
        if default is None:
            raise ValueError(f"{field} is required")
        return default
    try:
        return int(raw, 10)
    except ValueError as exc:
        raise ValueError(f"{field} must be a decimal integer, got {raw!r}") from exc


def as_bool(mapping: Dict[str, str], field: str, default: bool) -> bool:
    """Read a Boolean field and reject values outside the permitted encoding."""
    value = as_int(mapping, field, int(default))
    if value not in (0, 1):
        raise ValueError(f"{field} must be 0 or 1")
    return bool(value)


def decode_op(value: str) -> int:
    """Translate symbolic opcodes while preserving numeric reserved opcode tests."""
    try:
        return int(Operation[value.strip().upper()])
    except KeyError:
        try:
            return int(value, 10)
        except ValueError as exc:
            raise ValueError(f"unknown operation {value!r}") from exc


def parse_row(mapping: Dict[str, str], config: Config) -> FeedRow:
    """Validate a replay CSV row and construct its optional source command."""
    cycle = as_int(mapping, "cycle")
    if not 0 <= cycle < MAX_REPLAY_CYCLES:
        raise ValueError(f"cycle must be in 0..{MAX_REPLAY_CYCLES-1}")
    reset = as_bool(mapping, "reset", False)
    tick = as_bool(mapping, "tick", False)
    response_ready = as_bool(mapping, "response_ready", True)
    quote_ready = as_bool(mapping, "quote_ready", True)
    op = mapping["op"]
    command = None
    if op is not None and op.strip():
        if reset:
            raise ValueError("an arrival cannot be combined with reset")
        command = Command(
            instrument=as_int(mapping, "instrument", 0),
            op=decode_op(op),
            sequence=as_int(mapping, "sequence", 0),
            order_id=as_int(mapping, "order_id", 0),
            new_id=as_int(mapping, "new_id", 0),
            side=as_bool(mapping, "side", False),
            price=as_int(mapping, "price", 0),
            quantity=as_int(mapping, "quantity", 0),
            stat_index=as_int(mapping, "stat_index", 0),
            tag=as_int(mapping, "tag", 0),
        )
        command.validate_width(config)
    elif any((mapping[field] or "").strip() for field in COMMAND_FIELDS if field != "op"):
        raise ValueError("command fields require an operation")
    return FeedRow(cycle, reset, tick, response_ready, quote_ready, command)


def read_rows(data: TextIO, config: Config) -> Iterator[FeedRow]:
    """Stream increasing cycle records and report malformed data with line context."""
    source = csv.DictReader(data)
    if tuple(source.fieldnames or ()) != CSV_FIELDS:
        raise ValueError(f"expected CSV header {','.join(CSV_FIELDS)}")
    previous = -1
    for mapping in source:
        try:
            if None in mapping:
                raise ValueError("unexpected extra CSV columns")
            row = parse_row(mapping, config)
            if row.cycle <= previous:
                raise ValueError("feed rows must use strictly increasing cycle numbers")
        except ValueError as exc:
            raise ValueError(f"CSV line {source.line_num}: {exc}") from exc
        previous = row.cycle
        yield row


def write_rows(out: TextIO, rows: Iterable[FeedRow]) -> None:
    """Write replay events in canonical CSV column order."""
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row.mapping())


@dataclass(frozen=True)
class Journal:
    cycle: int
    reset: bool
    tick: bool
    offered: Optional[Dict]
    accepted: bool
    source_ready: bool
    response: Optional[Dict]
    response_ready: bool
    quote: Optional[Dict]
    quote_ready: bool
    quote_current: bool
    quote_action: str
    gate_revoked: Tuple[int, ...]
    epochs: Tuple[int, ...]
    eligible: Tuple[bool, ...]
    live: Tuple[bool, ...]
    pressure_bid: Tuple[bool, ...]
    pressure_ask: Tuple[bool, ...]
    pending: int
    dropped_ingress: int

    def mapping(self) -> Dict:
        """Convert a journal cycle and its nested records to serializable dictionaries."""
        return asdict(self)


class ReplaySession:
    def __init__(self, cfg: Config, capacity: int = 128):
        """Set up a bounded source backlog and independent downstream modeled state."""
        if capacity <= 0:
            raise ValueError("host backlog capacity must be positive")
        self.cfg = cfg
        self.model = Engine(cfg)
        self.capacity = capacity
        self.pending: Deque[Command] = deque()
        self.cycle = 0
        self.installed: Dict[int, Quote] = {}
        self.counters: Counter = Counter()

    def apply_gates(self, eligible: Tuple[bool, ...], epochs: Tuple[int, ...]) -> Tuple[int, ...]:
        """Withdraw active quotes whose epoch or eligibility no longer matches current state."""
        changes: List[int] = []
        for bank in sorted(self.installed):
            existing = self.installed[bank]
            if not eligible[bank] or epochs[bank] != existing.epoch:
                changes.append(bank)
        for bank in changes:
            del self.installed[bank]
            self.counters["gate_revokes"] += 1
        return tuple(changes)

    def dispatch(
        self, quote: Optional[Quote], ready: bool, current: bool,
        eligible: Tuple[bool, ...], epochs: Tuple[int, ...],
    ) -> str:
        """Classify a transferred quote as current, stale, or an explicit withdrawal."""
        if quote is None:
            return "idle"
        if not ready:
            return "held"
        bank = quote.instrument
        if epochs[bank] != quote.epoch:
            return "drop_epoch"
        if quote.active and not eligible[bank]:
            return "drop_ineligible"
        if not current:
            return "drop_superseded"
        if quote.active:
            self.installed[bank] = quote
            return "publish_intent"
        self.installed.pop(bank, None)
        return "withdraw_intent"

    def step(self, row: FeedRow) -> Journal:
        """Drive a replay cycle and record its accepted commands and output decisions."""
        if row.cycle != self.cycle:
            raise ValueError(f"row at cycle {row.cycle}, expected {self.cycle}")
        dropped = 0
        if row.reset and row.command is not None:
            raise ValueError("an arrival cannot be combined with reset")
        if row.reset:
            dropped = len(self.pending)
            self.pending.clear()
        if row.command is not None:
            row.command.validate_width(self.cfg)
            if len(self.pending) >= self.capacity:
                raise OverflowError(f"host backlog overflow at cycle {self.cycle}")
            self.pending.append(row.command)
            self.counters["arrivals"] += 1
        offered = self.pending[0] if self.pending else None
        emitted = self.model.cycle(CycleInput(
            row.reset, row.tick, offered, row.response_ready, row.quote_ready,
        ))
        pins = emitted.pins
        accepted = offered is not None and pins.source_ready
        if accepted:
            self.pending.popleft()
            self.counters["accepts"] += 1
        if pins.response is not None and row.response_ready:
            self.counters["responses"] += 1
        if pins.quote is not None and row.quote_ready:
            self.counters["quote_transfers"] += 1
        revoked = self.apply_gates(pins.eligible, pins.epochs)
        action = self.dispatch(
            pins.quote, row.quote_ready, pins.quote_current, pins.eligible, pins.epochs,
        )
        self.counters[action] += 1
        self.counters["dropped_ingress"] += dropped
        self.counters["peak_backlog"] = max(self.counters["peak_backlog"], len(self.pending))
        report = Journal(
            cycle=row.cycle, reset=row.reset, tick=row.tick, offered=asdict(offered) if offered else None,
            accepted=accepted, source_ready=pins.source_ready,
            response=asdict(pins.response) if pins.response else None, response_ready=row.response_ready,
            quote=asdict(pins.quote) if pins.quote else None, quote_ready=row.quote_ready,
            quote_current=pins.quote_current, quote_action=action, gate_revoked=revoked, epochs=pins.epochs,
            eligible=pins.eligible, live=pins.live, pending=len(self.pending), dropped_ingress=dropped,
            pressure_bid=pins.pressure_bid, pressure_ask=pins.pressure_ask,
        )
        self.cycle += 1
        return report

    def expand(self, events: Iterable[FeedRow], tail: int = 0) -> Iterator[Journal]:
        """Fill gaps between scheduled arrivals and optionally drain trailing cycles."""
        if tail < 0:
            raise ValueError("tail must be nonnegative")
        for row in events:
            if row.cycle < self.cycle:
                raise ValueError(f"late or duplicate input row at cycle {row.cycle}")
            while self.cycle < row.cycle:
                yield self.step(FeedRow(self.cycle))
            yield self.step(row)
        for _ in range(self.capacity + 3 * self.cfg.banks + 8 + tail):
            yield self.step(FeedRow(self.cycle))
            desired = [bank.desired() for bank in self.model.instruments]
            drained = (
                self.model.response is None and self.model.publisher.output is None
                and self.model.publisher.choose(desired) is None
                and self.model.publisher.choose(self.model.intent_stage) is None and not self.pending
            )
            if drained:
                for _ in range(tail):
                    yield self.step(FeedRow(self.cycle))
                return
        raise RuntimeError("the offline RTL behavior failed to reach a quiescent state")

    def inventory(self) -> List[Dict]:
        """Export validated active orders and diagnostics grouped by instrument."""
        banks = []
        for index, instrument in enumerate(self.model.instruments):
            banks.append({
                "instrument": index,
                "mode": instrument.mode.name,
                "epoch": instrument.epoch,
                "expected_sequence": instrument.expected,
                "freshness_age": instrument.age,
                "statistics": {
                    stat.name: instrument.statistic(stat) for stat in Statistic
                },
                "orders": [
                    {"id": key, **asdict(value)}
                    for key, value in sorted(instrument.book.orders.items())
                ],
                "levels": [
                    {"side": side, "price": level, **asdict(depth)}
                    for side in (False, True)
                    for level, depth in sorted(instrument.book.levels(side).items())
                ],
                "bbo": instrument.book.bbo().diagnostic(),
                "desired": asdict(instrument.desired()),
                "installed_offline_intent": (
                    asdict(self.installed[index]) if index in self.installed else None
                ),
            })
        return banks


def fixture(cfg: Config) -> Iterator[FeedRow]:
    """Create a small replay workload that exercises common mode and transport transitions."""
    cycle = 0
    yield FeedRow(cycle, reset=True)
    cycle += 2
    positions = []
    tag = 0

    def command(bank: int, op: Operation, seq: int, **payload) -> Command:
        """Build a correctly sized command for an event in the synthetic fixture."""
        nonlocal tag
        data = Command(instrument=bank, op=op, sequence=seq, tag=tag, **payload)
        tag = (tag + 1) & cfg.mask(cfg.tag_width)
        return data

    for bank in range(cfg.banks):
        start = (cfg.sequence_max-1-bank) & cfg.sequence_max
        yield FeedRow(cycle, command=command(bank, Operation.SNAP_BEGIN, start))
        cycle += 1
        yield FeedRow(cycle, command=command(
            bank, Operation.SNAP_ADD, (start+1) & cfg.sequence_max,
            side=False, price=0, quantity=min(15, cfg.quantity_max),
        ))
        cycle += 1
        yield FeedRow(cycle, command=command(
            bank, Operation.SNAP_ADD, (start+2) & cfg.sequence_max,
            side=True, price=cfg.levels-1, quantity=min(9, cfg.quantity_max), order_id=1,
        ))
        cycle += 1
        yield FeedRow(cycle, quote_ready=False, command=command(
            bank, Operation.SNAP_END, (start+3) & cfg.sequence_max,
        ))
        cycle += 1
        positions.append((start+4) & cfg.sequence_max)
    yield FeedRow(cycle, quote_ready=False, command=command(0, Operation.BBO, 0))
    cycle += 1
    yield FeedRow(cycle, quote_ready=False, command=command(
        0, Operation.HEARTBEAT, (positions[0]+1) & cfg.sequence_max,
    ))
    cycle += 1
    yield FeedRow(cycle, quote_ready=False, command=command(0, Operation.STAT, 0, stat_index=Statistic.GAPS))
    cycle += 2
    for _ in range(cfg.idle_ticks):
        yield FeedRow(cycle, tick=True)
        cycle += 1
    for bank in range(cfg.banks):
        yield FeedRow(cycle, command=command(bank, Operation.STAT, 0, stat_index=Statistic.TIMEOUTS))
        cycle += 1


def replay(args: argparse.Namespace) -> None:
    """Read the scheduled feed and write its predicted output journal."""
    cfg = PROFILES[args.profile]
    session = ReplaySession(cfg, args.capacity)
    with args.file.open(newline="", encoding="utf-8") as source, args.out.open("w", encoding="utf-8") as out:
        header = {"schema": JOURNAL_SCHEMA, "config": asdict(cfg), "profile": args.profile}
        out.write(json.dumps(header) + "\n")
        for result in session.expand(read_rows(source, cfg), tail=args.tail):
            out.write(json.dumps(result.mapping(), separators=(",", ":")) + "\n")
    summary = {"cycles": session.cycle, "counters": dict(session.counters)}
    if args.inventory is not None:
        args.inventory.write_text(json.dumps(
            {**summary, "instruments": session.inventory()}, indent=2,
        ) + "\n", encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))


def arguments() -> argparse.Namespace:
    """Define input, geometry, and output choices for the replay command."""
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="operation", required=True)
    fixture_cmd = modes.add_parser("fixture", help="write a synthetic CSV including faults and expiry")
    fixture_cmd.add_argument("file", type=Path)
    fixture_cmd.add_argument("--profile", choices=PROFILES, default="default")
    replay_cmd = modes.add_parser("replay", help="run the offline model and export a validated JSONL schema")
    replay_cmd.add_argument("file", type=Path)
    replay_cmd.add_argument("--profile", choices=PROFILES, default="default")
    replay_cmd.add_argument("--capacity", type=int, default=128)
    replay_cmd.add_argument("--tail", type=int, default=0)
    replay_cmd.add_argument("--out", type=Path, required=True)
    replay_cmd.add_argument("--inventory", type=Path)
    return parser.parse_args()


def main() -> None:
    """Run the requested fixture or replay operation."""
    args = arguments()
    if args.operation == "fixture":
        with args.file.open("w", newline="", encoding="utf-8") as out:
            write_rows(out, fixture(PROFILES[args.profile]))
    else:
        replay(args)


if __name__ == "__main__":
    main()
