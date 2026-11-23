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
PROJECTS = (
    "day01-redundant_market_data",
    "day02-banked_matching_engine",
    "day03-order_risk_gateway",
)
BASELINE_COMMIT = "80c7c41209814901a715ce0ee029ee4868b742cf"
IGNORED_DIRS = {"build", "obj_dir", "fixtures", "generated", "__pycache__"}


def sv_code_lines(source):
    # Match strings as a unit so a literal URL or escaped quote survives.
    grammar = r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/'
    stripped = re.sub(
        grammar,
        lambda match: match[0] if match[0].startswith('"') else "\n" * match[0].count("\n"),
        source,
    )
    return [line.strip() for line in stripped.splitlines() if line.strip()]


def py_code_lines(source):
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
    return py_code_lines(body) if path.suffix == ".py" else sv_code_lines(body)


def read_baseline(path):
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
    if ("formal" in path.parts or path.suffix == ".py" or path.name.startswith("tb_")
            or path.stem.endswith("_tb")):
        return "verification"
    return "rtl"


def collect(project, compare):
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimum", type=int, default=0, help="require this many source lines per project")
    parser.add_argument("--compare", action="store_true", help="compare same-name files at the original release")
    args = parser.parse_args()
    columns = "| Project | RTL | Verification / tools | Total |"
    alignment = "| --- | ---: | ---: | ---: |"
    if args.compare:
        columns += " Added or changed since baseline |"
        alignment += " ---: |"
    print(columns)
    print(alignment)
    below = []
    for project in PROJECTS:
        counts = collect(project, args.compare)
        total = counts["rtl"] + counts["verification"]
        row = f"| {project} | {counts['rtl']} | {counts['verification']} | {total} |"
        print(row + (f" {counts['added']} |" if args.compare else ""))
        if total < args.minimum:
            below.append(project)
    if below:
        parser.exit(1, f"Below {args.minimum} source lines: {', '.join(below)}\n")


if __name__ == "__main__":
    main()
