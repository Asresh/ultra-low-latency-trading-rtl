"""Host image validation and checkpoint lifecycle known-answer tests."""

from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zlib import crc32

from reserve_checkpoint import (
    BadCheckpoint, Checkpoint, HEADER, ImageOrder, TRAILER, avoid_overwrite, extract,
)
from reserve_model import (
    Command, Event, Geometry, ImportPart, Kind, Operation, Reject, ReserveMatcher, Result,
)
from reserve_verify import PinSample


GEOMETRY = Geometry(3, 9, 8, 8, 2, 2, sequence_width=2)
IMAGE = Checkpoint(GEOMETRY, 0, (
    ImageOrder(3, 1, 5, 8, 2, 3, 11),
    ImageOrder(6, 1, 5, 7, 7, 0, 9),
    ImageOrder(1, 0, 2, 9, 9, 0, 13),
))


def resigned(raw):
    """Recompute integrity so parsing reaches structural corruptions."""
    return raw[:-TRAILER.size] + TRAILER.pack(crc32(raw[:-TRAILER.size]))


def output_at(cycle, bank, sequence, rank, row=None, *, tag=0x92, ready=True, reset=False):
    """Build one observed snapshot transfer or held offer."""
    if row:
        result = Result(
            Kind.SNAPSHOT, last=False, side=row.side, maker=row.order_id, price=row.price,
            total=row.total, visible=row.visible, peak=row.peak, owner=row.owner, level_count=rank,
        )
    else:
        result = Result(Kind.SNAPSHOT, level_count=rank)
    event = Event(bank, tag, 1, sequence, 0, result)
    return PinSample(cycle, reset, False, False, True, ready, None, event, 1 << bank)


