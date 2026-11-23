"""Field definitions and reusable validation for the limiter wire interface."""

import json
from dataclasses import dataclass, fields
from enum import IntEnum
from pathlib import Path
from typing import Iterable, Optional, Tuple


class Operation(IntEnum):
    """Command opcode sampled with the request handshake."""

    CONFIG = 0
    OFFER = 1
    INSPECT = 2
    CLEAR = 3


class Status(IntEnum):
    """Decision code. Only PASS spends a token."""

    OK = 0
    PASS = 1
    LIMITED = 2
    BAD_KEY = 3
    BAD_FIELDS = 4
    DISABLED = 5
    BAD_TICK = 6


def maximum(width):
    """Return the greatest unsigned integer representable in width bits."""
    if type(width) is not int or width <= 0:
        raise ValueError(f"invalid width {width!r}")
    return (1 << width) - 1


def unsigned(value, width, name):
    """Require an exact unsigned field and name it when reporting an error."""
    if type(value) is not int or not 0 <= value <= maximum(width):
        raise ValueError(f"{name}={value!r} does not fit u{width}")
    return value


def bool_bit(value, name):
    """Validate that a Boolean field is a single wire bit."""
    return unsigned(value, 1, name)


def ints(line, length, origin):
    """Read one fixed-schema line of hexadecimal words from a simulator file."""
    tokens = line.split()
    if len(tokens) != length:
        raise ValueError(f"{origin}: expected {length} columns, got {len(tokens)}")
    try:
        return tuple(int(token, 16) for token in tokens)
    except ValueError as exc:
        raise ValueError(f"{origin}: invalid hexadecimal word") from exc


def hexadecimal(values):
    """Format a complete vector row with no whitespace-sensitive separators."""
    return " ".join(format(word, "x") for word in values)


def checked_keys(record, expected, location):
    """Require exactly the named keys so typos do not silently pick defaults."""
    if not isinstance(record, dict):
        raise ValueError(f"{location}: expected an object")
    unfamiliar = set(record) - expected
    missing = expected - set(record)
    if unfamiliar or missing:
        raise ValueError(
            f"{location}: missing {sorted(missing)}, unexpected {sorted(unfamiliar)}"
        )


