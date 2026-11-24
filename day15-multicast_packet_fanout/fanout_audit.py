"""Reconstruct packet deliveries directly from a fanout RTL capture."""

import argparse
from collections import Counter, deque
from dataclasses import dataclass, field
import hashlib
import json
from itertools import zip_longest
from pathlib import Path

from fanout_model import PROFILES, Status


@dataclass
class SourcePacket:
    """Accepted source bytes, order, and route snapshot within one reset epoch."""

    epoch: int
    ordinal: int
    first_cycle: int
    group: int
    tag: int
    mask: int
    chunks: list = field(default_factory=list)
    last_cycle: int = -1


@dataclass
class Subscription:
    """Expected receiver progression for a packet committed by the RTL."""

    packet: SourcePacket
    cursor: int = 0
    first_cycle: int = -1


def rows(path, width):
    """Yield unsigned hexadecimal rows and diagnose malformed capture records."""
    with path.open() as source:
        for index, line in enumerate(source, 1):
            fields = line.split()
            if len(fields) != width:
                raise ValueError(f"{path}:{index}: expected {width} fields, got {len(fields)}")
            try:
                words = tuple(int(word, 16) for word in fields)
            except ValueError as error:
                raise ValueError(f"{path}:{index}: invalid hexadecimal field") from error
            if any(word < 0 for word in words):
                raise ValueError(f"{path}:{index}: signed fields are invalid")
            yield words


def take(word, index, width):
    """Extract the port-indexed slice from a packed bit vector."""
    return (word >> (index * width)) & ((1 << width) - 1)


def selected(data, keep, lanes):
    """Return selected payload bytes in ascending source-lane order."""
    return bytes(take(data, lane, 8) for lane in range(lanes) if keep >> lane & 1)


def check(condition, cycle, message):
    """Report a failed capture invariant without depending on Python assert mode."""
    if not condition:
        raise ValueError(f"cycle {cycle}: {message}")


