"""Wire types and strict text serialization for the best-quote selector."""


from __future__ import annotations
from dataclasses import dataclass, fields
from enum import IntEnum
from typing import Iterable


class Op(IntEnum):
    REPLACE = 0
    WITHDRAW = 1
    ENABLE = 2
    DISABLE = 3


class Code(IntEnum):
    OK = 0
    BAD_INST = 1
    BAD_VENUE = 2
    BAD_OP = 3
    DISABLED = 4
    BAD_SEQ = 5
    BAD_PRICE = 6
    BAD_AGE = 7


class State(IntEnum):
    EMPTY = 0
    BID_ONLY = 1
    ASK_ONLY = 2
    OPEN = 3
    LOCKED = 4
    CROSSED = 5


class Side(IntEnum):
    BID = 0
    ASK = 1


@dataclass(frozen=True)
class Profile:
    instruments: int = 4
    venues: int = 4
    price_w: int = 20
    qty_w: int = 14
    time_w: int = 16
    seq_w: int = 12
    tag_w: int = 16

    def __post_init__(self):
        """Reject geometries that the portable decimal simulation ABI cannot represent."""
        for name in ("instruments", "venues"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 24:
                raise ValueError(f"{name} must be in [1, 24]")
        for name in ("price_w", "qty_w", "time_w", "seq_w", "tag_w"):
            value = getattr(self, name)
            if type(value) is not int or not 2 <= value <= 28:
                raise ValueError(f"{name} must be in [2, 28]")

    @property
    def inst_w(self):
        """Encode every valid instrument plus at least one invalid value."""
        return self.instruments.bit_length()

    @property
    def venue_w(self):
        """Encode every valid venue plus at least one invalid value."""
        return self.venues.bit_length()

    @property
    def price_max(self):
        """Return the largest positive tick price representable on a side."""
        return mask(self.price_w)

    @property
    def qty_max(self):
        """Return the largest size representable by one venue quote."""
        return mask(self.qty_w)

    @property
    def time_half(self):
        """Return the first ambiguous timestamp distance."""
        return 1 << (self.time_w - 1)

    @property
    def seq_half(self):
        """Return the first ambiguous serial number distance."""
        return 1 << (self.seq_w - 1)

    @property
    def venue_mask(self):
        """Return the mask containing exactly the physical venue bits."""
        return mask(self.venues)

    def sv_parameters(self):
        """Return named parameter defaults and overrides for bench elaboration."""
        return {
            "INSTRUMENTS": self.instruments,
            "VENUES": self.venues,
            "PRICE_W": self.price_w,
            "QTY_W": self.qty_w,
            "TIME_W": self.time_w,
            "SEQ_W": self.seq_w,
            "TAG_W": self.tag_w,
        }


PROFILES = {
    "default": Profile(),
    "solo": Profile(1, 1, 8, 4, 7, 5, 8),
    "odd": Profile(3, 5, 12, 9, 11, 8, 12),
    "wide": Profile(6, 8, 28, 22, 28, 21, 20),
}


def mask(width):
    """Return a mask containing width low one bits."""
    return (1 << width) - 1


def bounded(value, width):
    """Truncate a conceptual integer to an unsigned modular bus field."""
    return value & mask(width)


def distance(new, old, width):
    """Measure an unsigned modular delta from old to new."""
    return bounded(new - old, width)


def serial_is_forward(new, old, width):
    """Recognize strictly forward serial numbers within half the modulus."""
    delta = distance(new, old, width)
    return 0 < delta < 1 << (width - 1)


def values(record):
    """Flatten an integer record in its declared dataclass field order."""
    return tuple(int(getattr(record, field.name)) for field in fields(record))


def unsigned(value, width, label):
    """Check a Python integer is representable as the named unsigned field."""
    if not isinstance(value, int) or not 0 <= value <= mask(width):
        raise ValueError(f"{label}={value!r} does not fit unsigned {width} bits")
    return int(value)


def signed(value, width, label):
    """Check a Python integer is representable as a signed two's-complement field."""
    half = 1 << (width - 1)
    if not isinstance(value, int) or not -half <= value < half:
        raise ValueError(f"{label}={value!r} does not fit signed {width} bits")
    return int(value)


def validate_fields(record, widths, signed_fields=()):
    """Validate shape and ranges without changing a typed record."""
    labels = tuple(field.name for field in fields(record))
    if len(labels) != len(widths):
        raise ValueError("field-width declaration does not match record shape")
    for label, width in zip(labels, widths):
        check = signed if label in signed_fields else unsigned
        check(getattr(record, label), width, label)
    return record


@dataclass(frozen=True)
class Command:
    op: int = Op.REPLACE
    instrument: int = 0
    venue: int = 0
    sequence: int = 0
    bid_price: int = 0
    ask_price: int = 0
    bid_qty: int = 0
    ask_qty: int = 0
    tag: int = 0

    def check(self, profile):
        """Validate bus ranges while preserving intentionally malformed commands."""
        return validate_fields(
            self,
            (3, profile.inst_w, profile.venue_w, profile.seq_w,
             profile.price_w, profile.price_w, profile.qty_w, profile.qty_w, profile.tag_w),
        )

    @classmethod
    def replace(cls, instrument, venue, sequence, bid, ask, tag=0):
        """Build a market update from two (price, quantity) tuples."""
        return cls(Op.REPLACE, instrument, venue, sequence, bid[0], ask[0], bid[1], ask[1], tag)

    @classmethod
    def withdraw(cls, instrument, venue, sequence, tag=0):
        """Build a sequence-advancing removal of the current quote."""
        return cls(Op.WITHDRAW, instrument, venue, sequence, tag=tag)

    @classmethod
    def venue_enabled(cls, instrument, venue, on, tag=0):
        """Build a control command changing venue eligibility for one instrument."""
        return cls(Op.ENABLE if on else Op.DISABLE, instrument, venue, tag=tag)


@dataclass(frozen=True)
class Query:
    instrument: int = 0
    max_age: int = 0
    tag: int = 0

    def check(self, profile):
        """Validate representability without filtering out rejected query values."""
        return validate_fields(self, (profile.inst_w, profile.time_w, profile.tag_w))


@dataclass(frozen=True)
class Ack:
    code: int = Code.OK
    tag: int = 0

    def check(self, profile):
        """Validate one acknowledgement's status and tag fields."""
        return validate_fields(self, (4, profile.tag_w))


@dataclass(frozen=True)
class Winner:
    valid: int = 0
    venue: int = 0
    price: int = 0
    qty: int = 0
    age: int = 0
    sequence: int = 0

    def check(self, profile):
        """Validate the encoded ranges of one result-side candidate."""
        return validate_fields(
            self, (1, profile.venue_w, profile.price_w, profile.qty_w, profile.time_w, profile.seq_w)
        )


@dataclass(frozen=True)
class Result:
    tag: int = 0
    code: int = Code.OK
    state: int = State.EMPTY
    tick: int = 0
    active: int = 0
    stale: int = 0
    bid: Winner = Winner()
    ask: Winner = Winner()
    spread: int = 0

    def encode(self):
        """Flatten both side winners after the common fields, then append signed spread."""
        return (
            self.tag, self.code, self.state, self.tick, self.active, self.stale,
            *values(self.bid), *values(self.ask), self.spread,
        )

    @classmethod
    def decode(cls, data, profile):
        """Reconstruct a response from decimal fields and enforce bus width constraints."""
        key, code, state, tick, active, stale = data[:6]
        result = cls(
            key, code, state, tick, active, stale,
            Winner(*data[6:12]), Winner(*data[12:18]), data[18],
        )
        return result.check(profile)

    def check(self, profile):
        """Check every common and per-side output width."""
        unsigned(self.tag, profile.tag_w, "tag")
        unsigned(self.code, 4, "code")
        unsigned(self.state, 3, "state")
        unsigned(self.tick, profile.time_w, "tick")
        unsigned(self.active, profile.venues, "active")
        unsigned(self.stale, profile.venues, "stale")
        self.bid.check(profile)
        self.ask.check(profile)
        signed(self.spread, profile.price_w + 1, "spread")
        return self


@dataclass(frozen=True)
class Drive:
    tick: int = 0
    reset: int = 0
    ack_ready: int = 1
    result_ready: int = 1
    command: Command | None = None
    query: Query | None = None

    def check(self, profile):
        """Validate shape, including readiness bits even when no transaction is offered."""
        unsigned(self.tick, profile.time_w, "tick")
        unsigned(self.reset, 1, "reset")
        unsigned(self.ack_ready, 1, "ack_ready")
        unsigned(self.result_ready, 1, "result_ready")
        if self.command is not None:
            self.command.check(profile)
        if self.query is not None:
            self.query.check(profile)
        return self

    def encode(self):
        """Emit cycle inputs, zeroing ignored payloads on invalid cycles."""
        return (
            self.tick, self.reset, self.ack_ready, self.result_ready,
            int(self.command is not None), *values(self.command or Command()),
            int(self.query is not None), *values(self.query or Query()),
        )

    @classmethod
    def decode(cls, data, profile):
        """Read the fixed stimulus columns and recover optional offered payloads."""
        tick, reset, ack_ready, result_ready, command_valid = data[:5]
        unsigned(command_valid, 1, "command_valid")
        query_valid = data[14]
        unsigned(query_valid, 1, "query_valid")
        command = Command(*data[5:14]).check(profile)
        query = Query(*data[15:18]).check(profile)
        return cls(
            tick, reset, ack_ready, result_ready,
            command if command_valid else None, query if query_valid else None,
        ).check(profile)


@dataclass(frozen=True)
class Sample:
    cmd_ready: int
    query_ready: int
    ack: Ack | None
    result: Result | None

    def encode(self):
        """Emit observed readiness/valid bits and canonical ignored payload fields."""
        return (
            self.cmd_ready, self.query_ready, int(self.ack is not None),
            *values(self.ack or Ack()), int(self.result is not None),
            *(self.result or Result()).encode(),
        )

    @classmethod
    def decode(cls, data, profile):
        """Parse one observed RTL cycle while validating every present payload."""
        cmd_ready, query_ready, ack_valid, code, tag, result_valid = data[:6]
        for name, value in zip(
            ("cmd_ready", "query_ready", "ack_valid", "result_valid"),
            (cmd_ready, query_ready, ack_valid, result_valid),
        ):
            unsigned(value, 1, name)
        ack = Ack(code, tag).check(profile) if ack_valid else None
        result = Result.decode(data[6:25], profile) if result_valid else None
        return cls(cmd_ready, query_ready, ack, result)

    def accepted_command(self, drive):
        """Indicate an actual command transfer on this sampled edge."""
        return bool(self.cmd_ready and drive.command is not None and not drive.reset)

    def accepted_query(self, drive):
        """Indicate an actual query transfer on this sampled edge."""
        return bool(self.query_ready and drive.query is not None and not drive.reset)

    def transferred_ack(self, drive):
        """Return the delivered acknowledgement or None when the bus stalls."""
        return self.ack if drive.ack_ready and not drive.reset else None

    def transferred_result(self, drive):
        """Return the delivered result or None when blocked or resetting."""
        return self.result if drive.result_ready and not drive.reset else None


def decimal_row(line, size):
    """Parse exactly size whitespace-separated decimal integer fields."""
    columns = line.split()
    if len(columns) != size:
        raise ValueError(f"expected {size} columns, got {len(columns)}")
    try:
        return tuple(int(column, 10) for column in columns)
    except ValueError as exc:
        raise ValueError("non-decimal value in simulation row") from exc


def row_text(row: Iterable[int]):
    """Serialize a row as decimal numbers without platform-dependent formatting."""
    return " ".join(str(int(value)) for value in row)
