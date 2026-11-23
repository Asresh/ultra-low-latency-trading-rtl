"""Compile a typed offline configuration into ordered AXI-Lite transactions."""

import argparse
from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path
import sys

from latency_reference import HALF64, LIMIT64, Status
from pps_calibrate import Q32, nearest_fixed

ID = 0x000
GEOMETRY = 0x004
COMMAND = 0x008
STATUS = 0x00C
PERIOD_INT = 0x010
PERIOD_FRAC = 0x014
TIMEOUT_LOW = 0x018
TIMEOUT_HIGH = 0x01C
THRESHOLD_LOW = 0x020
THRESHOLD_HIGH = 0x024
TRACE_CONTROL = 0x028
RAW_LOW = 0x030
RAW_HIGH = 0x034
TIME_LOW = 0x038
TIME_HIGH = 0x03C
TIME_FRAC = 0x040
HIST_COMPLETED = 0x050
TRACE_COMPLETED = 0x054
DEVICE_SIGNATURE = 0x4C415431


class ConfigError(ValueError):
    """The input cannot be represented by the telemetry register contract."""


def parse_integer(options, key, maximum):
    """Read and validate a bounded integer option before encoding bus data."""
    data = options.get(key, 0)
    if type(data) is not int or not 0 <= data <= maximum:
        raise ConfigError(f"{key} must be an unsigned integer <= {maximum}")
    return data


def parse_flag(options, key, default=False):
    """Read a Boolean setting and apply its documented default if absent."""
    data = options.get(key, default)
    if type(data) is not bool:
        raise ConfigError(f"{key} must be a JSON boolean")
    return data


def reject_unknown(options, known):
    """Reject unrecognized settings so misspellings cannot silently change a plan."""
    if not isinstance(options, dict):
        raise ConfigError("expected an object")
    extras = sorted(options.keys() - known)
    if extras:
        raise ConfigError(f"unrecognized fields: {', '.join(extras)}")


@dataclass(frozen=True)
class Bus:
    operation: str
    offset: int
    value: int = 0
    mask: int = 0
    strobes: int = 0

    @classmethod
    def write(cls, offset, value, enables=15):
        """Construct a register write with validated address, value and byte lanes."""
        if offset & 3 or not 0 <= value < 1 << 32 or not 1 <= enables <= 15:
            raise ConfigError("AXI write exceeds register geometry")
        return cls("write", offset, value, 0, enables)

    @classmethod
    def poll(cls, offset, mask, want):
        """Construct a read-until-match operation using masked expected bits."""
        if offset & 3 or not 0 < mask < 1 << 32 or want & ~mask:
            raise ConfigError("invalid poll mask or comparison")
        return cls("poll", offset, want, mask)

    @classmethod
    def read(cls, offset):
        """Construct a checked read from the requested register offset."""
        if offset & 3:
            raise ConfigError("AXI read must be word-aligned")
        return cls("read", offset)

    def json(self):
        """Encode the bus operation as a plain object for a host-side executor."""
        record = {"op": self.operation, "addr": f"0x{self.offset:03x}"}
        if self.operation == "write":
            record.update({"data": f"0x{self.value:08x}", "wstrb": f"0x{self.strobes:x}"})
        if self.operation == "poll":
            record.update({"mask": f"0x{self.mask:08x}", "want": f"0x{self.value:08x}"})
        return record


def pair_words(low_offset, high_offset, value):
    """Split one wide value into little-endian low and high register writes."""
    return [Bus.write(low_offset, value & 0xFFFFFFFF), Bus.write(high_offset, value >> 32)]


