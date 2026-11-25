"""Generate exact scheduler tapes and check real RTL across four geometries."""

import argparse
from collections import Counter
from dataclasses import replace
from pathlib import Path
from random import Random
import subprocess

from deferred_model import Control, DeferredScheduler, Offer, Op, PROFILES, Status


class Workload:
    def __init__(self, geometry):
        self.g = geometry
        self.model = DeferredScheduler(geometry)
        self.edges = []
        self.results = []
        self.metrics = Counter()

    def edge(self, *, reset=False, ready=True, admin=None, offer=None):
        if reset:
            self.metrics["flushed_messages"] += sum(len(row.queue) for row in self.model.tenant)
            self.metrics["flushed_results"] += self.model.pending is not None
        pins = self.model.step(reset,ready,admin,offer)
        self.edges.append(
            f"{int(reset)} {int(ready)} {int(admin is not None)} {admin.pack(self.g) if admin else 0:x} "
            f"{int(offer is not None)} {offer.pack(self.g) if offer else 0:x} "
            f"{int(pins.admin_ready)} {int(pins.source_ready)} {int(pins.rejected)} {int(pins.result is not None)} "
            f"{pins.result.pack(self.g) if pins.result else 0:x} {pins.occupancy:x}\n"
        )
        self.metrics["cycles"] += 1
        if offer is not None:
            if not pins.source_ready:
                self.metrics["stalled_ingress"] += 1
            elif pins.rejected:
                self.metrics["invalid_ingress"] += 1
            else:
                self.metrics["accepted_ingress"] += 1
                if pins.occupancy >> (self.g.occupancy_bits*offer.session) & ((1<<self.g.occupancy_bits)-1) == self.g.depth:
                    self.metrics["full_replace"] += 1
        if pins.result:
            if ready:
                self.metrics[Status(pins.result.status).name] += 1
                self.results.append(((len(self.edges)-1)*10+15,pins.result))
            else:
                self.metrics["stalled_egress"] += 1
        return pins

    def admin(self, control):
        while not self.edge(admin=control).admin_ready:
            pass

    def offer(self, offer):
        for _ in range(1 << (self.g.ttl_bits+1)):
            if self.edge(offer=offer).source_ready:
                return
        raise AssertionError("test workload could not enqueue before timeout")

    def idle(self,count,ready=True):
        for _ in range(count):
            self.edge(ready=ready)

    def drain(self):
        for _ in range((1<<self.g.ttl_bits)+2*self.g.sessions*self.g.depth):
            if not self.model.occupancy and self.model.pending is None:
                self.edge()
                return
            self.edge()
        raise AssertionError("test workload failed to drain")


def example(g):
    """One tenant's exhausted budget cannot hold another tenant's data."""
    work = Workload(g)
    work.edge(reset=True)
    work.admin(Control(Op.SET_PORT,capacity=40,rate=1,enable=True,tag=0xa0))
    work.admin(Control(Op.SET_GROUP,group=0,capacity=3,enable=True,tag=0xa1))
    work.admin(Control(Op.SET_GROUP,group=1,capacity=10,rate=2,enable=True,tag=0xa2))
    work.admin(Control(Op.SET_SESSION,session=0,group=0,capacity=12,quantum=4,enable=True,tag=0xa3))
    work.admin(Control(Op.SET_SESSION,session=1,group=1,capacity=12,rate=3,quantum=2,enable=True,tag=0xa4))
    work.offer(Offer(0,3,24,0xb0,0xff0))
    work.offer(Offer(0,1,38,0xb1,0xff1))
    work.offer(Offer(1,2,31,0xc0,0xff2))
    work.offer(Offer(1,4,64,0xc1,0xff3))
    work.idle(4)
    work.idle(7,ready=False)
    work.drain()
    result = [(reply.tag,reply.status) for _,reply in work.results if not reply.admin]
    want = [(0xb0,Status.ADMITTED),(0xc0,Status.ADMITTED),(0xc1,Status.ADMITTED),(0xb1,Status.EXPIRED)]
    if result != want:
        raise AssertionError(f"worked example differs: {result}")
    return work


def directed(g):
    work = Workload(g)
    limit = (1 << g.ttl_bits)-1
    cap = min(24,(1<<g.token_bits)-1)
    work.edge(reset=True)
    work.offer(Offer(g.sessions,1,1,0x10))
    work.offer(Offer(0,0,1,0x11))
    work.offer(Offer(0,1,0,0x12))
    work.offer(Offer(0,1,limit,0x13))
    work.drain()
    work.admin(Control(Op.SET_PORT,capacity=cap,rate=cap,reserve=2,enable=True))
    for i in range(g.groups):
        work.admin(Control(Op.SET_GROUP,group=i,capacity=cap,rate=cap,enable=True))
    for i in range(g.sessions):
        work.admin(Control(Op.SET_SESSION,session=i,group=i%g.groups,capacity=cap,
                           rate=cap,quantum=cap,enable=True,critical=bool(i%2)))
    work.admin(Control(Op.GET_SESSION,session=g.sessions))
    work.admin(Control(Op.SET_GROUP,group=g.groups,capacity=1,enable=True))
    work.admin(Control(Op.SET_PORT,capacity=1,reserve=2,enable=True))
    work.admin(Control(7))
    work.offer(Offer(0,cap,limit,0x14))
    work.drain()
    # Queue while the management path uses the result slot.
    work.edge(admin=Control(Op.GET_PORT),offer=Offer(0,1,limit,0x15))
    work.admin(Control(Op.SET_SESSION,session=0))
    work.drain()
    # Freeze output while a live result and a queued short-lived successor coexist.
    work.edge(admin=Control(Op.GET_PORT))
    work.edge(ready=False,offer=Offer(0,1,1,0x16))
    work.idle(5,ready=False)
    work.drain()
    work.edge(admin=Control(Op.GET_PORT))
    work.edge(ready=False,offer=Offer(0,1,limit,0x17))
    work.edge(reset=True)
    work.drain()
    return work


