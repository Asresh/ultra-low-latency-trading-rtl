"""Compile a human-readable packet recipe into an Icarus-ready cycle tape."""

import argparse
from dataclasses import dataclass, replace
import json
from pathlib import Path

from width_capture import Geometry, check_destination
from width_model import Beat, Drive, Gearbox, full, to_beats
from width_workload import encode, row


class RecipeError(ValueError):
    """A packet or scheduling directive cannot be represented on the bus."""


def fields(value, required, optional=()):
    """Require a mapping with known keys and every mandatory field."""
    if not isinstance(value, dict):
        raise RecipeError("expected an object")
    must = set(required)
    found = set(value)
    missing = must - found
    extras = found - must - set(optional)
    if missing:
        raise RecipeError(f"required fields missing: {', '.join(sorted(missing))}")
    if extras:
        raise RecipeError(f"unrecognized fields: {', '.join(sorted(extras))}")
    return value


def integer(value, name, maximum, minimum=0):
    """Bounds-check a JSON integer while rejecting true/false aliases."""
    if type(value) is not int or not minimum <= value <= maximum:
        raise RecipeError(f"{name} must be an integer in {minimum}–{maximum}")
    return value


def list_values(value, name):
    """Accept an actual JSON array without interpreting a string as elements."""
    if not isinstance(value, list):
        raise RecipeError(f"{name} requires an array")
    return value


def decode_hex(payload):
    """Accept nonempty pairs of hexadecimal digits with optional byte whitespace."""
    if not isinstance(payload, str):
        raise RecipeError("payload must be hexadecimal text")
    try:
        data = bytes.fromhex(payload)
    except ValueError as exc:
        raise RecipeError("payload is not valid hexadecimal text") from exc
    if not data:
        raise RecipeError("each packet must contain at least one byte")
    if len(data) > 262144:
        raise RecipeError("fixture payload exceeds 262144-byte per-packet budget")
    return data


@dataclass(frozen=True)
class Window:
    start: int
    stop: int

    @classmethod
    def parse(cls, value):
        """Read a finite half-open interval for one ready-equals-zero window."""
        if not isinstance(value, list) or len(value) != 2:
            raise RecipeError("stall window must be [first_cycle, end_cycle)")
        start = integer(value[0], "stall start", 499999)
        stop = integer(value[1], "stall end", 500000, start + 1)
        return cls(start, stop)

    def covers(self, cycle):
        """Test inclusion while leaving the endpoint ready again."""
        return self.start <= cycle < self.stop


def ordered_windows(value, label):
    """Accept sorted disjoint stalls, so simulated pause boundaries are explicit."""
    result = sorted(
        (Window.parse(window) for window in list_values(value, label)),
        key=lambda item: item.start,
    )
    if any(left.stop > right.start for left, right in zip(result, result[1:])):
        raise RecipeError(f"{label} windows overlap")
    return tuple(result)


@dataclass(frozen=True)
class Edit:
    beat: int
    keep: int = None
    tag: int = None
    data: int = None

    @classmethod
    def parse(cls, value, width, total):
        """Validate a deliberate single-beat corruption against physical port widths."""
        fields(value, {"beat"}, {"keep", "tag", "data"})
        if len(value) < 2:
            raise RecipeError("beat edit must change keep, tag or data")
        beat = integer(value["beat"], "beat index", total - 1)
        keep = integer(value["keep"], "lane mask", full(width)) if "keep" in value else None
        tag = integer(value["tag"], "tag", 65535) if "tag" in value else None
        data = integer(value["data"], "beat word", (1 << (width * 8)) - 1) if "data" in value else None
        return cls(beat, keep, tag, data)

    def apply(self, source):
        """Overlay just the selected fields, preserving the framing marker."""
        edits = {"keep": self.keep, "tag": self.tag, "data": self.data}
        return replace(source, **{key: value for key, value in edits.items() if value is not None})


@dataclass(frozen=True)
class Packet:
    tag: int
    bytes_in_order: bytes
    start: int
    interbeat: int
    pad: int
    edits: tuple

    @classmethod
    def parse(cls, value, width):
        """Parse one packet, preparation timing, padding, and optional sparse lane faults."""
        fields(value, {"tag", "payload"}, {"at", "interbeat", "pad", "edits"})
        tag = integer(value["tag"], "tag", 65535)
        payload = decode_hex(value["payload"])
        start = integer(value.get("at", 0), "start cycle", 499999)
        interbeat = integer(value.get("interbeat", 0), "interbeat gap", 999)
        pad = integer(value.get("pad", 0), "padding byte", 255)
        total = (len(payload) + width - 1) // width
        edits = tuple(
            Edit.parse(edit, width, total)
            for edit in list_values(value.get("edits", []), "edits")
        )
        if len({edit.beat for edit in edits}) != len(edits):
            raise RecipeError("a beat can only have one edit")
        return cls(tag, payload, start, interbeat, pad, edits)

    def beats(self, width):
        """Resolve edits against packed physical words in timestamp-independent order."""
        by_index = {edit.beat: edit for edit in self.edits}
        for index, beat in enumerate(to_beats(self.bytes_in_order, width, self.tag, self.pad)):
            edit = by_index.get(index)
            yield edit.apply(beat) if edit else beat


