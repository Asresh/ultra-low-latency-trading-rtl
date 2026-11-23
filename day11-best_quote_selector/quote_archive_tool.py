#!/usr/bin/env python3
"""Pack and unpack portable best-quote replay or sample archives."""

import argparse
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

from quote_archive import Kind, format_for, read_archive, write_archive
from quote_schema import PROFILES, row_text
from quote_tool import reject_aliases
from quote_verify import read_drives, read_samples


def normalize_kind(name):
    """Convert a textual archive channel to its binary header identifier."""
    return Kind.DRIVES if name == "drives" else Kind.TRACE


def _replace_file(destination, mode, callback):
    """Produce a complete sibling temporary file before atomically replacing an artifact."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    arguments = {} if "b" in mode else {"encoding": "ascii"}
    temporary = None
    try:
        with NamedTemporaryFile(
            mode=mode, dir=destination.parent, delete=False, **arguments
        ) as output:
            temporary = Path(output.name)
            count = callback(output)
        temporary.replace(destination)
        return count
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def pack_file(source, target, kind, profile):
    """Convert strict decimal rows to binary records without risking an existing artifact."""
    reject_aliases([source], [target])
    parse = read_drives if kind == Kind.DRIVES else read_samples
    presentation = format_for(kind, profile)
    return _replace_file(
        target, "wb", lambda writer: write_archive(writer, presentation, parse(source, profile))
    )


def _emit_text(records, stream, kind):
    """Reconstruct the exact existing decimal driver or cycle-trace format."""
    count = 0
    for index, row in enumerate(records):
        values = row.encode()
        if kind == Kind.TRACE:
            values = (index, *values)
        stream.write(row_text(values) + "\n")
        count += 1
    return count


def unpack_file(source, target, expected):
    """Decode an archive atomically, optionally requiring a specific geometry and channel."""
    reject_aliases([source], [target])
    with source.open("rb") as stream:
        fmt, records = read_archive(stream, expected)
        count = _replace_file(target, "w", lambda writer: _emit_text(records, writer, fmt.kind))
    return fmt, count


def arguments(argv=None):
    """Parse the pack or unpack path and optional stream dimensions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("pack", "unpack"))
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--kind", choices=("drives", "trace"))
    parser.add_argument("--profile", choices=PROFILES)
    return parser.parse_args(argv)


def main(argv=None):
    """Dispatch a binary conversion and signal any format or filesystem failure."""
    args = arguments(argv)
    try:
        if args.operation == "pack":
            if args.kind is None:
                raise ValueError("pack requires --kind")
            kind = normalize_kind(args.kind)
            profile = PROFILES[args.profile or "default"]
            count = pack_file(args.source, args.target, kind, profile)
        else:
            if (args.kind is None) != (args.profile is None):
                raise ValueError("unpack expects both --kind and --profile or neither")
            expected = None
            if args.kind is not None:
                expected = format_for(normalize_kind(args.kind), PROFILES[args.profile])
            fmt, count = unpack_file(args.source, args.target, expected)
            kind = fmt.kind
        print(f"{args.operation}: {count} validated {kind.name.lower()} rows")
    except (ValueError, OSError) as exc:
        print(f"quote_archive_tool: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
