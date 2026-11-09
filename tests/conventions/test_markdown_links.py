"""The markdown-link lock: every inline link in the docs lands on something.

A markdown link is a promise to a reader: *"go there and this is what you
will find."* The promise rots silently: a rename, a heading edit, or a move
like ``VISION.md`` into ``docs/`` breaks every relative target while the
sentence around it keeps reading fine. The ``path:line`` lock
(:mod:`tests.conventions.test_docs_references`) cannot see this at all — it
extracts ``path:line`` citations from inline code, not ``[text](target)``
destinations — so this file is the second half of the same promise, for
links: it extracts every inline markdown link in the same scope and asserts
two things about each one:

* **the target exists** — a file or a directory, resolved relative to the
  linking document (and, when that fails, relative to the repository root);
* **an anchor names a heading** — when the target carries ``#fragment``,
  the fragment is the GitHub-style slug of a heading in the target file.

It is a lock, not a report: it runs in the suite, so a broken link fails
the build rather than waiting for the next reader to trip over it.

The scope mirrors the ``path:line`` lock exactly — every markdown file
under ``docs/``, plus ``README.md`` and ``AGENTS.md`` at the repository
root, excluding ``docs/agent-ops/`` (the run trail, allowed to have
rotted) and ``docs/plans/`` (dated proposals that discuss references as
findings). The exclusion reasons are stated below, and a test pins the
constants against the ``path:line`` lock's own, so the two scopes cannot
drift apart silently. Fenced code blocks are skipped: a shell or markdown
snippet inside a fence contains brackets and parentheses that are
examples, not links.

One allowlist exists, and it is a category, not a list of failures that
happen to pass today: the ADR index rows for decisions whose record body
is still to be written. ``docs/adr/README.md:66-73`` states that a row in
that index claims *a decision was taken*, not that a document exists, so
an index-only link that points at a not-yet-written body is a sanctioned
reference, and the file it names is expected to be absent. The allowlist
names exactly those 15 file names, scoped to that one index document, and
a test checks every entry against the tree — present on disk, or linked
from nowhere else, would make the entry stale and fail the lock.

What the lock cannot see: reference-style links (``[text][ref]`` with a
``[ref]: target`` definition), a link whose target exists but is the
wrong file for the sentence around it, and a heading that exists under a
different spelling than the fragment a reader would guess. The first is
not in the corpus today (no reference definitions exist in scope); the
latter two are human reading, exactly as with the ``path:line`` lock.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

#: The repository root, derived from this file's own location; the suite
#: runs from wherever pytest was started.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The directories whose markdown the lock covers, repository-relative.
SCOPE_DIRECTORIES: tuple[str, ...] = ("docs",)

#: The individual markdown files the lock covers, repository-relative.
SCOPE_FILES: tuple[str, ...] = ("README.md", "AGENTS.md")

#: Directories excluded from the scope, and the reason for each exclusion.
#: Copied from the ``path:line`` lock by requirement (see the drift test
#: at the bottom of this file).
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

#: Directories that are never part of the tree the lock reasons about.
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

#: The one document whose index-only links are allowlisted.
ADR_INDEX: str = "docs/adr/README.md"

#: The 15 ADR index rows whose record body is not yet written. Every one
#: of these names a file that the index itself says does not exist yet
#: (``docs/adr/README.md:66-73``); a link to any other missing file from
#: that document is still a defect. Adding a 16th entry requires stating
#: why the row is index-only, the same way the ``path:line`` lock
#: requires a reason for an exclusion.
ALLOWLISTED_INDEX_ONLY_ADR_FILES: frozenset[str] = frozenset(
    {
        "0001-sqlite-over-postgres.md",
        "0002-embedded-vector-store.md",
        "0003-graph-engine.md",
        "0004-content-addressed-cold-lake.md",
        "0005-durable-queue-over-redis.md",
        "0006-sandbox-without-gvisor.md",
        "0007-model-provider-abstraction.md",
        "0009-peak-rss-not-integral.md",
        "0011-optional-router.md",
        "0012-attention-score-division.md",
        "0013-hybrid-retrieval-decay.md",
        "0014-memory-domains-consolidated.md",
        "0016-friction-score-denominator.md",
        "0017-meta-learning-what-is-real.md",
        "0018-mediated-delegation.md",
    }
)

#: A fenced code block; its contents are examples, not links.
_FENCE = re.compile(r"^\s*(?:```|~~~)")

#: An inline code span. A ``[text](target)`` whose opening bracket sits
#: inside one is example text, not a link.
_SPAN = re.compile(r"`([^`\n]+)`")

#: An ATX heading.
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*$")

#: A trailing run of ``#`` that closes an ATX heading.
_CLOSING_HASHES = re.compile(r"\s+#+\s*$")

#: A markdown link tail: ``]`` then ``(``. The destination is parsed
#: from there, respecting ``<...>`` destinations and nested parentheses.
_LINK_TAIL = re.compile(r"\]\(")

#: An inline markdown link or image in ``[text](url)`` form, used only
#: to locate candidates whose destination then gets parsed properly.
_CANDIDATE = re.compile(r"!?\[[^\]]*\]\(")

#: An external destination: a URI scheme, protocol-relative, or mailto.
_EXTERNAL = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//|^[A-Za-z][A-Za-z0-9+.-]*:")

#: Decorations inside a heading that do not contribute to its slug.
_LINK_IN_HEADING = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")

#: Anything that is not a word character, a space, or a hyphen. GitHub's
#: slugger drops exactly these (punctuation, symbols, slashes).
_NON_SLUG = re.compile(r"[^\w\s-]", re.UNICODE)


@dataclass(frozen=True)
class Link:
    """One inline markdown link found in a document.

    :param document: the markdown file the link is in, repository-relative.
    :param document_line: the 1-based line of the markdown that carries it.
    :param destination: the link destination as written, with the
        surrounding ``<>`` removed but otherwise verbatim.
    """

    document: str
    document_line: int
    destination: str

    @property
    def quoted(self) -> str:
        """The link as a reader would write it: ``[text](destination)``."""
        return f"[{self.document}:{self.document_line}]({self.destination})"


def _is_excluded(relative: str) -> bool:
    """Whether a repository-relative path is inside an excluded directory."""
    return any(
        relative == excluded or relative.startswith(f"{excluded}/")
        for excluded in EXCLUDED_DIRECTORIES
    )


def scope_files(root: Path = REPO_ROOT) -> list[Path]:
    """Every markdown file in the lock's scope, in a stable order.

    Mirrors ``test_docs_references.scope_files``: the same directories,
    the same root files, the same exclusions.
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


