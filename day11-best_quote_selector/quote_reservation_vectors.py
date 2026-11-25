"""Check reserving sweeps and dispatch at every sampled RTL edge."""

from __future__ import annotations

import argparse
import random
from dataclasses import replace
from itertools import zip_longest
from pathlib import Path

from quote_reservation_model import Action, Drive, GrantCode, ReservationCode as RC, ReservingMachine
from quote_schema import Command, PROFILES, Profile, mask
from quote_sweep_model import SweepCode, SweepQuery
from quote_sweep_vectors import Config, Cycle, random_command, random_config, random_query
from quote_tool import simulation


GEOMETRY = {"default":(3,4,3), "solo":(1,4,3), "odd":(3,4,3), "wide":(5,4,3)}


class Scenario:
    def __init__(self,machine: ReservingMachine):
        self.machine = machine
        self.profile = machine.profile
        self.tick = 0
        self.tag = 0

    def next_tag(self) -> int:
        self.tag = (self.tag+1)&mask(self.profile.tag_w)
        return self.tag

    def edge(self,*,tick=None,advance=1,ttl=0,grant_ready=1,action=None,**fields):
        self.tick = (self.tick+advance if tick is None else tick)&mask(self.profile.time_w)
        return self.machine.step(Drive(Cycle(self.tick,**fields),ttl,grant_ready,action))

    def reset(self,tick=0):
        self.edge(reset=1,tick=tick)

    def show(self,venue=0,quantity=10,price=100,*,advance=1,**changes):
        initial = self.machine.book.last_sequences.get((0,venue),0)
        sequence = (initial+1)&mask(self.profile.seq_w)
        command = Command.replace(0,venue,sequence,(price-3,quantity),(price,quantity),self.next_tag())
        old = self.edge(command=command,advance=advance,**changes)
        assert old.cmd_ready
        return old

    def request(self,quantity=1,*,all_or_none=0,buy=1,max_age=None,venues=None) -> SweepQuery:
        limit = min(200,self.profile.price_max) if buy else 1
        return SweepQuery(0,self.next_tag(),buy,quantity,limit,
                          self.profile.time_half-1 if max_age is None else max_age,
                          self.profile.venue_mask if venues is None else venues,all_or_none)

    def wait_result(self,want,**kw):
        for _ in range(self.profile.venues+6):
            seen = self.edge(**kw)
            if seen.reservation is not None:
                assert seen.reservation.status == want, (want,seen.reservation)
                return seen.reservation
        raise AssertionError("no reservation result")

    def reserve(self,amount=1,*,want=RC.HELD,ttl=None,advance=1,**kwargs):
        request = self.request(amount,**kwargs)
        assert self.edge(query=request,ttl=self.profile.time_half-1 if ttl is None else ttl,
                         advance=advance).query_ready
        return self.wait_result(want,advance=advance)

    def offer(self,token,*,claim=1,generation=None,grant_ready=0,expected=GrantCode.OK,advance=1):
        action = Action(claim,self.next_tag(),token.slot,token.generation if generation is None else generation)
        assert self.edge(action=action,grant_ready=grant_ready,advance=advance).action_ready
        seen = self.edge(grant_ready=grant_ready,advance=advance)
        assert seen.grant is not None and seen.grant.status == expected, (expected,seen.grant)
        return seen

    def drain(self):
        for _ in range(self.profile.venues+10):
            self.edge()
            if self.machine.pending is None and self.machine.output is None and self.machine.grant is None:
                return
        raise AssertionError("drain timeout")


def dispatch_race(s: Scenario) -> None:
    """Exercise reserved/consumed inventory and a mutation on actual dispatch."""
    s.reset()
    s.show(quantity=10)
    multi = s.profile.venues > 1
    if multi:
        s.show(1,7,103)
    first = s.reserve(12 if multi else 7,all_or_none=1)
    assert first.result.quantity == (12 if multi else 7)
    if multi:
        second = s.reserve(9)
        assert second.result.status == SweepCode.PARTIAL and second.result.quantity == 5
        missed = s.reserve(1,all_or_none=1,want=RC.NO_PLAN)
        assert missed.result.status == SweepCode.AON_UNFILLED
        s.offer(second.token,claim=0,grant_ready=1)
    else:
        s.reserve(1,want=RC.NO_TOKEN)

    pending = s.offer(first.token)
    assert pending.applicable
    s.edge(grant_ready=0)
    cut = s.show(quantity=9,grant_ready=1)
    assert cut.grant == pending.grant and not cut.applicable
    assert s.machine.counters["dispatched"] == 0
    assert s.machine.counters["revoked"] == 1

    replacement = s.reserve(14 if multi else 8,all_or_none=1)
    quantities = tuple(child.quantity for child in replacement.result.children)
    assert quantities == ((9,5) if multi else (8,))
    committed = s.offer(replacement.token,grant_ready=1)
    assert committed.applicable
    remainder = s.reserve(8)
    assert remainder.result.quantity == (2 if multi else 1)
    s.offer(remainder.token,claim=0,grant_ready=1)
    untouched_bid = s.reserve(7,buy=0)
    assert untouched_bid.result.quantity == 7
    s.offer(untouched_bid.token,claim=0,grant_ready=1)
    s.drain()


