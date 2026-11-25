"""Transaction-level DMA model with separate data effects and CQ retirement."""

from collections import deque
from dataclasses import dataclass, field
from enum import Enum, IntEnum

from dma_protocol import (
    AxiResponse, Completion, Descriptor, LANES, MAX_SEGMENTS, Opcode, QueueConfig, RECORD_BYTES,
    SEGMENT_RECORD, Segment, Status, segment_plan,
    U32_MASK, U64_MASK, add_pointer, bounded, burst_plan, crc32, distance, packet_beats,
    write_beat_payload,
)


class Phase(Enum):
    DESCRIPTOR = "descriptor"
    TABLE = "table"
    PULL = "pull"
    PUSH = "push"
    READBACK = "readback"
    VERIFY = "verify"
    COMPLETION = "completion"


class Fatal(IntEnum):
    NONE = 0
    AXI_READ_PROTOCOL = 1
    COMPLETION_WRITE = 2


class SparseMemory:
    def __init__(self, fill=0):
        """Initialize empty byte-addressed storage with the checked default fill."""
        self.fill = bounded(fill,8,"memory fill")
        self.cells = {}

    @staticmethod
    def check(address, length):
        """Validate the requested memory span before reading or committing any bytes."""
        bounded(address,64,"physical address")
        if length < 0 or address+length > 1<<64:
            raise ValueError("memory span wraps")

    def read(self,address,length):
        """Read bytes from sparse storage, substituting the default fill for unwritten locations."""
        self.check(address,length)
        return bytes(self.cells.get(address+i,self.fill) for i in range(length))

    def write(self,address,data):
        """Write every supplied byte at consecutive positions in sparse memory."""
        self.check(address,len(data))
        self.cells.update((address+i,byte) for i,byte in enumerate(data))

    def word(self,address):
        """Return the fixed-width word corresponding to this value or memory address."""
        return int.from_bytes(self.read(address,LANES),"little")

    def strobed(self,address,word,mask):
        """Write only AXI byte lanes selected by the strobe mask."""
        self.check(address,LANES)
        bounded(mask,8,"write strobe")
        lanes = bounded(word,64,"write word").to_bytes(LANES,"little")
        for i in range(LANES):
            if mask & (1<<i):
                self.cells[address+i] = lanes[i]


@dataclass(frozen=True)
class Access:
    queue: int
    phase: Phase
    channel: str
    address: int
    data: int = 0
    mask: int = 0
    last: bool = False
    response: int = 0


@dataclass
class AxiFault:
    phase: Phase
    response: AxiResponse = AxiResponse.OKAY
    address: int = None
    queue: int = None
    last: bool = None
    remaining: int = 1
    apply_write: bool = False
    read_xor: int = 0

    def __post_init__(self):
        """Validate the bus phase, address and count for this fault injection."""
        AxiResponse(self.response)
        if self.remaining < 0:
            raise ValueError("fault injection hit count must be nonnegative")
        if self.address is not None:
            bounded(self.address,64,"fault address")
        bounded(self.read_xor,64,"read corruption mask")

    def select(self, phase, queue, address):
        """Check whether an armed fault matches this request and consume one hit."""
        valid = self.remaining and self.phase == phase and (
            self.queue is None or self.queue == queue
        ) and (self.address is None or self.address == address)
        if valid:
            self.remaining -= 1
        return bool(valid)


