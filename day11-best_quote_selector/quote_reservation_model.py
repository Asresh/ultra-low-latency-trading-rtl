"""Independent reservation, expiry and grant oracle with physical quote inventory."""

from __future__ import annotations

from collections import Counter
from copy import copy
from dataclasses import astuple, dataclass, replace
from enum import IntEnum

from quote_model import QuoteBook
from quote_schema import Code, Op, Profile, distance, mask
from quote_sweep_model import FeePolicy, SweepCode, SweepQuery, SweepResult, plan
from quote_sweep_vectors import Cycle


class ReservationCode(IntEnum):
    HELD = 0
    NO_PLAN = 1
    QUOTE_CHANGED = 2
    POLICY_CHANGED = 3
    NO_TOKEN = 4
    BAD_TTL = 5


class GrantCode(IntEnum):
    OK = 0
    BAD_ADDRESS = 1
    WRONG_GENERATION = 2
    NOT_HELD = 3
    REVOKED = 4


@dataclass(frozen=True)
class Action:
    claim: int
    tag: int
    slot: int
    generation: int


@dataclass(frozen=True)
class Drive:
    base: Cycle
    ttl: int = 0
    grant_ready: int = 1
    action: Action | None = None

    def encode(self) -> str:
        action_fields = (1,*astuple(self.action)) if self.action else (0,) * 5
        return self.base.encode() + " " + " ".join(map(str,(self.ttl,self.grant_ready,*action_fields)))


@dataclass(frozen=True)
class Token:
    slot: int
    generation: int
    request: SweepQuery
    birth: int
    ttl: int
    plan: SweepResult
    ticks: dict[int,int]

    def obsolete(self, tick: int, profile: Profile, update: tuple[int,int] | None, commit: bool) -> bool:
        elapsed = distance(tick,self.birth,profile.time_w)
        if commit or elapsed >= self.ttl:
            return True
        if update and update[0] == self.request.instrument and update[1] in self.ticks:
            return True
        return any(distance(tick,stamp,profile.time_w) > self.request.max_age
                   for stamp in self.ticks.values())


@dataclass(frozen=True)
class Forecast:
    at: int
    request: SweepQuery
    ttl: int
    result: SweepResult
    updates: dict[int,int]
    revisions: dict[int,int]
    policy_changed: bool


@dataclass(frozen=True)
class Reservation:
    status: ReservationCode
    result: SweepResult
    token: Token | None = None

    def encode(self, profile: Profile, fee_width: int) -> str:
        payload = self.result
        if not self.token:
            payload = replace(payload, quantity=0,value=0,children=())
        token = self.token
        bits = (int(self.status),token.slot,token.generation,
                (token.birth+token.ttl)&mask(profile.time_w)) if token else (int(self.status),0,0,0)
        return payload.encode(profile,fee_width) + " " + " ".join(map(str,bits))


@dataclass(frozen=True)
class Grant:
    request: Action
    status: GrantCode
    token: Token | None = None

    def encode(self, profile: Profile, fee_width: int, applicable: bool) -> str:
        token = self.token
        scope = (token.request.instrument,token.request.buy) if token else (0,0)
        children = encode_children(token.plan,profile,fee_width) if token and self.request.claim else "0"
        return " ".join(map(str,(int(applicable),int(self.status),*astuple(self.request),*scope,children)))


def encode_children(bundle: SweepResult, profile: Profile, fee_width: int) -> str:
    packed = 0
    for index,child in enumerate(bundle.children):
        word,bits = child.encode(profile,fee_width)
        packed |= word << (index*bits)
    return f"{packed:x}"


@dataclass(frozen=True)
class Sample:
    number: int
    cmd_ready: int
    cfg_ready: int
    commit_ready: int
    cfg_error: int
    epoch: int
    query_ready: int
    action_ready: int
    ack: tuple[int,int] | None
    reservation: Reservation | None
    grant: Grant | None
    applicable: bool
    counts: tuple[int,...]

    def encode(self, profile: Profile, fee_width: int) -> str:
        heads = self.number,self.cmd_ready,self.cfg_ready,self.commit_ready,self.cfg_error,self.epoch, \
            self.query_ready,self.action_ready
        ack = (1,*self.ack) if self.ack else (0,0,0)
        held = "1 " + self.reservation.encode(profile,fee_width) if self.reservation else "0"+" 0"*14
        grant = "1 " + self.grant.encode(profile,fee_width,self.applicable) if self.grant else "0"+" 0"*9
        return " ".join(map(str,heads+ack)) + f" {held} {grant} " + " ".join(map(str,self.counts))


