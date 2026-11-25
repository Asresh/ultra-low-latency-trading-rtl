"""Generate sequenced multicast tests and compare every RTL signal with the oracle."""

import argparse
from collections import Counter, deque
from dataclasses import replace
from itertools import zip_longest
from pathlib import Path
from random import Random
import subprocess

from replay_model import Beat, Configuration, ConfigStatus, Input, Op, PROFILES, ReplayJournal, Status


def hexadecimal(words):
    return " ".join(format(int(word),"x") for word in words)


class Scenario:
    def __init__(self, geometry):
        self.g = geometry
        self.model = ReplayJournal(geometry)
        self.stimulus = []
        self.expected = []
        self.events = [[] for _ in range(geometry.ports)]
        self.metrics = Counter()

    def edge(self, request=None, **fields):
        pins = request or Input(**fields)
        result = self.model.step(pins)
        self.stimulus.append(pins.words())
        self.expected.append(result.words(self.g))
        self.metrics["cycles"] += 1
        self.metrics["max_resident"] = max(self.metrics["max_resident"],result.resident)
        if result.report:
            if pins.report_ready:
                self.metrics[Status(result.report.status).name] += 1
            else:
                self.metrics["held_input_reports"] += 1
        if result.cfg_report:
            if pins.cfg_report_ready:
                self.metrics["cfg_"+ConfigStatus(result.cfg_report.status).name] += 1
            else:
                self.metrics["held_cfg_reports"] += 1
        for port,event in enumerate(result.offered):
            if event:
                if pins.ready >> port & 1:
                    self.events[port].append(((len(self.stimulus)-1)*10+15,event))
                    if event.gap:
                        self.metrics["gaps"] += 1
                        self.metrics["lost"] += event.resume-event.sequence
                    elif event.last:
                        self.metrics["packet_deliveries"] += 1
                else:
                    self.metrics["held_gaps" if event.gap else "held_data"] += 1
        if result.exhausted:
            self.metrics["exhausted_cycles"] += 1
        if pins.beat is not None and not result.source_ready:
            self.metrics["blocked_beats"] += 1
        return result

    def idle(self, count, ready=None):
        for _ in range(count):
            self.edge(ready=self.g.port_mask if ready is None else ready)

    def configure(self, cfg, ready=None):
        for _ in range(5):
            result = self.edge(config=cfg,ready=self.g.port_mask if ready is None else ready)
            if result.cfg_ready:
                return
        raise AssertionError("directed configuration never accepted")

    def publish(self, beats, ready=None):
        for beat in beats:
            for _ in range(5):
                response = self.edge(beat=beat,ready=self.g.port_mask if ready is None else ready)
                if response.source_ready:
                    break
            else:
                raise AssertionError("directed packet never accepted")

    def drain(self):
        for _ in range(5+self.g.retain*(self.g.max_beats+1)+self.g.max_beats):
            self.edge(ready=self.g.port_mask)
        if self.model.assembly or self.model.report or self.model.cfg_report or any(
            lane.active is not None or lane.enabled and lane.desired != self.model.next_sequence
            for lane in self.model.subscribers
        ):
            raise AssertionError("replay fixture did not drain")


def singleton(g,tag):
    return Beat(tag & ((1 << (8*g.data_bytes))-1),1,True,tag)


def worked(geometry):
    r = Scenario(geometry)
    r.edge(reset=True,epoch=9)
    r.configure(Configuration(Op.FOLLOW,0,9,tag=0xa0))
    r.configure(Configuration(Op.FOLLOW,1,9,tag=0xb0),ready=1)
    r.publish((Beat(0xface0031,geometry.full_mask,False,0xd0),Beat(0xffffbb32,3,True,0xd0)),ready=1)
    r.idle(3,1)
    r.configure(Configuration(Op.DETACH,1,9,tag=4),ready=1)
    for number in range(1,9):
        r.publish((singleton(geometry,0xd0+number),),ready=1)
        r.idle(2,1)
    r.idle(2,3)
    r.idle(1,1)
    for number in range(9,12):
        r.publish((singleton(geometry,0xd0+number),),ready=1)
        r.idle(2,1)
    r.drain()
    fast = [event.sequence for _,event in r.events[0] if event.last and not event.gap]
    slow = [(event.sequence,event.resume) if event.gap else event.sequence
            for _,event in r.events[1] if event.last]
    if fast != list(range(12)) or slow != [0,(1,5),(5,8),8,9,10,11]:
        raise AssertionError(f"bad worked recovery scenario: fast={fast}, slow={slow}")
    return r