class Fabric:
    def __init__(self,memory=None,faults=()):
        """Attach a memory model, programmable faults, and a recording bus trace."""
        self.memory = memory if memory is not None else SparseMemory()
        self.faults = list(faults)
        self.trace = []

    def fault(self,phase,queue,address):
        """Find and consume the next matching fault for this memory transaction."""
        for rule in self.faults:
            if rule.select(phase,queue,address):
                return rule
        return None

    def read(self,queue,phase,address,length):
        """Read through a planned AXI transaction while recording and applying bus faults."""
        raw = bytearray()
        failed = False
        for burst in burst_plan(address,length):
            self.trace.append(Access(queue,phase,"AR",burst.address,burst.axlen))
            for index,position in enumerate(burst.addresses()):
                rule = self.fault(phase,queue,position)
                wanted_last = (index+1 == burst.beats)
                last = rule.last if rule and rule.last is not None else wanted_last
                response = rule.response if rule else AxiResponse.OKAY
                word = self.memory.word(position)
                if rule:
                    word ^= rule.read_xor
                self.trace.append(Access(queue,phase,"R",position,word,last=last,response=response))
                if last != wanted_last:
                    return bytes(raw[:length]),failed,True
                failed |= response != AxiResponse.OKAY
                raw.extend(word.to_bytes(LANES,"little"))
        return bytes(raw[:length]),failed,False

    def _write_burst(self,queue,phase,burst,data,offset):
        """Apply one AXI burst and record data, masks and response order."""
        self.trace.append(Access(queue,phase,"AW",burst.address,burst.axlen))
        rule = self.fault(phase,queue,burst.address)
        response = rule.response if rule else AxiResponse.OKAY
        for address,word,strobe,last in write_beat_payload(data,burst,offset):
            self.trace.append(Access(queue,phase,"W",address,word,strobe,last))
            if response == AxiResponse.OKAY or (rule and rule.apply_write):
                self.memory.strobed(address,word,strobe)
        return Access(queue,phase,"B",burst.address,response=response)

    def write(self,queue,phase,address,data):
        """Run the AXI write plan and stop after the first rejected bus response."""
        for burst in burst_plan(address,len(data)):
            reply = self._write_burst(queue,phase,burst,data,burst.address-address)
            self.trace.append(reply)
            if reply.response != AxiResponse.OKAY:
                return False
        return True

    def prepare_completion_write(self,queue,address,data):
        """Stage CQ writeback while keeping its B response separately acknowledgeable."""
        burst, = burst_plan(address,len(data))
        return self._write_burst(queue,Phase.COMPLETION,burst,data,0)


@dataclass(frozen=True)
class Capture:
    beats: tuple
    delays: tuple = ()

    def __post_init__(self):
        """Check capture delays agree with the number of available beats."""
        if self.delays and len(self.delays) != len(self.beats):
            raise ValueError("capture delays must match input beat count")
        if any(delay < 0 for delay in self.delays):
            raise ValueError("capture cannot report a negative idle gap")

    @classmethod
    def body(cls,payload,delays=()):
        """Construct a capture from canonical packet beats and optional delays."""
        return cls(tuple(packet_beats(payload)),tuple(delays))

    def resolve(self,expected,timeout):
        """Validate framing while applying the configured capture timeout."""
        observed = bytearray()
        drain_elapsed = None
        delays = self.delays or (0,)*len(self.beats)
        for beat,delay in zip(self.beats,delays):
            if drain_elapsed is not None:
                if drain_elapsed+delay >= timeout:
                    return Status.CAPTURE_TIMEOUT,b""
                if beat.last:
                    return Status.STREAM_FORMAT,b""
                drain_elapsed += delay+1
                if drain_elapsed >= timeout:
                    return Status.CAPTURE_TIMEOUT,b""
                continue
            if delay >= timeout:
                return Status.CAPTURE_TIMEOUT,b""
            expected_remaining = expected-len(observed)
            wanted_keep = (1<<min(LANES,expected_remaining))-1
            wanted_last = expected_remaining <= LANES
            gate = (
                beat.first == (len(observed) == 0) and beat.last == wanted_last
                and beat.keep == wanted_keep
            )
            if gate:
                observed.extend(beat.payload)
                if beat.last:
                    return Status.OK,bytes(observed)
            elif beat.last:
                return Status.STREAM_FORMAT,b""
            else:
                drain_elapsed = 0
        return Status.CAPTURE_TIMEOUT,b""


