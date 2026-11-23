#!/usr/bin/env python3
"""Run a complete beginner-readable quote capture through the actual RTL."""

import argparse
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path
import sys

from quote_analysis import analyze, render_markdown
from quote_archive import Kind, format_for
from quote_archive_tool import pack_file, unpack_file
from quote_capture import QueryCapture
from quote_compare import compare_captures
from quote_csv import Catalog, read_csv
from quote_replay import schedule, write_events
from quote_schema import PROFILES, State
from quote_tool import checked_capture, reject_aliases, simulation, write_capture
from quote_verify import TraceError, save_report, write_drives


EXAMPLE = """time_ns,kind,instrument,venue,sequence,bid_price,bid_qty,ask_price,ask_qty,max_age,tag
1000,reset,,,,,,,,,
1010,replace,ALPHA,EAST,10,100,4,106,5,,1
1020,replace,ALPHA,WEST,1,102,6,105,2,,2
1020,query,ALPHA,,,,,,,16,11
1030,query,ALPHA,,,,,,,16,12
1040,replace,ALPHA,EAST,11,107,3,110,7,,3
1040,query,ALPHA,,,,,,,16,13
1050,query,ALPHA,,,,,,,16,14
1060,disable,ALPHA,WEST,,,,,,,4
1060,query,ALPHA,,,,,,,16,15
1070,query,ALPHA,,,,,,,2,16
1080,query,ALPHA,,,,,,,4,17
1090,withdraw,ALPHA,EAST,12,,,,,,5
1100,query,ALPHA,,,,,,,16,18
"""


EXPECTED = {
    # Context is (state, best bid, best ask, signed spread, stale mask).
    11: (State.OPEN, 100, 106, 6, 0),
    12: (State.OPEN, 102, 105, 3, 0),
    13: (State.OPEN, 102, 105, 3, 0),
    14: (State.CROSSED, 107, 105, -2, 0),
    15: (State.CROSSED, 107, 105, -2, 0),
    16: (State.EMPTY, 0, 0, 0, 1),
    17: (State.OPEN, 107, 110, 3, 0),
    18: (State.EMPTY, 0, 0, 0, 0),
}


def example_inputs():
    """Build dated named-venue observations with an intentional quote cross and cutoff."""
    profile = PROFILES["default"]
    aliases = {"instruments": {"ALPHA": 0}, "venues": {"EAST": 0, "WEST": 1}}
    catalog = Catalog.from_object(aliases, profile)
    events, normalized = read_csv(StringIO(EXAMPLE), catalog, profile)
    replay = schedule(profile, events)
    return profile, aliases, normalized, events, replay


def _check_named_queries(records):
    """Check the beginner example table independently of the executable cycle model."""
    seen = {}
    for item in records:
        if isinstance(item, QueryCapture):
            result = item.result
            if result.tag in seen:
                raise TraceError(f"duplicate example tag {result.tag}")
            seen[result.tag] = (
                result.state, result.bid.price, result.ask.price,
                result.spread, result.stale,
            )
    if seen != EXPECTED:
        delta = {
            tag: {"expected": EXPECTED.get(tag), "observed": seen.get(tag)}
            for tag in sorted(seen.keys() | EXPECTED.keys())
            if seen.get(tag) != EXPECTED.get(tag)
        }
        raise TraceError(f"named example changed: {delta}")
    return seen


def _archive_round_trip(profile, raw, folder):
    """Keep a verified packed trace and prove it reproduces the bench capture exactly."""
    archive = folder / "trace.bq"
    decoded = folder / "trace-decoded.txt"
    count = pack_file(raw, archive, Kind.TRACE, profile)
    fmt, restored = unpack_file(archive, decoded, format_for(Kind.TRACE, profile))
    if restored != count or fmt.profile != profile:
        raise TraceError("archive round trip changed profile or row count")
    if decoded.read_bytes() != raw.read_bytes():
        raise TraceError("archive round trip changed trace bytes")
    return decoded


def _schedule_report(profile, normalized, replay):
    """Record the date-to-cycle translation and replay acceptance separately."""
    return {
        "profile": profile.sv_parameters(),
        "normalization": asdict(normalized),
        "input_edges": len(replay.drives),
        "accepted": [asdict(item) | {"delay": item.delay} for item in replay.accepted],
        "canceled": [asdict(item) for item in replay.canceled],
    }


def run(output, image, vvp):
    """Create the source, run RTL, check the example, and build replay artifacts."""
    profile, aliases, normalized, events, replay = example_inputs()
    paths = (
        output / "quotes.csv", output / "catalog.json", output / "events.jsonl",
        output / "drive.txt", output / "trace.txt", output / "schedule.json",
        output / "capture.jsonl", output / "metrics.json", output / "report.md",
        output / "trace.bq", output / "trace-decoded.txt",
    )
    reject_aliases([image], paths)
    output.mkdir(exist_ok=True, parents=True)
    csv, catalog, event_file, driver, trace, plan, capture, metrics, markdown, _, _ = paths
    csv.write_text(EXAMPLE, encoding="ascii")
    catalog.write_text(json.dumps(aliases, indent=2) + "\n", encoding="utf-8")
    write_events(event_file, events, profile)
    write_drives(driver, replay.drives, profile)
    save_report(plan, _schedule_report(profile, normalized, replay))
    simulation(vvp, image, driver, trace)
    result, captured = checked_capture(profile, driver, trace, "named-venue-example")
    known = _check_named_queries(captured)
    rebuilt = _archive_round_trip(profile, trace, output)
    _, archived = checked_capture(profile, driver, rebuilt, "archive-reconstruction")
    if not compare_captures(captured, archived, maximum_p95=0)["ok"]:
        raise TraceError("archive replay changed the observation or latency")
    write_capture(capture, captured)
    report = analyze(captured, profile)
    save_report(metrics, {"timing": result, "market": report})
    markdown.write_text(render_markdown(report), encoding="utf-8")
    print(f"PASS {len(known)} named hardware queries, including cross, freshness, and reset")
    print(f"  Source and checked reports are in {output}")
    return result


def arguments(argv=None):
    """Parse an elaborated default simulator and destination for the walkthrough."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/example"))
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--vvp", default="vvp")
    return parser.parse_args(argv)


def main(argv=None):
    """Run the checked walkthrough and make trace failures visible to make."""
    args = arguments(argv)
    try:
        run(args.output, args.image, args.vvp)
    except (OSError, ValueError) as exc:
        print(f"quote_demo: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
