"""Canonical order snapshot images and checked restore command preparation."""

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from struct import Struct
from tempfile import NamedTemporaryFile
from typing import Tuple
from zlib import crc32


MAGIC = b"MSN1"
VERSION = 1
HEADER = Struct("<4sHHIQ10s2sII")
CRC = Struct("<I")
ORDER_KEY = Struct("<QHBBI")
ORDER_VALUE = Struct("<IIQ")
MAX_ORDERS = 1_000_000


class ImageError(ValueError):
    """Malformed, unsafe, or incompatible snapshot data."""


def value(number, width, name, positive=False, *, parse_text=True):
    if parse_text and type(number) is str:
        try:
            number = int(number, 0)
        except ValueError as error:
            raise ImageError(f"{name} must be an integer") from error
    if type(number) is not int or not int(positive) <= number < (1 << width):
        raise ImageError(f"{name} exceeds {width} unsigned bits or violates its lower bound")
    return number


def session_bytes(value):
    if isinstance(value, str):
        try:
            value = value.encode("ascii")
        except UnicodeError as error:
            raise ImageError("session text must be ASCII") from error
    if not isinstance(value, bytes) or len(value) != 10:
        raise ImageError("session token must be exactly ten bytes")
    return value


@dataclass(frozen=True, order=True)
class Order:
    reference: int
    locate: int
    side: str
    price: int
    shares: int

    def __post_init__(self):
        value(self.reference, 64, "order reference", True, parse_text=False)
        value(self.locate, 16, "stock locate", parse_text=False)
        if self.side not in ("B", "S"):
            raise ImageError("side must be B or S")
        value(self.price, 32, "price", True, parse_text=False)
        value(self.shares, 32, "shares", True, parse_text=False)

    @classmethod
    def parse(cls, row):
        fields = {"reference", "locate", "side", "price", "shares"}
        if not isinstance(row, dict) or set(row) != fields:
            raise ImageError("an order requires reference, locate, side, price and shares")
        return cls(
            value(row["reference"], 64, "order reference", True),
            value(row["locate"], 16, "stock locate"),
            row["side"],
            value(row["price"], 32, "price", True),
            value(row["shares"], 32, "shares", True),
        )

    def rows(self):
        return (ORDER_KEY.pack(self.reference, self.locate, ord(self.side), 0, 0),
                ORDER_VALUE.pack(self.price, self.shares, 0))


@dataclass(frozen=True)
class Snapshot:
    slot: int
    expected_epoch: int
    session: bytes
    sequence: int
    orders: Tuple[Order, ...]

    def __post_init__(self):
        value(self.slot, 16, "slot", parse_text=False)
        value(self.expected_epoch, 32, "epoch", parse_text=False)
        value(self.sequence, 64, "next sequence", parse_text=False)
        if not isinstance(self.session, bytes):
            raise ImageError("snapshot session must be bytes")
        session_bytes(self.session)
        if not isinstance(self.orders, tuple) or not all(isinstance(item, Order) for item in self.orders):
            raise ImageError("snapshot requires a tuple of checked orders")
        if len(self.orders) > MAX_ORDERS:
            raise ImageError("snapshot exceeds the host order capacity")
        keys = tuple(record.reference for record in self.orders)
        if any(right <= left for left, right in zip(keys, keys[1:])):
            raise ImageError("order references must be distinct and ascending")

    @property
    def row_count(self):
        return 2 * len(self.orders)

    def rows(self):
        for order in self.orders:
            yield from order.rows()

    def payload(self):
        return b"".join(self.rows())

    @property
    def crc(self):
        return crc32(self.payload())

    def check_hardware(self, slots, epoch_width, capacity):
        if slots < 1 or not 1 <= epoch_width <= 32 or capacity < 1:
            raise ImageError("invalid hardware geometry")
        if self.slot >= slots:
            raise ImageError("snapshot slot lies outside hardware geometry")
        if self.expected_epoch >= (1 << epoch_width) - 1:
            raise ImageError("receiver epoch is exhausted or exceeds the hardware width")
        if self.row_count > capacity:
            raise ImageError("snapshot rows exceed the hardware capture memory")
        return self

    def encode(self):
        body = self.payload()
        header = HEADER.pack(MAGIC, VERSION, self.slot, self.expected_epoch, self.sequence,
                             self.session, b"\0\0", len(self.orders), crc32(body))
        return header + CRC.pack(crc32(header)) + body

    @classmethod
    def parse(cls, document):
        keys = {"slot", "expected_epoch", "session", "sequence", "orders"}
        if not isinstance(document, dict) or set(document) != keys:
            raise ImageError(f"snapshot descriptor requires {', '.join(sorted(keys))}")
        raw = document["orders"]
        if not isinstance(raw, list) or len(raw) > MAX_ORDERS:
            raise ImageError("orders must be a bounded array")
        return cls(
            value(document["slot"], 16, "slot"),
            value(document["expected_epoch"], 32, "epoch"),
            session_bytes(document["session"]),
            value(document["sequence"], 64, "next sequence"),
            tuple(sorted(Order.parse(order) for order in raw)),
        )

    @classmethod
    def decode(cls, data):
        prefix = HEADER.size + CRC.size
        if len(data) < prefix:
            raise ImageError("truncated snapshot header")
        raw = data[:HEADER.size]
        provided, = CRC.unpack_from(data, HEADER.size)
        if crc32(raw) != provided:
            raise ImageError("snapshot header checksum mismatch")
        magic, version, slot, epoch, sequence, session, reserved, quantity, body_crc = HEADER.unpack(raw)
        if (magic, version, reserved) != (MAGIC, VERSION, b"\0\0"):
            raise ImageError("unsupported snapshot header or nonzero reserved field")
        body = memoryview(data)[prefix:]
        if quantity > MAX_ORDERS or len(body) != 32 * quantity:
            raise ImageError("snapshot length differs from declared order count")
        if crc32(body) != body_crc:
            raise ImageError("snapshot row checksum mismatch")
        return cls(slot, epoch, session, sequence, decode_orders(body))

    def manifest(self):
        return {
            "slot": self.slot,
            "expected_epoch": self.expected_epoch,
            "session_hex": self.session.hex(),
            "next_sequence": self.sequence,
            "row_count": self.row_count,
            "row_crc": f"{self.crc:08x}",
            "orders": [asdict(order) for order in self.orders],
        }


