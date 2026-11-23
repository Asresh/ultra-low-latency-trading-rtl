"""Generate deterministic wire-level simulation programs for the full endpoint."""

import argparse
from collections import Counter
from dataclasses import replace
from pathlib import Path
import random

from fault_matrix import sample as fault_sample

from net_wire import (
    Decoded, Match, Packet, Result, Rule, TxDescriptor, as_ipv4, bit_fields,
    classify, from_beats, parse_frame, to_beats, udp_checksum, UDP_HEAD,
)

WRITE, COMMIT, FLOWS, BURST, WAIT, COUNTS, STAT, CLEAR_STATS, CHECK_PRECISION = range(9)
DST = as_ipv4("239.1.2.3")
SRC = as_ipv4("10.15.4.9")
DST_MAC = 0x01005e010203
SRC_MAC = 0x022022334455

STATS_FROM_RX = {
    Result.BAD_AXIS: 6, Result.MAC_BAD: 7, Result.IP_CHECKSUM: 8,
    Result.UDP_CHECKSUM: 9, Result.RULE_MISS: 10, Result.RULE_DROP: 11,
    Result.FRAME_LIMIT: 15,
}
STATS_FROM_TX = {
    Result.TX_DESCRIPTOR: 21, Result.TX_LENGTH: 22, Result.BAD_AXIS: 23,
}


