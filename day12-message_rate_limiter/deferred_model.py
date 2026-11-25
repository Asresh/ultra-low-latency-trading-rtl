"""Absolute-deadline and queue reference for deferred hierarchical admission."""

from collections import deque
from dataclasses import dataclass
from enum import IntEnum


class Op(IntEnum):
    SET_PORT = 0
    SET_GROUP = 1
    SET_SESSION = 2
    GET_PORT = 3
    GET_GROUP = 4
    GET_SESSION = 5


class Status(IntEnum):
    OK = 0
    ADMITTED = 1
    EXPIRED = 2
    DISABLED = 3
    IMPOSSIBLE = 4
    BAD_KEY = 5
    BAD_FIELDS = 6
    BUSY = 7


@dataclass(frozen=True)
class Geometry:
    groups: int = 3
    sessions: int = 7
    depth: int = 4
    token_bits: int = 12
    ttl_bits: int = 12
    tag_bits: int = 16
    data_bits: int = 32

    @property
    def session_bits(self):
        return self.sessions.bit_length()

    @property
    def group_bits(self):
        return self.groups.bit_length()

    @property
    def occupancy_bits(self):
        return self.depth.bit_length()

    def parameters(self):
        return dict(GROUPS=self.groups,SESSIONS=self.sessions,DEPTH=self.depth,
                    TOKEN_W=self.token_bits,TTL_W=self.ttl_bits,TAG_W=self.tag_bits,DATA_W=self.data_bits)


PROFILES = {
    "default": Geometry(),
    "odd": Geometry(5,9,3,8,7,12,19),
    "solo": Geometry(1,1,1,5,3,10,9),
    "narrow": Geometry(2,3,5,4,4,13,12),
}


def packed(values, widths):
    if len(values) != len(widths):
        raise ValueError("wrong number of fields")
    word = 0
    for raw, size in zip(values,widths):
        value = int(raw)
        if not 0 <= value < 1 << size:
            raise ValueError(f"field {value} doesn't fit in {size} bits")
        word = (word << size) | value
    return word


@dataclass(frozen=True)
class Control:
    op: int
    session: int = 0
    group: int = 0
    capacity: int = 0
    rate: int = 0
    reserve: int = 0
    quantum: int = 0
    enable: bool = False
    critical: bool = False
    tag: int = 0

    def pack(self,g):
        sizes = (3,g.session_bits,g.group_bits,*((g.token_bits,)*4),1,1,g.tag_bits)
        return packed(tuple(vars(self).values()),sizes)


@dataclass(frozen=True)
class Offer:
    session: int
    cost: int
    ttl: int
    tag: int
    data: int = 0

    def pack(self,g):
        return packed(tuple(vars(self).values()),(g.session_bits,g.token_bits,g.ttl_bits,g.tag_bits,g.data_bits))


@dataclass(frozen=True)
class Reply:
    admin: bool
    op: int
    status: int
    detail: int = 0
    session: int = 0
    group: int = 0
    tag: int = 0
    data: int = 0
    cost: int = 0
    ttl: int = 0
    deficit: int = 0
    depth: int = 0
    balances: tuple = (0,0,0)

    def pack(self,g):
        order = tuple(vars(self).values())[:-1] + tuple(reversed(self.balances))
        widths = (1,3,4,3,g.session_bits,g.group_bits,g.tag_bits,g.data_bits,g.token_bits,
                  g.ttl_bits,g.token_bits,g.occupancy_bits,*((g.token_bits,)*3))
        return packed(order,widths)


@dataclass
class Budget:
    maximum: int = 0
    rate: int = 0
    reserve: int = 0
    available: int = 0

    @property
    def enabled(self):
        return self.maximum > 0

    def refill(self):
        self.available = min(self.maximum,self.available+self.rate)


@dataclass
class Lane:
    queue: deque
    deficit: int = 0
    quantum: int = 0
    group: int = 0
    critical: bool = False


@dataclass(frozen=True)
class Pins:
    admin_ready: bool
    source_ready: bool
    rejected: bool
    result: Reply
    occupancy: int


def bitmask(flags):
    return sum(int(bool(flag)) << index for index,flag in enumerate(flags))