def prime(geometry):
    run = Scenario(geometry)
    run.edge(reset=True,epoch=1)
    run.configure(Configuration(Op.QUERY,geometry.ports,1))
    run.configure(Configuration(Op.FOLLOW,0,0))
    run.configure(Configuration(Op.DETACH,0,1,sequence=1))
    run.configure(Configuration(Op.REPLAY,0,1,sequence=1))
    run.configure(Configuration(Op.FOLLOW,0,1))
    # Valid, malformed mask, a mid-packet tag and a frame exceeding depth.
    run.publish((singleton(geometry,1),),ready=0)
    run.idle(2,ready=0)
    run.configure(Configuration(Op.REPLAY,0,1,0,3),ready=0)
    run.edge(cfg_report_ready=False,report_ready=False)
    run.publish((Beat(0,0,True,6),))
    run.publish((Beat(1,geometry.full_mask,False,7),Beat(2,1,True,8)))
    run.publish(tuple(
        Beat(i,geometry.full_mask,i==geometry.max_beats,9)
        for i in range(geometry.max_beats+1)
    ))
    for index in range(geometry.retain+3):
        run.publish((singleton(geometry,10+index),),ready=0)
    run.idle(1,ready=1)
    run.idle(3,ready=0)
    run.drain()
    run.configure(Configuration(Op.REPLAY,0,1,0,4))
    run.drain()
    run.configure(Configuration(Op.QUERY,0,1,tag=7))
    run.configure(Configuration(Op.DETACH,0,1,tag=8))
    for _ in range(4):
        run.edge(report_ready=False,cfg_report_ready=False)
    if geometry.sequence_bits <= 5:
        while run.model.next_sequence < geometry.limit:
            run.publish((singleton(geometry,1),))
        run.edge(beat=singleton(geometry,2))
        run.edge(beat=singleton(geometry,2))
    else:
        # Tear down a partial incoming packet and a deliberately held config result.
        run.edge(beat=Beat(1,geometry.full_mask,False,3),cfg_report_ready=False)
    run.edge(reset=True,epoch=2)
    run.drain()
    return run


def random_frame(rng,g):
    tag = rng.randrange(1 << g.tag_bits)
    size = rng.randrange(1,g.max_beats+1)
    damage = rng.randrange(9)
    if damage == 0:
        size = g.max_beats+1
    result = []
    for index in range(size):
        last = index == size-1
        keep = (1 << rng.randrange(1,g.data_bytes+1))-1 if last else g.full_mask
        if last and damage == 1:
            keep = 0
        chosen_tag = tag^1 if last and index and damage == 2 else tag
        result.append(Beat(rng.getrandbits(8*g.data_bytes),keep,last,chosen_tag))
    return deque(result)


def randomized(run, cycles, seed):
    g = run.g
    rng = Random(seed)
    plan = deque()
    epoch = run.model.epoch
    pending_config = None
    tag_mask = (1 << g.tag_bits)-1

    for cycle in range(cycles):
        if cycle and cycle % 353 == 0:
            epoch = (epoch+1) & ((1 << g.epoch_bits)-1)
            run.edge(reset=True,epoch=epoch)
            plan.clear()
            pending_config = None
            for port in range(g.ports):
                run.configure(Configuration(Op.FOLLOW,port,epoch))
        ready = sum(int(rng.randrange(11) > (2+(3 if port==0 else 0))) << port for port in range(g.ports))
        if cycle % 73 >= 54:
            ready &= ~1
        if not plan and run.model.next_sequence < g.limit and rng.randrange(7) != 0:
            plan = random_frame(rng,g)
        if pending_config is None and rng.randrange(13) == 0:
            op = rng.choice(tuple(Op))
            port = rng.randrange(g.ports+int(rng.randrange(7)==0))
            sequence = 0
            if op == Op.REPLAY:
                sequence = rng.randint(0,min(g.limit,run.model.next_sequence+2))
            pending_config = Configuration(
                op,port,epoch^int(rng.randrange(10)==0),sequence,cycle&tag_mask,
            )
        pre = run.edge(
            config=pending_config,beat=plan[0] if plan else None,ready=ready,
            report_ready=rng.randrange(7)!=0 and cycle % 89 < 80,
            cfg_report_ready=rng.randrange(5)!=0,
        )
        if plan and pre.source_ready:
            plan.popleft()
        if pre.cfg_ready:
            pending_config = None
    if plan:
        run.publish(plan)
    if pending_config:
        run.configure(pending_config)
    run.drain()
    return run


