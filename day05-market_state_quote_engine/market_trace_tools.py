"""Audit an exported market journal without rerunning the market model."""

import argparse
from collections import Counter, deque
import csv
from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
from typing import Deque, Dict, Iterable, Iterator, List, Optional, TextIO, Tuple

from market_reference import Command, Config, Operation, Quote, Reason, Response, Status
from market_replay import JOURNAL_SCHEMA, Journal


class JournalError(ValueError):
    pass


def exact_keys(obj: Dict, names: Iterable[str], label: str) -> None:
    wanted = set(names)
    got = set(obj)
    if got != wanted:
        raise JournalError(f"{label} keys missing={sorted(wanted-got)} extra={sorted(got-wanted)}")


def bounded(value: int, high: int, label: str) -> None:
    if type(value) is not int or not 0 <= value <= high:
        raise JournalError(f"{label}={value!r} is outside 0..{high}")


def boolean(value: bool, label: str) -> None:
    if type(value) is not bool:
        raise JournalError(f"{label} must be JSON true or false")


def decode_mapping(row: Dict, record, label: str):
    if row is None:
        return None
    if not isinstance(row, dict):
        raise JournalError(f"{label} must be object or null")
    exact_keys(row, (field.name for field in fields(record)), label)
    return record(**row)


@dataclass(frozen=True)
class Entry:
    register: Journal
    command: Optional[Command]
    response: Optional[Response]
    quote: Optional[Quote]

    @classmethod
    def parse(cls, row: Dict, config: Config) -> "Entry":
        if not isinstance(row, dict):
            raise JournalError("journal entry must be an object")
        exact_keys(row, (field.name for field in fields(Journal)), "entry")
        register = Journal(**row)
        for name in ("reset", "tick", "accepted", "source_ready", "response_ready", "quote_ready", "quote_current"):
            boolean(getattr(register, name), name)
        bounded(register.cycle, 2**31-1, "cycle")
        bounded(register.pending, 2**31-1, "pending")
        bounded(register.dropped_ingress, 2**31-1, "dropped_ingress")
        for name in ("epochs", "eligible", "live", "pressure_bid", "pressure_ask"):
            value = getattr(register, name)
            if not isinstance(value, list) or len(value) != config.banks:
                raise JournalError(f"{name} needs exactly {config.banks} lane entries")
        if not isinstance(register.gate_revoked, list):
            raise JournalError("gate_revoked must be an array")
        for lane in register.gate_revoked:
            bounded(lane, config.banks-1, "gate_revoked lane")
        for bank, epoch in enumerate(register.epochs):
            bounded(epoch, config.mask(config.epoch_width), f"epoch[{bank}]")
            boolean(register.eligible[bank], f"eligible[{bank}]")
            boolean(register.live[bank], f"live[{bank}]")
            boolean(register.pressure_bid[bank], f"pressure_bid[{bank}]")
            boolean(register.pressure_ask[bank], f"pressure_ask[{bank}]")
        command = decode_mapping(register.offered, Command, "command")
        response = decode_mapping(register.response, Response, "response")
        quote = decode_mapping(register.quote, Quote, "quote")
        if command:
            boolean(command.side, "command.side")
            command.validate_width(config)
        if response:
            for name in ("present", "side", "bid_present", "ask_present"):
                boolean(getattr(response, name), f"response.{name}")
            response_limited = {
                "instrument": config.instrument_max, "op": 15, "sequence": config.sequence_max,
                "order_id": config.order_id_max, "tag": config.mask(config.tag_width),
                "status": max(Status), "mode": 3, "expected": config.sequence_max,
                "epoch": config.mask(config.epoch_width), "price": config.price_max,
                "count": config.slots, "statistic": config.mask(config.stat_width),
                "bid": config.price_max, "ask": config.price_max,
                "bid_count": config.slots, "ask_count": config.slots,
                "quantity": config.quantity_max*config.slots,
                "bid_quantity": config.quantity_max*config.slots,
                "ask_quantity": config.quantity_max*config.slots,
            }
            for name, high in response_limited.items():
                bounded(getattr(response, name), int(high), f"response.{name}")
        if quote:
            boolean(quote.active, "quote.active")
            bounded(quote.instrument, config.banks-1, "quote.instrument")
            bounded(quote.epoch, config.mask(config.epoch_width), "quote.epoch")
            bounded(quote.revision, config.mask(config.revision_width), "quote.revision")
            bounded(quote.bid, config.levels-1, "quote.bid")
            bounded(quote.ask, config.levels-1, "quote.ask")
            bounded(quote.size, config.quantity_max, "quote.size")
            bounded(quote.reason, int(max(Reason)), "quote.reason")
        return cls(register, command, response, quote)


