"""The documentation-reference lock: every ``path:line`` in the docs lands on something.

A ``path:line`` reference in a document is a **promise to a reader**: *"go
there and this is what you will find."* The promise is worthless if the line
has moved, and the failure is silent — a wrong reference reads exactly like a
right one, because the sentence around it was written when the reference was
true. Nothing in a test suite notices. The only thing that notices is a
reader who follows it, finds something else, and loses an hour.

This file is the lock that stops the promise rotting. It extracts every
``path:line`` reference from the markdown in scope and asserts three things
about each one:

* **the file exists** — and names exactly one file, so a reader can find it;
* **the line exists** — the cited line is not past the end of the file;
* **the cited line is not blank** — a blank line is almost always a wrong
  reference, because a reference is written to point at *something*. This is
  how ``cli/main.py:208`` and ``daemon.py:61`` were found: both cited a blank
  line, and the sentence around each was about a different line entirely.

It is a **lock**, not a report: it runs in the suite, so a stale reference
fails the build rather than waiting for the next reader to trip over it.

The scope, and why it is what it is
-----------------------------------

**In scope:** every markdown file under ``docs/``, plus ``README.md`` and
``AGENTS.md`` at the repository root. That is the documentation a reader is
sent to by the repository itself.

**Out of scope, with the reason stated here rather than left implicit:**

``docs/agent-ops/``
    This repository's own run trail — the working record of an operations
    run. A reference in it was true when it was written and is *allowed* to
    have rotted since: the file is a record of what the run found, not a
    description of the tree. A run-scoped report is "a directory the next run
    will not read" (the repository says so itself), and a lock that fails on
    the run's own history would make the trail unwriteable.

``docs/plans/``
    A plan is a **dated proposal**, and it discusses references as *findings*
    rather than citing them — the plan for this very task quotes
    ``cli/main.py:208`` and ``daemon.py:61`` as the two blank-line
    references it found, which is the opposite of a citation. A plan also
    belongs to the run that wrote it, not to the task that sweeps the docs.

Both exclusions are **whole directories with a stated reason**, not a list of
documents that happen to fail. The lock still catches a reference that rots
inside its scope, and the test at the bottom of this file proves it: a
synthetic tree with a broken reference in it goes red.

What the lock cannot see
------------------------

A reference can be **wrong without being broken**: the line exists, it is not
blank, and it no longer holds what the sentence claims. This lock cannot see
that — only a reader can. The sweep that introduced it read every reference
it corrected and checked that the sentence and the code still agree, and the
count of those is in the report that introduced the lock. The lock covers
what is mechanically decidable; the reading is the other half, and it is
human work by nature.

Resolution
----------

A cited path is resolved against the repository root, in this order:

1. a path **containing a slash** is repository-relative (``src/paa/x.py``,
   ``tests/conventions/y.py``, ``docs/z.md``);
2. a path **containing a slash** that does not resolve at the root is also
   tried under ``src/paa/`` — the docs' standing shorthand for "a module in
   the package" (``ledger/store.py``, ``cli/main.py``);
3. a path **without a slash** is a file name: the root of the repository
   first (``README.md``, ``pyproject.toml``), and otherwise the unique file
   in the tree with that name.

Rule 3's "unique" is the point. A bare ``base.py`` names **eight** files in
this tree, and a reference that names eight possible files names no file at
all — a reader following it has to guess. The lock calls that a defect, and
the fix is to qualify the path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: The repository root. Every cited path is resolved against it, and the
#: suite runs from wherever pytest was started, so it is derived from this
#: file's own location rather than from the working directory.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The directories whose markdown the lock covers, repository-relative.
SCOPE_DIRECTORIES: tuple[str, ...] = ("docs",)

#: The individual markdown files the lock covers, repository-relative.
SCOPE_FILES: tuple[str, ...] = ("README.md", "AGENTS.md")

#: Directories excluded from the scope, and the reason for each exclusion.
#:
#: The reason is part of the rule: an exclusion without one is a hole, and a
#: hole in a lock is where the next stale reference goes to hide. Every
#: exclusion below is a *category* of document, not a document that happened
#: to fail.
EXCLUDED_DIRECTORIES: dict[str, str] = {
    "docs/agent-ops": (
        "the run trail — a record of what a run found, not a description of the "
        "tree; a reference in it was true when it was written and is allowed to "
        "have rotted since, and a run-scoped report is 'a directory the next run "
        "will not read'"
    ),
    "docs/plans": (
        "dated proposals — a plan discusses references as findings rather than "
        "citing them (this task's own plan quotes 'cli/main.py:208' and "
        "'daemon.py:61' as the blank-line references it found), and a plan "
        "belongs to the run that wrote it"
    ),
}

#: Directories that are never part of the tree the lock reasons about: build
#: artefacts, caches and version-control metadata. A file in one of them is
#: not something a document can point a reader at.
IGNORED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)

#: A ``path:line`` reference: a file path, a colon, and a line or a line
#: range. The path must carry a **file extension**, and the extension must be
#: alphabetic — a numeric one is a port or a version, not a file
#: (``127.0.0.1:8787``, ``qwen2.5:3``).
_REFERENCE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_.\-]+\.[A-Za-z0-9_]+)"
    r":(?P<first>\d+)(?:\s*-\s*(?P<last>\d+))?"
)

#: A fenced code block. Its contents are examples, not citations — a shell
#: snippet in a document contains plenty of colons and numbers.
_FENCE = re.compile(r"^\s*(?:```|~~~)")

#: A heading. A heading that names a file scopes the citations under it: the
#: bare ``:NNN`` shorthand means a line in *that* file.
_HEADING = re.compile(r"^\s*#{1,6}\s")

#: An inline code span. The shorthand always lives in one.
_SPAN = re.compile(r"`([^`\n]+)`")

#: A **continuation**: a bare ``:NNN`` that names a line in the file the
#: surrounding citation named — the shorthand the module documents use for
#: "the same file, further down". The colon must not follow a path character,
#: or it would be the line half of a full citation.
_CONTINUATION = re.compile(r"^:(?P<first>\d+)(?:\s*-\s*(?P<last>\d+))?$")

#: A backticked token that names a file: a path with an alphabetic extension,
#: and not a directory (``src/paa/agents/`` is a package, not a file).
_FILE_NAME = re.compile(
    r"^(?P<path>(?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_.\-]+\.[A-Za-z0-9_]+)$"
)


@dataclass(frozen=True)
class Reference:
    """One citation found in a document.

    :param document: the markdown file the citation is in, repository-relative.
    :param document_line: the 1-based line of the markdown that carries it.
    :param path: the cited path. For a full citation, exactly as written; for
        a continuation, the file the shorthand resolves to.
    :param first: the first cited line.
    :param last: the last cited line — equal to :attr:`first` for a single line.
    :param continuation: whether the citation is the bare ``:NNN`` shorthand.
    """

    document: str
    document_line: int
    path: str
    first: int
    last: int
    continuation: bool = False

    @property
    def quoted(self) -> str:
        """The citation as a reader would write it: ``path:first``."""
        if self.continuation:
            if self.last == self.first:
                return f":{self.first}"
            return f":{self.first}-{self.last}"
        if self.last == self.first:
            return f"{self.path}:{self.first}"
        return f"{self.path}:{self.first}-{self.last}"


def _is_excluded(relative: str) -> bool:
    """Whether a repository-relative path is inside an excluded directory."""
    return any(
        relative == excluded or relative.startswith(f"{excluded}/")
        for excluded in EXCLUDED_DIRECTORIES
    )


def scope_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every markdown file in the lock's scope, in a stable order.

    :param root: the repository root to scan.
    :return: the files, sorted by their repository-relative path.
    """
    found: list[Path] = []
    for directory in SCOPE_DIRECTORIES:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            relative = path.relative_to(root).as_posix()
            if _is_excluded(relative):
                continue
            found.append(path)
    for name in SCOPE_FILES:
        path = root / name
        if path.is_file():
            found.append(path)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def find_references(text: str) -> list[tuple[int, str, int, int, bool]]:
    """Extract every ``path:line`` reference from a document's text.

    Fenced code blocks are skipped: a shell snippet contains colons and
    numbers that are not citations.

    The bare ``:NNN`` shorthand is extracted too, and resolved to the file it
    refers to. The resolution is the one a reader uses:

    1. a full citation **on the same line** wins;
    2. otherwise the file named by the **nearest preceding heading** that
       names one — a module document's ``### `orchestrator.py` — Chief
       Orchestrator`` scopes every shorthand under it;
    3. otherwise the **most recent full citation** earlier in the document.

    :param text: the document's text.
    :return: a list of ``(line number, path, first line, last line, is
        continuation)`` tuples, where a continuation's path is the file it
        resolved to.
    """
    found: list[tuple[int, str, int, int, bool]] = []
    in_fence = False
    section_path = ""
    carried_path = ""
    for number, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        spans = [(match.start(), match.end()) for match in _SPAN.finditer(line)]
        is_heading = bool(_HEADING.match(line))
        line_path = ""
        heading_path = ""
        for match in _REFERENCE.finditer(line):
            extension = match.group("path").rsplit(".", 1)[-1]
            if not extension.isalpha():
                continue
            first = int(match.group("first"))
            last = int(match.group("last")) if match.group("last") else first
            found.append((number, match.group("path"), first, last, False))
            line_path = match.group("path")
            carried_path = match.group("path")
        if is_heading:
            heading_path = line_path
            if not heading_path:
                for start, end in spans:
                    text_in_span = line[start + 1 : end - 1].strip()
                    if _FILE_NAME.match(text_in_span):
                        heading_path = text_in_span
                        break
            if heading_path:
                section_path = heading_path
        for start, end in spans:
            shorthand = _CONTINUATION.match(line[start + 1 : end - 1].strip())
            if shorthand is None:
                continue
            resolved = line_path or section_path or carried_path
            if not resolved:
                continue
            first = int(shorthand.group("first"))
            last = int(shorthand.group("last")) if shorthand.group("last") else first
            found.append((number, resolved, first, last, True))
    return found