def _parse_destination(line: str, tail: int) -> str | None:
    """Parse the destination that follows a ``](`` at ``tail`` in ``line``.

    ``tail`` is the index just past the ``(``. Whitespace is skipped.
    A destination inside ``<...>`` may contain spaces and parentheses and
    ends at the matching ``>``; any other destination ends at the first
    whitespace (a trailing ``"title"`` is ignored) or at the closing
    parenthesis, with nested parentheses balanced so a file name that
    itself contains parentheses still parses. Percent-escapes and case
    are preserved — decoding is the caller's job.

    :return: the destination, or ``None`` when the parenthesis never
        closes on this line (the link is malformed; the caller reports
        it rather than guessing).
    """
    index = tail
    while index < len(line) and line[index] in " \t":
        index += 1
    if index < len(line) and line[index] == "<":
        end = line.find(">", index + 1)
        if end == -1:
            return None
        return line[index + 1 : end]
    depth = 1
    index0 = index
    while index < len(line):
        char = line[index]
        if char in " \t":
            break
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return line[index0:index]
        index += 1
    if index == index0 or depth != 1:
        return None
    destination = line[index0:index]
    # A title follows the destination; the link still needs its closing
    # parenthesis somewhere after the title on this line.
    if ")" not in line[index:]:
        return None
    return destination


def find_links(text: str) -> list[tuple[int, str]]:
    """Extract every inline ``[text](target)`` destination from ``text``.

    Fenced code blocks are skipped. A candidate whose opening bracket is
    inside an inline code span is example text and is skipped too; a
    candidate like ``[`docs/VISION.md`](docs/VISION.md)`` is kept — the
    bracket is outside the span, only the link text inside it is
    backticked.

    :return: a list of ``(line number, destination)`` tuples.
    """
    found: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        spans = [(match.start(), match.end()) for match in _SPAN.finditer(line)]
        for match in _CANDIDATE.finditer(line):
            tail = _LINK_TAIL.search(line, match.start())
            if tail is None:
                continue
            if any(start <= match.start() < end for start, end in spans):
                continue
            destination = _parse_destination(line, tail.end())
            if destination is None:
                found.append((number, "<unparseable-link>"))
            else:
                found.append((number, destination))
    return found


