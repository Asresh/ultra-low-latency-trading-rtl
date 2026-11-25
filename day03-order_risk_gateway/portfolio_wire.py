"""Checked conversion between Day 03 portfolio pins and replay records."""

from __future__ import annotations

from dataclasses import astuple, dataclass
from enum import IntEnum

from portfolio_model import Control, PortfolioModel
from risk_model import Key, Policy, Profile, Request, bits_for, checked_row, encode_reply


class Kind(IntEnum):
    RESET = 0
    CONFIG = 1
    ORDER = 2
    CONTROL = 3
    HALT = 4


@dataclass(frozen=True)
class NativeConfig:
    key: Key
    policy: Policy


@dataclass(frozen=True)
class Input:
    kind: Kind
    command: NativeConfig | Request | Control | None


def decode_row(profile: Profile, row: tuple[int, ...], number: int) -> Input:
    """Validate exact wire widths before any operation enters the independent model."""
    if len(row) != 11:
        raise ValueError(f"row {number}: expected action and ten fields")
    try:
        kind = Kind(row[0])
    except ValueError as err:
        raise ValueError(f"row {number}: unknown action {row[0]}") from err
    p = profile
    account_bits, instrument_bits, slot_bits = (
        bits_for(p.accounts), bits_for(p.instruments), bits_for(p.slots)
    )
    count_bits = bits_for(p.slots + 1)
    total_bits = p.notional_bits + bits_for(p.slots + 2 * p.accounts * p.instruments + 1)
    widths = {
        Kind.CONFIG: (account_bits, instrument_bits, 1, 1, p.price_bits, p.price_bits,
                      p.qty_bits, p.notional_bits, count_bits),
        Kind.ORDER: (3, p.tag_bits, account_bits, instrument_bits, 1, slot_bits,
                     p.generation_bits, p.event_bits, p.price_bits, p.qty_bits),
        Kind.CONTROL: (2, 1, account_bits, 3, total_bits, count_bits, total_bits, 32),
    }.get(kind, ())
    used = len(widths)
    checked_row(row[1:1 + used], widths, f"row {number} {kind.name}")
    if any(row[used + 1:]):
        raise ValueError(f"row {number}: trailing padding must be zero")
    fields = row[1:1 + used]
    if kind is Kind.CONFIG:
        return Input(kind, NativeConfig(Key(*fields[:3]), Policy(*fields[3:])))
    if kind is Kind.ORDER:
        return Input(kind, Request(*fields))
    if kind is Kind.CONTROL:
        return Input(kind, Control(*fields))
    return Input(kind, None)


def expected_capture(model: PortfolioModel, number: int, issued: Input) -> tuple[int, ...] | None:
    """Apply one validated operation and return the captured pin row it should emit."""
    kind, command = issued.kind, issued.command
    if kind is Kind.RESET:
        model.reset()
    elif kind is Kind.HALT:
        model.halt()
    elif kind is Kind.CONFIG:
        assert isinstance(command, NativeConfig)
        model.write_native_policy(command.key, command.policy)
    elif kind is Kind.ORDER:
        assert isinstance(command, Request)
        receipt = model.transact(command)
        return (
            number, kind, encode_reply(model.profile, receipt.decision), receipt.reason,
            *astuple(receipt.account), *astuple(receipt.firm), receipt.epoch,
        )
    elif kind is Kind.CONTROL:
        assert isinstance(command, Control)
        receipt = model.control(command)
        return (
            number, kind, receipt.status, *receipt.caps.wire_fields(),
            *astuple(receipt.exposure), receipt.epoch,
        )
    return None
