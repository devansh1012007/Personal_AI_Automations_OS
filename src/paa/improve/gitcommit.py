"""The guarded git commit — host-side git behind the integrity gate.

C8.1's fifth stage lands the approved proposal as a git commit, and
host-side git is exactly the surface the coder's integrity gate exists
for (ADR-0047): the repository's own config can name programs git would
execute with the host's privileges, and a workspace the loop was pointed
at is as attacker-writable as the coder's. **This module does not bypass
that gate** — every host-side git call it makes happens after
:func:`paa.coder.git_integrity.repo_config_gate` has probed the
repository's local config against the deny table and scanned the root
attributes files for ``filter=``. The gate and the jail helpers
(``assert_inside_workspace`` / ``is_protected``) are imported from the
coder package's public surface; the hardened runner itself is
**copied, not imported**, because ``coder/git_tools.py``'s frozen public
surface (ADR-0045) is its four tools and the runner is not on it — the
same stated-reason copy ``coder/workspace.py`` makes of the patch
module's control-character class.

The runner keeps the coder's discipline, restated:

* argv built **in code only** — stage, commit and rev-parse are the
  literal tuples below, so no proposal content can smuggle a git option
  (the commit message always travels as the value of ``-m``, a fixed
  pair, never ``--amend``/editor);
* ``--no-pager`` and ``-c core.fsmonitor=false`` on every invocation, so
  no pager or fsync program from repo config executes;
* ``-c core.hooksPath=`` (empty) and ``--no-gpg-sign`` on the commit, so
  no hook or signing program runs — the D-7 mechanisms;
* a child environment built from an allowlist plus
  ``GIT_TERMINAL_PROMPT=0`` and ``GIT_CONFIG_NOSYSTEM=1``, so no
  ambient ``GIT_*`` injection reaches the child;
* a hard timeout, and capped-and-drained pipe reads so a chatty git
  cannot deadlock;
* ``git`` itself resolved once via ``shutil.which`` to an absolute path,
  so a mid-run ``PATH`` change cannot swap the binary.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from paa.coder.git_integrity import repo_config_gate
from paa.coder.workspace import Workspace, assert_inside_workspace, is_protected
from paa.core.errors import PaaError

__all__ = [
    "GitCommitResult",
    "guarded_git_commit",
    "is_git_repository",
]

#: The child-environment passthrough (the coder's set, restated): PATH to
#: find git's helpers, the home variables git needs for global config and
#: its trust database, and the Windows/TEMP basics. Everything else —
#: ambient secrets included — is dropped.
_ENV_PASSTHROUGH: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
    }
)

#: Forced child variables: no credential prompt (no network operations
#: exist here to authenticate, and a prompt would hang a headless run)
#: and no system config (defence in depth behind the integrity gate).
_ENV_FORCED: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
}

#: One git call's wall budget. Staging and committing named files is
#: fast; anything past this is a hung helper, and the loop stops.
_GIT_TIMEOUT_S = 60

#: Stored cap per stream (drained past the cap — see the module docstring).
_GIT_OUTPUT_CAP = 64 * 1024

#: The author-identity keys a commit needs, probed before anything is
#: staged so the refusal names the remedy instead of dying mid-commit
#: (the coder's git-config gate rule).
_IDENTITY_KEYS: tuple[str, ...] = ("user.name", "user.email")


@dataclass(frozen=True, slots=True)
class GitOutcome:
    """One finished git invocation, capped and decoded."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


@dataclass(frozen=True, slots=True)
class GitCommitResult:
    """What a guarded commit produced."""

    head: str
    staged: tuple[str, ...]


def _child_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _ENV_PASSTHROUGH}
    env.update(_ENV_FORCED)
    return env


async def _read_capped(stream: asyncio.StreamReader | None) -> str:
    """Read a pipe storing at most the cap, draining the rest."""
    if stream is None:
        return ""
    chunks: list[bytes] = []
    stored = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if stored < _GIT_OUTPUT_CAP:
            room = _GIT_OUTPUT_CAP - stored
            chunks.append(chunk[:room])
            stored += min(len(chunk), room)
    return b"".join(chunks).decode("utf-8", errors="replace")