class Program:
    def __init__(self, seed, *, fixture_geometry=False, allow_zero=True):
        """Initialize the Python reference policy and buffers for a host and RTL scenario."""
        self.rng = random.Random(seed)
        self.commands = []
        self.source_rx = []
        self.source_tx = [[], []]
        self.golden_info = []
        self.golden_payload = []
        self.golden_tx = [[], []]
        self.golden_status = [[], []]
        self.stats = Counter()
        self.active = 0
        self.epoch = 0
        self.rules = [[Rule(enabled=False)] * 8 for _ in range(2)]
        self.max_frame = 256 if fixture_geometry else 1536
        self.max_payload = 192 if fixture_geometry else 1472
        self.allow_zero = allow_zero
        self.cookie = 1234

    def step(self, op, *fields):
        """Append a fixed-format simulator action with its padded payload fields."""
        fields += (0,) * (4-len(fields))
        assert len(fields) == 4
        self.commands.append(f"{op:d} " + " ".join(f"{x:080x}" for x in fields))

    def write_rule(self, index, rule):
        """Write one packed rule into the inactive rule bank in model and stimulus."""
        self.rules[1-self.active][index] = rule
        self.step(WRITE, index, rule.encode())

    def commit(self):
        """Swap the staged policy in both the model and the simulated device."""
        self.active = 1-self.active
        self.epoch = (self.epoch + 1) & 255
        self.step(COMMIT)

    def replace_rules(self, rules):
        """Replace the entire policy before issuing its single commit."""
        for index in range(8):
            self.write_rule(index, rules[index] if index < len(rules) else Rule(enabled=False))
        self.commit()

    def flows(self, info=1, body=1, tx=1, status=1):
        """Choose stall modes for the virtual data consumers."""
        self.step(FLOWS, info, body, tx, status)

    def wait(self, clocks):
        """Add idle cycles to settle or drain the declared interfaces."""
        self.step(WAIT, clocks)

    def counters(self):
        """Return the reference monitor used to check the requested statistics."""
        self.step(COUNTS, len(self.golden_info), len(self.golden_payload),
                  sum(map(len, self.golden_tx)), sum(map(len, self.golden_status)))

    def snapshot(self, index, expected=None):
        """Capture a consistent counter bank view and append its hardware action."""
        self.step(STAT, index, self.stats[index] if expected is None else expected)

    def clear(self):
        """Clear the selected counters without changing the packet policy."""
        self.step(CLEAR_STATS)
        self.stats.clear()

    def check_precision(self, rx_first_to_info, tx_desc_to_mac, max_tx_beat_gap, parallel):
        """Record expected response-cycle bounds for the bench to check."""
        self.step(CHECK_PRECISION, rx_first_to_info, tx_desc_to_mac, max_tx_beat_gap, parallel)

    def rx_record(self, beats, *, expected_result=None):
        """Record the expected disposition and payload for one received packet."""
        frame, axis = from_beats(beats)
        if axis == Result.OK:
            result = classify(parse_frame(frame, max_frame=self.max_frame, allow_zero=self.allow_zero),
                              self.rules[self.active], self.epoch)
        else:
            result = Decoded(axis, min(len(frame), self.max_frame), epoch=self.epoch)
        if expected_result is not None:
            assert result.result == expected_result, (axis, result.result, expected_result)
        self.golden_info.append(result.rx_info())
        self.stats[0] += 1
        self.stats[1] += sum(len(beat.valid_bytes()) for beat in beats)
        self.stats[2 if result.result == Result.OK else 3] += 1
        if result.result in STATS_FROM_RX:
            self.stats[STATS_FROM_RX[result.result]] += 1
        if result.packet:
            tag_count = len(result.packet.tags)
            if tag_count:
                self.stats[24+tag_count] += 1
            if result.result == Result.OK:
                if result.packet.udp_zero:
                    self.stats[12] += 1
                if result.packet.payload:
                    self.golden_payload.extend(to_beats(result.packet.payload))
                self.stats[5] += len(result.packet.payload)
        return result

    def tx_record(self, lane, case):
        """Encode a descriptor and its expected Ethernet result as vector records."""
        descriptor, payload_beats = case
        result, expected = descriptor.expected(payload_beats, max_payload=self.max_payload)
        self.source_tx[lane].append(case)
        self.golden_status[lane].append(bit_fields(
            (result,8),(lane,8),(descriptor.cookie,32),(len(expected),16),
        ))
        if result == Result.OK:
            self.golden_tx[lane].extend(to_beats(expected))
            self.stats[16+lane] += 1
            self.stats[18] += len(expected)
        else:
            self.stats[20] += 1
            self.stats[STATS_FROM_TX[result]] += 1

    def burst(self, rx=(), tx_a=(), tx_b=(), perturb=0):
        """Schedule a complete group of receive and transmit traffic for the simulator."""
        rx_beats = []
        for frame in rx:
            rx_beats.extend(frame)
        self.source_rx.extend(rx_beats)
        for lane, cases in enumerate((tx_a, tx_b)):
            for case in cases:
                self.tx_record(lane, case)
        self.step(BURST, len(rx_beats), len(tx_a), len(tx_b), perturb)

    def rx_burst(self, *frames, perturb=0):
        """Schedule a receive-only burst using the supplied packet records."""
        converted = []
        for item in frames:
            if isinstance(item, bytes):
                beats = to_beats(item, rng=self.rng)
            else:
                beats = item
            self.rx_record(beats)
            converted.append(beats)
        self.burst(converted, perturb=perturb)

    def desc(self, packet, **arguments):
        """Create a transmit descriptor for a payload and the selected header parameters."""
        self.cookie += 1
        return TxDescriptor(packet, self.cookie, **arguments)

    def tx_case(self, packet, *, beats=None, **arguments):
        """Build one transmit case and register its expected acceptance or failure."""
        descriptor = self.desc(packet, **arguments)
        if beats is None:
            beats = to_beats(packet.payload, rng=self.rng) if packet.payload else []
        return descriptor, beats

    def write(self, directory):
        """Export scenario actions and all receive and transmit scoreboards."""
        directory.mkdir(exist_ok=True, parents=True)
        (directory/"commands.txt").write_text("\n".join(self.commands)+"\n")
        (directory/"rx_input.txt").write_text("\n".join(x.row() for x in self.source_rx)+"\n")
        (directory/"rx_info.txt").write_text("\n".join(f"{data:096x} {mask:096x}"
                                               for data, mask in self.golden_info)+"\n")
        (directory/"rx_data.txt").write_text("\n".join(x.row() for x in self.golden_payload)+"\n")
        for lane in (0, 1):
            encoded = []
            for descriptor, beats in self.source_tx[lane]:
                encoded.append(f"{descriptor.encode():080x} {len(beats):d}")
                encoded.extend(beat.row() for beat in beats)
            (directory/f"tx{lane}_input.txt").write_text("\n".join(encoded)+"\n")
            (directory/f"tx{lane}_wire.txt").write_text(
                "\n".join(beat.row() for beat in self.golden_tx[lane])+"\n"
            )
            (directory/f"tx{lane}_status.txt").write_text(
                "\n".join(f"{data:016x}" for data in self.golden_status[lane])+"\n"
            )


