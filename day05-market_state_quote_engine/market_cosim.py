"""Replay a validated host journal on Icarus and compare exact RTL pins."""

import argparse
from collections import Counter, deque
from dataclasses import asdict, dataclass
from itertools import zip_longest
import json
from pathlib import Path
from subprocess import run
from typing import Deque, Dict, Iterable, Iterator, Optional, TextIO, Tuple

from market_reference import Command, Combinational, Config, CycleInput, Operation, Status
from market_trace_tools import Audit, Entry, JournalError, read_config, read_entries
from verify_market import Trace, print_differences, records


SOURCES = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ReplayVector:
    cycle: int
    stimulus: CycleInput

    @classmethod
    def from_entry(cls, entry: Entry) -> "ReplayVector":
        row = entry.register
        return cls(row.cycle, CycleInput(
            row.reset, row.tick, entry.command, row.response_ready, row.quote_ready,
        ))

    @classmethod
    def from_trace(cls, trace: Trace) -> "ReplayVector":
        return cls(trace.cycle, trace.stimulus)

    def line(self) -> str:
        stim = self.stimulus
        cmd = stim.command or Command()
        vector = (
            self.cycle, int(stim.reset), int(stim.tick), int(stim.response_ready),
            int(stim.quote_ready), int(stim.command is not None), *cmd.values(),
        )
        return " ".join(map(str, vector))


@dataclass
class RunStats:
    cycles: int = 0
    accepts: int = 0
    response_transfers: int = 0
    quote_transfers: int = 0
    applied: int = 0
    blocked_by_epoch: int = 0
    blocked_by_fence: int = 0
    blocked_by_supersession: int = 0
    widest_offered_spread: int = 0
    largest_offered_size: int = 0
    max_response_latency: int = 0
    max_backlog: int = 0
    reset_discards: int = 0
    status_counts: Optional[Dict[str, int]] = None

    def visit(self, entry: Entry, trace: Trace) -> None:
        row = entry.register
        self.cycles += 1
        self.max_backlog = max(self.max_backlog, row.pending)
        if row.accepted:
            self.accepts += 1
        if row.response_ready and trace.observed.response is not None:
            self.response_transfers += 1
        if row.quote_ready and trace.observed.quote is not None:
            quote = trace.observed.quote
            self.quote_transfers += 1
            self.widest_offered_spread = max(self.widest_offered_spread, quote.ask-quote.bid)
            self.largest_offered_size = max(self.largest_offered_size, quote.size)
            if row.quote_action == "publish_intent":
                self.applied += 1
            elif row.quote_action == "drop_epoch":
                self.blocked_by_epoch += 1
            elif row.quote_action == "drop_ineligible":
                self.blocked_by_fence += 1
            elif row.quote_action == "drop_superseded":
                self.blocked_by_supersession += 1


def expected_pins(entry: Entry) -> Combinational:
    row = entry.register
    return Combinational(
        source_ready=row.source_ready, response=entry.response, quote=entry.quote,
        live=tuple(row.live), eligible=tuple(row.eligible), pressure_bid=tuple(row.pressure_bid),
        pressure_ask=tuple(row.pressure_ask), epochs=tuple(row.epochs), quote_current=row.quote_current,
    )


def explain_context(history: Deque[str], entry: Entry, trace: Trace, detail: str) -> JournalError:
    map_ = entry.register
    want = expected_pins(entry)
    suffix = "; ".join(history)
    return JournalError(
        f"cycle {map_.cycle}: {detail}\n"
        f"  expected reset={map_.reset} ready={want.source_ready} epoch={want.epochs}, "
        f"offered={entry.command}\n"
        f"  observed reset={trace.stimulus.reset} ready={trace.observed.source_ready} "
        f"epoch={trace.observed.epochs}\n"
        f"  recent transitions: {suffix or '(none)'}"
    )


def describe_command(command: Command) -> str:
    try:
        name = Operation(command.op).name
    except ValueError:
        name = f"RESERVED_{command.op}"
    return f"bank {command.instrument} {name} seq={command.sequence}"


def audit_and_export(source: TextIO, vector_sink: TextIO) -> Tuple[Config, int, Dict]:
    cfg, profile = read_config(source)
    audit = Audit(cfg)
    count = 0
    for entry in read_entries(source, cfg):
        audit.visit(entry)
        if count == 0 and not entry.register.reset:
            raise JournalError("journal replay must begin with an explicit hardware reset")
        vector_sink.write(ReplayVector.from_entry(entry).line() + "\n")
        count += 1
    return cfg, count, {"profile": profile, **audit.finish()}


