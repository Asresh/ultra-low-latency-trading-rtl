"""Create fault-injected dual-feed transactions and independent golden records."""

import argparse
from collections import namedtuple
from pathlib import Path
import random

from wire_codec import (
    MASK64, decode_itch, decode_packet, make_itch, make_packet, make_request, words,
)

(CONFIG, BURST, WAIT, STATUS, REQUEST, FLOW, STAT, EVENTS, NO_REQUEST,
 BENCHMARK, STAT_PEEK, OCCUPANCY_PEAK) = range(12)
SESSION = b"SIMREG0001"
OTHER = b"SIMREG9999"
Limits = namedtuple("Limits", "packet message blocks")
LIMITS = Limits(256, 64, 16)


class Suite:
    # Start an isolated scenario with a seeded RNG and separate stream, program, and event tapes.
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.instructions = []
        self.lanes = [[], []]
        self.events = []

    # Encode one testbench command with its five fixed-width operand slots.
    def command(self, opcode, *arguments):
        arguments = arguments + (0,) * (5 - len(arguments))
        assert len(arguments) == 5
        self.instructions.append(
            f"{opcode:d} " + " ".join(f"{number:040x}" for number in arguments)
        )

    # Queue a session configuration with a 64-bit initial receive sequence.
    def config(self, sequence, session=SESSION):
        self.command(CONFIG, int.from_bytes(session, "big"), sequence & MASK64)

    # Packetize both input lanes and queue them together under a selected source timing profile.
    def burst(self, lane_a=(), lane_b=(), source_profile=0):
        self.burst_words(
            [beat for packet in lane_a for beat in words(packet, self.rng)],
            [beat for packet in lane_b for beat in words(packet, self.rng)],
            source_profile,
        )

    # Append raw beats to each lane and record how many the bench should consume.
    def burst_words(self, lane_a, lane_b, source_profile=0):
        self.lanes[0].extend(lane_a)
        self.lanes[1].extend(lane_b)
        self.command(BURST, len(lane_a), len(lane_b), source_profile)

    # Inject one malformed bus field into a selected packet beat and schedule it on one lane.
    def fault(self, packet, beat, field, replacement, *, lane=0):
        beats = [list(item) for item in words(packet, self.rng)]
        beats[beat][field] = replacement
        self.burst_words(beats if lane == 0 else (), beats if lane == 1 else ())

    # Advance the bench by a fixed number of idle cycles.
    def idle(self, cycles):
        self.command(WAIT, cycles)

    # Check the receive cursor, watermark, flags, and reorder occupancy.
    def status(self, expected, high_water, gap=0, end=0, occupancy=0):
        self.command(STATUS, expected & MASK64, high_water & MASK64, gap, end, occupancy)

    # Expect a recovery request for a message interval and optionally hold it stalled.
    def request(self, first, count, hold=0):
        self.command(REQUEST, make_request(SESSION, first, count), hold)

    # Set the testbench's output ready policy for subsequent cycles.
    def flow(self, mode):
        self.command(FLOW, mode)

    # Compare a selected statistics counter with its expected value.
    def stat(self, index, value):
        self.command(STAT, index, value)

    # Check a statistics counter without requiring the normal output to drain.
    def peek(self, index, value):
        self.command(STAT_PEEK, index, value)

    # Require reorder occupancy to have reached the stated high-water mark.
    def peak_at_least(self, occupancy):
        self.command(OCCUPANCY_PEAK, occupancy)

    # Check that the DUT has emitted exactly the events written to the golden tape so far.
    def count(self):
        self.command(EVENTS, len(self.events))

    # Assert that no recovery request is pending at this checkpoint.
    def no_request(self):
        self.command(NO_REQUEST)

    # Check ingress concurrency and exact edge-to-edge latency for the current run.
    def benchmark(self, terminal_span, end_to_event, input_span, simultaneous):
        self.command(BENCHMARK, terminal_span, end_to_event, input_span, simultaneous)

    # Create reproducible sample messages for the requested ITCH type codes.
    def payloads(self, kinds):
        return [make_itch(kind, self.rng) for kind in kinds]

    # Build one well-formed datagram and validate it against the configured test geometry.
    def data(self, first, payloads, session=SESSION):
        blob = make_packet(session, first, payloads)
        decode_packet(blob, *LIMITS)
        return blob

    # Append decoded events in sequence order to the independent golden tape.
    def record(self, first, payloads):
        for offset, payload in enumerate(payloads):
            self.events.append(decode_itch((first + offset) & MASK64, payload).bits())

    # Write the stimulus program, both lane tapes, and the golden output file for the bench.
    def write(self, folder):
        folder.mkdir(exist_ok=True, parents=True)
        (folder/"program.txt").write_text("\n".join(self.instructions) + "\n")
        for index, lane in enumerate(self.lanes):
            (folder/f"feed{index}.txt").write_text(
                "\n".join(f"{data:016x} {keep:02x} {int(first)} {int(last)}"
                          for data, keep, first, last in lane) + "\n"
            )
        (folder/"golden.txt").write_text("\n".join(f"{data:0147x}" for data in self.events)+"\n")


