"""Generate a replayable mixed DMA stress workload and check its transaction ledger."""

import argparse
from collections import Counter, deque
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import random

from dma_model import AxiFault, Capture, Engine, Fabric, Phase, SparseMemory
from dma_protocol import (
    AxiResponse, Beat, Burst, Descriptor, FLAG_CRC, FLAG_IRQ, Opcode, QueueConfig, Region,
    Ring, Status, U32_MASK, add_pointer, crc32, rounded,
)


@dataclass(frozen=True)
class Work:
    lane: int
    descriptor: Descriptor
    status: Status
    capture: Capture = None
    pull: bytes = b""
    push: bytes = b""
    memory_prepare: bytes = b""
    fault: AxiFault = None
    observed_crc: int = 0

    def request_image(self):
        """Return the serialized submission and capture input for the replay bundle."""
        d = self.descriptor
        return {
            "queue": self.lane,
            "raw_descriptor": d.encode().hex(),
            "memory_init": self.memory_prepare.hex(),
            "capture": None if self.capture is None else {
                "beats": [beat.as_tuple() for beat in self.capture.beats],
                "timing": self.capture.delays,
            },
            "fault": None if self.fault is None else {
                **asdict(self.fault), "phase":self.fault.phase.value,
            },
            "expected_status":self.status.name,
            "expected_crc":self.observed_crc,
        }


CASES = (
    "pull", "pull", "push", "push", "fence", "bad_opcode", "bad_descriptor",
    "out_of_range", "pull_crc", "push_crc", "stream_format", "timeout", "read_error",
    "write_error", "zero_length", "unaligned",
)


def random_bytes(rng, length):
    """Generate deterministic fixture data of the requested length."""
    return bytes(rng.randrange(256) for _ in range(length))


def make_item(rng,lane,ordinal,aperture,kind):
    """Create one workload entry with its expected status and data geometry."""
    page = (3*ordinal+1)*4096
    # Alternate apertures crossing a page just after the first beat and
    # interior addresses crossing one or more 16-beat burst limits.
    address = aperture.start+page+(0xff8 if ordinal%2 == 0 else 104)
    length = rng.choice((1,7,8,9,63,127,128,129,255,257,511,512))
    cookie = (lane << 48) | ordinal
    data = random_bytes(rng,length)
    digest = crc32(data)
    flag = FLAG_IRQ if ordinal%7 == 0 else 0
    compare = digest if ordinal%3 == 0 else 0
    flag |= FLAG_CRC if ordinal%3 == 0 else 0
    d = Descriptor(Opcode.PULL,length,address,cookie,flag,ordinal,compare)
    if kind == "pull":
        return Work(lane,d,Status.OK,pull=data,memory_prepare=data,observed_crc=digest)
    if kind == "push":
        d = replace(d,op=Opcode.PUSH)
        return Work(lane,d,Status.OK,capture=Capture.body(data),push=data,observed_crc=digest)
    if kind == "fence":
        return Work(lane,Descriptor(Opcode.FENCE,cookie=cookie,tag=ordinal,flags=flag&FLAG_IRQ),
                    Status.OK)
    if kind == "bad_opcode":
        return Work(lane,replace(d,op=77),Status.BAD_OP)
    if kind == "bad_descriptor":
        return Work(lane,replace(d,reserved=1),Status.BAD_DESC)
    if kind == "out_of_range":
        return Work(lane,replace(d,address=aperture.stop),Status.ADDRESS)
    if kind == "zero_length":
        return Work(lane,replace(d,length=0),Status.ADDRESS)
    if kind == "unaligned":
        return Work(lane,replace(d,address=address+1),Status.ADDRESS)
    if kind == "pull_crc":
        d = replace(d,flags=flag|FLAG_CRC,expected_crc=digest^1)
        return Work(lane,d,Status.CRC_ERROR,memory_prepare=data,observed_crc=digest)
    if kind == "push_crc":
        d = replace(d,op=Opcode.PUSH,flags=flag|FLAG_CRC,expected_crc=digest^8)
        return Work(lane,d,Status.CRC_ERROR,capture=Capture.body(data),observed_crc=digest)
    if kind == "stream_format":
        d = replace(d,op=Opcode.PUSH)
        capture = Capture.body(data)
        beats = capture.beats[:-1]+(replace(capture.beats[-1],keep=0x55),)
        return Work(lane,d,Status.STREAM_FORMAT,capture=Capture(beats))
    if kind == "timeout":
        return Work(lane,replace(d,op=Opcode.PUSH),Status.CAPTURE_TIMEOUT,capture=Capture(()))
    if kind == "read_error":
        fault = AxiFault(Phase.PULL,AxiResponse.SLVERR,address,lane)
        return Work(lane,d,Status.READ_ERROR,memory_prepare=data,fault=fault)
    if kind == "write_error":
        fault = AxiFault(Phase.PUSH,AxiResponse.DECERR,address,lane)
        return Work(lane,replace(d,op=Opcode.PUSH),Status.WRITE_ERROR,
                    capture=Capture.body(data),fault=fault,observed_crc=digest)
    raise ValueError(f"unrecognized workload case {kind!r}")