def validation_races(s: Scenario) -> None:
    s.reset()
    s.show(quantity=12)
    now = s.request(5,max_age=7)
    same_edge = Command.replace(0,0,2,(98,9),(101,9),s.next_tag())
    s.edge(command=same_edge,query=now,ttl=20)
    rejected = s.wait_result(RC.QUOTE_CHANGED)
    assert rejected.token is None and rejected.result.status == SweepCode.FILLED

    now = s.request(5)
    s.edge(commit=1,query=now,ttl=20)
    assert s.wait_result(RC.POLICY_CHANGED).token is None
    s.reserve(1,ttl=0,want=RC.BAD_TTL)
    s.reserve(1,ttl=s.profile.time_half,want=RC.BAD_TTL)
    s.reserve(1,max_age=s.profile.time_half,want=RC.NO_PLAN)

    # Start with a fresh quote, then cross the age boundary while computing.
    s.show(quantity=12)
    s.edge(query=s.request(1,max_age=1),ttl=10)
    assert s.wait_result(RC.QUOTE_CHANGED).token is None

    valid = s.reserve(4)
    s.offer(valid.token)
    assert not s.edge(commit=1,grant_ready=1).applicable
    assert s.machine.counters["revoked"] == 1
    s.drain()


def wrapped_feed_serial(s: Scenario) -> None:
    s.reset()
    last = mask(s.profile.seq_w)
    at_last = Command.replace(0,0,last,(90,9),(100,9),s.next_tag())
    assert s.edge(command=at_last).cmd_ready
    first = s.reserve(7)
    pending = s.offer(first.token)
    duplicate = replace(at_last,tag=s.next_tag(),ask_qty=3)
    same = s.edge(command=duplicate,grant_ready=0)
    assert same.applicable and same.grant == pending.grant
    if s.profile.instruments > 1:
        other = Command.replace(1,0,1,(90,9),(100,9),s.next_tag())
        assert s.edge(command=other,grant_ready=0).applicable
    wrapped = replace(at_last,sequence=0,tag=s.next_tag(),ask_qty=8)
    assert not s.edge(command=wrapped,grant_ready=1).applicable
    second = s.reserve(8,all_or_none=1)
    assert second.result.quantity == 8


def idempotent_enable_preserves_consumption(s: Scenario) -> None:
    s.reset()
    s.show(quantity=11)
    result = s.reserve(7)
    assert s.offer(result.token,grant_ready=1).applicable
    s.edge(command=Command.venue_enabled(0,0,True,s.next_tag()))
    left = s.reserve(11)
    assert len(left.result.children) == 1 and left.result.quantity == 4
    s.offer(left.token)
    assert s.edge(command=Command.venue_enabled(0,0,True,s.next_tag()),grant_ready=1).applicable
    assert s.reserve(1,want=RC.NO_PLAN).token is None


def expiry_and_wrap(s: Scenario) -> None:
    maximum = mask(s.profile.time_w)
    s.reset(maximum-9)
    s.show(quantity=12,advance=0)
    chosen = s.reserve(6,ttl=12,advance=0)
    s.offer(chosen.token,advance=0)
    deadline = (chosen.token.birth+chosen.token.ttl)&maximum
    prior = s.edge(tick=deadline-1,grant_ready=0)
    assert prior.applicable and prior.counts[0] == 1
    final = s.edge(tick=deadline,grant_ready=1)
    assert not final.applicable and final.counts[0] == 0
    assert s.machine.counters["invalidated"] == 1
    assert s.reserve(12,advance=0).result.quantity == 12
    s.drain()


def exhaustion(s: Scenario) -> None:
    s.reset(10)
    s.show(quantity=12,advance=0)
    for round_id in range(s.machine.token_max):
        tokens = [s.reserve(1,advance=0).token for _ in range(s.machine.slots)]
        assert all(token.generation == round_id+1 for token in tokens)
        s.reserve(1,want=RC.NO_TOKEN,advance=0)
        for token in tokens:
            s.offer(token,claim=0,grant_ready=1,advance=0)
    s.reserve(1,want=RC.NO_TOKEN,advance=0)
    s.offer(tokens[-1],grant_ready=1,expected=GrantCode.NOT_HELD,advance=0)
    s.offer(tokens[-1],generation=1,grant_ready=1,expected=GrantCode.WRONG_GENERATION,advance=0)
    bad = replace(tokens[0],slot=s.machine.slots)
    s.offer(bad,grant_ready=1,expected=GrantCode.BAD_ADDRESS,advance=0)

    s.reset(10)
    for _ in range(s.machine.revision_max):
        s.show(quantity=12,advance=0)
    token = s.reserve(3,advance=0).token
    old = s.offer(token,advance=0)
    assert old.applicable
    s.show(quantity=12,advance=0,grant_ready=0)
    assert not s.edge(grant_ready=1,advance=0).applicable
    s.reserve(3,want=RC.NO_PLAN,advance=0)
    s.drain()


