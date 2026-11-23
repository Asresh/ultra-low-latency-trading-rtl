"""Compile a readable command script into deterministic replay pin stimuli."""

from dataclasses import dataclass
from pathlib import Path
import shlex

from replay_format import parse_integer, parse_last, parse_payload, read_tape
from replay_model import ReplayModel, load_commands
from replay_program import Beat, Command, Cycle, Operation, ReplayError, Status, U16, U32, U64

ACTIONS = frozenset(("clear", "start", "pause", "resume", "stop", "append", "raw"))
DIRECTIVES = frozenset((
    "reset", "sink", "responses", "idle", "await_offer", "await_done", "load",
))


@dataclass(frozen=True)
class Instruction:
    """One source statement, including its expected configuration result."""

    line: int
    verb: str
    arguments: tuple
    status: object = None

    @property
    def label(self):
        """Return a concise prefix for a source-level diagnostic."""
        return f"line {self.line} ({self.verb})"


def parse_expected(words):
    """Separate an optional '-> STATUS' suffix from its command arguments."""
    if "->" not in words:
        return tuple(words), None
    if len(words) < 2 or words[-2] != "->" or words.count("->") != 1:
        raise ReplayError("expected a single '-> STATUS' suffix")
    try:
        expected = Status[words[-1].upper()]
    except KeyError as exc:
        raise ReplayError(f"unknown response status {words[-1]!r}") from exc
    return tuple(words[:-2]), expected


def parse_script(source):
    """Read whitespace-separated statements with comments and optional quoted paths."""
    for number, text in enumerate(source, 1):
        try:
            tokens = shlex.split(text, comments=True)
            if not tokens:
                continue
            verb = tokens[0].lower()
            if verb not in ACTIONS | DIRECTIVES:
                raise ReplayError(f"unknown statement {tokens[0]!r}")
            arguments, expected = parse_expected(tokens[1:])
            if expected is not None and verb not in ACTIONS:
                raise ReplayError("response expectation is only supported for configuration commands")
            yield Instruction(number, verb, arguments, expected)
        except ValueError as exc:
            raise ReplayError(f"script line {number}: {exc}") from exc


def arity(statement, low, high):
    """Require the argument count to fit one documented command signature."""
    count = len(statement.arguments)
    if not low <= count <= high:
        width = str(low) if low == high else f"{low}–{high}"
        raise ReplayError(f"{statement.verb} needs {width} arguments, got {count}")


def parse_raw(statement):
    """Preserve intentionally malformed bus fields for negative replay tests."""
    arity(statement, 8, 8)
    op, tick, data, keep, last, tag, repeats, gap = statement.arguments
    return Command(
        parse_integer(op, 7, "opcode"),
        parse_integer(tick, U32, "tick"),
        parse_integer(data, U64, "data"),
        parse_integer(keep, 255, "keep"),
        parse_last(last),
        parse_integer(tag, U16, "tag"),
        parse_integer(repeats, U16, "repeat count"),
        parse_integer(gap, U32, "gap"),
    )


def request(statement):
    """Convert a parsed command into exact packed configuration pin fields."""
    verb, arguments = statement.verb, statement.arguments
    if verb == "raw":
        return parse_raw(statement)
    if verb == "append":
        arity(statement, 4, 4)
        tick, tag, last, payload = arguments
        beat = Beat.from_payload(
            parse_integer(tick, U32, "tick"),
            parse_payload(payload, 8),
            parse_integer(tag, U16, "tag"),
            parse_last(last),
        )
        return Command.append(beat)
    if verb == "start":
        arity(statement, 0, 2)
        repeats = parse_integer(arguments[0], U16, "repeat count") if arguments else 1
        gap = parse_integer(arguments[1], U32, "gap") if len(arguments) == 2 else 0
        return Command.start(repeats, gap)
    if verb in ACTIONS:
        arity(statement, 0, 0)
        return Command(Operation[verb.upper()])
    raise ReplayError(f"not a configuration command: {verb}")


@dataclass(frozen=True)
class Applied:
    """One accepted configuration request and its predicted response."""

    line: int
    cycle: int
    operation: int
    status: Status
    length: int

    def record(self):
        """Expose the accepted request in a compact JSON-compatible object."""
        return {
            "line": self.line,
            "cycle": self.cycle,
            "op": self.operation,
            "status": self.status.name,
            "tape_length": self.length,
        }