@dataclass
class Irq:
    minimum_batch: int = 0
    maximum_delay: int = 0
    backlog: int = 0
    elapsed: int = 0
    request: bool = False

    def __post_init__(self):
        """Validate timer and moderation settings before tracking interrupt state."""
        bounded(self.minimum_batch,8,"IRQ batch")
        bounded(self.maximum_delay,32,"IRQ delay")

    def edge(self, *, completion=False, immediate=False, acknowledge=False):
        """Advance interrupt state through acknowledgment, a completion and timer changes."""
        if acknowledge:
            self.request = False
            self.backlog = 0
            self.elapsed = 0
        if completion:
            self.backlog = min(255,self.backlog+1)
        if self.backlog:
            self.elapsed = min(U32_MASK,self.elapsed+1)
            due_batch = bool(self.minimum_batch and self.backlog>=self.minimum_batch)
            due_timer = bool(self.maximum_delay and self.elapsed>=self.maximum_delay)
            self.request |= due_batch or due_timer
        else:
            self.elapsed = 0
        self.request |= completion and immediate

    def idle(self,cycles):
        """Advance the moderation timer during cycles without completions."""
        if cycles < 0:
            raise ValueError("cannot move IRQ clock backwards")
        if cycles and self.backlog:
            self.elapsed = min(U32_MASK,self.elapsed+cycles)
            if self.maximum_delay and self.elapsed>=self.maximum_delay:
                self.request = True


@dataclass
class Queue:
    config: QueueConfig
    sq_head: int
    sq_tail: int
    cq_head: int
    cq_tail: int
    irq: Irq
    captures: deque = field(default_factory=deque)

    @classmethod
    def fresh(cls,config,seed,batch,delay):
        """Initialize absolute queue cursors and interrupt settings from configuration."""
        bounded(seed,32,"queue seed")
        return cls(config,seed,seed,seed,seed,Irq(batch,delay))

    @property
    def submission_count(self):
        """Return the outstanding descriptor count using modular ring cursors."""
        return self.config.sq.live(self.sq_tail,self.sq_head)

    @property
    def completion_count(self):
        """Return generated completions that the host has yet to release."""
        return self.config.cq.live(self.cq_tail,self.cq_head)

    @property
    def eligible(self):
        """Check for work that can start immediately with current credits."""
        return self.submission_count>0 and self.completion_count<self.config.cq.entries

    def doorbell(self,sq_tail,cq_head):
        """Atomically update SQ producer and CQ consumer cursors if both are valid."""
        submitted = distance(sq_tail,self.sq_tail)
        consumed = distance(cq_head,self.cq_head)
        if submitted>self.config.sq.entries-self.submission_count or consumed>self.completion_count:
            return False
        self.sq_tail,self.cq_head = sq_tail,cq_head
        return True


@dataclass(frozen=True)
class Emission:
    queue: int
    tag: int
    cookie: int
    bytes: bytes

    def beats(self):
        """Return the packet as ordered AXI stream beats with a final partial mask."""
        return tuple(packet_beats(self.bytes))


@dataclass(frozen=True)
class Pending:
    queue: int
    descriptor: Descriptor
    completion: Completion
    reply: Access


