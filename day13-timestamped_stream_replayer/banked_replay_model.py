"""Cycle reference for an explicitly scheduled two-bank replay manager."""

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

from replay_model import ReplayModel
from replay_program import Command, Cycle, Offer, Operation, Statistics, Status, U32, U64


CANCEL = 6
INSPECT = 7
STALE_RUN = 11
ID_EXHAUSTED = 12


class State(IntEnum):
    WRITABLE = 0
    ARMED = 1
    PLAYING = 2


@dataclass(frozen=True)
class Request:
    command: Command
    bank: int = 0
    run_id: int = 0
    release: int = 0


@dataclass(frozen=True)
class Port:
    request: Optional[Request] = None
    reset: bool = False
    ready: bool = True
    reply_ready: bool = True


@dataclass(frozen=True)
class Run:
    bank: int
    run_id: int
    release: int
    command: Command


@dataclass(frozen=True)
class Reply:
    status: int
    op: int
    bank: int
    run_id: int
    state: State
    length: int
    image_open: bool


@dataclass(frozen=True)
class Retirement:
    bank: int
    run_id: int
    stopped: bool
    truncated: bool
    tick: int
    stats: Statistics


@dataclass(frozen=True)
class Pins:
    command_ready: bool
    reply: Optional[Reply]
    offer: Optional[Offer]
    active: bool
    bank: int
    run_id: int
    paused: bool
    now: int
    wall: int
    started: bool
    start_tick: int
    retired: bool
    retirement: Optional[Retirement]
    counters: tuple


class BankedReplay:
    """Compose the existing tape model with a separate reservation lifecycle."""

    def __init__(self, depth, run_width):
        self.max_id = (1 << run_width) - 1
        self.banks = (ReplayModel(depth), ReplayModel(depth))
        self.reset()

    def reset(self):
        for engine in self.banks:
            engine.reset()
        self.owner = None
        self.pending = None
        self.last_id = 0
        self.reply = None
        self.delayed = None
        self.open_packet = False
        self.stopping = False
        self.wall = 0
        self.started = False
        self.start_tick = 0
        self.retired = False
        self.retirement = None
        self.counters = [0] * 4

    def count(self, index):
        self.counters[index] = min(U32, self.counters[index] + 1)

    def snapshot(self, bank):
        if self.owner and self.owner.bank == bank:
            return State.PLAYING, self.owner.run_id
        if self.pending and self.pending.bank == bank:
            return State.ARMED, self.pending.run_id
        return State.WRITABLE, 0

    def observe(self, port):
        owner = self.owner if not port.reset else None
        engine = self.banks[owner.bank] if owner else None
        return Pins(
            not port.reset and self.delayed is None and (self.reply is None or port.reply_ready),
            self.reply,
            engine.output if engine else None,
            owner is not None,
            owner.bank if owner else 0,
            owner.run_id if owner else 0,
            bool(engine and engine.paused),
            engine.now if engine else 0,
            self.wall, self.started, self.start_tick, self.retired, self.retirement,
            tuple(self.counters),
        )

    def classify(self, request, state, token):
        command = request.command
        engine = self.banks[request.bank]
        op = command.op
        if op in (Operation.APPEND, Operation.CLEAR):
            return (True, Status.OK) if state == State.WRITABLE else (False, Status.BUSY)
        if op == Operation.START:
            if self.pending or state == State.PLAYING:
                return False, Status.BUSY
            status = engine.disposition(command)
            if status == Status.OK and self.last_id == self.max_id:
                status = ID_EXHAUSTED
            return False, status
        if op in (Operation.PAUSE, Operation.RESUME, Operation.STOP, CANCEL):
            required = State.ARMED if op == CANCEL else State.PLAYING
            if state != required:
                return False, Status.BAD_STATE
            if token != request.run_id:
                return False, STALE_RUN
            return op != CANCEL, Status.OK
        return False, Status.OK  # INSPECT

    def local_reply(self, request, state, token, status):
        image = self.banks[request.bank].image
        command = request.command
        if status == Status.OK and command.op == Operation.START:
            self.last_id += 1
            self.pending = Run(request.bank, self.last_id, request.release, command)
            state, token = State.ARMED, self.last_id
        elif status == Status.OK and command.op == CANCEL:
            self.pending = None
            self.count(2)
            state = State.WRITABLE
        return Reply(status, command.op, request.bank, token, state,
                     len(image), bool(image and not image[-1].last))

    def retire(self, owner):
        engine = self.banks[owner.bank]
        self.retirement = Retirement(
            owner.bank, owner.run_id, self.stopping, self.open_packet, self.wall,
            engine.statistics,
        )
        self.retired = True
        self.open_packet = False
        self.stopping = False
        self.owner = None
        self.count(1)

    def advance(self, port):
        pins = self.observe(port)
        if port.reset:
            self.reset()
            return pins
        due = self.pending if self.pending and self.pending.release <= self.wall else None
        old_owner = self.owner
        retiring = bool(old_owner and self.banks[old_owner.bank].done)
        self.started = False
        self.retired = False
        if port.reply_ready:
            self.reply = None
        if self.delayed:
            request, state, token = self.delayed
            engine = self.banks[request.bank]
            response = engine.response
            image = engine.image
            self.reply = Reply(response.status, request.command.op, request.bank, token, state,
                               response.length, bool(image and not image[-1].last))
            self.delayed = None
        commands = [None, None]
        if port.request and pins.command_ready:
            request = port.request
            state, token = self.snapshot(request.bank)
            forwarded, status = self.classify(request, state, token)
            if forwarded:
                commands[request.bank] = request.command
                self.delayed = request, state, token
                if request.command.op == Operation.STOP:
                    self.stopping |= self.banks[request.bank].disposition(request.command) == Status.OK
            else:
                self.reply = self.local_reply(request, state, token, status)
        if pins.offer and port.ready:
            self.open_packet = not pins.offer.event.last
        if retiring:
            self.retire(old_owner)
        if due is not None and due == self.pending:
            if self.owner:
                self.count(3)
            else:
                if commands[due.bank] is not None:
                    raise ValueError("dispatch conflicted with a host command")
                commands[due.bank] = due.command
                self.pending = None
                self.owner = due
                self.started = True
                self.start_tick = self.wall
                self.count(0)
        for index, engine in enumerate(self.banks):
            engine.step(Cycle(cfg=commands[index], ready=port.ready and bool(
                old_owner and old_owner.bank == index
            )))
        self.wall = min(U64, self.wall + 1)
        return pins