def standard(payload=b"", **kwargs):
    """Build the default directed scenario using the chosen random seed."""
    return replace(Packet(DST_MAC, SRC_MAC, SRC, DST, 12000, 18000, payload), **kwargs)


def mutate(data, offset, value):
    """Produce a modified packet or beat sequence for a specific framing check."""
    return data[:offset] + bytes((value,)) + data[offset+1:]


def precision_baseline(program):
    """Check exact first-response latency under an unstalled schedule."""
    program.flows()
    program.write_rule(3, Rule(queue=2))
    program.commit()
    packet = standard()
    frame = to_beats(packet.serialize(), rng=program.rng)
    program.rx_record(frame)
    program.burst([frame], [program.tx_case(packet)])
    program.wait(100)
    program.counters()
    # Bench verifies deterministic stalls-free timing with a minimum frame.
    program.check_precision(26, 2, 1, 6)


def classify_during_stall(program):
    """Change rules while a completed receive result waits behind backpressure."""
    program.replace_rules([
        Rule(dst_port=Match(18000, 0xffff), queue=3),
        Rule(dst_ip=Match(DST, 0xffffffff), src_ip=Match(SRC, 0xffffff00),
             src_port=Match(12000, 0xfff0), drop=True),
        Rule(tags=Match(1, 3), tci=Match(9, 0xfff), queue=7),
    ])
    program.flows(info=0)
    program.rx_burst(standard(b"preserve committed metadata").serialize())
    program.wait(100)
    program.replace_rules([
        Rule(dst_port=Match(18000, 0xffff), queue=5),
        Rule(dst_ip=Match(DST, 0xffffffff), src_ip=Match(SRC, 0xffffff00),
             src_port=Match(12000, 0xfff0), drop=True),
        Rule(tags=Match(1, 3), tci=Match(9, 0xfff), queue=7),
    ])
    program.flows(info=2, body=2, tx=2, status=2)
    program.rx_burst(
        standard(b"next generation", tags=((0x8100, 0xa123),)).serialize(),
        standard(b"blocked", dst_port=1000).serialize(),
        standard(b"no route", dst_port=1000, src_port=100).serialize(),
        standard(b"vlan match", dst_port=1000, src_port=100, tags=((0x8100, 0xe009),)).serialize(),
        perturb=1,
    )
    program.wait(450)
    program.counters()


def framing_errors(program):
    """Exercise malformed keep masks and packet boundary sidebands."""
    packet = standard(b"abcdefghi")
    frame = packet.serialize(include_pad=False)
    start = 14
    udp = start+20
    cases = [
        (frame, Result.OK),
        (frame[:13], Result.L2_SHORT),
        (frame[:20], Result.IP_LENGTH),
        (frame[:-1], Result.IP_LENGTH),
        (frame[:12]+b"\x08\x06"+frame[14:], Result.L2_TYPE),
        (frame[:12]+b"\x81\x00\x00", Result.L2_SHORT),
        (mutate(frame, start, 0x65), Result.IP_SHAPE),
        (mutate(frame, start, 0x44), Result.IP_SHAPE),
        (mutate(frame, start+10, frame[start+10]^1), Result.IP_CHECKSUM),
        (mutate(frame, udp+5, 8), Result.UDP_LENGTH),
        (mutate(frame, udp+8, frame[udp+8]^1), Result.UDP_CHECKSUM),
        (replace(packet, frag_bits=0x2000).serialize(), Result.IP_FRAGMENT),
        (replace(packet, frag_bits=1).serialize(), Result.IP_FRAGMENT),
        (replace(packet, frag_bits=0x8000).serialize(), Result.IP_FRAGMENT),
        (replace(packet, ttl=0).serialize(), Result.IP_SHAPE),
        (replace(packet, protocol=6).serialize(), Result.IP_PROTOCOL),
        (replace(packet, tags=((0x88a8,1),(0x8100,2),(0x8100,3))).serialize(), Result.VLAN_LIMIT),
        (replace(packet, udp_zero=True).serialize(), Result.OK if program.allow_zero else Result.UDP_ZERO),
        (replace(packet, options=b"\x01"*40).serialize(), Result.OK),
        (replace(packet, options=b"\x01"*36, tags=((0x88a8,1),(0x8100,2))).serialize(), Result.OK),
        (frame.ljust(61,b"\x12"), Result.IP_LENGTH),
        (b"x"*(program.max_frame+7), Result.FRAME_LIMIT),
        (b"x"*(program.max_frame+25), Result.FRAME_LIMIT),
    ]
    raw = []
    for value, expected in cases:
        beats = to_beats(value, rng=program.rng)
        program.rx_record(beats, expected_result=expected)
        raw.append(beats)
    for index, replacement, expected in (
        (0, {"first":False}, Result.BAD_AXIS),
        (1, {"first":True}, Result.BAD_AXIS),
        (1, {"keep":0x7f}, Result.BAD_AXIS),
        (-1, {"keep":0x15}, Result.BAD_AXIS),
        (-1, {"bad":True}, Result.MAC_BAD),
        (0, {"bad":True}, Result.MAC_BAD),
    ):
        beats = to_beats(frame, rng=program.rng)
        beats[index] = replace(beats[index], **replacement)
        program.rx_record(beats, expected_result=expected)
        raw.append(beats)
    program.burst(raw, perturb=1)
    program.wait(350)
    program.counters()