def simulate(run,args,label):
    directory = Path("build")/"replay"/label
    directory.mkdir(parents=True,exist_ok=True)
    top = "tb_multicast_replay_buffer"
    binary,stimulus,observed = (directory/"replay.vvp",directory/"inputs.hex",directory/"observed.hex")
    stimulus.write_text("".join(hexadecimal(row)+"\n" for row in run.stimulus),encoding="ascii")
    compiler = [args.iverilog,"-g2012","-s",top,"-o",str(binary)]
    compiler.extend(f"-P{top}.{key}={value}" for key,value in run.g.parameters().items())
    compiler.extend(("-f","rtl_replay.f",f"{top}.sv"))
    subprocess.run(compiler,check=True)
    command = [args.vvp,str(binary),f"+input={stimulus}",f"+capture={observed}"]
    if args.wave:
        args.wave.parent.mkdir(parents=True,exist_ok=True)
        command.append(f"+wave={args.wave}")
    subprocess.run(command,check=True)
    with observed.open(encoding="ascii") as capture:
        for cycle,(wanted,actual) in enumerate(zip_longest(run.expected,capture)):
            if wanted is None or actual is None:
                raise AssertionError(f"capture length differs at {cycle}")
            fields = tuple(int(value,16) for value in actual.split())
            if fields != wanted:
                mismatches = [(index,x,y) for index,(x,y) in enumerate(zip(wanted,fields)) if x!=y]
                raise AssertionError(f"{label} cycle {cycle}: {mismatches}, {len(fields)} observed fields")
    print(f"{label}: {dict(run.metrics)}",flush=True)
    return directory


def handoff(args):
    """Extract real simulated packets, restore them under a new epoch, and check delivery."""
    from replay_checkpoint import Archive, audit_window

    geometry = PROFILES["default"]
    source = simulate(worked(geometry),args,"handoff-source")
    with (source/"inputs.hex").open() as stimulus, (source/"observed.hex").open() as capture:
        archive = audit_window(stimulus,capture,geometry)
    if (archive.epoch,archive.earliest,archive.next_sequence) != (9,8,12):
        raise AssertionError("worked journal did not retain the expected source bounds")
    image = source/"retained.mrj"
    image.write_bytes(archive.encode())
    recovered = Archive.decode(image.read_bytes())
    fresh = Scenario(geometry)
    for step in recovered.restore(10,replay_port=0):
        fresh.edge(step)
    fresh.configure(Configuration(Op.FOLLOW,1,9,tag=0x55))
    fresh.drain()
    simulate(fresh,args,"handoff-target")
    collected = fresh.events[0]
    actual = [(event.sequence, event.data, event.keep, event.tag)
              for _,event in collected if not event.gap]
    expected = [(packet.sequence, beat.data, beat.keep, packet.tag)
                for packet in archive.packets for beat in packet.beats]
    if actual != expected or any(event.gap for _,event in collected):
        raise AssertionError("restored subscriber did not replay the exact retained bytes")
    if fresh.model.epoch != 10 or fresh.model.next_sequence != 12 or fresh.metrics["cfg_BAD_EPOCH"] != 1:
        raise AssertionError("restoration did not preserve cursor position or reject stale epoch")
    print("Replay handoff checked frames [8,12), preserved sequence, and rejected the previous epoch.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iverilog",default="iverilog")
    parser.add_argument("--vvp",default="vvp")
    parser.add_argument("--example",action="store_true")
    parser.add_argument("--handoff",action="store_true")
    parser.add_argument("--wave",type=Path)
    parser.add_argument("--cycles",type=int,default=4500)
    parser.add_argument("--seed",type=int,default=155911)
    args = parser.parse_args()
    if args.wave and not args.example:
        parser.error("waveform export uses --example")
    if args.cycles < 10:
        parser.error("cycles must be at least 10")
    if args.handoff:
        if args.example:
            parser.error("choose either --example or --handoff")
        handoff(args)
        return
    if args.example:
        work = worked(PROFILES["default"])
        simulate(work,args,"example")
        print([(edge,event.sequence,event.resume,event.gap) for edge,event in work.events[1] if event.last])
        return
    for name,geometry in PROFILES.items():
        run = randomized(prime(geometry),args.cycles,args.seed)
        simulate(run,args,name)
        must = ("FORWARDED","BAD_BEAT","BAD_TAG","TOO_LONG","cfg_OK","cfg_BAD_PORT","cfg_BAD_EPOCH",
                "cfg_BAD_FIELDS","cfg_FUTURE","cfg_BUSY","gaps","held_data","held_gaps")
        if any(not run.metrics[key] for key in must):
            raise AssertionError(f"incomplete phase coverage for {name}")
        if geometry.sequence_bits <= 5 and run.metrics["exhausted_cycles"] < 2:
            raise AssertionError(f"sequence exhaustion was not visible in {name}")


if __name__ == "__main__":
    main()
