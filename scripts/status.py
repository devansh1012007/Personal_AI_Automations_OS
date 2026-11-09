#!/usr/bin/env python3
"""Generate the README status block, and check that it is unedited.

Usage:
    python scripts/status.py            print the generated block (markers included)
    python scripts/status.py --write    replace the block in README.md
    python scripts/status.py --check    exit non-zero if README.md disagrees

The block reports two numbers. Both are pinned below so that a re-run on the
same checkout produces byte-identical output, here and in CI.

Source lines
    What: the total number of physical lines in ``src/**/*.py``.
    Method (pinned): every ``*.py`` file under ``src/``, read with
    ``Path.read_text(encoding="utf-8")`` and counted with ``splitlines()``.
    Blank lines and comment lines are counted; nothing is filtered. This is
    the ``cloc``-equivalent total implemented in plain Python so the count
    does not depend on a ``cloc`` binary being installed. ``cloc`` itself is
    not run: its totals vary with version and flags.
    Why not non-blank lines: the spec's 57,300 figure is physical lines
    [docs/2026-11-09-next-spec.md:34] while the investigator's 48,879 is
    non-blank lines [docs/plans/2026-11-09-next-spec-revised.md:201]. The two
    differ only by whether blank lines count. This script counts them.
    Stability: files are visited in sorted repository-relative order and the
    count is a pure sum, so the result cannot vary with filesystem order.

Tests collected
    What: how many tests pytest collects (it does not run them).
    Method (pinned): ``python -m pytest --collect-only -q -o addopts=""`` run
    with the current interpreter from the repository root; the count is the
    last ``N tests collected`` line of its output. The ``-o addopts=""``
    override drops the project's ``addopts`` exactly as the pinned invocation
    does, and the current interpreter is used (so a CI venv counts with the
    venv's interpreter, not a system one).
    Install shape: the count requires the documented developer install
    (``pip install -e ".[dev]"``). The ``dev`` extra carries ``paa[api]`` and
    ``paa[s3]``; a minimal install collects fewer tests because whole
    directories skip themselves at import. The CI ``status`` job therefore
    pins the ``[dev]`` shape.
    Why this is host-stable: collecting is not running. Tests gated on Docker,
    PostgreSQL or S3 servers still collect; they skip at run time. So Docker /
    server presence does not change the collected count. Outcome counts do
    depend on all of those, so they are deliberately not part of the
    generated block. Run ``python -m pytest tests/ -q`` for
    passed/skipped/xfailed/failed.

Check semantics
    ``--check`` compares the text strictly between the ``<!-- STATUS:BEGIN -->``
    and ``<!-- STATUS:END -->`` markers in ``README.md`` against a freshly
    generated block, byte for byte after normalising CRLF to LF. Any hand edit
    inside the markers fails the check; regenerating (``--write`` or pasting
    the printed block) makes it pass. The markers themselves must each appear
    exactly once and in order, otherwise the script exits 2 rather than
    guessing.
    Exit codes: 0 = ok (printed, written, or matched); 1 = ``--check`` found
    a mismatch; 2 = error (bad arguments, missing/duplicated markers, count
    failure).

    Only the standard library is used. No third-party package is imported at
    module scope, so the script stays runnable under every install shape.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

#: The status markers in README.md. Each must appear on its own line, exactly
#: once, begin before end. The generated block is the text strictly between
#: them; the markers are part of the printed snippet but not of the compared
#: body.
BEGIN_MARKER = "<!-- STATUS:BEGIN -->"
END_MARKER = "<!-- STATUS:END -->"

#: The pinned collected-count invocation, minus the interpreter and program.
#: ``-o addopts=`` (empty) drops the project's addopts, exactly as the pinned
#: ``pytest --collect-only -q -o addopts=""`` invocation does.
PYTEST_COLLECT_ARGV: tuple[str, ...] = (
    "-m",
    "pytest",
    "--collect-only",
    "-q",
    "-o",
    "addopts=",
)

#: Matches pytest's ``N tests collected`` summary line in collect-only output.
_COLLECTED_RE = re.compile(r"^(\d+)\s+tests?\s+collected\b", re.MULTILINE)


class StatusError(RuntimeError):
    """A user-facing failure of this script (exit code 2)."""


def repo_root() -> Path:
    """The repository root: the parent of this script's ``scripts/`` directory."""
    return Path(__file__).resolve().parents[1]


