#!/usr/bin/env python3
"""Convert a named-venue CSV capture into a Day 11 playback event tape."""

import argparse
from dataclasses import asdict
from pathlib import Path
import sys

from quote_csv import read_catalog, read_csv
from quote_replay import write_events
from quote_schema import PROFILES
from quote_tool import reject_aliases
from quote_verify import save_report


def arguments(argv=None):
    """Parse normalized input, catalog, geometry, and output choices."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("events", type=Path)
    parser.add_argument("--clock-hz", type=int, default=100_000_000)
    parser.add_argument("--profile", choices=PROFILES, default="default")
    parser.add_argument("--report", type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    """Validate the entire source before publishing its deterministic event plan."""
    args = arguments(argv)
    try:
        outputs = [args.events]
        if args.report is not None:
            outputs.append(args.report)
        reject_aliases([args.source, args.catalog], outputs)
        profile = PROFILES[args.profile]
        catalog = read_catalog(args.catalog, profile)
        with args.source.open(encoding="utf-8", newline="") as stream:
            events, summary = read_csv(stream, catalog, profile, args.clock_hz)
        args.events.parent.mkdir(parents=True, exist_ok=True)
        write_events(args.events, events, profile)
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            save_report(args.report, asdict(summary))
        print(f"Imported {summary.rows} data rows through cycle {summary.last_cycle}")
    except (ValueError, OSError) as exc:
        print(f"quote_csv_tool: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