def read_config(source: TextIO) -> Tuple[Config, str]:
    try:
        header = json.loads(next(source))
    except (StopIteration, ValueError) as exc:
        raise JournalError("missing or malformed JSONL header") from exc
    if not isinstance(header, dict) or header.get("schema") != JOURNAL_SCHEMA:
        raise JournalError(f"expected journal schema {JOURNAL_SCHEMA}")
    exact_keys(header, ("schema", "config", "profile"), "header")
    if not isinstance(header["config"], dict) or not isinstance(header["profile"], str):
        raise JournalError("bad profile metadata")
    exact_keys(header["config"], (f.name for f in fields(Config)), "config")
    for field in fields(Config):
        bounded(header["config"][field.name], 2**31-1, f"config.{field.name}")
    cfg = Config(**header["config"])
    positive = (
        cfg.banks, cfg.levels, cfg.slots, cfg.quantity_width, cfg.sequence_width,
        cfg.epoch_width, cfg.tag_width, cfg.stat_width, cfg.revision_width, cfg.idle_ticks,
    )
    if not all(0 < value <= 4096 for value in positive):
        raise JournalError("config dimensions must be positive and bounded for offline parsing")
    if not all(width <= 64 for width in (
        cfg.quantity_width, cfg.sequence_width, cfg.epoch_width, cfg.tag_width,
        cfg.stat_width, cfg.revision_width,
    )):
        raise JournalError("config encoded widths are limited to 64 bits in a journal")
    return cfg, header["profile"]


def read_entries(source: TextIO, config: Config) -> Iterator[Entry]:
    for number, row in enumerate(source, start=2):
        try:
            yield Entry.parse(json.loads(row), config)
        except (ValueError, TypeError, KeyError) as exc:
            raise JournalError(f"JSONL line {number}: {exc}") from exc


@dataclass
class Window:
    first: int
    last: int
    eligible_cycles: int = 0
    live_cycles: int = 0
    applied: int = 0
    rejected: int = 0
    max_spread: int = 0
    max_size: int = 0

    def advance(self, cycle: int, *, live: bool, eligible: bool) -> None:
        self.last = cycle
        self.eligible_cycles += int(eligible)
        self.live_cycles += int(live)

    def mapped(self) -> Dict:
        return {**asdict(self), "cycles": self.last-self.first+1}


