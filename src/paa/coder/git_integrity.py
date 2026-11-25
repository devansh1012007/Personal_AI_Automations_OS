"""Host-side git integrity gate (Wave 7-F task H-1, ADR-0047).

``git_status`` / ``git_diff`` / ``git_stage`` / ``git_commit`` run ``git`` on
the HOST, not in the sandbox. Git will happily run programs named in the
repository's own config — ``core.fsmonitor`` on the next status, a
``filter.<name>.clean`` driver on the next stage, ``diff.<driver>.command``
on any diff. The agent's ``bash`` can write ``.git/config`` (the
protected-path rule binds the fs tools, not bash), so that config is
attacker-writable input, and a program it names would execute with the
host's privileges. This module is the refusal: before any host-side git
call, the repository's *local* config is probed (read-only) against a deny
table, and the root attributes files are scanned for ``filter=``.

The probe (run by :func:`repo_config_gate`, invoked from
``git_tools._preflight``) is ``git config --local --list -z --no-includes``
under the same hardened child environment every other git call gets:
``-z`` makes the output machine-splittable, ``--no-includes`` stops
``include.*`` indirection from being *followed* while listing (the key
itself is denied as indirection), and ``--local`` keeps the operator's own
global filters (git-lfs and friends) out of scope — they are theirs,
outside what the workspace can write.

The refusal names the **key, never its value**: a value can carry anything
the adversary wrote, and an error payload that quotes it becomes a
reflection channel into the model's context.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from paa.core.errors import PaaError

#: Config keys (case-insensitive) that directly name a program git would
#: execute. The Wave 7-F H-1 table, plus two documented supersets kept from
#: the A1 gate lineage: ``diff.external`` (runs an arbitrary command on every
#: diff) and ``core.attributesfile`` (redirects the attributes scan to a file
#: this gate does not read). See ADR-0047.
_DENY_EXACT: frozenset[str] = frozenset(
    {
        "core.fsmonitor",
        "core.hookspath",
        "core.sshcommand",
        "core.editor",
        "core.pager",
        "core.askpass",
        "gpg.program",
        "diff.external",
        "core.attributesfile",
    }
)

#: Key prefixes whose whole class is refused: credential helpers execute on
#: every auth path, aliases smuggle verbs, includes and protocols redirect
#: where config comes from. ``protocol.*`` is denied wholesale per the H-1
#: table (it includes the benign ``protocol.version``; the tool surface has
#: no network git operations, so nothing functional is lost).
_DENY_PREFIXES: tuple[str, ...] = (
    "credential.",
    "alias.",
    "include.",
    "includeif.",
    "protocol.",
)

#: Any key whose last dot-segment is one of these is refused, so a
#: program-naming key git adds in a future version is denied **by default**
#: rather than waiting for a table update. Every real git key ending in one
#: of these segments names something executable or loadable.
_DENY_LAST_SEGMENT: frozenset[str] = frozenset(
    {
        "command",
        "driver",
        "helper",
        "program",
        "textconv",
        "clean",
        "smudge",
        "process",
    }
)

#: ``url.<anything>.insteadOf`` / ``pushInsteadOf`` rewrite where a remote
#: URL points — indirection under attacker-controlled spelling.
_URL_DENY_SUFFIXES: frozenset[str] = frozenset({"insteadof", "pushinsteadof"})

#: How many offending keys the refusal lists before switching to a count.
#: The message must stay bounded whatever the adversary wrote.
_MAX_REPORTED_KEYS = 5


class _ProbeResult(Protocol):
    """The shape the injected hardened runner returns (``_GitResult`` —
    frozen, so the protocol's members are read-only properties to match)."""

    @property
    def exit_code(self) -> int: ...

    @property
    def stdout(self) -> str: ...

    @property
    def timed_out(self) -> bool: ...


#: The hardened runner from ``git_tools`` (injected to avoid an import
#: cycle; the argv is built here and executed under the child environment
#: ``_run_git`` enforces).
_ProbeRunner = Callable[..., Awaitable[_ProbeResult]]


def classify_config_key(key: str) -> str | None:
    """Classify one config key; the denial class, or ``None`` when benign.

    Matching is case-insensitive (git sections and key names are), applied to
    the whole key — safe in the refusing direction: a benign key whose
    lowercase spelling collides with a deny pattern does not exist in git's
    vocabulary, and a hostile spelling that collides with nothing is exactly
    what the last-segment rule exists to catch.
    """
    lowered = key.strip().lower()
    if not lowered:
        return None
    if lowered in _DENY_EXACT:
        return "program-running"
    for prefix in _DENY_PREFIXES:
        if lowered.startswith(prefix):
            return "indirection"
    last_segment = lowered.rsplit(".", 1)[-1] if "." in lowered else ""
    if last_segment in _DENY_LAST_SEGMENT:
        return "program-running"
    if lowered.startswith("url.") and last_segment in _URL_DENY_SUFFIXES:
        return "indirection"
    return None


def attributes_refusal(ws_root: Path) -> str | None:
    """The ``filter=`` scan of the root attributes files.

    Checks ``<root>/.gitattributes`` and ``<root>/.git/info/attributes`` —
    the two files that apply to the whole tree. A subdirectory
    ``.gitattributes`` can only *select* a driver; the driver's program is
    defined in config, which :func:`repo_config_gate` denies, so the root
    scan is defence in depth rather than the only barrier (ADR-0047).
    Returns a bounded human refusal naming file and line, or ``None``.
    """
    candidates = (ws_root / ".gitattributes", ws_root / ".git" / "info" / "attributes")
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Absent or unreadable: git cannot run a filter from a file it
            # cannot read, so this is not evidence of a driver.
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            body = line.split("#", 1)[0].strip()
            if not body:
                continue
            for attr in body.split()[1:]:  # [0] is the pattern or [attr]macro
                if attr == "filter" or attr.startswith("filter="):
                    return f"{path.name}:{lineno} sets a filter attribute"
    return None


async def repo_config_gate(ws_root: Path, run_git: _ProbeRunner) -> None:
    """Refuse host-side git when repo config or attributes name programs.

    Raises :class:`~paa.core.errors.PaaError` naming the offending key(s) —
    never their values. ``git_tools._preflight`` catches the raise and hands
    the model the refusal as an ``{"error": ...}`` value (the
    ``runtime_tools.py`` convention), so the tamper answer stays a raised,
    named, explicit no for direct callers while the loop-level contract
    ("errors as values") survives.

    Exit-code semantics of the probe: 0 = listed (possibly empty), 1 = no
    matches (tolerated for symmetry with ``--get``), anything else = a
    config git cannot even read — including a deleted ``.git/config`` —
    which is itself a workspace that cannot be shown safe, and is refused.
    """
    try:
        result = await run_git(
            (
                "git",
                "--no-pager",
                "config",
                "--local",
                "--list",
                "-z",
                "--no-includes",
            ),
            cwd=ws_root,
        )
    except OSError as exc:
        raise PaaError(f"git executable could not be started: {exc}") from exc
    if result.timed_out:
        raise PaaError("repo config probe timed out")
    if result.exit_code not in (0, 1):
        raise PaaError(
            "refusing host-side git: the repository config could not be "
            f"probed (exit {result.exit_code}) — a workspace whose config "
            "cannot be read cannot be shown safe"
        )
    denied: list[str] = []
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        key = entry.split("\n", 1)[0]  # `-z` entries are "key\nvalue"
        if classify_config_key(key) is not None:
            denied.append(key)
    if denied:
        shown = ", ".join(denied[:_MAX_REPORTED_KEYS])
        overflow = len(denied) - _MAX_REPORTED_KEYS
        more = f" (+{overflow} more)" if overflow > 0 else ""
        raise PaaError(
            "refusing host-side git: the repository's own config names "
            f"programs or indirection ({shown}{more}) that git would "
            "execute or follow on the host — remove them from .git/config; "
            "they could have been written from inside the workspace"
        )
    refusal = attributes_refusal(ws_root)
    if refusal is not None:
        raise PaaError(
            f"refusing host-side git: {refusal} — filter drivers run on the "
            "host; remove it from the attributes file"
        )
