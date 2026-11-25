"""One-shot citation remapper: re-points docs references across a pure-insertion diff.

Reads the diff 0662027..HEAD for the four changed source files, builds an
old->new line map per file, walks the conventions lock's scope with the lock's
own regexes (positions included), and rewrites every citation whose target
line moved. Prints every edit for review.
"""

from __future__ import annotations

import re
import subprocess as sp
from pathlib import Path

ROOT = Path("/home/z/my-project/wt-thread")
BASE = "0662027"

CHANGED = [
    "src/paa/cli/main.py",
    "src/paa/core/types.py",
    "src/paa/ledger/recovery.py",
    "src/paa/ledger/replay.py",
]

_REFERENCE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_.\-]+\.[A-Za-z0-9_]+)"
    r":(?P<first>\d+)(?:\s*-\s*(?P<last>\d+))?"
)
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_SPAN = re.compile(r"`([^`\n]+)`")
_CONTINUATION = re.compile(r"^:(?P<first>\d+)(?:\s*-\s*(?P<last>\d+))?$")
_FILE_NAME = re.compile(
    r"^(?P<path>(?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_.\-]+\.[A-Za-z0-9_]+)$"
)


def line_map(path: str) -> dict[int, int] | None:
    """Old->new line numbers from a pure-insertion diff; None when not pure."""
    diff = sp.run(
        ["git", "diff", "--unified=3", f"{BASE}..HEAD", "--", path],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    old_total = len(
        sp.run(
            ["git", "show", f"{BASE}:{path}"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
    )
    # (old line the insertion sits before, count of inserted lines)
    insertions: list[tuple[int, int]] = []
    old_no = new_no = 0
    for line in diff.splitlines():
        if line.startswith("@@"):
            m = re.search(r"-(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))?", line)
            assert m is not None
            old_no = int(m.group(1))
            new_no = int(m.group(3))
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            if insertions and insertions[-1][0] == old_no:
                insertions[-1] = (old_no, insertions[-1][1] + 1)
            else:
                insertions.append((old_no, 1))
            new_no += 1
        elif line.startswith("-"):
            return None
        else:
            old_no += 1
            new_no += 1
    mapping: dict[int, int] = {}
    for old_line in range(1, old_total + 1):
        shift = sum(count for before, count in insertions if before <= old_line)
        mapping[old_line] = old_line + shift
    return mapping


def resolve(path: str) -> Path | None:
    if "/" in path:
        for prefix in ("", "src/paa"):
            candidate = ROOT / prefix / path if prefix else ROOT / path
            if candidate.exists():
                return candidate
        return None
    at_root = ROOT / path
    if at_root.exists():
        return at_root
    hits = [p for p in ROOT.rglob(path) if ".git" not in p.parts and p.parent != ROOT]
    return hits[0] if len(hits) == 1 else None


def scope_docs() -> list[Path]:
    docs = []
    for path in (ROOT / "docs").rglob("*.md"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("docs/agent-ops/", "docs/plans/")):
            continue
        docs.append(path)
    for name in ("README.md", "AGENTS.md"):
        if (ROOT / name).is_file():
            docs.append(ROOT / name)
    return sorted(docs, key=lambda p: p.relative_to(ROOT).as_posix())


def remap_doc(doc: Path, maps: dict[Path, dict[int, int]]) -> list[str]:
    text = doc.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    edits: list[str] = []
    in_fence = False
    section_path = ""
    carried_path = ""
    for number, line in enumerate(lines, 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        spans = [(m.start(), m.end()) for m in _SPAN.finditer(line)]
        is_heading = bool(_HEADING.match(line))
        line_path = ""

        def rewrite(match: re.Match[str], continuation: bool, path_resolved: str) -> str:
            target = resolve(path_resolved)
            if target is None or target not in maps:
                return match.group(0)
            mapping = maps[target]
            first = int(match.group("first"))
            last = int(match.group("last")) if match.group("last") else None
            if first not in mapping or (last is not None and last not in mapping):
                return match.group(0)
            new_first, new_last = mapping[first], mapping[last] if last else None
            if (new_first, new_last) == (first, last):
                return match.group(0)
            edits.append(
                f"  {doc.relative_to(ROOT).as_posix()}:{number}: "
                f"{path_resolved}:{first}{'-' + str(last) if last else ''} -> "
                f"{new_first}{'-' + str(new_last) if new_last else ''}"
            )
            if continuation:
                core = f":{new_first}" if new_last is None else f":{new_first}-{new_last}"
            else:
                core = f"{path_resolved}:{new_first}"
                if new_last is not None:
                    core += f"-{new_last}"
            return core

        out = line
        for match in list(_REFERENCE.finditer(line)):
            extension = match.group("path").rsplit(".", 1)[-1]
            if not extension.isalpha():
                continue
            replacement = rewrite(match, False, match.group("path"))
            if replacement != match.group(0):
                out = out.replace(match.group(0), replacement, 1)
            line_path = match.group("path")
            carried_path = match.group("path")
        heading_path = ""
        if is_heading:
            heading_path = line_path
            if not heading_path:
                for start, end in spans:
                    inner = line[start + 1 : end - 1].strip()
                    if _FILE_NAME.match(inner):
                        heading_path = inner
                        break
            if heading_path:
                section_path = heading_path
        for start, end in spans:
            inner = line[start + 1 : end - 1].strip()
            shorthand = _CONTINUATION.match(inner)
            if shorthand is None:
                continue
            resolved = line_path or section_path or carried_path
            if not resolved:
                continue
            replacement = rewrite(shorthand, True, resolved)
            if replacement != inner:
                out = out.replace("`" + inner + "`", "`" + replacement + "`", 1)
        lines[number - 1] = out
    if edits:
        doc.write_text("".join(lines), encoding="utf-8")
    return edits


def main() -> None:
    maps: dict[Path, dict[int, int]] = {}
    for path in CHANGED:
        mapping = line_map(path)
        if mapping is None:
            raise SystemExit(f"{path}: diff is not pure insertion")
        maps[ROOT / path] = mapping
    total = 0
    for doc in scope_docs():
        edits = remap_doc(doc, maps)
        total += len(edits)
        print("\n".join(edits), end="" if not edits else "\n")
    print(f"-> {total} citations re-pointed")


if __name__ == "__main__":
    main()
