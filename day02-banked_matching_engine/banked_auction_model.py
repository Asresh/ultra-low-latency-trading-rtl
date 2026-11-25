"""Cycle/transaction reference for a banked closing auction and admission barrier."""

from collections import deque
from dataclasses import dataclass
from enum import IntEnum

from auction_model import Auction, Command, Geometry, Kind, Op, Result, pack


class ControlOp(IntEnum):
    ARM = 0
    CANCEL = 1
    QUERY = 2


class ControlStatus(IntEnum):
    OK = 0
    FIELDS = 1
    ROLLBACK = 2


class Detail(IntEnum):
    READY = 0
    INACTIVE = 1
    EPOCH = 2
    BUSY = 3
    ARMED = 4
    UNARMED = 5
    PAST = 6


class Wrapper(IntEnum):
    ADMITTED = 0
    INVALID_KEY = 1
    LOCKED = 2
    CUTOFF = 3


@dataclass(frozen=True)
class Profile:
    auction: Geometry = Geometry()
    banks: int = 3
    commands: int = 4
    events: int = 4
    sequence_bits: int = 16
    clock_bits: int = 48

    @property
    def bank_bits(self):
        return self.banks.bit_length()

    @property
    def maximum_clock(self):
        return (1 << self.clock_bits)-1

    @property
    def seq_mask(self):
        return (1 << self.sequence_bits)-1

    def parameters(self):
        return {
            **self.auction.parameters(), "BANKS":self.banks, "CMD_DEPTH":self.commands,
            "EVENT_DEPTH":self.events,"SEQ_W":self.sequence_bits,"CLOCK_W":self.clock_bits,
        }


PROFILES = {
    "default": Profile(),
    "odd": Profile(Geometry(7,9,8,3,4,10),5,3,2,4,10),
    "solo": Profile(Geometry(2,3,4,2,3,9),1,1,1,3,8),
    "narrow_clock": Profile(Geometry(5,6,6,3,3,11),2,2,3,4,6),
}


@dataclass(frozen=True)
class Control:
    op: int
    mask: int
    epochs: tuple = ()
    cutoff: int = 0
    tag: int = 0


@dataclass(frozen=True)
class Schedule:
    epoch: int
    cutoff: int
    tag: int
    inserted: bool = False


@dataclass(frozen=True)
class Response:
    control: Control
    status: int
    detail: int
    armed: int
    cutoff: int


@dataclass(frozen=True)
class Enqueued:
    command: Command
    number: int
    scheduled: bool = False
    wrapper: int = Wrapper.ADMITTED


@dataclass(frozen=True)
class Event:
    bank: int
    command_sequence: int
    event_sequence: int
    scheduled: bool
    wrapper: int
    result: Result

    def pack(self,profile):
        return pack(
            (self.bank,self.command_sequence,self.event_sequence,self.scheduled,self.wrapper,self.result.pack(profile.auction)),
            (profile.bank_bits,profile.sequence_bits,profile.sequence_bits,1,2,sum(profile.auction.result_widths)),
        )


@dataclass(frozen=True)
class Input:
    reset: bool = False
    bank: int = 0
    command: Command = None
    control: Control = None
    output_ready: bool = True
    control_ready: bool = True

    def words(self,p):
        choices = self.control or Control(0,0)
        return (
            self.reset,self.command is not None,self.bank,self.command.pack(p.auction) if self.command else 0,
            self.control is not None,choices.op,choices.mask,lane_pack(choices.epochs,p.auction.epoch_bits),
            choices.cutoff,choices.tag,self.output_ready,self.control_ready,
        )


@dataclass(frozen=True)
class Sample:
    source_ready: bool
    control_ready: bool
    output: Event
    report: Response
    clock: int
    armed: int
    due: int
    queued: int
    lane_valid: int

    def words(self,profile):
        control = self.report or Response(Control(0,0),0,0,0,0)
        return (
            self.source_ready,self.control_ready,self.output is not None,
            self.output.pack(profile) if self.output else 0,self.report is not None,
            control.control.op,control.status,control.control.mask,control.control.tag,
            control.detail,control.armed,control.cutoff,self.clock,self.armed,self.due,self.queued,self.lane_valid,
        )