def config_for(lane,depth,requests):
    """Create nonoverlapping queue rings and payload space for this traffic lane."""
    ring = 0x10000+lane*0x4000
    low = 0x2000000+lane*0x10000000
    return QueueConfig(Ring(ring,depth),Ring(ring+0x2000,depth),
                       Region(low,low+(3*requests+5)*4096))


def build_workload(seed,requests,depth):
    """Generate a reproducible mixture of transfer sizes, operations and failures."""
    if not len(CASES) <= requests <= 1024:
        raise ValueError(f"requests per queue must be between {len(CASES)} and 1024")
    rng = random.Random(seed)
    configs = [config_for(lane,depth,requests) for lane in range(2)]
    entries = []
    for lane,config in enumerate(configs):
        sequence = list(CASES)
        rng.shuffle(sequence)
        sequence.extend(rng.choices(CASES,k=requests-len(sequence)))
        entries.append([make_item(rng,lane,index+1,config.aperture,kind)
                        for index,kind in enumerate(sequence)])
    return configs,entries


@dataclass
class Run:
    model: Engine
    events: list
    entries: dict
    occupancy_high: list
    full_blocks: int
    reaped: tuple

    def summary(self):
        """Return run counters, diagnostics and measured distributions as structured fields."""
        transaction_counts = Counter(event["event"] for event in self.events)
        return {
            "completed":self.model.completed,
            "rejected":self.model.rejected,
            "pull_bytes":self.model.pull_bytes,
            "push_bytes":self.model.push_bytes,
            "statuses":dict(sorted(Counter(c.status.name for c in self.model.completions).items())),
            "completion_high_water":self.occupancy_high,
            "completion_capacity_blocked_edges":self.full_blocks,
            "transactions":dict(sorted(transaction_counts.items())),
        }


def assert_completion(wanted,completion):
    """Compare the actual completion fields with this workload entry."""
    d = wanted.descriptor
    expected_id = (wanted.lane,d.op,d.tag,d.cookie)
    identity = (completion.queue,completion.op,completion.tag,completion.cookie)
    if identity != expected_id:
        raise AssertionError(f"completion identity {identity} should be {expected_id}")
    if completion.status != wanted.status or completion.crc != wanted.observed_crc:
        raise AssertionError(f"wrong result for cookie {completion.cookie:#x}")
    count = d.length if wanted.status == Status.OK else 0
    if count != completion.length:
        raise AssertionError("incorrect valid-byte count")


def assert_body_effect(memory,item,fill):
    """Check the expected payload changes and untouched memory sentinels."""
    d = item.descriptor
    if item.push:
        rounded_len = rounded(d.length)
        got = memory.read(d.address,rounded_len)
        expected = item.push+bytes((fill,))*(rounded_len-len(item.push))
        if got != expected:
            raise AssertionError(f"write strobe payload discrepancy at {d.address:#x}")
    if d.op == Opcode.PUSH and item.status in (
        Status.CRC_ERROR,Status.WRITE_ERROR,Status.STREAM_FORMAT,Status.CAPTURE_TIMEOUT
    ):
        if memory.read(d.address,rounded(d.length)) != bytes((fill,))*rounded(d.length):
            raise AssertionError("rejected first-burst PUSH modified guarded test range")


def verify_burst_shapes(trace):
    """Reject burst traces with invalid masks, limits or page crossings."""
    open_transfers = {}
    for entry in trace:
        key = entry.queue,entry.phase
        if entry.channel in ("AR","AW"):
            if key in open_transfers:
                raise AssertionError("AXI address issued before previous burst completed")
            mode = "R" if entry.channel=="AR" else "W"
            open_transfers[key] = Burst(entry.address,entry.data+1),mode,0
        elif entry.channel in ("R","W"):
            if key not in open_transfers:
                raise AssertionError("AXI data beat without prior address")
            schedule,mode,index = open_transfers[key]
            if index >= schedule.beats or entry.channel != mode or entry.address != schedule.address+8*index:
                raise AssertionError("AXI channel direction or address changed mid-burst")
            if entry.last != (index+1==schedule.beats):
                raise AssertionError("AXI last marker differs from programmed length")
            if mode=="W":
                if not entry.mask or entry.mask & (entry.mask+1) or (
                    entry.mask!=255 and (entry.phase==Phase.COMPLETION or not entry.last)
                ):
                    raise AssertionError("AXI byte strobe is invalid")
            if entry.last and mode=="R":
                del open_transfers[key]
            else:
                open_transfers[key] = schedule,mode,index+1
        elif entry.channel=="B":
            if key not in open_transfers:
                raise AssertionError("AXI response without matching address")
            schedule,mode,index = open_transfers.pop(key)
            if mode!="W" or index!=schedule.beats or schedule.address!=entry.address:
                raise AssertionError("AXI write response arrived before last accepted beat or wrong address")
        else:
            raise AssertionError(f"unexpected AXI channel {entry.channel!r}")
    if open_transfers:
        raise AssertionError("AXI transfer remains open at end of thread")