def tx_errors_and_boundaries(program):
    """Cover transmit validation and edge cases around its declared length."""
    packet = standard(bytes(range(17)))
    bad_shape = to_beats(packet.payload)
    bad_shape[1] = replace(bad_shape[1], first=True)
    bad_keep = to_beats(packet.payload)
    bad_keep[-1] = replace(bad_keep[-1], keep=0)
    maximum = standard(bytes((i*31) & 255 for i in range(program.max_payload)))
    inputs = [
        program.tx_case(packet),
        program.tx_case(replace(packet, ttl=0)),
        program.tx_case(packet, tags_override=3),
        program.tx_case(packet, reserved=3),
        program.tx_case(packet, payload_size=25),
        program.tx_case(packet, payload_size=8),
        program.tx_case(packet, beats=bad_shape),
        program.tx_case(packet, beats=bad_keep),
        program.tx_case(standard(b"x"), payload_size=program.max_payload+1),
        program.tx_case(maximum),
        program.tx_case(replace(packet, tags=((0x8100,7),), udp_zero=True)),
        program.tx_case(replace(packet, tags=((0x88a8,8),(0x8100,9)), df=False)),
        program.tx_case(standard(tags=((0x88a8,8),(0x8100,9)))),
    ]
    blank = standard(b"\x00\x00")
    header = UDP_HEAD.pack(blank.src_port, blank.dst_port, 10, 0)
    correction = udp_checksum(blank.src_ip, blank.dst_ip, header+blank.payload)
    zero = replace(blank, payload=correction.to_bytes(2, "big"))
    assert zero.segment()[6:8] == b"\xff\xff"
    inputs.append(program.tx_case(zero))
    program.burst(tx_a=inputs[::2], tx_b=inputs[1::2], perturb=1)
    program.wait(300)
    program.counters()


def random_packet(program, *, for_receive):
    """Choose reproducible layer-two, IPv4, and UDP fields."""
    r = program.rng
    max_length = min(160, program.max_payload)
    payload = bytes(r.randrange(256) for _ in range(r.randrange(max_length+1)))
    depth = r.randrange(3)
    tags = tuple((0x88a8 if depth == 2 and i == 0 else 0x8100, r.randrange(65536))
                 for i in range(depth))
    extras = r.randrange(11)*4 if for_receive else 0
    options = b"\x01"*extras
    return standard(payload, tags=tags, ident=r.randrange(65536), ttl=r.randrange(1,256),
                    tos=r.randrange(256), df=bool(r.randrange(2)),
                    udp_zero=r.randrange(5)==0, options=options)


def random_bursts(program, count):
    """Mix varied packet types, queue selection, and stall schedules."""
    program.replace_rules([Rule(queue=6)])
    for _ in range(count):
        packets = [random_packet(program, for_receive=True) for _ in range(4)]
        frames = []
        for packet in packets:
            frame = packet.serialize(include_pad=bool(program.rng.randrange(2)), fill=0x99)
            if program.rng.randrange(6) == 0:
                frame = mutate(frame, len(packet.l2())+11, frame[len(packet.l2())+11]^0x80)
            frames.append(to_beats(frame, rng=program.rng, bad=program.rng.randrange(20)==0))
        for beats in frames:
            program.rx_record(beats)
        tx_a = [program.tx_case(random_packet(program, for_receive=False)) for _ in range(2)]
        tx_b = [program.tx_case(random_packet(program, for_receive=False)) for _ in range(2)]
        program.burst(frames, tx_a, tx_b, perturb=1)
    program.wait(700)
    program.counters()