def build_index(root: Path = REPO_ROOT) -> dict[str, list[Path]]:
    """Map every file name in the tree to the files that carry it.

    The result is cached: the lock resolves a thousand references, and
    walking the tree once per reference would make the suite slow for no
    reason.

    :param root: the repository root to walk.
    :return: a mapping of file name to the files with that name.
    """
    return _build_index(str(root))


@lru_cache(maxsize=4)
def _build_index(root: str) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    base = Path(root)
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if relative.parts[0] in IGNORED_DIRECTORIES:
            continue
        if any(part in IGNORED_DIRECTORIES for part in relative.parts):
            continue
        index.setdefault(path.name, []).append(path)
    return index


@lru_cache(maxsize=4096)
def _file_lines(path: str) -> tuple[str, ...]:
    """The lines of a file, read once and kept.

    :param path: the file to read.
    :return: the file's lines, as a tuple.
    """
    return tuple(Path(path).read_text(encoding="utf-8", errors="replace").splitlines())



def resolve(
    path: str,
    root: Path = REPO_ROOT,
    index: dict[str, list[Path]] | None = None,
) -> list[Path]:
    """Resolve a cited path to the files it could name.

    An empty list means *no such file*. More than one means the reference
    does not name a file a reader can find.

    :param path: the cited path, exactly as written.
    :param root: the repository root to resolve against.
    :param index: the file-name index; built on demand when omitted.
    :return: the candidate files, in resolution order.
    """
    if index is None:
        index = build_index(root)
    if "/" in path:
        for prefix in ("", "src/paa"):
            candidate = root / prefix / path if prefix else root / path
            if candidate.exists():
                return [candidate]
        return []
    at_root = root / path
    if at_root.exists():
        return [at_root]
    return [found for found in index.get(path, []) if found.parent != root]