def exercise(configs,entries,seed):
    """Run the model on a workload with deterministic scheduling and fault injection."""
    rng = random.Random(seed^0xae4320)
    fill = 0xa5
    memory = SparseMemory(fill=fill)
    faults = [replace(item.fault) for group in entries for item in group if item.fault]
    engine = Engine(Fabric(memory,faults))
    all_items = {item.descriptor.cookie:item for group in entries for item in group}
    outstanding = [deque(group) for group in entries]
    seeds = (U32_MASK-2,U32_MASK-5)
    trace = []
    for lane,config in enumerate(configs):
        assert engine.configure(lane,config,seed=seeds[lane],irq_batch=(3,4)[lane],irq_delay=9)
    all_reaped = []
    high_water = [0,0]
    full_blocks = 0
    previous_emissions = 0

    def log(event,**fields):
        """Capture a transition in the host workload's chronological output."""
        trace.append({"event":event,**fields})

    for tick in range(1,len(all_items)*40+200):
        if len(all_reaped)==len(all_items):
            break
        lane = rng.randrange(2)
        for index in (lane,1-lane):
            queue = engine.queues[index]
            high_water[index] = max(high_water[index],queue.completion_count)
            if queue.eligible or not queue.submission_count:
                pass
            elif queue.completion_count == queue.config.cq.entries:
                full_blocks += 1
            if outstanding[index] and queue.submission_count<queue.config.sq.entries and rng.random()<0.72:
                item = outstanding[index].popleft()
                if item.memory_prepare:
                    memory.write(item.descriptor.address,item.memory_prepare)
                assert engine.host_post(index,item.descriptor,item.capture)
                log("post",queue=index,cookie=item.descriptor.cookie,sq_tail=queue.sq_tail)
            if queue.completion_count and (rng.random()<0.11 or not outstanding[index]):
                count = rng.randrange(1,queue.completion_count+1)
                result = engine.reap(index,count)
                all_reaped.extend(result)
                log("reap",queue=index,cq_head=queue.cq_head,count=count)
        if tick%9 == 0:
            engine.idle(1+rng.randrange(3))
            log("idle",time=engine.timestamp)
        if engine.pending:
            pending_queue = engine.queues[engine.pending.queue]
            before = pending_queue.sq_head,pending_queue.cq_tail
            item = all_items[engine.pending.descriptor.cookie]
            if rng.random()<0.70:
                done = engine.ack_completion([lane] if rng.random()<0.21 else ())
                assert_completion(item,done)
                assert_body_effect(memory,item,fill)
                log("retire",queue=done.queue,cookie=done.cookie,sq_head=pending_queue.sq_head,
                    cq_tail=pending_queue.cq_tail)
            else:
                assert engine.start() is None
                assert before == (pending_queue.sq_head,pending_queue.cq_tail)
                log("hold_b",queue=engine.pending.queue,cookie=item.descriptor.cookie)
        else:
            inflight = engine.start()
            if inflight:
                assert_completion(all_items[inflight.descriptor.cookie],inflight.completion)
                log("dispatch",queue=inflight.queue,cookie=inflight.descriptor.cookie)
                new_emission = engine.emissions[previous_emissions:]
                previous_emissions = len(engine.emissions)
                wanted = all_items[inflight.descriptor.cookie].pull
                if (tuple(e.bytes for e in new_emission) != ((wanted,) if wanted else ())):
                    raise AssertionError("incorrect PULL emission or early CRC release")
    else:
        raise AssertionError("randomized workload did not drain")
    if engine.completed != len(all_items) or any(outstanding) or engine.pending or engine.fatal:
        raise AssertionError("workload lost completion or entered fault state")
    if len({out.cookie for out in all_reaped}) != len(all_items):
        raise AssertionError("duplicate CQ record")
    verify_burst_shapes(engine.fabric.trace)
    return Run(engine,trace,all_items,high_water,full_blocks,tuple(all_reaped))


def export(configs,entries,result):
    """Write the request image, model outcomes and run statistics for external audit."""
    return {
        "format":"host_dma_audit_v1",
        "config":[asdict(config) for config in configs],
        "requests":[item.request_image() for group in entries for item in group],
        "transaction_schedule":result.events,
        "expected_completions":[record.encode().hex() for record in result.model.completions],
        "expected_packets":[{"queue":data.queue,"tag":data.tag,"cookie":data.cookie,
                             "payload":data.bytes.hex()} for data in result.model.emissions],
        "summary":result.summary(),
    }


def main():
    """Generate, run and export one deterministic DMA workload."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed",type=int,default=8088)
    ap.add_argument("--requests",type=int,default=96)
    ap.add_argument("--depth",type=int,default=8,choices=(2,4,8,16,32,64))
    ap.add_argument("--output",type=Path)
    args = ap.parse_args()
    configs,entries = build_workload(args.seed,args.requests,args.depth)
    run = exercise(configs,entries,args.seed)
    if args.output:
        args.output.write_text(json.dumps(export(configs,entries,run),indent=2)+"\n")
    print(json.dumps(run.summary(),indent=2))


if __name__ == "__main__":
    main()
