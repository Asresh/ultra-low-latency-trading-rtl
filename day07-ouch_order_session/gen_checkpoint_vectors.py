"""Drive a checked cold restore through the integrated wire-session bench."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from gen_ouch_vectors import Act, DEFAULT_LOGIN, SESSION, Scenario
from ouch_checkpoint import (
    Archive,
    CheckpointStatus as CP,
    META_WORDS,
    ORDER_BITS,
    ORDER_WORDS,
    WORD,
    crc32c,
    wire_words,
    words,
)
from ouch_model import CommandKind as Cmd, Pending
from ouch_protocol import BLANK_SESSION, Status, control, login_accepted


IMAGE_ID = 0x21A00710002018B5


def cp_command(scenario: Scenario, action: int, result: CP, image: int = IMAGE_ID,
               variant: int = 0, bad_slot: int = 255) -> None:
    if action == 15:
        scenario._action(action, variant, int(result), image | (bad_slot << 64))
    else:
        scenario._action(action, int(result), payload=image)


def open_session(s: Scenario) -> None:
    s.reset()
    s._action(Act.LINK, 1)
    s._action(16)
    cp_command(s,13,CP.UNSAFE,image=0)
    s._action(Act.LINK, 0)
    cp_command(s,13,CP.BAD_CMD,image=1)
    cp_command(s,13,CP.OK,image=0)
    s.link(True)
    s.login()
    s.rx(control("+",b"cold checkpoint fixture"),login_accepted(SESSION,1),max_keep=2)
    s.system()
    cp_command(s,14,CP.UNSAFE)


def interrupted_command(s: Scenario) -> tuple[int,int,int | None]:
    original = s.enter(0,qty=55,price=125_000)
    s.ack(0)
    s.exec(0,11,124_900,match=0x6E55BABAF0120001)
    if s.device.capacity > 1:
        second = s.enter(1,qty=27,price=128_010,side="S")
        s.ack(1)
        s.exec(1,4,129_002,match=0x6E55BABAF0120002)
        s.cmd(Cmd.CANCEL,1,ref=second,qty=0,expected=Status.OK)
        assert s.device.store.view(1).pending == Pending.CANCEL
    else:
        second = None

    s.barrier()
    s.hold_tx_after(2)
    replacement = s.replace(0,qty=60,price=126_001)
    wire = s.packets.pop()
    s.aborted.append(wire[:16])
    s._action(Act.TX_WAIT)
    s.link(False)
    s.hold_tx_after(-1)
    s.barrier(0)
    assert s.device.store.view(0).pending == Pending.REPLACE
    return original,replacement,second


def image_mutations(archive: Archive) -> dict[int,tuple[bytes,CP,int]]:
    original = archive.encode()
    words_in = list(words(original))

    changed_crc = words_in.copy()
    changed_crc[-1] ^= 0x10

    padded = words_in.copy()
    padded[4 + META_WORDS + ORDER_WORDS - 1] |= (1 << (ORDER_BITS % 32))
    padded[-1] = crc32c(wire_words(padded[:-1]))

    invalid_session = replace(archive,metadata=replace(archive.metadata,session=BLANK_SESSION))
    impossible_height = replace(archive,metadata=replace(archive.metadata,high_water=0))
    variants = {
        1: (wire_words(changed_crc),CP.BAD_CRC,255),
        2: (wire_words(padded),CP.BAD_FORMAT,255),
        3: (invalid_session.encode_unchecked(),CP.BAD_STATE,255),
        4: (original[:-8],CP.BAD_FRAME,255),
        5: (original + WORD.pack(0),CP.BAD_FRAME,255),
        6: (impossible_height.encode_unchecked(),CP.BAD_STATE,0),
    }
    if len(archive.records) > 1:
        aliased = replace(archive,records=(archive.records[0],archive.records[0],*archive.records[2:]))
        variants[7] = (aliased.encode_unchecked(),CP.BAD_STATE,1)
    return variants


def capture_requires_completed_replay(s: Scenario) -> None:
    start = s.device.checkpoint - 1
    s.link(True)
    s.login(replace(DEFAULT_LOGIN,session=SESSION,sequence=start))
    s.rx(login_accepted(SESSION,start))
    s.link(False)
    s.barrier()
    cp_command(s,14,CP.UNSAFE)
    s.link(True)
    s.login(replace(DEFAULT_LOGIN,session=SESSION,sequence=start))
    s.rx(login_accepted(SESSION,start))
    s.rx(s.history[start-1],remember=False)
    s.link(False)
    s.barrier()


def write_variant(directory: Path, number: int, payload: bytes) -> None:
    values = words(payload)
    (directory / f"import-{number}.hex").write_text(
        f"{len(values)}\n"+"".join(f"{word:08x}\n" for word in values),
        encoding="ascii",
    )


def checkpoint_restore(s: Scenario, directory: Path, archive: Archive) -> None:
    expected = archive.encode()
    (directory / "expected-archive.bin").write_bytes(expected)
    cp_command(s,14,CP.OK)

    # Clear the FPGA and leave its boot permission closed while testing
    # malformed inputs. The final import gets the words captured from RTL.
    s._action(Act.RESET)
    s._action(16)
    cp_command(s,15,CP.ABORTED,variant=-3)  # reset a partially staged image
    s._action(16)
    cp_command(s,15,CP.ABORTED,variant=-1)
    s._action(16)
    for number,(payload,status,bad_slot) in image_mutations(archive).items():
        write_variant(directory,number,payload)
        cp_command(s,15,status,variant=number,bad_slot=bad_slot)
        s._action(16)

    cp_command(s,15,CP.BAD_ID,image=IMAGE_ID+1,variant=0)
    s._action(16)
    cp_command(s,15,CP.OK,variant=0)
    s.device = archive.restore(s.device.replay_depth)
    s.barrier(0)
    if s.device.capacity > 1:
        s.barrier(1)
    cp_command(s,15,CP.UNSAFE,variant=-2)


def reconnect_and_settle(s: Scenario, first: int, replacement: int, second: int | None) -> None:
    saved = s.device.checkpoint
    s.link(True)
    s.login(replace(DEFAULT_LOGIN,session=SESSION,sequence=saved-1),Status.REPLAY_WINDOW)
    s.login(replace(DEFAULT_LOGIN,session=SESSION,sequence=saved))
    s.rx(login_accepted(SESSION,saved),max_keep=1)
    s.cmd(Cmd.RETRY,0,ref=replacement,expected=Status.OK)
    s.exec(0,3,124_801)  # old reference is live while replace is pending
    s.barrier(0)
    assert s.device.store.view(0).ref == first
    s.ack(0,quantity=46)
    assert s.device.store.view(0).ref == replacement
    if second is not None:
        s.cmd(Cmd.RETRY,1,ref=second,expected=Status.OK)
        s.event_cancel(1,23)
        s.barrier(1)
        assert not s.device.store.view(1).live
    s.event_cancel(0,46)
    for slot in range(s.device.capacity):
        s.barrier(slot)
    assert s.device.store.live_count == s.device.store.pending_count == 0


def restored_kill(s: Scenario,directory: Path,archive: Archive,
                  replace_ref: int,cancel_ref: int | None) -> None:
    s.link(False)
    s.barrier()
    s._action(Act.RESET)
    killed = replace(archive,image_id=IMAGE_ID+2,metadata=replace(archive.metadata,killed=True))
    write_variant(directory,8,killed.encode())
    cp_command(s,15,CP.OK,image=killed.image_id,variant=8)
    s.device = killed.restore(s.device.replay_depth)
    s.barrier(0)
    s.link(True)
    s.login(replace(DEFAULT_LOGIN,session=SESSION,sequence=s.device.checkpoint))
    s.rx(login_accepted(SESSION,s.device.checkpoint))
    s.cmd(Cmd.RETRY,0,ref=replace_ref,expected=Status.KILLED)
    if cancel_ref is not None:
        s.cmd(Cmd.RETRY,1,ref=cancel_ref,expected=Status.OK)
    s.barrier()


def compare(directory: Path, saved: Path | None = None) -> None:
    raw = (directory / "expected-archive.bin").read_bytes()
    actual = wire_words(int(value,16) for value in
                        (directory / "hardware_archive.hex").read_text(encoding="ascii").split())
    if actual != raw:
        for index,(wanted,received) in enumerate(zip(words(raw),words(actual))):
            if wanted != received:
                raise AssertionError(f"archive word {index} expected {wanted:08x}, observed {received:08x}")
        raise AssertionError("archive frame length mismatch")
    parsed = Archive.decode(actual,IMAGE_ID)
    if saved is not None:
        saved.write_bytes(actual)
    print(f"PASS {len(raw)//4} exact archive words for {len(parsed.records)} slots, "
          f"saved next sequence {parsed.metadata.checkpoint}, high reference {parsed.metadata.high_water}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command",required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("directory",type=Path)
    gen.add_argument("--slots",type=int,choices=(1,4,8,12),default=8)
    check = sub.add_parser("check")
    check.add_argument("directory",type=Path)
    check.add_argument("--save",type=Path)
    args = parser.parse_args()
    if args.command == "check":
        compare(args.directory,args.save)
        return

    args.directory.mkdir(exist_ok=True,parents=True)
    s = Scenario(args.slots,replay_depth=5)
    open_session(s)
    original,replacement,other = interrupted_command(s)
    capture_requires_completed_replay(s)
    image = Archive.from_session(IMAGE_ID,s.device)
    checkpoint_restore(s,args.directory,image)
    reconnect_and_settle(s,original,replacement,other)
    restored_kill(s,args.directory,image,replacement,other)
    s.write(args.directory)


if __name__ == "__main__":
    main()
