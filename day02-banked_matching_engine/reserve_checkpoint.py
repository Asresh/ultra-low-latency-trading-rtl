"""Extract a checked reserve-book image and build an RTL restore tape."""

import argparse
from dataclasses import dataclass, fields
import json
from pathlib import Path
import struct
from zlib import crc32

from reserve_model import (
    Command, Geometry, ImportPart, Kind, Operation, PROFILES, check_unsigned, pack, unpack,
)
from reserve_verify import parse_trace


MAGIC = b"RSV1"
VERSION = 1
HEADER = struct.Struct("<4sB" + "H" * (len(fields(Geometry)) + 1) + "I")
TRAILER = struct.Struct("<I")


class BadCheckpoint(ValueError):
    """A malformed image could not have been captured from a valid book."""


@dataclass(frozen=True)
class ImageOrder:
    order_id: int
    side: int
    price: int
    total: int
    visible: int
    peak: int
    owner: int

    @classmethod
    def from_result(cls, result):
        """Extract the state-bearing fields from one nonterminal snapshot result."""
        return cls(result.maker, result.side, result.price, result.total,
                   result.visible, result.peak, result.owner)


def row_layout(geometry):
    """Canonical image rows list rank implicitly by file order."""
    return (
        ("order_id", geometry.id_width), ("side", 1), ("price", geometry.price_width),
        ("total", geometry.quantity_width), ("visible", geometry.quantity_width),
        ("peak", geometry.quantity_width), ("owner", geometry.owner_width),
    )


def row_size(geometry):
    """Round the bit-packed row up to a whole number of bytes."""
    return (sum(width for _, width in row_layout(geometry)) + 7) // 8


def validate_rows(geometry, rows):
    """Reject duplicate identifiers, impossible quantities, and crossed images."""
    if len(rows) > geometry.slots:
        raise BadCheckpoint("image exceeds configured live-order capacity")
    seen = set()
    bids, asks = [], []
    for rank, row in enumerate(rows):
        pack(row, row_layout(geometry))
        if row.order_id >= geometry.slots:
            raise BadCheckpoint(f"rank {rank}: invalid order id")
        if row.order_id in seen:
            raise BadCheckpoint(f"rank {rank}: repeated order id")
        seen.add(row.order_id)
        if row.price >= geometry.prices:
            raise BadCheckpoint(f"rank {rank}: price outside configured grid")
        if not 0 < row.visible <= row.total:
            raise BadCheckpoint(f"rank {rank}: impossible display balance")
        if (not row.peak and row.visible != row.total) or (row.peak and row.visible > row.peak):
            raise BadCheckpoint(f"rank {rank}: displayed quantity disagrees with peak")
        (asks if row.side else bids).append(row.price)
    if bids and asks and max(bids) >= min(asks):
        raise BadCheckpoint("image contains crossed orders")


@dataclass(frozen=True)
class Checkpoint:
    geometry: Geometry
    bank: int
    orders: tuple

    def __post_init__(self):
        """Validate the cross-field constraints as soon as an image becomes visible."""
        if not 0 <= self.bank < self.geometry.banks:
            raise BadCheckpoint("checkpoint bank is invalid")
        validate_rows(self.geometry, self.orders)

    def to_bytes(self):
        """Serialize a canonical CRC-protected image for a local checkpoint file."""
        dimensions = [getattr(self.geometry, field.name) for field in fields(Geometry)]
        for i, value in enumerate(dimensions):
            check_unsigned(value, 16, fields(Geometry)[i].name)
        check_unsigned(self.bank, 16, "bank")
        size = row_size(self.geometry)
        layout = row_layout(self.geometry)
        header = HEADER.pack(MAGIC, VERSION, *dimensions, self.bank, len(self.orders))
        body = b"".join(pack(row, layout).to_bytes(size, "little") for row in self.orders)
        message = header + body
        return message + TRAILER.pack(crc32(message))

    @classmethod
    def from_bytes(cls, raw):
        """Verify complete length, version and checksum before decoding any row."""
        if len(raw) < HEADER.size + TRAILER.size:
            raise BadCheckpoint("truncated checkpoint")
        header = HEADER.unpack_from(raw)
        magic, version, *dims, bank, count = header
        if magic != MAGIC or version != VERSION:
            raise BadCheckpoint("unsupported checkpoint format or version")
        try:
            geometry = Geometry(*dims)
        except ValueError as exc:
            raise BadCheckpoint(str(exc)) from exc
        if count > geometry.slots:
            raise BadCheckpoint("row count exceeds slot capacity")
        size = row_size(geometry)
        expected_bytes = HEADER.size + count * size + TRAILER.size
        if expected_bytes != len(raw):
            raise BadCheckpoint(f"file needs {expected_bytes} bytes, got {len(raw)}")
        [expected_crc] = TRAILER.unpack_from(raw, len(raw) - TRAILER.size)
        if crc32(raw[:-TRAILER.size]) != expected_crc:
            raise BadCheckpoint("checkpoint CRC mismatch")
        layout = row_layout(geometry)
        orders = []
        for i in range(count):
            start = HEADER.size + size * i
            bits = int.from_bytes(raw[start:start+size], "little")
            try:
                orders.append(ImageOrder(**unpack(bits, layout)))
            except ValueError as exc:
                raise BadCheckpoint(f"rank {i}: {exc}") from exc
        return cls(geometry, bank, tuple(orders))

    def restore(self, first_tag=0, bank=None, capture_after=False):
        """Yield an import with explicit count, then optionally take a readback."""
        destination = self.bank if bank is None else bank
        if not 0 <= destination < self.geometry.banks:
            raise BadCheckpoint("target bank is invalid")
        check_unsigned(first_tag, self.geometry.tag_width, "first_tag")
        mask = (1 << self.geometry.tag_width) - 1

        def command(index, part, **changes):
            """Allocate a deterministic tag for each ordered import operation."""
            return Command(bank=destination, tag=(first_tag + index) & mask,
                           operation=Operation.IMPORT, tif=part, **changes)

        yield command(0, ImportPart.BEGIN, order_id=len(self.orders))
        for i, row in enumerate(self.orders, 1):
            yield command(i, ImportPart.ROW, order_id=row.order_id, side=row.side,
                          price=row.price, quantity=row.total, display=row.peak,
                          import_visible=row.visible, owner=row.owner)
        yield command(len(self.orders) + 1, ImportPart.COMMIT)
        if capture_after:
            yield Command(bank=destination, tag=(first_tag + len(self.orders) + 2) & mask,
                          operation=Operation.SNAPSHOT)


