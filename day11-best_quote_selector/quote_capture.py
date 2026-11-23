"""Join a sampled ready/valid bus trace into readable logical transactions."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, fields

from quote_schema import Ack, Code, Command, Op, Query, Result, State
from quote_verify import TraceError


@dataclass(frozen=True)
class CommandCapture:
    accepted: int
    delivered: int
    tick: int
    request: Command
    ack: Ack

    @property
    def latency(self):
        """Measure external command acceptance to acknowledgement delivery in clocks."""
        return self.delivered - self.accepted


@dataclass(frozen=True)
class QueryCapture:
    accepted: int
    delivered: int
    tick: int
    request: Query
    result: Result

    @property
    def latency(self):
        """Measure external query acceptance to result delivery in clocks."""
        return self.delivered - self.accepted


@dataclass(frozen=True)
class Flush:
    cycle: int
    commands: tuple[int, ...]
    queries: tuple[int, ...]


class CaptureJoin:
    def __init__(self):
        """Start edge-local queues for matching responses to accepted inputs in order."""
        self.commands = deque()
        self.queries = deque()
        self.next_cycle = 0

    def _reset(self, cycle):
        """Clear both queues and record the identifying tags lost to reset."""
        event = Flush(
            cycle, tuple(entry[2].tag for entry in self.commands),
            tuple(entry[2].tag for entry in self.queries),
        )
        self.commands.clear()
        self.queries.clear()
        return event

    def _take(self, pending, tag, cycle, label):
        """Require a queued request and match the tag at the front of its channel."""
        if not pending:
            raise TraceError(f"cycle {cycle}: extra {label} without an accepting edge")
        oldest = pending.popleft()
        if oldest[2].tag != tag:
            raise TraceError(
                f"cycle {cycle}: expected {label} tag {oldest[2].tag}, got {tag}"
            )
        return oldest

    def step(self, cycle, drive, sample):
        """Deliver old entries before enqueueing new ones from the same sampled edge."""
        if cycle != self.next_cycle:
            raise TraceError(f"expected edge {self.next_cycle}, got {cycle}")
        self.next_cycle += 1
        if drive.reset:
            return (self._reset(cycle),)
        captured = []
        ack = sample.transferred_ack(drive)
        if ack is not None:
            at, tick, request = self._take(self.commands, ack.tag, cycle, "acknowledgement")
            captured.append(CommandCapture(at, cycle, tick, request, ack))
        result = sample.transferred_result(drive)
        if result is not None:
            at, tick, request = self._take(self.queries, result.tag, cycle, "result")
            if result.tick != tick:
                raise TraceError(f"cycle {cycle}: result tick differs from accepting edge")
            captured.append(QueryCapture(at, cycle, tick, request, result))
        if sample.accepted_command(drive):
            self.commands.append((cycle, drive.tick, drive.command))
        if sample.accepted_query(drive):
            self.queries.append((cycle, drive.tick, drive.query))
        return tuple(captured)

    def finish(self):
        """Reject an incomplete capture whose accepted jobs never returned or flushed."""
        if self.commands or self.queries:
            raise TraceError(
                f"trace ends with {len(self.commands)} commands and {len(self.queries)} queries pending"
            )


def join_cycles(drives, samples):
    """Read both iterables together and convert transfers and resets to logical records."""
    no_row = object()
    source = iter(drives)
    sink = iter(samples)
    joiner = CaptureJoin()
    while True:
        drive = next(source, no_row)
        sample = next(sink, no_row)
        cycle = joiner.next_cycle
        if drive is no_row and sample is no_row:
            joiner.finish()
            return
        if drive is no_row or sample is no_row:
            raise TraceError(f"cycle {cycle}: drive/trace length mismatch")
        yield from joiner.step(cycle, drive, sample)


def enum_or_number(enum_type, value):
    """Use known symbolic codes in reports while preserving unknown corrupt values."""
    try:
        return enum_type(value).name
    except ValueError:
        return int(value)


def command_object(captured):
    """Render a delivered update with the original wire fields and acceptance time."""
    obj = asdict(captured)
    obj["kind"] = "command"
    obj["latency"] = captured.latency
    obj["request"]["op"] = enum_or_number(Op, captured.request.op)
    obj["ack"]["code"] = enum_or_number(Code, captured.ack.code)
    return obj


def query_object(captured):
    """Render a delivered result, using null instead of an all-zero absent side."""
    obj = asdict(captured)
    obj["kind"] = "query"
    obj["latency"] = captured.latency
    result = obj["result"]
    result["code"] = enum_or_number(Code, captured.result.code)
    result["state"] = enum_or_number(State, captured.result.state)
    for side in ("bid", "ask"):
        if not getattr(captured.result, side).valid:
            result[side] = None
    if result["bid"] is None or result["ask"] is None:
        result["spread"] = None
    return obj


def serialize(captured):
    """Return portable Python data for one captured transfer or reset event."""
    if isinstance(captured, CommandCapture):
        return command_object(captured)
    if isinstance(captured, QueryCapture):
        return query_object(captured)
    if isinstance(captured, Flush):
        return {"kind": "reset", **asdict(captured)}
    raise ValueError(f"unknown capture record kind {type(captured).__name__}")


def _detail_keys(obj, cls):
    """Validate a normalized object contains exactly the named dataclass fields."""
    expected = {field.name for field in fields(cls)}
    if not isinstance(obj, dict) or set(obj) != expected:
        raise ValueError(f"bad {cls.__name__} fields")
    return obj


def _resolved_enum(enum_type, value):
    """Decode the numeric or named form emitted for a known enum field."""
    try:
        return int(enum_type[value]) if isinstance(value, str) else int(enum_type(value))
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"unknown {enum_type.__name__} value {value!r}") from exc


def _unsigned(value, name):
    """Require a nonnegative integer for a capture edge or timestamp."""
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _check_timing(obj):
    """Cross-check ordered acceptance and delivery edges with their published latency."""
    accepted = _unsigned(obj["accepted"], "accepted")
    delivered = _unsigned(obj["delivered"], "delivered")
    tick = _unsigned(obj["tick"], "tick")
    latency = _unsigned(obj["latency"], "latency")
    if delivered - accepted != latency or latency < 1:
        raise ValueError("delivery latency disagrees with the recorded edges")
    return accepted, delivered, tick


def deserialize_command(obj, profile):
    """Reconstruct a typed command capture from the exported detail object."""
    if set(obj) != {"kind", "latency", "accepted", "delivered", "tick", "request", "ack"}:
        raise ValueError("unexpected command capture fields")
    accepted, delivered, tick = _check_timing(obj)
    raw_request = dict(_detail_keys(obj["request"], Command))
    if isinstance(raw_request["op"], str):
        raw_request["op"] = _resolved_enum(Op, raw_request["op"])
    command = Command(**raw_request).check(profile)
    raw_ack = dict(_detail_keys(obj["ack"], Ack))
    raw_ack["code"] = _resolved_enum(Code, raw_ack["code"])
    ack = Ack(**raw_ack).check(profile)
    if command.tag != ack.tag:
        raise ValueError("mismatched acknowledgement tag")
    return CommandCapture(accepted, delivered, tick, command, ack)


def deserialize_query(obj, profile):
    """Reconstruct a typed query capture while restoring canonical invalid winners."""
    from quote_schema import Winner

    if set(obj) != {"kind", "latency", "accepted", "delivered", "tick", "request", "result"}:
        raise ValueError("unexpected query capture fields")
    accepted, delivered, tick = _check_timing(obj)
    request = Query(**_detail_keys(obj["request"], Query)).check(profile)
    raw_result = dict(_detail_keys(obj["result"], Result))
    raw_result["code"] = _resolved_enum(Code, raw_result["code"])
    raw_result["state"] = _resolved_enum(State, raw_result["state"])
    for side in ("bid", "ask"):
        raw_side = raw_result[side]
        raw_result[side] = Winner() if raw_side is None else Winner(**_detail_keys(raw_side, Winner))
    if raw_result["spread"] is None:
        raw_result["spread"] = 0
    result = Result(**raw_result).check(profile)
    if result.tag != request.tag or result.tick != tick:
        raise ValueError("query capture tag or timestamp mismatch")
    return QueryCapture(accepted, delivered, tick, request, result)


def deserialize(obj, profile):
    """Read an exported detail object and validate its typed shape and identity."""
    if not isinstance(obj, dict) or "kind" not in obj:
        raise ValueError("capture record requires a kind")
    if obj["kind"] == "command":
        return deserialize_command(obj, profile)
    if obj["kind"] == "query":
        return deserialize_query(obj, profile)
    if obj["kind"] == "reset":
        if set(obj) != {"kind", "cycle", "commands", "queries"}:
            raise ValueError("unexpected reset capture fields")
        time = _unsigned(obj["cycle"], "cycle")
        if not isinstance(obj["commands"], (list, tuple)) or not isinstance(obj["queries"], (list, tuple)):
            raise ValueError("reset needs two tag lists")
        commands = tuple(_unsigned(val, "command tag") for val in obj["commands"])
        queries = tuple(_unsigned(val, "query tag") for val in obj["queries"])
        return Flush(time, commands, queries)
    raise ValueError(f"unknown capture kind {obj['kind']!r}")
