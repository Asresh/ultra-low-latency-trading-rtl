#!/usr/bin/env python3
"""Render a selected time window of an RTL simulation VCD as a standalone SVG."""

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
import sys


NS = {
    "s": Decimal("1e9"), "ms": Decimal("1e6"), "us": Decimal("1e3"),
    "ns": Decimal(1), "ps": Decimal("1e-3"), "fs": Decimal("1e-6"),
}
INK = "#12253b"
LABEL = "#42566b"
SIGNAL = "#06756f"


@dataclass(frozen=True)
class Variable:
    """Bind a VCD identifier to its logical width, including aliases."""

    code: str
    width: int


@dataclass(frozen=True)
class Channel:
    """A waveform lane with its display label and chronological changes."""

    label: str
    width: int
    events: list


def declaration_tokens(source):
    """Read header directives through enddefinitions, leaving the body unread."""
    pending = []
    for line in source:
        pending.extend(line.split())
        while "$end" in pending:
            end = pending.index("$end")
            directive, pending = pending[:end], pending[end + 1:]
            if directive:
                yield directive
            if directive and directive[0] == "$enddefinitions":
                return
    raise ValueError("VCD header has no enddefinitions")


def read_header(source):
    """Resolve full hierarchical signal names and the time unit in nanoseconds."""
    prefix = []
    signals = {}
    scale = None
    for record in declaration_tokens(source):
        kind, *fields = record
        if kind == "$scope":
            prefix.append(fields[1])
        elif kind == "$upscope":
            prefix.pop()
        elif kind == "$var":
            variable_kind, width, code, reference, *_ = fields
            if variable_kind not in ("real", "realtime", "string"):
                signals[".".join([*prefix, reference])] = Variable(code, int(width))
        elif kind == "$timescale":
            size_and_unit = "".join(fields)
            end = len(size_and_unit.rstrip("abcdefghijklmnopqrstuvwxyz"))
            try:
                scale = Decimal(size_and_unit[:end]) * NS[size_and_unit[end:]]
            except (InvalidOperation, KeyError) as exc:
                raise ValueError(f"unsupported VCD timescale: {size_and_unit}") from exc
    if scale is None or scale <= 0:
        raise ValueError("VCD timescale is missing or invalid")
    return signals, scale


def read_changes(source, wanted, scale, start, end):
    """Capture the value at the left boundary and all subsequent selected changes."""
    changes = {code: [(start, "x")] for code in wanted}
    now = Decimal(0)
    for raw in source:
        line = raw.strip()
        if not line or line[0] == "$":
            continue
        if line[0] == "#":
            now = int(line[1:]) * scale
            if now > end:
                break
            continue
        if line[0] in "01xXzZ":
            value, code = line[0].lower(), line[1:]
        elif line[0] in "bB":
            value, code = line[1:].split()
            value = value.lower()
        else:
            continue
        lane = changes.get(code)
        if lane is None:
            continue
        if now <= start:
            lane[0] = (start, value)
        elif lane[-1][0] == now:
            if len(lane) > 1 and lane[-2][1] == value:
                lane.pop()
            else:
                lane[-1] = (now, value)
        elif lane[-1][1] != value:
            lane.append((now, value))
    return changes


def read_vcd(source, selection, start, end):
    """Read only requested scalar and vector signal data from a VCD stream."""
    signals, scale = read_header(source)
    requested = []
    for item in selection:
        path, _, label = item.partition("=")
        if path not in signals:
            raise ValueError(f"signal {path!r} was not found in the VCD header")
        requested.append((label or path.rsplit(".", 1)[-1], signals[path]))
    changes = read_changes(source, {var.code for _, var in requested}, scale, start, end)
    return [Channel(label, var.width, changes[var.code]) for label, var in requested]


def ns_text(time):
    """Use a compact fixed decimal representation for ruler labels."""
    return format(time.normalize(), "f")


def bus_text(value):
    """Display binary data in hex and retain explicit unknown and high-Z labels."""
    if "x" in value or "z" in value:
        if len(value) > 12:
            return "X/Z"
        return value.upper()
    return f"0x{int(value, 2):X}"


def grid_times(start, end):
    """Choose readable ruler spacing on 1, 2 or 5 multiples of a power of ten."""
    target = (end - start) / 9
    unit = Decimal(10) ** target.adjusted()
    spacing = next(unit * factor for factor in (1, 2, 5, 10) if unit * factor >= target)
    tick = (start // spacing + 1) * spacing
    yield start
    while tick < end:
        if tick - start >= spacing / 2 and end - tick >= spacing / 2:
            yield tick
        tick += spacing
    yield end


def svg_text(x, y, label, style="", anchor="start"):
    """Emit an escaped SVG text node with consistent positioning."""
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" class="{style}" text-anchor="{anchor}">'
        f'{escape(label)}</text>'
    )


