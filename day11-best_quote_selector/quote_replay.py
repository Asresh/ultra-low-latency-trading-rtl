"""Convert normalized dated quotes to handshakes for the offline RTL bench."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, fields
import json
from pathlib import Path

from quote_model import Machine
from quote_schema import Command, Drive, Op, Query, bounded


class ReplayError(ValueError):
    """A replay record or schedule violated its configured limits."""


@dataclass(frozen=True)
class Event:
    cycle: int
    command: Command | None = None
    query: Query | None = None
    reset: bool = False

    def check(self, profile):
        """Validate event shape and payload ranges before any scheduling."""
        if type(self.cycle) is not int or self.cycle < 0:
            raise ReplayError("cycle must be a nonnegative integer")
        if type(self.reset) is not bool:
            raise ReplayError("reset must be Boolean")
        if self.reset and (self.command is not None or self.query is not None):
            raise ReplayError("a reset event cannot carry a transaction")
        if not self.reset and self.command is None and self.query is None:
            raise ReplayError("an event needs a command, query, or reset")
        if self.command is not None:
            if not isinstance(self.command, Command):
                raise ReplayError("expected a typed command")
            self.command.check(profile)
        if self.query is not None:
            if not isinstance(self.query, Query):
                raise ReplayError("expected a typed query")
            self.query.check(profile)
        return self


@dataclass(frozen=True)
class SinkWindow:
    start: int
    stop: int
    ack: bool = False
    result: bool = False

    def __post_init__(self):
        """Require nonempty half-open intervals blocking at least one channel."""
        if type(self.start) is not int or type(self.stop) is not int:
            raise ReplayError("sink window endpoints must be integers")
        if self.start < 0 or self.stop <= self.start:
            raise ReplayError("sink windows must have 0 <= start < stop")
        if type(self.ack) is not bool or type(self.result) is not bool:
            raise ReplayError("sink window options must be Boolean")
        if not self.ack and not self.result:
            raise ReplayError("sink window does not block a channel")


@dataclass(frozen=True)
class Accepted:
    channel: str
    planned: int
    actual: int
    tag: int
    tick: int

    @property
    def delay(self):
        """Return how many additional clocks the request waited before admission."""
        return self.actual - self.planned


@dataclass(frozen=True)
class Canceled:
    channel: str
    planned: int
    reset_at: int
    tag: int


@dataclass(frozen=True)
class Schedule:
    drives: tuple[Drive, ...]
    accepted: tuple[Accepted, ...]
    canceled: tuple[Canceled, ...]


def event_channel(event, channel):
    """Return the one optional payload carried on the named input bus."""
    if channel == "command":
        return event.command
    if channel == "query":
        return event.query
    raise ReplayError(f"unknown channel {channel!r}")


def check_order(events, profile, limit):
    """Check normalized time, unique reset edges, and portable scheduling bounds."""
    previous = None
    reset_cycles = set()
    data_cycles = set()
    clean = []
    for event in events:
        if not isinstance(event, Event):
            raise ReplayError(f"expected Event, received {type(event).__name__}")
        try:
            event.check(profile)
        except ValueError as exc:
            raise ReplayError(f"edge {event.cycle}: {exc}") from exc
        if event.cycle >= limit:
            raise ReplayError(f"edge {event.cycle} exceeds schedule limit {limit}")
        if previous is not None and event.cycle < previous:
            raise ReplayError("events must be sorted by cycle")
        previous = event.cycle
        if event.reset:
            if event.cycle in reset_cycles:
                raise ReplayError(f"duplicate reset at edge {event.cycle}")
            reset_cycles.add(event.cycle)
        else:
            data_cycles.add(event.cycle)
        clean.append(event)
    if reset_cycles & data_cycles:
        cycle = min(reset_cycles & data_cycles)
        raise ReplayError(f"reset and data share edge {cycle}")
    if 0 not in reset_cycles:
        raise ReplayError("expected an explicit reset at edge zero")
    return clean


def merge_windows(windows, channel):
    """Return sorted nonoverlapping blocked intervals for one output."""
    if channel not in ("ack", "result"):
        raise ReplayError(f"unknown sink channel {channel!r}")
    merged = []
    for window in sorted(windows, key=lambda item: item.start):
        if not getattr(window, channel):
            continue
        if merged and merged[-1][1] >= window.start:
            start, old_stop = merged[-1]
            merged[-1] = start, max(old_stop, window.stop)
        else:
            merged.append((window.start, window.stop))
    return deque(merged)


def ready_at(windows, edge):
    """Consume ended intervals and return ready unless the head blocks this edge."""
    while windows and windows[0][1] <= edge:
        windows.popleft()
    return int(not windows or windows[0][0] > edge)


class ReplayScheduler:
    def __init__(self, profile, events, windows, origin, limit):
        """Validate the whole description before building deterministic input signals."""
        if type(limit) is not int or limit < 4:
            raise ReplayError("max_cycles must be an integer of at least four")
        if type(origin) is not int or not 0 <= origin < profile.time_half * 2:
            raise ReplayError("tick_origin does not fit the timestamp width")
        active_windows = tuple(windows)
        for window in active_windows:
            if not isinstance(window, SinkWindow):
                raise ReplayError("expected a sink window")
            if window.stop > limit:
                raise ReplayError("sink window exceeds schedule limit")
        self.events = deque(check_order(events, profile, limit))
        self.sink = {name: merge_windows(active_windows, name)
                     for name in ("ack", "result")}
        self.origin = origin
        self.limit = limit
        self.profile = profile
        self.model = Machine(profile)
        self.pending = {"command": deque(), "query": deque()}
        self.accepted = []
        self.canceled = []
        self.drives = []

    def _prepare(self, cycle):
        """Release this edge's events in file order and surface any reset boundary."""
        reset = False
        while self.events and self.events[0].cycle == cycle:
            event = self.events.popleft()
            if event.reset:
                reset = True
            else:
                for channel, queue in self.pending.items():
                    payload = event_channel(event, channel)
                    if payload is not None:
                        queue.append((cycle, payload))
        return reset

    def _cancel(self, cycle):
        """Report queued requests abandoned by reset, including an already stalled head."""
        for channel, queue in self.pending.items():
            for planned, payload in queue:
                self.canceled.append(Canceled(channel, planned, cycle, payload.tag))
            queue.clear()

    def _record(self, channel, cycle, tick, ready):
        """Pop and timestamp one admitted source transaction when ready is true."""
        queue = self.pending[channel]
        if ready and queue:
            planned, payload = queue.popleft()
            self.accepted.append(Accepted(channel, planned, cycle, payload.tag, tick))

    def _pending_payload(self, channel, reset):
        """Return the stalled head verbatim until its handshake or cancellation."""
        queue = self.pending[channel]
        if not reset and queue:
            return queue[0][1]
        return None

    def step(self, cycle):
        """Queue due events, generate one edge, and record its actual input handshakes."""
        reset = self._prepare(cycle)
        if reset:
            self._cancel(cycle)
        tick = bounded(self.origin + cycle, self.profile.time_w)
        drive = Drive(
            tick, int(reset), ready_at(self.sink["ack"], cycle),
            ready_at(self.sink["result"], cycle), self._pending_payload("command", reset),
            self._pending_payload("query", reset),
        )
        self.drives.append(drive)
        sample = self.model.step(drive)
        if not reset:
            self._record("command", cycle, tick, sample.cmd_ready)
            self._record("query", cycle, tick, sample.query_ready)

    def finished(self):
        """Return whether all input events and both output pipelines have drained."""
        return not self.events and not any(self.pending.values()) and self.model.drained()

    def run(self):
        """Run the bounded scheduler and return vectors with acceptance/cancellation metadata."""
        for cycle in range(self.limit):
            self.step(cycle)
            if self.finished():
                return Schedule(tuple(self.drives), tuple(self.accepted), tuple(self.canceled))
        raise ReplayError("schedule ran out of cycles before all outputs could drain")


