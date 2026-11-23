"""Exact integer oracle for the two rounding points in the notional datapath."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import NamedTuple

FEE_SCALE = 10_000
U64 = (1 << 64) - 1


class Rounding(IntEnum):
    FLOOR = 0
    CEIL = 1
    NEAREST_EVEN = 2


class Kind(IntEnum):
    OK = 0
    BAD_QUANTITY = 1
    BAD_PRICE = 2
    BAD_FEE = 3
    BAD_MODE = 4
    OVERFLOW = 5
    LIMIT = 6


def unsigned(value, bits, name):
    """Validate a value as an unsigned fixed-width port field."""
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValueError(f"{name} must fit in {bits} unsigned bits")
    return value


@dataclass(frozen=True)
class Request:
    tag: int
    price: int
    quantity: int
    fee_bps: int = 0
    rounding: int = 0
    sell: bool = False
    limit: int | None = None

    def __post_init__(self):
        """Enforce wire widths while keeping representable malformed requests testable."""
        for name, bits in (("tag", 16), ("price", 32), ("quantity", 32), ("fee_bps", 16)):
            unsigned(getattr(self, name), bits, name)
        if isinstance(self.rounding, bool) or not isinstance(self.rounding, int) or not 0 <= self.rounding < 4:
            raise ValueError("rounding must fit in two bits")
        if type(self.sell) is not bool:
            raise ValueError("sell must be a boolean")
        if self.limit is not None:
            unsigned(self.limit, 64, "limit")

    def wire(self):
        """Produce the eight physical fields in the testbench's input order."""
        return (self.tag, self.price, self.quantity, self.fee_bps,
                int(self.rounding), int(self.sell), int(self.limit is not None), self.limit or 0)


class Rounded(NamedTuple):
    amount: int
    inexact: bool


@dataclass(frozen=True)
class Reply:
    tag: int
    status: Kind
    sell: bool
    gross: int = 0
    fee: int = 0
    cash: int = 0
    inexact: bool = False

    def wire(self):
        """Produce the reply fields in the same order as the hardware output log."""
        return (self.tag, int(self.status), int(self.sell),
                self.gross, self.fee, self.cash, int(self.inexact))


def rounded_divide(numerator, denominator, mode):
    """Round a nonnegative fraction, reporting whether it discarded a remainder."""
    if numerator < 0 or denominator <= 0:
        raise ValueError("division requires a nonnegative numerator and positive divisor")
    mode = Rounding(mode)
    whole, remainder = divmod(numerator, denominator)
    if remainder == 0:
        return Rounded(whole, False)
    if mode == Rounding.CEIL:
        whole += 1
    elif mode == Rounding.NEAREST_EVEN:
        upper_distance = denominator - remainder
        if remainder > upper_distance or (remainder == upper_distance and whole % 2):
            whole += 1
    return Rounded(whole, True)


def request_fault(request):
    """Apply the reply's documented validation priority to raw input fields."""
    if request.quantity == 0:
        return Kind.BAD_QUANTITY
    if request.price == 0:
        return Kind.BAD_PRICE
    if request.fee_bps > FEE_SCALE:
        return Kind.BAD_FEE
    if request.rounding == 3:
        return Kind.BAD_MODE
    return Kind.OK


def calculate(request, fraction_bits):
    """Compute quantized gross, fee and buy debit or sell proceeds."""
    if not 0 <= fraction_bits <= 31:
        raise ValueError("price fractional width must be in the range 0–31")
    status = request_fault(request)
    if status != Kind.OK:
        return Reply(request.tag, status, request.sell)
    gross, coarse = rounded_divide(
        request.price * request.quantity,
        1 << fraction_bits,
        request.rounding,
    )
    fee, fee_fraction = rounded_divide(gross * request.fee_bps, FEE_SCALE, request.rounding)
    net = gross - fee if request.sell else gross + fee
    if net > U64:
        net = U64
        status = Kind.OVERFLOW
    elif request.limit is not None:
        outside = net < request.limit if request.sell else net > request.limit
        if outside:
            status = Kind.LIMIT
    return Reply(request.tag, status, request.sell, gross, fee, net, coarse or fee_fraction)
