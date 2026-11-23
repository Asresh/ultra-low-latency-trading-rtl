"""Audit the real RTL integration capture against a byte-oriented Python fixture."""

import argparse
from pathlib import Path
from struct import pack
from zlib import crc32

from dma_audit import assemble_stream, compare, completion_rows


def stimulus_bytes(seed, length):
    """Build the independently generated seeded byte sequence read by the DMA."""
    mask = (1 << 32) - 1
    output = bytearray()
    for index in range(length):
        value = (index * 0x70FCAB93 + seed) & mask
        folded = (value ^ (value >> 17)) + ((value >> 7) ^ 0xBE6701AE)
        output.append(folded & 0xFF)
    return bytes(output)


def completion_hex(queue, status, opcode, tag, length, cookie, crc=0):
    # Encode from byte offsets independently of the host ABI classes.
    """Pack the expected CQ record into little-endian hex for comparison."""
    return pack("<IHBBQ3xBIQ", length, tag, opcode, status, cookie, queue, crc, 0).hex()


def directed_fixture():
    """Return the acceptance and payload expectations for the directed run."""
    transmitted = stimulus_bytes(1331, 139)
    received = stimulus_bytes(722, 129)
    bad_crc = stimulus_bytes(14, 29)
    write_error = stimulus_bytes(411, 154)
    entries = (
        (0, 0, 1, 0x015F, 139, 0x55557777, crc32(transmitted)),
        (1, 0, 2, 0x018D, 129, 0x489DA, crc32(received)),
        (0, 6, 1, 0xD000, 0, 17, crc32(transmitted)),
        (1, 6, 2, 0xD001, 0, 18, crc32(bad_crc)),
        (0, 3, 1, 0xD002, 0, 19),
        (1, 2, 1, 0xD003, 0, 20),
        (1, 1, 49, 0xD004, 0, 21),
        (0, 2, 3, 0xD005, 0, 22),
        (1, 7, 2, 0xD006, 0, 23),
        (1, 8, 2, 0xD007, 0, 24),
        (0, 4, 1, 0xD008, 0, 25),
        (0, 4, 0, 0, 0, 0),
        (1, 5, 2, 0xD00A, 0, 27, crc32(write_error)),
        (0, 0, 3, 0xD00B, 0, 28),
    )
    return {
        "format": "host_dma_audit_v1",
        "expected_completions": [completion_hex(*row) for row in entries],
        "expected_packets": [
            {
                "queue": 0,
                "tag": 0x015F,
                "cookie": 0x55557777,
                "payload": transmitted.hex(),
            },
        ],
    }


def verify(directory):
    """Check captured data against the independent reference for this scenario."""
    ledger = directed_fixture()
    with (directory / "completions.hex").open() as completions:
        with (directory / "packets.txt").open() as packets:
            result = compare(
                ledger, completion_rows(completions), assemble_stream(packets),
                strict_schedule=True, check_clock=True,
            )
    print(
        f"PASS external DMA capture audit: {result['completion_count']} exact records, "
        f"{result['packet_count']} packet, {result['copied_payload_bytes']} bytes"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    verify(parser.parse_args().directory)
