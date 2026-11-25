"""Generate adversarial audit traffic, compare pins, and replay actual DCA1 bytes."""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from dataclasses import replace
from itertools import zip_longest
from pathlib import Path

from audit_investigator_model import Drive, InvestigatorModel, RUN_ID
from audit_tape import publish_seal, verify
from check_drop_copy import REFERENCE, read_rows, write_rows
from drop_copy_audit import Profile
from drop_copy_model import Key, Payload, Status
from drop_copy_partition_model import BAD_NAMESPACE, Observation, Restart


FIELDS = (
    "primary_ready","copy_ready","epoch_ready","mark_ready","report_valid","report_index",
    "report_namespace","report_status","report_source","report_key","report_expected",
    "report_observed","report_diff","report_age","report_flushed","audit_valid","audit_record",
    "audit_index","sealed","serialized_valid","first","last","word","active_epochs",
    "pending_counts","namespace_metrics","namespace_incidents","bad_namespace_count",
)


class Scenario:
    def __init__(self,profile: Profile):
        self.model = InvestigatorModel(profile)
        self.profile = profile
        self.inputs = []
        self.samples = []
        self.coverage = Counter()

    def step(self,**values):
        event = Drive(**values)
        state = self.model.step(event)
        self.inputs.append(event.encode())
        self.samples.append(state.encode(self.profile))
        if state.journal and not (event.audit_allow and
                             (state.frame_word is None or (state.frame_word[1] and event.frame_ready))):
            self.coverage["journal_stall"] += 1
        if state.diagnostic and not event.diag_ready:
            self.coverage["report_stall"] += 1
        if state.frame_word and not event.frame_ready:
            self.coverage["word_stall"] += 1
        accepted = tuple(bool(getattr(event,key) and ready) for key,ready in (
            ("primary",state.primary_ready),("copy",state.copy_ready),("restart",state.epoch_ready),
        ))
        if sum(accepted) >= 2:
            self.coverage["parallel_accept"] += 1
        if all(accepted):
            self.coverage["triple_accept"] += 1
        return state

    def send(self,port: str,item,**other):
        signal = {"primary":"primary_ready","copy":"copy_ready","restart":"epoch_ready","mark":"mark_ready"}[port]
        for _ in range(400):
            state = self.step(**{port:item},**other)
            if getattr(state,signal):
                return state
        raise AssertionError(f"{port} did not transfer")

    def wait_report(self,code: int,key: Key | None = None):
        for _ in range(500):
            for row in reversed(self.model.journal):
                if row.report and row.report[1] == code and (key is None or row.report[3] == key.packed()):
                    return row
            self.step()
        raise AssertionError(f"no {code} report for {key}")

    def drain(self):
        for _ in range(800):
            if not (self.model.audit or self.model.report or self.model.sparse_frames):
                return
            self.step()
        raise AssertionError("audit and report mailboxes failed to drain")

    def write(self,out: Path):
        out.mkdir(parents=True,exist_ok=True)
        write_rows(out/"drive.hex",self.inputs)
        write_rows(out/"expected.hex",self.samples)
        (out/"expected.dca").write_bytes(self.model.emitted)


def incident(s: Scenario) -> None:
    """Hold the recorder beyond timeout, retain the timeout, then attach a late mate."""
    s.step(reset=True)
    s.step(run_id=RUN_ID+19)  # a live change to the reset identity has no effect
    key = Key(0,4,6,0x917)
    s.send("primary",Observation(0,(key,REFERENCE)))
    for _ in range(s.profile.wait+5):
        s.step(audit_allow=False,frame_ready=False,diag_ready=False)
    timeout = s.wait_report(Status.MISSING_COPY,key)
    assert timeout.report[7] >= s.profile.wait
    s.send("copy",Observation(0,(key,REFERENCE)))
    late = s.wait_report(Status.LATE,key)
    assert late.cycle > timeout.cycle and late.report[4] == REFERENCE.packed()
    s.send("mark",10)

    s.send("restart",Restart(0,2))
    s.wait_report(Status.RESTART)
    s.send("copy",Observation(0,(key,REFERENCE)))
    s.wait_report(Status.STALE_EPOCH,key)

    unknown = Observation(s.profile.namespaces,(Key(2,1,1,77),REFERENCE))
    s.send("primary",unknown)
    s.wait_report(BAD_NAMESPACE)
    s.send("mark",20)

    if s.profile.namespaces > 2:
        a = Observation(0,(Key(2,31,19,941),REFERENCE))
        b = Observation(1,(Key(0,28,19,955),REFERENCE))
        for _ in range(400):
            seen = s.step(primary=a,copy=b,restart=Restart(2,7))
            if any((seen.primary_ready,seen.copy_ready,seen.epoch_ready)):
                assert seen.primary_ready and seen.copy_ready and seen.epoch_ready
                break
        else:
            raise AssertionError("parallel lanes and restart never transferred")
    s.drain()


