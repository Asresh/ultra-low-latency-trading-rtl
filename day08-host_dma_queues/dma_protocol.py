"""Host-visible DMA ring ABI and independent byte-lane utilities."""

from dataclasses import dataclass
from enum import IntEnum
import struct
import zlib


COUNTER_BITS = 32
U32_MASK = (1 << COUNTER_BITS)-1
U64_MASK = (1 << 64)-1
RECORD_BYTES = 32
MAX_PAYLOAD = 512
LANES = 8
MAX_AXI_BEATS = 16
AXI_PAGE = 4096
RING_RECORD = struct.Struct("<QQQQ")
FLAG_IRQ = 1
FLAG_CRC = 2
VALID_FLAGS = FLAG_IRQ|FLAG_CRC


class Opcode(IntEnum):
    PULL = 1
    PUSH = 2
    FENCE = 3


class Status(IntEnum):
    OK = 0
    BAD_OP = 1
    BAD_DESC = 2
    ADDRESS = 3
    READ_ERROR = 4
    WRITE_ERROR = 5
    CRC_ERROR = 6
    STREAM_FORMAT = 7
    CAPTURE_TIMEOUT = 8


class AxiResponse(IntEnum):
    OKAY = 0
    EXOKAY = 1
    SLVERR = 2
    DECERR = 3


def bounded(value, bits, label):
    if not isinstance(value, int) or not 0 <= value < 1<<bits:
        raise ValueError(f"{label} must fit in unsigned {bits} bits")
    return value


def packed_fields(*fields):
    image = 0
    for field,width in fields:
        image = (image << width) | bounded(field,width,"packed field")
    return image


def round_beats(byte_count):
    if byte_count < 0:
        raise ValueError("negative byte count")
    return (byte_count+LANES-1)//LANES


def rounded(byte_count):
    return round_beats(byte_count)*LANES


def crc32(data):
    return zlib.crc32(data)&U32_MASK


def add_pointer(counter, increment=1):
    bounded(counter,32,"ring pointer")
    if increment < 0:
        raise ValueError("ring pointer cannot move backwards")
    return (counter+increment)&U32_MASK


def distance(newer, older):
    bounded(newer,32,"newer ring pointer")
    bounded(older,32,"older ring pointer")
    return (newer-older)&U32_MASK


@dataclass(frozen=True)
class Region:
    start: int
    stop: int

    def __post_init__(self):
        bounded(self.start,64,"region start")
        if not self.start < self.stop <= 1<<64:
            raise ValueError("invalid half-open address bounds")

    def contains(self, address, size):
        return size >= 0 and self.start <= address <= self.stop and address+size <= self.stop

    def overlaps(self, other):
        return self.start < other.stop and other.start < self.stop

    @property
    def size(self):
        return self.stop-self.start


@dataclass(frozen=True)
class Ring:
    base: int
    entries: int

    def __post_init__(self):
        if self.entries < 2 or self.entries >= 1<<31 or self.entries & (self.entries-1):
            raise ValueError("ring depth must be a power of two in the monotonic counter half range")
        bounded(self.base,64,"ring base")
        if self.base%RECORD_BYTES:
            raise ValueError("ring base must be record aligned")
        if self.base+self.entries*RECORD_BYTES >= 1<<64:
            raise ValueError("ring footprint wraps the physical address space")

    def address(self, index):
        bounded(index,32,"ring index")
        return self.base+(index & (self.entries-1))*RECORD_BYTES

    def live(self, producer, consumer):
        occupancy = distance(producer,consumer)
        if occupancy > self.entries:
            raise ValueError(f"pointer distance {occupancy} exceeds ring depth {self.entries}")
        return occupancy

    @property
    def region(self):
        return Region(self.base,self.base+self.entries*RECORD_BYTES)

    def spans(self, begin, count):
        bounded(begin,32,"first counter")
        if not 0 <= count <= self.entries:
            raise ValueError("ring span count exceeds depth")
        return tuple(self.address(add_pointer(begin,index)) for index in range(count))


