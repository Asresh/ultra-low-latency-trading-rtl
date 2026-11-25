"""Prepare and verify host scatter/gather descriptors and segment-table images."""

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import tempfile

from dma_protocol import (
    Descriptor, FLAG_CRC, FLAG_IRQ, FLAG_VERIFY, MAX_SEGMENTS, Opcode,
    Region, Segment, Status, burst_plan, crc32, segment_plan,
)


OPERATIONS = {"gather":Opcode.GATHER, "scatter":Opcode.SCATTER, "digest":Opcode.SEGMENT_DIGEST}
NAMES = {opcode:name for name,opcode in OPERATIONS.items()}
SPEC_FIELDS = {
    "operation","table","aperture","segments","tag","cookie","irq","verify","expected_crc","max_segments",
}


def number(value, label):
    """Accept an unsigned JSON integer or a conventional base-prefixed address."""
    if type(value) is int:
        parsed = value
    elif isinstance(value,str):
        try:
            parsed = int(value,0)
        except ValueError as exc:
            raise ValueError(f"{label} is not an integer") from exc
    else:
        raise ValueError(f"{label} is not an integer")
    if parsed < 0:
        raise ValueError(f"{label} must be unsigned")
    return parsed


def flag(data, name):
    value = data.get(name,False)
    if type(value) is not bool:
        raise ValueError(f"{name} expects true or false")
    return value


def only_fields(data, allowed):
    if not isinstance(data,dict):
        raise ValueError("descriptor plan must be a JSON object")
    different = data.keys()-allowed
    if different:
        raise ValueError(f"unexpected fields: {sorted(different)}")


@dataclass(frozen=True)
class Plan:
    descriptor: Descriptor
    aperture: Region
    max_segments: int
    rows: tuple

    @property
    def table(self):
        return b"".join(row.encode() for row in self.rows)

    @classmethod
    def checked(cls, descriptor, aperture, max_segments, wire):
        if not 1 <= max_segments <= 65535:
            raise ValueError("max_segments must be between 1 and 65535")
        if aperture.start%8 or aperture.stop%8 or aperture.stop >= 1 << 64:
            raise ValueError("DMA aperture endpoints must be aligned and representable")
        status = descriptor.validate(aperture,max_segments)
        if status != Status.OK:
            raise ValueError(f"descriptor rejected: {status.name}")
        return cls(descriptor,aperture,max_segments,segment_plan(descriptor,aperture,wire,max_segments))

    @classmethod
    def from_spec(cls, spec, payload=None):
        only_fields(spec,SPEC_FIELDS)
        try:
            opcode = OPERATIONS[spec["operation"]]
        except (KeyError,TypeError) as exc:
            raise ValueError("operation must be gather, scatter or digest") from exc
        bounds = spec["aperture"]
        if not isinstance(bounds,list) or len(bounds) != 2:
            raise ValueError("aperture must be [low, high]")
        aperture = Region(number(bounds[0],"aperture low"),number(bounds[1],"aperture high"))
        source = spec["segments"]
        if not isinstance(source,list):
            raise ValueError("segments must be a list of address and length objects")
        segments = []
        for row in source:
            only_fields(row,{"address","length"})
            segments.append(Segment(number(row["address"],"segment address"),number(row["length"],"segment length")))
        count = sum(row.length for row in segments)
        flags = (FLAG_IRQ if flag(spec,"irq") else 0) | (FLAG_VERIFY if flag(spec,"verify") else 0)
        expected = None
        if "expected_crc" in spec:
            expected = number(spec["expected_crc"],"expected_crc")
        if payload is not None:
            if expected is not None:
                raise ValueError("choose expected_crc or a payload file")
            if len(payload) != count:
                raise ValueError("payload file length must equal the exact segment total")
            expected = crc32(payload)
        if expected is not None:
            flags |= FLAG_CRC
        descriptor = Descriptor(
            opcode,length=count,address=number(spec["table"],"table address"),
            cookie=number(spec.get("cookie",0),"cookie"),flags=flags,tag=number(spec.get("tag",0),"tag"),
            expected_crc=expected if expected is not None else 0,reserved=len(segments),
        )
        max_segments = number(spec.get("max_segments",MAX_SEGMENTS),"max_segments")
        return cls.checked(descriptor,aperture,max_segments,b"".join(row.encode() for row in segments))

    def describe(self):
        """Produce a reproducible report with logical offsets and bus footprints."""
        begin = 0
        entries = []
        for row in self.rows:
            bursts = list(burst_plan(row.address,row.length))
            entries.append({
                "address":row.address, "length":row.length, "payload_offset":begin,
                "memory_bytes":sum(burst.byte_count for burst in bursts),
                "bursts":[[burst.address,burst.beats] for burst in bursts],
            })
            begin += row.length
        return {
            "version":1, "operation":NAMES[self.descriptor.op], "table_address":self.descriptor.address,
            "length":self.descriptor.length, "aperture":[self.aperture.start,self.aperture.stop],
            "max_segments":self.max_segments, "tag":self.descriptor.tag, "cookie":self.descriptor.cookie,
            "interrupt":self.descriptor.immediate_irq, "verify_write":self.descriptor.verify_write,
            "expected_crc":self.descriptor.expected_crc if self.descriptor.check_crc else None,
            "descriptor_sha256":sha256(self.descriptor.encode()).hexdigest(),
            "table_sha256":sha256(self.table).hexdigest(), "segments":entries,
        }


def export(plan, output):
    """Publish a new bundle after each file is written successfully."""
    if output.exists() or output.is_symlink():
        raise ValueError("output directory already exists")
    with tempfile.TemporaryDirectory(prefix=".dma-plan-",dir=output.parent) as temporary:
        staged = Path(temporary)/"bundle"
        staged.mkdir()
        (staged/"descriptor.bin").write_bytes(plan.descriptor.encode())
        (staged/"segments.bin").write_bytes(plan.table)
        (staged/"plan.json").write_text(json.dumps(plan.describe(),indent=2)+"\n",encoding="utf-8")
        staged.rename(output)


def inspect(directory):
    """Revalidate the binary payload and require consistency with its stored manifest."""
    metadata = json.loads((directory/"plan.json").read_text(encoding="utf-8"))
    descriptor = Descriptor.decode((directory/"descriptor.bin").read_bytes())
    plan = Plan.checked(
        descriptor,Region(*metadata["aperture"]),metadata["max_segments"],
        (directory/"segments.bin").read_bytes(),
    )
    if plan.describe() != metadata:
        raise ValueError("binary descriptor or list differs from the recorded plan")
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command",required=True)
    build = sub.add_parser("build")
    build.add_argument("spec",type=Path)
    build.add_argument("output",type=Path)
    build.add_argument("--payload",type=Path)
    audit = sub.add_parser("inspect")
    audit.add_argument("directory",type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build":
            spec = json.loads(args.spec.read_text(encoding="utf-8"))
            payload = args.payload.read_bytes() if args.payload else None
            plan = Plan.from_spec(spec,payload)
            export(plan,args.output)
        else:
            plan = inspect(args.directory)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        parser.exit(2,f"segment plan: {exc}\n")
    print(json.dumps(plan.describe(),indent=2))


if __name__ == "__main__":
    main()
