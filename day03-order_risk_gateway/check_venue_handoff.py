"""Generate deterministic venue boundary races and compare exact captured replies."""

from __future__ import annotations

import argparse
import random
from collections import Counter
from dataclasses import astuple, replace
from enum import IntEnum
from itertools import zip_longest
from pathlib import Path

from portfolio_model import Control, ControlOp
from portfolio_vectors import PROFILES
from risk_model import Key, Policy, Profile, Status
from venue_handoff_model import Event, Handoff, Intent, LeaseCommand, LeaseOp, LeaseStatus, Report, Result


GEOMETRY = {"narrow": (3, 3), "wide": (3, 5), "singleton": (1, 1)}
STREAMS = {"audit": 18, "management": 6, "control": 9, "egress": 14, "pins": 6}


class Action(IntEnum):
    RESET = 0
    CONFIG = 1
    CONTROL = 2
    LEASE = 3
    INTENT = 4
    REPORT = 5
    TAKE = 6
    LOSS = 7
    TICKS = 8
    HEARTBEAT = 9
    HALT = 10
    WAIT = 11
    WAIT_TX = 12
    CANCEL_RACE = 13
    PINS = 14
    EXPIRE_RACE = 15
    CORRUPT_LEDGER = 16


class Campaign:
    def __init__(self, profile: Profile, venues: int, depth: int):
        self.model = Handoff(profile, venues, 4, depth)
        self.profile = profile
        self.stimulus = []
        self.expected = {name: [] for name in STREAMS}
        self.coverage = Counter()
        self.next_tag = 1

    def tag(self) -> int:
        value = self.next_tag
        self.next_tag += 1
        assert self.next_tag < (1 << self.profile.tag_bits)
        return value

    def append(self, action: Action, *values: int) -> None:
        if len(values) > 11:
            raise ValueError("fixture action exceeds eleven fields")
        self.stimulus.append((action, *values, *([0] * (11-len(values)))))

    def barrier(self, idle: int = 0) -> None:
        names = ("audit", "management", "control", "egress")
        self.append(Action.WAIT, *(len(self.expected[name]) for name in names), idle)

    def push_audit(self, response) -> None:
        self.expected["audit"].append(response.pins(self.profile))
        self.coverage[response.status.name] += 1

    def unwind(self) -> None:
        for response in self.model.unwind():
            self.push_audit(response)
        self.barrier()

    def reset(self) -> None:
        self.barrier()
        self.append(Action.RESET)
        self.model.reset()

    def config(self, key: Key, policy: Policy) -> None:
        self.model.risk.write_native_policy(key, policy)
        self.append(Action.CONFIG, *key, *astuple(policy))

    def setup(self, keys: tuple[Key, ...] = (Key(0, 0, 0),)) -> None:
        self.reset()
        qty = (1 << self.profile.qty_bits) - 1
        price = (1 << self.profile.price_bits) - 1
        for key in keys:
            self.config(key, Policy(1, 1, price, qty, (1 << self.profile.notional_bits) - 1,
                                    self.profile.slots))

    def lease(self, op: int, venue: int, epoch=0, timeout=0, expected=LeaseStatus.OK):
        request = LeaseCommand(op, venue, epoch, timeout)
        reply = self.model.leases.command(request)
        if reply[0] != expected:
            raise AssertionError(f"lease {request}: expected {expected.name}, got {reply}")
        self.append(Action.LEASE, *astuple(request))
        self.expected["management"].append(reply)
        self.unwind()
        return reply

    def arm(self, venue: int, epoch: int, timeout: int = 40):
        return self.lease(LeaseOp.ARM, venue, epoch, timeout)

    def control(self, command: Control):
        reply = self.model.risk.control(command)
        self.append(Action.CONTROL, *astuple(command))
        self.expected["control"].append((reply.status, *reply.caps.wire_fields(),
                                         *astuple(reply.exposure), reply.epoch))
        self.barrier()
        return reply

    def firm(self, gross: int, turnover: int, orders: int) -> None:
        reply = self.control(Control(ControlOp.READ, firm=1))
        assert (reply.exposure.gross, reply.exposure.turnover, reply.exposure.orders) == (
            gross, turnover, orders,
        )

    def observe(self) -> None:
        active = sum(self.model.leases.active(i) << i for i in range(self.model.venue_count))
        epochs = sum(row.epoch << (8*i) for i, row in enumerate(self.model.leases.rows))
        self.append(Action.PINS)
        self.expected["pins"].append((active, epochs, self.model.leases.ignored,
                                      self.model.leases.expirations, self.model.risk.halted,
                                      self.model.handoff_fault))

    def intent(self, request: Intent, expected: Result, *, race: bool = False):
        response = self.model.strategy(request)
        if response.status != expected:
            raise AssertionError(f"strategy {request}: expected {expected.name}, got {response}")
        self.append(Action.CANCEL_RACE if race else Action.INTENT, *astuple(request))
        self.push_audit(response)
        if race:
            egress = self.model.take_egress()
            if egress[0]:
                raise AssertionError("the same-edge cancel must waive the held NEW")
            self.expected["egress"].append(egress)
        self.unwind()
        return response

    def new(self, *, slot=0, venue=0, key=Key(0,0,0), price=100, quantity=8, expected=Result.APPLIED):
        epoch = self.model.leases.epoch(venue) if venue < self.model.venue_count else 0
        order = Intent(0, self.tag(), venue, epoch, *key, slot, price=price, quantity=quantity)
        return self.intent(order, expected)

    def cancel(self, slot: int = 0, *, race=False, expected=Result.CANCEL_QUEUED):
        route = self.model.orders[slot]
        order = replace(route.intent, cancel=1, tag=self.tag(), generation=route.generation,
                        price=0, quantity=0)
        return self.intent(order, expected, race=race)

    def report(self, request: Report, outcome: Result):
        response = self.model.report(request)
        if response.status != outcome:
            raise AssertionError(f"report {request}: wanted {outcome.name}, got {response}")
        self.append(Action.REPORT, *astuple(request))
        self.push_audit(response)
        self.unwind()
        return response

    def message(
        self, event: Event, *, slot=0, sequence=None, price=0, quantity=0, match=0, action=None,
    ) -> Report:
        route = self.model.orders[slot]
        if action is None:
            action = max(1,route.cancel_action) if event in (Event.CANCELED, Event.CANCEL_REJECT) else 0
        return Report(event, self.tag(), route.intent.venue, route.intent.epoch, slot, route.generation,
                      route.sequence+1 if sequence is None else sequence, price, quantity, match, action)

    def offer(self, valid: bool = True) -> None:
        message = self.model.select_egress()
        if message is None or self.model.applicable(message) != valid:
            raise AssertionError(f"wanted {'live' if valid else 'waived'} egress but saw {message}")
        self.append(Action.WAIT_TX, valid)

    def take(self, valid: bool = True):
        self.offer(valid)
        receipt = self.model.take_egress()
        assert receipt[0] == valid
        self.append(Action.TAKE)
        self.expected["egress"].append(receipt)
        return receipt

    def loss(self, bitmap: int) -> None:
        self.append(Action.LOSS, bitmap)
        self.model.leases.set_losses(bitmap)
        self.unwind()

    def pulse(self, ticks: int = 0, heartbeat: tuple[int, int] | None = None):
        if heartbeat:
            assert ticks in (0, 1)
            self.append(Action.HEARTBEAT, *heartbeat, ticks)
        else:
            self.append(Action.TICKS, ticks)
        self.model.leases.pulse(ticks, heartbeat)
        self.unwind()

    def halt(self) -> None:
        self.append(Action.HALT)
        self.model.risk.halt()
        self.unwind()

    def write(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.barrier(idle=5)
        write_rows(directory / "stimulus.hex", self.stimulus)
        for name, rows in self.expected.items():
            write_rows(directory / f"expected-{name}.hex", rows)


def supervised_recovery(c: Campaign) -> None:
    """Prove the distinction between never-sent and sent with unknown outcome."""
    c.setup()
    c.lease(LeaseOp.ARM, 0, 0, 5, LeaseStatus.BAD_EPOCH)
    c.lease(LeaseOp.ARM, c.model.venue_count, 1, 5, LeaseStatus.BAD_VENUE)
    c.arm(0, 1, 4)
    c.new(quantity=8)
    c.offer()
    c.firm(800, 0, 1)
    c.report(c.message(Event.ACK), Result.UNSENT)
    c.loss(1)
    c.lease(LeaseOp.ARM, 0, 2, 4, LeaseStatus.LINK_DOWN)
    c.firm(0, 0, 0)
    c.observe()
    c.take(False)

    c.loss(0)
    c.arm(0, 2, 4)
    assert c.new().generation == 2
    c.take()
    c.firm(800, 0, 1)
    c.lease(LeaseOp.FENCE, 0, 1, expected=LeaseStatus.BAD_EPOCH)
    c.pulse(3)
    c.pulse(1, (0,1))  # stale heartbeat cannot keep the active lease open
    c.observe()
    c.firm(800, 0, 1)
    c.report(c.message(Event.ACK), Result.ENTRY_ACKED)
    c.report(c.message(Event.REJECT), Result.WRONG_PHASE)
    fill = c.message(Event.FILL, price=99, quantity=3, match=11)
    c.report(fill, Result.APPLIED)
    c.report(replace(fill, tag=c.tag()), Result.REPLAY)
    c.report(replace(fill, tag=c.tag(), quantity=4), Result.CHANGED_REPORT)
    c.report(c.message(Event.FILL, price=100, quantity=1, match=11), Result.REUSED_MATCH)
    c.report(c.message(Event.EXPIRED, sequence=4), Result.REPORT_GAP)
    c.firm(500, 297, 1)
    c.report(c.message(Event.FILL, price=94, quantity=1, match=12), Result.APPLIED)
    c.cancel()
    c.report(c.message(Event.CANCELED), Result.WRONG_PHASE)
    c.firm(400, 391, 1)
    c.arm(0, 3)
    sent = c.take()
    assert sent[4:6] == (2, 3)  # original order lease, current cancel connection
    c.report(c.message(Event.CANCEL_REJECT), Result.CANCEL_REFUSED)
    c.cancel()
    c.take()
    terminal = c.message(Event.CANCELED)
    c.report(replace(terminal, action=1), Result.WRONG_CANCEL_ACTION)
    c.report(terminal, Result.APPLIED)
    c.report(replace(terminal, tag=c.tag()), Result.REPLAY)
    c.report(c.message(Event.ACK, sequence=1),
             Result.OLD_REPORT if c.model.history < 5 else Result.REPLAY)
    c.firm(0, 391, 0)
    c.observe()


def watchdog_and_cancel_edges(c: Campaign) -> None:
    c.setup()
    c.arm(0, 1, 2)
    c.new(quantity=5)
    c.offer()
    c.cancel(race=True)
    c.firm(0, 0, 0)
    c.new(quantity=9)
    c.offer()
    c.pulse()
    c.pulse(1)
    c.pulse(1, (0,1))  # exact heartbeat at the expiry edge wins
    c.lease(LeaseOp.READ, 0)
    c.observe()
    c.offer()
    c.pulse(1)
    c.append(Action.EXPIRE_RACE)
    c.model.leases.pulse(1)
    expired = c.model.take_egress()
    assert expired[0] == 0
    c.expected["egress"].append(expired)
    c.unwind()
    c.firm(0, 0, 0)
    c.arm(0, 2, 2)
    c.new(quantity=9)
    c.take()
    c.cancel()
    c.offer()
    c.lease(LeaseOp.FENCE, 0, 2)
    c.take(False)
    c.arm(0, 3, 4)
    c.halt()  # an already submitted order can still transmit its cancellation
    sent = c.take()
    assert sent[4:6] == (2,3)
    c.report(c.message(Event.CANCELED), Result.APPLIED)
    c.firm(0, 0, 0)
    c.new(expected=Result.LEASE_CLOSED)


def fill_boundaries(c: Campaign) -> None:
    c.setup()
    c.arm(0,1)
    c.control(Control(ControlOp.SET, 1, mask=1, gross_limit=5000))
    refusal = c.new(price=101, quantity=50, expected=Result.RISK_REFUSED)
    assert refusal.native is not None and refusal.native.reason == 4
    c.new(quantity=40)
    c.take()
    c.report(c.message(Event.ACK), Result.ENTRY_ACKED)
    for match in range(1,14):
        c.report(c.message(Event.FILL, price=101, quantity=1, match=match), Result.APPLIED)
    c.report(c.message(Event.FILL, price=101, quantity=1, match=14), Result.LAST_SEQUENCE)
    c.report(c.message(Event.FILL, price=100, quantity=27, match=15), Result.APPLIED)
    c.firm(0, 4013, 0)
    c.arm(0,2)
    c.new()
    c.take()
    first = c.message(Event.FILL, price=110, quantity=9, match=21)
    error = c.report(first, Result.RISK_REFUSED)
    assert error.native and error.native.decision.code == Status.OVERFILL
    c.report(replace(first, tag=c.tag(), quantity=8), Result.APPLIED)
    c.firm(0, 4893, 0)


def interleaved_venues(c: Campaign) -> None:
    if c.model.venue_count == 1 or c.profile.slots < 3:
        return
    c.setup()
    c.arm(0, 1)
    c.arm(1, 1)
    c.new(venue=1, slot=1, price=50, quantity=11)
    c.take()
    c.new(venue=0, slot=0, price=100, quantity=4)
    c.offer()
    c.new(venue=0, slot=2, price=100, quantity=4)
    c.firm(1350, 0, 3)
    c.report(c.message(Event.FILL, slot=1, price=49, quantity=1, match=63), Result.APPLIED)
    c.firm(1300, 49, 3)
    c.loss(1)
    c.firm(500, 49, 1)
    c.observe()
    c.cancel(slot=1)
    c.take(False)
    action = c.take()
    assert (action[1], action[3], action[9]) == (1, 1, 1)
    c.report(c.message(Event.CANCELED, slot=1), Result.APPLIED)
    c.firm(0, 49, 0)


def corrupted_native_read(c: Campaign) -> None:
    c.setup()
    c.arm(0, 1)
    if c.profile.slots > 1:
        c.new(slot=1, price=40, quantity=5)
        c.take()
    c.new()
    c.offer()
    c.append(Action.CORRUPT_LEDGER, 1)
    c.model.corrupt_read = True
    c.loss(1)
    c.append(Action.CORRUPT_LEDGER, 0)
    c.model.corrupt_read = False
    c.take(False)
    c.observe()
    c.loss(0)
    c.arm(0, 2)
    c.new(expected=Result.LEASE_CLOSED)
    if c.profile.slots > 1:
        c.report(c.message(Event.FILL, slot=1, price=38, quantity=5, match=25), Result.APPLIED)
    c.firm(800, 190 if c.profile.slots > 1 else 0, 1)


def action_exhaustion(c: Campaign) -> None:
    c.setup()
    c.arm(0,1)
    c.new()
    c.take()
    for action in range(1,c.model.last_action+1):
        c.cancel()
        sent = c.take()
        assert sent[-3] == action
        c.report(c.message(Event.CANCEL_REJECT), Result.CANCEL_REFUSED)
    c.cancel(expected=Result.ACTION_EXHAUSTED)
    c.firm(800, 0, 1)
    c.report(c.message(Event.EXPIRED), Result.APPLIED)
    c.firm(0, 0, 0)


def randomized(c: Campaign, seed: int, trials: int) -> None:
    rng = random.Random(seed)
    key1 = Key(c.profile.accounts-1,c.profile.instruments-1,1)
    keys = (Key(0,0,0),key1)
    for i in range(trials):
        if i % 5 == 0:
            c.setup(keys)
        venue = rng.randrange(c.model.venue_count)
        if not c.model.leases.active(venue):
            c.arm(venue, c.model.leases.epoch(venue)+1, rng.randint(2,12))
        slot = rng.randrange(c.profile.slots)
        key = rng.choice(keys)
        price, qty = rng.randint(1,140), rng.randint(4,24)
        c.new(venue=venue, key=key, slot=slot, price=price, quantity=qty)
        c.offer()
        case = rng.randrange(7)
        if case == 0:
            c.cancel(slot, race=True)
        elif case == 1:
            c.arm(venue,c.model.leases.epoch(venue)+1)
            c.take(False)
        elif case == 2:
            c.loss(1 << venue)
            c.take(False)
            c.loss(0)
        else:
            c.take()
            if rng.randrange(2):
                c.report(c.message(Event.ACK, slot=slot), Result.ENTRY_ACKED)
            first = c.message(Event.FILL, slot=slot, price=rng.randint(1,130), quantity=qty//2,
                              match=rng.randrange(1,65536))
            c.report(first, Result.APPLIED)
            c.report(replace(first, tag=c.tag()), Result.REPLAY)
            if case == 3:
                c.report(replace(first, tag=c.tag(), quantity=qty), Result.CHANGED_REPORT)
                c.arm(venue, c.model.leases.epoch(venue)+1)
            if case == 4:
                c.pulse(c.model.leases.rows[venue].remaining)
                c.arm(venue,c.model.leases.epoch(venue)+1)
            if case == 5:
                c.cancel(slot)
                c.take()
                c.report(c.message(Event.CANCEL_REJECT, slot=slot), Result.CANCEL_REFUSED)
                c.cancel(slot)
                c.take()
                c.report(c.message(Event.CANCELED, slot=slot), Result.APPLIED)
            else:
                c.report(c.message(Event.EXPIRED, slot=slot), Result.APPLIED)
        expected = c.model.risk.exposure(None)
        c.firm(*astuple(expected))
        c.observe()


def write_rows(path: Path, rows) -> None:
    with path.open("w", encoding="ascii") as stream:
        for row in rows:
            print(" ".join(f"{int(value):x}" for value in row), file=stream)


def read_rows(path: Path, width: int):
    with path.open(encoding="ascii") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = tuple(int(field, 16) for field in line.split())
            except ValueError as error:
                raise ValueError(f"{path}:{number}: unknown or malformed value") from error
            if len(row) != width:
                raise ValueError(f"{path}:{number}: expected {width} fields, saw {len(row)}")
            yield row


def compare(directory: Path) -> None:
    summary = {}
    for name,width in STREAMS.items():
        expected = read_rows(directory / f"expected-{name}.hex", width)
        observed = read_rows(directory / f"actual-{name}.hex", width)
        count = 0
        for number,(want,actual) in enumerate(zip_longest(expected,observed), 1):
            if want != actual:
                raise AssertionError(f"{name} receipt {number}\\nexpected {want}\\nactual   {actual}")
            count += 1
        summary[name] = count
    print(f"PASS exact handoff streams {summary}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("directory", type=Path)
    gen.add_argument("--profile", choices=PROFILES, default="narrow")
    gen.add_argument("--seed", type=lambda text: int(text,0), default=0x30E6)
    gen.add_argument("--trials", type=int, default=125)
    gen.add_argument("--demo", action="store_true")
    sub.add_parser("check").add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "check":
        compare(args.directory)
        return
    if not 0 <= args.trials <= 3000:
        parser.error("trials must be between 0 and 3000")
    campaign = Campaign(PROFILES[args.profile], *GEOMETRY[args.profile])
    supervised_recovery(campaign)
    if not args.demo:
        watchdog_and_cancel_edges(campaign)
        fill_boundaries(campaign)
        action_exhaustion(campaign)
        interleaved_venues(campaign)
        corrupted_native_read(campaign)
        randomized(campaign,args.seed,args.trials)
    campaign.write(args.directory)
    print(" ".join(f"{name}={n}" for name,n in sorted(campaign.coverage.items())))


if __name__ == "__main__":
    main()