def compare(
    expected: Iterable[Entry], observed: Iterable[Trace], history_depth: int = 8,
) -> RunStats:
    if history_depth <= 0:
        raise ValueError("history depth must be positive")
    history: Deque[str] = deque(maxlen=history_depth)
    latency: Deque[int] = deque()
    status: Counter = Counter()
    summary = RunStats()
    for entry, trace in zip_longest(expected, observed):
        if entry is None:
            raise JournalError(f"observed RTL cycle {trace.cycle} past the journal end")
        if trace is None:
            raise JournalError(f"RTL trace ended before journal cycle {entry.register.cycle}")
        vector = ReplayVector.from_entry(entry)
        if vector != ReplayVector.from_trace(trace):
            raise explain_context(history, entry, trace, "clock controls or input payload differ")
        wanted = expected_pins(entry)
        if trace.observed != wanted:
            raise explain_context(history, entry, trace, print_differences(trace.observed, wanted))
        row = entry.register
        if row.reset:
            history.append(f"{row.cycle} reset")
            summary.reset_discards += len(latency)
            latency.clear()
        if entry.response is not None and row.response_ready:
            summary.max_response_latency = max(summary.max_response_latency, row.cycle-latency.popleft())
            status[Status(entry.response.status).name] += 1
        if row.accepted:
            history.append(f"{row.cycle} accept {describe_command(entry.command)}")
            latency.append(row.cycle)
        if entry.quote is not None and row.quote_ready:
            history.append(f"{row.cycle} quote {entry.quote.instrument}/{entry.quote.revision} {row.quote_action}")
        summary.visit(entry, trace)
    if latency:
        raise JournalError("pending diagnostics at end of RTL replay")
    summary.status_counts = dict(status)
    return summary


def validate_simulator_config(cfg: Config) -> None:
    limits = {
        "banks": 8, "slots": 512, "levels": 256, "quantity_width": 30,
        "sequence_width": 30, "tag_width": 30,
    }
    for field, limit in limits.items():
        if getattr(cfg, field) > limit:
            raise JournalError(f"{field} exceeds the offline simulation vector limit of {limit}")
    if cfg.quote_size > cfg.quantity_max or cfg.imbalance_q8 > 256:
        raise JournalError("quote_size or Q0.8 imbalance is outside the legal parameter range")
    if not all(value <= cfg.levels for value in (cfg.half_spread, cfg.skew_ticks, cfg.minimum_spread)):
        raise JournalError("price policy parameters exceed the configured grid")


def compile_rtl(cfg: Config, iverilog: str, simulator: Path) -> None:
    validate_simulator_config(cfg)
    params = []
    for name, value in cfg.rtl_parameters().items():
        params += ["-P", f"tb_market_state_quote_engine.{name}={value}"]
    run(
        [iverilog, "-g2012", "-Wall", "-s", "tb_market_state_quote_engine", *params,
         "-o", str(simulator), "-f", "rtl.f", "tb_market_state_quote_engine.sv"],
        cwd=SOURCES, check=True,
    )


def compare_files(journal: Path, trace: Path) -> Dict:
    with journal.open(encoding="utf-8") as expected:
        cfg, profile = read_config(expected)
        judge = compare(read_entries(expected, cfg), records(trace, cfg))
    return {"profile": profile, **asdict(judge)}


def run_cosim(args: argparse.Namespace) -> Dict:
    work = args.build.resolve()
    work.mkdir(parents=True, exist_ok=True)
    vectors, raw_trace = work / "clock_vectors.txt", work / "rtl_trace.txt"
    simulator = work / "journal_cosim.vvp"
    with args.journal.open(encoding="utf-8") as journal, vectors.open("w", encoding="utf-8") as out:
        cfg, count, _ = audit_and_export(journal, out)
    compile_rtl(cfg, args.iverilog, simulator)
    run([args.vvp, str(simulator), f"+cycle_vectors={vectors}", f"+trace={raw_trace}"], check=True)
    checked = compare_files(args.journal, raw_trace)
    if checked["cycles"] != count:
        raise JournalError("comparison did not cover every exported cycle")
    return checked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    drive = sub.add_parser("run", help="audit, simulate, and compare a JSONL journal")
    drive.add_argument("journal", type=Path)
    drive.add_argument("--build", type=Path, required=True)
    drive.add_argument("--iverilog", default="iverilog")
    drive.add_argument("--vvp", default="vvp")
    compare_cmd = sub.add_parser("compare", help="compare an existing RTL trace against the journal")
    compare_cmd.add_argument("journal", type=Path)
    compare_cmd.add_argument("trace", type=Path)
    compare_cmd.add_argument("--out", type=Path)
    export = sub.add_parser("vectors", help="verify a journal and export the input wire vectors")
    export.add_argument("journal", type=Path)
    export.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "run":
        result = run_cosim(args)
        (args.build / "comparison.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    elif args.action == "compare":
        result = compare_files(args.journal, args.trace)
        if args.out:
            args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    else:
        with args.journal.open(encoding="utf-8") as source, args.out.open("w", encoding="utf-8") as out:
            _, count, report = audit_and_export(source, out)
        result = {"exported_cycles": count, "profile": report["profile"]}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