@dataclass(frozen=True)
class Recipe:
    geometry: Geometry
    packets: tuple
    output_stalls: tuple
    report_stalls: tuple

    @classmethod
    def parse(cls, value):
        """Load a complete recipe with a nonempty monotonic list of packet arrivals."""
        fields(value, {"input_width", "output_width", "capacity", "packets"},
               {"output_stalls", "report_stalls"})
        geometry = Geometry(
            integer(value["input_width"], "input width", 16, 1),
            integer(value["output_width"], "output width", 16, 1),
            integer(value["capacity"], "capacity", 65535, 1),
        )
        packets = []
        for index, raw in enumerate(list_values(value["packets"], "packets"), 1):
            try:
                packets.append(Packet.parse(raw, geometry.input_width))
            except RecipeError as exc:
                raise RecipeError(f"packet {index}: {exc}") from exc
        if not packets:
            raise RecipeError("recipe must contain at least one packet")
        if any(left.start > right.start for left, right in zip(packets, packets[1:])):
            raise RecipeError("packet arrivals must be ordered by start cycle")
        output_stalls = ordered_windows(value.get("output_stalls", []), "output stalls")
        report_stalls = ordered_windows(value.get("report_stalls", []), "report stalls")
        return cls(geometry, tuple(packets), output_stalls, report_stalls)


class Source:
    """Hold the same beat through backpressure and advance only on transfer."""

    def __init__(self, recipe):
        """Initialize an iterator over packet and beat state."""
        self.pending = iter(recipe.packets)
        self.width = recipe.geometry.input_width
        self.active = None
        self.index = 0
        self.next_cycle = 0
        self.held = None
        self.done = False

    def offer(self, cycle):
        """Present the first eligible beat without discarding it on a stall."""
        if self.active is None and not self.done:
            packet = next(self.pending, None)
            if packet is None:
                self.done = True
            else:
                self.active = packet
                self.stream = iter(packet.beats(self.width))
                self.index = 0
                self.next_cycle = max(cycle, packet.start)
        if self.active is not None and cycle >= self.next_cycle and self.held is None:
            self.held = next(self.stream)
        return self.held

    def retire(self, cycle):
        """Finish a packet or establish the next beat's earliest eligible cycle."""
        if self.held is None:
            raise RuntimeError("source retirement requires an offered beat")
        if self.held.last:
            self.active = None
        else:
            self.index += 1
            self.next_cycle = cycle + 1 + self.active.interbeat
        self.held = None


def channel_ready(windows, cycle):
    """Check that a given cycle is outside every explicit pause."""
    return not any(window.covers(cycle) for window in windows)


def simulate(recipe, max_cycles=500000):
    """Replay source and sink policy through the cycle oracle and yield each edge."""
    spec = recipe.geometry
    model = Gearbox(spec.input_width, spec.output_width, spec.capacity)
    source = Source(recipe)
    for cycle in range(max_cycles):
        if cycle == 0:
            drive = Drive(reset=True)
        else:
            beat = source.offer(cycle)
            drive = Drive(beat is not None, beat or Beat(),
                          channel_ready(recipe.output_stalls, cycle),
                          channel_ready(recipe.report_stalls, cycle))
        observed = model.advance(drive)
        yield drive, observed
        if drive.valid and observed.input_ready:
            source.retire(cycle)
        if source.done and not model.pending and model.report is None:
            return
    raise RecipeError(f"recipe did not drain within {max_cycles} cycles")


def compile_recipe(source, target):
    """Validate a recipe and avoid overwriting either input or a previous valid tape on failure."""
    check_destination(source, target)
    with source.open(encoding="utf-8") as file:
        recipe = Recipe.parse(json.load(file))
    rows = [encode(row(drive, observed)) for drive, observed in simulate(recipe)]
    target.write_text("\n".join(rows) + "\n", encoding="ascii")
    return len(rows)


def main():
    """Compile one explicit workload for experimentation with the RTL."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe", type=Path)
    parser.add_argument("tape", type=Path)
    args = parser.parse_args()
    try:
        print(f"Compiled {compile_recipe(args.recipe, args.tape)} edges.")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
