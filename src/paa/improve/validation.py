"""Deterministic validation — the proposal proven against a scratch copy.

C8.1's third stage, and the stage the whole design turns on: **no gate is
ever model-decided** (SPEC 2 §9 rule 2). The proposal is applied to a
scratch copy of the repository and the repository's *own* gate commands —
ruff, mypy, pytest, whatever the operator names — run as plain
subprocesses. A command's exit code is the verdict; nothing interprets
the output, nothing scores it, no model is consulted.

Three properties the implementation holds:

* **The real tree is never touched.** Validation copies the repository
  (skipping ``.git`` and the usual build/cache directories) into a
  scratch root, applies the proposal there, and runs the commands with
  the scratch copy as their working directory. A failing gate leaves the
  operator's checkout exactly as it was.
* **A command's output never enters the ledger.** Each stream is read
  with a store-cap-and-drain reader (the sandbox backend's asymmetry: a
  chatty child fills neither RAM nor the pipe), and the journal carries
  only its SHA-256 and byte size — the same opaque-secret rule the call
  journal applies to prompts. The full (capped) text lives in the
  evidence file the attested bundle later carries as a blob.
* **Zero commands is a refusal, not a pass.** A loop that validated
  nothing has not validated; the verdict fails with a named reason
  rather than green-washing an empty run — an improvement without
  validation is exactly what the guarded loop exists to prevent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.core.errors import PaaError
from paa.improve.proposal import PatchProposal

__all__ = [
    "CommandOutcome",
    "ValidationVerdict",
    "run_validation",
]

#: Directories never copied into the scratch tree: version-control state
#: (validation must not see or touch history), and build/caches whose
#: contents are derived and can be huge.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)

#: Per-stream storage cap for a gate command's output. The digest is
#: always over the *full* stream (drained past the cap); only the stored
#: text is capped, so the attested evidence is bounded while the checksum
#: still commits to everything the command printed.
_OUTPUT_CAP_BYTES = 256 * 1024

#: Chunk size for the capped reader.
_READ_CHUNK = 65536


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """One gate command's fate, with attestable digests and no full text.

    ``stdout``/``stderr`` are the capped texts for the human gate to read
    and the evidence file to carry; the ledger only ever sees
    ``stdout_sha256``/``stderr_sha256`` and the byte sizes.
    """

    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    duration_ms: float
    stdout: str
    stderr: str
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    truncated: bool

    @property
    def green(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_json(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": round(self.duration_ms, 3),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ValidationVerdict:
    """The whole validation stage's outcome: the commands, and the rule.

    ``passed`` is exit-code-0-and-not-timed-out for every command, with at
    least one command — the refusal the module docstring states. The rule
    is deterministic by construction; there is no score to read and
    nothing to interpret.
    """

    commands: tuple[CommandOutcome, ...] = ()
    scratch_root: str = ""
    reason: str = ""

    @property
    def passed(self) -> bool:
        if not self.commands:
            return False
        return all(command.green for command in self.commands)

    @property
    def failed_commands(self) -> tuple[CommandOutcome, ...]:
        return tuple(command for command in self.commands if not command.green)

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "scratch_root": self.scratch_root,
            "commands": [command.to_json() for command in self.commands],
        }


async def _read_capped(
    stream: asyncio.StreamReader | None,
) -> tuple[bytes, str, int, bool]:
    """Read a pipe storing at most the cap, hashing everything it read.

    Returns ``(stored, sha256_of_full_stream, total_len, truncated)`` —
    the digest and length commit to the *full* stream, the stored bytes
    only to the cap, and the reader keeps draining past the cap so a
    verbose child cannot deadlock on a full pipe (the sandbox backend's
    asymmetry, restated).
    """
    if stream is None:
        return b"", hashlib.sha256(b"").hexdigest(), 0, False
    chunks: list[bytes] = []
    hasher = hashlib.sha256()
    stored = 0
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        hasher.update(chunk)
        total += len(chunk)
        if stored < _OUTPUT_CAP_BYTES:
            room = _OUTPUT_CAP_BYTES - stored
            chunks.append(chunk[:room])
            if len(chunk) > room:
                truncated = True
            stored += min(len(chunk), room)
        else:
            truncated = True
    return b"".join(chunks), hasher.hexdigest(), total, truncated


def copy_repo(repo_root: Path, dest: Path) -> Path:
    """Copy the repository into ``dest`` deterministically (no metadata).

    A plain content copy, walked in sorted order: the scratch tree is
    derived data that exists only for the duration of one validation, so
    permissions, mtimes and symlinks are not preserved — only files and
    directories. Skips :data:`_SKIP_DIRS` at any depth.
    """
    repo_root = Path(repo_root)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for source in sorted(repo_root.rglob("*")):
        relative = source.relative_to(repo_root)
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        if source.is_dir():
            continue  # created implicitly by file writes, in sorted order
        if source.is_symlink():
            continue  # a symlink is not content; the gates re-derive theirs
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return dest


async def _run_command(argv: Sequence[str], cwd: Path, timeout_s: int) -> CommandOutcome:
    """Run one gate command: fixed argv, no shell, capped reads, a clock."""
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # A gate command that cannot even start is a resolved red, with the
        # named reason — not a crash of the loop that ran it.
        reason = f"{type(exc).__name__}: {exc}"[:200]
        return CommandOutcome(
            argv=tuple(argv),
            exit_code=None,
            timed_out=False,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            stdout="",
            stderr=reason,
            stdout_sha256=hashlib.sha256(b"").hexdigest(),
            stderr_sha256=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            stdout_bytes=0,
            stderr_bytes=len(reason.encode("utf-8")),
            truncated=False,
        )
    stdout_task = asyncio.create_task(_read_capped(proc.stdout))
    stderr_task = asyncio.create_task(_read_capped(proc.stderr))
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        proc.kill()
        await proc.wait()
    out_stored, out_sha, out_len, out_trunc = await stdout_task
    err_stored, err_sha, err_len, err_trunc = await stderr_task
    return CommandOutcome(
        argv=tuple(argv),
        exit_code=None if timed_out else proc.returncode,
        timed_out=timed_out,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
        stdout=out_stored.decode("utf-8", errors="replace"),
        stderr=err_stored.decode("utf-8", errors="replace"),
        stdout_sha256=out_sha,
        stderr_sha256=err_sha,
        stdout_bytes=out_len,
        stderr_bytes=err_len,
        truncated=out_trunc or err_trunc,
    )


async def run_validation(
    proposal: PatchProposal,
    *,
    repo_root: Path,
    scratch_root: Path,
    commands: Sequence[Sequence[str]],
    timeout_s: int = 300,
    evidence_dir: Path | None = None,
) -> ValidationVerdict:
    """Validate one proposal against a scratch copy of ``repo_root``.

    The commands run with the scratch copy as their working directory and
    the ambient environment: they are **the operator's own gate commands**
    (typed at the CLI, never chosen by a model), so they get the toolchain
    the operator runs — unlike the sandbox's untrusted children, whose
    scrubbed environment exists precisely because their commands are not
    the operator's word. When ``evidence_dir`` is given, each command's
    full record (capped outputs included) is written there as JSON, ready
    to ride the attested bundle as an evidence blob.
    """
    repo_root = Path(repo_root)
    if not repo_root.is_dir():
        raise PaaError(f"the repository to improve was not found: {repo_root}")
    scratch = copy_repo(repo_root, Path(scratch_root) / "repo")
    proposal.apply(scratch)
    outcomes: list[CommandOutcome] = []
    for argv in commands:
        # A bare string is the one shape that slips through a
        # parts-are-strings check (iterating a str yields 1-char strs),
        # and it is exactly the mistake an operator makes typing one
        # command where a list of argv belongs — refuse it by name.
        if (
            isinstance(argv, str)
            or not isinstance(argv, Sequence)
            or not argv
            or not all(isinstance(part, str) for part in argv)
        ):
            raise PaaError(
                "every validation command must be a non-empty argv of "
                "strings (a list of lists, not a flat list)"
            )
        outcomes.append(await _run_command(argv, scratch, timeout_s))
    verdict = ValidationVerdict(
        commands=tuple(outcomes),
        scratch_root=str(scratch),
        reason=(
            "no validation commands were configured; an improvement "
            "without validation is exactly what the guarded loop exists "
            "to prevent"
            if not outcomes
            else ""
        ),
    )
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for index, outcome in enumerate(outcomes, start=1):
            payload = {
                "v": 1,
                "kind": "improve-validation/1",
                "command": outcome.to_json(),
            }
            path = evidence_dir / f"validation-{index:02d}.json"
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return verdict
