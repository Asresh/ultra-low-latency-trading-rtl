"""Independent packet-list model for the hardware replay fault stage."""

from collections import Counter, deque
from dataclasses import dataclass, replace
from typing import Optional, Tuple

from replay_impairment_recipe import (
    Action, Control, Match, Operation, Status, Verdict, packed, resolve,
)
from replay_program import U32, U64


TOTAL_KEYS = ("frames", "drops", "duplicates", "corruptions", "flip_misses",
              "malformed", "oversizes", "aborted", "emitted", "lost", "stalled")
VERDICT_STATS = {
    Verdict.DROPPED: "drops", Verdict.DUPLICATED: "duplicates", Verdict.CORRUPTED: "corruptions",
    Verdict.FLIP_MISS: "flip_misses", Verdict.FRAMING: "malformed",
    Verdict.CAPACITY: "oversizes", Verdict.TRUNCATED: "aborted",
}


@dataclass(frozen=True)
class Geometry:
    rules: int = 4
    queue: int = 3
    max_beats: int = 4
    run_width: int = 5
    revision_width: int = 3

    @property
    def key_width(self):
        return self.rules.bit_length()


@dataclass(frozen=True)
class Beat:
    data: int = 0
    keep: int = 0
    first: bool = False
    last: bool = False
    tag: int = 0
    pass_id: int = 0
    due: int = 0
    offered: int = 0
    bank: int = 0
    run: int = 0

    def shape(self):
        size = bin(self.keep).count("1")
        if self.keep == (1 << size) - 1:
            return size
        return 0

    @property
    def bytes(self):
        return self.data.to_bytes(8, "little")[:self.shape()]


@dataclass(frozen=True)
class Retirement:
    bank: int
    run: int


@dataclass(frozen=True)
class Reply:
    op: Operation
    status: Status
    revision: int
    slot: int
    seed: int
    word: int


@dataclass(frozen=True)
class ActionRecord:
    scope: Match
    selected: object
    start: int
    end: int
    verdict: Verdict
    length: int
    sequence: int

    @property
    def release(self):
        return min(U64, self.start + self.selected.delay)

    def word(self, g):
        f, p = self.scope, self.selected
        return packed(
            (f.bank, 1), (f.run, g.run_width), (f.tag, 16), (f.pass_id, 16),
            (f.ordinal, 32), (p.revision, g.revision_width), (p.slot, g.key_width),
            (p.action, 2), (self.verdict, 4), (self.start, 64), (self.end, 64),
            (self.release, 64), (self.length, 17),
        )


@dataclass(frozen=True)
class Frame:
    words: Tuple[Beat, ...]
    record: ActionRecord
    action: Action
    copies: tuple

    def offer(self, index, copy, release):
        beat = self.words[index]
        offset = index * 8
        data = int.from_bytes(self.copies[copy][offset:offset+8], "little")
        return Output(replace(beat, data=data), bool(copy), self.record.selected.revision,
                      self.record.scope.ordinal, self.action, release)


@dataclass(frozen=True)
class Output:
    beat: Beat
    copy: bool
    revision: int
    ordinal: int
    action: Action
    release: int


@dataclass(frozen=True)
class Port:
    beat: Optional[Beat] = None
    control: Optional[Control] = None
    abort: Optional[Retirement] = None
    wall: int = 0
    reset: bool = False
    ready: bool = True
    reply_ready: bool = True
    audit_ready: bool = True


@dataclass(frozen=True)
class Pins:
    s_ready: bool
    cfg_ready: bool
    reply: Optional[Reply]
    output: Optional[Output]
    audit: Optional[ActionRecord]
    occupancy: int
    peak: int
    audit_cursor: int
    counts: Tuple[int, ...]


def inc(counter, name):
    counter[name] = min(U32, counter[name] + 1)


class Rules:
    def __init__(self, g):
        self.geometry = g
        self.active = [None] * g.rules
        self.staging = [None] * g.rules
        self.revision = 0
        self.seed = 0

    def decide(self, beat, ordinal):
        scope = Match(beat.run, beat.bank, beat.tag, beat.pass_id, ordinal)
        return scope, resolve(self.active, scope, self.revision, self.seed)

    def apply(self, control):
        op, slot = control.op, control.slot
        selection = self.active if op == Operation.READ and control.active else self.staging
        old = selection[slot] if slot < len(selection) and op in (Operation.READ, Operation.WRITE) else None
        word = old.word(self.geometry.run_width) if old else 0
        result = Status.OK
        if op != Operation.READ and self.revision != control.revision:
            result = Status.STALE
        elif op in (Operation.READ, Operation.WRITE) and slot >= len(selection):
            result = Status.SLOT_RANGE
        elif op == Operation.WRITE:
            if control.rule and not control.rule.valid:
                result = Status.BAD_RULE
            else:
                self.staging[slot] = control.rule
        elif op == Operation.CLEAR:
            self.staging[:] = [None] * len(self.staging)
        elif op == Operation.COMMIT:
            if self.revision == (1 << self.geometry.revision_width) - 1:
                result = Status.EXHAUSTED
            else:
                self.active, self.staging = self.staging, self.active
                self.revision += 1
                self.seed = control.seed
        return Reply(op, result, self.revision, slot, self.seed, word)


