"""Extract complete reset epochs from a gearbox trace without cutting packets in half."""

import argparse
from collections import Counter
from dataclasses import dataclass, replace
import io
import json
from pathlib import Path

from width_audit import AuditError, TraceAudit, Transfer
from width_capture import Geometry, check_destination, read_rows, write_capture

MAX_EPOCH_ROWS = 250000


class ExcerptError(ValueError):
    """A requested complete epoch does not exist in the input."""


@dataclass(frozen=True)
class Epoch:
    number: int
    rows: tuple
    closing_reset: int

    @property
    def first(self):
        """Return the absolute first observed edge of this segment."""
        return self.rows[0].cycle if self.rows else None

    @property
    def last(self):
        """Return the final observed edge inside this reset generation."""
        return self.rows[-1].cycle if self.rows else None

    def summarize(self, geometry):
        """Audit this epoch using the next reset to account for pending discards."""
        auditor = TraceAudit(geometry.input_width, geometry.output_width, geometry.capacity)
        transfers = Counter()
        for item in self.rows:
            auditor.apply(item)
            transfers[item.channel] += 1
        if self.closing_reset is not None:
            auditor.apply(Transfer("reset", self.closing_reset, None))
        return {
            "epoch": self.number,
            "first_cycle": self.first,
            "last_cycle": self.last,
            "next_reset_cycle": self.closing_reset,
            "observed_edges": dict(transfers),
            **auditor.finish(),
        }


def epochs(records):
    """Group a monotonic trace into a leading region and subsequent reset epochs."""
    items = []
    index = 0
    for item in records:
        if item.channel == "reset":
            if items:
                yield Epoch(index, tuple(items), item.cycle)
                index += 1
                items.clear()
        items.append(item)
        if len(items) > MAX_EPOCH_ROWS:
            raise ExcerptError("one reset epoch exceeded the extractable row budget")
    if items:
        yield Epoch(index, tuple(items), None)


def epoch_summaries(source):
    """Scan a whole portable trace and check that every reset segment is consistent."""
    with source.open("rb") as file:
        geometry = Geometry.read(file)
        summaries = [group.summarize(geometry) for group in epochs(read_rows(file, geometry))]
    return {"geometry": vars(geometry), "epochs": summaries}


def range_bounds(first, count):
    """Protect a requested segment from invalid starts, lengths, and booleans."""
    if type(first) is not int or first < 0:
        raise ExcerptError("first epoch must be a nonnegative integer")
    if type(count) is not int or not 1 <= count <= 256:
        raise ExcerptError("epoch count must be an integer in 1–256")
    return first, first + count


def shift_rows(records, origin):
    """Normalize absolute ticks while preserving every relative gap."""
    for row in records:
        if row.cycle < origin:
            raise ExcerptError("cannot place an event before the excerpt origin")
        yield replace(row, cycle=row.cycle - origin)


def selected_rows(groups):
    """Reproduce the chosen epochs and the final explicit discard boundary, when present."""
    for group in groups:
        yield from group.rows
    if groups[-1].closing_reset is not None:
        yield Transfer("reset", groups[-1].closing_reset, None)


def extract(source, target, first, count, rebase=True):
    """Verify the complete source before emitting a selected set of replayable epochs."""
    check_destination(source, target)
    begin, stop = range_bounds(first, count)
    chosen = []
    with source.open("rb") as file:
        geometry = Geometry.read(file)
        for group in epochs(read_rows(file, geometry)):
            group.summarize(geometry)
            if begin <= group.number < stop:
                chosen.append(group)
    if len(chosen) != count:
        raise ExcerptError("requested epoch range extends past the capture")
    origin = chosen[0].first if rebase else 0
    rows = shift_rows(selected_rows(chosen), origin)
    destination = io.BytesIO()
    records = write_capture(destination, geometry, rows)
    target.write_bytes(destination.getvalue())
    return {
        "source_first_epoch": first,
        "captured_epochs": count,
        "cycles_subtracted": origin,
        "rows": records,
        "closing_reset_included": chosen[-1].closing_reset is not None,
    }


def main():
    """List verified reset boundaries or export a reusable piece of a larger trace."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("epochs")
    listing.add_argument("source", type=Path)
    cut = sub.add_parser("extract")
    cut.add_argument("source", type=Path)
    cut.add_argument("target", type=Path)
    cut.add_argument("--first", type=int, required=True)
    cut.add_argument("--count", type=int, default=1)
    cut.add_argument("--absolute-ticks", action="store_true")
    args = parser.parse_args()
    try:
        result = (epoch_summaries(args.source) if args.action == "epochs"
                  else extract(args.source, args.target, args.first, args.count, not args.absolute_ticks))
    except (OSError, ValueError, AuditError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
