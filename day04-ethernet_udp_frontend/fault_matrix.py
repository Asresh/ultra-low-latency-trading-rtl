"""Targeted cross-layer mutations with named, expected endpoint dispositions."""

from dataclasses import dataclass, replace
from enum import Enum
import struct

from net_wire import Result, from_beats, parse_frame, to_beats


class Fault(Enum):
    TYPE = "eth_type"
    VLAN = "vlan_stack"
    VERSION = "ip_version"
    IHL = "ip_ihl"
    FRAGMENT = "ip_fragment"
    IP_SUM = "ip_checksum"
    IP_SHORT = "ip_short"
    UDP_SHORT = "udp_short"
    UDP_LONG = "udp_long"
    UDP_DATA = "udp_data"
    UDP_ADDRESS = "udp_address"
    RX_BAD = "rx_bad"
    RX_KEEP = "rx_keep"
    RX_FIRST = "rx_first"
    WIRE_SPLICE = "wire_splice"


EXPECTED = {
    Fault.TYPE:Result.L2_TYPE, Fault.VLAN:Result.VLAN_LIMIT,
    Fault.VERSION:Result.IP_SHAPE, Fault.IHL:Result.IP_SHAPE,
    Fault.FRAGMENT:Result.IP_FRAGMENT, Fault.IP_SUM:Result.IP_CHECKSUM,
    Fault.IP_SHORT:Result.IP_LENGTH, Fault.UDP_SHORT:Result.UDP_LENGTH,
    Fault.UDP_LONG:Result.UDP_LENGTH, Fault.UDP_DATA:Result.UDP_CHECKSUM,
    Fault.UDP_ADDRESS:Result.UDP_CHECKSUM, Fault.RX_BAD:Result.MAC_BAD,
    Fault.RX_KEEP:Result.BAD_AXIS, Fault.RX_FIRST:Result.BAD_AXIS,
    Fault.WIRE_SPLICE:Result.IP_LENGTH,
}


@dataclass(frozen=True)
class Mutated:
    kind: Fault
    original: object
    wire: bytes
    input_beats: tuple
    expected: Result


def changed(wire, offset, nbytes, value):
    return wire[:offset]+value.to_bytes(nbytes,"big")+wire[offset+nbytes:]


def toggled(wire, offset, mask):
    return changed(wire,offset,1,wire[offset]^mask)


def mutate(packet, fault, rng):
    # Every byte mutation other than an intentional truncation begins with
    # a valid frame and keeps the lower-layer wire contract intact.
    payload = packet.payload or b"\xc8"
    packet = replace(packet,payload=payload,udp_zero=False)
    l2 = packet.l2()
    segment = packet.segment()
    ip_offset = len(l2)
    udp_offset = ip_offset+20+len(packet.options)
    wire = packet.serialize(include_pad=False)
    if fault is Fault.TYPE:
        wire=changed(wire,ip_offset-2,2,0x86dd)
    elif fault is Fault.VLAN:
        updated=replace(packet,tags=((0x88a8,25),(0x8100,27),(0x8100,29)))
        wire=updated.serialize()
    elif fault is Fault.VERSION:
        wire=toggled(wire,ip_offset,0x10)
    elif fault is Fault.IHL:
        wire=changed(wire,ip_offset,1,0x44)
    elif fault is Fault.FRAGMENT:
        updated=replace(packet,frag_bits=rng.choice((0x8000,0x2000,1,2048)))
        wire=updated.serialize()
    elif fault is Fault.IP_SUM:
        wire=toggled(wire,ip_offset+10+rng.randrange(2),0x04)
    elif fault is Fault.IP_SHORT:
        wire=changed(wire,ip_offset+2,2,19)
    elif fault is Fault.UDP_SHORT:
        wire=changed(wire,udp_offset+4,2,7)
    elif fault is Fault.UDP_LONG:
        wire=changed(wire,udp_offset+4,2,8+len(payload)+rng.randrange(1,33))
    elif fault is Fault.UDP_DATA:
        start=udp_offset+8+rng.randrange(len(payload))
        wire=toggled(wire,start,1<<rng.randrange(8))
    elif fault is Fault.UDP_ADDRESS:
        updated=replace(packet,src_ip=packet.src_ip ^ (1<<rng.randrange(32)))
        wire=l2+updated.ip_header(len(segment))+segment
    elif fault is Fault.WIRE_SPLICE:
        offset=udp_offset+rng.randrange(len(segment))
        wire=wire[:offset]+wire[offset+1:]
    elif fault not in (Fault.RX_BAD,Fault.RX_KEEP,Fault.RX_FIRST):
        raise ValueError(f"unknown mutation {fault!r}")

    beats=to_beats(wire,rng=rng,bad=fault is Fault.RX_BAD)
    if fault is Fault.RX_KEEP:
        position=rng.randrange(len(beats))
        beats[position]=replace(beats[position],keep=0x55)
    elif fault is Fault.RX_FIRST:
        position=rng.randrange(len(beats))
        beats[position]=replace(beats[position],first=not beats[position].first)
    return Mutated(fault,packet,wire,tuple(beats),EXPECTED[fault])


def observe(case, limit):
    raw,axis=from_beats(case.input_beats)
    if axis != Result.OK:
        return axis
    return parse_frame(raw,max_frame=limit).result


def sample(rng, packet_generator, count, limit):
    if count < len(Fault):
        raise ValueError(f"at least {len(Fault)} samples are required to cover all faults")
    categories=list(Fault)
    rng.shuffle(categories)
    categories.extend(rng.choices(categories,k=count-len(categories)))
    for kind in categories:
        packet=packet_generator()
        # Enforce enough headroom so a tag expansion tests the intended
        # field rather than packet capture capacity.
        if len(packet.serialize(include_pad=False)) > limit-16:
            overhead=len(packet.l2())+20+len(packet.options)+8
            packet=replace(packet,payload=packet.payload[:max(1,limit-16-overhead)])
        case=mutate(packet,kind,rng)
        actual=observe(case,limit)
        if actual != case.expected:
            raise AssertionError(f"{kind.value}: got {actual.name}, expected {case.expected.name}")
        yield case