class Model:
    """Use complete byte strings, a deque and explicit copy lifetimes."""

    def __init__(self, geometry):
        self.geometry = geometry
        self.reset()

    def reset(self):
        self.response = None
        self.audit = None
        self.cursor = 0
        self.rules = Rules(self.geometry)
        self.queue = deque()
        self.replay = None
        self.replay_index = 0
        self.copy = 0
        self.copy_after = 0
        self.stats = Counter()
        self.peak = 0
        self.run_pair = None
        self.ordinal = 0
        self.incoming = []
        self.selection = None
        self.frame_scope = None
        self.arrival = 0
        self.length = 0
        self.fault = None

    def first_context(self, beat):
        identity = (beat.bank, beat.run)
        index = self.ordinal if self.run_pair == identity else 0
        return self.rules.decide(beat, index)

    def aborts(self, retirement):
        return bool(self.incoming and retirement and
                    (retirement.bank, retirement.run) == (self.frame_scope.bank, self.frame_scope.run))

    def observe(self, port):
        offer = self.replay if not port.reset else None
        frees = bool(offer and port.ready and offer.beat.last and (
            offer.action != Action.DUPLICATE or offer.copy
        ))
        selected = self.first_context(port.beat or Beat())[1]
        return Pins(
            not port.reset and not self.aborts(port.abort) and
            (bool(self.incoming) or len(self.queue) < self.geometry.queue or frees or
             selected.action == Action.DROP),
            not port.reset and (self.response is None or port.reply_ready),
            self.response if not port.reset else None,
            offer,
            self.audit if not port.reset else None,
            len(self.queue), self.peak, self.cursor,
            tuple(self.stats[key] for key in TOTAL_KEYS),
        )

    def capture(self, beat, scope, policy, wall):
        if not self.incoming:
            self.run_pair = (beat.bank, beat.run)
            self.ordinal = min(U32, scope.ordinal + 1)
            self.selection = policy
            self.frame_scope = scope
            self.arrival = wall
            self.length = 0
            self.fault = None
        first = self.incoming[0] if self.incoming else beat
        size = beat.shape()
        bad = (
            beat.first != (not self.incoming) or size == 0 or
            (not beat.last and size != 8) or
            (beat.run, beat.bank, beat.tag, beat.pass_id) != (first.run, first.bank, first.tag, first.pass_id)
        )
        self.incoming.append(beat)
        self.length = min(0x1FFFF, self.length + size)
        if self.fault is None:
            if bad:
                self.fault = Verdict.FRAMING
            elif len(self.incoming) > self.geometry.max_beats:
                self.fault = Verdict.CAPACITY
        if beat.last:
            self.close_capture(wall, self.fault)

    def close_capture(self, wall, fault):
        if fault is not None:
            verdict, copies = fault, ()
        else:
            payload = b"".join(beat.bytes for beat in self.incoming)
            verdict, copies = self.selection.mutate(payload)
        record = ActionRecord(
            self.frame_scope, self.selection, self.arrival, wall, verdict, self.length, self.cursor
        )
        if copies:
            action = Action.PASS if verdict == Verdict.FLIP_MISS else self.selection.action
            self.queue.append(Frame(tuple(self.incoming), record, action, copies))
        if verdict in VERDICT_STATS:
            inc(self.stats, VERDICT_STATS[verdict])
        inc(self.stats, "frames")
        if self.audit is None and self.cursor < U64:
            self.audit = record
        else:
            inc(self.stats, "lost")
        self.cursor = min(U64, self.cursor + 1)
        self.incoming.clear()

    def step(self, port):
        before = self.observe(port)
        if port.reset:
            self.reset()
            return before

        if port.audit_ready:
            self.audit = None
        if port.reply_ready:
            self.response = None
        incoming_rule = self.first_context(port.beat) if port.beat and before.s_ready else None
        active_frame = self.queue[0] if self.queue else None
        release = (self.copy_after if self.copy else active_frame.record.release) if active_frame else 0
        if before.output:
            if not port.ready:
                inc(self.stats, "stalled")
            elif before.output.beat.last:
                inc(self.stats, "emitted")
                self.replay = None
                self.replay_index = 0
                if active_frame.action == Action.DUPLICATE and self.copy == 0:
                    self.copy = 1
                    self.copy_after = min(U64, port.wall + active_frame.record.selected.copy_gap + 1)
                else:
                    self.queue.popleft()
                    self.copy = 0
            else:
                self.replay_index += 1
                self.replay = active_frame.offer(self.replay_index, self.copy, release)
        elif active_frame and port.wall >= release:
            self.replay = active_frame.offer(0, self.copy, release)

        if self.aborts(port.abort):
            self.close_capture(port.wall, Verdict.TRUNCATED)
        elif incoming_rule:
            self.capture(port.beat, *incoming_rule, port.wall)
        if port.control and before.cfg_ready:
            self.response = self.rules.apply(port.control)
        self.peak = max(self.peak, len(self.queue))
        return before
