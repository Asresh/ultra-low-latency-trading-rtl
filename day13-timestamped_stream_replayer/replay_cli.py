"""Compile, replay, and inspect timestamped FPGA tapes with Python's standard library."""

import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import sys

from replay_audit import read_transcript, verify
from replay_capture import CaptureSelection, import_pcap
from replay_control import compile_script
from replay_compare import ComparisonOptions, check_run, export_packets, select_run
from replay_format import (
    atomic_output,
    dump_packet_csv,
    load_packet_csv,
    parse_decimal,
    parse_integer,
    read_tape,
    same_path,
    write_tape,
)
from replay_metrics import Budget, load_runs
from replay_planner import CompileOptions, compile_packets, predict_report, rebase_tape, retime_tape
from replay_program import ReplayError, U32
from replay_vectors import encode_cycle, read_cyclestream


def reject_alias(inputs, target):
    """Protect input captures and command programs from output truncation or renaming."""
    for source in inputs:
        if source is not None and same_path(source, target):
            raise ReplayError("destination aliases one of the inputs")


def json_report(report, stream=None):
    """Write stable indented JSON to stdout or a test-provided sink."""
    stream = sys.stdout if stream is None else stream
    json.dump(report, stream, ensure_ascii=False, indent=2)
    stream.write("\n")


def integer(text):
    """Accept a bounded CLI integer in decimal or explicitly hexadecimal form."""
    return parse_integer(text, U32, "CLI integer")


def clock_options(args):
    """Make exact clock and packet timing options from CLI values."""
    return CompileOptions(
        parse_decimal(args.clock_hz, "clock frequency"),
        lead_ticks=args.lead,
        beat_spacing=args.beat_spacing,
        packet_gap=args.packet_gap,
        rounding=args.rounding,
        collision=args.collision,
        limit_ticks=args.maximum_tick,
        maximum_beats=args.depth,
        speed=parse_decimal(args.speed, "replay speed"),
    )


def source_packets(args, resources):
    """Open the selected source with a mode and validation policy appropriate to its format."""
    if args.operation == "compile-csv":
        stream = resources.enter_context(args.input.open(encoding="utf-8", newline=""))
        return load_packet_csv(stream)
    stream = resources.enter_context(args.input.open("rb"))
    selection = CaptureSelection(
        ethertype=args.ethertype,
        vlan=args.vlan,
        start_ns=args.start_ns,
        end_ns=args.end_ns,
        max_packets=args.max_packets,
        truncated=args.truncated,
        tag=args.tag,
        first_tag=args.first_tag,
    )
    return import_pcap(stream, selection, hard_limit=args.snap_limit)


def compile_source(args):
    """Build a checked tape from capture input and summarize the timing decisions."""
    reject_alias((args.input,), args.output)
    origin = parse_decimal(args.origin_ns, "origin") if args.origin_ns is not None else None
    with ExitStack() as resources:
        result = compile_packets(source_packets(args, resources), clock_options(args), origin=origin)
    write_tape(result.tape, args.output)
    json_report(result.report(verbose=args.verbose))
    return 0


def analyze_image(tape, *, detailed=False):
    """Describe the complete packets and optional byte hashes in a tape image."""
    packets = tuple(tape.packets())
    document = {
        "packets": len(packets),
        "beats": len(tape.events),
        "bytes": tape.payload_bytes,
        "first_tick": packets[0].start if packets else None,
        "last_tick": packets[-1].end if packets else None,
        "distinct_tags": len({p.tag for p in packets}),
    }
    if detailed:
        document["packet_details"] = [
            {
                "position": index,
                "tag": packet.tag,
                "bytes": len(packet.payload),
                "first_tick": packet.start,
                "final_tick": packet.end,
                "sha256": sha256(packet.payload).hexdigest(),
            }
            for index, packet in enumerate(packets)
        ]
    return document


def inspect_image(args):
    """Read an image and print checked content and optional no-stall timing."""
    tape = read_tape(args.input, args.depth)
    document = analyze_image(tape, detailed=args.verbose)
    if args.repeats is not None:
        document["prediction"] = predict_report(tape, args.repeats, args.gap)
    json_report(document)
    return 0