class Audit:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cycle = -1
        self.pending: Deque[Command] = deque()
        self.held_command: Optional[Command] = None
        self.held_response: Optional[Response] = None
        self.held_quote: Optional[Quote] = None
        self.visible: Dict[int, Quote] = {}
        self.last_revision: Dict[int, int] = {}
        self.last_epoch: Dict[int, int] = {}
        self.windows: Dict[Tuple[int, int, int], Window] = {}
        self.count: Counter = Counter()
        self.rejections: Counter = Counter()
        self.action: Counter = Counter()
        self.epoch_number: List[int] = [0] * cfg.banks
        self.reset_seen = False

    def require(self, flag: bool, message: str) -> None:
        if not flag:
            raise JournalError(f"cycle {self.cycle}: {message}")

    def check_reset(self, entry: Entry) -> None:
        row = entry.register
        self.require(not entry.command and not entry.response and not entry.quote,
                     "reset published a payload")
        self.require(not any((row.accepted, row.source_ready, row.quote_current,
                              any(row.eligible), any(row.live), row.pending)),
                     "reset did not fence interfaces")
        if not self.reset_seen:
            self.count["reset_episodes"] += 1
            self.count["response_resets"] += len(self.pending)
            self.count["held_quote_resets"] += int(self.held_quote is not None)
            self.last_revision.clear()
            self.last_epoch.clear()
            for bank in range(self.cfg.banks):
                self.epoch_number[bank] += 1
        self.pending.clear()
        self.held_command = None
        self.held_response = None
        self.held_quote = None

    def check_response(self, entry: Entry) -> None:
        rsp = entry.response
        row = entry.register
        self.require((rsp is not None) == bool(self.pending),
                     "diagnostic register does not match outstanding accepts")
        if self.held_response is not None:
            self.require(rsp == self.held_response, "stalled diagnostic changed")
        self.held_response = None
        if rsp is None:
            return
        accepted = self.pending[0]
        self.require(
            (rsp.instrument, rsp.op, rsp.sequence, rsp.order_id, rsp.tag)
            == (accepted.instrument, accepted.op, accepted.sequence, accepted.order_id, accepted.tag),
            "diagnostic does not echo oldest accepted command",
        )
        if rsp.instrument >= self.cfg.banks:
            self.require(rsp.status == Status.BANK, "invalid bank did not return BANK")
        else:
            self.require(rsp.status != Status.BANK, "valid bank returned BANK")
        if rsp.op != Operation.BBO:
            self.require(not any((
                rsp.bid_present, rsp.ask_present, rsp.bid, rsp.ask, rsp.bid_quantity,
                rsp.ask_quantity, rsp.bid_count, rsp.ask_count,
            )), "non-BBO response contains BBO data")
        elif rsp.status == Status.OK:
            self.require(rsp.bid_present == bool(rsp.bid_count) == bool(rsp.bid_quantity),
                         "BBO bid depth/count/presence disagree")
            self.require(rsp.ask_present == bool(rsp.ask_count) == bool(rsp.ask_quantity),
                         "BBO ask depth/count/presence disagree")
        if row.response_ready:
            self.pending.popleft()
            self.count["responses"] += 1
            self.rejections[Status(rsp.status).name] += 1
        else:
            self.held_response = rsp
            self.count["response_stall_cycles"] += 1

    def check_source(self, entry: Entry) -> None:
        row = entry.register
        self.require(row.source_ready == (entry.response is None or row.response_ready),
                     "source ready lacks response capacity or blocked unnecessarily")
        if self.held_command is not None:
            self.require(entry.command == self.held_command, "source replaced a stalled command")
        self.require(row.accepted == (entry.command is not None and row.source_ready),
                     "accept flag is not a handshake")
        self.held_command = None
        if row.accepted:
            self.pending.append(entry.command)
            self.count["accepted"] += 1
        elif entry.command is not None:
            self.held_command = entry.command
            self.count["source_stall_cycles"] += 1

    def action_for(self, quote: Optional[Quote], info: Journal) -> str:
        if quote is None:
            return "idle"
        if not info.quote_ready:
            return "held"
        if quote.epoch != info.epochs[quote.instrument]:
            return "drop_epoch"
        if quote.active and not info.eligible[quote.instrument]:
            return "drop_ineligible"
        if not info.quote_current:
            return "drop_superseded"
        return "publish_intent" if quote.active else "withdraw_intent"

    def enforce_gates(self, row: Journal) -> None:
        invalid = [
            bank for bank, quote in sorted(self.visible.items())
            if not row.eligible[bank] or row.epochs[bank] != quote.epoch
        ]
        self.require(invalid == row.gate_revoked, "recorded automatic gate revocations do not match")
        for bank in invalid:
            del self.visible[bank]
        self.count["gate_revokes"] += len(invalid)

    def check_quote(self, entry: Entry) -> None:
        row = entry.register
        quote = entry.quote
        action = self.action_for(quote, row)
        self.require(row.quote_action == action,
                     f"logged quote action {row.quote_action!r} instead of {action!r}")
        self.action[action] += 1
        if self.held_quote is not None:
            self.require(quote == self.held_quote, "publisher changed a stalled quote")
        self.held_quote = None
        if quote is None:
            self.require(not row.quote_current, "publication current without a valid quote")
            return
        bank = quote.instrument
        if row.quote_current:
            self.require(quote.epoch == row.epochs[bank], "current quote has wrong epoch")
            self.require(quote.active == row.eligible[bank], "current quote has wrong eligibility")
        if quote.active:
            self.require(quote.bid < quote.ask and quote.size > 0 and quote.reason == Reason.QUOTING,
                         "active quote has crossed prices, zero size or a withdrawal reason")
        else:
            self.require(not any((quote.bid, quote.ask, quote.size)) and quote.reason != Reason.QUOTING,
                         "withdrawal contains active quote values")
        if not row.quote_ready:
            self.held_quote = quote
            return
        mask = self.cfg.mask(self.cfg.revision_width)
        expected_revision = (self.last_revision.get(bank, -1) + 1) & mask
        self.require(quote.revision == expected_revision,
                     f"bank {bank} revision {quote.revision} expected {expected_revision}")
        self.last_revision[bank] = quote.revision
        self.count["quote_transfers"] += 1
        if action == "publish_intent":
            self.visible[bank] = quote
        elif action == "withdraw_intent":
            self.visible.pop(bank, None)

    def update_windows(self, entry: Entry) -> None:
        row = entry.register
        for bank in range(self.cfg.banks):
            if row.live[bank]:
                self.require(not row.reset, "live sideband during reset")
            self.require(not row.eligible[bank] or row.live[bank],
                         "quote eligibility without a live instrument")
            up, down = row.pressure_bid[bank], row.pressure_ask[bank]
            self.require(not (up and down), "simultaneous buy and sell pressure")
            self.require(row.live[bank] or not (up or down), "pressure without a live book")
            epoch = row.epochs[bank]
            previous = self.last_epoch.get(bank)
            if previous is not None and epoch != previous:
                self.epoch_number[bank] += 1
            self.last_epoch[bank] = epoch
            key = (bank, self.epoch_number[bank], epoch)
            self.windows.setdefault(key, Window(row.cycle, row.cycle)).advance(
                row.cycle, live=row.live[bank], eligible=row.eligible[bank],
            )
        quote = entry.quote
        if quote and row.quote_ready:
            bank = quote.instrument
            key = (bank, self.epoch_number[bank], row.epochs[bank])
            window = self.windows[key]
            if row.quote_action == "publish_intent":
                window.applied += 1
                window.max_spread = max(window.max_spread, quote.ask-quote.bid)
                window.max_size = max(window.max_size, quote.size)
            elif row.quote_action.startswith("drop_"):
                window.rejected += 1

    def visit(self, entry: Entry) -> None:
        row = entry.register
        self.require(row.cycle == self.cycle+1, f"nonconsecutive row {row.cycle}")
        self.cycle = row.cycle
        if row.reset:
            self.check_reset(entry)
        else:
            self.check_response(entry)
            self.check_source(entry)
        self.enforce_gates(row)
        self.check_quote(entry)
        self.update_windows(entry)
        self.reset_seen = row.reset

    def finish(self) -> Dict:
        self.require(self.cycle >= 0, "empty journal")
        self.require(not self.pending and self.held_quote is None,
                     "journal ends before diagnostics and publication drain")
        return {
            "cycles": self.cycle+1,
            "counters": dict(self.count),
            "status_counts": dict(self.rejections),
            "quote_actions": dict(self.action),
            "epochs": [
                {"instrument": bank, "generation": generation, "wire_epoch": epoch, **window.mapped()}
                for (bank, generation, epoch), window in sorted(self.windows.items())
            ],
            "installed_offline": {str(bank): asdict(quote) for bank, quote in sorted(self.visible.items())},
        }