def random_load(s: Scenario,seed: int,cycles: int) -> None:
    rng = random.Random(seed)
    slots = [None,None]
    e = marker = None
    history: list[Observation] = []
    reference = 3000
    for edge in range(cycles + 500):
        advancing = edge < cycles
        if advancing:
            for lane in range(2):
                if slots[lane] or rng.randrange(3) == 0:
                    continue
                if history and rng.randrange(3) == 0:
                    previous = rng.choice(history[-12:])
                    key,body = previous.event
                    changed = (replace(body,quantity=max(1,body.quantity-3),side=1-body.side)
                               if rng.randrange(4) == 0 else body)
                    slots[lane] = Observation(previous.namespace,(key,changed))
                else:
                    user = rng.randrange(s.profile.namespaces + int(rng.randrange(11)==0))
                    epoch = s.model.ledger.ledgers[user].epoch if user < s.profile.namespaces else 0
                    key = Key(epoch,rng.randrange(8),rng.randrange(6),reference,int(rng.randrange(12)==0))
                    reference += 1
                    item = Observation(user,(key,Payload(rng.randrange(2048),rng.randrange(1,100),
                                                          rng.randrange(1,100000),rng.randrange(2))))
                    history.append(item)
                    slots[lane] = item
            if e is None and rng.randrange(190) == 0:
                venue = rng.randrange(s.profile.namespaces)
                e = Restart(venue,s.model.ledger.ledgers[venue].epoch + rng.randrange(3))
            if marker is None and edge % 233 == 0:
                marker = 100 + edge
        state = s.step(primary=slots[0],copy=slots[1],restart=e,mark=marker,
                       audit_allow=not advancing or (edge%71<64 and rng.randrange(11)>0),
                       frame_ready=not advancing or rng.randrange(7)>1,
                       diag_ready=not advancing or (edge%103<87 and rng.randrange(4)>0))
        if state.primary_ready:
            slots[0] = None
        if state.copy_ready:
            slots[1] = None
        if state.epoch_ready:
            e = None
        if state.mark_ready:
            marker = None
        if not advancing and not any((*slots,e,marker)):
            break
    else:
        raise AssertionError("random input holds did not drain")


def seal(s: Scenario) -> None:
    inevitable = Observation(0,(Key(2,1,1,0x7171),REFERENCE))
    # Preserve an already journaled diagnostic in the public mailbox
    # while the terminal marker prevents any additional publication.
    s.drain()
    proof_key = Key(s.model.ledger.ledgers[0].epoch,2,2,0x7172)
    s.send("primary",Observation(0,(proof_key,REFERENCE)),diag_ready=False)
    for _ in range(400):
        if s.model.report is not None:
            break
        s.step(diag_ready=False)
    else:
        raise AssertionError("no diagnostic pending at the terminal cutoff")
    for _ in range(400):
        seen = s.step(primary=inevitable,mark=0xC105E,terminal=True,diag_ready=False)
        assert not seen.primary_ready
        if seen.mark_ready:
            assert seen.diagnostic is not None
            break
    else:
        raise AssertionError("terminal marker did not transfer")
    for _ in range(5):
        seen = s.step(primary=inevitable,restart=Restart(0,4),diag_ready=False)
        assert seen.sealed and not seen.primary_ready and not seen.epoch_ready
    s.drain()
    assert s.model.journal[-1].marker.terminal


def capture_bytes(path: Path) -> bytes:
    result = bytearray()
    with path.open(encoding="ascii") as source:
        for number,word in enumerate(source,1):
            value = word.strip()
            if len(value) != 8 or set(value.lower())-set("0123456789abcdef"):
                raise ValueError(f"{path}:{number}: expected one 32-bit word")
            result.extend(int(value,16).to_bytes(4,"little"))
    return bytes(result)


def compare(out: Path,profile: Profile) -> None:
    expected = read_rows(out/"expected.hex",len(FIELDS))
    seen = read_rows(out/"observed.hex",len(FIELDS))
    for cycle,(want,actual) in enumerate(zip_longest(expected,seen)):
        if want is None or actual is None:
            raise AssertionError("different sample counts")
        if want != actual:
            differences = [f"{name}=0x{a:x}/0x{b:x}" for name,a,b in zip(FIELDS,actual,want) if a!=b]
            raise AssertionError(f"cycle {cycle}: actual/expected {', '.join(differences)}")

    actual = capture_bytes(out/"hardware_tape.hex")
    reference = (out/"expected.dca").read_bytes()
    if actual != reference:
        raise AssertionError("serial framing differs from the independent DCA1 encoding")
    tape = out/"hardware.dca"
    tape.write_bytes(actual)
    summary,anchor = verify(tape,profile,execution=0x917)
    counts = summary["report_counts"]
    if not all(counts.get(status,0) for status in ("MISSING_COPY","LATE","RESTART","STALE_EPOCH","BAD_NAMESPACE")):
        raise AssertionError("missing a directed incident status in actual capture")
    publish_seal(out/"hardware.seal.json",anchor)
    (out/"analysis.json").write_text(json.dumps(summary,indent=2)+"\n",encoding="utf-8")
    print(f"PASS {len(seen)} exact sampled edges, {anchor.records} framed audit records, "
          f"{sum(counts.values())} replayed diagnostics, {len(actual)} wire bytes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("generate","check"))
    parser.add_argument("output",type=Path)
    parser.add_argument("--namespaces",type=int,default=3)
    parser.add_argument("--sets",type=int,default=4)
    parser.add_argument("--cycles",type=int,default=9600)
    parser.add_argument("--seed",type=int,default=0xDCA17)
    args = parser.parse_args()
    plan = Profile(args.namespaces,args.sets,history=96)
    if args.action == "check":
        compare(args.output,plan)
        return
    s = Scenario(plan)
    incident(s)
    if args.cycles:
        random_load(s,args.seed,args.cycles)
    seal(s)
    s.write(args.output)
    print(" ".join(f"{k}={v}" for k,v in sorted(s.coverage.items())))


if __name__ == "__main__":
    main()