def transform_image(args):
    """Crop complete packets, optionally rebase or retime, then serialize."""
    reject_alias((args.input,), args.output)
    tape = read_tape(args.input).between(args.start, args.end)
    if args.new_origin is not None:
        tape = rebase_tape(tape, args.new_origin)
    if args.scale is not None:
        tape = retime_tape(tape, parse_decimal(args.scale, "scale"), args.rounding)
    if args.depth is not None:
        tape.checked_depth(args.depth)
    write_tape(tape, args.output)
    json_report(analyze_image(tape, detailed=args.verbose))
    return 0


def compile_control(args):
    """Compile commands and expected dispositions into a simulator stimulus tape."""
    reject_alias((args.input,), args.output)
    cycles, applied = compile_script(args.input, args.depth, max_cycles=args.max_cycles)
    # The writer owns this output path. No bytes have been written on a compile error.
    atomic_output(args.output, "w", lambda file: write_control_cycles(file, cycles))
    json_report({"cycles": len(cycles), "requests": [row.record() for row in applied]})
    return 0


def write_control_cycles(stream, cycles):
    """Write the length-prefixed pin stimulus format to an open file."""
    stream.write(f"{len(cycles)}\n")
    for cycle in cycles:
        stream.write(encode_cycle(cycle) + "\n")


def audit_trace(args):
    """Compare the actual hardware pins with the independent cycle model."""
    measured = verify(read_cyclestream(args.stim), read_transcript(args.trace),
                      args.depth, args.directed)
    json_report({
        "events": measured.events,
        "statuses": measured.statuses,
        "opcodes": measured.ops,
        "maximum_lateness": measured.maximum_lateness,
        "maximum_hold": measured.max_output_hold,
    })
    return 0


def trace_metrics(args):
    """Extract only actual handshakes, then apply optional timing or framing budgets."""
    runs = load_runs(args.stim, args.trace, allow_incomplete=args.allow_incomplete)
    budget = Budget(args.maximum_lateness, args.maximum_first, not args.require_complete)
    failures = tuple(budget.failures(runs))
    json_report({
        "runs": [run.record(detailed=args.verbose) for run in runs],
        "violations": failures,
    })
    return bool(failures)


def compare_program(args):
    """Verify accepted replay beats against a programmed tape without the cycle oracle."""
    tape = read_tape(args.input)
    run = select_run(load_runs(args.stim, args.trace, allow_incomplete=args.allow_cut), args.run)
    measured = check_run(tape, run, ComparisonOptions(
        args.repeats, args.gap, args.allow_cut, args.maximum_lateness,
    ))
    json_report(measured.record())
    return 0


def export_deliveries(args):
    """Write complete captured packets and selected tick timestamps to a fresh CSV."""
    reject_alias((args.stim, args.trace), args.output)
    run = select_run(load_runs(args.stim, args.trace, allow_incomplete=args.allow_incomplete), args.run)
    packets = tuple(export_packets(
        run, parse_decimal(args.clock_hz, "clock frequency"), reference=args.reference,
        rebase=not args.absolute, selected_pass=args.selected_pass,
    ))
    atomic_output(args.output, "w", lambda file: dump_packet_csv(packets, file))
    json_report({"packets": len(packets), "bytes": sum(len(packet.payload) for packet in packets)})
    return 0


def compile_arguments(parser):
    """Install flags shared between packet CSV and PCAP compilation."""
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--clock-hz", default="100000000")
    parser.add_argument("--origin-ns")
    parser.add_argument("--lead", type=integer, default=0)
    parser.add_argument("--beat-spacing", type=integer, default=1)
    parser.add_argument("--packet-gap", type=integer, default=1)
    parser.add_argument("--rounding", choices=("floor", "ceil", "nearest"), default="ceil")
    parser.add_argument("--collision", choices=("delay", "fail"), default="delay")
    parser.add_argument("--speed", default="1")
    parser.add_argument("--maximum-tick", type=integer, default=U32)
    parser.add_argument("--depth", type=integer)
    parser.add_argument("--verbose", action="store_true")
    parser.set_defaults(handler=compile_source)