class ImageTests(unittest.TestCase):
    def test_canonical_file_round_trip(self):
        raw = IMAGE.to_bytes()
        self.assertEqual(raw[:4], b"RSV1")
        self.assertEqual(Checkpoint.from_bytes(raw), IMAGE)
        self.assertEqual(Checkpoint.from_bytes(raw).to_bytes(), raw)

    def test_checksum_and_declared_length(self):
        raw = IMAGE.to_bytes()
        edit = bytearray(raw)
        edit[HEADER.size] ^= 8
        with self.assertRaisesRegex(BadCheckpoint, "checksum|CRC"):
            Checkpoint.from_bytes(edit)
        with self.assertRaisesRegex(BadCheckpoint, "bytes"):
            Checkpoint.from_bytes(raw[:-1])
        with self.assertRaisesRegex(BadCheckpoint, "bytes"):
            Checkpoint.from_bytes(raw + b"discard")

    def test_header_geometry_must_encode_positive_values(self):
        raw = bytearray(IMAGE.to_bytes())
        raw[5:7] = bytes(2)
        with self.assertRaisesRegex(BadCheckpoint, "banks must be positive"):
            Checkpoint.from_bytes(resigned(raw))

    def test_out_of_range_id_and_impossible_peak(self):
        row = IMAGE.orders[0]
        with self.assertRaisesRegex(BadCheckpoint, "invalid order id"):
            Checkpoint(GEOMETRY, 0, (replace(row, order_id=GEOMETRY.slots),))
        with self.assertRaisesRegex(BadCheckpoint, "displayed quantity disagrees"):
            Checkpoint(GEOMETRY, 0, (replace(row, visible=5),))

    def test_duplicate_id_and_crossed_book(self):
        a, b, c = IMAGE.orders
        with self.assertRaisesRegex(BadCheckpoint, "repeated"):
            Checkpoint(GEOMETRY, 0, (a, replace(b, order_id=a.order_id)))
        with self.assertRaisesRegex(BadCheckpoint, "crossed"):
            Checkpoint(GEOMETRY, 0, (a, replace(c, price=a.price)))

    def test_unused_padding_above_bit_fields_is_rejected(self):
        raw = bytearray(IMAGE.to_bytes())
        # Six stored bytes carry just 41 used bits with this geometry.
        raw[HEADER.size + 5] |= 0x80
        with self.assertRaisesRegex(BadCheckpoint, "payload"):
            Checkpoint.from_bytes(resigned(raw))

    def test_avoid_overwriting_input_through_hardlink(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "checkpoint.bin"
            output = Path(directory) / "hardlink.bin"
            source.write_bytes(IMAGE.to_bytes())
            os.link(source, output)
            with self.assertRaisesRegex(BadCheckpoint, "different files"):
                avoid_overwrite(source, output)
            self.assertEqual(source.read_bytes(), IMAGE.to_bytes())


class CaptureTests(unittest.TestCase):
    def test_interleaving_wrap_and_backpressure(self):
        a, b, c = IMAGE.orders
        stream = [
            output_at(100, 0, 2, 0, a, ready=False),
            output_at(101, 0, 2, 0, a),
            output_at(102, 1, 0, 0, c),
            output_at(103, 0, 3, 1, b),
            output_at(104, 1, 1, 1),
            output_at(105, 0, 0, 2, c),
            output_at(106, 0, 1, 3),
        ]
        copy, image = list(extract(stream, GEOMETRY))
        self.assertEqual(copy.checkpoint, Checkpoint(GEOMETRY, 1, (c,)))
        self.assertEqual(image.checkpoint, IMAGE)
        self.assertEqual(image.last_event_sequence, 1)

    def test_reset_drops_incomplete_image_and_next_is_clean(self):
        a = IMAGE.orders[0]
        stream = [
            output_at(3, 0, 0, 0, a),
            output_at(4, 0, 1, 1, reset=True),
            output_at(8, 0, 0, 0),
        ]
        [latest] = list(extract(stream, GEOMETRY))
        self.assertFalse(latest.checkpoint.orders)

    def test_lost_event_and_rank_jump(self):
        a, b, _ = IMAGE.orders
        with self.assertRaisesRegex(BadCheckpoint, "duplicated snapshot event"):
            list(extract([output_at(1, 0, 0, 0, a), output_at(2, 0, 2, 1, b)], GEOMETRY))
        with self.assertRaisesRegex(BadCheckpoint, "row rank"):
            list(extract([output_at(1, 0, 0, 0, a), output_at(2, 0, 1, 2, b)], GEOMETRY))

    def test_mismatched_tag_and_unterminated_slice(self):
        a = IMAGE.orders[0]
        with self.assertRaisesRegex(BadCheckpoint, "identity changed"):
            list(extract([output_at(1, 0, 0, 0, a), output_at(2, 0, 1, 1, tag=0)], GEOMETRY))
        with self.assertRaisesRegex(BadCheckpoint, "middle of a snapshot"):
            list(extract([output_at(1, 0, 0, 0, a)], GEOMETRY))


class RestoreTests(unittest.TestCase):
    def test_snapshot_rows_and_next_match_after_remap(self):
        matcher = ReserveMatcher(GEOMETRY)
        out = []
        for command in IMAGE.restore(first_tag=120, bank=1, capture_after=True):
            out.extend(matcher.execute(command))
        ranked = [event.result.maker for event in out
                  if event.result.kind == Kind.SNAPSHOT and not event.result.last]
        self.assertEqual(ranked, [3, 6, 1])
        ranking = matcher.execute(Command(bank=1, order_id=2, price=5, quantity=6))
        self.assertEqual([(e.result.maker, e.result.quantity) for e in ranking], [(3, 2), (6, 4)])

    def test_import_isolation_commit_count_and_abort(self):
        model = ReserveMatcher(GEOMETRY)
        model.execute(Command(order_id=4, side=1, price=6, quantity=10, owner=1))
        begin, first, second, third, commit = tuple(IMAGE.restore())
        model.execute(begin)
        model.execute(first)
        [failure] = model.execute(commit)
        self.assertEqual(failure.result.reason, Reject.IMPORT)
        blocked = model.execute(Command(operation=Operation.CANCEL, order_id=4, owner=1))
        self.assertEqual(blocked[0].result.reason, Reject.IMPORT)
        observed = model.execute(Command(operation=Operation.SNAPSHOT))
        self.assertEqual(observed[0].result.maker, 4)
        model.execute(Command(operation=Operation.IMPORT, tif=ImportPart.ABORT))
        self.assertEqual(model.execute(commit)[0].result.reason, Reject.IMPORT)
        for request in (begin, first, second, third, commit):
            self.assertEqual(model.execute(request)[0].result.kind, Kind.IMPORT)
        observed = model.execute(Command(operation=Operation.SNAPSHOT))
        self.assertEqual([e.result.maker for e in observed[:-1]], [3, 6, 1])

    def test_empty_import_is_an_atomic_book_clear(self):
        matcher = ReserveMatcher(GEOMETRY)
        matcher.execute(Command(order_id=1, quantity=4))
        for command in Checkpoint(GEOMETRY, 0, ()).restore():
            self.assertEqual(matcher.execute(command)[0].result.kind, Kind.IMPORT)
        observed = matcher.execute(Command(operation=Operation.SNAPSHOT))
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].result.level_count, 0)

    def test_bad_row_leaves_the_shadow_slot_available(self):
        model = ReserveMatcher(GEOMETRY)
        begin, first, second, third, commit = tuple(IMAGE.restore())
        model.execute(begin)
        model.execute(first)
        invalid = replace(second, side=0)
        [result] = model.execute(invalid)
        self.assertEqual(result.result.reason, Reject.CROSSED)
        for request in (second, third, commit):
            [result] = model.execute(request)
            self.assertEqual(result.result.kind, Kind.IMPORT)


if __name__ == "__main__":
    unittest.main()
