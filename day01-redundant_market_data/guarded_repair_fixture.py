"""Build a checked, fault-injected relay trace from accepted FPGA requests."""

import argparse
from dataclasses import dataclass
from pathlib import Path
import tempfile

from mold_recovery_store import ArchiveError, RecoveryArchive
from recovery_guard_model import Route, Verdict
from recovery_guard_vectors import packet_beats
from session_repair_example import load_requests, verify_requests
from wire_codec import decode_packet, make_packet


@dataclass(frozen=True)
class FaultFrame:
    route: Route
    expected: Verdict
    wire: bytes


def scenarios(cases):
    """Return valid split replies mixed with byte, scope, and delivery faults."""
    first_case, first_answer = cases[0]
    second_case, second_answer = cases[1]
    first_slot = first_case.identity.slot
    second_slot = second_case.identity.slot
    epoch_a = first_case.identity.epoch
    epoch_b = second_case.identity.epoch
    path_a, path_b = Route(first_slot, epoch_a, 1), Route(second_slot, epoch_b, 1)
    head_a, tail_a = first_answer.datagrams
    head_b, tail_b = second_answer.datagrams

    one = decode_packet(head_a)
    outside = make_packet(one.session, one.first + len(first_answer.datagrams), one.messages)
    yield FaultFrame(path_a, Verdict.RANGE, outside)
    yield FaultFrame(path_a, Verdict.FRAME, head_a[:-1])
    yield FaultFrame(Route(second_slot, epoch_b - 1, 1), Verdict.EPOCH, head_b)
    yield FaultFrame(Route(first_slot, epoch_a, 2), Verdict.TOKEN, tail_a)
    yield FaultFrame(path_a, Verdict.ACCEPT, tail_a)
    yield FaultFrame(path_a, Verdict.DUPLICATE, tail_a)
    yield FaultFrame(path_b, Verdict.ACCEPT, tail_b)
    yield FaultFrame(path_a, Verdict.ACCEPT, head_a)
    yield FaultFrame(path_b, Verdict.ACCEPT, head_b)


def build(requests_path, fixture_path):
    requests = load_requests(requests_path)
    with tempfile.TemporaryDirectory() as scratch:
        with RecoveryArchive(Path(scratch) / "repair.sqlite", access="rwc") as archive:
            answers = verify_requests(requests, archive)

    cases = tuple(scenarios(answers))
    with fixture_path.open("w", encoding="ascii") as output:
        output.write(f"{len(cases)}\n")
        for case in cases:
            route = case.route
            beats = tuple(packet_beats(case.wire, route))
            output.write(
                f"{route.slot} {route.epoch} {route.token} {int(case.expected)} {len(beats)}\n"
            )
            for beat in beats:
                output.write(f"{beat.data:016x} {beat.keep:02x} {int(beat.first)*2+beat.last:x}\n")
    return len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requests", type=Path)
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args()
    try:
        print(f"Prepared {build(args.requests, args.fixture)} archive-backed guard frames")
    except (ArchiveError, OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