# Measure simultaneous lane ingress and single-message decode latency without sink stalls.
def latency_case(suite):
    suite.config(50)
    suite.flow(1)
    first, second = suite.payloads("DD")
    suite.burst([suite.data(50, [first])], [suite.data(50, [first])])
    suite.record(50, [first])
    suite.idle(30)
    suite.count()
    suite.status(51, 51)
    # 41-byte datagram, six beats, one completed event. Check a real dual-lane
    # run with simultaneous ingress. A dedicated clock monitor captures it.
    suite.benchmark(0, 15, 6, 6)
    suite.burst([suite.data(51, [second])])
    suite.record(51, [second])
    suite.idle(30)
    suite.count()
    suite.stat(0, 2)
    suite.stat(1, 1)
    suite.stat(11, 2)


# Create a short hole, then check delayed request and recovery when the missing data arrives.
def delayed_gap_case(suite):
    suite.config(100)
    suite.flow(0)
    payloads = suite.payloads("AFECX")
    suite.burst([suite.data(102, payloads[2:4])])
    suite.idle(50)
    suite.status(100, 104, gap=1, occupancy=2)
    conflict = suite.payloads("E")[0]
    suite.burst(lane_b=[suite.data(102, [conflict]), suite.data(104, [payloads[4]])])
    suite.idle(50)
    suite.status(100, 105, gap=1, occupancy=3)
    suite.count()
    suite.stat(0, 3)
    suite.stat(1, 1)
    suite.stat(2, 1)
    suite.burst(lane_b=[suite.data(100, payloads[:2])])
    suite.idle(60)
    suite.record(100, payloads)
    suite.flow(2)
    suite.idle(60)
    suite.count()
    suite.status(105, 105)
    # A ready/valid request retains the original range after B repairs it.
    suite.request(100, 2, hold=6)
    suite.idle(70)
    suite.no_request()
    suite.stat(10, 1)
    suite.stat(11, 5)


# Block downstream progress while both capture paths keep accepting data when possible.
def capture_stall_case(suite):
    baseline = 200
    suite.config(baseline)
    suite.flow(0)
    suite.stat(0, 0)
    payloads = suite.payloads("DDECDDUX")
    suite.burst([suite.data(baseline, payloads[:4]),
                 suite.data(baseline+4, payloads[4:6]),
                 suite.data(baseline+6, payloads[6:])])
    suite.record(baseline, payloads)
    suite.idle(55)
    # The exposed view stays frozen while candidates are added and while
    # the two decoder slots plus serializer are blocked by the sink.
    suite.peek(0, 0)
    suite.stat(0, 8)
    suite.peak_at_least(3)
    suite.flow(1)
    suite.idle(55)
    suite.count()
    suite.status(208, 208)
    suite.stat(11, 8)


# Exercise sequence wrap while keeping duplicate suppression and delivery order intact.
def wrap_case(suite):
    baseline = MASK64 - 2
    suite.config(baseline)
    suite.flow(2)
    payloads = suite.payloads("AFUECXD")
    suite.burst(
        [suite.data(baseline, payloads[:2]), suite.data(baseline+4, payloads[4:])],
        [suite.data(baseline+2, payloads[2:4])],
        source_profile=2,
    )
    suite.record(baseline, payloads)
    suite.idle(150)
    suite.count()
    suite.status(4, 4)
    suite.no_request()
    suite.burst(lane_b=[suite.data(MASK64, [payloads[2]])])
    suite.idle(40)
    suite.count()
    suite.stat(3, 1)


