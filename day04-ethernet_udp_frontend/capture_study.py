"""Audit a capture against the endpoint policy and emit deterministic RTL replay vectors."""

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
from pathlib import Path
import zlib

from capture_io import CaptureError, DLT_ETHERNET, check_output_paths, packets, tick_period_ns
from net_wire import Decoded, Result, Rule, classify, parse_frame, to_beats
from rule_control import load_policy


@dataclass(frozen=True)
class Replay:
    cycle: int
    capture_serial: int
    index: int
    beat: object
    result: object = None


class CycleScheduler:
    def __init__(self, frequency_hz, interframe_idle=0):
        """Set the rational replay period and initialize the next available Ethernet edge."""
        self.period = tick_period_ns(frequency_hz)
        if interframe_idle < 0:
            raise ValueError("interframe idle must be nonnegative")
        self.gap = interframe_idle
        self.next_available = 0
        self.epoch = None
        self.reordered_time = 0
        self.last_timestamp = None

    def start(self, time_ns, count):
        """Map an optional packet timestamp to a cycle without overlapping earlier traffic."""
        if count <= 0:
            raise ValueError("cannot schedule empty packet")
        if time_ns is not None:
            if self.epoch is None:
                self.epoch = time_ns
            if self.last_timestamp is not None and time_ns < self.last_timestamp:
                self.reordered_time += 1
            self.last_timestamp = time_ns
            offset = max(0, time_ns-self.epoch) / self.period
            requested = (offset.numerator + offset.denominator-1)//offset.denominator
        else:
            requested = self.next_available
        cycle = max(self.next_available, requested)
        self.next_available = cycle+count+self.gap
        return cycle


@dataclass
class Totals:
    packets: int = 0
    wire_bytes: int = 0
    capture_bytes: int = 0
    emitted_packets: int = 0
    emitted_beats: int = 0
    payload_bytes: int = 0
    first_ns: int = None
    last_ns: int = None
    disposition: Counter = field(default_factory=Counter)
    queues: Counter = field(default_factory=Counter)
    vlans: Counter = field(default_factory=Counter)

    def count(self, record):
        """Increment an aggregate count and byte total for one packet."""
        self.packets += 1
        self.wire_bytes += record.wire_length
        self.capture_bytes += len(record.captured)
        if record.capture_time_ns is not None:
            t = record.capture_time_ns
            self.first_ns = t if self.first_ns is None else min(t,self.first_ns)
            self.last_ns = t if self.last_ns is None else max(t,self.last_ns)

    def resolved(self, result, replay_count):
        """Account for a decoded disposition and categorize its delivered data."""
        self.emitted_packets += 1
        self.emitted_beats += replay_count
        self.disposition[result.result.name] += 1
        if result.result == Result.OK:
            self.queues[str(result.queue)] += 1
            self.payload_bytes += len(result.packet.payload)
        if result.packet:
            identifier = "/".join(str(value & 0xfff) for _,value in result.packet.tags)
            self.vlans[identifier or "untagged"] += 1

    def as_dict(self):
        """Export the accumulated counts in a stable reporting format."""
        stats = vars(self).copy()
        for key in ("disposition","queues","vlans"):
            stats[key] = dict(sorted(stats[key].items()))
        if self.first_ns is None:
            stats["span_ns"] = None
        else:
            stats["span_ns"] = self.last_ns-self.first_ns
        return stats


def wire_without_fcs(record, fcs_mode):
    """Remove declared capture FCS bytes and report the frame's verification state."""
    if fcs_mode not in ("metadata","none","force","verify"):
        raise ValueError("invalid FCS mode")
    count = 4 if fcs_mode in ("force","verify") else record.fcs_bytes if fcs_mode == "metadata" else 0
    frame = record.captured
    if count == 0:
        return frame, False
    if count != 4:
        raise ValueError("unsupported FCS length")
    if len(frame) <= 4:
        raise ValueError("frame has no MAC data after FCS")
    actual = int.from_bytes(frame[-4:],"little")
    mac_data = frame[:-4]
    wrong_crc = fcs_mode == "verify" and zlib.crc32(mac_data) != actual
    return mac_data,wrong_crc


