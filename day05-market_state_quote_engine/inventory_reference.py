"""Transaction and publication model for the inventory-aware quote planner."""
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Optional


class Op(IntEnum):
    INSTALL = 0
    FILL = 1
    INSPECT = 2
    FENCE = 3


class Status(IntEnum):
    OK = 0
    BAD_INSTRUMENT = 1
    STALE = 2
    BAD_POLICY = 3
    EXHAUSTED = 4
    FENCED = 5
    DUPLICATE = 6
    CONFLICT = 7
    OLD = 8
    GAP = 9
    ZERO_FILL = 10
    OVERFLOW = 11


class Reason(IntEnum):
    BOTH = 0
    ONE = 1
    OFF = 2
    FENCED = 3
    QUOTE_SHAPE = 4
    NO_CAPACITY = 5


@dataclass(frozen=True)
class Shape:
    instruments: int
    levels: int
    pos: int = 8
    qty: int = 6
    reserve: int = 9
    epoch: int = 3
    seq: int = 4
    rev: int = 3
    tag: int = 10

    @property
    def instrument(self):
        return self.instruments.bit_length()

    @property
    def price(self):
        return self.levels.bit_length()


@dataclass(frozen=True)
class Book:
    armed: bool = False
    revision: int = 0
    next_fill: int = 0
    position: int = 0
    floor: int = 0
    ceiling: int = 0
    bid_cap: int = 0
    ask_cap: int = 0
    trigger: int = 0
    skew: int = 0
    last: Optional[tuple] = None
    fills: int = 0
    faults: int = 0


@dataclass(frozen=True)
class Command:
    op: Op
    instrument: int
    tag: int
    revision: int = 0
    sequence: int = 0
    sell: bool = False
    quantity: int = 0
    position: int = 0
    floor: int = -10
    ceiling: int = 10
    bid_cap: int = 8
    ask_cap: int = 7
    trigger: int = 5
    skew: int = 3


@dataclass(frozen=True)
class Quote:
    instrument: int
    active: bool
    current: bool
    epoch: int
    bid: int
    ask: int
    size: int


@dataclass(frozen=True)
class Stored:
    active: bool = False
    bad: bool = False
    epoch: int = 0
    bid: int = 0
    ask: int = 0
    size: int = 0


@dataclass(frozen=True)
class Plan:
    instrument: int
    epoch: int
    revision: int
    bid: int
    ask: int
    bid_size: int
    ask_size: int
    position: int
    reason: Reason


def settle(shape, book, command):
    """Return status, next state and whether this edge fences an old offer."""
    if command.op != Op.INSPECT and book.revision != command.revision:
        return Status.STALE, book, False
    if command.op == Op.INSPECT:
        return Status.OK, book, False
    if command.op == Op.FENCE:
        return Status.OK, replace(book, armed=False), True
    if command.op == Op.INSTALL:
        if book.revision == (1 << shape.rev)-1:
            return Status.EXHAUSTED, book, False
        if not command.floor <= 0 <= command.ceiling:
            return Status.BAD_POLICY, book, False
        return Status.OK, Book(
            True, book.revision+1, command.sequence, command.position, command.floor,
            command.ceiling, command.bid_cap, command.ask_cap, command.trigger, command.skew,
            None, book.fills, book.faults,
        ), True

    movement = -command.quantity if command.sell else command.quantity
    update = book.position + movement
    identity = (command.sequence, command.sell, command.quantity)
    bound = 1 << (shape.pos-1)
    if book.last and book.last[0] == command.sequence:
        if book.last == identity:
            return Status.DUPLICATE, book, False
        error = Status.CONFLICT
    elif not book.armed:
        return Status.FENCED, book, False
    elif command.sequence < book.next_fill:
        return Status.OLD, book, False
    elif command.sequence > book.next_fill:
        error = Status.GAP
    elif not movement:
        error = Status.ZERO_FILL
    elif not -bound <= update < bound:
        error = Status.OVERFLOW
    else:
        last_value = (1 << shape.seq)-1
        return Status.OK, replace(
            book, armed=command.sequence < last_value, position=update, last=identity,
            next_fill=min(command.sequence+1, last_value), fills=min(book.fills+1, 0xFFFFFFFF),
        ), True
    return error, replace(book, armed=False, faults=min(book.faults+1, 0xFFFFFFFF)), True