@dataclass(frozen=True)
class Geometry:
    """Compile-time sizes shared by RTL test profiles and text trace tools."""

    keys: int = 5
    tick_w: int = 12
    token_w: int = 12
    count_w: int = 16
    tag_w: int = 16

    def __post_init__(self):
        """Keep values within the checked RTL and signed scanner limits."""
        if type(self.keys) is not int or not 1 <= self.keys <= 64:
            raise ValueError("keys must be between 1 and 64")
        for name in ("tick_w", "token_w", "count_w", "tag_w"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 24:
                raise ValueError(f"{name} must be between 1 and 24")
        if self.tick_w < 2:
            raise ValueError("tick_w must leave a time-order bit")

    @property
    def key_w(self):
        """Size the key port even when there is only one implemented row."""
        return max(1, (self.keys - 1).bit_length())

    @property
    def tick_mask(self):
        """Mask for arithmetic performed in the modular clock domain."""
        return maximum(self.tick_w)

    @property
    def half(self):
        """The first modular distance considered ambiguous or backwards."""
        return 1 << (self.tick_w - 1)

    @property
    def token_max(self):
        """Largest capacity, charge, or per-tick rate the port accepts."""
        return maximum(self.token_w)

    @property
    def count_max(self):
        """Saturation level of each statistics field."""
        return maximum(self.count_w)

    @property
    def tag_max(self):
        """Largest tag accepted by the command and echoed in the response."""
        return maximum(self.tag_w)

    def delta(self, tick, anchor):
        """Return a forward modular distance or None beyond half the range."""
        unsigned(tick, self.tick_w, "tick")
        unsigned(anchor, self.tick_w, "anchor")
        difference = (tick - anchor) & self.tick_mask
        return difference if difference < self.half else None

    def wrapped(self, tick):
        """Encode an absolute software tick on the hardware time port."""
        if type(tick) is not int or tick < 0:
            raise ValueError("absolute tick must be a nonnegative integer")
        return tick & self.tick_mask

    def to_record(self):
        """Export named parameters in the trace manifest spelling."""
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_record(cls, record):
        """Recreate a geometry after rejecting unknown or missing parameters."""
        checked_keys(record, {field.name for field in fields(cls)}, "geometry")
        return cls(**record)


PROFILES = {
    "default": Geometry(),
    "small": Geometry(keys=3, tick_w=6, token_w=6, count_w=4, tag_w=12),
    "single": Geometry(keys=1, tick_w=5, token_w=8, count_w=8, tag_w=8),
    "wide_tokens": Geometry(keys=7, tick_w=8, token_w=16, count_w=5, tag_w=16),
    "wide_tick": Geometry(keys=2, tick_w=20, token_w=4, count_w=7, tag_w=10),
}


def as_operation(value):
    """Accept a known operation code while rejecting invalid enum values."""
    if type(value) is not int and not isinstance(value, Operation):
        raise ValueError(f"invalid operation {value!r}")
    try:
        return Operation(value)
    except ValueError as exc:
        raise ValueError(f"invalid operation {value!r}") from exc


def as_status(value):
    """Accept a known decision status from a trace or the reference model."""
    if type(value) is not int and not isinstance(value, Status):
        raise ValueError(f"invalid status {value!r}")
    try:
        return Status(value)
    except ValueError as exc:
        raise ValueError(f"invalid status {value!r}") from exc


@dataclass(frozen=True)
class Command:
    """Request fields in simulator vector order after valid/ready control."""

    op: Operation
    key: int
    tick: int
    amount: int = 0
    rate: int = 0
    enable: int = 0
    tag: int = 0

    def validate(self, geometry):
        """Check wire width only. The limiter validates behavior and keys."""
        as_operation(self.op)
        unsigned(self.key, geometry.key_w, "key")
        unsigned(self.tick, geometry.tick_w, "tick")
        unsigned(self.amount, geometry.token_w, "amount")
        unsigned(self.rate, geometry.token_w, "rate")
        bool_bit(self.enable, "enable")
        unsigned(self.tag, geometry.tag_w, "tag")
        return self

    def words(self):
        """Return the seven numeric fields accepted by the SV scanner."""
        return (
            int(self.op),
            self.key,
            self.tick,
            self.amount,
            self.rate,
            self.enable,
            self.tag,
        )

    @classmethod
    def from_words(cls, values, geometry):
        """Decode fields read from SV and apply port-width checks."""
        op, key, tick, amount, rate, enable, tag = values
        return cls(as_operation(op), key, tick, amount, rate, enable, tag).validate(geometry)

    @classmethod
    def configure(cls, key, tick, capacity, refill, tag=0):
        """Enable or replace one row and fill it to its new capacity."""
        return cls(Operation.CONFIG, key, tick, capacity, refill, 1, tag)

    @classmethod
    def disable(cls, key, tick, tag=0):
        """Construct the exact zero-capacity wire image for a disabled row."""
        return cls(Operation.CONFIG, key, tick, tag=tag)

    @classmethod
    def offer(cls, key, tick, amount, tag=0):
        """Build a request which consumes credits on a successful admission."""
        return cls(Operation.OFFER, key, tick, amount, tag=tag)

    @classmethod
    def inspect(cls, key, tick, tag=0):
        """Read a bucket after advancing its available credit to this tick."""
        return cls(Operation.INSPECT, key, tick, tag=tag)

    @classmethod
    def clear(cls, key, tick, tag=0):
        """Reset statistics without replacing the configured bucket policy."""
        return cls(Operation.CLEAR, key, tick, tag=tag)


ZERO_COMMAND = Command(Operation.CONFIG, 0, 0)


@dataclass(frozen=True)
class Response:
    """Decision and snapshot sufficient to reconstruct each state transition."""

    op: Operation
    key: int
    tag: int
    status: Status
    enabled: int
    anchor: int
    capacity: int
    rate: int
    before: int
    balance: int
    passed: int
    limited: int
    charged: int
    saturated: int

    def words(self):
        """Return fields in the exact order of the SV capture file."""
        return (
            int(self.op),
            self.key,
            self.tag,
            int(self.status),
            self.enabled,
            self.anchor,
            self.capacity,
            self.rate,
            self.before,
            self.balance,
            self.passed,
            self.limited,
            self.charged,
            self.saturated,
        )

    def validate(self, geometry):
        """Reject corrupt captures without guessing an alternative width."""
        as_operation(self.op)
        as_status(self.status)
        unsigned(self.key, geometry.key_w, "response key")
        unsigned(self.tag, geometry.tag_w, "response tag")
        bool_bit(self.enabled, "enabled")
        unsigned(self.anchor, geometry.tick_w, "anchor")
        for name in ("capacity", "rate", "before", "balance"):
            unsigned(getattr(self, name), geometry.token_w, name)
        for name in ("passed", "limited", "charged"):
            unsigned(getattr(self, name), geometry.count_w, name)
        unsigned(self.saturated, 3, "saturated")
        return self

    @classmethod
    def from_words(cls, values, geometry):
        """Convert the fourteen wire words into typed and validated fields."""
        op, key, tag, status, *snapshot = values
        return cls(as_operation(op), key, tag, as_status(status), *snapshot).validate(geometry)

    def differences(self, actual):
        """Describe each unexpected field so a failing seed is actionable."""
        return {
            field.name: (getattr(self, field.name), getattr(actual, field.name))
            for field in fields(self)
            if getattr(self, field.name) != getattr(actual, field.name)
        }


ZERO_RESPONSE = Response(Operation.CONFIG, 0, 0, Status.OK, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
COMMAND_COLUMNS = len(fields(Command))
RESPONSE_COLUMNS = len(fields(Response))


@dataclass(frozen=True)
class Drive:
    """Cycle inputs, including whether the request and sink are asserting valid/ready."""

    reset: int = 0
    valid: int = 0
    ready: int = 1
    command: Command = ZERO_COMMAND

    def words(self):
        """Place the three control bits before the request payload in a row."""
        return (self.reset, self.valid, self.ready, *self.command.words())

    def validate(self, geometry):
        """Confirm driver control and payload fit the simulator's interfaces."""
        bool_bit(self.reset, "reset")
        bool_bit(self.valid, "valid")
        bool_bit(self.ready, "ready")
        self.command.validate(geometry)
        return self

    @classmethod
    def from_words(cls, values, geometry):
        """Decode a driver row with payload even when input valid is false."""
        reset, valid, ready, *request = values
        return cls(reset, valid, ready, Command.from_words(request, geometry)).validate(geometry)


@dataclass(frozen=True)
class Observation:
    """Signals sampled just before one active clock edge."""

    input_ready: int
    output_valid: int
    response: Optional[Response]

    def words(self):
        """Use a canonical zero payload whenever there is no visible response."""
        payload = self.response or ZERO_RESPONSE
        return (self.input_ready, self.output_valid, *payload.words())

    @classmethod
    def from_words(cls, values, geometry):
        """Decode the sampled ready, valid, and normalized output payload."""
        input_ready, output_valid, *payload = values
        bool_bit(input_ready, "input ready")
        bool_bit(output_valid, "output valid")
        if not output_valid and any(payload):
            raise ValueError("invalid output must have a normalized zero payload")
        response = Response.from_words(payload, geometry) if output_valid else None
        return cls(input_ready, output_valid, response)


@dataclass(frozen=True)
class Manifest:
    """Identity and reproducibility metadata for a three-file simulation run."""

    profile: str
    scenario: str
    seed: int
    cycles: int
    geometry: Geometry

    def to_record(self):
        """Serialize the manifest without relying on Python dataclass repr."""
        return {
            "schema": 1,
            "profile": self.profile,
            "scenario": self.scenario,
            "seed": self.seed,
            "cycles": self.cycles,
            "geometry": self.geometry.to_record(),
        }

    @classmethod
    def from_record(cls, record):
        """Require the current schema before accepting vector compatibility."""
        expected = {"schema", "profile", "scenario", "seed", "cycles", "geometry"}
        checked_keys(record, expected, "manifest")
        if type(record["schema"]) is not int or record["schema"] != 1:
            raise ValueError("unsupported trace schema")
        for name in ("profile", "scenario"):
            if not isinstance(record[name], str) or not record[name]:
                raise ValueError(f"{name} must be nonempty")
        for name in ("seed", "cycles"):
            if type(record[name]) is not int or record[name] < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        return cls(
            record["profile"],
            record["scenario"],
            record["seed"],
            record["cycles"],
            Geometry.from_record(record["geometry"]),
        )

    def write(self, path):
        """Record the full configuration alongside generated simulator input."""
        Path(path).write_text(json.dumps(self.to_record(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path):
        """Load and validate a manifest before interpreting the associated files."""
        return cls.from_record(json.loads(Path(path).read_text(encoding="utf-8")))


def read_rows(path, count):
    """Yield numbered numeric rows and reject truncated or extra columns."""
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            yield ints(line, count, f"{path}:{number}")


def write_rows(path, rows: Iterable[Tuple[int, ...]]):
    """Write vector rows sequentially so large reproductions stay memory-light."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(hexadecimal(row))
            handle.write("\n")