def _line_text(path: Path, number: int) -> str:
    """The text of a 1-based line, or the empty string when out of range."""
    lines = _file_lines(str(path))
    if number < 1 or number > len(lines):
        return ""
    return lines[number - 1]


def check_reference(
    reference: Reference,
    root: Path = REPO_ROOT,
    index: dict[str, list[Path]] | None = None,
) -> str | None:
    """Check one reference against the tree.

    :param reference: the citation to check.
    :param root: the repository root to resolve against.
    :param index: the file-name index; built on demand when omitted.
    :return: a description of the defect, or ``None`` when the reference holds.
    """
    candidates = resolve(reference.path, root=root, index=index)
    if not candidates:
        return (
            f"{reference.document}:{reference.document_line}: "
            f"{reference.quoted!r} — no such file in the repository"
        )
    if len(candidates) > 1:
        names = ", ".join(
            candidate.relative_to(root).as_posix() for candidate in candidates
        )
        return (
            f"{reference.document}:{reference.document_line}: "
            f"{reference.quoted!r} — the name is ambiguous; it could be {names}"
        )
    target = candidates[0]
    total = len(_file_lines(str(target)))
    if reference.first > total or reference.last > total:
        return (
            f"{reference.document}:{reference.document_line}: "
            f"{reference.quoted!r} — {target.relative_to(root).as_posix()} has "
            f"{total} lines"
        )
    if not _line_text(target, reference.first).strip():
        return (
            f"{reference.document}:{reference.document_line}: "
            f"{reference.quoted!r} — the cited line of "
            f"{target.relative_to(root).as_posix()} is blank"
        )
    return None