def candidate(shape, book, found, instrument, eligible, epoch, bid_reserved, ask_reserved):
    """Compute each side from remaining worst-case inventory room."""
    bid_size = ask_size = bid = ask = 0
    wanted = found.epoch == epoch and eligible
    if wanted and found.bad:
        code = Reason.QUOTE_SHAPE
    elif not wanted or not found.active:
        code = Reason.OFF
    elif not book.armed:
        code = Reason.FENCED
    else:
        bid_size = max(0, min(book.ceiling-book.position-bid_reserved, found.size, book.bid_cap))
        ask_size = max(0, min(book.position-book.floor-ask_reserved, found.size, book.ask_cap))
        shade_bid = book.skew if book.trigger and book.position >= book.trigger else 0
        shade_ask = book.skew if book.trigger and book.position <= -book.trigger else 0
        bid = max(0, found.bid-shade_bid) if bid_size else 0
        ask = min(shape.levels-1, found.ask+shade_ask) if ask_size else 0
        code = (Reason.BOTH if bid_size and ask_size else
                Reason.ONE if bid_size or ask_size else Reason.NO_CAPACITY)
    return Plan(instrument, epoch, book.revision, bid, ask, bid_size, ask_size, book.position, code)


class Manager:
    def __init__(self, shape):
        self.shape = shape
        self.reset()

    def reset(self):
        n = self.shape.instruments
        self.books = [Book() for _ in range(n)]
        self.intents = [None] * n
        self.last_delivered = [None] * n
        self.reply = None
        self.held = None
        self.cursor = 0

    def step(self, reset, cmd, allow_reply, quote, allow_offer, eligible, epochs, buy, sell):
        """Return pre-edge signals, then apply command, quote, and transfer edges."""
        shape = self.shape
        n = shape.instruments
        if reset:
            self.reset()
            return False, None, False, None, False, [0]*n, [0]*n

        allow_command = self.reply is None or allow_reply
        reply, next_book, changed_bank = None, None, None
        if cmd is not None and allow_command:
            if cmd.instrument >= n:
                outcome, observed = Status.BAD_INSTRUMENT, Book()
            else:
                outcome, observed, changed = settle(shape, self.books[cmd.instrument], cmd)
                if changed:
                    changed_bank = cmd.instrument
                next_book = observed
            reply = (cmd.op, cmd.instrument, cmd.tag, outcome, observed.armed, observed.revision,
                     observed.next_fill, observed.position, observed.fills, observed.faults)

        wanted = []
        for i, book in enumerate(self.books):
            wanted.append(candidate(shape, book, self.intents[i] or Stored(), i,
                                    eligible[i], epochs[i], buy[i], sell[i]))
        snapshot, replaced = None, False
        if quote and quote.current and quote.instrument < n and quote.epoch == epochs[quote.instrument]:
            legal = quote.bid < quote.ask < shape.levels and quote.size > 0
            snapshot = Stored(quote.active and legal, quote.active and not legal,
                              quote.epoch, quote.bid, quote.ask, quote.size)
            replaced = snapshot != (self.intents[quote.instrument] or Stored())

        current = bool(self.held and self.held == wanted[self.held.instrument] and
                       changed_bank != self.held.instrument and
                       not (snapshot is not None and quote.instrument == self.held.instrument and replaced))
        fenced = {changed_bank}
        if replaced:
            fenced.add(quote.instrument)
        signals = (allow_command, self.reply, True, self.held, current,
                   [p.bid_size if p.instrument not in fenced else 0 for p in wanted],
                   [p.ask_size if p.instrument not in fenced else 0 for p in wanted])

        if self.held is not None and allow_offer:
            if current:
                self.last_delivered[self.held.instrument] = self.held
            self.held = None
        elif self.held is None:
            for step in range(n):
                i = (self.cursor + step) % n
                if self.intents[i] is not None and self.last_delivered[i] != wanted[i]:
                    self.held = wanted[i]
                    self.cursor = (i+1) % n
                    break

        if allow_command:
            self.reply = reply
            if next_book is not None:
                self.books[cmd.instrument] = next_book
        for i, captured in enumerate(self.intents):
            if captured is not None and (not eligible[i] or captured.epoch != epochs[i]):
                self.intents[i] = replace(captured, active=False, bad=False)
        if snapshot is not None:
            if not eligible[quote.instrument]:
                snapshot = replace(snapshot, active=False, bad=False)
            self.intents[quote.instrument] = snapshot
        return signals