def count_source_lines(src_dir: Path) -> tuple[int, int]:
    """Count ``src/**/*.py`` files and their physical lines.

    :param src_dir: the ``src/`` directory.
    :return: ``(file_count, physical_line_count)``.
    :raises StatusError: when ``src_dir`` is missing or a file cannot be read.
    """
    if not src_dir.is_dir():
        raise StatusError(f"source directory not found: {src_dir}")
    files = sorted(
        src_dir.rglob("*.py"),
        key=lambda p: p.relative_to(src_dir).as_posix(),
    )
    total = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise StatusError(f"cannot read {path.as_posix()}: {exc}") from exc
        total += len(text.splitlines())
    return len(files), total


def count_collected(root: Path) -> int:
    """Run the pinned collect-only invocation and return the collected count.

    :param root: the repository root, used as the subprocess working directory.
    :return: the number of tests collected.
    :raises StatusError: when pytest is unavailable, collection errors, or the
        summary line cannot be parsed. Failures are never silently defaulted:
        a wrong count in the README is worse than a red check.
    """
    env = dict(os.environ)
    # PYTEST_ADDOPTS would inject the caller's filters into the count. The
    # pinned invocation names no filters, so the caller's environment must
    # not add any.
    env.pop("PYTEST_ADDOPTS", None)
    argv = [sys.executable, *PYTEST_COLLECT_ARGV]
    try:
        proc = subprocess.run(  # noqa: S603  # argv is fixed: interpreter + constant tuple, no shell, no caller input
            argv,
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            shell=False,
        )
    except OSError as exc:
        raise StatusError(f"cannot run pytest: {exc}") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + "\n" + proc.stderr).splitlines()[-20:])
        raise StatusError(f"pytest --collect-only failed (exit {proc.returncode}):\n{tail}")
    matches = _COLLECTED_RE.findall(proc.stdout)
    if not matches:
        raise StatusError("pytest --collect-only output had no 'tests collected' line")
    return int(matches[-1])


def generate_body(root: Path) -> str:
    """Generate the block body (the text strictly between the markers).

    :param root: the repository root.
    :return: the block body with ``\\n`` line endings, no leading/trailing
        blank line. Includes the marker-adjacent newlines in the returned
        middle text by the caller's convention (see :func:`generate_snippet`).
    """
    file_count, line_count = count_source_lines(root / "src")
    collected = count_collected(root)
    lines = [
        (
            f"**Generated status** (`scripts/status.py`): {file_count} "
            f"`src/**/*.py` files, {line_count:,} physical lines; "
            f"{collected:,} tests collected."
        ),
        (
            "Method: physical lines count blank and comment lines "
            "(`Path.read_text` + `splitlines` over `src/**/*.py`); "
            'collected = `pytest --collect-only -q -o addopts=""`. '
            "Outcome counts are not measured here (host-dependent); "
            "run `python -m pytest tests/ -q`."
        ),
    ]
    return "\n".join(lines)


def generate_snippet(root: Path) -> str:
    """Generate the full marker-delimited snippet, as printed and as written.

    :param root: the repository root.
    :return: ``"BEGIN\\n<body>\\nEND\\n"`` with ``\\n`` line endings.
    """
    return f"{BEGIN_MARKER}\n{generate_body(root)}\n{END_MARKER}\n"


def read_readme(readme_path: Path) -> str:
    """Read README.md preserving its line endings for later comparison.

    :param readme_path: path to the README.
    :return: the file text with original line endings intact.
    :raises StatusError: when the file cannot be read or decoded.
    """
    try:
        data = readme_path.read_bytes()
    except OSError as exc:
        raise StatusError(f"cannot read {readme_path}: {exc}") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StatusError(f"cannot decode {readme_path} as UTF-8: {exc}") from exc