def default_exact_bounds(program):
    """Check lengths immediately below, at, and above default buffer limits."""
    program.replace_rules([Rule(queue=7)])
    program.flows(info=2, body=2, tx=2, status=2)

    # The first capture occupies the receive bank much longer than the
    # following minimum frame. The RX scoreboard still expects FIFO order.
    options = b"\x01"*40
    tags = ((0x88a8,0xb02a),(0x8100,0x102f))
    overhead = 22+20+len(options)+8
    size = program.max_frame-overhead
    largest = standard(bytes((i*13)&255 for i in range(size)),
                       options=options,tags=tags,ident=0xfffe)
    first = largest.serialize()
    assert len(first) == program.max_frame
    after = standard(b"D",udp_zero=True,ident=0xffff).serialize()
    pair = [to_beats(first,rng=program.rng),to_beats(after,rng=program.rng)]
    program.rx_record(pair[0],expected_result=Result.OK)
    program.rx_record(pair[1],expected_result=Result.OK)
    program.burst(pair,perturb=1)
    program.wait(650)
    program.counters()

    # Every final MAC keep width is checked deliberately for each TX header
    # width. The body can start in the same beat as the last header lane.
    for depth in range(3):
        stacked=tags[:depth]
        packets=[]
        for remainder in range(1,9):
            header_length=42+4*depth
            n=48+((remainder-header_length) % 8)
            payload=bytes((i+depth*31+remainder*17)&255 for i in range(n))
            packets.append(standard(payload,tags=stacked,ident=depth*256+remainder))
        for packet in packets:
            pad=packet.serialize()
            program.rx_record(to_beats(pad,rng=program.rng))
        program.burst([to_beats(packet.serialize(),rng=program.rng) for packet in packets],
                      [program.tx_case(packet) for packet in packets[::2]],
                      [program.tx_case(packet) for packet in packets[1::2]],perturb=1)
    program.wait(400)
    program.counters()


def injected_cross_layer_faults(program, count):
    """Inject modeled faults across Ethernet, IPv4, and UDP parsing stages."""
    cases = list(fault_sample(program.rng,
                             lambda: random_packet(program, for_receive=True),
                             count, program.max_frame))
    for start in range(0,len(cases),6):
        frames = []
        for case in cases[start:start+6]:
            program.rx_record(case.input_beats,expected_result=case.expected)
            frames.append(case.input_beats)
        program.burst(frames,perturb=1)
    program.wait(450)
    program.counters()


def check_statistics(program):
    """Compare model statistics with the hardware expectation tape."""
    for index in (0,1,2,3,5,6,7,8,9,10,11,12,15,16,17,18,20,21,22,23):
        program.snapshot(index)
    program.clear()
    for index in (0,1,2,5,16,17,18,20):
        program.snapshot(index, 0)


def main():
    """Parse geometry and seed and write a reproducible frontend scenario."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--directory", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=4101)
    ap.add_argument("--bursts", type=int, default=72)
    ap.add_argument("--fault-samples", type=int, default=0)
    ap.add_argument("--strict-udp", action="store_true")
    ap.add_argument("--default-geometry", action="store_true")
    args = ap.parse_args()
    program = Program(args.seed, fixture_geometry=not args.default_geometry,
                      allow_zero=not args.strict_udp)
    precision_baseline(program)
    classify_during_stall(program)
    framing_errors(program)
    tx_errors_and_boundaries(program)
    random_bursts(program, args.bursts)
    if args.default_geometry:
        default_exact_bounds(program)
    if args.fault_samples:
        injected_cross_layer_faults(program, args.fault_samples)
    check_statistics(program)
    program.write(args.directory)
    print(f"Generated {len(program.golden_info)} RX frames, "
          f"{sum(map(len,program.golden_status))} TX commands")


if __name__ == "__main__":
    main()