def audit_stream(source: TextIO) -> Dict:
    cfg, profile = read_config(source)
    audit = Audit(cfg)
    for entry in read_entries(source, cfg):
        audit.visit(entry)
    return {"profile": profile, **audit.finish()}


WINDOW_COLUMNS = (
    "cycle", "instrument", "entity", "event", "epoch", "sequence", "status",
    "bid", "ask", "quantity", "current",
)


def export_rows(entries: Iterable[Entry], bank: Optional[int], first: int, last: int) -> Iterable[Dict]:
    for entry in entries:
        row = entry.register
        if row.cycle < first or row.cycle > last:
            continue
        quote = entry.quote
        if quote is not None and (bank is None or bank == quote.instrument):
            yield {
                "cycle": row.cycle, "instrument": quote.instrument, "entity": "quote",
                "event": row.quote_action, "epoch": quote.epoch, "sequence": quote.revision,
                "status": Reason(quote.reason).name, "bid": quote.bid, "ask": quote.ask,
                "quantity": quote.size, "current": int(row.quote_current),
            }
        rsp = entry.response
        if rsp is not None and row.response_ready and (bank is None or bank == rsp.instrument):
            yield {
                "cycle": row.cycle, "instrument": rsp.instrument, "entity": "response",
                "event": Operation(rsp.op).name if rsp.op <= max(Operation) else f"RESERVED_{rsp.op}",
                "epoch": rsp.epoch, "sequence": rsp.sequence, "status": Status(rsp.status).name,
                "bid": rsp.bid, "ask": rsp.ask, "quantity": rsp.quantity,
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="operation", required=True)
    audit = actions.add_parser("audit", help="verify journal structure, handshake, and consumer decisions")
    audit.add_argument("journal", type=Path)
    audit.add_argument("--out", type=Path)
    timeline = actions.add_parser("timeline", help="export selected journal events to CSV")
    timeline.add_argument("journal", type=Path)
    timeline.add_argument("--out", type=Path, required=True)
    timeline.add_argument("--bank", type=int)
    timeline.add_argument("--start", type=int, default=0)
    timeline.add_argument("--end", type=int, default=2**31-1)
    args = parser.parse_args()

    with args.journal.open(encoding="utf-8") as source:
        if args.operation == "audit":
            result = json.dumps(audit_stream(source), indent=2) + "\n"
            if args.out:
                args.out.write_text(result, encoding="utf-8")
            print(result, end="")
        else:
            cfg, _ = read_config(source)
            if not 0 <= args.start <= args.end:
                raise SystemExit("timeline bounds must be nonnegative and sorted")
            if args.bank is not None and not 0 <= args.bank <= cfg.instrument_max:
                raise SystemExit("bank is outside encoded instrument range")
            rows = export_rows(read_entries(source, cfg), args.bank, args.start, args.end)
            with args.out.open("w", newline="", encoding="utf-8") as out:
                sink = csv.DictWriter(out, fieldnames=WINDOW_COLUMNS)
                sink.writeheader()
                sink.writerows(rows)


if __name__ == "__main__":
    main()