# Verify framing and block-length errors are rejected before any output becomes visible.
def bad_datagram_case(suite):
    seq = 300
    suite.config(seq)
    suite.flow(1)
    first, second = suite.payloads("DX")
    good = suite.data(seq, [first])
    truncated = make_packet(SESSION, seq, [first, second])[:-1]
    fault_sets = [
        [b"s", good[:19], truncated],
        [good+b"?", make_packet(SESSION, seq, [first], declared_count=2)],
        [make_packet(SESSION, seq, [], declared_count=17),
         make_packet(SESSION, seq, [b"R"*65]),
         make_packet(SESSION, seq, [b"R"*245])],
        [make_packet(OTHER, seq, [first]), make_packet(SESSION, seq, [], declared_count=0)+b"x",
         make_packet(SESSION, seq, [], declared_count=65535)+b"x"],
    ]
    for packets in fault_sets:
        suite.burst(packets[::2], packets[1::2], source_profile=1)
        suite.idle(70)
        suite.count()
    suite.fault(good, 0, 2, 0)
    suite.fault(good, 1, 2, 1, lane=1)
    suite.fault(good, 1, 1, 0x7f)
    suite.fault(good, -1, 1, 0x05, lane=1)
    suite.idle(80)
    suite.count()
    suite.status(seq, seq)
    suite.no_request()
    suite.stat(5, 11)
    suite.stat(6, 3)
    suite.stat(7, 1)
    suite.stat(0, 0)

    payloads = [first, b"", b"R"*39, second[:-1]]
    suite.burst(lane_b=[suite.data(seq, payloads)])
    suite.record(seq, payloads)
    suite.idle(100)
    suite.count()
    suite.status(seq+len(payloads), seq+len(payloads))
    suite.stat(11, 1)
    suite.stat(12, 1)
    suite.stat(13, 2)


# Check partial replay, repeated requests, and end-of-session handling across a gap.
def recovery_case(suite):
    seq = 500
    suite.config(seq)
    suite.flow(1)
    item = suite.payloads("D")[0]
    future = suite.data(seq+20, [item])
    heartbeat = make_packet(SESSION, seq+20)
    end = make_packet(SESSION, seq+21, declared_count=65535)
    half_range = suite.data(seq + (1 << 63), [item])
    old_heartbeat = make_packet(SESSION, seq-4)
    suite.burst([heartbeat, future, half_range],
                [suite.data(seq-1, [item]), end, old_heartbeat], source_profile=2)
    suite.idle(90)
    suite.status(seq, seq+21, gap=1, end=1)
    suite.request(seq, 4, hold=10)
    suite.idle(80)
    suite.request(seq, 4)
    suite.stat(10, 2)
    suite.stat(3, 2)
    suite.stat(4, 1)
    suite.stat(8, 2)
    suite.stat(9, 1)

    # Reconfigure with a packet partly captured and another request due.
    partial = words(suite.data(seq, [item]), suite.rng)
    suite.burst_words(partial[:2], ())
    suite.idle(80)
    suite.config(80)
    suite.idle(12)
    suite.status(80, 80)
    suite.no_request()
    suite.stat(10, 0)
    suite.burst([suite.data(80, [item])])
    suite.record(80, [item])
    suite.idle(60)
    suite.count()
    suite.status(81, 81)


# Check the last unambiguous forward sequence and its exclusive recovery watermark.
def recovery_horizon_case(suite):
    seq = MASK64-5
    half = 1 << 63
    suite.config(seq)
    suite.flow(1)
    _, item = suite.payloads("DD")

    # A bare half-range heartbeat is ambiguous and must leave the cursor alone.
    suite.burst([make_packet(SESSION, seq+half)])
    suite.idle(64)
    suite.status(seq, seq)
    suite.no_request()

    # The last unambiguously forward message is outside the window, but its
    # exclusive watermark lies exactly at the half-range boundary.
    suite.burst([suite.data(seq+half-1, [item])])
    suite.idle(80)
    suite.status(seq, seq+half, gap=1)
    suite.request(seq, 4)

    # The same modular watermark remains ahead when the head advances.
    suite.burst([suite.data(seq, [item])])
    suite.record(seq, [item])
    suite.idle(32)
    suite.count()
    suite.status(seq+1, seq+half, gap=1)
    suite.stat(4, 1)