def slugify_heading(text: str) -> str:
    """The GitHub-style anchor slug for a heading's text.

    The text is stripped of link/image decoration and HTML tags, with
    backticks and emphasis markers removed by the slug rule itself, then
    lowercased, with every character that is not a word character, a
    space, or a hyphen dropped, and each space replaced by a hyphen.
    """
    text = _LINK_IN_HEADING.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    text = text.strip().lower()
    text = _NON_SLUG.sub("", text)
    return re.sub(r"[ \t]", "-", text)


def heading_anchors(text: str) -> set[str]:
    """The set of anchor slugs a markdown document exposes.

    Every ATX heading contributes its slug; a repeated heading also
    contributes the GitHub deduplicated forms (``name-1``, ``name-2``),
    so a link to the second ``Creation`` heading can be written and is
    checked. Setext headings (text underlined with ``===`` or ``---``)
    are included the same way.
    """
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    lines = text.splitlines()
    in_fence = False

    def record(raw: str) -> None:
        slug = slugify_heading(raw)
        if not slug:
            return
        seen = counts.get(slug, 0)
        anchors.add(slug if seen == 0 else f"{slug}-{seen}")
        counts[slug] = seen + 1

    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _HEADING.match(line)
        if heading:
            record(_CLOSING_HASHES.sub("", heading.group(2)))
            continue
        if index + 1 < len(lines):
            underline = lines[index + 1].strip()
            if underline and set(underline) in ({"="}, {"-"}):
                stripped = line.strip()
                if (
                    stripped
                    and not stripped.startswith(("#", ">", "-", "*", "|"))
                    and not _FENCE.match(line)
                ):
                    record(stripped)
    return anchors


def split_destination(destination: str) -> tuple[str, str]:
    """Split a destination into its path part and its fragment."""
    destination = destination.strip()
    path, _, fragment = destination.partition("#")
    return path, fragment


def is_external(path: str) -> bool:
    """Whether a destination path is outside the repository (or a scheme)."""
    if not path:
        return False
    if path.startswith("//"):
        return True
    return bool(_EXTERNAL.match(path))


def _anchors_for(target: Path, cache: dict[str, set[str]]) -> set[str]:
    """The anchor slugs of a markdown file, cached by path."""
    key = str(target)
    cached = cache.get(key)
    if cached is not None:
        return cached
    anchors = heading_anchors(target.read_text(encoding="utf-8", errors="replace"))
    cache[key] = anchors
    return anchors


def resolve(document: Path, path: str, root: Path) -> Path | None:
    """Resolve a link path to the filesystem entry it names, if any.

    An empty path names the linking document itself. A leading ``/``
    is repository-root absolute. Anything else resolves first relative
    to the linking document's directory, then relative to the
    repository root — the two spellings documents in this tree actually
    use. The result is the entry that exists, or ``None``.
    """
    if not path:
        return document
    decoded = urllib.parse.unquote(path)
    candidates: list[Path] = []
    if decoded.startswith("/"):
        candidates.append(root / decoded[1:])
    else:
        candidates.append(document.parent / decoded)
        candidates.append(root / decoded)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _is_allowlisted(document: str, path: str) -> bool:
    """Whether a missing target is a sanctioned index-only ADR body.

    The allowlist is scoped to the ADR index document and to bare file
    names: an allowlisted name reachable through any other document, or
    with a fragment attached, is not covered — the name alone is the
    sanctioned reference, and the body it names is what is still to be
    written.
    """
    if document != ADR_INDEX or not path:
        return False
    if "/" in path:
        return False
    return urllib.parse.unquote(path) in ALLOWLISTED_INDEX_ONLY_ADR_FILES