class CaptureAudit:
    """Audit order and bytes using observed handshakes, independently of the oracle."""

    def __init__(self, profile):
        """Record geometry and initialize the capture-wide summaries."""
        self.p = profile
        self.epoch = 0
        self.deliveries = []
        self.reports = []
        self.cycles = 0
        self.counts = Counter()
        self.reset()

    def reset(self):
        """Discard open packet and subscriptions at a reset edge."""
        self.frame = None
        self.due_report = None
        self.report_latch = None
        self.routes = [0] * self.p.groups
        self.queues = [deque() for _ in range(self.p.ports)]
        self.held_outputs = [None] * self.p.ports
        self.ordinal = 0

    def record_report(self, cycle, rtl, ready):
        """Check that a terminal report describes the preceding accepted packet."""
        valid = rtl[8]
        fields = tuple(rtl[9:14])
        if self.report_latch is not None:
            check(valid and self.report_latch == fields, cycle, "held report changed")
        frame = self.due_report
        if frame is None:
            check(bool(valid) == (self.report_latch is not None), cycle, "unexpected report validity")
        else:
            check(valid, cycle, "accepted terminal beat produced no report")
            code, tag, group, mask, count = fields
            status = Status(code)
            byte_count = min(65535, sum(map(len, frame.chunks)))
            check((tag, group, mask, count) == (frame.tag, frame.group, frame.mask, byte_count),
                  cycle, "report tag, group, snapshot mask, or byte count differs from ingress")
            self.reports.append(dict(epoch=frame.epoch, ordinal=frame.ordinal, tag=tag,
                                     group=group, destinations=mask, status=status.name,
                                     first_cycle=frame.first_cycle, last_cycle=frame.last_cycle,
                                     input_bytes=count))
            self.counts[status.name] += 1
            if status == Status.FORWARDED:
                check(mask != 0, cycle, "forwarded packet has no subscriber")
                for port, queue in enumerate(self.queues):
                    if mask >> port & 1:
                        queue.append(Subscription(frame))
        self.due_report = None
        self.report_latch = fields if valid and not ready else None

    def record_outputs(self, cycle, rtl, ready):
        """Check every offered beat against its subscriber's queued packet and lane order."""
        for port, queue in enumerate(self.queues):
            valid = take(rtl[3], port, 1)
            check(bool(queue) == bool(valid), cycle, f"port {port} output valid differs from queued data")
            if not valid:
                continue
            last = take(rtl[4], port, 1)
            data = take(rtl[5], port, self.p.data_bytes * 8)
            keep = take(rtl[6], port, self.p.data_bytes)
            tag = take(rtl[7], port, self.p.tag_width)
            fields = data, keep, last, tag
            if self.held_outputs[port] is not None:
                check(fields == self.held_outputs[port], cycle, f"port {port} changed a held beat")
            subscription = queue[0]
            packet = subscription.packet
            chunk = packet.chunks[subscription.cursor]
            want_last = subscription.cursor + 1 == len(packet.chunks)
            check(tag == packet.tag, cycle, f"port {port} expected tag {packet.tag:#x}, got {tag:#x}")
            check(last == want_last, cycle, f"port {port} packet boundary differs from input")
            check(keep == (1 << len(chunk)) - 1, cycle, f"port {port} byte mask differs from input")
            check(data == int.from_bytes(chunk, "little"), cycle,
                  f"port {port} data or cleared padding differs from input")
            if not (ready >> port & 1):
                self.held_outputs[port] = fields
                continue
            self.held_outputs[port] = None
            if subscription.cursor == 0:
                subscription.first_cycle = cycle
            subscription.cursor += 1
            if want_last:
                self.finish_delivery(port, cycle, queue.popleft())

    def finish_delivery(self, port, cycle, subscription):
        """Store a byte-level fingerprint and timing record for a completed replica."""
        packet = subscription.packet
        payload = b"".join(packet.chunks)
        self.deliveries.append(dict(
            epoch=packet.epoch, ordinal=packet.ordinal, port=port, tag=packet.tag,
            bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
            first_cycle=subscription.first_cycle, last_cycle=cycle,
            first_after_input=subscription.first_cycle - packet.last_cycle,
        ))

    def record_input(self, cycle, stimulus, rtl):
        """Build a source packet from actual edges before applying a route write."""
        _, cfg_valid, cfg_group, mask, _, valid, data, keep, last, group, tag, _, _ = stimulus
        if valid and rtl[2]:
            if self.frame is None:
                route = self.routes[group] if group < self.p.groups else 0
                self.frame = SourcePacket(self.epoch, self.ordinal, cycle, group, tag, route)
                self.ordinal += 1
            self.frame.chunks.append(selected(data, keep, self.p.data_bytes))
            if last:
                self.frame.last_cycle = cycle
                self.due_report = self.frame
                self.frame = None
        if cfg_valid and rtl[1] and cfg_group < self.p.groups:
            self.routes[cfg_group] = mask

    def update(self, cycle, stimulus, rtl):
        """Apply one observed edge, retiring subscribers independently."""
        check(cycle == rtl[0], cycle, "trace numbering is discontinuous")
        self.cycles = cycle + 1
        rst, *_, ready, report_ready = stimulus
        if rst:
            check(rtl[1:5] == (0, 0, 0, 0), cycle, "a data or configuration channel active in reset")
            self.epoch += 1
            self.reset()
            return
        self.record_report(cycle, rtl, report_ready)
        self.record_outputs(cycle, rtl, ready)
        self.record_input(cycle, stimulus, rtl)

    def summary(self, allow_pending):
        """Return the capture records and assert completion for a drained trace."""
        pending = dict(input_frame=self.frame is not None, report=self.due_report is not None
                       or self.report_latch is not None, subscribers=sum(map(len, self.queues)))
        if not allow_pending and any(pending.values()):
            raise ValueError(f"capture ended with live state: {pending}")
        return dict(cycles=self.cycles, reset_edges=self.epoch, statuses=dict(self.counts),
                    pending=pending, reports=self.reports, deliveries=self.deliveries)


def audit(profile, source, observed, allow_pending=False):
    """Read paired stimulus and RTL trace files and return a JSON-ready audit."""
    capture = CaptureAudit(profile)
    for cycle, (stimulus, rtl) in enumerate(zip_longest(rows(source, 13), rows(observed, 21))):
        check(stimulus is not None and rtl is not None, cycle, "stimulus and RTL trace lengths differ")
        capture.update(cycle, stimulus, rtl)
    return capture.summary(allow_pending)


def main():
    """Audit a captured simulation without running the model or simulator again."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="default")
    parser.add_argument("--allow-pending", action="store_true")
    parser.add_argument("source", type=Path)
    parser.add_argument("observed", type=Path)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    try:
        result = audit(PROFILES[options.profile], options.source, options.observed, options.allow_pending)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Capture audit failed: {error}\n")
    payload = json.dumps(result, indent=2) + "\n"
    if options.output is None:
        print(payload, end="")
    else:
        check(options.output.resolve() not in (options.source.resolve(), options.observed.resolve()),
              0, "output would overwrite a capture input")
        options.output.write_text(payload)


if __name__ == "__main__":
    main()