def lane_pack(values,width):
    return sum(int(value) << (width*index) for index,value in enumerate(values))


class Instrument:
    """Translate one command into a timed result stream with finite local credits."""

    def __init__(self,profile,bank):
        self.profile = profile
        self.bank = bank
        self.book = Auction(profile.auction)
        self.input = deque()
        self.output = deque()
        self.command_sequence = 0
        self.event_sequence = 0
        self.schedule = None
        self.open_epoch = None
        self.active = None
        self.results = deque()
        self.available_at = 0

    def enqueue(self,command,scheduled=False,wrapper=Wrapper.ADMITTED):
        self.input.append(Enqueued(command,self.command_sequence,scheduled,wrapper))
        self.command_sequence = (self.command_sequence+1)&self.profile.seq_mask

    def append(self,request,result):
        event = Event(self.bank,request.number,self.event_sequence,request.scheduled,request.wrapper,result)
        self.output.append(event)
        self.event_sequence = (self.event_sequence+1)&self.profile.seq_mask
        if result.kind == Kind.OPENED:
            self.open_epoch = result.epoch
        elif result.kind in (Kind.CLEARING,Kind.ABORTED):
            self.open_epoch = None
        if request.scheduled and result.last:
            self.schedule = None

    def advance(self,cycle,had_command,output_credit):
        if self.active is not None:
            if output_credit and cycle >= self.available_at:
                result = self.results.popleft()
                self.append(self.active,result)
                if result.last:
                    self.active = None
                elif self.results:
                    self.available_at = cycle+(2 if self.results[0].kind == Kind.DONE else 1)
        elif had_command:
            head = self.input[0]
            if head.wrapper != Wrapper.ADMITTED:
                if output_credit:
                    self.input.popleft()
                    self.append(head,Result(Kind.REJECT,epoch=head.command.epoch,tag=head.command.tag))
            else:
                self.active = self.input.popleft()
                band = self.book.band
                self.results = deque(self.book.execute(head.command))
                long = self.results[0].kind in (Kind.CLEARING,Kind.INDICATIVE)
                self.available_at = cycle+(band.stop+2 if long else 1)