def draw_scalar(events, end, center, position):
    """Draw held scalar levels and use dotted midline segments for X or Z."""
    prior_y = None
    for index, (at, value) in enumerate(events):
        following = events[index + 1][0] if index + 1 < len(events) else end
        begin, finish = position(at), position(following)
        level = center + {"0": 11, "1": -11}.get(value, 0)
        if prior_y is not None and prior_y != level:
            yield f'<path class="edge" d="M{begin:.2f} {prior_y:.2f}V{level:.2f}"/>'
        if at < following:
            style = "trace" if value in ("0", "1") else "unknown"
            yield f'<path class="{style}" d="M{begin:.2f} {level:.2f}H{finish:.2f}"/>'
        prior_y = level


def draw_bus(events, end, center, position):
    """Draw a bus value per interval with clipped diamonds at transitions."""
    for index, (at, value) in enumerate(events):
        following = events[index + 1][0] if index + 1 < len(events) else end
        if at == following:
            continue
        begin, finish = position(at), position(following)
        half_tip = min(5, (finish - begin) / 3)
        upper, lower = center - 12, center + 12
        style = "bus gap" if "x" in value or "z" in value else "bus"
        path = (
            f"M{begin:.2f} {center:.2f}L{begin+half_tip:.2f} {upper:.2f}"
            f"H{finish-half_tip:.2f}L{finish:.2f} {center:.2f}"
            f"L{finish-half_tip:.2f} {lower:.2f}H{begin+half_tip:.2f}Z"
        )
        yield f'<path class="{style}" d="{path}"/>'
        label = bus_text(value)
        if finish - begin > len(label) * 7 + 10:
            yield svg_text((begin + finish) / 2, center + 4, label, "value", "middle")


def render(channels, start, end, title):
    """Render a legible, fixed-layout image for documentation and GitHub previews."""
    width, origin, right = 1160, 218, 1127
    first_y, lane_height = 100, 43
    axis_y = first_y + len(channels) * lane_height
    height = axis_y + 68

    def position(time):
        """Convert nanoseconds to an x-coordinate within the chosen window."""
        return origin + float((time - start) / (end - start)) * (right - origin)

    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="wave-title">',
        f'<title id="wave-title">{escape(title)}</title>',
        '<style>',
        f'text{{fill:{INK};font:13px ui-monospace,Menlo,Consolas,monospace}}',
        '.title{font:600 24px system-ui,sans-serif}',
        f'.sub,.axis{{fill:{LABEL}}}',
        '.ruler{stroke:#d2dfe8;stroke-width:1}',
        f'.trace,.edge{{fill:none;stroke:{SIGNAL};stroke-width:2.3}}',
        '.edge{opacity:.7}',
        '.unknown{fill:none;stroke:#987542;stroke-width:2;stroke-dasharray:4 3}',
        f'.bus{{fill:#e1f3ef;stroke:{SIGNAL};stroke-width:1.5}}',
        '.gap{fill:#fff2d6;stroke:#987542}',
        '.value{font-size:12px}',
        '</style>',
        '<rect width="100%" height="100%" rx="14" fill="#f5f9fc"/>',
        svg_text(28, 38, title, "title"),
        svg_text(28, 64, "RTL simulation · bus values in hex · X/Z shown in amber", "sub"),
    ]
    for index, channel in enumerate(channels):
        y = first_y + index * lane_height
        if index % 2 == 0:
            elements.append(f'<rect x="22" y="{y-20}" width="{width-44}" height="{lane_height}" rx="4" fill="#ebf2f7"/>')
        label = channel.label if len(channel.label) <= 24 else channel.label[:23] + "…"
        elements.append(svg_text(32, y + 5, label))
    for time in grid_times(start, end):
        x = position(time)
        elements.append(f'<path class="ruler" d="M{x:.2f} {first_y-20}V{axis_y-18}"/>')
        elements.append(svg_text(x, axis_y + 6, ns_text(time), "axis", "middle"))
    for index, channel in enumerate(channels):
        draw = draw_scalar if channel.width == 1 else draw_bus
        elements.extend(draw(channel.events, end, first_y + index * lane_height, position))
    elements.extend((svg_text(right, axis_y + 32, "Time (ns)", "axis", "end"), "</svg>"))
    return "\n".join(elements) + "\n"


def main():
    """Parse a waveform request, report invalid inputs, and write the image."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vcd", type=Path)
    parser.add_argument("svg", type=Path)
    parser.add_argument("--start", type=Decimal, required=True, help="window start in ns")
    parser.add_argument("--end", type=Decimal, required=True, help="window end in ns")
    parser.add_argument("--title", required=True)
    parser.add_argument("--signal", action="append", required=True, help="scope.signal=Label")
    args = parser.parse_args()
    if not (args.start.is_finite() and args.end.is_finite() and 0 <= args.start < args.end):
        parser.error("time window must be finite with 0 <= start < end")
    try:
        with args.vcd.open(encoding="utf-8") as source:
            channels = read_vcd(source, args.signal, args.start, args.end)
        args.svg.parent.mkdir(parents=True, exist_ok=True)
        args.svg.write_text(render(channels, args.start, args.end, args.title), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"waveform: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