@dataclass(frozen=True)
class Captured:
    checkpoint: Checkpoint
    tag: int
    command_sequence: int
    last_event_sequence: int
    terminal_cycle: int


@dataclass
class _Pending:
    tag: int
    command_sequence: int
    event_sequence: int
    orders: list


def extract(samples, geometry):
    """Yield complete images from physically retired snapshot events."""
    pending = {}
    for sample in samples:
        if sample.reset:
            pending.clear()
            continue
        if not sample.output_valid or not sample.output_ready:
            continue
        output = sample.output
        bank, result = output.bank, output.result
        if result.kind != Kind.SNAPSHOT:
            if bank in pending:
                raise BadCheckpoint(f"cycle {sample.cycle}: snapshot event was interrupted")
            continue
        if bank not in pending:
            pending[bank] = _Pending(output.tag, output.command_sequence, output.event_sequence, [])
        image = pending[bank]
        if (output.tag, output.command_sequence) != (image.tag, image.command_sequence):
            raise BadCheckpoint(f"cycle {sample.cycle}: snapshot identity changed")
        if output.event_sequence != image.event_sequence:
            raise BadCheckpoint(f"cycle {sample.cycle}: lost or duplicated snapshot event")
        if result.level_count != len(image.orders):
            raise BadCheckpoint(f"cycle {sample.cycle}: skipped or duplicated row rank")
        if result.last:
            yield Captured(
                Checkpoint(geometry, bank, tuple(image.orders)), output.tag,
                output.command_sequence, output.event_sequence, sample.cycle,
            )
            del pending[bank]
        else:
            image.orders.append(ImageOrder.from_result(result))
            image.event_sequence = (image.event_sequence + 1) & geometry.sequence_mask
    if pending:
        raise BadCheckpoint("capture ended in the middle of a snapshot")


def write_tape(commands, geometry, path):
    """Write the same pin-driver tape format as the main RTL campaign."""
    with path.open("w", encoding="ascii") as stream:
        for command in commands:
            print(f"0 {command.bank:x} {command.tag:x} {command.payload(geometry):x}", file=stream)


def avoid_overwrite(source, target):
    """Reject writing the output over the input, including symlinks and hardlinks."""
    if source.resolve() == target.resolve() or (target.exists() and source.samefile(target)):
        raise BadCheckpoint("the input and output must be different files")


def _extract_command(args):
    """Save the last captured image matching the requested selectors."""
    avoid_overwrite(args.capture, args.image)
    geometry = PROFILES[args.profile]
    chosen = None
    for found in extract(parse_trace(args.capture, geometry), geometry):
        if found.checkpoint.bank == args.bank and (args.tag is None or found.tag == args.tag):
            chosen = found
    if chosen is None:
        raise BadCheckpoint("no complete snapshot matched the bank and tag")
    args.image.write_bytes(chosen.checkpoint.to_bytes())
    print(
        f"checkpoint bank={chosen.checkpoint.bank} tag={chosen.tag} "
        f"rows={len(chosen.checkpoint.orders)} cycle={chosen.terminal_cycle}"
    )


def _restore_command(args):
    """Emit a fresh restore tape without touching a currently running matcher."""
    avoid_overwrite(args.image, args.tape)
    checkpoint = Checkpoint.from_bytes(args.image.read_bytes())
    write_tape(checkpoint.restore(args.first_tag, args.bank, args.snapshot), checkpoint.geometry, args.tape)


def _inspect_command(args):
    """Print ordered rows and exact widths of a checksum-validated file."""
    from dataclasses import asdict

    checkpoint = Checkpoint.from_bytes(args.image.read_bytes())
    print(json.dumps(asdict(checkpoint), indent=2))


def main():
    """Dispatch local inspect, capture extraction, and offline tape generation."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    command = sub.add_parser("extract")
    command.add_argument("capture", type=Path)
    command.add_argument("image", type=Path)
    command.add_argument("--profile", choices=PROFILES, default="default")
    command.add_argument("--bank", type=int, required=True)
    command.add_argument("--tag", type=int)
    command.set_defaults(call=_extract_command)
    command = sub.add_parser("restore")
    command.add_argument("image", type=Path)
    command.add_argument("tape", type=Path)
    command.add_argument("--bank", type=int)
    command.add_argument("--first-tag", type=int, default=0)
    command.add_argument("--snapshot", action="store_true")
    command.set_defaults(call=_restore_command)
    command = sub.add_parser("inspect")
    command.add_argument("image", type=Path)
    command.set_defaults(call=_inspect_command)
    args = parser.parse_args()
    try:
        args.call(args)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"checkpoint: {exc}\n")


if __name__ == "__main__":
    main()
