"""Make directed and seeded pin vectors for the portfolio gateway."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import astuple
from pathlib import Path
from random import Random

from portfolio_model import Caps, Control, ControlOp, Denial, PortfolioModel
from portfolio_wire import Kind
from risk_model import Key, Operation, Policy, Profile, Request, Status, encode_reply


PROFILES = {
    "narrow": Profile("portfolio-narrow", 3, 3, 5, 8, 8, 4, 5),
    "wide": Profile("portfolio-wide", 3, 3, 5, 24, 16, 4, 5),
    "singleton": Profile("portfolio-singleton", 1, 1, 1, 8, 8, 3, 4),
}


class Campaign:
    def __init__(self, profile: Profile, seed: int):
        self.profile = profile
        self.model = PortfolioModel(profile)
        self.rng = Random(seed)
        self.stimulus = []
        self.gold = []
        self.coverage = Counter()
        self.tag = 0

    def event(self, kind: Kind, *payload: int) -> int:
        if len(payload) > 10:
            raise ValueError("stimulus row exceeds ten fields")
        self.stimulus.append((kind, *payload, *([0] * (10 - len(payload)))))
        return len(self.stimulus)

    def reset(self) -> None:
        self.model.reset()
        self.event(Kind.RESET)

    def halt(self) -> None:
        self.model.halt()
        self.event(Kind.HALT)

    def configure(self, key: Key, policy: Policy) -> None:
        self.model.write_native_policy(key, policy)
        self.event(Kind.CONFIG, *key, *astuple(policy))

    def configure_all(self) -> None:
        for a in range(self.profile.accounts):
            for i in range(self.profile.instruments):
                for s in (0, 1):
                    self.configure(Key(a, i, s), Policy(
                        1, 1, self.profile.max_price, self.profile.max_qty,
                        self.profile.max_notional, self.profile.slots,
                    ))

    def control(self, command: Control):
        sequence = self.event(Kind.CONTROL, *astuple(command))
        receipt = self.model.control(command)
        self.gold.append((sequence, Kind.CONTROL, receipt.status,
                          *receipt.caps.wire_fields(), *astuple(receipt.exposure), receipt.epoch))
        self.coverage[f"control/{receipt.status.name}"] += 1
        return receipt

    def caps(self, scope: int | None, limits: Caps):
        return self.control(Control(ControlOp.SET, int(scope is None), scope or 0, *limits.wire_fields()))

    def read(self, scope: int | None):
        return self.control(Control(ControlOp.READ, int(scope is None), scope or 0))

    def roll(self, epoch: int):
        return self.control(Control(ControlOp.ROLL, 1, epoch=epoch))

    def order(self, op: int, key: Key, slot: int, price=0, qty=0, generation=0, event=0):
        self.tag = (self.tag + 1) & 0xffff
        request = Request(op, self.tag, *key, slot, generation, event, price, qty)
        sequence = self.event(Kind.ORDER, *astuple(request))
        receipt = self.model.transact(request)
        self.gold.append((sequence, Kind.ORDER, encode_reply(self.profile, receipt.decision),
                          receipt.reason, *astuple(receipt.account), *astuple(receipt.firm), receipt.epoch))
        self.coverage[f"native/{receipt.decision.code}"] += 1
        self.coverage[f"reason/{receipt.reason.name}"] += 1
        return receipt

    def lifecycle(self) -> None:
        a = Key(0, 0, 0)
        alt = Key(0, min(self.profile.instruments - 1, 1), 1)
        b = Key(min(self.profile.accounts - 1, 1), 0, 0)
        self.reset()
        self.configure_all()
        # Inclusive gross cap at exactly one hundred units.
        self.caps(0, Caps(gross=100))
        assert self.order(Operation.NEW, a, 0, 10, 10).decision.code == Status.OK
        self.read(0)
        try_bump = self.order(Operation.AMEND, a, 0, 20, 10, 1, 1)
        assert try_bump.reason == Denial.ACCOUNT_GROSS
        # Lower the cap below current gross, then allow a risk-reducing amend,
        # partial fill at an improved price, and final cancellation.
        self.caps(0, Caps(gross=40))
        assert self.order(Operation.AMEND, a, 0, 10, 8, 1, 1).decision.code == Status.OK
        fill = self.order(Operation.FILL, a, 0, 20, 3, 1, 2)
        assert (fill.account.gross, fill.account.turnover, fill.account.orders) == (50, 60, 1)
        assert self.order(Operation.CANCEL, a, 0, generation=1, event=3).decision.code == Status.OK
        assert self.order(Operation.FILL, a, 0, 20, 1, 1, 3).decision.code == Status.NOT_LIVE

        for denial, scope, caps in [
            (Denial.ACCOUNT_COUNT, 0, Caps(count=0)),
            (Denial.ACCOUNT_USAGE, 0, Caps(usage=66)),
            (Denial.FIRM_GROSS, None, Caps(gross=6)),
            (Denial.FIRM_COUNT, None, Caps(count=0)),
            (Denial.FIRM_USAGE, None, Caps(usage=66)),
        ]:
            self.caps(scope, caps)
            assert self.order(Operation.NEW, a, 0, 7, 1).reason == denial
            self.caps(scope, Caps())

        assert self.order(Operation.NEW, a, 0, 25, 4).decision.generation == 2
        if self.profile.slots > 1:
            self.caps(0, Caps(gross=150))
            self.caps(None, Caps(gross=160))
            assert self.order(Operation.NEW, alt, 1, 10, 4).decision.code == Status.OK
            assert self.order(Operation.NEW, alt, 2, 10, 2).reason == Denial.ACCOUNT_GROSS
            assert self.order(Operation.NEW, b, 2, 10, 3).reason == Denial.FIRM_GROSS
            assert self.order(Operation.NEW, b, 2, 10, 2).firm.gross == 160
        prior = self.read(None).exposure
        assert self.roll(1).exposure.gross == prior.gross
        assert self.roll(0).status
        assert self.roll(1).status
        self.read(0)
        # A stale amend must reach native lifecycle checks even when the cap is zero.
        self.caps(0, Caps(gross=0, count=0, usage=0))
        assert self.order(Operation.AMEND, a, 0, 1, 1, 1, 1).decision.code == Status.STALE_GENERATION
        self.halt()
        assert self.order(Operation.AMEND, a, 0, 24, 4, 2, 1).decision.code == Status.HALTED
        assert self.order(Operation.FILL, a, 0, 37, 1, 2, 1).decision.code == Status.OK
        assert self.order(Operation.CANCEL, a, 0, generation=2, event=2).decision.code == Status.OK
        self.read(None)
        self.control(Control(3))
        self.control(Control(ControlOp.READ, firm=1, account=1))
        self.control(Control(ControlOp.ROLL, firm=0, epoch=2))
        self.control(Control(ControlOp.SET, mask=2, count_limit=(1 << self.profile.slots.bit_length()) - 1))
        assert self.roll(0xffffffff).epoch == 0xffffffff
        self.roll(0xfffffffe)

    def wide_arithmetic(self) -> None:
        self.reset()
        self.configure_all()
        reserve = self.profile.max_qty * self.profile.max_price
        for idx in range(self.profile.slots):
            account, other = divmod(idx, self.profile.instruments * 2)
            instrument, side = divmod(other, 2)
            key = Key(account % self.profile.accounts, instrument, side)
            receipt = self.order(Operation.NEW, key, idx, self.profile.max_price, self.profile.max_qty)
            assert receipt.decision.code == Status.OK
        assert self.read(None).exposure.gross == reserve * self.profile.slots
        self.caps(None, Caps(gross=reserve))
        for slot, order in sorted(self.model.gateway.orders.items()):
            self.order(Operation.CANCEL, order.key, slot, generation=order.generation, event=1)
        assert self.read(None).exposure.gross == 0

    def random_request(self) -> None:
        p, rng = self.profile, self.rng
        slot = rng.randrange(p.slots)
        old = self.model.gateway.orders.get(slot)
        key = Key(rng.randrange(p.accounts), rng.randrange(p.instruments), rng.randrange(2))
        if old and old.live and rng.randrange(5) != 0:
            op = rng.choice((Operation.FILL, Operation.CANCEL, Operation.REJECT, Operation.AMEND, Operation.SNAPSHOT))
            do_fill = op is Operation.FILL
            has_amount = op in (Operation.FILL, Operation.AMEND)
            price = rng.randint(1, 31) if has_amount else 0
            qty = rng.randint(1, old.qty + int(do_fill)) if has_amount else 0
            generation = old.generation - int(rng.randrange(14) == 0)
            event = min(p.last_event, old.event + 1 + int(rng.randrange(13) == 0))
            self.order(op, old.key, slot, price, qty, generation, event)
        elif rng.randrange(8) == 0:
            self.order(rng.choice((Operation.SNAPSHOT, 7)), key, slot)
        else:
            self.order(Operation.NEW, key, slot, rng.randrange(36), rng.randrange(15))

    def random_activity(self, operations: int) -> None:
        p, rng = self.profile, self.rng
        for idx in range(operations):
            if idx % 160 == 0:
                self.reset()
                self.configure_all()
            step = rng.randrange(100)
            scope = rng.randrange(p.accounts) if step & 1 else None
            if step < 7:
                self.caps(scope, Caps(
                    rng.choice((None, 0, rng.randrange(900))),
                    rng.choice((None, rng.randrange(p.slots + 1))),
                    rng.choice((None, 0, rng.randrange(900))),
                ))
            elif step < 15:
                self.read(scope)
            elif step < 22:
                self.roll(min(0xffffffff, self.model.epoch + rng.randrange(5)))
            elif step == 22:
                self.halt()
            else:
                self.random_request()

    def write(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for name, rows in (("stimulus.hex", self.stimulus), ("expected.hex", self.gold)):
            with (directory / name).open("w", encoding="ascii") as output:
                for row in rows:
                    output.write(" ".join(f"{value:x}" for value in row) + "\n")


def read_capture(path: Path) -> list[tuple[int, ...]]:
    """Read a capture and give a line number for missing or unknown signals."""
    rows = []
    with path.open("r", encoding="ascii") as source:
        for number, line in enumerate(source, 1):
            try:
                rows.append(tuple(int(value, 16) for value in line.split()))
            except ValueError as err:
                raise ValueError(f"{path}:{number}: invalid hex field") from err
    return rows


def verify(directory: Path) -> int:
    expected = read_capture(directory / "expected.hex")
    actual = read_capture(directory / "actual.hex")
    for index, (want, have) in enumerate(zip(expected, actual), 1):
        if want != have:
            raise ValueError(f"comparison {index} disagreed\nexpected {want!r}\nactual   {have!r}")
    if len(expected) != len(actual):
        raise ValueError(f"expected {len(expected)} replies but observed {len(actual)}")
    print(f"PASS: {len(expected)} exact native/portfolio/control results")
    return len(expected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("directory", type=Path)
    generate.add_argument("--profile", choices=PROFILES, default="narrow")
    generate.add_argument("--operations", type=int, default=850)
    generate.add_argument("--seed", type=int, default=30971)
    compare = commands.add_parser("check")
    compare.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "check":
        verify(args.directory)
        return
    if not 0 <= args.operations <= 200_000:
        parser.error("--operations must be within 0..200000")
    campaign = Campaign(PROFILES[args.profile], args.seed)
    campaign.lifecycle()
    campaign.wide_arithmetic()
    campaign.random_activity(args.operations)
    campaign.write(args.directory)
    print(f"Wrote {len(campaign.stimulus)} inputs, {len(campaign.gold)} results.")
    print(", ".join(f"{label}={count}" for label, count in sorted(campaign.coverage.items())))


if __name__ == "__main__":
    main()