@dataclass(frozen=True)
class QueueConfig:
    sq: Ring
    cq: Ring
    aperture: Region

    def __post_init__(self):
        if self.aperture.stop > U64_MASK:
            raise ValueError("exclusive DMA aperture bound must fit in 64 bits")
        if self.aperture.start%LANES or self.aperture.stop%LANES:
            raise ValueError("aperture endpoints must be bus-beat aligned")
        regions = (self.sq.region,self.cq.region,self.aperture)
        for i, a in enumerate(regions):
            if any(a.overlaps(b) for b in regions[i+1:]):
                raise ValueError("SQ, CQ and data aperture must not overlap within a queue")


@dataclass(frozen=True)
class Descriptor:
    op: int
    length: int = 0
    address: int = 0
    cookie: int = 0
    flags: int = 0
    tag: int = 0
    expected_crc: int = 0
    reserved: int = 0

    def __post_init__(self):
        for value, width, name in (
            (self.op,8,"opcode"),(self.length,32,"payload length"),(self.address,64,"DMA address"),
            (self.cookie,64,"cookie"),(self.flags,8,"flags"),(self.tag,16,"tag"),
            (self.expected_crc,32,"expected CRC"),(self.reserved,32,"reserved data"),
        ):
            bounded(value,width,name)

    @property
    def check_crc(self):
        return bool(self.flags & FLAG_CRC)

    @property
    def immediate_irq(self):
        return bool(self.flags & FLAG_IRQ)

    def words(self):
        first = packed_fields((self.op,8),(self.flags,8),(self.tag,16),(self.length,32))
        fourth = packed_fields((self.reserved,32),(self.expected_crc,32))
        return (first,self.address,self.cookie,fourth)

    def encode(self):
        return RING_RECORD.pack(*self.words())

    @classmethod
    def from_words(cls, words):
        first, address, cookie, fourth = words
        return cls(first>>56,length=first&U32_MASK,address=address,cookie=cookie,
                   flags=(first>>48)&255,tag=(first>>32)&65535,
                   expected_crc=fourth&U32_MASK,reserved=fourth>>32)

    @classmethod
    def decode(cls, data):
        if len(data) != RECORD_BYTES:
            raise ValueError("descriptor must contain exactly 32 bytes")
        return cls.from_words(RING_RECORD.unpack(data))

    def validate(self, aperture):
        if self.flags & ~VALID_FLAGS or self.reserved or (self.expected_crc and not self.check_crc):
            return Status.BAD_DESC
        if self.op == Opcode.FENCE:
            if self.length or self.address or self.check_crc:
                return Status.BAD_DESC
        elif self.op not in (Opcode.PULL,Opcode.PUSH):
            return Status.BAD_OP
        elif not 1 <= self.length <= MAX_PAYLOAD or self.address%LANES:
            return Status.ADDRESS
        elif not aperture.contains(self.address,rounded(self.length)):
            return Status.ADDRESS
        return Status.OK


@dataclass(frozen=True)
class Completion:
    status: Status
    op: int
    tag: int
    length: int
    cookie: int
    crc: int
    queue: int
    timestamp: int

    def __post_init__(self):
        Status(self.status)
        for value,width,label in (
            (self.op,8,"opcode"),(self.tag,16,"tag"),(self.length,32,"byte count"),
            (self.cookie,64,"cookie"),(self.crc,32,"computed CRC"),(self.queue,8,"queue"),
            (self.timestamp,64,"completion timestamp"),
        ):
            bounded(value,width,label)
        if self.status != Status.OK and self.length:
            raise ValueError("error completion reports a nonzero byte count")

    @classmethod
    def of_descriptor(cls, descriptor, queue, status, timestamp, computed_crc=0):
        byte_count = descriptor.length if status == Status.OK else 0
        reported_crc = computed_crc if status in (Status.OK,Status.CRC_ERROR,Status.WRITE_ERROR) else 0
        return cls(status,descriptor.op,descriptor.tag,byte_count,descriptor.cookie,reported_crc,
                   queue,timestamp)

    def words(self):
        first = packed_fields((self.status,8),(self.op,8),(self.tag,16),(self.length,32))
        third = packed_fields((self.crc,32),(self.queue,8),(0,24))
        return first,self.cookie,third,self.timestamp

    def encode(self):
        return RING_RECORD.pack(*self.words())

    @classmethod
    def decode(cls, data):
        if len(data) != RECORD_BYTES:
            raise ValueError("completion must contain exactly 32 bytes")
        first, cookie, third, timestamp = RING_RECORD.unpack(data)
        if third & 0xffffff:
            raise ValueError("reserved completion bits must be zero")
        return cls(Status(first>>56),(first>>48)&255,(first>>32)&65535,first&U32_MASK,
                   cookie,third>>32,(third>>24)&255,timestamp)

    def matches(self, other, *, compare_time=True):
        if compare_time:
            return self == other
        return self.words()[:3] == other.words()[:3]


