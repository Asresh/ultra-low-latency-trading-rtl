"""Split a large replay program at complete packet boundaries for bounded FPGA memory."""

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from replay_format import decode_tape, encode_tape, read_tape
from replay_planner import rebase_tape
from replay_program import ReplayError, Tape, U32, unsigned


@dataclass(frozen=True)
class Partition:
    """One independently loadable tape and the offset restoring its original time."""

    index: int
    origin: int
    tape: Tape

    @property
    def basename(self):
        """Choose a canonical filename independent of input payload or tags."""
        return f"part-{self.index:04d}.trp"

    def absolute(self):
        """Return offsets in the original schedule after restoring the time origin."""
        return Tape(tuple(beat.at(beat.tick+self.origin) for beat in self.tape.events))

    def record(self, digest):
        """Expose enough bounded geometry to verify a transferred bundle."""
        return {
            "file": self.basename,
            "origin": self.origin,
            "beats": len(self.tape.events),
            "packets": sum(beat.last for beat in self.tape.events),
            "payload_bytes": self.tape.payload_bytes,
            "last_tick": self.tape.duration,
            "sha256": digest,
        }


def partition_tape(tape, depth, *, shift=True):
    """Greedily fill each bank while ensuring a packet stays wholly within one image."""
    if unsigned(depth, U32, "bundle depth") == 0:
        raise ReplayError("bundle depth must be positive")
    if type(shift) is not bool:
        raise ReplayError("partition rebase flag must be Boolean")
    batches = []
    buffer = []
    for position, packet in enumerate(tape.packets()):
        count = len(packet.ticks)
        if count > depth:
            raise ReplayError(f"packet {position} needs {count} records, exceeding depth {depth}")
        if len(buffer) + count > depth:
            batches.append(Tape(tuple(buffer)))
            buffer.clear()
        buffer.extend(packet.beats())
    if buffer:
        batches.append(Tape(tuple(buffer)))
    return tuple(
        Partition(index, part.events[0].tick, rebase_tape(part))
        if shift else Partition(index, 0, part)
        for index, part in enumerate(batches)
    )


def join_partitions(parts):
    """Restore absolute offsets and reject missing, reordered, or overlapping records."""
    events = []
    for index, part in enumerate(parts):
        if part.index != index:
            raise ReplayError(f"expected part index {index}, got {part.index}")
        if not part.tape.events:
            raise ReplayError("a partition cannot contain an empty image")
        events.extend(part.absolute().events)
    return Tape(tuple(events))


def write_bundle(destination, tape, depth, *, shift=True):
    """Create a fresh directory containing atomic validated tape images and a manifest."""
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ReplayError("bundle destination must be a new directory")
    parts = partition_tape(tape, depth, shift=shift)
    payload = {
        "version": 1,
        "depth": depth,
        "rebase": shift,
        "records": [],
        "total_beats": len(tape.events),
        "total_bytes": tape.payload_bytes,
    }
    with TemporaryDirectory(prefix=destination.name+".", dir=destination.parent) as temp:
        staging = Path(temp)/"bundle"
        staging.mkdir()
        for part in parts:
            raw = encode_tape(part.tape)
            (staging/part.basename).write_bytes(raw)
            payload["records"].append(part.record(sha256(raw).hexdigest()))
        (staging/"manifest.json").write_text(
            json.dumps(payload, indent=2)+"\n", encoding="utf-8",
        )
        staging.rename(destination)
    return payload


def read_bundle(directory):
    """Verify canonical paths, each digest and geometry, then restore the full schedule."""
    directory = Path(directory)
    manifest = json.loads((directory/"manifest.json").read_text(encoding="utf-8"))
    schema = {"version", "depth", "rebase", "records", "total_beats", "total_bytes"}
    if type(manifest) is not dict or set(manifest) != schema or manifest["version"] != 1:
        raise ReplayError("unsupported replay partition manifest")
    if type(manifest["rebase"]) is not bool or type(manifest["records"]) is not list:
        raise ReplayError("invalid partition geometry in manifest")
    limit = unsigned(manifest["depth"], U32, "bundle depth")
    if limit == 0:
        raise ReplayError("bundle depth must be positive")
    parts = []
    for index, record in enumerate(manifest["records"]):
        name = f"part-{index:04d}.trp"
        if type(record) is not dict or record.get("file") != name:
            raise ReplayError(f"manifest part {index} must use canonical name {name}")
        raw = (directory/name).read_bytes()
        checksum = sha256(raw).hexdigest()
        if checksum != record.get("sha256"):
            raise ReplayError(f"partition {index} SHA-256 mismatch")
        origin = unsigned(record.get("origin"), U32, "partition origin")
        part = Partition(index, origin, decode_tape(raw, limit))
        if part.record(checksum) != record:
            raise ReplayError(f"partition {index} metadata does not match its tape")
        if manifest["rebase"] and (not part.tape.events or part.tape.events[0].tick != 0):
            raise ReplayError(f"rebased partition {index} does not begin at tick zero")
        if not manifest["rebase"] and origin:
            raise ReplayError(f"unshifted partition {index} must have origin zero")
        parts.append(part)
    combined = join_partitions(parts)
    if len(combined.events) != manifest["total_beats"] or combined.payload_bytes != manifest["total_bytes"]:
        raise ReplayError("partition totals differ from the manifest")
    return combined


def main():
    """Create a bank-sized bundle or check the integrity of a transferred bundle."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    split = commands.add_parser("split", help="partition a large .trp or .csv tape")
    split.add_argument("input", type=Path)
    split.add_argument("destination", type=Path)
    split.add_argument("--depth", type=int, required=True)
    split.add_argument("--preserve-absolute-ticks", action="store_true")
    verify = commands.add_parser("verify", help="verify and summarize every partition")
    verify.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "split":
            result = write_bundle(args.destination, read_tape(args.input), args.depth,
                                  shift=not args.preserve_absolute_ticks)
        else:
            combined = read_bundle(args.directory)
            result = {
                "beats": len(combined.events),
                "bytes": combined.payload_bytes,
                "last_tick": combined.duration,
            }
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
