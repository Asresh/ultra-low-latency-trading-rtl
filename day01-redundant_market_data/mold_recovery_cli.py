"""Operate an offline, epoch-scoped archive of MoldUDP64 datagrams."""

import argparse
from contextlib import nullcontext
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys

from mold_recovery_store import ArchiveError, Identity, RecoveryArchive


def number(value):
    """Accept decimal argument values or an explicitly 0x-prefixed hexadecimal value."""
    try:
        return int(value, 16) if value.startswith(("0x", "0X")) else int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer or 0x-prefixed hex") from exc


def wire_bytes(value):
    """Decode textual wire bytes with a uniform CLI diagnostic."""
    try:
        return bytes.fromhex(value)
    except (TypeError, ValueError) as exc:
        raise ArchiveError("wire field is not a hexadecimal byte string") from exc


def printable(context):
    """Keep the session byte-exact even when it is not valid UTF-8."""
    row = asdict(context)
    row["session"] = context.session.hex()
    row["retained_sequence"] = context.sequence(context.retained)
    row["advertised_sequence"] = context.sequence(context.advertised)
    return row


def print_json(row):
    print(json.dumps(row, sort_keys=True))


def identity_args(command):
    command.add_argument("--slot", type=number, required=True)
    command.add_argument("--epoch", type=number, required=True)


def datagram_args(command, *, intake=False):
    command.add_argument("--max-bytes", type=number, default=1536)
    command.add_argument("--max-blocks", type=number, default=64)
    if intake:
        command.add_argument("--max-message", type=number, default=64)
    else:
        command.add_argument("--request-cap", type=number, default=1024)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    sub = parser.add_subparsers(dest="operation", required=True)

    register = sub.add_parser("register", help="install an identity once")
    identity_args(register)
    register.add_argument("--session-hex", required=True)
    register.add_argument("--seed", type=number, required=True)
    register.add_argument("--record-limit", type=number, default=1_000_000)

    ingest = sub.add_parser("ingest", help="ingest JSONL tagged datagrams from a file or stdin")
    ingest.add_argument("input", help="JSONL path, or - for stdin")
    datagram_args(ingest, intake=True)

    reply = sub.add_parser("reply", help="build recovery datagrams for one raw request")
    identity_args(reply)
    reply.add_argument("request_hex")
    datagram_args(reply)

    prune = sub.add_parser("prune", help="remove archived messages before a new retention boundary")
    identity_args(prune)
    prune.add_argument("--keep-from", type=number, required=True)

    audit = sub.add_parser("audit", help="validate and inspect a registered context")
    identity_args(audit)
    audit.add_argument("--gap-limit", type=number, default=100)
    return parser


def read_capture_lines(source):
    """Yield up to one independently parseable tagged wire packet per line."""
    for index, line in enumerate(source, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or set(item) != {"slot", "epoch", "source", "wire"}:
                raise ArchiveError("expected slot, epoch, source and wire fields")
            yield index, Identity(item["slot"], item["epoch"]), item["source"], wire_bytes(item["wire"])
        except (ArchiveError, json.JSONDecodeError) as exc:
            raise ArchiveError(f"line {index}: {exc}") from exc


def ingest_records(args):
    """Feed a stream of datagrams and report the durable disposition of each."""
    source = (nullcontext(sys.stdin) if args.input == "-"
              else Path(args.input).open(encoding="utf-8"))
    with source as lines, RecoveryArchive(args.archive, access="rw") as archive:
        for number, identity, lane, wire in read_capture_lines(lines):
            try:
                result = archive.intake(
                    identity, wire, lane, max_bytes=args.max_bytes,
                    max_message=args.max_message, max_blocks=args.max_blocks,
                )
            except ArchiveError as exc:
                raise ArchiveError(f"line {number}: {exc}") from exc
            print_json({"line": number, **asdict(identity), **asdict(result)})


def execute(args):
    if args.operation == "ingest":
        ingest_records(args)
        return
    identity = Identity(args.slot, args.epoch)
    if args.operation == "register":
        with RecoveryArchive(args.archive, access="rwc") as archive:
            context = archive.register(
                identity, wire_bytes(args.session_hex), args.seed, record_limit=args.record_limit,
            )
            print_json(printable(context))
    elif args.operation == "prune":
        with RecoveryArchive(args.archive, access="rw") as archive:
            print_json({"removed": archive.prune(identity, args.keep_from)})
    elif args.operation == "reply":
        with RecoveryArchive(args.archive) as archive:
            response = archive.retrieve(
                identity, wire_bytes(args.request_hex), max_bytes=args.max_bytes,
                max_blocks=args.max_blocks, request_cap=args.request_cap,
            )
            row = asdict(response)
            row["datagrams"] = [raw.hex() for raw in response.datagrams]
            print_json(row)
    else:
        with RecoveryArchive(args.archive) as archive:
            context, gaps, truncated = archive.inspect(identity, limit=args.gap_limit)
            print_json({**printable(context), "missing_runs": gaps, "gaps_truncated": truncated})


def main():
    parser = build_parser()
    try:
        execute(parser.parse_args())
    except (ArchiveError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"recovery archive: {exc}\n")


if __name__ == "__main__":
    main()
