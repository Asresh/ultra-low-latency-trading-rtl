"""Seal, independently replay, and inspect verified portfolio gateway captures.

The digest chain detects damaged or reordered records. Trust the terminal
digest through a separate authenticated channel if authenticity is required.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TextIO

from portfolio_model import Caps, PortfolioModel
from portfolio_vectors import PROFILES
from portfolio_wire import Kind, decode_row, expected_capture


SCHEMA = "day03-portfolio-journal-v1"
CHECKPOINT_SCHEMA = "day03-portfolio-checkpoint-v1"
MAX_RECORD_BYTES = 1 << 16


class JournalError(ValueError):
    """The input stream has an invalid schema, result, sequence, or digest."""


def canonical(payload) -> str:
    """Produce a deterministic JSON encoding without ambiguous whitespace or NaNs."""
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def digest(payload) -> str:
    """Return SHA-256 over the exact canonical UTF-8 bytes."""
    return sha256(canonical(payload).encode("ascii")).hexdigest()


def with_digest(payload: dict) -> dict:
    """Append a checksum that covers all supplied metadata and payload fields."""
    return {**payload, "digest": digest(payload)}


def require_digest(record, fields: set[str], line: int) -> str:
    """Reject noncanonical schemas and verify the record checksum."""
    if type(record) is not dict or set(record) != fields | {"digest"}:
        raise JournalError(f"line {line}: unexpected journal record fields")
    payload = {name: record[name] for name in fields}
    actual = digest(payload)
    if record["digest"] != actual:
        raise JournalError(f"line {line}: digest mismatch")
    return actual


def hex_rows(source: TextIO, label: str):
    """Read checked hardware captures while reporting damaged row locations."""
    for number, line in enumerate(source, 1):
        fields = line.strip().split()
        if not fields:
            raise JournalError(f"{label}:{number}: blank capture row")
        try:
            if any(not set(field) <= set("0123456789abcdefABCDEF") for field in fields):
                raise ValueError
            yield tuple(int(field, 16) for field in fields)
        except ValueError as err:
            raise JournalError(f"{label}:{number}: expected unsigned hexadecimal") from err


def integer_row(value, line: int) -> tuple[int, ...]:
    """Require an integer list so Python booleans cannot alias hardware values."""
    if type(value) is not list or any(type(field) is not int or field < 0 for field in value):
        raise JournalError(f"line {line}: expected an unsigned integer row")
    return tuple(value)


def json_records(source: TextIO):
    """Decode journal records with line-numbered errors and bounded record length."""
    number = 0
    while True:
        line = source.readline(MAX_RECORD_BYTES + 1)
        if not line:
            return
        number += 1
        if len(line) > MAX_RECORD_BYTES or not line.endswith("\n"):
            raise JournalError(f"line {number}: oversized or unterminated record")
        try:
            record = json.loads(line)
        except ValueError as err:
            raise JournalError(f"line {number}: malformed JSON record") from err
        if canonical(record) + "\n" != line:
            raise JournalError(f"line {number}: record is not canonical")
        yield record


def pack_journal(profile_name: str, inputs, actual, output: TextIO) -> str:
    """Check independent model outcomes before emitting the sealed journal."""
    profile = PROFILES[profile_name]
    model = PortfolioModel(profile)
    header = with_digest({"schema": SCHEMA, "profile": profile_name})
    output.write(canonical(header) + "\n")
    previous = header["digest"]
    receipts = iter(actual)
    count = 0
    for number, wire in enumerate(inputs, 1):
        issued = decode_row(profile, wire, number)
        if number == 1 and issued.kind is not Kind.RESET:
            raise JournalError("the first action must reset to a known initial state")
        expected = expected_capture(model, number, issued)
        got = next(receipts, None) if expected is not None else None
        if got != expected:
            raise JournalError(f"row {number}: capture disagrees with replay\nexpected {expected}\nactual   {got}")
        entry = with_digest({"order": number, "previous": previous, "input": wire, "result": got})
        output.write(canonical(entry) + "\n")
        previous = entry["digest"]
        count = number
    if not count or next(receipts, None) is not None:
        raise JournalError("empty input or unmatched capture results")
    return previous


def check_journal(source: TextIO, requested_digest: str | None = None, replay: TextIO | None = None):
    """Reconstruct state from one complete chain, optionally writing simulator inputs."""
    records = iter(json_records(source))
    header = next(records, None)
    previous = require_digest(header, {"schema", "profile"}, 1)
    if header["schema"] != SCHEMA or type(header["profile"]) is not str or header["profile"] not in PROFILES:
        raise JournalError("unsupported journal schema or profile")
    profile = PROFILES[header["profile"]]
    model = PortfolioModel(profile)
    count = receipts = 0
    for number, entry in enumerate(records, 1):
        current = require_digest(entry, {"order", "previous", "input", "result"}, number + 1)
        if type(entry["order"]) is not int or entry["order"] != number or entry["previous"] != previous:
            raise JournalError(f"record {number}: bad order or previous digest")
        wire = integer_row(entry["input"], number + 1)
        issued = decode_row(profile, wire, number)
        if number == 1 and issued.kind is not Kind.RESET:
            raise JournalError("the first action must reset to a known initial state")
        expected = expected_capture(model, number, issued)
        got = integer_row(entry["result"], number + 1) if entry["result"] is not None else None
        if got != expected:
            raise JournalError(f"record {number}: captured result disagrees with reconstructed state")
        if replay is not None:
            replay.write(" ".join(f"{field:x}" for field in wire) + "\n")
        receipts += int(got is not None)
        count = number
        previous = current
    if not count:
        raise JournalError("journal does not contain a reset or any transactions")
    if requested_digest is not None and previous != requested_digest:
        raise JournalError("terminal anchor mismatch")
    return header["profile"], previous, count, receipts, model


def snapshot(profile: str, anchor: str, count: int, receipts: int, model: PortfolioModel) -> dict:
    """Export a checksummed operator snapshot keyed to the verified replay prefix."""
    native = model.gateway
    direction = [
        {"key": list(key), "policy": asdict(policy)}
        for key, policy in sorted(native.policies.items())
    ]
    cumulative = [
        {"key": list(key), "value": total}
        for key, total in sorted(native.executed.items())
    ]
    slots = [
        {"slot": slot, "key": list(order.key), "generation": order.generation,
         "event": order.event, "price": order.price, "quantity": order.qty,
         "filled": order.ever_filled, "reserved": order.reserved}
        for slot, order in sorted(native.orders.items())
    ]
    scopes = [
        {"account": account, "caps": asdict(model.caps.get(account, Caps())),
         "exposure": asdict(model.exposure(account))}
        for account in [*range(model.profile.accounts), None]
    ]
    return with_digest({
        "schema": CHECKPOINT_SCHEMA, "profile": profile, "terminal_digest": anchor,
        "rows": count, "receipts": receipts, "epoch": model.epoch, "halted": model.halted,
        "scopes": scopes, "native_policies": direction, "native_turnover": cumulative,
        "slots": slots,
    })


def check_paths(destination: Path, *sources: Path) -> None:
    """Keep explicit output paths separate from capture and journal inputs."""
    for source in sources:
        aliases = destination.exists() and source.exists() and destination.samefile(source)
        if aliases or destination.resolve() == source.resolve():
            raise JournalError(f"output would replace input {source}")


@contextmanager
def atomic_text(path: Path):
    """Leave all existing output intact unless the complete operation succeeds."""
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="ascii", newline="\n",
                                dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            yield output
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    seal = commands.add_parser("seal")
    seal.add_argument("--profile", choices=PROFILES, required=True)
    seal.add_argument("stimulus", type=Path)
    seal.add_argument("capture", type=Path)
    seal.add_argument("journal", type=Path)
    for action in ("check", "snapshot", "export-stimulus"):
        command = commands.add_parser(action)
        command.add_argument("journal", type=Path)
        command.add_argument("--terminal-digest")
        if action != "check":
            command.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "seal":
            check_paths(args.journal, args.stimulus, args.capture)
            with args.stimulus.open(encoding="ascii") as stimulus, args.capture.open(encoding="ascii") as capture:
                with atomic_text(args.journal) as journal:
                    anchor = pack_journal(
                        args.profile, hex_rows(stimulus, str(args.stimulus)),
                        hex_rows(capture, str(args.capture)), journal,
                    )
            print(anchor)
            return
        with args.journal.open(encoding="ascii") as journal:
            if args.action == "export-stimulus":
                check_paths(args.output, args.journal)
                with atomic_text(args.output) as output:
                    replayed = check_journal(journal, args.terminal_digest, output)
            else:
                replayed = check_journal(journal, args.terminal_digest)
        if args.action == "snapshot":
            check_paths(args.output, args.journal)
            with atomic_text(args.output) as output:
                output.write(canonical(snapshot(*replayed)) + "\n")
        _, anchor, count, receipts, _ = replayed
        print(f"PASS: {count} operations and {receipts} replies, terminal digest {anchor}")
    except (TypeError, ValueError, OSError) as err:
        parser.exit(1, f"error: {err}\n")


if __name__ == "__main__":
    main()
