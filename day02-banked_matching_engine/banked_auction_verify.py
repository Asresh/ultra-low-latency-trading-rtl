"""Pin-level cohort cutoff checks against an independent cycle model."""

import argparse
from collections import Counter
from dataclasses import replace
from itertools import zip_longest
from pathlib import Path
from random import Random
import subprocess

from auction_model import Command, Kind, Op
from banked_auction_model import (
    Control, Controller, ControlOp, ControlStatus, Detail, Input, PROFILES, Profile, Wrapper,
)


def hexline(values):
    return " ".join(f"{int(value):x}" for value in values)+"\n"


class Fixture:
    def __init__(self,profile):
        self.p = profile
        self.model = Controller(profile)
        self.inputs = []
        self.expected = []
        self.metrics = Counter()
        self.events = []
        self.reports = []

    def edge(self, **fields):
        request = Input(**fields)
        state = self.model.step(request)
        self.inputs.append(request.words(self.p))
        self.expected.append(state.words(self.p))
        flags = self.metrics
        flags["cycles"] += 1
        if request.command is not None:
            flags["commands" if state.source_ready else "blocked_inputs"] += 1
            if state.source_ready and request.bank < self.p.banks and state.due>>request.bank&1:
                flags["late_inputs"] += 1
        if request.reset and state.armed:
            flags["schedules_reset"] += 1
        if state.output:
            if request.output_ready:
                self.events.append(((len(self.inputs)-1)*10+15,state.output))
                flags[Wrapper(state.output.wrapper).name] += 1
                if state.output.scheduled:
                    flags["scheduled_events"] += 1
                    if state.output.result.last:
                        flags["scheduled_terminals"] += 1
                if state.output.event_sequence == self.p.seq_mask:
                    flags["sequence_wraps"] += 1
            else:
                flags["held_egress"] += 1
        if state.report:
            if request.control_ready:
                self.reports.append(state.report)
                flags[ControlStatus(state.report.status).name] += 1
                for key in range(self.p.banks):
                    if (state.report.control.mask>>key)&1:
                        detail = state.report.detail >> (key*3)&7
                        flags["detail_"+Detail(detail).name] += 1
            else:
                flags["held_control"] += 1
        if bin(state.due).count("1") >= 2:
            flags["cohort_edges"] += 1
        if state.clock == self.p.maximum_clock:
            flags["saturated_clock"] += 1
        return state

    def command(self,bank,command,output_ready=True):
        for _ in range(250):
            if self.edge(bank=bank,command=command,output_ready=output_ready).source_ready:
                return
        raise AssertionError("test source failed to transfer")

    def manage(self,control):
        for _ in range(250):
            if self.edge(control=control).control_ready:
                return
        raise AssertionError("test control failed to transfer")

    def idle(self,cycles,output_ready=True):
        for _ in range(cycles):
            self.edge(output_ready=output_ready)

    def drain(self):
        bound = 8+self.p.banks*(self.p.events+self.p.commands+2*self.p.auction.slots+self.p.auction.prices)*5
        for _ in range(bound):
            if all(not (l.input or l.output or l.active or l.schedule) for l in self.model.lanes) and (
                not self.model.reject and self.model.report is None
            ):
                self.edge()
                return
            self.edge()
        raise AssertionError("checked queues did not drain")