def schedule(profile, events, windows=(), tick_origin=0, max_cycles=100_000):
    """Create a replay schedule for ordered source events and optional sink windows."""
    return ReplayScheduler(profile, events, windows, tick_origin, max_cycles).run()


def _strict_object(pairs):
    """Decode a JSON object while rejecting duplicate keys instead of silently replacing them."""
    result = {}
    for key, val in pairs:
        if key in result:
            raise ReplayError(f"duplicate field {key!r}")
        result[key] = val
    return result


def _integer(value, label):
    """Reject booleans and numeric strings before constructing binary fields."""
    if type(value) is not int:
        raise ReplayError(f"{label} must be an integer")
    return value


def _shape(value, allowed, expected, label):
    """Check required and unknown keys for one structured input record."""
    if not isinstance(value, dict):
        raise ReplayError(f"{label} must be an object")
    extras = set(value) - allowed
    if extras:
        raise ReplayError(f"unknown {label} fields {sorted(extras)}")
    missing = expected - set(value)
    if missing:
        raise ReplayError(f"missing {label} fields {sorted(missing)}")


def _load_command(value):
    """Read a normalized source command, accepting symbolic or explicitly numeric opcodes."""
    allowed = {field.name for field in fields(Command)}
    _shape(value, allowed, {"op", "instrument", "venue"}, "command")
    data = dict(value)
    op = data.pop("op")
    if isinstance(op, str):
        try:
            op = Op[op.upper()]
        except KeyError as exc:
            raise ReplayError(f"unknown operation {op!r}") from exc
    else:
        op = _integer(op, "op")
    if op in (Op.REPLACE, Op.WITHDRAW) and "sequence" not in data:
        raise ReplayError("market-data commands require sequence")
    return Command(op, **{key: _integer(val, key) for key, val in data.items()})


