"""Audit CQ records and the absence of stream data in the RTL readback run."""

import argparse
from pathlib import Path
from zlib import crc32

from audit_directed_capture import completion_hex, stimulus_bytes
from dma_audit import assemble_stream, compare, completion_rows


def expected_capture():
    """Describe the hardware fixture independently as little-endian result bytes."""
    cross_page = stimulus_bytes(774, 129)
    corruption = bytearray(bytes.fromhex("ddeeefbeadde55aa"))
    corruption[0] ^= 0x80
    rows = [
        (0, 0, 2, 0x100, 129, 0x51, crc32(cross_page)),
        (0, 0, 4, 0x101, 129, 0x52, crc32(cross_page)),
        (0, 6, 4, 0x102, 0, 0x53, crc32(cross_page)),
        (0, 9, 2, 0x103, 0, 0x54, crc32(corruption)),
        (0, 6, 4, 0x104, 0, 0x55, crc32(corruption)),
        (0, 4, 2, 0x105, 0, 0x56),
        (0, 4, 4, 0x106, 0, 0x57),
        (0, 5, 2, 0x107, 0, 0x58, crc32(stimulus_bytes(74, 13))),
        (0, 6, 2, 0x108, 0, 0x59, crc32(stimulus_bytes(353, 7))),
        (0, 2, 4, 0x109, 0, 0x5A),
        (0, 3, 4, 0x10A, 0, 0x5B),
    ]
    for variant in range(1, 10):
        count = 512 if variant == 9 else variant
        address = 0x6000 + variant * 576
        digest = crc32(stimulus_bytes(5000 + 11 * variant, count))
        rows.append((1, 0, 2, 0x200 + variant, count, address, digest))
        rows.append((1, 0, 4, 0x300 + variant, count, address, digest))
    return {
        "format": "host_dma_audit_v1",
        "expected_completions": [completion_hex(*row) for row in rows],
        "expected_packets": [],
    }


def verify(directory):
    """Compare both hardware capture files with fixed-order binary expectations."""
    with (directory / "completions.hex").open() as completions, (directory / "packets.txt").open() as packets:
        result = compare(
            expected_capture(), completion_rows(completions), assemble_stream(packets),
            strict_schedule=True, check_clock=True,
        )
    print(
        f"PASS external integrity audit: {result['completion_count']} CQ records, "
        f"{result['error_completions']} errors, {result['packet_count']} stream packets"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    verify(parser.parse_args().directory)
