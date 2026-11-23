"""Independent untimed quote book and queue-based cycle reference."""


from __future__ import annotations
from collections import deque
from dataclasses import dataclass

from quote_schema import (
    Ack, Code, Drive, Op, Profile, Result, Sample, Side, State, Winner,
    distance, serial_is_forward,
)


@dataclass(frozen=True)
class Quote:
    bid_price: int
    bid_qty: int
    ask_price: int
    ask_qty: int
    sequence: int
    tick: int

    @classmethod
    def from_command(cls, command, tick):
        """Copy a valid update into immutable per-venue storage."""
        return cls(
            command.bid_price, command.bid_qty, command.ask_price,
            command.ask_qty, command.sequence, tick,
        )

    def price_size(self, side):
        """Select the price and quantity tuple for the requested side."""
        if side == Side.BID:
            return self.bid_price, self.bid_qty
        return self.ask_price, self.ask_qty

    def winner(self, side, venue, age):
        """Return a complete candidate while marking absent sides invalid."""
        price, qty = self.price_size(side)
        if not qty:
            return Winner()
        return Winner(1, venue, price, qty, age, self.sequence)


@dataclass(frozen=True)
class VenueSnapshot:
    instrument: int
    venue: int
    enabled: bool
    last_sequence: int | None
    quote: Quote | None


def price_error(command):
    """Recognize invalid side shapes and a locally crossed replacement."""
    sides = (
        (command.bid_price, command.bid_qty),
        (command.ask_price, command.ask_qty),
    )
    if not any(qty > 0 for _, qty in sides):
        return True
    if any(bool(price) != bool(qty) for price, qty in sides):
        return True
    return all(qty > 0 for _, qty in sides) and command.bid_price > command.ask_price


def market_state(bid, ask):
    """Classify the consolidated relationship between two optional winners."""
    if not bid.valid:
        return State.ASK_ONLY if ask.valid else State.EMPTY
    if not ask.valid:
        return State.BID_ONLY
    if bid.price == ask.price:
        return State.LOCKED
    return State.OPEN if bid.price < ask.price else State.CROSSED