def _load_query(value):
    """Read a normalized query with an optional application tag."""
    allowed = {field.name for field in fields(Query)}
    _shape(value, allowed, {"instrument", "max_age"}, "query")
    return Query(**{key: _integer(val, key) for key, val in value.items()})


def decode_event(line, profile):
    """Validate a single normalized JSON line and return its typed event."""
    try:
        parsed = json.loads(line, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"invalid JSON: {exc.msg}") from exc
    _shape(parsed, {"cycle", "command", "query", "reset"}, {"cycle"}, "event")
    command = _load_command(parsed["command"]) if "command" in parsed else None
    query = _load_query(parsed["query"]) if "query" in parsed else None
    event = Event(_integer(parsed["cycle"], "cycle"), command, query, parsed.get("reset", False))
    return event.check(profile)


def read_events(path, profile):
    """Read newline-delimited source events with path and line diagnostics."""
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                yield decode_event(line, profile)
            except ValueError as exc:
                raise ReplayError(f"{path}:{number}: {exc}") from exc


def _command_json(command):
    """Serialize command fields and spell known opcodes by name for readability."""
    obj = {field.name: int(getattr(command, field.name)) for field in fields(command)}
    try:
        obj["op"] = Op(command.op).name.lower()
    except ValueError:
        pass
    return obj


def encode_event(event):
    """Serialize one replay event without adding unused sections."""
    obj = {"cycle": event.cycle}
    if event.reset:
        obj["reset"] = True
    if event.command is not None:
        obj["command"] = _command_json(event.command)
    if event.query is not None:
        obj["query"] = {field.name: getattr(event.query, field.name)
                        for field in fields(event.query)}
    return json.dumps(obj, separators=(",", ":"))


def write_events(path, events, profile):
    """Write normalized events for reproducible simulator inputs."""
    with Path(path).open("w", encoding="utf-8") as stream:
        for event in events:
            event.check(profile)
            stream.write(encode_event(event) + "\n")


def parse_window(text):
    """Read START:STOP:ack|result|both into one half-open sink window."""
    try:
        start_text, stop_text, channel = text.split(":")
        start, stop = int(start_text), int(stop_text)
    except ValueError as exc:
        raise ReplayError("use START:STOP:ack|result|both for sink windows") from exc
    if channel not in ("ack", "result", "both"):
        raise ReplayError("sink channel must be ack, result, or both")
    return SinkWindow(start, stop, channel in ("ack", "both"), channel in ("result", "both"))
