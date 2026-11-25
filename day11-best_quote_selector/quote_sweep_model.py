"""Reference sweep economics derived from an immutable quote and policy view."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from quote_model import QuoteBook
from quote_schema import Profile, Side, distance, mask


class SweepCode(IntEnum):
    FILLED = 0
    PARTIAL = 1
    NO_LIQUIDITY = 2
    AON_UNFILLED = 3
    BAD_INST = 4
    BAD_AGE = 5
    BAD_QTY = 6
    BAD_LIMIT = 7


@dataclass(frozen=True)
class FeePolicy:
    enabled: int
    cap: int
    fee: int


@dataclass(frozen=True)
class SweepQuery:
    instrument: int
    tag: int
    buy: int
    quantity: int
    limit: int
    max_age: int
    venues: int
    all_or_none: int = 0


@dataclass(frozen=True)
class Allocation:
    venue: int
    price: int
    quantity: int
    fee: int
    sequence: int
    age: int

    def encode(self, profile: Profile, fee_width: int) -> tuple[int, int]:
        """Pack the documented child fields in their MSB to LSB order."""
        fields = (
            (self.venue, profile.venue_w),
            (self.price, profile.price_w),
            (self.quantity, profile.qty_w),
            (self.fee, fee_width),
            (self.sequence, profile.seq_w),
            (self.age, profile.time_w),
        )
        word, bits = 0, 0
        for value, width in fields:
            word = (word << width) | (value & mask(width))
            bits += width
        return word, bits


@dataclass(frozen=True)
class SweepResult:
    tag: int
    tick: int
    epoch: int
    status: SweepCode
    eligible: int = 0
    stale: int = 0
    quantity: int = 0
    value: int = 0
    children: tuple[Allocation, ...] = ()

    @property
    def scan_steps(self) -> int:
        """Return the number of consumed candidates, even on an aborted AON."""
        if self.status == SweepCode.AON_UNFILLED:
            return bin(self.eligible).count("1")
        return len(self.children)

    def encode(self, profile: Profile, fee_width: int) -> str:
        """Serialize exact numeric scoreboard fields and an LSB-first child array."""
        packed = 0
        for index, child in enumerate(self.children):
            word, bits = child.encode(profile, fee_width)
            packed |= word << (index * bits)
        return (f"{self.tag} {self.tick} {self.epoch} {int(self.status)} "
                f"{self.eligible} {self.stale} {len(self.children)} "
                f"{self.quantity} {self.value} {packed:x}")


def plan(
    book: QuoteBook,
    policy: list[FeePolicy],
    epoch: int,
    request: SweepQuery,
    tick: int,
) -> SweepResult:
    """Sort qualifying venues by adjusted value and allocate integer shares."""
    profile = book.profile
    common = (request.tag, tick, epoch)
    if request.instrument >= profile.instruments:
        return SweepResult(*common, SweepCode.BAD_INST)
    if request.max_age >= profile.time_half:
        return SweepResult(*common, SweepCode.BAD_AGE)
    if not request.quantity:
        return SweepResult(*common, SweepCode.BAD_QTY)
    if not request.limit:
        return SweepResult(*common, SweepCode.BAD_LIMIT)

    candidates = []
    eligible = stale = 0
    side = Side.ASK if request.buy else Side.BID
    for venue, rules in enumerate(policy):
        row = book.status(request.instrument, venue)
        if not row.enabled or row.quote is None:
            continue
        price, displayed = row.quote.price_size(side)
        if displayed == 0:
            continue
        age = distance(tick, row.quote.tick, profile.time_w)
        if age >= profile.time_half or age > request.max_age:
            stale |= 1 << venue
            continue
        if not (rules.enabled and rules.cap and (request.venues >> venue) & 1):
            continue
        if (request.buy and price > request.limit) or (not request.buy and price < request.limit):
            continue
        eligible |= 1 << venue
        capacity = min(displayed, rules.cap)
        candidates.append(Allocation(venue, price, capacity, rules.fee, row.quote.sequence, age))

    if request.all_or_none and sum(row.quantity for row in candidates) < request.quantity:
        return SweepResult(*common, SweepCode.AON_UNFILLED, eligible, stale)
    if not candidates:
        return SweepResult(*common, SweepCode.NO_LIQUIDITY, eligible, stale)

    def economic_rank(row: Allocation) -> tuple[int, int]:
        if request.buy:
            return row.price + row.fee, row.venue
        return row.fee - row.price, row.venue

    remaining = request.quantity
    selected = []
    for row in sorted(candidates, key=economic_rank):
        size = min(remaining, row.quantity)
        selected.append(Allocation(row.venue, row.price, size, row.fee, row.sequence, row.age))
        remaining -= size
        if not remaining:
            break
    if request.buy:
        value = sum(row.quantity * (row.price + row.fee) for row in selected)
    else:
        value = sum(row.quantity * (row.price - row.fee) for row in selected)
    status = SweepCode.PARTIAL if remaining else SweepCode.FILLED
    return SweepResult(*common, status, eligible, stale, request.quantity - remaining,
                       value, tuple(selected))
