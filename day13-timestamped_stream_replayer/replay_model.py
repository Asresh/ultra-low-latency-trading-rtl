"""Independent transaction-oriented next-state model for the replay engine."""

from collections import deque
from dataclasses import dataclass
from typing import Optional

from replay_program import (
    Beat,
    Command,
    Cycle,
    Offer,
    Operation,
    ReplayError,
    Response,
    Statistics,
    Status,
    U48,
    lanes_from_keep,
    unsigned,
)


@dataclass(frozen=True)
class Observation:
    """Output pins before the input cycle's rising edge."""

    cfg_ready: bool
    response: Optional[Response]
    offer: Optional[Offer]
    active: bool
    paused: bool
    done: bool
    now: int
    statistics: Statistics


@dataclass(frozen=True)
class Transition:
    """Pin image and the transactions accepted on its rising edge."""

    observation: Observation
    request: Optional[Command]
    response: Optional[Response]
    output: Optional[Offer]


def add_tick(count, elapsed):
    """Track virtual time with unsigned 48-bit saturation."""
    return min(U48, count + elapsed)


class ReplayModel:
    """Reference using an image list and a queue of events still to be offered."""

    def __init__(self, depth):
        """Allocate a bounded programming image and initialize all visible state."""
        self.depth = unsigned(depth, (1 << 31) - 1, "depth")
        if self.depth == 0:
            raise ReplayError("device depth must be positive")
        self.reset()

    def reset(self):
        """Clear staged content, outstanding responses, output, and run counters."""
        self.image = []
        self.pending = deque()
        self.response = None
        self.output = None
        self.active = False
        self.paused = False
        self.done = False
        self.now = 0
        self.pass_number = 0
        self.pass_limit = 0
        self.pass_base = 0
        self.repeat_gap = 0
        self.ending = False
        self.statistics = Statistics()

    def observe(self, cycle):
        """Sample the externally visible state before consuming the current pins."""
        return Observation(
            not cycle.reset and (self.response is None or cycle.rsp_ready),
            self.response,
            self.output,
            self.active,
            self.paused,
            self.done,
            self.now,
            self.statistics,
        )

    def _append_result(self, request):
        """Check capacity, timestamp order, framing, and packet tag constraints."""
        if self.active:
            return Status.BUSY
        if len(self.image) >= self.depth:
            return Status.FULL
        previous = self.image[-1] if self.image else None
        if previous and previous.tick > request.tick:
            return Status.BAD_TIME
        try:
            count = lanes_from_keep(request.keep)
        except ReplayError:
            return Status.BAD_KEEP
        if not request.last and count != 8:
            return Status.BAD_KEEP
        if previous and not previous.last and previous.tag != request.tag:
            return Status.BAD_TAG
        return Status.OK

    def _start_result(self, request):
        """Check that the image is idle, complete, nonempty, and repeatable."""
        if self.active:
            return Status.BUSY
        if not self.image:
            return Status.EMPTY
        if not self.image[-1].last:
            return Status.OPEN_PACKET
        if not request.repeats:
            return Status.BAD_REPEAT
        return Status.OK

    def disposition(self, request):
        """Choose the request status without applying any side effect."""
        if request.op == Operation.APPEND:
            return self._append_result(request)
        if request.op == Operation.CLEAR:
            return Status.BUSY if self.active else Status.OK
        if request.op == Operation.START:
            return self._start_result(request)
        if request.op == Operation.PAUSE:
            return Status.OK if self.active and not self.paused and not self.ending else Status.BAD_STATE
        if request.op == Operation.RESUME:
            return Status.OK if self.active and self.paused and not self.ending else Status.BAD_STATE
        if request.op == Operation.STOP:
            return Status.OK if self.active and not self.ending else Status.BAD_STATE
        return Status.BAD_OP

    def _arm(self, request):
        """Start a fresh pass at virtual tick zero and reset per-run statistics."""
        self.active = True
        self.paused = False
        self.ending = False
        self.now = 0
        self.pass_base = 0
        self.pass_number = 1
        self.pass_limit = request.repeats
        self.repeat_gap = request.gap
        self.pending = deque(self.image)
        self.statistics = Statistics()

    def _complete(self):
        """Record one done pulse and make the immutable image programmable again."""
        self.active = False
        self.paused = False
        self.ending = False
        self.done = True

    def _finish_pass(self, timestamp):
        """Schedule the next pass from the edge that accepted the last beat."""
        if self.pass_number == self.pass_limit:
            self._complete()
        else:
            self.pass_number += 1
            self.pass_base = add_tick(timestamp, self.repeat_gap + 1)
            self.pending.extend(self.image)

    def _consume_output(self, selected):
        """Retire the accepted offer and return whether its pass is complete."""
        if selected is None:
            return False
        self.output = None
        end_pass = not self.pending
        self.statistics = self.statistics.receive(selected, end_pass)
        return end_pass

    def _schedule(self, timestamp):
        """Move one due event into an empty output slot, preserving all metadata."""
        if not self.pending or self.output is not None:
            return
        next_event = self.pending[0]
        due = add_tick(self.pass_base, next_event.tick)
        if timestamp >= due:
            self.output = Offer(self.pending.popleft(), due, timestamp, self.pass_number)

    def _run(self, accepted_op, selected):
        """Advance virtual time, prioritize stop, and schedule at most one offer."""
        timestamp = self.now
        stop = accepted_op == Operation.STOP
        going = not self.paused and not self.ending and not stop and accepted_op != Operation.PAUSE
        if accepted_op == Operation.PAUSE:
            self.paused = True
        if accepted_op == Operation.RESUME:
            self.paused = False
        if going:
            self.now = add_tick(self.now, 1)
        end_pass = self._consume_output(selected)
        if self.ending or stop:
            self.ending = True
            if self.output is None:
                self._complete()
        elif end_pass:
            self._finish_pass(timestamp)
        elif going:
            self._schedule(timestamp)

    def step(self, cycle: Cycle):
        """Return pin observations before the edge, then apply all handshakes once."""
        observed = self.observe(cycle)
        request = cycle.cfg if observed.cfg_ready else None
        read_rsp = self.response if cycle.rsp_ready and not cycle.reset else None
        selected = self.output if cycle.ready and not cycle.reset else None
        if cycle.reset:
            self.reset()
            return Transition(observed, None, None, None)

        accepted_op = None
        self.done = False
        if request is not None:
            status = self.disposition(request)
            if status == Status.OK:
                accepted_op = request.op
                if request.op == Operation.APPEND:
                    self.image.append(request.event())
                elif request.op == Operation.CLEAR:
                    self.image.clear()
            self.response = Response(status, len(self.image))
        elif read_rsp is not None:
            self.response = None

        if accepted_op == Operation.START:
            self._arm(request)
        elif self.active:
            self._run(accepted_op, selected)
        return Transition(observed, request, read_rsp, selected)

    def play(self, cycles):
        """Yield independent observations for every input cycle in order."""
        for cycle in cycles:
            yield self.step(cycle)


def load_commands(tape, clear=True):
    """Generate a chronological configuration stream for a checked tape."""
    if clear:
        yield Command(Operation.CLEAR)
    yield from (Command.append(event) for event in tape.events)


def bounded_flush(model, cycles, limit):
    """Append idle slots until the device becomes idle, or fail on no progress."""
    produced = []
    for cycle in cycles:
        produced.append((cycle, model.step(cycle)))
    for _ in range(limit):
        if not model.active and model.response is None:
            return tuple(produced)
        idle = Cycle()
        produced.append((idle, model.step(idle)))
    raise ReplayError(f"model failed to drain within {limit} edges")