class ScriptCompiler:
    """Drive one command at a time with configurable stalls and explicit bounds."""

    def __init__(self, depth, root, *, max_cycles=100000, request_wait=256):
        """Create model state, set path resolution, and enforce positive watchdog limits."""
        self.model = ReplayModel(depth)
        self.root = Path(root)
        if min(max_cycles, request_wait) <= 0:
            raise ReplayError("script watchdog limits must be positive")
        self.max_cycles = max_cycles
        self.request_wait = request_wait
        self.stream_ready = True
        self.response_ready = True
        self.cycles = []
        self.applied = []
        self.edge(reset=True)

    def edge(self, command=None, *, reset=False):
        """Append a single complete physical edge and update predictor state."""
        if len(self.cycles) >= self.max_cycles:
            raise ReplayError(f"script exceeds the {self.max_cycles}-edge watchdog")
        cycle = Cycle(reset, command, self.response_ready, self.stream_ready)
        self.cycles.append(cycle)
        return self.model.step(cycle)

    def send(self, command, expected, line):
        """Hold a request across backpressure and assert any promised result."""
        for _ in range(self.request_wait):
            edge = len(self.cycles)
            result = self.edge(command)
            if result.request is not None:
                response = self.model.response
                self.applied.append(Applied(line, edge, command.op, response.status, response.length))
                if expected is not None and response.status != expected:
                    raise ReplayError(f"expected {expected.name}, got {response.status.name}")
                return
        raise ReplayError("configuration request stayed blocked by the response sink")

    def wait(self, goal, limit):
        """Advance idle cycles until an observable output or an idle engine."""
        if goal == "await_done":
            for _ in range(limit+1):
                if not self.model.active and self.model.response is None:
                    return
                if _ != limit:
                    self.edge()
        elif goal == "await_offer":
            for _ in range(limit+1):
                if self.model.output is not None:
                    return
                if _ != limit:
                    self.edge()
        raise ReplayError(f"{goal} did not finish within {limit} idle edges")

    def load(self, filename, line):
        """Clear and preload a checked CSV or binary image relative to the script."""
        path = self.root / filename
        tape = read_tape(path, self.model.depth)
        for command in load_commands(tape):
            self.send(command, Status.OK, line)

    def directive(self, statement):
        """Apply a runtime control and preserve its scope until changed."""
        verb, arguments = statement.verb, statement.arguments
        if verb == "reset":
            arity(statement, 0, 0)
            self.edge(reset=True)
        elif verb in ("sink", "responses"):
            arity(statement, 1, 1)
            if arguments[0] not in ("on", "off"):
                raise ReplayError(f"{verb} accepts on or off")
            flag = arguments[0] == "on"
            if verb == "sink":
                self.stream_ready = flag
            else:
                self.response_ready = flag
        elif verb == "idle":
            arity(statement, 1, 1)
            count = parse_integer(arguments[0], U32, "idle cycles")
            for _ in range(count):
                self.edge()
        elif verb in ("await_offer", "await_done"):
            arity(statement, 1, 1)
            self.wait(verb, parse_integer(arguments[0], U32, "watchdog cycles"))
        elif verb == "load":
            arity(statement, 1, 1)
            self.load(arguments[0], statement.line)

    def compile(self, instructions, *, settle=True):
        """Apply each instruction, canonicalize source diagnostics, and finalize stimuli."""
        for statement in instructions:
            try:
                if statement.verb in ACTIONS:
                    self.send(request(statement), statement.status, statement.line)
                else:
                    self.directive(statement)
            except (OSError, ValueError) as exc:
                raise ReplayError(f"{statement.label}: {exc}") from exc
        if settle:
            self.edge()
        return tuple(self.cycles)


def compile_script(path, depth, *, max_cycles=100000, request_wait=256):
    """Read a script once and return its checked pin tape and command audit."""
    path = Path(path)
    compiler = ScriptCompiler(depth, path.parent, max_cycles=max_cycles, request_wait=request_wait)
    with path.open(encoding="utf-8") as source:
        cycles = compiler.compile(parse_script(source))
    return cycles, tuple(compiler.applied)
