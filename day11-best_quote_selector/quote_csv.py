"""Normalize offline named-venue CSV quotes into timestamped selector events."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType

from quote_replay import Event
from quote_schema import Command, Query


COLUMNS = (
    "time_ns", "kind", "instrument", "venue", "sequence", "bid_price",
    "bid_qty", "ask_price", "ask_qty", "max_age", "tag",
)
NANOSECONDS = 1_000_000_000


class CsvImportError(ValueError):
    """A normalized input could not be converted to selector geometry."""


def _checked_catalog(data, limit, label):
    """Check symbolic names and physical row IDs and freeze the resulting map."""
    if not isinstance(data, dict) or not data:
        raise CsvImportError(f"catalog needs nonempty {label} mapping")
    numbers = set()
    for name, number in data.items():
        if not isinstance(name, str) or not name or name != name.strip():
            raise CsvImportError(f"invalid {label} name {name!r}")
        if type(number) is not int or not 0 <= number < limit:
            raise CsvImportError(f"{label} row {number!r} is outside profile")
        if number in numbers:
            raise CsvImportError(f"duplicate {label} row {number}")
        numbers.add(number)
    return MappingProxyType(dict(data))


@dataclass(frozen=True)
class Catalog:
    instruments: dict[str, int]
    venues: dict[str, int]

    @classmethod
    def from_object(cls, obj, profile):
        """Resolve a catalog object against the current hardware geometry."""
        if not isinstance(obj, dict) or set(obj) != {"instruments", "venues"}:
            raise CsvImportError("catalog needs instruments and venues")
        return cls(
            _checked_catalog(obj["instruments"], profile.instruments, "instrument"),
            _checked_catalog(obj["venues"], profile.venues, "venue"),
        )

    def instrument(self, name):
        """Resolve an instrument name with an actionable data-entry error."""
        try:
            return self.instruments[name]
        except KeyError as exc:
            raise CsvImportError(f"unknown instrument {name!r}") from exc

    def venue(self, name):
        """Resolve a venue name with an actionable data-entry error."""
        try:
            return self.venues[name]
        except KeyError as exc:
            raise CsvImportError(f"unknown venue {name!r}") from exc


def _object(pairs):
    """Reject duplicate keys in the catalog JSON, including nested name collisions."""
    obj = {}
    for name, number in pairs:
        if name in obj:
            raise CsvImportError(f"duplicate catalog key {name!r}")
        obj[name] = number
    return obj


def read_catalog(path, profile):
    """Decode the project's small JSON instrument and venue alias file."""
    try:
        decoded = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_object)
    except json.JSONDecodeError as exc:
        raise CsvImportError(f"bad catalog JSON at line {exc.lineno}") from exc
    return Catalog.from_object(decoded, profile)


def decimal(token, label, default=None):
    """Parse unsigned base-ten input, distinguishing an omitted field from zero."""
    cell = token.strip()
    if cell == "":
        if default is None:
            raise CsvImportError(f"missing {label}")
        return default
    if not cell.isascii() or not cell.isdecimal():
        raise CsvImportError(f"{label} requires a nonnegative integer")
    try:
        return int(cell)
    except ValueError as exc:
        raise CsvImportError(f"{label} is too wide") from exc


def _nonblank(row, names):
    """Return named columns populated by this record."""
    return [name for name in names if row[name].strip()]


def _unused(row, permitted):
    """Reject extra fields that would otherwise be silently discarded."""
    extras = _nonblank(row, (key for key in COLUMNS if key not in permitted))
    if extras:
        raise CsvImportError(f"unexpected fields for {row['kind']}: {', '.join(extras)}")