class Study:
    def __init__(self, rules, *, clock_hz=156250000, fcs_mode="metadata",
                 max_frame=1536, allow_zero=True, interface=None, idle=0, epoch=1):
        """Set capture filters, clock geometry, and classification state for a replay study."""
        if max_frame < 64 or max_frame > 65535:
            raise ValueError("invalid capture study frame bound")
        self.rules = tuple(rules)
        self.fcs_mode = fcs_mode
        self.allow_zero = allow_zero
        self.max_frame = max_frame
        self.interface = interface
        self.epoch = epoch
        self.scheduler = CycleScheduler(clock_hz,idle)
        self.interfaces = {}
        self.total = Totals()

    def add(self, record):
        """Filter and classify one capture record before assigning its transmission interval."""
        if self.interface is not None and self.interface != record.key:
            return ()
        index = f"{record.section}:{record.interface}"
        per_interface = self.interfaces.setdefault(index,interface_totals(record.name))
        targets = self.total,per_interface["totals"]
        for entry in targets:
            entry.count(record)
        status = None
        if record.link_type != DLT_ETHERNET:
            status = "non_ethernet_capture"
        elif record.truncated:
            status = "capture_truncated"
        elif not record.captured:
            status = "empty_capture_record"
        else:
            try:
                frame,mac_error = wire_without_fcs(record,self.fcs_mode)
            except ValueError:
                status = "unsupported_capture_fcs"
        if status is not None:
            for entry in targets:
                entry.disposition[status] += 1
            return ()

        if mac_error:
            result = Decoded(Result.MAC_BAD, min(len(frame), self.max_frame), epoch=self.epoch)
        else:
            result = classify(
                parse_frame(frame,max_frame=self.max_frame,allow_zero=self.allow_zero),
                self.rules,self.epoch,
            )
        beats = to_beats(frame,bad=mac_error)
        start = self.scheduler.start(record.capture_time_ns,len(beats))
        for entry in targets:
            entry.resolved(result,len(beats))
        return tuple(Replay(start+i,record.serial,i,beat,result if beat.last else None)
                     for i,beat in enumerate(beats))

    def report(self):
        """Summarize errors, timing, and packet and byte totals for the inspected capture."""
        return {
            "total":self.total.as_dict(),
            "interfaces":{key:{"name":entry["name"],**entry["totals"].as_dict()}
                          for key,entry in sorted(self.interfaces.items())},
            "capture_timestamp_reorders":self.scheduler.reordered_time,
            "time_horizon_cycles":self.scheduler.next_available,
            "allow_zero_checksum":self.allow_zero,
        }


def interface_totals(name):
    """Aggregate capture outcomes separately for each section and interface."""
    return {"name":name,"totals":Totals()}


def interface_id(value):
    """Parse the section and interface selector used to filter the capture."""
    try:
        values = tuple(int(x) for x in value.split(":"))
        if len(values) != 2 or min(values) < 0:
            raise ValueError()
        return values
    except ValueError:
        raise argparse.ArgumentTypeError("interface must be section:index") from None


def row_outputs(study, capture, data_handle=None, expectations=None):
    """Write the requested replay rows and summary from one completed study."""
    for record in capture:
        for row in study.add(record):
            if data_handle is not None:
                data_handle.write(f"{row.cycle:d} {row.capture_serial:d} {row.beat.row()}\n")
            if expectations is not None and row.result is not None:
                info,mask = row.result.rx_info()
                body = row.result.packet.payload.hex() if row.result.result == Result.OK else ""
                expectations.write(f"{row.capture_serial:d} {info:096x} {mask:096x} {body or '-'}\n")


def main():
    """Load a capture and select its analysis, timing, and export options."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capture",type=Path)
    ap.add_argument("--policy",type=Path)
    ap.add_argument("--strict-udp",action="store_true")
    ap.add_argument("--fcs",default="metadata",choices=("metadata","none","force","verify"))
    ap.add_argument("--clock-hz",type=int,default=156250000)
    ap.add_argument("--frame-limit",type=int,default=1536)
    ap.add_argument("--interface",type=interface_id)
    ap.add_argument("--idle",type=int,default=0)
    ap.add_argument("--output",type=Path,help="output MAC vector rows")
    ap.add_argument("--expected",type=Path,help="write oracle completions")
    args = ap.parse_args()

    try:
        check_output_paths((args.capture, args.policy), (args.output, args.expected))
        rules = load_policy(args.policy) if args.policy else (Rule(),)
        study = Study(rules,clock_hz=args.clock_hz,fcs_mode=args.fcs,max_frame=args.frame_limit,
                      allow_zero=not args.strict_udp,interface=args.interface,idle=args.idle)
        from contextlib import ExitStack
        with ExitStack() as stack:
            data = stack.enter_context(args.output.open("w")) if args.output else None
            compare = stack.enter_context(args.expected.open("w")) if args.expected else None
            row_outputs(study,packets(args.capture),data,compare)
        print(json.dumps(study.report(),indent=2))
    except (OSError,CaptureError,ValueError) as error:
        ap.error(str(error))


if __name__ == "__main__":
    main()