@dataclass(frozen=True)
class Burst:
    address: int
    beats: int

    def __post_init__(self):
        bounded(self.address,64,"AXI burst address")
        if self.address%LANES or not 1 <= self.beats <= MAX_AXI_BEATS:
            raise ValueError("unaligned or oversize burst")
        if self.address%AXI_PAGE+self.beats*LANES > AXI_PAGE:
            raise ValueError("AXI burst cannot cross 4 KiB")
        if self.address+self.beats*LANES > 1<<64:
            raise ValueError("AXI burst address wraps")

    @property
    def axlen(self):
        return self.beats-1

    @property
    def byte_count(self):
        return self.beats*LANES

    def addresses(self):
        for index in range(self.beats):
            yield self.address+index*LANES


def burst_plan(address, length):
    bounded(address,64,"transfer address")
    if address%LANES or length < 0:
        raise ValueError("DMA needs a beat-aligned start and nonnegative length")
    if address+rounded(length) > 1<<64:
        raise ValueError("rounded transfer wraps the address space")
    remain = round_beats(length)
    position = address
    while remain:
        page_capacity = (AXI_PAGE-position%AXI_PAGE)//LANES
        count = min(page_capacity,remain,MAX_AXI_BEATS)
        yield Burst(position,count)
        remain -= count
        position += count*LANES


@dataclass(frozen=True)
class Beat:
    data: int
    keep: int
    first: bool
    last: bool

    def __post_init__(self):
        bounded(self.data,64,"stream data")
        bounded(self.keep,8,"stream keep")

    @property
    def payload(self):
        if self.keep == 0 or self.keep & (self.keep+1):
            raise ValueError(f"bad contiguous keep 0x{self.keep:02x}")
        return self.data.to_bytes(LANES,"little")[:len(bin(self.keep))-2]

    def as_tuple(self):
        return self.data,self.keep,int(self.first),int(self.last)


def packet_beats(data):
    if not data:
        raise ValueError("empty body has no stream representation")
    for offset in range(0,len(data),LANES):
        chunk = data[offset:offset+LANES]
        yield Beat(int.from_bytes(chunk,"little"),(1<<len(chunk))-1,
                   offset == 0,offset+len(chunk) == len(data))


def collect_packet(beats, length, timed_out=False):
    received = bytearray()
    for i, beat in enumerate(beats):
        if beat.first != (i == 0):
            return Status.STREAM_FORMAT,b""
        try:
            data = beat.payload
        except ValueError:
            return Status.STREAM_FORMAT,b""
        received.extend(data)
        if len(received) > length:
            return Status.STREAM_FORMAT,b""
        if beat.last:
            if i+1 != len(beats) or len(received) != length:
                return Status.STREAM_FORMAT,b""
            return Status.OK,bytes(received)
        if beat.keep != 255 or len(received) >= length:
            return Status.STREAM_FORMAT,b""
    return (Status.CAPTURE_TIMEOUT if timed_out else Status.STREAM_FORMAT),b""


def write_beat_payload(data, burst, data_offset):
    for index,address in enumerate(burst.addresses()):
        start = data_offset+index*LANES
        chunk = data[start:start+LANES]
        if not chunk:
            raise ValueError("burst contains bytes outside transfer")
        yield address, int.from_bytes(chunk,"little"), (1<<len(chunk))-1, index+1==burst.beats
