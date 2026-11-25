"""Build and audit a real receiver-to-book snapshot cutover fixture."""

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import random

from mold_snapshot_image import ImageError, Order, Snapshot, control_fixture, decode_orders, save_image
from wire_codec import ADD, COMMON, EXECUTE, ItchRecord, MASK64, QUANTITY, REPLACE, decode_itch, make_packet, words


TX = 31
OLD = b"OLDBOOK001"
PEER = b"PEERBOOK01"
NEW = b"NEWBOOK001"
SEED = MASK64 - 1


def message(kind, ref, locate=15, tail=b""):
    return kind.encode("ascii") + COMMON.pack(locate, 2, b"\0\0\0\0\0\7", ref) + tail


def fixtures():
    snapshot = Snapshot(0, 1, NEW, SEED, (
        Order(7001, 15, "B", 180_000, 90),
        Order(7002, 15, "S", 181_000, 45),
    ))
    recovery = (
        message("D", 7001),
        message("X", 7002, tail=QUANTITY.pack(5)),
        message("A", 9001, tail=ADD.pack(ord("B"), 150, b"DEMO    ", 180_500)),
        message("U", 7002, tail=REPLACE.pack(9010, 24, 181_050)),
    )
    post = message("E", 9010, tail=EXECUTE.pack(2, 1234))
    groups = (
        (OLD, 90, (message("D", 44),)),
        (PEER, 11, (message("D", 51), message("D", 52))),
        (NEW, SEED, recovery),
        (NEW, 2, (post,)),
    )
    return snapshot, groups


def build(directory):
    directory.mkdir(parents=True, exist_ok=True)
    snapshot, groups = fixtures()
    image_path = directory / "order-book.msn"
    save_image(image_path, snapshot)
    hardware = Snapshot.decode(image_path.read_bytes()).check_hardware(3, 3, 8)
    with (directory / "snapshot-commands.txt").open("w", encoding="ascii") as output:
        for line in control_fixture(hardware, TX, 7, False):
            output.write(line + "\n")

    generator = random.Random(81027)
    with (directory / "snapshot-wire.txt").open("w", encoding="ascii") as output:
        for session, first, bodies in groups:
            records = [
                beat for i, body in enumerate(bodies)
                for beat in words(make_packet(session, first+i, (body,)), generator)
            ]
            output.write(f"{len(records)}\n")
            for raw, keep, sop, eop in records:
                output.write(f"{raw:016x} {keep:02x} {int(sop)*2+int(eop):x}\n")


class Book:
    """Restore an order-level image and apply checked normalized ITCH deltas."""

    def __init__(self, snapshot):
        self.orders = {order.reference: order for order in snapshot.orders}
        self.sequence = snapshot.sequence

    def apply(self, record):
        if record.sequence != self.sequence or record.result != 0:
            raise ImageError("event has the wrong sequence or unsupported decoded status")
        opcode = chr(record.kind)
        if opcode in ("A", "F"):
            if record.reference in self.orders:
                raise ImageError("order addition reuses an active reference")
            self.orders[record.reference] = Order(
                record.reference, record.locate, chr(record.side), record.price, record.shares,
            )
        else:
            order = self.orders.get(record.reference)
            if order is None or order.locate != record.locate:
                raise ImageError("delta refers to an absent order or different instrument")
            if opcode in ("X", "E", "C"):
                if not 0 < record.shares <= order.shares:
                    raise ImageError("delta exceeds displayed shares")
                if record.shares == order.shares:
                    del self.orders[order.reference]
                else:
                    self.orders[order.reference] = replace(order, shares=order.shares-record.shares)
            elif opcode == "D":
                del self.orders[order.reference]
            elif opcode == "U":
                if record.replacement in self.orders:
                    raise ImageError("replacement collides with an active reference")
                updated = replace(order, reference=record.replacement,
                                  price=record.price, shares=record.shares)
                del self.orders[order.reference]
                self.orders[updated.reference] = updated
            else:
                raise ImageError(f"unsupported delta {opcode}")
        self.sequence = (self.sequence+1) & MASK64