class Controller:
    def __init__(self,profile):
        self.profile = profile
        self.reset()

    def reset(self):
        self.cycle = 0
        self.clock = 0
        self.lanes = [Instrument(self.profile,bank) for bank in range(self.profile.banks)]
        self.reject = deque()
        self.reject_sequence = 0
        self.cursor = 0
        self.held_lane = None
        self.report = None

    def management(self,control):
        if not control.mask or control.op > ControlOp.QUERY or (
            control.op != ControlOp.ARM and control.cutoff
        ):
            return ControlStatus.FIELDS,(Detail.READY,)*self.profile.banks
        details = []
        epochs = control.epochs or (0,)*self.profile.banks
        for i,lane in enumerate(self.lanes):
            detail = Detail.READY
            row = lane.schedule
            if control.mask>>i&1 and control.op == ControlOp.ARM:
                if control.cutoff <= self.clock:
                    detail = Detail.PAST
                elif lane.open_epoch is None:
                    detail = Detail.INACTIVE
                elif epochs[i] != lane.open_epoch:
                    detail = Detail.EPOCH
                elif row is not None:
                    detail = Detail.ARMED
                elif lane.input or lane.active is not None:
                    detail = Detail.BUSY
            elif control.mask>>i&1 and control.op == ControlOp.CANCEL:
                if row is None:
                    detail = Detail.UNARMED
                elif epochs[i] != row.epoch:
                    detail = Detail.EPOCH
                elif row.cutoff <= self.clock:
                    detail = Detail.PAST
            details.append(detail)
        return (ControlStatus.ROLLBACK if any(details) else ControlStatus.OK),tuple(details)

    def choose_output(self,results):
        if self.held_lane is not None:
            return self.held_lane
        for offset in range(len(results)):
            lane = (self.cursor+offset)%len(results)
            if results[lane]:
                return lane
        return None

    def step(self,request):
        p = self.profile
        clock = self.clock
        old = [lane.schedule for lane in self.lanes]
        due = [row is not None and clock >= row.cutoff for row in old]
        queued = [row is not None and row.inserted for row in old]
        insert = [d and not sent for d,sent in zip(due,queued)]
        outgoing = [lane.output for lane in self.lanes]+[self.reject]
        cfg = request.control
        ctrl_ready = not request.reset and (self.report is None or request.control_ready)
        ctl_status,detail = self.management(cfg) if cfg else (None,())
        transaction = cfg is not None and ctrl_ready and ctl_status == ControlStatus.OK
        armed_now = [transaction and cfg.op == ControlOp.ARM and bool(cfg.mask>>i&1) for i in range(p.banks)]
        known = request.bank < p.banks
        input_room = [len(lane.input) < p.commands for lane in self.lanes]
        source_ready = not request.reset and (
            input_room[request.bank] and not armed_now[request.bank] and not insert[request.bank]
            if known else len(self.reject)<p.events
        )
        grant = self.choose_output(outgoing) if not request.reset else None
        observed = Sample(
            source_ready,ctrl_ready,outgoing[grant][0] if grant is not None else None,
            None if request.reset else self.report,clock,lane_pack((row is not None for row in old),1),
            lane_pack(due,1),lane_pack(queued,1),
            0 if request.reset else lane_pack((bool(fifo) for fifo in outgoing),1),
        )
        if request.reset:
            self.reset()
            return observed

        present_input = [bool(lane.input) for lane in self.lanes]
        output_credit = [len(fifo)<p.events for fifo in outgoing]
        if grant is not None:
            if request.output_ready:
                outgoing[grant].popleft()
                self.held_lane = None
                self.cursor = (grant+1)%len(outgoing)
            else:
                self.held_lane = grant
        if ctrl_ready:
            self.report = None
            if cfg:
                wanted = [row if cfg.mask>>i&1 else None for i,row in enumerate(old)]
                if transaction:
                    for i,lane in enumerate(self.lanes):
                        if armed_now[i]:
                            lane.schedule = wanted[i] = Schedule(lane.open_epoch,cfg.cutoff,cfg.tag)
                        elif cfg.op == ControlOp.CANCEL and cfg.mask>>i&1:
                            lane.schedule = wanted[i] = None
                self.report = Response(
                    cfg,ctl_status,lane_pack(detail,3),
                    lane_pack((row is not None for row in wanted),1),
                    lane_pack((row.cutoff if row else 0 for row in wanted),p.clock_bits),
                )
        for i,lane in enumerate(self.lanes):
            lane.advance(self.cycle,present_input[i],output_credit[i])
            if insert[i] and input_room[i]:
                row = old[i]
                lane.enqueue(Command(Op.CLOSE,row.epoch,row.tag),scheduled=True)
                lane.schedule = Schedule(row.epoch,row.cutoff,row.tag,True)
        if request.command is not None and source_ready:
            cmd = request.command
            if not known:
                self.reject.append(Event(
                    request.bank,self.reject_sequence,self.reject_sequence,False,Wrapper.INVALID_KEY,
                    Result(Kind.REJECT,epoch=cmd.epoch,tag=cmd.tag),
                ))
                self.reject_sequence = (self.reject_sequence+1)&p.seq_mask
            else:
                reason = Wrapper.ADMITTED
                if due[request.bank]:
                    reason = Wrapper.CUTOFF
                elif old[request.bank] is not None and cmd.op in (Op.CLOSE,Op.ABORT):
                    reason = Wrapper.LOCKED
                self.lanes[request.bank].enqueue(cmd,wrapper=reason)
        self.clock = min(clock+1,p.maximum_clock)
        self.cycle += 1
        return observed
