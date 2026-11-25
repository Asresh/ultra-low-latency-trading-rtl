"""Reconcile captured segmented DMA bytes with independent host expectations."""

import argparse
from pathlib import Path
from zlib import crc32

from audit_directed_capture import completion_hex, stimulus_bytes
from dma_audit import assemble_stream, compare, completion_rows


def fixture():
    """Build expected stream and completion records from independently packed bytes."""
    gathered = stimulus_bytes(88,163)
    tail = stimulus_bytes(141,13)
    scatter = stimulus_bytes(117,153)
    rows = [
        (0,0,5,0x500,163,0x1101,crc32(gathered)),
        (1,0,6,0x501,153,0x1202,crc32(scatter)),
        (0,4,5,0x502,0,0x1103),
        (0,6,5,0x503,0,0x1104,crc32(gathered)),
        (0,0,7,0x504,163,0x1105,crc32(gathered)),
    ]
    rows.extend((0,10,6,0x510+i,0,0x1300+i) for i in range(7))
    rows.extend((
        (0,10,5,0x517,0,0x1307),
        (0,2,5,0x520,0,0x1310),
        (0,2,6,0x521,0,0x1311),
        (0,3,5,0x522,0,0x1312),
        (0,4,6,0x523,0,0x1313),
        (1,5,6,0x530,0,0x1400,crc32(stimulus_bytes(121,25))),
        (1,9,6,0x531,0,0x1401,crc32(b"ABCDEFGHxJKLMNOP")),
        (0,0,5,0x540,13,0x1500,crc32(tail)),
    ))
    return {
        "format": "host_dma_audit_v1",
        "expected_completions": [completion_hex(*row) for row in rows],
        "expected_packets": [
            {"queue":0,"tag":0x500,"cookie":0x1101,"payload":gathered.hex()},
            {"queue":0,"tag":0x540,"cookie":0x1500,"payload":tail.hex()},
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    inputs = parser.parse_args().directory
    with (inputs/"completions.hex").open() as completions, (inputs/"packets.txt").open() as packets:
        report = compare(
            fixture(), completion_rows(completions), assemble_stream(packets),
            strict_schedule=True, check_clock=True,
        )
    print(
        f"PASS segmented capture audit: {report['completion_count']} exact CQ records, "
        f"{report['packet_count']} frames, {report['copied_payload_bytes']} bytes"
    )


if __name__ == "__main__":
    main()
