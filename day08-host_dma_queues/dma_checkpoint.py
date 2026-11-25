"""Inspect and recover a Day 08 host queue from binary captures and a durable journal."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from uuid import UUID

from dma_protocol import Completion
from dma_queue_journal import JournalStore, QueueIdentity, Snapshot, rows


def number(text):
    return int(text, 0)


def identity_document(identity):
    return {
        "generation": str(identity.generation), "queue": identity.number,
        "seed": identity.seed, "depth": identity.depth,
        "sq_base": identity.sq_base, "cq_base": identity.cq_base,
    }


def plan_document(plan):
    return {
        "format": "day08-resume-v1",
        "identity": identity_document(plan.identity),
        "observed": {"sq_tail": plan.observed_sq_tail, "cq_head": plan.observed_cq_head},
        "doorbell": {"sq_tail": plan.submit_tail, "cq_head": plan.release_head} if plan.has_doorbell else None,
        "writes": [
            {"ordinal": item.ordinal, "address": item.address, "record": item.record.hex()}
            for item in plan.writes
        ],
        "archived": {"begin": plan.completed_begin, "end": plan.completed_end},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="bind a new local journal to a queue")
    initialize.add_argument("--generation", type=UUID, required=True)
    for option in ("queue", "seed", "depth", "sq-base", "cq-base"):
        initialize.add_argument(f"--{option}", type=number, required=True)
    prepare = commands.add_parser("prepare", help="stage consecutive 32-byte descriptors from a binary file")
    prepare.add_argument("ordinal", type=number)
    prepare.add_argument("records", type=Path)
    reconcile = commands.add_parser("reconcile", help="archive committed completions and return a doorbell plan")
    reconcile.add_argument("snapshot", type=Path)
    show = commands.add_parser("show", help="show durable cursors and an optional completion range")
    show.add_argument("--begin", type=number, default=0)
    show.add_argument("--count", type=number, default=0)
    args = parser.parse_args()
    store = JournalStore(args.journal)
    try:
        if args.command == "init":
            identity = QueueIdentity(args.generation, args.queue, args.seed, args.depth, args.sq_base, args.cq_base)
            store.create(identity)
            result = identity_document(identity)
        elif args.command == "prepare":
            journal = store.prepare(args.ordinal, rows(args.records.read_bytes()))
            result = {"prepared": len(journal.submissions)}
        elif args.command == "reconcile":
            result = plan_document(store.reconcile(Snapshot.decode(args.snapshot.read_bytes())))
        else:
            if args.begin < 0 or args.count < 0:
                raise ValueError("completion range must be nonnegative")
            journal = store.read()
            result = {
                "identity": identity_document(journal.identity),
                "prepared": len(journal.submissions),
                "published": journal.published,
                "released": journal.released,
                "archived": len(journal.completions),
                "completions": [
                    {"ordinal": ordinal, **asdict(Completion.decode(record))}
                    for ordinal, record in enumerate(
                        journal.completions[args.begin:args.begin + args.count], start=args.begin,
                    )
                ],
            }
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