class QuoteBook:
    def __init__(self, profile=Profile()):
        """Initialize an enabled, empty logical book using dictionaries for state."""
        self.profile = profile
        self.reset()

    def reset(self):
        """Clear the book, disabled overrides, and sequence fences."""
        self.quotes = {}
        self.last_sequences = {}
        self.disabled = set()

    def status(self, instrument, venue):
        """Return state for a valid key, or reject an address outside the book."""
        if not 0 <= instrument < self.profile.instruments:
            raise IndexError(f"instrument {instrument} is out of range")
        if not 0 <= venue < self.profile.venues:
            raise IndexError(f"venue {venue} is out of range")
        key = (instrument, venue)
        return VenueSnapshot(
            instrument, venue, key not in self.disabled,
            self.last_sequences.get(key), self.quotes.get(key),
        )

    def snapshot(self):
        """Expose an immutable book image ordered by instrument and venue."""
        return tuple(
            self.status(instrument, venue)
            for instrument in range(self.profile.instruments)
            for venue in range(self.profile.venues)
        )

    def _update_allowed(self, key, sequence):
        """Allow an unseen serial or a strictly forward modular increment."""
        last = self.last_sequences.get(key)
        if last is None:
            return True
        return serial_is_forward(sequence, last, self.profile.seq_w)

    def _market_data(self, key, command, tick):
        """Validate a market operation before mutating a row and sequence fence."""
        if key in self.disabled:
            return Code.DISABLED
        if not self._update_allowed(key, command.sequence):
            return Code.BAD_SEQ
        if command.op == Op.REPLACE:
            if price_error(command):
                return Code.BAD_PRICE
            self.quotes[key] = Quote.from_command(command, tick)
        else:
            self.quotes.pop(key, None)
        self.last_sequences[key] = command.sequence
        return Code.OK

    def apply(self, command, tick):
        """Apply one accepted wire command and return its tagged acknowledgement."""
        command.check(self.profile)
        Drive(tick=tick).check(self.profile)
        instrument = command.instrument
        venue = command.venue
        key = (instrument, venue)
        if instrument >= self.profile.instruments:
            return Ack(Code.BAD_INST, command.tag)
        if venue >= self.profile.venues:
            return Ack(Code.BAD_VENUE, command.tag)
        if command.op in (Op.REPLACE, Op.WITHDRAW):
            return Ack(self._market_data(key, command, tick), command.tag)
        if command.op == Op.ENABLE:
            self.disabled.discard(key)
        elif command.op == Op.DISABLE:
            self.disabled.add(key)
            self.quotes.pop(key, None)
        else:
            return Ack(Code.BAD_OP, command.tag)
        return Ack(Code.OK, command.tag)

    def _fresh_candidates(self, query, tick):
        """Collect active and stale masks and both sides for one instrument."""
        active = 0
        stale = 0
        bids = []
        asks = []
        for venue in range(self.profile.venues):
            key = (query.instrument, venue)
            row = self.quotes.get(key)
            if row is None or key in self.disabled:
                continue
            age = distance(tick, row.tick, self.profile.time_w)
            bit = 1 << venue
            if age >= self.profile.time_half or age > query.max_age:
                stale |= bit
                continue
            active |= bit
            if row.bid_qty:
                bids.append(row.winner(Side.BID, venue, age))
            if row.ask_qty:
                asks.append(row.winner(Side.ASK, venue, age))
        return active, stale, bids, asks

    def query(self, request, tick):
        """Return a frozen consolidated snapshot without changing the underlying book."""
        request.check(self.profile)
        Drive(tick=tick).check(self.profile)
        if request.instrument >= self.profile.instruments:
            return Result(tag=request.tag, tick=tick, code=Code.BAD_INST)
        if request.max_age >= self.profile.time_half:
            return Result(tag=request.tag, tick=tick, code=Code.BAD_AGE)
        active, stale, bids, asks = self._fresh_candidates(request, tick)
        bid = min(bids, key=lambda winner: (-winner.price, winner.venue), default=Winner())
        ask = min(asks, key=lambda winner: (winner.price, winner.venue), default=Winner())
        spread = ask.price - bid.price if bid.valid and ask.valid else 0
        return Result(
            request.tag, Code.OK, market_state(bid, ask), tick,
            active, stale, bid, ask, spread,
        )

    def check_invariants(self):
        """Assert book state, enabled flags, and serial fences describe the same rows."""
        for (instrument, venue), row in self.quotes.items():
            assert (instrument, venue) not in self.disabled
            assert 0 <= instrument < self.profile.instruments
            assert 0 <= venue < self.profile.venues
            assert self.last_sequences[instrument, venue] == row.sequence
            assert any((row.bid_qty, row.ask_qty))
            assert bool(row.bid_price) == bool(row.bid_qty)
            assert bool(row.ask_price) == bool(row.ask_qty)
            if row.bid_qty and row.ask_qty:
                assert row.bid_price <= row.ask_price


class Machine:
    def __init__(self, profile=Profile()):
        """Set up an independent two-slot deadline queue and a one-slot acknowledgement queue."""
        self.profile = profile
        self.book = QuoteBook(profile)
        self.cycle = 0
        self.responses = deque()
        self.acks = deque()

    def reset(self):
        """Flush every pending transfer and clear the book at a reset edge."""
        self.book.reset()
        self.responses.clear()
        self.acks.clear()

    def peek(self, drive):
        """Observe before an edge, accounting for available capacity and simultaneous pops."""
        if drive.reset:
            return Sample(0, 0, None, None)
        ack = self.acks[0] if self.acks else None
        visible_result = None
        if self.responses and self.responses[0][0] <= self.cycle:
            visible_result = self.responses[0][1]
        ack_pop = ack is not None and drive.ack_ready
        result_pop = visible_result is not None and drive.result_ready
        return Sample(
            int(not self.acks or ack_pop),
            int(len(self.responses) < 2 or result_pop),
            ack,
            visible_result,
        )

    def step(self, drive):
        """Observe the current bus, then apply pops, query, and command in pre-state order."""
        drive.check(self.profile)
        current = self.peek(drive)
        if drive.reset:
            self.reset()
        else:
            if current.transferred_ack(drive) is not None:
                self.acks.popleft()
            if current.transferred_result(drive) is not None:
                self.responses.popleft()
            if current.accepted_query(drive):
                result = self.book.query(drive.query, drive.tick)
                self.responses.append((self.cycle + 2, result))
            if current.accepted_command(drive):
                self.acks.append(self.book.apply(drive.command, drive.tick))
        self.cycle += 1
        return current

    def drained(self):
        """Return whether no retained or pending output can still transfer."""
        return not self.acks and not self.responses