def random_load(s: Scenario,seed: int,edges: int) -> None:
    rng = random.Random(seed)
    m,p = s.machine,s.profile
    s.reset(mask(p.time_w)-3)
    cmd = query = config = action = None
    ttl = 0
    for index in range(edges):
        if index and index % 311 == 0:
            s.reset(s.tick)
            cmd = query = config = action = None
            continue
        if cmd is None and rng.randrange(4) not in (0,1):
            cmd = random_command(m,rng,s.next_tag())
        if query is None and rng.randrange(4):
            query = random_query(m,rng,s.next_tag())
            ttl = rng.choice((0,5,11,p.time_half-1,p.time_half))
        if config is None and rng.randrange(9) == 0:
            config = random_config(m,rng)
        if action is None and rng.randrange(4) == 0:
            slot = rng.choice(list(m.tokens)) if m.tokens else rng.randrange(m.slots+1)
            generation = m.generations.get(slot,0)
            if rng.randrange(4) == 0:
                generation ^= 1
            action = Action(rng.randrange(2),s.next_tag(),slot,generation)
        seen = s.edge(command=cmd,query=query,config=config,action=action,ttl=ttl,
                      commit=int(rng.randrange(13)==0),grant_ready=int(rng.randrange(5)>1),
                      ack_ready=int(rng.randrange(4)>0),result_ready=int(rng.randrange(5)>1),
                      advance=int(rng.randrange(3)>0))
        if seen.cmd_ready:
            cmd = None
        if seen.query_ready:
            query = None
        if seen.cfg_ready:
            config = None
        if seen.action_ready:
            action = None
    for _ in range(p.venues+12):
        seen = s.edge(command=cmd,query=query,config=config,action=action,ttl=ttl)
        if seen.cmd_ready:
            cmd = None
        if seen.query_ready:
            query = None
        if seen.cfg_ready:
            config = None
        if seen.action_ready:
            action = None
        if all(x is None for x in (cmd,query,config,action)):
            break
    else:
        raise AssertionError("accepted input queues did not drain")
    s.drain()


def compare(machine: ReservingMachine,trace: Path) -> None:
    with trace.open(encoding="ascii") as handle:
        for expected,actual in zip_longest(machine.samples,handle):
            if expected is None or actual is None:
                raise AssertionError("different sample count")
            wanted = expected.encode(machine.profile,machine.fee_width)
            if wanted != actual.strip():
                raise AssertionError(f"cycle {expected.number}\nRTL {actual.strip()}\n"
                                     f"ref {wanted}\ndrive {machine.cycles[expected.number]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile",choices=PROFILES,default="default")
    parser.add_argument("--image",required=True,type=Path)
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--vvp",default="vvp")
    parser.add_argument("--cycles",type=int,default=2200)
    parser.add_argument("--seed",type=lambda v:int(v,0),default=0x8711)
    parser.add_argument("--demo",action="store_true")
    parser.add_argument("--waveform",type=Path)
    args = parser.parse_args()
    if not 0 <= args.cycles <= 200_000:
        parser.error("cycles must be between 0 and 200,000")
    m = ReservingMachine(PROFILES[args.profile],*GEOMETRY[args.profile])
    scenario = Scenario(m)
    dispatch_race(scenario)
    if not args.demo:
        validation_races(scenario)
        wrapped_feed_serial(scenario)
        idempotent_enable_preserves_consumption(scenario)
        expiry_and_wrap(scenario)
        exhaustion(scenario)
        random_load(scenario,args.seed,args.cycles)
        if any(not m.coverage[code.name] for code in RC):
            raise AssertionError("missing a directed reservation status")
    args.output.mkdir(exist_ok=True,parents=True)
    driver = args.output / "drive.txt"
    trace = args.output / "trace.txt"
    driver.write_text("".join(drive.encode()+"\n" for drive in m.cycles),encoding="ascii")
    trace.unlink(missing_ok=True)
    simulation(args.vvp,args.image,driver,trace,args.waveform)
    compare(m,trace)
    print(f"PASS {args.profile} exact reservation scoreboard, {len(m.cycles)} edges")
    print("  " + " ".join(f"{k}={v}" for k,v in sorted(m.coverage.items())))


if __name__ == "__main__":
    main()