def extract_middle(text: str, readme_path: Path) -> str:
    """Extract the block body strictly between the markers (normalised EOLs).

    :param text: the README text (raw line endings preserved).
    :param readme_path: used only in error messages.
    :return: the middle text with CRLF normalised to LF.
    :raises StatusError: when the markers are missing, duplicated, or out
        of order.
    """
    normalised = text.replace("\r\n", "\n")
    begin_count = normalised.count(BEGIN_MARKER)
    end_count = normalised.count(END_MARKER)
    if begin_count != 1 or end_count != 1:
        raise StatusError(
            f"{readme_path}: expected exactly one {BEGIN_MARKER!r} and one "
            f"{END_MARKER!r}, found {begin_count} and {end_count}"
        )
    begin_start = normalised.index(BEGIN_MARKER)
    end_start = normalised.index(END_MARKER)
    if end_start < begin_start:
        raise StatusError(f"{readme_path}: {END_MARKER!r} appears before {BEGIN_MARKER!r}")
    middle_start = begin_start + len(BEGIN_MARKER)
    return normalised[middle_start:end_start]


def generated_middle(root: Path) -> str:
    """The expected middle text (between markers) for a fresh generation.

    :param root: the repository root.
    :return: ``"\\n<body>\\n"`` with ``\\n`` line endings.
    """
    return f"\n{generate_body(root)}\n"


def check_readme(root: Path, readme_path: Path) -> int:
    """Compare README.md's block against a fresh generation (``--check``).

    :param root: the repository root (counts are measured here).
    :param readme_path: the README to compare.
    :return: 0 when the blocks match, 1 when they differ.
    :raises StatusError: when the README or counting fails (exit 2).
    """
    text = read_readme(readme_path)
    actual_middle = extract_middle(text, readme_path)
    expected_middle = generated_middle(root)
    if actual_middle == expected_middle:
        print("status: README.md block matches the generated block")
        return 0
    print("status: README.md block differs from the generated block", file=sys.stderr)
    diff = difflib.unified_diff(
        expected_middle.splitlines(keepends=True),
        actual_middle.splitlines(keepends=True),
        fromfile="generated",
        tofile=str(readme_path),
    )
    sys.stdout.writelines(diff)
    print("status: regenerate with `python scripts/status.py --write`", file=sys.stderr)
    return 1


def write_readme(root: Path, readme_path: Path) -> int:
    """Replace README.md's block with a fresh generation (``--write``).

    :param root: the repository root (counts are measured here).
    :param readme_path: the README to rewrite.
    :return: 0.
    :raises StatusError: when rewriting fails (exit 2).
    """
    text = read_readme(readme_path)
    # Validate the markers before generating, so a malformed README fails fast
    # without having spent a collection run.
    extract_middle(text, readme_path)
    # Normalise EOLs for the edit, then re-apply the file's original ending so
    # the rewrite does not reflow the rest of the document on Windows checkouts.
    eol = "\r\n" if "\r\n" in text else "\n"
    normalised = text.replace("\r\n", "\n")
    begin_start = normalised.index(BEGIN_MARKER)
    end_start = normalised.index(END_MARKER)
    middle_start = begin_start + len(BEGIN_MARKER)
    new_middle = generated_middle(root)
    updated = normalised[:middle_start] + new_middle + normalised[end_start:]
    if eol != "\n":
        updated = updated.replace("\n", eol)
    try:
        tmp_path = readme_path.parent / (readme_path.name + ".tmp")
        tmp_path.write_bytes(updated.encode("utf-8"))
        tmp_path.replace(readme_path)
    except OSError as exc:
        raise StatusError(f"cannot write {readme_path}: {exc}") from exc
    print(f"status: wrote generated block to {readme_path}")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse this script's arguments.

    :param argv: argument vector, or ``None`` for ``sys.argv[1:]``.
    :return: the parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="Generate the README status block, or check it is unedited."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the README block differs from a fresh generation",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="rewrite the README block with a fresh generation",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=None,
        help="README path (default: <repo>/README.md)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    :param argv: argument vector, or ``None`` for ``sys.argv[1:]``.
    :return: the process exit code.
    """
    args = parse_args(argv)
    root = repo_root()
    readme_path = args.readme if args.readme is not None else root / "README.md"
    try:
        if args.check:
            return check_readme(root, readme_path)
        if args.write:
            return write_readme(root, readme_path)
        sys.stdout.write(generate_snippet(root))
        return 0
    except StatusError as exc:
        print(f"status: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
