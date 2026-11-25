"""Build synthetic replay fault traces for the integrated OUCH testbench."""

from __future__ import annotations

import argparse
import random
from dataclasses import replace
from pathlib import Path

from gen_ouch_vectors import DEFAULT_LOGIN, SESSION, Scenario, randomized, socket_tx_boundary
from ouch_model import CommandKind as Cmd, NoticeKind
from ouch_protocol import Envelope, Status, VenueEvent, control, login_accepted, unframe


def request(s: Scenario, sequence: int, status: Status | None = None) -> None:
    s.login(replace(DEFAULT_LOGIN, session=SESSION, sequence=sequence), status)


def new_socket(s: Scenario) -> None:
    s.barrier()
    s.link(False)
    s.link(True)


def diverged_fill(s: Scenario) -> None:
    """Corrupt an execution under weak framing while evidence is being replayed."""
    s.up()
    ref = s.enter(0, qty=180)
    s.ack(0)
    for _ in range(s.device.replay_depth + 1):
        s.system()
    s.exec(0, 25, 124_900)
    pending = s.enter(1, qty=50)
    checkpoint = s.device.checkpoint
    oldest = checkpoint - s.device.replay_depth
    closed_snapshot = s.device.store.view(0).slot_fields()
    assert s.device.oldest_verifiable == oldest

    new_socket(s)
    request(s, oldest - 1, Status.REPLAY_WINDOW)
    request(s, oldest)
    s.rx(login_accepted(SESSION, oldest - 1), expected=Status.REPLAY_WINDOW)
    assert s.device.last_fault == Status.REPLAY_WINDOW

    new_socket(s)
    request(s, checkpoint, Status.REPLAY_FENCE)
    request(s, oldest)
    s.rx(login_accepted(SESSION, oldest + 1), expected=Status.REPLAY_FENCE)

    s.reconnect(oldest)
    for seq in range(oldest, checkpoint - 1):
        s.rx(s.history[seq - 1], max_keep=2, remember=False)
    original = s.history[checkpoint - 2]
    s.rx(control("H"), control("+", b"cursor remains at execution"))
    s.cmd(Cmd.RETRY, 1, ref=pending, expected=Status.NOT_ACTIVE)
    s.partial(original[:-2])
    new_socket(s)
    request(s, checkpoint, Status.REPLAY_FENCE)
    request(s, checkpoint - 1)
    s.rx(login_accepted(SESSION, checkpoint - 1), max_keep=4)

    event = bytearray(original)
    event[8] ^= 0x20  # Change one byte of the OUCH timestamp; framing remains valid.
    s.rx(bytes(event), max_keep=5, expected=Status.REPLAY_DIVERGED, remember=False)
    assert s.device.close_socket and s.device.unverified == checkpoint - 1
    assert s.device.store.view(0).slot_fields() == closed_snapshot

    new_socket(s)
    request(s, checkpoint, Status.REPLAY_FENCE)
    request(s, checkpoint - 1)
    s.rx(login_accepted(SESSION, checkpoint - 1), max_keep=5)
    s.rx(original, max_keep=2, remember=False)
    assert not s.device.replaying and s.device.unverified is None
    s.cmd(Cmd.RETRY, 1, ref=pending, expected=Status.OK)
    s.ack(1)
    s.exec(0, 1, 124_900)
    assert s.device.store.view(0).ref == ref
    assert s.device.store.view(0).total_filled == 26
    s.barrier(1)


def changed_appendage(s: Scenario) -> None:
    """Detect a byte change even when both frame encodings normalize identically."""
    s.up()
    s.enter(0, qty=70)
    s.ack(0)
    s.event_cancel(0, 1)
    sequence = s.device.checkpoint - 1
    original = s.history[sequence - 1]
    extended = Envelope("S", unframe(original).payload + b"\x00\x00").frame()

    new_socket(s)
    request(s, sequence)
    new_socket(s)  # Valid local request was accepted, but no peer reply survived.
    request(s, s.device.checkpoint, Status.REPLAY_FENCE)
    request(s, sequence)
    s.rx(login_accepted(SESSION, sequence))
    s.rx(extended, max_keep=3, expected=Status.REPLAY_DIVERGED, remember=False)
    assert s.device.store.view(0).leaves == 69

    s.reconnect(sequence)
    s.rx(original, remember=False)
    assert s.notices[-1].kind == NoticeKind.REPLAY
    s.event_cancel(0, 69, explicit=True)
    assert not s.device.store.view(0).live
    s.barrier()


def varied_replays(s: Scenario, rng: random.Random, rounds: int) -> None:
    """Walk the ring repeatedly while probing expiry, exact and regressed cursors."""
    s.up()
    for n in range(rounds):
        for _ in range(rng.randint(1, 5)):
            s.system(rng.choice("SE"))
        checkpoint = s.device.checkpoint
        oldest = max(1, checkpoint - s.device.replay_depth)
        requested = rng.randrange(oldest, checkpoint + 1)
        accepted = rng.randrange(oldest, requested + 1)
        new_socket(s)
        if oldest > 1 and n % 3 == 0:
            request(s, oldest - 1, Status.REPLAY_WINDOW)
        request(s, requested)
        s.rx(login_accepted(SESSION, accepted), max_keep=rng.randint(1, 8))

        replay_cut = rng.randrange(accepted, checkpoint + 1)
        for sequence in range(accepted, replay_cut):
            s.rx(s.history[sequence - 1], max_keep=rng.randint(1, 8), remember=False)
            assert s.notices[-1].kind == NoticeKind.REPLAY
        if replay_cut != checkpoint:
            original = s.history[replay_cut - 1]
            if n % 5 == 0:
                s.partial(original[:rng.randint(2, len(original) - 1)])
            new_socket(s)
            request(s, checkpoint, Status.REPLAY_FENCE)
            request(s, replay_cut)
            s.rx(login_accepted(SESSION, replay_cut), max_keep=2)

        for sequence in range(replay_cut, checkpoint):
            s.rx(s.history[sequence - 1], max_keep=rng.randint(1, 8), remember=False)
            assert s.notices[-1].kind == NoticeKind.REPLAY
        assert s.device.checkpoint == checkpoint and s.device.unverified is None
    s.barrier()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--depth", required=True, type=int)
    parser.add_argument("--seed", required=True, type=lambda value: int(value, 0))
    parser.add_argument("--operations", type=int, default=550)
    args = parser.parse_args()
    if not 1 <= args.depth <= 256 or not 1 <= args.operations <= 30_000:
        parser.error("require 1..256 history rows and 1..30,000 randomized operations")
    s = Scenario(slots=8, replay_depth=args.depth)
    diverged_fill(s)
    changed_appendage(s)
    socket_tx_boundary(s)
    varied_replays(s, random.Random(args.seed), rounds=60)
    randomized(s, args.seed, args.operations)
    s.write(args.output)
    print(f"{len(s.actions)} verified-replay stimuli, {len(s.packets)} exact TX frames, "
          f"{len(s.notices)} expected notices at depth {args.depth}")


if __name__ == "__main__":
    main()