async def _run_git(argv: tuple[str, ...], *, cwd: Path) -> GitOutcome:
    """Run one fixed git argv under the caps.

    The coder's argv discipline, adopted verbatim so the integrity
    gate's probe runner contract is satisfied unchanged: callers pass
    ``("git", "--no-pager", ...)`` literals only, the prefix is enforced,
    and the forced ``-c core.fsmonitor=false`` is inserted once, here,
    where nothing a proposal carried can reach it. The executable name is
    resolved to an absolute path before the spawn (``shutil.which``, once
    per call), so the child's ``PATH`` is never asked to find git and a
    mid-run ``PATH`` change cannot swap the binary. ``stdin`` is closed:
    no git subcommand here reads it, and a credential prompt must fail
    fast rather than hang a headless run.
    """
    if argv[:2] != ("git", "--no-pager"):
        raise PaaError("internal: fixed git argv discipline violated")
    git = shutil.which("git")
    if git is None:
        raise PaaError("git is not available on this host; the guarded loop cannot commit")
    forced_argv = (git, "--no-pager", "-c", "core.fsmonitor=false", *argv[2:])
    try:
        proc = await asyncio.create_subprocess_exec(
            *forced_argv,
            cwd=str(cwd),
            env=_child_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise PaaError(f"git could not be started: {exc}") from exc
    stdout_task = asyncio.create_task(_read_capped(proc.stdout))
    stderr_task = asyncio.create_task(_read_capped(proc.stderr))
    timed_out = False
    exit_code: int
    try:
        exit_code = await asyncio.wait_for(proc.wait(), timeout=_GIT_TIMEOUT_S)
    except TimeoutError:
        timed_out = True
        proc.kill()
        exit_code = await proc.wait()
    stdout = await stdout_task
    stderr = await stderr_task
    return GitOutcome(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out)


async def is_git_repository(root: Path) -> bool:
    """Whether ``root`` is itself the top of a git working tree.

    A subdirectory of someone else's repository is **not** a repository
    this loop may commit into — git would walk up and write into a repo
    the loop never named — so the check is ``show-toplevel == root``,
    the coder's jail rule, not merely ``rev-parse`` succeeding.
    """
    root = Path(root).resolve()
    outcome = await _run_git(("git", "--no-pager", "rev-parse", "--show-toplevel"), cwd=root)
    return outcome.exit_code == 0 and outcome.stdout.strip() == str(root)


async def _require_identity(root: Path) -> None:
    """Refuse before staging when the host cannot author a commit."""
    missing: list[str] = []
    for key in _IDENTITY_KEYS:
        outcome = await _run_git(("git", "--no-pager", "config", "--get", key), cwd=root)
        if outcome.exit_code != 0 or not outcome.stdout.strip():
            missing.append(key)
    if missing:
        raise PaaError(
            f"git cannot author a commit here: {', '.join(missing)} is not "
            "configured — set it (git config user.name / user.email) before "
            "the guarded loop commits; a commit misattributed to a "
            "fabricated author is a lie in the record"
        )


async def guarded_git_commit(
    repo_root: Path, paths: list[str], message: str
) -> GitCommitResult:
    """Stage and commit ``paths`` in ``repo_root``, behind the integrity gate.

    Every call sequence this function makes: the integrity gate's probe
    first, then the identity probe, then stage, commit, and the new HEAD
    — all with fixed argv, all under the hardened child environment. The
    paths must be named files (no directories, no glob characters: git
    expands both recursively and the jail would have approved nothing),
    jailed inside the repository and not protected — the same frozen
    order the coder's stage tool applies. The refusal names the path and
    the reason; it never quotes file content.
    """
    root = Path(repo_root).resolve()
    workspace = Workspace(root=root)

    # 1. the integrity gate (ADR-0047) — never bypassed, always first.
    await repo_config_gate(root, _run_git)

    # 2. the repo-root jail: a worktree root that is someone else's
    #    subdirectory would commit into a repo the loop never named.
    toplevel = await _run_git(("git", "--no-pager", "rev-parse", "--show-toplevel"), cwd=root)
    if toplevel.exit_code != 0 or toplevel.stdout.strip() != str(root):
        raise PaaError(
            f"{repo_root} is not the top of a git working tree; the guarded "
            "loop refuses to commit into a repository it was not pointed at"
        )

    # 3. the identity gate — before anything is staged.
    await _require_identity(root)

    # 4. the path jail: named files only, inside the repo, not protected.
    relative: list[str] = []
    for raw in paths:
        if not raw or any(char in raw for char in "*?["):
            raise PaaError(
                f"staging path {raw!r} is not a named file; directories and "
                "glob characters are refused because git would expand them "
                "to files the loop never approved"
            )
        candidate = Path(raw)
        if candidate.is_absolute():
            raise PaaError(f"staging path {raw!r} must be relative to the repository root")
        resolved = assert_inside_workspace(workspace, root / candidate)
        protected = is_protected(workspace, resolved)
        if protected is not None:
            raise PaaError(
                f"refusing to stage {raw!r}: {protected}; the guarded loop "
                "writes neither the git directory nor environment files"
            )
        relative.append(resolved.relative_to(root).as_posix())

    # 5. stage, then commit, then read the new HEAD — all fixed argv.
    stage = await _run_git(("git", "--no-pager", "add", "--", *relative), cwd=root)
    if stage.exit_code != 0 or stage.timed_out:
        raise PaaError(
            f"git add failed (exit {stage.exit_code}); nothing was committed"
        )
    commit = await _run_git(
        (
            "git",
            "--no-pager",
            "-c",
            "core.hooksPath=",
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
        ),
        cwd=root,
    )
    if commit.exit_code != 0 or commit.timed_out:
        raise PaaError(
            "git commit failed; the working tree still carries the applied "
            f"change (exit {commit.exit_code})"
        )
    head = await _run_git(("git", "--no-pager", "rev-parse", "HEAD"), cwd=root)
    if head.exit_code != 0 or not head.stdout.strip():
        raise PaaError("git could not read the new HEAD after committing")
    return GitCommitResult(head=head.stdout.strip(), staged=tuple(relative))
