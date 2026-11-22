"""Generate ITCH messages and independent expected event records for simulation."""

import argparse
from pathlib import Path
import random
import struct

MASK64 = (1 << 64) - 1
FIELD_BITS = (2, 2, 64, 64, 8, 16, 16, 48, 64, 64, 8, 32, 64, 32, 64, 8, 32)
KINDS = "AFECXDU"


def pack_record(result, disposition, sequence, expected, kind, fields):
    packed = 0
    for value, width in zip(
        (result, disposition, sequence, expected, kind, *fields), FIELD_BITS
    ):
        assert 0 <= value < (1 << width)
        packed = (packed << width) | value
    return f"{packed:0147x}"


class Vectors:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.beats = []
        self.records = []
        self.expected = None

    def op(self, code, value=0):
        self.beats.append(f"{code} {value:016x} 00 0 0 0000000000000000")

    def seed(self, sequence):
        self.op(1, sequence)
        self.expected = sequence

    def reset(self):
        self.op(3)
        self.expected = None

    def message(self, kind):
        r = self.rng.getrandbits
        locate, tracking, timestamp, order = r(16), r(16), r(48), r(64)
        header = struct.pack(">cHH", kind.encode("ascii"), locate, tracking)
        header += timestamp.to_bytes(6, "big") + struct.pack(">Q", order)
        new_order = side = shares = stock = price = match = printable = mpid = 0

        if kind in "AF":
            side = ord(self.rng.choice("BS"))
            shares, price = r(32), r(32)
            symbol = self.rng.choice([b"ABCD    ", b"LONGNAME", b"NVDA    "])
            stock = int.from_bytes(symbol, "big")
            tail = struct.pack(">BI8sI", side, shares, symbol, price)
            if kind == "F":
                attribution = self.rng.choice([b"ABCD", b"WXYZ"])
                mpid = int.from_bytes(attribution, "big")
                tail += attribution
        elif kind in "EC":
            shares, match = r(32), r(64)
            tail = struct.pack(">IQ", shares, match)
            if kind == "C":
                printable, price = ord(self.rng.choice("YN")), r(32)
                tail += struct.pack(">BI", printable, price)
        elif kind == "X":
            shares = r(32)
            tail = struct.pack(">I", shares)
        elif kind == "U":
            new_order, shares, price = r(64), r(32), r(32)
            tail = struct.pack(">QII", new_order, shares, price)
        else:
            assert kind == "D"
            tail = b""

        return header + tail, (
            locate, tracking, timestamp, order, new_order, side, shares, stock,
            price, match, printable, mpid,
        )

    def beat(self, chunk, keep, first, last, seq):
        # Pad invalid lanes with random bytes to catch accidental use of them.
        padded = chunk + bytes(self.rng.randrange(256) for _ in range(8 - len(chunk)))
        word = int.from_bytes(padded, "little")
        self.beats.append(f"0 {word:016x} {keep:02x} {first} {last} {seq:016x}")

    def frame(self, sequence, kind, fault=None):
        if kind in KINDS:
            payload, fields = self.message(kind)
            result = 0
        else:
            payload, fields = kind.encode("ascii") + b"\x01" * (65 if kind == "R" else 0), (0,) * 12
            result = 1
        if fault == "truncated":
            payload = payload[:-2]
        elif fault == "overlong":
            payload += b"\xa5" * 36
        elif fault == "bad_char":
            offset = 31 if kind == "C" else 19
            payload = payload[:offset] + b"?" + payload[offset + 1:]
        if fault:
            result, fields = 2, (0,) * 12

        for base in range(0, len(payload), 8):
            chunk = payload[base:base + 8]
            tail = int(base + len(chunk) == len(payload))
            keep = (1 << len(chunk)) - 1
            if fault == "bad_keep" and tail:
                keep = 0x05
            if fault == "short_middle" and base == 8:
                keep = 0x7f
            first = int(base == 0)
            if fault == "mid_first" and base == 8:
                first = 1
            if fault == "orphan":
                first = 0
            # Only the accepted first beat supplies the real sequence number.
            self.beat(chunk, keep, first, tail, sequence if base == 0 else self.rng.getrandbits(64))

        if fault == "orphan":
            expected, disposition, sequence, type_byte = 0, 1, 0, 0
        else:
            type_byte = ord(kind)
            expected = self.expected or 0
            if self.expected is None:
                disposition = 1
            else:
                # Signed serial distance using Python's unbounded arithmetic.
                distance = (sequence - self.expected + (1 << 63)) % (1 << 64) - (1 << 63)
                disposition = 0 if not distance else (2 if distance > 0 else 3)
            if disposition != 3:
                self.expected = (sequence + 1) & MASK64
        self.records.append(pack_record(result, disposition, sequence, expected, type_byte, fields))


def generate(seed):
    v = Vectors(seed)
    # Unstalled block for direct line-rate and latency measurements.
    for i, kind in enumerate(KINDS * 3):
        v.frame(i, kind)
    v.op(2)
    v.frame(21, "R")
    v.frame(22, "?")
    v.frame(25, "C")
    v.frame(25, "A")
    v.frame(23, "A")
    v.frame(26 + (1 << 63), "D")  # ambiguous half-range distance
    v.frame(26, "E")

    for i, (kind, fault) in enumerate([
        ("A", "truncated"), ("C", "overlong"), ("F", "short_middle"),
        ("D", "bad_keep"), ("X", "mid_first"), ("U", "orphan"),
        ("A", "bad_char"), ("C", "bad_char"), ("?", "bad_keep"),
    ]):
        v.frame(27 + i, kind, fault)
    v.frame(36, "U")

    v.seed(MASK64 - 1)
    for sequence, kind in [(MASK64 - 1, "A"), (MASK64, "X"), (0, "D"), (MASK64, "E"), (1, "C")]:
        v.frame(sequence, kind)

    # A synchronous reset aborts a partially assembled message. The prior
    # queued output is drained by the testbench before applying this reset.
    payload, _ = v.message("A")
    v.beat(payload[:8], 0xff, 1, 0, 2)
    v.reset()
    v.frame(500, "F")

    faults = [None] * 15 + ["truncated", "overlong", "bad_keep", "short_middle",
                            "mid_first", "orphan"]
    for i in range(1500):
        sequence = (v.expected + v.rng.choice([-9, -1, 0, 0, 0, 0, 2, 8])) & MASK64
        if i % 53 == 0:
            v.frame(sequence, v.rng.choice("R?"))
        else:
            v.frame(sequence, v.rng.choice(KINDS), v.rng.choice(faults))
    return v


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=int, default=7201)
    p.add_argument("--directory", type=Path, required=True)
    args = p.parse_args()
    vector = generate(args.seed)
    args.directory.mkdir(parents=True, exist_ok=True)
    (args.directory / "stimulus.txt").write_text("\n".join(vector.beats) + "\n")
    (args.directory / "expected.txt").write_text("\n".join(vector.records) + "\n")
    print(f"Generated {len(vector.records)} expected completions (seed {args.seed})")


if __name__ == "__main__":
    main()
