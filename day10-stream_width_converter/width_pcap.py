"""Turn Ethernet capture bytes into a time-indexed gearbox teaching workload."""

import argparse
from dataclasses import dataclass
from fractions import Fraction
import importlib.util
import json
from pathlib import Path
import sys

from width_capture import Geometry, check_destination
from width_recipe import Recipe, simulate
from width_workload import encode, row

MAX_PACKETS = 3000
MAX_CYCLE = 499999


class ImportErrorDetail(ValueError):
    """The capture cannot be replayed under the requested geometry or clock policy."""


def shared_capture_module():
    """Reuse the repository's Day 04 pcap/pcapng reader and interpretation."""
    path = Path(__file__).resolve().parents[1] / "day04-ethernet_udp_frontend" / "capture_io.py"
    spec = importlib.util.spec_from_file_location("width_day04_capture_io", path)
    if spec is None or spec.loader is None:
        raise ImportErrorDetail("Day 04 capture reader is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ensure_pace(ns_per_cycle):
    """Validate a positive rational period without introducing float error."""
    try:
        period = Fraction(ns_per_cycle)
    except (ValueError, ZeroDivisionError) as exc:
        raise ImportErrorDetail("invalid rational period in nanoseconds") from exc
    if period <= 0:
        raise ImportErrorDetail("period in nanoseconds must be positive")
    return period


def interface_key(value):
    """Parse the optional SECTION:INTERFACE selector."""
    if value is None:
        return None
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdecimal() for part in parts):
        raise ImportErrorDetail("interface selector must be two nonnegative integers")
    return int(parts[0]), int(parts[1])


@dataclass(frozen=True)
class ImportStats:
    imported: int
    skipped_interface: int
    skipped_truncated: int
    skipped_link_type: int
    rejected_oversize: int
    removed_fcs: int

    def values(self):
        """Give a stable stand-alone diagnostic report for the selected source."""
        return dict(vars(self))


def select_rows(captured, geometry, period, key=None, keep_fcs=False):
    """Schedule accepted captured bytes and account for every skipped record."""
    result = []
    initial = None
    previous = None
    interface = 0
    truncated = 0
    other_link = 0
    oversize = 0
    trimmed = 0
    for packet in captured:
        if key is not None and packet.key != key:
            interface += 1
            continue
        if packet.link_type != 1:
            other_link += 1
            continue
        if packet.truncated:
            truncated += 1
            continue
        if packet.fcs_bytes > len(packet.captured):
            raise ImportErrorDetail("packet FCS size exceeds the available captured bytes")
        body = packet.captured
        if not keep_fcs and packet.fcs_bytes:
            body = body[:-packet.fcs_bytes]
            trimmed += packet.fcs_bytes
        if not body:
            raise ImportErrorDetail("empty captured frame cannot be replayed")
        if previous is not None and packet.capture_time_ns < previous:
            raise ImportErrorDetail("selected capture timestamps are decreasing")
        latest = packet.capture_time_ns
        if initial is None:
            initial = latest
        cycle, leftover = divmod(Fraction(latest - initial), period)
        cycle += bool(leftover)
        if cycle > MAX_CYCLE:
            raise ImportErrorDetail("timestamp spacing exceeds the fixture horizon")
        index = len(result)
        if index >= MAX_PACKETS:
            raise ImportErrorDetail(f"capture exceeds {MAX_PACKETS} selected packets")
        tag = index & 0xFFFF
        result.append({"tag": tag, "payload": body.hex(), "at": cycle})
        if len(body) > geometry.capacity:
            oversize += 1
        previous = latest
    if not result:
        raise ImportErrorDetail("no complete Ethernet packets matched the selectors")
    recipe = {
        "input_width": geometry.input_width,
        "output_width": geometry.output_width,
        "capacity": geometry.capacity,
        "packets": result,
    }
    stats = ImportStats(len(result), interface, truncated, other_link, oversize, trimmed)
    return recipe, stats


def compile_pcap(capture, target, spec, ns_per_cycle, *, select=None, keep_fcs=False, tape=None):
    """Compile to a human-readable recipe and optionally to simulator edge rows."""
    output = [target] if tape is None else [target, tape]
    for index, path in enumerate(output):
        check_destination(capture, path)
        for other in output[:index]:
            if path.resolve() == other.resolve() or (path.exists() and other.exists() and path.samefile(other)):
                raise ImportErrorDetail("output recipe and output tape refer to the same file")
    driver = shared_capture_module()
    packets = driver.packets(capture)
    recipe, stats = select_rows(packets, spec, ensure_pace(ns_per_cycle),
                                interface_key(select), keep_fcs)
    parsed = Recipe.parse(recipe)
    if tape is not None:
        rows = [encode(row(drive, view)) for drive, view in simulate(parsed)]
        tape.write_text("\n".join(rows) + "\n", encoding="ascii")
    target.write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")
    return stats


def main():
    """Import a bounded pcap under explicit clock and interface options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--input-width", type=int, default=4)
    parser.add_argument("--output-width", type=int, default=8)
    parser.add_argument("--capacity", type=int, default=128)
    parser.add_argument("--period-ns", default="8")
    parser.add_argument("--interface")
    parser.add_argument("--keep-fcs", action="store_true")
    parser.add_argument("--tape", type=Path)
    args = parser.parse_args()
    try:
        spec = Geometry(args.input_width, args.output_width, args.capacity)
        result = compile_pcap(args.source, args.target, spec, args.period_ns,
                              select=args.interface, keep_fcs=args.keep_fcs, tape=args.tape)
        print(json.dumps(result.values(), indent=2, sort_keys=True))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