def open_auction(work,bank,epoch=1,tag=0):
    p = work.p.auction.prices
    work.command(bank,Command(Op.OPEN,epoch,tag,price=p//2,high=p-1))


def order(epoch,id,side,price,quantity,tag=0):
    return Command(Op.ADD,epoch,tag,id=id,side=side,owner=1+side,price=price,quantity=quantity)


def worked(p):
    work = Fixture(p)
    mask = 3
    work.edge(reset=True)
    for bank in range(3):
        open_auction(work,bank,tag=0x20+bank)
        work.command(bank,order(1,0,0,5,8,0x30+bank))
        work.command(bank,order(1,1,1,4,4,0x40+bank))
    work.idle(6)
    deadline = work.model.clock+14
    work.manage(Control(ControlOp.ARM,mask,(1,2,0),deadline,0xa0))
    work.manage(Control(ControlOp.ARM,mask,(1,1,0),deadline,0xa1))
    work.command(0,order(1,2,1,4,3,0xb0))
    work.command(1,Command(Op.ABORT,1,0xb1))
    while work.model.clock < deadline:
        work.edge(output_ready=False)
    work.edge(control=Control(ControlOp.CANCEL,mask,(1,1,0),tag=0xa2),output_ready=False)
    # The cutoff is reached, independently of the shared event channel.
    work.command(0,order(1,3,1,4,8,0xc0),output_ready=False)
    work.command(1,order(1,3,1,4,8,0xc1),output_ready=False)
    work.command(2,order(1,2,1,4,2,0xc2))
    work.command(2,Command(Op.CLOSE,1,0xc3))
    work.drain()

    terminals = [(event.bank,event.result.tag,event.result.quantity) for _,event in work.events
                 if event.scheduled and event.result.kind == Kind.DONE]
    if sorted(terminals) != [(0,0xa1,7),(1,0xa1,4)]:
        raise AssertionError(f"unexpected scheduled terminal results: {terminals}")
    for bank in (0,1):
        stream = [event for _,event in work.events if event.bank == bank]
        close = next(i for i,event in enumerate(stream) if event.scheduled and event.result.kind==Kind.DONE)
        late = next(i for i,event in enumerate(stream) if event.wrapper==Wrapper.CUTOFF)
        if close >= late:
            raise AssertionError("cutoff audit event overtook the scheduled close")
    if [r.status for r in work.reports] != [ControlStatus.ROLLBACK,ControlStatus.OK,ControlStatus.ROLLBACK]:
        raise AssertionError("cohort application and rollback were not distinguished")
    return work


def directed(p):
    w = Fixture(p)
    w.edge(reset=True)
    w.manage(Control(ControlOp.ARM,0,cutoff=3))
    w.manage(Control(ControlOp.CANCEL,1,(1,)*p.banks))
    w.manage(Control(ControlOp.ARM,1,(1,)*p.banks,w.model.clock+3))
    w.command(p.banks,Command(Op.ABORT,1,7))
    open_auction(w,0)
    w.idle(4)
    w.manage(Control(ControlOp.ARM,1,(2,)*p.banks,w.model.clock+7))
    w.manage(Control(ControlOp.ARM,1,(1,)*p.banks,w.model.clock))
    w.command(0,order(1,0,0,p.auction.prices-1,2,0x15))
    w.manage(Control(ControlOp.ARM,1,(1,)*p.banks,w.model.clock+7))
    w.idle(4)
    w.manage(Control(ControlOp.ARM,1,(1,)*p.banks,w.model.clock+16,0x40))
    w.manage(Control(ControlOp.ARM,1,(1,)*p.banks,w.model.clock+17))
    w.command(0,Command(Op.CLOSE,1,0x41))
    w.manage(Control(ControlOp.CANCEL,1,(1,)*p.banks,tag=0x42))
    w.idle(4)
    w.manage(Control(ControlOp.ARM,1,(1,)*p.banks,w.model.clock+8,0x43))
    w.edge(reset=True)
    open_auction(w,0)
    w.command(0,order(1,0,0,p.auction.prices-1,4))
    w.command(0,order(1,1,1,0,4))
    w.idle(5)
    seal = w.model.clock+3
    w.manage(Control(ControlOp.ARM,1,(1,)*p.banks,seal,0x49))
    while w.model.clock < seal:
        w.edge(output_ready=False,control_ready=False)
    w.command(0,Command(Op.INSPECT,1,0x50,owner=1))
    w.drain()
    return w


def pressure(p):
    """Hold a live offer over cutoff while one bank's input and output FIFOs are full."""
    w = Fixture(p)
    w.edge(reset=True)
    for bank in (0,1):
        open_auction(w,bank)
        w.command(bank,order(1,0,0,1,8))
        w.command(bank,order(1,1,1,0,5))
    w.idle(5)
    cutoff = w.model.clock+13
    w.manage(Control(ControlOp.ARM,3,(1,1),cutoff,0xa1))
    for index in range(2):
        w.command(0,Command(Op.INSPECT,1,0xb0+index,id=0,owner=1),output_ready=False)
    w.command(0,Command(Op.REDUCE,1,0xb2,id=0,owner=1,quantity=3),output_ready=False)
    late = order(1,2,0,1,9,0xc0)
    blocked_pre = blocked_post = 0
    until = cutoff+3
    while w.model.clock < until:
        sample = w.edge(bank=0,command=late,output_ready=False)
        if sample.source_ready:
            raise AssertionError("pre-cutoff offer accepted without either FIFO credit")
        if sample.clock < cutoff:
            blocked_pre += 1
        else:
            blocked_post += 1
    if not blocked_pre or not blocked_post or w.model.lanes[0].schedule.inserted or (
        not w.model.lanes[1].schedule.inserted
    ):
        raise AssertionError("pressure fixture failed to block only one automatic marker")
    w.command(0,late)
    w.drain()
    stream = [event for _,event in w.events if event.bank==0]
    reduced = next(index for index,event in enumerate(stream) if event.result.tag==0xb2)
    later = next(index for index,event in enumerate(stream) if event.result.tag==0xc0)
    finished = next(index for index,event in enumerate(stream)
                    if event.scheduled and event.result.kind==Kind.DONE)
    if not reduced < finished < later or stream[later].wrapper!=Wrapper.CUTOFF:
        raise AssertionError("held cutoff was evaluated before acceptance or reordered")
    if stream[finished].result.quantity!=3:
        raise AssertionError("scheduled allocation included something outside the accepted prefix")
    return w


def next_order(rng,profile,state,tag):
    if state.book.band is None:
        p = profile.auction.prices
        return Command(Op.OPEN,(state.book.epoch+1)&profile.auction.epoch_mask,tag,price=p//2,high=p-1)
    epoch = state.book.epoch
    op = rng.choices((Op.ADD,Op.CANCEL,Op.INDICATE,Op.CLOSE,Op.ABORT,Op.INSPECT),
                     (48,12,12,8,3,17))[0]
    if op in (Op.CLOSE,Op.ABORT,Op.INDICATE):
        return Command(op,epoch,tag)
    identifier = rng.randrange(profile.auction.slots)
    if op == Op.ADD:
        return order(epoch,identifier,rng.randrange(2),rng.randrange(profile.auction.prices),
                     rng.randint(1,min(31,profile.auction.quantity_max)),tag)
    row = state.book.orders.get(identifier)
    return Command(op,epoch,tag,id=identifier,owner=row.owner if row else 1)


def randomize(w,seed,cycles):
    rng = Random(seed)
    p = w.p
    cmd = ctl = None
    bank = 0
    for cycle in range(cycles):
        if cycle and cycle%191 == 0:
            w.edge(reset=True)
            cmd = ctl = None
        if cmd is None and rng.randrange(11) != 0:
            bank = rng.randrange(p.banks+int(rng.randrange(29)==0))
            lane = w.model.lanes[min(bank,p.banks-1)]
            cmd = next_order(rng,p,lane,cycle & ((1<<p.auction.tag_bits)-1))
        if ctl is None and rng.randrange(9) == 0:
            op = rng.choice(tuple(ControlOp))
            choices = [i for i,lane in enumerate(w.model.lanes)
                       if lane.open_epoch is not None and not (lane.input or lane.active or lane.schedule)]
            mask = rng.randrange(1<<p.banks)
            if op == ControlOp.ARM and choices and rng.randrange(6):
                mask = sum(1<<i for i in choices)
            deadline = min(w.model.clock+rng.randrange(25),p.maximum_clock) if op==ControlOp.ARM else 0
            epochs = tuple(lane.open_epoch or 0 for lane in w.model.lanes)
            ctl = Control(op,mask,epochs,deadline,cycle & ((1<<p.auction.tag_bits)-1))
        sample = w.edge(bank=bank,command=cmd,control=ctl,
                        output_ready=rng.randrange(5)!=0 and cycle%57 < 45,
                        control_ready=rng.randrange(5)!=0 and cycle%61 < 52)
        if sample.source_ready:
            cmd = None
        if sample.control_ready:
            ctl = None
    while cmd is not None or ctl is not None:
        sample = w.edge(bank=bank,command=cmd,control=ctl)
        if sample.source_ready:
            cmd = None
        if sample.control_ready:
            ctl = None
    w.drain()
    return w


def simulate(work,args,name):
    folder = args.build/name
    folder.mkdir(parents=True,exist_ok=True)
    fixture,observed,binary = (folder/"input.hex",folder/"observed.hex",folder/"banked.vvp")
    fixture.write_text("".join(map(hexline,work.inputs)),encoding="ascii")
    top = "tb_banked_call_auction"
    cmd = [args.iverilog,"-g2012","-s",top,"-o",str(binary)]
    cmd.extend(f"-P{top}.{key}={value}" for key,value in work.p.parameters().items())
    cmd.extend(("-f","rtl_auction_banked.f",f"{top}.sv"))
    subprocess.run(cmd,check=True)
    cmd = [args.vvp,str(binary),f"+input={fixture}",f"+output={observed}"]
    if args.wave:
        args.wave.parent.mkdir(parents=True,exist_ok=True)
        cmd.append(f"+wave={args.wave}")
    subprocess.run(cmd,check=True)
    with observed.open(encoding="ascii") as actual:
        for edge,(wanted,record) in enumerate(zip_longest(work.expected,actual)):
            if wanted is None or record is None:
                raise AssertionError("observed pin count differs")
            got = tuple(int(token,16) for token in record.split())
            if got != wanted:
                fields = [(i,x,y) for i,(x,y) in enumerate(zip(wanted,got)) if x!=y]
                raise AssertionError(f"{name} edge {edge}: mismatched fields {fields}")
    print(f"{name}: {dict(work.metrics)}",flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iverilog",default="iverilog")
    parser.add_argument("--vvp",default="vvp")
    parser.add_argument("--build",type=Path,default=Path("build/auction-banked"))
    parser.add_argument("--example",action="store_true")
    parser.add_argument("--pressure",action="store_true")
    parser.add_argument("--wave",type=Path)
    parser.add_argument("--cycles",type=int,default=3000)
    parser.add_argument("--seed",type=int,default=225513)
    args = parser.parse_args()
    if args.wave and not args.example:
        parser.error("wave export requires --example")
    if args.cycles < 10:
        parser.error("cycles must be at least 10")
    if args.example:
        work = worked(PROFILES["default"])
        simulate(work,args,"example")
        print([(edge,event.bank,Kind(event.result.kind).name,event.result.tag,int(event.wrapper))
                for edge,event in work.events if event.scheduled or event.wrapper])
    elif args.pressure:
        tight = Profile(PROFILES["solo"].auction,2,1,1,4,8)
        simulate(pressure(tight),args,"pressure")
    else:
        for name,p in PROFILES.items():
            work = randomize(directed(p),args.seed,args.cycles)
            simulate(work,args,name)


if __name__ == "__main__":
    main()