@dataclass(frozen=True)
class TraceConfiguration:
    enable: bool
    post: int
    threshold: int
    statuses: tuple
    arm: bool

    @classmethod
    def from_mapping(cls, options, depth):
        """Construct a validated typed record from external field names and values."""
        reject_unknown(options, {"enable", "post", "threshold_ticks", "statuses", "arm"})
        enable = parse_flag(options, "enable", default=True)
        post = parse_integer(options, "post", depth-1)
        threshold = parse_integer(options, "threshold_ticks", LIMIT64)
        arm = parse_flag(options, "arm")
        status_names = options.get("statuses", [])
        if not isinstance(status_names, list) or not all(type(s) is str for s in status_names):
            raise ConfigError("trace statuses must be a JSON list of names")
        try:
            status_values = tuple(Status[s.upper()] for s in status_names)
        except KeyError as exc:
            raise ConfigError(f"unrecognized trace status {exc.args[0]}") from exc
        if len(set(status_values)) != len(status_values):
            raise ConfigError("duplicate trigger status")
        if arm and (not enable or (not threshold and not status_values)):
            raise ConfigError("armed trace needs capture enabled and at least one trigger")
        return cls(enable, post, threshold, status_values, arm)

    def bus_words(self):
        """Pack checked trace settings into the device's register representation."""
        mask = sum(1 << status for status in self.statuses)
        control = (int(self.enable) << 16) | (self.post << 8) | mask
        return [
            *pair_words(THRESHOLD_LOW, THRESHOLD_HIGH, self.threshold),
            Bus.write(TRACE_CONTROL, control),
        ]


def synthesize_plan(options, trace_depth=16):
    """Compile user settings into ordered bus operations, with capture quiesced during changes."""
    keys = {"period_ns", "timeout_ticks", "armed", "quiesce", "flush",
            "clear_counts", "snapshot", "latch_clock", "trace"}
    reject_unknown(options, keys)
    if trace_depth < 2 or trace_depth > 128 or trace_depth & (trace_depth - 1):
        raise ConfigError("trace depth must be a power of two in 2..128")
    armed = parse_flag(options, "armed")
    quiesce = parse_flag(options, "quiesce", default=True)
    commands = int(armed)
    transactions = [Bus.poll(ID, 0xFFFFFFFF, DEVICE_SIGNATURE), Bus.read(GEOMETRY)]
    if quiesce:
        transactions.extend([Bus.write(COMMAND, 0, 1), Bus.poll(STATUS, 1, 0)])

    if "period_ns" in options:
        raw = options["period_ns"]
        if type(raw) not in (int, str):
            raise ConfigError("period_ns must be a decimal string or integer")
        try:
            period = Fraction(raw)
        except (ValueError, ZeroDivisionError) as exc:
            raise ConfigError("period_ns has invalid exact decimal syntax") from exc
        word = nearest_fixed(period, Q32)
        if not 0 < word < (1 << 64):
            raise ConfigError("period_ns must be positive and fit an unsigned 32.32 increment")
        transactions.extend([
            Bus.poll(STATUS, 0x40, 0),
            Bus.write(PERIOD_INT, word >> 32),
            Bus.write(PERIOD_FRAC, word & 0xFFFFFFFF),
        ])
        commands |= 1 << 4

    if "timeout_ticks" in options:
        timeout = parse_integer(options, "timeout_ticks", HALF64-1)
        if timeout == 0:
            raise ConfigError("timeout_ticks must be nonzero")
        transactions.extend(pair_words(TIMEOUT_LOW, TIMEOUT_HIGH, timeout))
        commands |= 1 << 5

    for field, bit in (("snapshot", 1), ("clear_counts", 2), ("flush", 3), ("latch_clock", 7)):
        if parse_flag(options, field):
            commands |= 1 << bit

    if commands & (1 << 1):
        transactions.append(Bus.poll(STATUS, 0x80, 0))

    if "trace" in options:
        trace = TraceConfiguration.from_mapping(options["trace"], trace_depth)
        transactions.extend(trace.bus_words())
        if trace.arm:
            transactions.append(Bus.poll(STATUS, 0x20, 0))
            commands |= 1 << 6

    transactions.append(Bus.write(COMMAND, commands, 1))
    seen_mask = 1 | (0x40 if commands & 0x10 else 0)
    transactions.append(Bus.poll(STATUS, seen_mask, int(armed)))
    if commands & (1 << 7):
        transactions.extend(Bus.read(x) for x in (RAW_LOW, RAW_HIGH, TIME_LOW, TIME_HIGH, TIME_FRAC))
    if commands & (1 << 1):
        transactions.append(Bus.read(HIST_COMPLETED))
    if commands & (1 << 6):
        transactions.append(Bus.read(TRACE_COMPLETED))
    return transactions


def main():
    """Validate a configuration file and emit the register plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="JSON fixture")
    parser.add_argument("--trace-depth", type=int, default=16)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        plan = synthesize_plan(config, args.trace_depth)
        print(json.dumps([op.json() for op in plan], indent=2))
    except (ConfigError, OSError, ValueError, TypeError) as failure:
        print(f"latency_config: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