def randomize(work, seed, clocks):
    rng = Random(seed)
    g = work.g
    max_token = (1 << g.token_bits)-1
    max_ttl = (1 << g.ttl_bits)-1
    tag_mask = (1 << g.tag_bits)-1
    cap = min(40,max_token)
    source = None
    config = None

    def bootstrap():
        work.admin(Control(Op.SET_PORT,capacity=cap,rate=cap//5,reserve=2,enable=True))
        for group in range(g.groups):
            work.admin(Control(Op.SET_GROUP,group=group,capacity=cap,rate=cap//7,enable=True))
        for session in range(g.sessions):
            work.admin(Control(Op.SET_SESSION,session=session,group=session%g.groups,capacity=cap,
                               rate=max(1,cap//11),quantum=max(1,cap//9),enable=True,critical=session%3==0))

    bootstrap()
    for cycle in range(clocks):
        if cycle and cycle % 463 == 0:
            work.edge(reset=True)
            source = None
            config = None
            bootstrap()
        if config is None and rng.randrange(17) == 0:
            code = rng.randrange(8)
            config = Control(code,tag=cycle&tag_mask)
            if code in (Op.SET_GROUP,Op.GET_GROUP):
                config = replace(config,group=rng.randrange(g.groups+1))
            elif code in (Op.SET_SESSION,Op.GET_SESSION):
                config = replace(config,session=rng.randrange(g.sessions+1))
            if code <= Op.SET_SESSION and rng.randrange(5) != 0:
                n = rng.randint(1,max_token)
                config = replace(config,capacity=n,rate=rng.randrange(n+1),enable=True)
                if code == Op.SET_SESSION:
                    config = replace(config,group=rng.randrange(g.groups),
                                     quantum=rng.randint(1,max_token),critical=bool(rng.randrange(2)))
                else:
                    config = replace(config,reserve=rng.randrange(n+1))
        if source is None and rng.randrange(8) != 0:
            ttl = rng.choice((1,max_ttl,min(max_ttl,17),rng.randrange(max_ttl+1)))
            cost = rng.choice((1,2,max(1,cap//7),rng.randrange(max_token+1),rng.randrange(cap+1)))
            source = Offer(rng.randrange(g.sessions+int(rng.randrange(22)==0)),cost,
                           ttl,cycle&tag_mask,rng.getrandbits(g.data_bits))
        ready = cycle % 71 <= 56 and rng.randrange(5) != 0
        pins = work.edge(ready=ready,admin=config,offer=source)
        if pins.admin_ready:
            config = None
        if pins.source_ready:
            source = None
    while config is not None or source is not None:
        pins = work.edge(admin=config,offer=source)
        if pins.admin_ready:
            config = None
        if pins.source_ready:
            source = None
    work.drain()
    return work


def simulate(g, work, paths, args):
    paths.mkdir(parents=True,exist_ok=True)
    source = paths/"cycles.txt"
    binary = paths/"scheduler.vvp"
    source.write_text("".join(work.edges),encoding="ascii")
    top = "tb_deferred_rate_scheduler"
    compile_cmd = [args.iverilog,"-g2012","-s",top,"-o",str(binary)]
    compile_cmd.extend(f"-P{top}.{k}={v}" for k,v in g.parameters().items())
    compile_cmd.extend(("-f","rtl_deferred.f",f"{top}.sv"))
    output = subprocess.run(compile_cmd,text=True,capture_output=True)
    if output.returncode:
        raise SystemExit(output.stderr or output.stdout)
    test_cmd = [args.vvp,str(binary),f"+tape={source}"]
    if args.wave:
        args.wave.parent.mkdir(parents=True,exist_ok=True)
        test_cmd.append(f"+wave={args.wave}")
    subprocess.run(test_cmd,check=True)
    print(f"{paths.name}: {dict(work.metrics)}",flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iverilog",default="iverilog")
    parser.add_argument("--vvp",default="vvp")
    parser.add_argument("--example",action="store_true")
    parser.add_argument("--wave",type=Path)
    parser.add_argument("--cycles",type=int,default=4400)
    parser.add_argument("--seed",type=int,default=122471)
    args = parser.parse_args()
    base = Path("build")/"deferred"
    if args.example:
        g = PROFILES["default"]
        work = example(g)
        simulate(g,work,base/"example",args)
        print([(edge,reply.tag,Status(reply.status).name,reply.ttl) for edge,reply in work.results])
    else:
        if args.cycles < 100 or args.wave:
            parser.error("matrix requires at least 100 cycles, and waveform requires --example")
        for label,g in PROFILES.items():
            work = randomize(directed(g),args.seed,args.cycles)
            if set(Status.__members__)-work.metrics.keys():
                raise AssertionError(f"incomplete status coverage {label}: {work.metrics}")
            if any(not work.metrics[key] for key in (
                "invalid_ingress","stalled_ingress","stalled_egress","flushed_messages","full_replace"
            )):
                raise AssertionError(f"incomplete flow coverage {label}: {work.metrics}")
            simulate(g,work,base/label,args)


if __name__ == "__main__":
    main()