def decode_normalized(word):
    """Read actual hardware event fields for the reference book consumer."""
    if not 0 <= word < 1 << 588:
        raise ImageError("normalized event lies outside its bus width")
    sizes = (2, 2, 64, 64, 8, 16, 16, 48, 64, 64, 8, 32, 64, 32, 64, 8, 32)
    fields = []
    for width in reversed(sizes):
        fields.append(word & ((1 << width)-1))
        word >>= width
    fields.reverse()
    result, disposition, sequence, expected, *decoded = fields
    if disposition != 0 or sequence != expected:
        raise ImageError("receiver output carries a discontinuous sequence disposition")
    return ItchRecord(result, sequence, *decoded)


def read_trace(path):
    for line_no, line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        items = line.split()
        try:
            if len(items) != 2:
                raise ValueError("expected tick and event bundle")
            yield int(items[0]), int(items[1], 16)
        except ValueError as error:
            raise ImageError(f"trace line {line_no}: {error}") from error


def check(directory):
    snapshot = Snapshot.decode((directory / "order-book.msn").read_bytes())
    _, groups = fixtures()
    restored = [tuple(int(item, 16) for item in line.split())
                for line in (directory / "snapshot-restore.txt").read_text(encoding="ascii").splitlines()]
    response = tuple(map(int, (directory / "snapshot-response.txt").read_text(encoding="ascii").split()))
    if response != (TX, 0, 0, 2, 1, 1, 4, snapshot.crc):
        raise ImageError(f"unexpected cutover result {response}")
    if len(restored) != snapshot.row_count+2:
        raise ImageError("restore stream lost its header, rows, or commit marker")

    previous = -1
    for offset, (cycle, kind, identity, slot, epoch, first, count, index, checksum, word) in enumerate(restored):
        expected_kind = 0 if offset == 0 else 2 if offset == len(restored)-1 else 1
        expected_word = int.from_bytes(tuple(snapshot.rows())[offset-1], "little") if expected_kind == 1 else 0
        if (
            cycle <= previous or kind != expected_kind or identity != TX or slot != snapshot.slot or
            epoch != 2 or first != snapshot.sequence or count != snapshot.row_count or checksum != snapshot.crc or
            index != (offset-1 if expected_kind == 1 else 0) or word != expected_word
        ):
            raise ImageError("restored row sequence or payload differed from the verified image")
        previous = cycle
    committed_cycle = previous

    def expected(group):
        _, first, bodies = group
        return [decode_itch((first+i) & MASK64, body) for i, body in enumerate(bodies)]

    previous = -1
    traces = {(0, 1): [], (1, 1): [], (0, 2): []}
    for cycle, bundle in read_trace(directory / "snapshot-live.txt"):
        slot = bundle >> 591
        epoch = (bundle >> 588) & 7
        if cycle <= previous or (slot, epoch) not in traces:
            raise ImageError("live output clock or context differs")
        traces[slot, epoch].append((cycle, bundle & ((1 << 588)-1)))
        previous = cycle
    expected_new = expected(groups[2]) + expected(groups[3])
    for identity, events in (((0, 1), expected(groups[0])), ((1, 1), expected(groups[1])),
                             ((0, 2), expected_new)):
        if [actual for _, actual in traces[identity]] != [event.bits() for event in events]:
            raise ImageError(f"decoded replay differs for slot and epoch {identity}")
    if traces[0, 1][-1][0] >= committed_cycle or traces[0, 2][0][0] <= committed_cycle:
        raise ImageError("old and new events crossed the book commit")
    if not any(cycle < committed_cycle for cycle, _ in traces[1, 1]):
        raise ImageError("fixture did not exercise the peer slot while target was fenced")

    saved_rows = b"".join(record[-1].to_bytes(16, "little") for record in restored[1:-1])
    book = Book(replace(snapshot, orders=decode_orders(saved_rows)))
    for _, actual in traces[0, 2]:
        book.apply(decode_normalized(actual))
    if tuple(sorted(book.orders)) != (9001, 9010) or book.orders[9010].shares != 22:
        raise ImageError("restored order book disagrees after live deltas")
    result = {
        "commit_cycle": committed_cycle,
        "next_sequence": book.sequence,
        "orders": [asdict(order) for order in sorted(book.orders.values())],
    }
    (directory / "restored-book.json").write_text(json.dumps(result, indent=2)+"\n", encoding="ascii")
    return len(expected_new), len(traces[1, 1]), len(book.orders)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("build", "check"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "build":
            build(args.directory)
        else:
            updates, peer, rows = check(args.directory)
            print(f"PASS real snapshot audit: {updates} decoded deltas, {peer} peer events, {rows} final live orders")
    except (OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