def parser():
    """Construct the command-line parser and bind each operation to one handler."""
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="operation", required=True)
    compile_arguments(commands.add_parser("compile-csv", help="compile timed packet CSV"))
    pcap = commands.add_parser("compile-pcap", help="compile classic Ethernet PCAP")
    compile_arguments(pcap)
    pcap.add_argument("--ethertype", type=integer)
    pcap.add_argument("--vlan", type=integer)
    pcap.add_argument("--start-ns", type=int)
    pcap.add_argument("--end-ns", type=int)
    pcap.add_argument("--max-packets", type=integer)
    pcap.add_argument("--truncated", choices=("error", "reject", "keep"), default="error")
    pcap.add_argument("--tag", choices=("record", "sequential", "outer-vlan"), default="record")
    pcap.add_argument("--first-tag", type=integer, default=0)
    pcap.add_argument("--snap-limit", type=integer, default=1 << 20)
    inspect = commands.add_parser("inspect", help="validate a tape and report its coverage")
    inspect.add_argument("input", type=Path)
    inspect.add_argument("--depth", type=integer)
    inspect.add_argument("--repeats", type=integer)
    inspect.add_argument("--gap", type=integer, default=0)
    inspect.add_argument("--verbose", action="store_true")
    inspect.set_defaults(handler=inspect_image)
    transform = commands.add_parser("transform", help="crop, shift, or retime complete packets")
    transform.add_argument("input", type=Path)
    transform.add_argument("output", type=Path)
    transform.add_argument("--start", type=integer, default=0)
    transform.add_argument("--end", type=integer, default=U32)
    transform.add_argument("--depth", type=integer)
    transform.add_argument("--rebase", dest="new_origin", type=integer)
    transform.add_argument("--scale")
    transform.add_argument("--rounding", choices=("floor", "ceil", "nearest"), default="ceil")
    transform.add_argument("--verbose", action="store_true")
    transform.set_defaults(handler=transform_image)
    script = commands.add_parser("script", help="compile an interactive control sequence")
    script.add_argument("input", type=Path)
    script.add_argument("output", type=Path)
    script.add_argument("--depth", type=integer, default=16)
    script.add_argument("--max-cycles", type=integer, default=100000)
    script.set_defaults(handler=compile_control)
    audit = commands.add_parser("audit", help="compare the RTL capture with the independent model")
    audit.add_argument("stim", type=Path)
    audit.add_argument("trace", type=Path)
    audit.add_argument("--depth", type=integer, default=16)
    audit.add_argument("--directed", action="store_true")
    audit.set_defaults(handler=audit_trace)
    metrics = commands.add_parser("metrics", help="report physical and virtual timing from a capture")
    metrics.add_argument("stim", type=Path)
    metrics.add_argument("trace", type=Path)
    metrics.add_argument("--maximum-lateness", type=integer)
    metrics.add_argument("--maximum-first", type=integer)
    metrics.add_argument("--require-complete", action="store_true")
    metrics.add_argument("--allow-incomplete", action="store_true")
    metrics.add_argument("--verbose", action="store_true")
    metrics.set_defaults(handler=trace_metrics)
    compare = commands.add_parser("compare", help="check captured delivery against a tape")
    compare.add_argument("input", type=Path)
    compare.add_argument("stim", type=Path)
    compare.add_argument("trace", type=Path)
    compare.add_argument("--run", type=integer)
    compare.add_argument("--repeats", type=integer, default=1)
    compare.add_argument("--gap", type=integer, default=0)
    compare.add_argument("--allow-cut", action="store_true")
    compare.add_argument("--maximum-lateness", type=integer)
    compare.set_defaults(handler=compare_program)
    export = commands.add_parser("export", help="export complete accepted packets as timed CSV")
    export.add_argument("stim", type=Path)
    export.add_argument("trace", type=Path)
    export.add_argument("output", type=Path)
    export.add_argument("--run", type=integer)
    export.add_argument("--clock-hz", default="100000000")
    export.add_argument("--reference", choices=("physical", "accepted", "offered", "due"), default="physical")
    export.add_argument("--pass", dest="selected_pass", type=integer)
    export.add_argument("--absolute", action="store_true")
    export.add_argument("--allow-incomplete", action="store_true")
    export.set_defaults(handler=export_deliveries)
    return root


def main(argv=None):
    """Run one offline replay workflow and report errors without a traceback."""
    root = parser()
    args = root.parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError) as exc:
        root.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