def check_link(
    link: Link,
    document_path: Path,
    root: Path = REPO_ROOT,
    cache: dict[str, set[str]] | None = None,
) -> str | None:
    """Check one link against the tree.

    :return: a description of the defect, or ``None`` when the link holds.
    """
    if cache is None:
        cache = {}
    path, fragment = split_destination(link.destination)
    if is_external(path):
        return None
    if link.destination == "<unparseable-link>":
        return (
            f"{link.quoted} — the link parenthesis never closes on the line; "
            "the destination cannot be parsed"
        )
    allowlisted = _is_allowlisted(link.document, path) and not fragment
    target = resolve(document_path, path, root)
    if target is None:
        if allowlisted:
            return None
        return f"{link.quoted} — no such file or directory in the repository"
    if fragment:
        decoded_fragment = urllib.parse.unquote(fragment).strip().lower()
        if target.is_dir():
            return (
                f"{link.quoted} — the target is the directory "
                f"{target.relative_to(root).as_posix()!r}, which has no headings"
            )
        if target.suffix.lower() != ".md":
            return (
                f"{link.quoted} — the target "
                f"{target.relative_to(root).as_posix()!r} is not a markdown "
                "file, so the anchor names nothing"
            )
        if decoded_fragment not in _anchors_for(target, cache):
            return (
                f"{link.quoted} — no heading in "
                f"{target.relative_to(root).as_posix()} has the anchor "
                f"{decoded_fragment!r}"
            )
    return None


def all_links(root: Path = REPO_ROOT) -> list[Link]:
    """Every link in the lock's scope.

    :return: the links, in document and line order.
    """
    found: list[Link] = []
    for document in scope_files(root):
        relative = document.relative_to(root).as_posix()
        text = document.read_text(encoding="utf-8")
        for number, destination in find_links(text):
            found.append(Link(relative, number, destination))
    return found


def _violations(links: list[Link], root: Path = REPO_ROOT) -> list[str]:
    cache: dict[str, set[str]] = {}
    defects: list[str] = []
    scope = {document.relative_to(root).as_posix(): document for document in scope_files(root)}
    for link in links:
        document_path = scope.get(link.document)
        if document_path is None:
            document_path = root / link.document
        defect = check_link(link, document_path, root=root, cache=cache)
        if defect is not None:
            defects.append(defect)
    return defects


def test_every_relative_link_names_an_existing_path() -> None:
    """Every link destination resolves to a file or a directory."""
    defects: list[str] = []
    for link in all_links():
        path, _fragment = split_destination(link.destination)
        if is_external(path) or link.destination == "<unparseable-link>":
            continue
        document_path = REPO_ROOT / link.document
        if resolve(document_path, path, REPO_ROOT) is None:
            if _is_allowlisted(link.document, path):
                continue
            defects.append(
                f"{link.quoted} — no such file or directory in the repository"
            )
    assert not defects, "broken links:\n" + "\n".join(defects)


def test_every_anchor_names_a_heading() -> None:
    """Every ``#fragment`` is the slug of a heading in its target file."""
    cache: dict[str, set[str]] = {}
    defects: list[str] = []
    for link in all_links():
        path, fragment = split_destination(link.destination)
        if not fragment or is_external(path):
            continue
        document_path = REPO_ROOT / link.document
        target = resolve(document_path, path, REPO_ROOT)
        if target is None:
            continue
        defect = check_link(link, document_path, root=REPO_ROOT, cache=cache)
        if defect is not None:
            defects.append(defect)
    assert not defects, "broken anchors:\n" + "\n".join(defects)


def test_the_lock_is_not_vacuous() -> None:
    """The lock finds links at all, and finds the ones it should."""
    links = all_links()
    assert len(links) > 200, f"the lock found only {len(links)} links"
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in scope_files()}
    assert "README.md" in scanned
    assert "AGENTS.md" in scanned
    documents = {link.document for link in links}
    assert not any(document.startswith("docs/agent-ops/") for document in documents)
    assert not any(document.startswith("docs/plans/") for document in documents)
    assert any(
        link.document == "README.md" and link.destination.startswith("docs/VISION.md")
        for link in links
    )
    assert any(link.document == "docs/VISION.md" for link in links)