def _market(row, cycle, catalog, kind):
    """Build a labeled update, withdrawal, or eligibility control."""
    common = ("time_ns", "kind", "instrument", "venue", "tag")
    instrument = catalog.instrument(row["instrument"].strip())
    venue = catalog.venue(row["venue"].strip())
    tag = decimal(row["tag"], "tag", 0)
    if kind == "replace":
        fields = ("sequence", "bid_price", "bid_qty", "ask_price", "ask_qty")
        _unused(row, (*common, *fields))
        sequence = decimal(row["sequence"], "sequence")
        bid = decimal(row["bid_price"], "bid_price", 0), decimal(row["bid_qty"], "bid_qty", 0)
        ask = decimal(row["ask_price"], "ask_price", 0), decimal(row["ask_qty"], "ask_qty", 0)
        return Event(cycle, command=Command.replace(instrument, venue, sequence, bid, ask, tag))
    if kind == "withdraw":
        _unused(row, (*common, "sequence"))
        sequence = decimal(row["sequence"], "sequence")
        return Event(cycle, command=Command.withdraw(instrument, venue, sequence, tag))
    _unused(row, common)
    return Event(cycle, command=Command.venue_enabled(instrument, venue, kind == "enable", tag))


def decode_row(row, cycle, catalog, profile):
    """Convert one row by its operation-specific contract and validate the encoded fields."""
    kind = row["kind"].strip().lower()
    if kind == "reset":
        _unused(row, ("time_ns", "kind"))
        event = Event(cycle, reset=True)
    elif kind == "query":
        _unused(row, ("time_ns", "kind", "instrument", "max_age", "tag"))
        instrument = catalog.instrument(row["instrument"].strip())
        age = decimal(row["max_age"], "max_age")
        tag = decimal(row["tag"], "tag", 0)
        event = Event(cycle, query=Query(instrument, age, tag))
    elif kind in ("replace", "withdraw", "enable", "disable"):
        event = _market(row, cycle, catalog, kind)
    else:
        raise CsvImportError(f"unknown row kind {kind!r}")
    return event.check(profile)


def ceil_cycles(delta_ns, clock_hz):
    """Round a nonnegative elapsed time up to the next whole clock edge."""
    if type(delta_ns) is not int or delta_ns < 0:
        raise CsvImportError("elapsed time must be a nonnegative integer")
    if type(clock_hz) is not int or clock_hz <= 0:
        raise CsvImportError("clock_hz must be a positive integer")
    return (delta_ns * clock_hz + NANOSECONDS - 1) // NANOSECONDS


@dataclass(frozen=True)
class ImportSummary:
    origin_ns: int
    clock_hz: int
    rows: int
    first_cycle: int
    last_cycle: int
    injected_reset: bool
    collocated_data_rows: int


def read_csv(stream, catalog, profile, clock_hz=100_000_000):
    """Compile ordered named rows and pad a zero-edge reset when the first row is data."""
    ceil_cycles(0, clock_hz)
    reader = csv.DictReader(stream, strict=True)
    if set(reader.fieldnames or ()) != set(COLUMNS) or len(reader.fieldnames) != len(COLUMNS):
        raise CsvImportError("capture requires exactly the normalized CSV columns")
    events = []
    clock_origin = None
    previous_ns = None
    paired_data = 0
    reset_inserted = False
    for row in reader:
        try:
            if None in row or any(val is None for val in row.values()):
                raise CsvImportError("too many or too few CSV fields")
            instant = decimal(row["time_ns"], "time_ns")
            if clock_origin is None:
                clock_origin = instant
                reset_inserted = row["kind"].strip().lower() != "reset"
                if reset_inserted:
                    events.append(Event(0, reset=True))
            if previous_ns is not None and instant < previous_ns:
                raise CsvImportError("timestamps must be nondecreasing")
            previous_ns = instant
            cycle = ceil_cycles(instant - clock_origin, clock_hz) + int(reset_inserted)
            event = decode_row(row, cycle, catalog, profile)
            if events and events[-1].cycle == cycle:
                if events[-1].reset or event.reset:
                    raise CsvImportError(f"reset and another row map to the same edge {cycle}")
                paired_data += 1
            events.append(event)
        except ValueError as exc:
            raise CsvImportError(f"CSV line {reader.line_num}: {exc}") from exc
    if not events:
        raise CsvImportError("capture has no records")
    number = len(events) - int(reset_inserted)
    return tuple(events), ImportSummary(
        clock_origin, clock_hz, number, events[int(reset_inserted)].cycle,
        events[-1].cycle, reset_inserted, paired_data,
    )
