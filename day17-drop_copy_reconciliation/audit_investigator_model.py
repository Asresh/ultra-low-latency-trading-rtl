"""Independent cycle reference for the investigator and DCA1 transport."""

from __future__ import annotations

from dataclasses import dataclass

from audit_tape import frame
from drop_copy_audit import Marker, Profile, Record
from drop_copy_model import WORD32
from drop_copy_partition_model import Input, Observation, PartitionModel, Restart


RUN_ID = 0xDC17120000891B0A


@dataclass(frozen=True)
class Drive:
    primary: Observation | None = None
    copy: Observation | None = None
    restart: Restart | None = None
    mark: int | None = None
    terminal: bool = False
    diag_ready: bool = True
    audit_allow: bool = True
    frame_ready: bool = True
    reset: bool = False
    run_id: int = RUN_ID

    def encode(self) -> tuple[int,...]:
        p = self.primary.encode() if self.primary else (0,0,0,0)
        c = self.copy.encode() if self.copy else (0,0,0,0)
        e = (1,self.restart.namespace,self.restart.epoch) if self.restart else (0,0,0)
        m = (1,self.mark,self.terminal) if self.mark is not None else (0,0,0)
        return (self.reset,self.run_id,self.diag_ready,self.audit_allow,self.frame_ready,*p,*c,*e,*m)


@dataclass(frozen=True)
class Sample:
    primary_ready: bool
    copy_ready: bool
    epoch_ready: bool
    mark_ready: bool
    diagnostic: tuple[int,...] | None
    journal: Record | None
    index: int
    sealed: bool
    frame_word: tuple[int,int,int] | None
    state: tuple[int,...]

    def encode(self,profile: Profile) -> tuple[int,...]:
        d = (1,*self.diagnostic) if self.diagnostic else (0,)*11
        a = (1,self.journal.encode(profile)) if self.journal else (0,0)
        f = (1,*self.frame_word) if self.frame_word else (0,0,0,0)
        return (self.primary_ready,self.copy_ready,self.epoch_ready,self.mark_ready,*d,*a,
                self.index,self.sealed,*f,*self.state)


class InvestigatorModel:
    """Represent the report mailbox and serialized frame as independent queues."""

    def __init__(self,profile: Profile):
        self.profile = profile
        self.clear(0)
        self.emitted = bytearray()
        self.journal = []
        self.public = []

    def clear(self,identity: int) -> None:
        p = self.profile
        self.ledger = PartitionModel(p.namespaces,p.sets,p.wait,p.history,p.reset_tick)
        self.run = identity
        self.cycle = 0
        self.ordinal = 0
        self.seal = False
        self.audit: Record | None = None
        self.report: tuple[int,...] | None = None
        self.sparse_frames = b""
        self.read_at = 0

    def step(self,req: Drive) -> Sample:
        if req.reset:
            self.clear(req.run_id)
            return Sample(False,False,False,False,None,None,0,False,None,(0,)*5)

        word = None
        if self.sparse_frames:
            first = self.read_at == 0
            last = self.read_at + 4 == len(self.sparse_frames)
            word = (int(first),int(last),
                    int.from_bytes(self.sparse_frames[self.read_at:self.read_at+4],"little"))
        frame_room = word is None or (bool(word[1]) and req.frame_ready)
        audit_take = self.audit is not None and req.audit_allow and frame_room
        report_take = self.report is not None and req.diag_ready
        room = bool(self.run and not self.seal and (self.audit is None or audit_take))
        marker = req.mark is not None and room
        cut = marker and req.terminal
        forward = room and not cut
        core_drive = Input(
            req.primary if forward else None,req.copy if forward else None,
            req.restart if forward else None,forward and (self.report is None or report_take),
        )
        pins = self.ledger.advance(core_drive)
        ready = tuple(bool(visible and forward) for visible in pins[:3])
        snapshot = tuple(pins[13:18])
        actual = Sample(*ready,room,self.report,self.audit,self.ordinal,self.seal,word,snapshot)

        p = req.primary if ready[0] else None
        c = req.copy if ready[1] else None
        e = req.restart if ready[2] else None
        diagnostic = tuple(pins[4:13]) if core_drive.ready and pins[3] else None
        marked = (Marker(int(cut),req.mark,(snapshot[2] >> (9*32)) & WORD32,
                         snapshot[0],snapshot[1],snapshot[3],snapshot[4]) if marker else None)

        if word is not None and req.frame_ready:
            self.emitted.extend(word[2].to_bytes(4,"little"))
            self.read_at += 4
            if self.read_at == len(self.sparse_frames):
                self.sparse_frames = b""
        if audit_take:
            self.sparse_frames = frame(self.audit.encode(self.profile),self.profile)
            self.read_at = 0
            self.audit = None
        if report_take:
            self.public.append(self.report)
            self.report = None
        if any((p,c,e,diagnostic,marked)):
            self.ordinal += 1
            self.audit = Record(self.run,self.ordinal,self.cycle,p,c,e,diagnostic,marked)
            self.journal.append(self.audit)
        if diagnostic:
            self.report = (self.ordinal,*diagnostic)
        if cut:
            self.seal = True
        self.cycle += 1
        return actual
