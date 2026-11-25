"""Audit an RTL trace, serialize its retained frames, and build a seeded restore tape."""

import argparse
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
import struct
from zlib import crc32

from replay_model import Beat, Configuration, Geometry, Input, Op, Packet, PROFILES, ReplayJournal


MAGIC = b"MRJ1"
VERSION = 1
HEADER = struct.Struct("<4sB7HI")
U16 = struct.Struct("<H")
CRC = struct.Struct("<I")
LIMITS = (32,4096,1024,64,128,32,32)


def dimensions(geometry):
    return tuple(vars(geometry).values())


def check_geometry(geometry):
    if any(not 1 <= value <= bound for value,bound in zip(dimensions(geometry),LIMITS)):
        raise ValueError("archive geometry exceeds supported positive bounds")
    if geometry.retain > geometry.limit:
        raise ValueError("sequence field cannot cover requested retention")


def pack_bits(value,bits):
    if not 0 <= value < 1 << bits:
        raise ValueError(f"value {value} exceeds {bits} unsigned bits")
    return value.to_bytes((bits+7)//8,"little")


class Reader:
    def __init__(self,data):
        self.data = data
        self.offset = 0

    def get(self,count):
        low = self.offset
        self.offset += count
        if self.offset > len(self.data):
            raise ValueError("truncated archive body")
        return self.data[low:self.offset]

    def bits(self,width):
        value = int.from_bytes(self.get((width+7)//8),"little")
        if value >= 1 << width:
            raise ValueError("nonzero high bits in a packed field")
        return value


def frame_valid(packet,geometry):
    if not 1 <= len(packet.beats) <= geometry.max_beats:
        raise ValueError("archive contains an empty or oversize packet")
    pack_bits(packet.tag,geometry.tag_bits)
    for index,beat in enumerate(packet.beats):
        terminal = index == len(packet.beats)-1
        if beat.tag != packet.tag or beat.last != terminal:
            raise ValueError("inconsistent archived tag or last marker")
        if (not 1 <= beat.keep <= geometry.full_mask or beat.keep & (beat.keep+1) or
                not terminal and beat.keep != geometry.full_mask):
            raise ValueError("noncanonical archived keep mask")
        pack_bits(beat.data,8*geometry.data_bytes)
        if beat.data >> (8*beat.keep.bit_length()):
            raise ValueError("archived bytes outside keep must be zero")


@dataclass(frozen=True)
class Archive:
    geometry: Geometry
    epoch: int
    next_sequence: int
    packets: tuple

    def __post_init__(self):
        g = self.geometry
        check_geometry(g)
        pack_bits(self.epoch,g.epoch_bits)
        pack_bits(self.next_sequence,g.sequence_bits)
        if len(self.packets) > min(g.retain,self.next_sequence):
            raise ValueError("packet count exceeds retention or sequence capacity")
        for offset,packet in enumerate(self.packets):
            if packet.sequence != self.earliest+offset:
                raise ValueError("retained sequences must be contiguous")
            frame_valid(packet,g)

    @property
    def earliest(self):
        return self.next_sequence-len(self.packets)

    def encode(self):
        g = self.geometry
        result = bytearray(HEADER.pack(MAGIC,VERSION,*dimensions(g),len(self.packets)))
        result.extend(pack_bits(self.epoch,g.epoch_bits))
        result.extend(pack_bits(self.next_sequence,g.sequence_bits))
        for packet in self.packets:
            result.extend(pack_bits(packet.tag,g.tag_bits))
            result.extend(U16.pack(len(packet.beats)))
            for beat in packet.beats:
                result.extend(pack_bits(beat.keep,g.data_bytes))
                result.extend(pack_bits(beat.data,8*g.data_bytes))
        result.extend(CRC.pack(crc32(result)))
        return bytes(result)

    @classmethod
    def decode(cls,data):
        if len(data) < HEADER.size+2+CRC.size:
            raise ValueError("truncated archive header")
        magic,version,*header = HEADER.unpack_from(data)
        if magic != MAGIC or version != VERSION:
            raise ValueError("unknown multicast-journal archive format")
        geometry = Geometry(*header[:-1])
        check_geometry(geometry)
        count = header[-1]
        if count > geometry.retain:
            raise ValueError("archive frame count exceeds retention")
        if crc32(data[:-CRC.size]) != CRC.unpack_from(data,len(data)-CRC.size)[0]:
            raise ValueError("archive CRC mismatch")
        reader = Reader(data[HEADER.size:-CRC.size])
        epoch = reader.bits(geometry.epoch_bits)
        next_sequence = reader.bits(geometry.sequence_bits)
        if next_sequence < count:
            raise ValueError("archive sequence range underflows")
        packets = []
        for sequence in range(next_sequence-count,next_sequence):
            tag = reader.bits(geometry.tag_bits)
            beat_count = U16.unpack(reader.get(U16.size))[0]
            if not 1 <= beat_count <= geometry.max_beats:
                raise ValueError("archived frame length outside geometry")
            beats = []
            for index in range(beat_count):
                keep = reader.bits(geometry.data_bytes)
                body = reader.bits(8*geometry.data_bytes)
                beats.append(Beat(body,keep,index+1==beat_count,tag))
            packets.append(Packet(sequence,tag,tuple(beats)))
        if reader.offset != len(reader.data):
            raise ValueError("archive has trailing unparsed bytes")
        return cls(geometry,epoch,next_sequence,tuple(packets))

    def restore(self,target_epoch,replay_port=None):
        g = self.geometry
        pack_bits(target_epoch,g.epoch_bits)
        if target_epoch == self.epoch:
            raise ValueError("restored journal requires a different epoch")
        if replay_port is not None and not 0 <= replay_port < g.ports:
            raise ValueError("restore subscriber is outside the geometry")
        yield Input(reset=True,epoch=target_epoch,seed=self.earliest)
        for packet in self.packets:
            for beat in packet.beats:
                yield Input(beat=beat,ready=g.port_mask)
        if replay_port is not None:
            yield Input(config=Configuration(Op.REPLAY,replay_port,target_epoch,self.earliest),
                        ready=g.port_mask)
        for _ in range(4+g.retain*(g.max_beats+1)):
            yield Input(ready=g.port_mask)


def unpack_input(fields,g):
    if len(fields) != 17:
        raise ValueError("input row needs 17 fields")
    for index in (0,3,9,12,15,16):
        if fields[index] not in (0,1):
            raise ValueError("invalid scalar control encoding")
    rst,epoch,seed,cv,op,port,ce,seq,ct,sv,data,keep,last,tag,ready,rep_ready,cfg_ready = fields
    pack_bits(epoch,g.epoch_bits)
    pack_bits(seed,g.sequence_bits)
    pack_bits(op,2)
    pack_bits(port,g.ports.bit_length())
    pack_bits(ce,g.epoch_bits)
    pack_bits(seq,g.sequence_bits)
    pack_bits(ct,g.tag_bits)
    pack_bits(data,8*g.data_bytes)
    pack_bits(keep,g.data_bytes)
    pack_bits(tag,g.tag_bits)
    pack_bits(ready,g.ports)
    return Input(
        bool(rst),epoch,seed,Configuration(op,port,ce,seq,ct) if cv else None,
        Beat(data,keep,bool(last),tag) if sv else None,ready,bool(rep_ready),bool(cfg_ready),
    )


def audit_window(stimulus,capture,geometry):
    """Replay and verify the recorded RTL pins, then return the terminal journal window."""
    check_geometry(geometry)
    checker = ReplayJournal(geometry)
    cycles = 0
    held_beat = held_config = None
    for cycle,(driven,observed) in enumerate(zip_longest(stimulus,capture)):
        if driven is None or observed is None:
            raise ValueError(f"stimulus and trace lengths disagree at cycle {cycle}")
        request = unpack_input(tuple(int(value,16) for value in driven.split()),geometry)
        if not cycle and not request.reset:
            raise ValueError("checkpoint audit requires a reset boundary")
        if request.reset:
            held_beat = held_config = None
        else:
            if held_beat is not None and request.beat != held_beat:
                raise ValueError(f"source changed a held beat at cycle {cycle}")
            if held_config is not None and request.config != held_config:
                raise ValueError(f"source changed a held configuration at cycle {cycle}")
        actual = tuple(int(value,16) for value in observed.split())
        sample = checker.step(request)
        if actual != sample.words(geometry):
            raise ValueError(f"replay trace disagrees with the protocol at cycle {cycle}")
        if not request.reset:
            held_beat = None if sample.source_ready else request.beat
            held_config = None if sample.cfg_ready else request.config
        cycles += 1
    if not cycles or checker.assembly is not None or checker.report is not None:
        raise ValueError("archive requires a complete and acknowledged ingress boundary")
    return Archive(geometry,checker.epoch,checker.next_sequence,tuple(checker.packets))


def avoid_alias(output,inputs):
    for location in inputs:
        if output.resolve() == location.resolve() or (
            output.exists() and location.exists() and output.samefile(location)
        ):
            raise ValueError("output aliases an input")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action",required=True)
    capture = commands.add_parser("extract")
    capture.add_argument("inputs",type=Path)
    capture.add_argument("capture",type=Path)
    capture.add_argument("archive",type=Path)
    capture.add_argument("--profile",choices=PROFILES,default="default")
    restore = commands.add_parser("restore")
    restore.add_argument("archive",type=Path)
    restore.add_argument("tape",type=Path)
    restore.add_argument("--epoch",type=int,required=True)
    restore.add_argument("--port",type=int)
    args = parser.parse_args()
    try:
        if args.action == "extract":
            avoid_alias(args.archive,(args.inputs,args.capture))
            with args.inputs.open(encoding="ascii") as stimulus,args.capture.open(encoding="ascii") as captured:
                image = audit_window(stimulus,captured,PROFILES[args.profile])
            args.archive.write_bytes(image.encode())
        else:
            avoid_alias(args.tape,(args.archive,))
            image = Archive.decode(args.archive.read_bytes())
            text = "".join(" ".join(f"{int(word):x}" for word in row.words())+"\n"
                           for row in image.restore(args.epoch,args.port))
            args.tape.write_text(text,encoding="ascii")
    except (ValueError,OSError) as exc:
        parser.exit(2,f"replay checkpoint: {exc}\n")
    print(f"epoch {image.epoch}, retained [{image.earliest},{image.next_sequence}), frames {len(image.packets)}")


if __name__ == "__main__":
    main()