# Generate reproducible duplicate and out-of-order bursts, then compare the merged output.
def randomized_case(suite):
    next_seq = 1000
    suite.config(next_seq)
    suite.flow(2)
    for _ in range(100):
        kinds = suite.rng.choices("AFECXDU", k=6)
        payloads = suite.payloads(kinds)
        sources = [[], []]
        parity = suite.rng.randrange(2)
        split = suite.rng.choice((2, 3, 4))
        sources[parity].append(suite.data(next_seq+split, payloads[split:]))
        sources[1-parity].append(suite.data(next_seq, payloads[:split]))
        if suite.rng.choice((True, False)):
            lo = suite.rng.randrange(0, 5)
            hi = suite.rng.randrange(lo+1, 7)
            lane = suite.rng.randrange(2)
            sources[lane].append(suite.data(next_seq+lo, payloads[lo:hi]))
        suite.burst(*sources, source_profile=1)
        suite.record(next_seq, payloads)
        next_seq += len(payloads)
        suite.idle(100)
        suite.count()
        suite.status(next_seq, next_seq)
    suite.stat(0, 600)
    suite.stat(11, 600)


# Cover maximum packet and block dimensions using the normal receiver parameters.
def default_geometry_case(suite):
    baseline = 4000
    suite.config(baseline)
    suite.flow(2)
    # Exactly 1536 bytes and 64 blocks. The 23-byte strides exercise all
    # byte alignments, including length fields straddling adjacent words.
    payloads = [b"R" * 21 for _ in range(62)] + [b"I" * 43, b"I" * 43]
    packet = make_packet(SESSION, baseline, payloads)
    assert len(packet) == 1536
    decoded = decode_packet(packet)
    assert len(decoded.messages) == 64
    suite.burst([packet], [suite.data(baseline, payloads[:2])], source_profile=1)
    suite.events.extend(decode_itch(seq, body).bits() for seq, body in decoded.numbered_messages())
    suite.idle(850)
    suite.count()
    suite.status(baseline+64, baseline+64)
    suite.stat(0, 64)
    suite.stat(12, 64)

    too_long = packet + b"!"
    too_many = make_packet(SESSION, baseline+64, [b"R"] * 65)
    largest_body = make_packet(SESSION, baseline+64, [b"R" * 64])
    suite.burst([too_long, largest_body], [too_many])
    suite.record(baseline+64, [b"R" * 64])
    suite.idle(200)
    suite.count()
    suite.stat(6, 2)
    suite.status(baseline+65, baseline+65)
    suite.no_request()

    baseline = 6000
    suite.config(baseline)
    suite.flow(1)
    payloads = suite.payloads("ADUECFXDXAUCDAEXF")
    suite.burst([suite.data(baseline+15, payloads[15:])])
    suite.idle(130)
    suite.status(baseline, baseline+17, gap=1, occupancy=1)
    suite.stat(4, 1)
    suite.request(baseline, 15)
    for offset in (0, 5, 10):
        suite.burst(lane_b=[suite.data(baseline+offset, payloads[offset:offset+5])])
        suite.idle(95)
    suite.record(baseline, payloads[:16])
    # The original sequence 6016 was out of range and only this repeat
    # can emit it. Current high water remains 6017 while 6016 is missing.
    suite.status(baseline+16, baseline+17, gap=1)
    suite.burst(lane_b=[suite.data(baseline+16, payloads[16:])])
    suite.record(baseline+16, payloads[16:])
    suite.idle(80)
    suite.count()
    suite.status(baseline+17, baseline+17)
    suite.stat(11, 17)


# Parse the generator's profile and seed, build its scenarios, and persist the bench inputs.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=7201)
    parser.add_argument("--default-geometry", action="store_true")
    args = parser.parse_args()
    suite = Suite(args.seed)
    latency_case(suite)
    if args.default_geometry:
        default_geometry_case(suite)
    else:
        delayed_gap_case(suite)
        capture_stall_case(suite)
        wrap_case(suite)
        bad_datagram_case(suite)
        recovery_case(suite)
        randomized_case(suite)
        recovery_horizon_case(suite)
    suite.write(args.output)
    print(f"Generated {len(suite.events)} expected events and "
          f"{sum(map(len, suite.lanes))} input beats for seed {args.seed}")


if __name__ == "__main__":
    main()