class DeferredScheduler:
    def __init__(self, geometry):
        self.geometry = geometry
        self.reset()

    def reset(self):
        g = self.geometry
        self.tick = 0
        self.tenant = [Lane(deque()) for _ in range(g.sessions)]
        self.budgets = (
            [Budget()], [Budget() for _ in range(g.groups)], [Budget() for _ in range(g.sessions)],
        )
        self.cursor = 0
        self.in_visit = False
        self.pending = None

    @property
    def occupancy(self):
        return sum(len(lane.queue) << (self.geometry.occupancy_bits*i) for i,lane in enumerate(self.tenant))

    def validation(self, cmd):
        session_op = cmd.op in (Op.SET_SESSION,Op.GET_SESSION)
        group_op = cmd.op in (Op.SET_GROUP,Op.GET_GROUP)
        writes = cmd.op <= Op.SET_SESSION
        if session_op and cmd.session >= self.geometry.sessions:
            return Status.BAD_KEY
        if (group_op or cmd.op == Op.SET_SESSION and cmd.enable) and cmd.group >= self.geometry.groups:
            return Status.BAD_KEY
        if (cmd.op > Op.GET_SESSION or cmd.session and not session_op
                or cmd.group and not (group_op or cmd.op == Op.SET_SESSION)):
            return Status.BAD_FIELDS
        if writes and cmd.enable:
            if not cmd.capacity or cmd.rate > cmd.capacity or cmd.reserve > cmd.capacity:
                return Status.BAD_FIELDS
            if session_op and (not cmd.quantum or cmd.reserve) or not session_op and (cmd.quantum or cmd.critical):
                return Status.BAD_FIELDS
        elif any((cmd.capacity,cmd.rate,cmd.reserve,cmd.quantum,cmd.enable,cmd.critical)) or (
            cmd.op == Op.SET_SESSION and cmd.group
        ):
            return Status.BAD_FIELDS
        if cmd.op == Op.SET_SESSION and self.tenant[cmd.session].queue:
            return Status.BUSY
        return Status.OK

    def control(self, cmd):
        status = self.validation(cmd)
        values = [0,0,0]
        group = cmd.group
        depth = deficit = 0
        configured = None
        if status == Status.OK:
            scope = cmd.op % 3
            key = (0,cmd.group,cmd.session)[scope]
            writes = cmd.op < 3
            if writes:
                self.budgets[scope][key] = Budget(cmd.capacity,cmd.rate,cmd.reserve,cmd.capacity)
                configured = (scope,key)
            values[scope] = self.budgets[scope][key].available
            if scope == 2:
                lane = self.tenant[key]
                if writes:
                    lane.group = cmd.group
                    lane.quantum = cmd.quantum
                    lane.critical = cmd.critical
                    lane.deficit = 0
                group = lane.group
                depth = len(lane.queue)
                deficit = lane.deficit
        return Reply(True,cmd.op,status,session=cmd.session,group=group,tag=cmd.tag,
                     deficit=deficit,depth=depth,balances=tuple(values)), configured

    def visit(self):
        """Attempt one head using absolute expiry and independently reconstructed budget thresholds."""
        key = self.cursor
        lane = self.tenant[key]
        if not lane.queue:
            lane.deficit = 0
            self.rotate()
            return None
        offer, expires = lane.queue[0]
        path = (self.budgets[0][0],self.budgets[1][lane.group],self.budgets[2][key])
        grant = lane.deficit
        if not self.in_visit:
            grant = min(grant+lane.quantum,(1<<self.geometry.token_bits)-1)
        ttl = max(0,expires-self.tick)
        needed = [offer.cost+(0 if lane.critical else item.reserve) for item in path]
        detail = bitmask(not item.enabled for item in path)
        if not ttl:
            status, detail = Status.EXPIRED,0
        elif detail:
            status = Status.DISABLED
        else:
            detail = bitmask(target > item.maximum for target,item in zip(needed,path))
            if detail:
                status = Status.IMPOSSIBLE
            elif grant < offer.cost or any(target > item.available for target,item in zip(needed,path)):
                lane.deficit = grant
                self.rotate()
                return None
            else:
                status = Status.ADMITTED
        depth = len(lane.queue)
        if status == Status.ADMITTED:
            for item in path:
                item.available -= offer.cost
            lane.deficit = grant-offer.cost
        lane.queue.popleft()
        if not lane.queue:
            lane.deficit = 0
        if status == Status.ADMITTED and lane.queue:
            self.in_visit = True
        else:
            self.rotate()
        return Reply(False,6,status,detail,offer.session,lane.group,offer.tag,offer.data,offer.cost,
                     ttl,lane.deficit,depth,tuple(item.available for item in path))

    def rotate(self):
        self.cursor = (self.cursor+1)%self.geometry.sessions
        self.in_visit = False

    def step(self, reset=False, ready=True, admin=None, offer=None):
        pre = self.occupancy
        if reset:
            self.reset()
            return Pins(False,False,False,None,pre)
        output = self.pending
        spare = output is None or ready
        # Save occupancies before any same-edge retirement or configuration.
        filled = tuple(len(lane.queue) for lane in self.tenant)
        config = None
        retired = None
        if spare:
            if admin is not None:
                self.pending,config = self.control(admin)
            else:
                retired = self.visit()
                self.pending = retired
        source = offer if offer is not None else Offer(0,0,0,0)
        valid = source.session < self.geometry.sessions and source.cost != 0 and source.ttl != 0
        source_ready = (
            not valid or filled[source.session] < self.geometry.depth
            or retired is not None and retired.session == source.session
        )
        if offer is not None and valid and source_ready:
            self.tenant[source.session].queue.append((offer,self.tick+offer.ttl+1))
        for level, buckets in enumerate(self.budgets):
            for index, bucket in enumerate(buckets):
                if (level,index) != config:
                    bucket.refill()
        self.tick += 1
        return Pins(spare,source_ready,offer is not None and not valid,output,pre)
