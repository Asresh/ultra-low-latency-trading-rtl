"""Count project source records without crediting comments, prose or generated files."""

import argparse
import ast
import difflib
import io
import re
import subprocess
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_PROJECTS = (
    "day01-redundant_market_data",
    "day02-banked_matching_engine",
    "day03-order_risk_gateway",
)
EXPANSION_PROJECTS = (
    "day04-ethernet_udp_frontend",
    "day05-market_state_quote_engine",
    "day06-multiclock_latency_telemetry",
    "day07-ouch_order_session",
    "day08-host_dma_queues",
)
BEGINNER_PROJECTS = (
    "day10-stream_width_converter",
    "day11-best_quote_selector",
    "day12-message_rate_limiter",
    "day13-timestamped_stream_replayer",
    "day14-fixed_point_notional",
)
PROJECTS = LEGACY_PROJECTS + EXPANSION_PROJECTS + BEGINNER_PROJECTS
BASELINE_COMMIT = "80c7c41209814901a715ce0ee029ee4868b742cf"
IGNORED_DIRS = {"build", "obj_dir", "fixtures", "generated", "__pycache__"}


def sv_code_lines(source):
    """Count compilable text after removing SV line and block comments."""
    # Match strings as a unit so a literal URL or escaped quote survives.
    grammar = r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/'
    stripped = re.sub(
        grammar,
        lambda match: match[0] if match[0].startswith('"') else "\n" * match[0].count("\n"),
        source,
    )
    return [line.strip() for line in stripped.splitlines() if line.strip()]


def py_code_lines(source):
    """Retain only lines carrying Python tokens outside docstrings."""
    prose = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.body and isinstance(node.body[0], ast.Expr)
        ):
            first = node.body[0]
            if isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                prose.update(range(first.lineno, first.end_lineno + 1))
    ignored = {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
    }
    rows = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in ignored and token.start[0] not in prose:
            rows.add(token.start[0])
    lines = source.splitlines()
    return [lines[row - 1].strip() for row in sorted(rows)]


def source_lines(path, body):
    """Select the appropriate lexical counting rule for the file's language."""
    return py_code_lines(body) if path.suffix == ".py" else sv_code_lines(body)


def read_baseline(path):
    """Retrieve the same named source from the original release, if unambiguous."""
    # Match the old flat filename even though the old layout had rtl/ and tb/.
    candidates = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", BASELINE_COMMIT], cwd=ROOT, text=True
    ).splitlines()
    matches = [old for old in candidates if Path(old).name == path.name]
    if len(matches) != 1:
        return []
    old_source = subprocess.check_output(
        ["git", "show", f"{BASELINE_COMMIT}:{matches[0]}"], cwd=ROOT, text=True
    )
    return source_lines(path, old_source)


def file_role(path):
    """Count implementation separately from testbenches, formal, and host tools."""
    if ("formal" in path.parts or path.suffix == ".py" or path.name.startswith("tb_")
            or path.stem.endswith("_tb")):
        return "verification"
    return "rtl"


def collect(project, compare):
    """Walk one project and total handwritten RTL and verification source."""
    counts = {"rtl": 0, "verification": 0, "added": 0}
    for path in sorted((ROOT / project).rglob("*")):
        if path.suffix not in {".sv", ".svh", ".v", ".vh", ".py"}:
            continue
        if IGNORED_DIRS.intersection(path.relative_to(ROOT / project).parts[:-1]):
            continue
        lines = source_lines(path, path.read_text(encoding="utf-8"))
        counts[file_role(path)] += len(lines)
        if compare:
            baseline = read_baseline(path)
            unchanged = sum(block.size for block in difflib.SequenceMatcher(
                a=baseline, b=lines, autojunk=False
            ).get_matching_blocks())
            counts["added"] += len(lines) - unchanged
    return counts


def main():
    """Print the source inventory and fail if an enabled line floor is unmet."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimum", type=int, default=0, help="require this many source lines per project")
    parser.add_argument("--compare", action="store_true", help="compare same-name files at the original release")
    parser.add_argument("--enforce", action="store_true", help="apply the portfolio's per-release source floors")
    parser.add_argument("--project", choices=PROJECTS, action="append", help="inspect selected projects")
    args = parser.parse_args()
    if args.minimum < 0:
        parser.error("--minimum cannot be negative")
    columns = "| Project | RTL | Verification / tools | Total |"
    alignment = "| --- | ---: | ---: | ---: |"
    if args.compare:
        columns += " Added or changed since baseline |"
        alignment += " ---: |"
    print(columns)
    print(alignment)
    below = []
    for project in args.project or PROJECTS:
        counts = collect(project, args.compare)
        total = counts["rtl"] + counts["verification"]
        row = f"| {project} | {counts['rtl']} | {counts['verification']} | {total} |"
        print(row + (f" {counts['added']} |" if args.compare else ""))
        minimum = args.minimum
        if args.enforce:
            floor = 2000 if project in LEGACY_PROJECTS else 5001
            minimum = max(floor, minimum)
        if total < minimum:
            below.append(f"{project} ({total} < {minimum})")
    if below:
        parser.exit(1, f"Source floor unmet: {', '.join(below)}\n")


if __name__ == "__main__":
    main()