def test_the_lock_catches_a_broken_link(tmp_path: Path) -> None:
    """A link that rots inside the scope turns the lock red.

    Mirrors the ``path:line`` lock's self-test: build a synthetic tree —
    the shape of a small repository, with a document that links to a file
    and to a heading — and break the link three ways: the file is
    missing, the anchor names no heading, and a same-document anchor is
    wrong. The allowlisted ADR name in the ADR index is the one missing
    target that must *not* be reported, and the healthy links must not
    be reported either.
    """
    root = tmp_path / "repo"
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "README.md").write_text(
        "# Home\n\n"
        "See [the guide](docs/guide.md#the-guide).\n"
        "See [the missing file](docs/missing.md).\n"
        "See [the missing heading](docs/guide.md#no-such-heading).\n"
        "See [the wrong self anchor](#no-such-heading-on-this-page).\n",
        encoding="utf-8",
    )
    (root / "docs" / "guide.md").write_text(
        "# Guide\n\n## The guide\n\nBody.\n", encoding="utf-8"
    )
    (root / "docs" / "adr" / "README.md").write_text(
        "# ADRs\n\n[0001](0001-sqlite-over-postgres.md)\n", encoding="utf-8"
    )
    links = all_links(root)
    assert len(links) == 5, f"expected five links, found {len(links)}: {links}"
    defects = _violations(links, root=root)
    joined = "\n".join(defects)
    assert "no such file or directory" in joined, joined
    assert "docs/missing.md" in joined, joined
    assert "no heading" in joined, joined
    assert "no-such-heading" in joined, joined
    # The healthy links and the sanctioned index-only row are not reported.
    assert "docs/guide.md#the-guide" not in joined, joined
    assert "0001-sqlite-over-postgres.md" not in joined, joined
    assert len(defects) == 3, joined


def test_allowlist_only_covers_genuinely_missing_adr_bodies() -> None:
    """Every allowlisted name is missing on disk and linked from the index."""
    assert len(ALLOWLISTED_INDEX_ONLY_ADR_FILES) == 15
    index_text = (REPO_ROOT / ADR_INDEX).read_text(encoding="utf-8")
    linked = {
        urllib.parse.unquote(split_destination(destination)[0])
        for _number, destination in find_links(index_text)
    }
    for name in ALLOWLISTED_INDEX_ONLY_ADR_FILES:
        assert not (REPO_ROOT / "docs" / "adr" / name).exists(), name
        assert name in linked, name
    missing = {
        urllib.parse.unquote(split_destination(destination)[0])
        for _number, destination in find_links(index_text)
        if not (REPO_ROOT / "docs" / "adr" / urllib.parse.unquote(
            split_destination(destination)[0]
        )).exists()
    }
    assert missing == ALLOWLISTED_INDEX_ONLY_ADR_FILES, missing


def test_scope_mirrors_the_docs_references_lock() -> None:
    """The scope constants match the ``path:line`` lock's, verbatim.

    Loaded by file path rather than by import: pytest's import mode
    makes a sibling test-module import fragile, and a scope that cannot
    be compared cannot be kept in step.
    """
    spec = importlib.util.spec_from_file_location(
        "test_docs_references_lock",
        REPO_ROOT / "tests" / "conventions" / "test_docs_references.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    assert SCOPE_DIRECTORIES == module.SCOPE_DIRECTORIES
    assert SCOPE_FILES == module.SCOPE_FILES
    assert EXCLUDED_DIRECTORIES == module.EXCLUDED_DIRECTORIES
    assert IGNORED_DIRECTORIES == module.IGNORED_DIRECTORIES
    assert REPO_ROOT == module.REPO_ROOT


def test_heading_slugs_follow_the_github_rule() -> None:
    """Slugs match GitHub's: punctuation dropped, spaces to hyphens."""
    assert slugify_heading("Validation and kill criteria") == "validation-and-kill-criteria"
    assert slugify_heading("Appendix: Temporal and spatial composability for PAA") == (
        "appendix-temporal-and-spatial-composability-for-paa"
    )
    assert slugify_heading("Enabling gVisor (`runsc`)") == "enabling-gvisor-runsc"
    assert slugify_heading(
        "What was deliberately *not* changed"
    ) == "what-was-deliberately-not-changed"
    assert slugify_heading("The one thing — until & now!") == "the-one-thing--until--now"
    anchors = heading_anchors("# Title\n\n## Creation\n\n## Creation\n")
    assert anchors == {"title", "creation", "creation-1"}, anchors


def test_destination_parsing_handles_angles_and_titles() -> None:
    """``<...>`` destinations keep spaces; plain ones stop at a title."""
    assert _parse_destination("[x](<plans and audits/a.md>)", 4) == (
        "plans and audits/a.md"
    )
    assert _parse_destination('[x](guide.md "a title")', 4) == "guide.md"
    assert find_links("See [the doc](<a b.md>) and `[not a link](missing.md)`.\n") == [
        (1, "a b.md")
    ]
