"""Compare externally captured DMA completions and stream packets with a workload ledger."""

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path

from dma_protocol import Beat, Completion, Status, U64_MASK, bounded


class AuditError(ValueError):
    pass


def hex_bytes(raw,label):
    """Decode a hexadecimal capture field while retaining errors with its label."""
    if not isinstance(raw,str):
        raise AuditError(f"{label} must contain hexadecimal text")
    try:
        result = bytes.fromhex(raw)
    except ValueError as exc:
        raise AuditError(f"invalid {label}: {exc}") from None
    if raw.replace(" ","").replace("\n","").lower() != result.hex():
        raise AuditError(f"{label} contains extraneous separators")
    return result


def completion_rows(raw):
    """Parse little-endian completion records and report invalid row numbers."""
    for index,line in enumerate(raw,1):
        try:
            record = Completion.decode(hex_bytes(line.rstrip(),"completion"))
        except (AuditError,ValueError) as exc:
            raise AuditError(f"completion row {index}: {exc}") from None
        yield record


@dataclass(frozen=True)
class PacketCapture:
    queue: int
    tag: int
    cookie: int
    payload: bytes

    def __post_init__(self):
        """Validate the packet's queue identity, tag, cookie and payload type."""
        bounded(self.queue,8,"packet queue")
        bounded(self.tag,16,"packet tag")
        bounded(self.cookie,64,"packet cookie")
        if not self.payload:
            raise AuditError("captured packet must contain data")

    @classmethod
    def from_object(cls,row):
        """Normalize a capture record from its input object representation."""
        return cls(row["queue"],row["tag"],row["cookie"],hex_bytes(row["payload"],"payload"))

    def identity(self):
        """Return the queue, tag, cookie and payload identity used for comparison."""
        return self.queue,self.tag,self.cookie


def to_int(value,bits,label):
    """Parse a capture field and reject values outside its declared bit width."""
    if isinstance(value,int):
        return bounded(value,bits,label)
    try:
        parsed = int(value,0)
    except (ValueError,TypeError):
        raise AuditError(f"{label} value is not an integer") from None
    return bounded(parsed,bits,label)


def assemble_stream(rows):
    """Rebuild complete packets from framed stream rows with byte masks."""
    active = None
    data = bytearray()
    for number,row in enumerate(rows,1):
        try:
            queue,tag,cookie,word,keep,first,last = row.split()
            identity = to_int(queue,8,"queue"),to_int(tag,16,"tag"),to_int(cookie,64,"cookie")
            markers = int(first),int(last)
            if markers[0] not in (0,1) or markers[1] not in (0,1):
                raise AuditError("marker must be 0 or 1")
            beat = Beat(to_int(word,64,"data"),to_int(keep,8,"keep"),bool(markers[0]),bool(markers[1]))
            if not beat.last and beat.keep != 0xff:
                raise AuditError("partial keep on continuation")
            if beat.first:
                if active is not None:
                    raise AuditError("new first before old last")
                active = identity
            if active != identity:
                raise AuditError("packet sideband changed before last or missing first")
            data.extend(beat.payload)
            if beat.last:
                yield PacketCapture(*identity,bytes(data))
                data.clear()
                active = None
        except (AuditError,ValueError) as exc:
            raise AuditError(f"stream row {number}: {exc}") from None
    if active is not None:
        raise AuditError("incomplete packet at end of stream capture")


def ordered_by_queue(records):
    """Partition records by queue without changing order within each queue."""
    result = defaultdict(list)
    for entry in records:
        result[entry.queue].append(entry)
    return result


def monotonic_time(records):
    """Check completion timestamps increase in modular half-range order."""
    for a,b in zip(records, records[1:]):
        if (b.timestamp-a.timestamp)&U64_MASK >= 1<<63:
            raise AuditError("completion timestamps run backward outside modular half range")


def compare(expected_document,actual_completions,actual_packets, *, strict_schedule=False,
            check_clock=False):
    """Check captured packets and completions against generated expectations."""
    if expected_document.get("format") != "host_dma_audit_v1":
        raise AuditError("unrecognized expected ledger format")
    expected = tuple(completion_rows(expected_document["expected_completions"]))
    completion_records = tuple(actual_completions)
    if len(expected) != len(completion_records):
        raise AuditError(f"completion count {len(completion_records)} should be {len(expected)}")
    left = ordered_by_queue(completion_records)
    right = ordered_by_queue(expected)
    if left.keys() != right.keys():
        raise AuditError("completion queue set differs from ledger")
    for lane in right:
        if len(left[lane]) != len(right[lane]):
            raise AuditError(f"queue {lane} completion count differs from ledger")
        for slot,(reference,capture) in enumerate(zip(right[lane],left[lane])):
            if not capture.matches(reference,compare_time=False):
                raise AuditError(f"queue {lane} completion {slot} mismatch, cookie "
                                 f"{capture.cookie:#x} expected {reference.cookie:#x}")
        if check_clock:
            monotonic_time(left[lane])
    if strict_schedule and any(not a.matches(b,compare_time=False)
                               for a,b in zip(completion_records,expected)):
        raise AuditError("cross-queue retirement order differs from expected schedule")
    expected_packets = tuple(PacketCapture.from_object(row) for row in expected_document["expected_packets"])
    packets = tuple(actual_packets)
    left_packets = ordered_by_queue(packets)
    right_packets = ordered_by_queue(expected_packets)
    if len(packets) != len(expected_packets) or left_packets.keys() != right_packets.keys():
        raise AuditError("packet count or queue set differs from expected workload")
    for lane in right_packets:
        ref = right_packets[lane]
        got = left_packets[lane]
        if got != ref:
            raise AuditError(f"queue {lane} packets differ in ordering, sideband or bytes")
    if strict_schedule and packets != expected_packets:
        raise AuditError("global packet order differs from expected schedule")
    return {
        "copied_payload_bytes":sum(len(packet.payload) for packet in packets),
        "completion_count":len(completion_records),
        "error_completions":sum(record.status!=Status.OK for record in completion_records),
        "packet_count":len(packets),
        "queues":sorted(right),
    }


def main():
    """Compare an actual DMA capture with the specified workload expectations."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ledger",type=Path)
    ap.add_argument("completions",type=Path,help="each row is one 32-byte little-endian record")
    ap.add_argument("packets",type=Path,help="accepted stream rows: queue tag cookie data keep first last")
    ap.add_argument("--strict-schedule",action="store_true")
    ap.add_argument("--check-clock",action="store_true")
    args = ap.parse_args()
    try:
        ledger = json.loads(args.ledger.read_text())
        with args.completions.open() as completions,args.packets.open() as packets:
            report = compare(ledger,completion_rows(completions),assemble_stream(packets),
                             strict_schedule=args.strict_schedule,check_clock=args.check_clock)
    except (OSError,KeyError,ValueError) as exc:
        ap.error(str(exc))
    print(json.dumps(report,indent=2))


if __name__ == "__main__":
    main()
