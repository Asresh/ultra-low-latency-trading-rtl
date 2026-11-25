"""Answer checked RTL recovery requests and write beat vectors for a second simulation."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import random
import tempfile

from mold_recovery_store import ArchiveError, Identity, RecoveryArchive, Request
from wire_codec import COMMON, decode_packet, make_packet, words


@dataclass(frozen=True)
class RepairCase:
    identity: Identity
    session: bytes
    seed: int
    first_missing: int
    references: tuple

    def messages(self):
        return tuple(b"D" + COMMON.pack(0, 0, bytes(6), ref) for ref in self.references)


CASES = (
    RepairCase(Identity(0, 2), b"RENAME0004", 900, 901, (0xA901, 0xA902)),
    RepairCase(Identity(1, 2), b"BETA000002", 0, 0, (0xB000, 0xB001)),
)


def load_requests(path):
    observed = {}
    with path.open(encoding="ascii") as handle:
        for line in handle:
            slot, epoch, payload = line.split()
            identity = Identity(int(slot), int(epoch))
            if identity in observed:
                raise ArchiveError("trace contains more than one sampled request for a slot")
            observed[identity] = bytes.fromhex(payload)
    if set(observed) != {case.identity for case in CASES}:
        raise ArchiveError("did not find the two documented recovery requests")
    return observed


def verify_requests(requests, archive):
    """Recover two unrelated slot/epoch ranges from the first-arrival archive bodies."""
    answer = []
    for case in CASES:
        payload = requests[case.identity]
        req = Request.from_bytes(payload)
        refs = case.references
        if (req.session, req.first, req.count) != (case.session, case.first_missing, len(refs)):
            raise ArchiveError("hardware request does not describe the expected missing range")
        expected = case.messages()
        archive.register(case.identity, case.session, case.seed)
        source_wire = make_packet(case.session, case.first_missing, expected)
        archive.intake(case.identity, source_wire, "first capture")
        archive.intake(case.identity, source_wire, "mirrored capture")
        archive.intake(case.identity, make_packet(case.session, case.first_missing, (b"conflict",)),
                       "conflicting capture")
        context = archive.audit(case.identity)
        if (context.stored, context.duplicates, context.conflicts) != (len(refs), len(refs), 1):
            raise ArchiveError("archive reconciliation differs from the expected original and duplicates")
        reply = archive.retrieve(case.identity, payload, max_blocks=1, max_bytes=64)
        recovered = tuple(original for raw in reply.datagrams for original in decode_packet(raw).messages)
        if reply.kind != "complete" or recovered != expected:
            raise ArchiveError("archive return does not reproduce captured bytes")
        answer.append((case, reply))
    return answer


def write_beat_file(destination, reply_cases):
    """Use a distinct recovery lane per slot in the bench fixture."""
    rng = random.Random(9023)
    with destination.open("w", encoding="ascii") as handle:
        for case, reply in reply_cases:
            beats = [beat for payload in reply.datagrams for beat in words(payload, rng)]
            handle.write(f"{len(beats)}\n")
            for data, keep, first, last in beats:
                handle.write(f"{case.identity.slot} {data:016x} {keep:02x} {first*2+last:x}\n")


def report_json(reply_cases):
    return [
        {
            "slot": case.identity.slot,
            "epoch": case.identity.epoch,
            "request_sequence": case.first_missing,
            "first_unrequested_sequence": reply.next_sequence,
            "datagrams": len(reply.datagrams),
            "wire_bytes": sum(map(len, reply.datagrams)),
            "recovered_messages": reply.count,
        }
        for case, reply in reply_cases
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requests", type=Path)
    parser.add_argument("beat_output", type=Path)
    args = parser.parse_args()
    try:
        requests = load_requests(args.requests)
        with tempfile.TemporaryDirectory() as scratch:
            with RecoveryArchive(Path(scratch) / "replay.sqlite", access="rwc") as archive:
                reply_cases = verify_requests(requests, archive)
        write_beat_file(args.beat_output, reply_cases)
        print(json.dumps(report_json(reply_cases), indent=2))
    except (ArchiveError, OSError, ValueError) as exc:
        parser.exit(1, f"fixture repair: {exc}\n")


if __name__ == "__main__":
    main()