def decode_orders(body):
    """Decode exact row pairs independent of the file's contextual header."""
    if len(body) % 32:
        raise ImageError("a snapshot order requires exactly two 16-byte rows")
    orders = []
    for offset in range(0, len(body), 32):
        ref, locate, side, flags, reserved_key = ORDER_KEY.unpack_from(body, offset)
        price, shares, reserved_value = ORDER_VALUE.unpack_from(body, offset+16)
        if flags or reserved_key or reserved_value:
            raise ImageError("order record contains a reserved field")
        orders.append(Order(ref, locate, chr(side), price, shares))
    return tuple(orders)


def unique_fields(pairs):
    result = {}
    for key, item in pairs:
        if key in result:
            raise ImageError(f"duplicate JSON field {key}")
        result[key] = item
    return result


def read_descriptor(path):
    return Snapshot.parse(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields))


def save_image(path, image):
    temp = None
    try:
        with NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f"{path.name}.", delete=False) as output:
            temp = Path(output.name)
            output.write(image.encode())
        temp.replace(path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def control_fixture(snapshot, transaction, spacing, force):
    value(transaction, 32, "transaction identity", True)
    value(spacing, 16, "request spacing")
    if type(force) is not bool:
        raise ImageError("force must be a Boolean")
    fields = (
        transaction, snapshot.slot, snapshot.expected_epoch, int.from_bytes(snapshot.session, "big"),
        snapshot.sequence, snapshot.row_count, snapshot.crc, int(force), spacing,
    )
    yield " ".join(format(number, "x") for number in fields)
    for index, row in enumerate(snapshot.rows(), 1):
        yield f"{int.from_bytes(row, 'little'):032x} {int(index == snapshot.row_count):x}"


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    subparsers = cli.add_subparsers(dest="operation", required=True)
    compile_cmd = subparsers.add_parser("compile", help="build a canonical checksummed binary snapshot")
    compile_cmd.add_argument("descriptor", type=Path)
    compile_cmd.add_argument("output", type=Path)
    read_cmd = subparsers.add_parser("inspect", help="validate and decode a snapshot")
    read_cmd.add_argument("snapshot", type=Path)
    control = subparsers.add_parser("control", help="emit validated begin and row transfers")
    control.add_argument("snapshot", type=Path)
    control.add_argument("--transaction", type=int, required=True)
    control.add_argument("--spacing", type=int, default=0)
    control.add_argument("--force", action="store_true")
    control.add_argument("--slots", type=int, default=3)
    control.add_argument("--epoch-width", type=int, default=16)
    control.add_argument("--capacity", type=int, default=16)
    args = cli.parse_args()
    try:
        if args.operation == "compile":
            save_image(args.output, read_descriptor(args.descriptor))
        else:
            snapshot = Snapshot.decode(args.snapshot.read_bytes())
            if args.operation == "inspect":
                print(json.dumps(snapshot.manifest(), indent=2))
            else:
                snapshot.check_hardware(args.slots, args.epoch_width, args.capacity)
                for entry in control_fixture(snapshot, args.transaction, args.spacing, args.force):
                    print(entry)
    except (OSError, ValueError) as error:
        cli.exit(1, f"{cli.prog}: {error}\n")


if __name__ == "__main__":
    main()
