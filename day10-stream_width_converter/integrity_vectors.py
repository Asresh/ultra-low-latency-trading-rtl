"""Write exact packet, byte-lane and CRC expectations for the RTL scoreboard."""
import argparse
import random
import zlib
from pathlib import Path


def lane_word(data):
    return int.from_bytes(data, "little")


def write_rows(path, rows):
    with path.open("w") as handle:
        for row in rows:
            handle.write(" ".join(f"{item:x}" for item in row) + "\n")


def build(destination, in_bytes, out_bytes, capacity, seed, count):
    if not 1 <= in_bytes <= 16 or not 1 <= out_bytes <= 16:
        raise ValueError("lane counts must be between 1 and 16")
    if not 1 <= capacity <= 65535 or count < 0:
        raise ValueError("capacity must fit the nonzero 16-bit length and frames cannot be negative")
    rng = random.Random(seed)
    incoming, outgoing, audits = [], [], []

    def frame(data, tag, fault=None):
        if not data:
            raise ValueError("a frame must contain at least one input beat")
        length = (len(data) + (fault == "length")) & 0xffff
        checksum = zlib.crc32(data) ^ (fault == "crc")
        received = bytearray()
        state = 0
        for offset in range(0, len(data), in_bytes):
            chunk = data[offset:offset + in_bytes]
            final = offset + in_bytes >= len(data)
            keep = (1 << len(chunk)) - 1
            if fault == "keep" and final:
                # Include nonprefix masks in wider geometries.
                keep = (1 << (in_bytes - 1)) if len(chunk) == in_bytes and in_bytes > 1 else 0
            actual_tag = tag ^ int(fault == "tag" and offset != 0)
            padded = chunk.ljust(in_bytes, b"\xff")
            received.extend(byte for lane, byte in enumerate(padded) if keep >> lane & 1)
            if not state:
                if not keep or keep & (keep + 1) or (not final and keep != (1 << in_bytes) - 1):
                    state = 1
                elif actual_tag != tag:
                    state = 2
                elif len(received) > capacity:
                    state = 3
            # Later metadata is deliberately poisoned. Only the first beat
            # starts a new integrity context.
            incoming.append((lane_word(padded), keep, final, actual_tag,
                             length if not offset else 0, checksum if not offset else 0))
        actual_crc = zlib.crc32(received)
        if not state:
            if length != len(received):
                state = 4
            elif checksum != actual_crc:
                state = 5
        audits.append((tag, min(len(received), 65535), length, actual_crc, checksum, state))
        if not state:
            for offset in range(0, len(received), out_bytes):
                chunk = received[offset:offset + out_bytes]
                outgoing.append((lane_word(chunk), (1 << len(chunk)) - 1,
                                 offset + out_bytes >= len(received), tag))

    first = b"123456789"[:capacity]
    frame(first, 0x19, "crc")
    frame(first, 0x19)
    frame(b"\x84", 0x19, "length")
    frame(b"\xff", 0x19, "keep")
    frame(b"\x70", 0x19)

    for index in range(count):
        fault = (None, None, None, "crc", "length", "keep", "tag", "capacity")[index % 8]
        low = in_bytes + 1 if fault == "tag" else 1
        if low > capacity:
            fault = "capacity"
        length = capacity + rng.randrange(1, 2*in_bytes + 1) if fault == "capacity" else rng.randint(low, capacity)
        data = bytes(rng.randrange(256) for _ in range(length))
        frame(data, rng.randrange(16), fault)

    destination.mkdir(parents=True, exist_ok=True)
    write_rows(destination / "input.txt", incoming)
    write_rows(destination / "packets.txt", outgoing)
    write_rows(destination / "audit.txt", audits)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--in-bytes", type=int, required=True)
    parser.add_argument("--out-bytes", type=int, required=True)
    parser.add_argument("--capacity", type=int, required=True)
    parser.add_argument("--seed", type=int, default=90211)
    parser.add_argument("--frames", type=int, default=307)
    options = parser.parse_args()
    build(options.directory, options.in_bytes, options.out_bytes,
          options.capacity, options.seed, options.frames)


if __name__ == "__main__":
    main()
