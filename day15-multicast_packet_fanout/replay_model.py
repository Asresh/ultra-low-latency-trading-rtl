"""Independent sequence-indexed model for the multicast replay journal."""

from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum

from fanout_model import Status, pack


class Op(IntEnum):
    FOLLOW = 0
    REPLAY = 1
    DETACH = 2
    QUERY = 3


class ConfigStatus(IntEnum):
    OK = 0
    BAD_PORT = 1
    BAD_EPOCH = 2
    BAD_FIELDS = 3
    FUTURE = 4
    BUSY = 5


@dataclass(frozen=True)
class Geometry:
    ports: int = 3
    retain: int = 4
    max_beats: int = 8
    data_bytes: int = 4
    sequence_bits: int = 48
    epoch_bits: int = 16
    tag_bits: int = 16

    @property
    def full_mask(self):
        return (1 << self.data_bytes)-1

    @property
    def port_mask(self):
        return (1 << self.ports)-1

    @property
    def limit(self):
        return (1 << self.sequence_bits)-1

    def parameters(self):
        return dict(PORTS=self.ports,RETAIN=self.retain,MAX_BEATS=self.max_beats,DATA_BYTES=self.data_bytes,
                    SEQUENCE_W=self.sequence_bits,EPOCH_W=self.epoch_bits,TAG_W=self.tag_bits)


PROFILES = {
    "default": Geometry(),
    "odd": Geometry(5,3,5,3,10,6,9),
    "solo": Geometry(1,1,1,1,4,3,5),
    "wrapped_storage": Geometry(2,5,3,2,5,4,8),
}


@dataclass(frozen=True)
class Beat:
    data: int
    keep: int
    last: bool
    tag: int


@dataclass(frozen=True)
class Packet:
    sequence: int
    tag: int
    beats: tuple


@dataclass(frozen=True)
class Gap:
    first: int
    resume: int


@dataclass(frozen=True)
class Configuration:
    op: int
    port: int
    epoch: int
    sequence: int = 0
    tag: int = 0


@dataclass(frozen=True)
class Reply:
    status: int
    op: int
    port: int
    tag: int
    enabled: bool
    cursor: int


@dataclass(frozen=True)
class Input:
    reset: bool = False
    epoch: int = 0
    seed: int = 0
    config: Configuration = None
    beat: Beat = None
    ready: int = 0
    report_ready: bool = True
    cfg_report_ready: bool = True

    def words(self):
        cfg = self.config or Configuration(0,0,0)
        beat = self.beat or Beat(0,0,False,0)
        return (
            self.reset,self.epoch,self.seed,self.config is not None,cfg.op,cfg.port,cfg.epoch,cfg.sequence,cfg.tag,
            self.beat is not None,beat.data,beat.keep,beat.last,beat.tag,self.ready,
            self.report_ready,self.cfg_report_ready,
        )


@dataclass(frozen=True)
class Event:
    gap: bool
    first: bool
    last: bool
    sequence: int
    resume: int = 0
    data: int = 0
    keep: int = 0
    tag: int = 0


@dataclass(frozen=True)
class Report:
    status: int
    tag: int
    sequence: int


@dataclass
class Subscriber:
    enabled: bool = False
    desired: int = 0
    active: object = None
    beat_index: int = 0
    sent: int = 0
    gaps: int = 0
    lost: int = 0

    def offer(self):
        if self.active is None:
            return None
        if isinstance(self.active,Gap):
            return Event(True,True,True,self.active.first,self.active.resume)
        assert self.active.sequence == self.desired
        beat = self.active.beats[self.beat_index]
        return Event(False,self.beat_index==0,beat.last,self.desired,
                     data=beat.data,keep=beat.keep,tag=self.active.tag)

    def consume(self,event,limit):
        if event.gap:
            self.desired = event.resume
            self.gaps = min(0xffffffff,self.gaps+1)
            self.lost = min(limit,self.lost+event.resume-event.sequence)
        elif event.last:
            self.desired += 1
            self.sent = min(0xffffffff,self.sent+1)
        if event.last:
            self.active = None
            self.beat_index = 0
        else:
            self.beat_index += 1


@dataclass
class Assembly:
    tag: int
    good: list = field(default_factory=list)
    count: int = 0
    fault: Status = Status.FORWARDED

    def read(self,beat,geometry):
        if self.fault == Status.FORWARDED:
            lanes = [lane for lane in range(geometry.data_bytes) if beat.keep >> lane & 1]
            if not lanes or lanes != list(range(len(lanes))) or (
                not beat.last and beat.keep != geometry.full_mask
            ):
                self.fault = Status.BAD_BEAT
            elif beat.tag != self.tag:
                self.fault = Status.BAD_TAG
            elif self.count == geometry.max_beats:
                self.fault = Status.TOO_LONG
            else:
                self.good.append(Beat(beat.data & ((1 << (8*len(lanes)))-1),beat.keep,beat.last,beat.tag))
        self.count = min(self.count+1,geometry.max_beats)


