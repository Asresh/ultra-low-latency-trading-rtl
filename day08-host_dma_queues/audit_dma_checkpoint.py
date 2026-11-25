"""Check resume plans against raw bytes and rollover from the Icarus integration bench."""

import argparse
import json
from pathlib import Path
from struct import pack
from tempfile import TemporaryDirectory
from uuid import UUID

from dma_checkpoint import plan_document
from dma_queue_journal import JournalStore, QueueIdentity, Snapshot


IDENTITY = QueueIdentity(UUID("c23ea317-dbf2-4d18-a05d-024acd663084"), 0, 0xfffffffe, 8, 0x100, 0x300)
STAGES = ("initial", "first_held", "first_retired", "next_held", "next_retired", "complete")


def descriptor(operation, flags, tag, cookie):
    return pack("<IHBBQQII", 0, tag, flags, operation, 0, cookie, 0, 0)


def result_prefix(operation, status, tag, cookie):
    return pack("<IHBBQ3xBI", 0, tag, operation, status, cookie, 0, 0)


def read_snapshots(directory):
    snapshots = {}
    for line in (directory / "cursors.txt").read_text(encoding="ascii").splitlines():
        name, *numbers = line.split()
        if name not in STAGES or name in snapshots:
            raise AssertionError(f"unknown or duplicate capture {name}")
        sq_head, sq_tail, cq_head, cq_tail, fatal, enabled = (int(x, 16) for x in numbers)
        if enabled not in (0, 1):
            raise AssertionError(f"invalid capture enabled bit {enabled}")
        snapshot = Snapshot(
            IDENTITY, sq_head, sq_tail, cq_head, cq_tail,
            (directory / f"{name}.sq.bin").read_bytes(), (directory / f"{name}.cq.bin").read_bytes(),
            fatal, bool(enabled),
        )
        payload = snapshot.encode()
        (directory / f"{name}.dmcap").write_bytes(payload)
        snapshots[name] = Snapshot.decode(payload)
    if tuple(snapshots) != STAGES:
        raise AssertionError(f"incorrect capture sequence {tuple(snapshots)}")
    return snapshots


def check_plan(plan, cursors, ordinals, archived):
    got = (plan.submit_tail, plan.release_head)
    slots = [(write.ordinal, write.address) for write in plan.writes]
    positions = (plan.completed_begin, plan.completed_end)
    if (got, slots, positions) != (cursors, ordinals, archived):
        raise AssertionError(f"unexpected resume plan {plan_document(plan)}")


def verify(directory):
    snapshots = read_snapshots(directory)
    first_cookie, second_cookie, third_cookie = (0xc0ffee0000000000 + i for i in range(1, 4))
    first = descriptor(3, 0, 0x1001, first_cookie)
    second = descriptor(0xa5, 0, 0x1002, second_cookie)
    third = descriptor(3, 1, 0x1003, third_cookie)
    (directory / "first-submissions.bin").write_bytes(first + second)
    (directory / "next-submission.bin").write_bytes(third)
    first_prefix = result_prefix(3, 0, 0x1001, first_cookie)
    second_prefix = result_prefix(0xa5, 1, 0x1002, second_cookie)
    third_prefix = result_prefix(3, 0, 0x1003, third_cookie)
    seed = IDENTITY.seed

    with TemporaryDirectory(prefix="dma-restart-", dir=directory) as scratch:
        path = Path(scratch) / "queue.journal"
        JournalStore(path).create(IDENTITY)
        JournalStore(path).prepare(0, (first, second))
        prepared = JournalStore(path).reconcile(snapshots["initial"])
        check_plan(prepared, (0, seed), [(0, 0x1c0), (1, 0x1e0)], (0, 0))
        if [item.record for item in prepared.writes] != [first, second]:
            raise AssertionError("resume SQ writes changed durable descriptor bytes")

        # The first CQ words are already in memory, but neither engine cursor advanced.
        held = snapshots["first_held"]
        first_held_bytes = held.record(held.cq_image, 0)
        if first_held_bytes[:24] != first_prefix:
            raise AssertionError("held capture lacks physical, uncommitted CQ bytes")
        uncommitted = JournalStore(path).reconcile(held)
        check_plan(uncommitted, (0, seed), [], (0, 0))
        if uncommitted.has_doorbell:
            raise AssertionError("a stalled CQ B released or replayed an outstanding command")

        # Restart again after seeing the first two completions but before acking them.
        JournalStore(path).prepare(2, (third,))
        retire = JournalStore(path).reconcile(snapshots["first_retired"])
        check_plan(retire, (1, 0), [(2, 0x100)], (0, 2))
        retry = JournalStore(path).reconcile(snapshots["first_retired"])
        check_plan(retry, (1, 0), [(2, 0x100)], (2, 2))
        if JournalStore(path).read().completions[0] != first_held_bytes:
            raise AssertionError("RTL changed the CQ words between B stall and publication")

        # The joint doorbell has now taken effect. Ordinal 2 must never be reposted.
        after_ack = JournalStore(path).reconcile(snapshots["next_held"])
        check_plan(after_ack, (1, 0), [], (2, 2))
        if after_ack.has_doorbell:
            raise AssertionError("a post-ack restart repeated a completed doorbell")
        last = JournalStore(path).reconcile(snapshots["next_retired"])
        check_plan(last, (1, 1), [], (2, 3))
        done = JournalStore(path).reconcile(snapshots["complete"])
        check_plan(done, (1, 1), [], (3, 3))
        journal = JournalStore(path).read()
        if [record[:24] for record in journal.completions] != [first_prefix, second_prefix, third_prefix]:
            raise AssertionError("archived CQ records differ from the expected byte layouts")
        if (journal.published, journal.released, done.has_doorbell) != (3, 3, False):
            raise AssertionError("restart did not converge to empty rings")
        (directory / "example-resume.json").write_text(json.dumps(plan_document(retire), indent=2) + "\n")

    print("PASS durable DMA checkpoint: 6 real ring captures, 3 exact archived completions, rollover and B stall")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    verify(parser.parse_args().directory)
