"""Check a DCA1 tape, publish a terminal seal, or extract execution evidence."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import struct
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO, Iterator

from drop_copy_audit import MAX64, AuditError, Profile, Record, Replay


MAGIC = b"DCA1"
FORMAT_VERSION = 1
WORD = struct.Struct("<I")


def crc32c(data: bytes) -> int:
    """Independent reflected byte-at-a-time reference for the serial formatter."""
    value = 0xFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x82F63B78 if value & 1 else 0)
    return value ^ 0xFFFFFFFF


def payload_bytes(profile: Profile) -> int:
    return 4*((profile.record_bits+31)//32)


def frame(word: int,profile: Profile) -> bytes:
    if not 0 <= word < 1 << profile.record_bits:
        raise AuditError("record value outside configured width")
    body = MAGIC + WORD.pack((FORMAT_VERSION << 24) | profile.record_bits)
    body += word.to_bytes(payload_bytes(profile),"little")
    return body + WORD.pack(crc32c(body))


def decode_frames(stream: BinaryIO,profile: Profile) -> Iterator[tuple[bytes,Record]]:
    declared = (FORMAT_VERSION << 24) | profile.record_bits
    frame_bytes = payload_bytes(profile)+4
    number = 0
    while True:
        header = stream.read(8)
        if not header:
            return
        number += 1
        if len(header) != 8:
            raise AuditError(f"frame {number}: torn header")
        if header[:4] != MAGIC or WORD.unpack_from(header,4)[0] != declared:
            raise AuditError(f"frame {number}: wrong magic, version, or payload width")
        data = stream.read(frame_bytes)
        if len(data) != frame_bytes:
            raise AuditError(f"frame {number}: truncated payload or checksum")
        if WORD.unpack(data[-4:])[0] != crc32c(header + data[:-4]):
            raise AuditError(f"frame {number}: bad frame checksum")
        try:
            yield header+data, Record.decode(int.from_bytes(data[:-4],"little"),profile)
        except AuditError as error:
            raise AuditError(f"frame {number}: {error}") from error


@dataclass(frozen=True)
class Seal:
    layout: Profile
    run: int
    records: int
    final_cycle: int
    sha256: str

    def __post_init__(self) -> None:
        if any(type(value) is not int for value in (self.run,self.records,self.final_cycle)):
            raise AuditError("seal integer fields are not canonical")
        if not 0 < self.run <= MAX64 or not 0 < self.records <= MAX64 or not 0 <= self.final_cycle <= MAX64:
            raise AuditError("seal counters are outside DCA1 range")
        if (not isinstance(self.sha256,str) or len(self.sha256) != 64 or
                set(self.sha256)-set("0123456789abcdef")):
            raise AuditError("seal needs a canonical SHA-256 digest")

    @classmethod
    def read(cls,path: Path) -> Seal:
        value = json.loads(path.read_text(encoding="utf-8"))
        expected = {"layout","run","records","final_cycle","sha256"}
        if not isinstance(value,dict) or set(value) != expected:
            raise AuditError("unrecognized seal fields")
        layout_fields = {"namespaces","sets","wait","history","reset_tick"}
        if not isinstance(value["layout"],dict) or set(value["layout"]) != layout_fields:
            raise AuditError("invalid seal geometry")
        return cls(Profile(**value["layout"]),value["run"],value["records"],
                   value["final_cycle"],value["sha256"])

    def check(self,verified: Seal) -> None:
        if ((self.layout,self.run,self.records,self.final_cycle) !=
                (verified.layout,verified.run,verified.records,verified.final_cycle)
                or not hmac.compare_digest(self.sha256,verified.sha256)):
            raise AuditError("tape differs from the independent terminal seal")


def verify(path: Path,profile: Profile,*,expected: Seal | None = None,max_cycles: int = 1_000_000_000,
           namespace: int | None = None,execution: int | None = None) -> tuple[dict,Seal]:
    if expected and expected.layout != profile:
        raise AuditError("requested layout differs from the independent seal")
    replay = Replay(profile,max_cycles,namespace,execution)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for binary,record in decode_frames(stream,profile):
            replay.advance(record)
            digest.update(binary)
    summary = replay.summary()
    seal = Seal(profile,summary["run"],summary["records"],summary["through_cycle"],digest.hexdigest())
    if expected:
        expected.check(seal)
    return summary,seal


def publish_seal(path: Path,seal: Seal) -> None:
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent,prefix=path.name+".",delete=False,
                                mode="w",encoding="utf-8") as handle:
            temporary = Path(handle.name)
            json.dump(asdict(seal),handle,sort_keys=True,indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,path)
        temporary = None
        descriptor = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def profiles(command: argparse.ArgumentParser) -> None:
    command.add_argument("--namespaces",type=int,default=3)
    command.add_argument("--sets",type=int,default=4)
    command.add_argument("--wait",type=int,default=24)
    command.add_argument("--history",type=int,default=40)
    command.add_argument("--reset-tick",type=lambda text:int(text,0),default=0)
    command.add_argument("--max-cycles",type=int,default=1_000_000_000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command",required=True)
    seal_cmd = commands.add_parser("seal",help="verify a complete capture and publish its terminal seal")
    seal_cmd.add_argument("tape",type=Path)
    seal_cmd.add_argument("seal",type=Path)
    profiles(seal_cmd)
    check = commands.add_parser("check",help="verify frames, report provenance, and the terminal marker")
    check.add_argument("tape",type=Path)
    check.add_argument("--seal",type=Path)
    check.add_argument("--namespace",type=int)
    check.add_argument("--execution",type=lambda text:int(text,0))
    profiles(check)
    args = parser.parse_args()
    try:
        seal = Seal.read(args.seal) if args.command == "check" and args.seal else None
        geometry = Profile(args.namespaces,args.sets,args.wait,args.history,args.reset_tick)
        if seal is not None:
            geometry = seal.layout
        summary,computed = verify(args.tape,geometry,expected=seal,max_cycles=args.max_cycles,
                                  namespace=getattr(args,"namespace",None),
                                  execution=getattr(args,"execution",None))
        if args.command == "seal":
            if (args.tape.resolve() == args.seal.resolve() or
                    (args.seal.exists() and args.tape.samefile(args.seal))):
                raise AuditError("seal destination aliases the capture")
            publish_seal(args.seal,computed)
        print(json.dumps(summary,indent=2))
        return 0
    except (OSError,ValueError,TypeError) as error:
        print(f"DCA1: {error}",file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
