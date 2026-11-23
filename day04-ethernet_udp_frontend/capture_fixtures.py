"""Synthetic wire fixtures shared by the capture tools' tests."""

import struct

from capture_io import CLASSIC_MAGIC, DLT_ETHERNET, NG_SHB, aligned_length


def align(data):
    return data.ljust(aligned_length(len(data)),b"\x00")


def pcap(records, endian="<", rate=1_000_000, snap=65535, network=DLT_ETHERNET):
    magic, = (magic for magic,dialect in CLASSIC_MAGIC.items() if dialect == (endian,rate))
    global_header = struct.pack(endian+"HHiiII",2,4,0,0,snap,network)
    return magic+global_header+b"".join(
        struct.pack(endian+"4I",seconds,fraction,len(data),wirelen)+data
        for seconds,fraction,data,wirelen in records
    )


def ng_block(endian, kind, contents):
    length = len(contents)+12
    if length % 4:
        raise ValueError("block contents must be word-aligned")
    marker = NG_SHB if kind is None else struct.pack(endian+"I",kind)
    packed_length = struct.pack(endian+"I",length)
    return marker+packed_length+contents+packed_length


def ng_option(endian, kind, data):
    return struct.pack(endian+"HH",kind,len(data))+align(data)


def ng_options(endian, *entries):
    return b"".join(ng_option(endian,kind,data) for kind,data in entries)+b"\x00"*4


def section(endian="<", *, extra=b"", version=1):
    return ng_block(endian,None,struct.pack(endian+"IHHq",0x1a2b3c4d,version,0,-1)+extra)


def interface(endian="<", *, link=DLT_ETHERNET, snap=65535, name=None, scale=None,
              offset=None, fcs=None, arbitrary=()):
    settings = list(arbitrary)
    if name is not None:
        settings.append((2,name.encode()))
    if scale is not None:
        settings.append((9,bytes((scale,))))
    if offset is not None:
        settings.append((14,struct.pack(endian+"q",offset)))
    if fcs is not None:
        settings.append((13,bytes((fcs,))))
    return ng_block(endian,1,struct.pack(endian+"HHI",link,0,snap)+ng_options(endian,*settings))


def enhanced(endian, data, *, iface=0, ticks=0, wirelen=None, flags=None, arbitrary=()):
    original = len(data) if wirelen is None else wirelen
    fields = struct.pack(endian+"5I",iface,ticks>>32,ticks&0xffffffff,len(data),original)
    entries = list(arbitrary)
    if flags is not None:
        entries.append((2,struct.pack(endian+"I",flags)))
    return ng_block(endian,6,fields+align(data)+ng_options(endian,*entries))


def simple(endian, data, *, wirelen=None):
    original = len(data) if wirelen is None else wirelen
    return ng_block(endian,3,struct.pack(endian+"I",original)+align(data))


def corrupt_tail(blob):
    return blob[:-1]+bytes((blob[-1]^0xff,))