def all_references(root: Path = REPO_ROOT) -> list[Reference]:
    """Every reference in the lock's scope.

    :param root: the repository root to scan.
    :return: the references, in document and line order.
    """
    build_index(root)
    found: list[Reference] = []
    for document in scope_files(root):
        relative = document.relative_to(root).as_posix()
        text = document.read_text(encoding="utf-8")
        for number, path, first, last, continuation in find_references(text):
            found.append(
                Reference(
                    document=relative,
                    document_line=number,
                    path=path,
                    first=first,
                    last=last,
                    continuation=continuation,
                )
            )
    return found


def _violations(references: list[Reference], root: Path = REPO_ROOT) -> list[str]:
    index = build_index(root)
    return [
        defect
        for reference in references
        if (defect := check_reference(reference, root=root, index=index)) is not None
    ]


def test_every_reference_names_exactly_one_file() -> None:
    """Every cited path resolves to a file a reader can find.

    A path that names no file is a broken promise. A path that names several
    — a bare ``base.py``, which is eight files in this tree — is the same
    promise with the specifics left out, and a reader cannot follow it
    without guessing.
    """
    index = build_index()
    broken: list[str] = []
    for reference in all_references():
        candidates = resolve(reference.path, root=REPO_ROOT, index=index)
        if not candidates:
            broken.append(
                f"{reference.document}:{reference.document_line}: "
                f"{reference.quoted!r} — no such file"
            )
        elif len(candidates) > 1:
            names = ", ".join(c.relative_to(REPO_ROOT).as_posix() for c in candidates)
            broken.append(
                f"{reference.document}:{reference.document_line}: "
                f"{reference.quoted!r} — ambiguous ({names})"
            )
    assert not broken, "unresolvable references:\n" + "\n".join(broken)