class ReservingMachine:
    """Model accepted edges, integer remaining quantities, and retired ID evidence."""

    def __init__(self, profile: Profile, slots: int, revision_bits: int, token_bits: int, fee_width: int = 12):
        self.profile = profile
        self.slots = slots
        self.revision_max = mask(revision_bits)
        self.token_max = mask(token_bits)
        self.fee_width = fee_width
        self.cycles: list[Drive] = []
        self.samples: list[Sample] = []
        self.coverage = Counter()
        self.clear()

    def clear(self) -> None:
        self.book = QuoteBook(self.profile)
        self.ack = None
        self.pending: Forecast | None = None
        self.output: Reservation | None = None
        self.grant: Grant | None = None
        self.tokens: dict[int,Token] = {}
        self.generations: dict[int,int] = {}
        self.revisions: dict[tuple[int,int],int] = {}
        self.poisoned: set[tuple[int,int]] = set()
        self.used: dict[tuple[int,int,int],int] = {}
        self.counters = dict.fromkeys(("reserved","dispatched","released","invalidated","revoked"),0)
        self.cursor = self.epoch = self.cfg_error = 0
        fee_default = FeePolicy(1,self.profile.qty_max,0)
        self.active = [fee_default] * self.profile.venues
        self.shadow = self.active.copy()

    def quote_view(self) -> QuoteBook:
        prior = copy(self.book)
        prior.quotes = self.book.quotes.copy()
        prior.disabled = self.book.disabled.copy()
        return prior

    def forecast(self, drive: Drive, active_tokens: dict[int,Token], quote_view: QuoteBook) -> Forecast:
        req = drive.base.query
        unavailable = [self.used.get((req.instrument,i,req.buy),0) for i in range(self.profile.venues)]
        for token in active_tokens.values():
            if (token.request.instrument,token.request.buy) == (req.instrument,req.buy):
                for child in token.plan.children:
                    unavailable[child.venue] += child.quantity
        ticks = {}
        revisions = {}
        for venue,amount in enumerate(unavailable):
            key = (req.instrument,venue)
            original = quote_view.quotes.get(key)
            if original:
                current = original.ask_qty if req.buy else original.bid_qty
                free = 0 if key in self.poisoned else max(0,current-amount)
                quote_view.quotes[key] = (
                    replace(original,ask_qty=free) if req.buy else replace(original,bid_qty=free)
                )
                ticks[venue] = original.tick
            revisions[venue] = self.revisions.get(key,0)
        result = plan(quote_view,self.active,self.epoch,req,drive.base.tick)
        return Forecast(len(self.samples)+result.scan_steps+1,req,drive.ttl,result,ticks,revisions,
                        bool(drive.base.commit))

    def decide(self, update: tuple[int,int] | None, tick: int, commit: bool,
               active_tokens: dict[int,Token]) -> Reservation:
        stage = self.pending
        plan_result = stage.result
        status = ReservationCode.HELD
        if plan_result.status >= SweepCode.BAD_INST:
            status = ReservationCode.NO_PLAN
        elif not 0 < stage.ttl < self.profile.time_half:
            status = ReservationCode.BAD_TTL
        elif not plan_result.children:
            status = ReservationCode.NO_PLAN
        elif stage.policy_changed or commit:
            status = ReservationCode.POLICY_CHANGED
        else:
            for child in plan_result.children:
                key = (stage.request.instrument,child.venue)
                if (update == key or key in self.poisoned
                        or self.revisions.get(key,0) != stage.revisions[child.venue]
                        or distance(tick,stage.updates[child.venue],self.profile.time_w) > stage.request.max_age):
                    status = ReservationCode.QUOTE_CHANGED
                    break
        token = None
        if status == ReservationCode.HELD:
            eligible = [i for i in range(self.slots) if i not in active_tokens
                        and self.generations.get(i,0) < self.token_max]
            if eligible:
                slot = min(eligible,key=lambda i:(i-self.cursor)%self.slots)
                token = Token(slot,self.generations.get(slot,0)+1,stage.request,tick,stage.ttl,plan_result,
                              {child.venue:stage.updates[child.venue] for child in plan_result.children})
            else:
                status = ReservationCode.NO_TOKEN
        return Reservation(status,plan_result,token)

    def action_result(self, request: Action, live: dict[int,Token]) -> Grant:
        if not 0 <= request.slot < self.slots:
            return Grant(request,GrantCode.BAD_ADDRESS)
        if request.generation == 0 or request.generation != self.generations.get(request.slot,0):
            return Grant(request,GrantCode.WRONG_GENERATION)
        if request.slot not in self.tokens:
            return Grant(request,GrantCode.NOT_HELD)
        if request.slot not in live:
            return Grant(request,GrantCode.REVOKED)
        return Grant(request,GrantCode.OK,live[request.slot])

    def step(self, drive: Drive) -> Sample:
        edge = len(self.cycles)
        self.cycles.append(drive)
        base = drive.base
        if base.reset:
            self.clear()
            entry = Sample(edge,0,0,0,0,0,0,0,None,None,None,False,(0,)*6)
            self.samples.append(entry)
            return entry

        cmd_ready = int(self.ack is None or base.ack_ready)
        cfg_ready = int(not base.commit)
        query_ready = int(self.pending is None and (self.output is None or base.result_ready))
        action_ready = self.grant is None
        prior_quotes = self.quote_view() if base.query and query_ready else None
        mutation = None
        ack_after = None
        if cmd_ready and base.command:
            command = base.command
            key = command.instrument,command.venue
            was_disabled = key in self.book.disabled
            ack_after = self.book.apply(command,base.tick)
            if ack_after.code == Code.OK and (
                command.op in (Op.REPLACE,Op.WITHDRAW)
                or was_disabled != (key in self.book.disabled)
            ):
                mutation = key
        active = {slot:token for slot,token in self.tokens.items()
                  if not token.obsolete(base.tick,self.profile,mutation,bool(base.commit))}
        applicable = bool(self.grant and self.grant.request.claim and self.grant.token and
                          active.get(self.grant.token.slot) == self.grant.token)
        metrics = (len(active),*self.counters.values())
        entry = Sample(edge,cmd_ready,cfg_ready,1,self.cfg_error,self.epoch,query_ready,int(action_ready),
                       self.ack,self.output,self.grant,applicable,metrics)

        new_action = self.action_result(drive.action,active) if drive.action and action_ready else None
        candidate = self.decide(mutation,base.tick,bool(base.commit),active) if (
            self.pending and self.pending.at == edge) else None
        if self.output:
            self.coverage["result_transfer" if base.result_ready else "result_stall"] += 1
            if base.result_ready:
                self.output = None
        if self.grant:
            self.coverage["grant_transfer" if drive.grant_ready else "grant_stall"] += 1
        if base.query:
            self.coverage["query_transfer" if query_ready else "query_stall"] += 1
        if base.command and not cmd_ready:
            self.coverage["command_stall"] += 1
        self.counters["invalidated"] += len(self.tokens)-len(active)
        self.tokens = active.copy()

        if candidate:
            self.output = candidate
            self.pending = None
            self.coverage[candidate.status.name] += 1
            if candidate.token:
                token = candidate.token
                self.tokens[token.slot] = token
                self.generations[token.slot] = token.generation
                self.cursor = (token.slot+1)%self.slots
                self.counters["reserved"] += 1
        elif self.pending and base.commit:
            self.pending = replace(self.pending,policy_changed=True)
        if prior_quotes is not None:
            self.pending = self.forecast(drive,active,prior_quotes)

        if self.grant and drive.grant_ready:
            grant = self.grant
            if applicable:
                token = grant.token
                for child in token.plan.children:
                    key = token.request.instrument,child.venue,token.request.buy
                    self.used[key] = self.used.get(key,0)+child.quantity
                del self.tokens[token.slot]
                self.counters["dispatched"] += 1
            elif grant.request.claim and grant.status == GrantCode.OK:
                self.counters["revoked"] += 1
            self.grant = None
        if new_action:
            self.coverage["grant_"+new_action.status.name] += 1
            self.grant = new_action
            if new_action.token and not new_action.request.claim:
                self.counters["released"] += 1
                del self.tokens[new_action.token.slot]

        if mutation:
            generation = self.revisions.get(mutation,0)
            if generation == self.revision_max:
                self.poisoned.add(mutation)
            else:
                self.revisions[mutation] = generation+1
            self.used.pop((*mutation,0),None)
            self.used.pop((*mutation,1),None)
        if cmd_ready:
            self.ack = (int(ack_after.code),ack_after.tag) if ack_after else None
        self.cfg_error = 0
        if base.commit:
            self.active,self.shadow = self.shadow,self.active
            self.epoch = (self.epoch+1)&255
        elif base.config:
            change = base.config
            if change.venue < self.profile.venues:
                self.shadow[change.venue] = FeePolicy(change.enabled,change.cap,change.fee)
            else:
                self.cfg_error = 1
        self.samples.append(entry)
        return entry