@dataclass(frozen=True)
class Sample:
    cfg_ready: bool
    source_ready: bool
    offered: tuple
    cfg_report: Reply
    report: Report
    epoch: int
    next_sequence: int
    earliest: int
    retained: int
    resident: int
    exhausted: bool
    delivered: tuple
    gaps: tuple
    lost: tuple

    def words(self,g):
        empty = Event(False,False,False,0)
        outputs = [event or empty for event in self.offered]
        cfg = self.cfg_report or Reply(0,0,0,0,False,0)
        report = self.report or Report(0,0,0)
        return (
            self.cfg_ready,self.source_ready,
            pack((item is not None for item in self.offered),1),
            pack((item.gap for item in outputs),1),
            pack((item.first for item in outputs),1),
            pack((item.last for item in outputs),1),
            pack((item.sequence for item in outputs),g.sequence_bits),
            pack((item.resume for item in outputs),g.sequence_bits),
            pack((item.data for item in outputs),g.data_bytes*8),
            pack((item.keep for item in outputs),g.data_bytes),
            pack((item.tag for item in outputs),g.tag_bits),
            self.cfg_report is not None,cfg.status,cfg.op,cfg.port,cfg.tag,cfg.enabled,cfg.cursor,
            self.report is not None,report.status,report.tag,report.sequence,
            self.epoch,self.next_sequence,self.earliest,self.retained,self.resident,self.exhausted,
            pack(self.delivered,32),pack(self.gaps,32),pack(self.lost,g.sequence_bits),
        )


class ReplayJournal:
    """The deque retains immutable packets; a Subscriber can outlive deque eviction."""

    def __init__(self,geometry):
        self.g = geometry
        self.reset(0)

    def reset(self,epoch,seed=0):
        self.epoch = epoch
        self.packets = deque(maxlen=self.g.retain)
        self.subscribers = [Subscriber() for _ in range(self.g.ports)]
        self.next_sequence = seed
        self.report = None
        self.cfg_report = None
        self.assembly = None

    @property
    def earliest(self):
        return self.packets[0].sequence if self.packets else self.next_sequence

    def status(self,command):
        if not 0 <= command.port < self.g.ports:
            return ConfigStatus.BAD_PORT
        if command.epoch != self.epoch:
            return ConfigStatus.BAD_EPOCH
        if command.op != Op.REPLAY and command.sequence:
            return ConfigStatus.BAD_FIELDS
        if command.op == Op.REPLAY and command.sequence > self.next_sequence:
            return ConfigStatus.FUTURE
        lane = self.subscribers[command.port]
        if command.op != Op.QUERY and lane.active is not None:
            return ConfigStatus.BUSY
        return ConfigStatus.OK

    def configure(self,command):
        status = self.status(command)
        lane = self.subscribers[command.port] if command.port < self.g.ports else Subscriber()
        change = status == ConfigStatus.OK and command.op != Op.QUERY
        if change:
            lane.enabled = command.op != Op.DETACH
            lane.desired = self.next_sequence if command.op == Op.FOLLOW else command.sequence
        self.cfg_report = Reply(status,command.op,command.port,command.tag,lane.enabled,lane.desired)
        return command.port if change else None

    def sample(self,request):
        resident = set(self.packets)
        resident.update(
            lane.active for lane in self.subscribers if isinstance(lane.active,Packet)
        )
        held = len(resident) + (self.assembly is not None)
        assert held <= self.g.retain+self.g.ports+1
        blocked = request.reset
        exhausted = self.next_sequence == self.g.limit
        return Sample(
            not blocked and (self.cfg_report is None or request.cfg_report_ready),
            not blocked and (self.assembly is not None or (
                not exhausted and (self.report is None or request.report_ready)
            )),
            tuple(None if blocked else lane.offer() for lane in self.subscribers),
            None if blocked else self.cfg_report,None if blocked else self.report,
            self.epoch,self.next_sequence,self.earliest,len(self.packets),held,exhausted,
            tuple(lane.sent for lane in self.subscribers),
            tuple(lane.gaps for lane in self.subscribers),
            tuple(lane.lost for lane in self.subscribers),
        )

    def step(self,request=Input()):
        pre = self.sample(request)
        if request.reset:
            self.reset(request.epoch,request.seed)
            return pre
        altered = None
        if pre.cfg_ready:
            self.cfg_report = None
            if request.config is not None:
                altered = self.configure(request.config)
        for port,(lane,event) in enumerate(zip(self.subscribers,pre.offered)):
            if port == altered:
                continue
            if event:
                if request.ready >> port & 1:
                    lane.consume(event,self.g.limit)
            elif lane.enabled and lane.desired < self.earliest:
                lane.active = Gap(lane.desired,self.earliest)
            elif lane.enabled and lane.desired < self.next_sequence:
                lane.active = self.packets[lane.desired-self.earliest]
        if request.report_ready:
            self.report = None
        if request.beat is not None and pre.source_ready:
            beat = request.beat
            if self.assembly is None:
                self.assembly = Assembly(beat.tag)
            self.assembly.read(beat,self.g)
            if beat.last:
                frame = self.assembly
                self.assembly = None
                self.report = Report(frame.fault,frame.tag,self.next_sequence)
                if frame.fault == Status.FORWARDED:
                    self.packets.append(Packet(self.next_sequence,frame.tag,tuple(frame.good)))
                    self.next_sequence += 1
        return pre