def test_every_referenced_line_is_within_the_file() -> None:
    """No citation points past the end of the file it names."""
    index = build_index()
    broken: list[str] = []
    for reference in all_references():
        candidates = resolve(reference.path, root=REPO_ROOT, index=index)
        if len(candidates) != 1:
            continue
        target = candidates[0]
        total = len(_file_lines(str(target)))
        if reference.first > total or reference.last > total:
            broken.append(
                f"{reference.document}:{reference.document_line}: "
                f"{reference.quoted!r} — {target.relative_to(REPO_ROOT).as_posix()} "
                f"has {total} lines"
            )
    assert not broken, "citations past the end of their file:\n" + "\n".join(broken)


def test_every_referenced_line_is_not_blank() -> None:
    """No citation lands on a blank line.

    A reference is written to point at *something*. A blank line is the
    signature of a reference whose target has moved: the line the writer
    meant is still there, two lines up or two lines down, and the citation
    kept the number it had when the code was shorter.
    """
    index = build_index()
    broken: list[str] = []
    for reference in all_references():
        candidates = resolve(reference.path, root=REPO_ROOT, index=index)
        if len(candidates) != 1:
            continue
        target = candidates[0]
        total = len(_file_lines(str(target)))
        if reference.first > total or reference.last > total:
            continue
        if not _line_text(target, reference.first).strip():
            broken.append(
                f"{reference.document}:{reference.document_line}: "
                f"{reference.quoted!r} — the cited line of "
                f"{target.relative_to(REPO_ROOT).as_posix()} is blank"
            )
    assert not broken, "citations on a blank line:\n" + "\n".join(broken)


def test_the_lock_is_not_vacuous() -> None:
    """The lock finds references at all, and finds the ones it should.

    A lock that finds nothing passes. This asserts the corpus is non-empty,
    that the documented exclusions really are excluded, and that a known
    reference in a known document is among the ones found — so a change to
    the extraction cannot quietly turn the lock into a no-op.
    """
    references = all_references()
    assert len(references) > 500, f"the lock found only {len(references)} references"
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in scope_files()}
    assert "README.md" in scanned
    assert "AGENTS.md" in scanned
    documents = {reference.document for reference in references}
    assert not any(document.startswith("docs/agent-ops/") for document in documents)
    assert not any(document.startswith("docs/plans/") for document in documents)
    assert any(reference.path == "src/paa/runtime.py" for reference in references)
    assert any(reference.document == "docs/README.md" for reference in references)


def test_the_lock_catches_a_broken_reference(tmp_path: Path) -> None:
    """A reference that rots inside the scope turns the lock red.

    The lock is only worth having if it fails. This builds a synthetic tree
    — the shape of a small repository, with a source file and a document that
    cites it — and breaks the citation three ways: the file is renamed, the
    line moves past the end of the file, and the cited line is blank. Each
    one has to be reported.
    """
    root = tmp_path / "repo"
    (root / "src" / "paa").mkdir(parents=True)
    (root / "src" / "paa" / "thing.py").write_text(
        "def thing():\n    return 1\n", encoding="utf-8"
    )
    (root / "docs").mkdir()
    (root / "docs" / "note.md").write_text(
        "See `src/paa/thing.py:2` for the thing.\n"
        "See `src/paa/missing.py:2` for the other thing.\n"
        "See `src/paa/thing.py:99` for the far thing.\n"
        "See `src/paa/thing.py:3` for the blank thing.\n",
        encoding="utf-8",
    )
    (root / "src" / "paa" / "thing.py").write_text(
        "def thing():\n    return 1\n\n", encoding="utf-8"
    )
    references = all_references(root)
    assert len(references) == 4, f"expected four references, found {len(references)}"
    defects = _violations(references, root=root)
    joined = "\n".join(defects)
    assert "no such file" in joined, joined
    assert "has 3 lines" in joined, joined
    assert "is blank" in joined, joined
    # The healthy reference is not reported.
    assert sum(1 for defect in defects if "thing.py:2" in defect) == 0, joined
