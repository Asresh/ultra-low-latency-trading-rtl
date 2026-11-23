"""Rebuild complete packets from the real SV trace and audit lossless conversion."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path

from width_model import Beat, Report, Status, full, validate_packet


class AuditError(ValueError):
    """The capture cannot be produced by a correctly framed gearbox."""


@dataclass(frozen=True)
class Transfer:
    channel: str
    cycle: int
    value: Beat | Report | None


def capture(lines, source_width, sink_width):
    """Read typed trace records and reject corrupt or nonmonotonic rows."""
    previous_clock = -1
    occupied_channels = set()
    for number, line in enumerate(lines, 1):
        fields = line.split()
        try:
            channel, clock = fields[:2]
            cycle = int(clock)
            if cycle < 0 or cycle < previous_clock:
                raise AuditError("clock must be nonnegative and monotonic")
            if cycle > previous_clock:
                occupied_channels.clear()
            if channel in occupied_channels or (
                occupied_channels and (channel == "reset" or "reset" in occupied_channels)
            ):
                raise AuditError("multiple transfers used the same channel or reset edge")
            if channel == "reset" and len(fields) == 2:
                value = None
            elif channel in {"input", "output"} and len(fields) == 6:
                data, keep, last, tag = (int(field, 16) for field in fields[2:])
                if last not in (0, 1):
                    raise AuditError("last flag must be one bit")
                value = Beat(data, keep, bool(last), tag)
                value.validate(source_width if channel == "input" else sink_width)
            elif channel == "report" and len(fields) == 5:
                tag, length, status = (int(field, 16) for field in fields[2:])
                if min(tag, length) < 0 or max(tag, length) > 0xFFFF:
                    raise AuditError("report fields exceed their 16-bit encoding")
                value = Report(tag, length, Status(status))
            else:
                raise AuditError(f"malformed {channel} row")
        except (ValueError, TypeError) as exc:
            raise AuditError(f"line {number}: {exc}") from exc
        previous_clock = cycle
        occupied_channels.add(channel)
        yield Transfer(channel, cycle, value)


class TraceAudit:
    """Check semantic packet and report order independently of the cycle model."""

    def __init__(self, in_width, out_width, maximum):
        """Initialize empty expected packets, outcomes, and coverage counters."""
        self.in_width = in_width
        self.out_width = out_width
        self.maximum = maximum
        self.reports = deque()
        self.valid_packets = deque()
        self.input_frame = []
        self.output_frame = []
        self.statuses = Counter()
        self.output_bytes = 0
        self.resets = 0
        self.completed = 0
        self.abandoned = 0
        self.first_latency = []
        self.last_latency = []
        self.output_start = None

    def apply(self, transfer):
        """Update only the channel indicated by this observed bus transfer."""
        if transfer.channel == "reset":
            self._reset()
        elif transfer.channel == "input":
            self._input(transfer)
        elif transfer.channel == "output":
            self._output(transfer)
        else:
            self._report(transfer)

    def _reset(self):
        """Account for accepted but undelivered work at a reset boundary."""
        self.resets += 1
        self.abandoned += len(self.valid_packets) + bool(self.input_frame)
        self.reports.clear()
        self.valid_packets.clear()
        self.input_frame.clear()
        self.output_frame.clear()
        self.output_start = None

    def _input(self, transfer):
        """Accumulate an input frame, retaining its timestamp at the last beat."""
        beat = transfer.value
        self.input_frame.append(beat)
        if not beat.last:
            return
        report, data = validate_packet(self.input_frame, self.in_width, self.maximum)
        self.input_frame.clear()
        self.reports.append((transfer.cycle, report))
        if data is not None:
            self.valid_packets.append((transfer.cycle, report.tag, data))
        if len(self.valid_packets) > 2:
            raise AuditError("more than two complete packets occupy the banks")

    def _output(self, transfer):
        """Check every output mask, physical padding byte, tag and packet length."""
        beat = transfer.value
        if not self.valid_packets:
            raise AuditError("output without a validated input packet")
        committed_at, tag, data = self.valid_packets[0]
        if committed_at >= transfer.cycle:
            raise AuditError("output appeared before its packet committed")
        if beat.tag != tag:
            raise AuditError("output packet identity changed")
        selected = beat.bytes(self.out_width)
        if not selected or full(len(selected)) != beat.keep:
            raise AuditError("output mask must mark a nonempty low-lane prefix")
        if not beat.last and beat.keep != full(self.out_width):
            raise AuditError("non-final output must occupy every lane")
        if beat.data >> (len(selected) * 8):
            raise AuditError("unused output lanes are not zero")
        if self.output_start is None:
            self.output_start = transfer.cycle
        offset = len(self.output_frame)
        self.output_frame.extend(selected)
        if data[offset:offset + len(selected)] != selected:
            raise AuditError("payload byte order or packet data changed")
        if beat.last != (len(self.output_frame) == len(data)):
            raise AuditError("output packet ended at the wrong byte")
        if beat.last:
            self.output_bytes += len(data)
            self.completed += 1
            self.first_latency.append(self.output_start - committed_at)
            self.last_latency.append(transfer.cycle - committed_at)
            self.valid_packets.popleft()
            self.output_frame.clear()
            self.output_start = None

    def _report(self, transfer):
        """Match the report with the next complete input frame, good or bad."""
        if not self.reports:
            raise AuditError("report without a complete input packet")
        committed_at, expected = self.reports.popleft()
        if committed_at >= transfer.cycle:
            raise AuditError("report appeared before the frame ended")
        if transfer.value != expected:
            raise AuditError(f"report {transfer.value!r} differs from expected {expected!r}")
        self.statuses[expected.status.name] += 1

    def finish(self):
        """Require a clean end and return auditable traffic and latency metrics."""
        if any((self.reports, self.valid_packets, self.input_frame, self.output_frame)):
            raise AuditError("capture ended with transactions still outstanding")
        return {
            "packets": self.completed,
            "bytes": self.output_bytes,
            "reports": dict(self.statuses),
            "resets": self.resets,
            "abandoned_by_reset": self.abandoned,
            "first_beat_min": min(self.first_latency, default=None),
            "first_beat_max": max(self.first_latency, default=None),
            "final_beat_max": max(self.last_latency, default=None),
        }


def verify(path, input_width, output_width, maximum):
    """Audit one simulator trace and return a report suitable for CI logs."""
    auditor = TraceAudit(input_width, output_width, maximum)
    with path.open(encoding="ascii") as trace:
        for transfer in capture(trace, input_width, output_width):
            auditor.apply(transfer)
    return auditor.finish()


def main():
    """Audit the four profiles produced by the Makefile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    for profile, iw, ow, size in (("up", 4, 8, 128), ("down", 8, 4, 128),
                                  ("odd", 3, 5, 31), ("tiny", 1, 2, 3)):
        report = verify(args.directory / f"{profile}.trace", iw, ow, size)
        print(f"PASS {profile} independent captured-packet audit: {report}")


if __name__ == "__main__":
    main()