class Engine:
    def __init__(self,fabric=None,*,capture_timeout=128,queues=2,isolate_queues=False,
                 max_segments=MAX_SEGMENTS):
        """Initialize independent host queues, interrupt state and descriptor scheduling."""
        if capture_timeout <= 0 or queues < 1 or max_segments < 1:
            raise ValueError("capture timeout, queue count and segment capacity must be positive")
        self.fabric = fabric if fabric is not None else Fabric()
        self.capture_timeout = capture_timeout
        self.isolate_queues = isolate_queues
        self.max_segments = max_segments
        self.queues = [None]*queues
        self.cursor = 0
        self.pending = None
        self.fatal = Fatal.NONE
        self.timestamp = 0
        self.emissions = []
        self.completed = 0
        self.rejected = 0
        self.pull_bytes = 0
        self.push_bytes = 0
        self.completions = []

    def configure(self,index,config=None,*,seed=0,irq_batch=0,irq_delay=0):
        """Install a queue's aligned ring geometry before making it active."""
        if not 0 <= index < len(self.queues) or self.pending or self.fatal:
            return False
        old = self.queues[index]
        if old and (old.submission_count or old.completion_count):
            return False
        if self.isolate_queues and config is not None and any(
            peer is not None and peer_index != index and config.overlaps(peer.config)
            for peer_index,peer in enumerate(self.queues)
        ):
            return False
        self.queues[index] = None if config is None else Queue.fresh(config,seed,irq_batch,irq_delay)
        return True

    def _queue(self,index):
        """Resolve a configured queue index or raise a clear model error."""
        if not 0 <= index < len(self.queues) or self.queues[index] is None:
            raise ValueError("queue is disabled or out of range")
        return self.queues[index]

    def idle(self,cycles):
        """Advance IRQ timers across queues without issuing another descriptor."""
        if cycles < 0:
            raise ValueError("cannot advance a negative time")
        self.timestamp = (self.timestamp+cycles)&U64_MASK
        for queue in self.queues:
            if queue:
                queue.irq.idle(cycles)

    def host_post(self,index,descriptor,capture=None):
        """Write a descriptor into simulated host memory and publish its SQ cursor."""
        queue = self._queue(index)
        if queue.submission_count == queue.config.sq.entries:
            return False
        address = queue.config.sq.address(queue.sq_tail)
        self.fabric.memory.write(address,descriptor.encode())
        queue.captures.append(capture)
        return queue.doorbell(add_pointer(queue.sq_tail),queue.cq_head)

    def push_capture(self,index,capture):
        # Use only with externally staged SQ images. host_post records its
        # own per-descriptor capture entry, including a missing packet.
        """Attach provider stream beats to the corresponding queued capture operation."""
        self._queue(index).captures.append(capture)

    def reap(self,index,count=None):
        """Release the requested count of host-visible completions from this queue."""
        queue = self._queue(index)
        ready = queue.completion_count
        count = ready if count is None else count
        if not 0 <= count <= ready:
            raise ValueError("host cannot claim a not-yet-retired completion")
        records = [
            Completion.decode(self.fabric.memory.read(address,RECORD_BYTES))
            for address in queue.config.cq.spans(queue.cq_head,count)
        ]
        queue.doorbell(queue.sq_tail,add_pointer(queue.cq_head,count))
        return records

    def eligible(self):
        """Check for work that can start immediately with current credits."""
        return tuple(i for i,queue in enumerate(self.queues) if queue and queue.eligible)

    def select(self):
        """Return the first eligible queue starting at the round-robin cursor."""
        available = set(self.eligible())
        for offset in range(len(self.queues)):
            slot = (self.cursor+offset)%len(self.queues)
            if slot in available:
                return slot
        return None

    def _read_body(self,index,phase,segments):
        """Read a logical packet in segment order and stop at the first bus failure."""
        parts = []
        for segment in segments:
            piece,failed,protocol = self.fabric.read(index,phase,segment.address,segment.length)
            if protocol:
                self.fatal = Fatal.AXI_READ_PROTOCOL
            if protocol or failed:
                return None
            parts.append(piece)
        return b"".join(parts)

    def _execute(self,index,queue,descriptor,capture):
        """Perform a checked DMA operation and construct its eventual completion."""
        aperture = queue.config.aperture
        validation = descriptor.validate(aperture,self.max_segments)
        if validation != Status.OK or descriptor.op == Opcode.FENCE:
            return validation,0
        segments = (Segment(descriptor.address,descriptor.length),)
        if descriptor.segmented:
            table = self._read_body(
                index,Phase.TABLE,(Segment(descriptor.address,SEGMENT_RECORD.size*descriptor.reserved),),
            )
            if table is None:
                return Status.READ_ERROR,0
            try:
                segments = segment_plan(descriptor,aperture,table,self.max_segments)
            except ValueError:
                return Status.SEGMENT_ERROR,0
        if not descriptor.push:
            phase = Phase.PULL if descriptor.pull else Phase.READBACK
            body = self._read_body(index,phase,segments)
            if body is None:
                return Status.READ_ERROR,0
        else:
            capture = capture if capture is not None else Capture(())
            status,body = capture.resolve(descriptor.length,self.capture_timeout)
            if status != Status.OK:
                return status,0
        digest = crc32(body)
        if descriptor.check_crc and descriptor.expected_crc != digest:
            return Status.CRC_ERROR,digest
        if descriptor.pull:
            self.emissions.append(Emission(index,descriptor.tag,descriptor.cookie,body))
            return Status.OK,digest
        if descriptor.op == Opcode.READBACK:
            return Status.OK,digest
        offset = 0
        for segment in segments:
            chunk = body[offset:offset+segment.length]
            if not self.fabric.write(index,Phase.PUSH,segment.address,chunk):
                return Status.WRITE_ERROR,digest
            offset += segment.length
        if descriptor.verify_write:
            observed = self._read_body(index,Phase.VERIFY,segments)
            if observed is None:
                return Status.READ_ERROR,0
            observed_crc = crc32(observed)
            if observed_crc != digest:
                return Status.VERIFY_ERROR,observed_crc
        return Status.OK,digest

    def start(self):
        """Execute the scheduler's next eligible descriptor and defer CQ visibility to B."""
        if self.pending or self.fatal:
            return None
        index = self.select()
        if index is None:
            return None
        queue = self.queues[index]
        self.cursor = (index+1)%len(self.queues)
        capture = queue.captures.popleft() if queue.captures else None
        descriptor_address = queue.config.sq.address(queue.sq_head)
        raw,failed,protocol = self.fabric.read(index,Phase.DESCRIPTOR,descriptor_address,RECORD_BYTES)
        if protocol:
            self.fatal = Fatal.AXI_READ_PROTOCOL
            return None
        if failed:
            descriptor = Descriptor(0)
            status,digest = Status.READ_ERROR,0
        else:
            descriptor = Descriptor.decode(raw)
            status,digest = self._execute(index,queue,descriptor,capture)
        if self.fatal:
            return None
        completion = Completion.of_descriptor(descriptor,index,status,self.timestamp,digest)
        cq_location = queue.config.cq.address(queue.cq_tail)
        b = self.fabric.prepare_completion_write(index,cq_location,completion.encode())
        self.pending = Pending(index,descriptor,completion,b)
        return self.pending

    def ack_completion(self,irq_acknowledged=()):
        """Commit the deferred completion response before exposing any new CQ credit."""
        if self.pending is None:
            return None
        work = self.pending
        self.fabric.trace.append(work.reply)
        self.pending = None
        queue = self.queues[work.queue]
        if work.reply.response != AxiResponse.OKAY:
            self.fatal = Fatal.COMPLETION_WRITE
            return None
        queue.sq_head = add_pointer(queue.sq_head)
        queue.cq_tail = add_pointer(queue.cq_tail)
        result = work.completion
        self.completed = min(U32_MASK,self.completed+1)
        if result.status != Status.OK:
            self.rejected = min(U32_MASK,self.rejected+1)
        elif work.descriptor.pull:
            self.pull_bytes = min(U64_MASK,self.pull_bytes+result.length)
        elif work.descriptor.push:
            self.push_bytes = min(U64_MASK,self.push_bytes+result.length)
        for index,active in enumerate(self.queues):
            if active is not None:
                active.irq.edge(completion=index==work.queue,immediate=work.descriptor.immediate_irq,
                                acknowledge=index in irq_acknowledged)
        self.timestamp = (self.timestamp+1)&U64_MASK
        self.completions.append(result)
        return result
