"""The observer core — run a CLI, watch honestly, classify the aftermath (C7.1).

``paa observe`` is the spec's T1 adapter: it runs an arbitrary CLI in a
workspace and records what it can *see* — the child's streams, the file
manifest's before/after diff, git's own view of the changes — without
claiming any control over the harness's loop. The containment story is
the sandbox's (:class:`~paa.sandbox.base.Sandbox`), the honesty story is
the bundle's (:mod:`paa.observe.bundle`), and this module is the
sequencing between them:

1. write the ``started`` skeleton (the crash marker — a bundle stuck in
   ``started`` *is* the evidence that nobody came back);
2. snapshot the manifest and git state **before**;
3. run the child under the sandbox's one-shot contract;
4. snapshot the manifest and git state **after**;
5. classify the state the run left behind and write the final bundle.

The classification is the crash discipline the spec demands of a
``resume = "none"`` adapter: name the state, never promise a resume.
A killed child is reported as killed with the workspace facts beside
it; nothing here re-runs the command, and nothing marks the lineage
recoverable — the ledger's recovery sweep already refuses to re-drive
pipeline-only lineages (:attr:`paa.ledger.recovery.RecoveryOutcome.SKIPPED_PIPELINE_LINEAGE`),
which is the structural half of that promise.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from paa.core.errors import SandboxError
from paa.observe.bundle import (
    EVIDENCE_TIER,
    GitChanges,
    ManifestDiff,
    ObserveBundle,
    capped_stream,
    snapshot_manifest,
)
from paa.sandbox.base import Sandbox, SandboxResult, SandboxSpec

__all__ = [
    "CrashClass",
    "CrashClassification",
    "ObserveOutcome",
    "ObserverCore",
    "classify_outcome",
]

#: POSIX signal numbers to names, for the classification's notes. The
#: short list is deliberate: these are the ones a resource-capped child
#: actually meets (kill, term, segv, abort, bus). Anything else prints
#: its number — honest and unambiguous.
_SIGNAL_NAMES = {
    1: "SIGHUP",
    2: "SIGINT",
    3: "SIGQUIT",
    4: "SIGILL",
    6: "SIGABRT",
    7: "SIGBUS",
    9: "SIGKILL",
    11: "SIGSEGV",
    13: "SIGPIPE",
    15: "SIGTERM",
}

#: Free-text caps for the git section: a diff is evidence, not a dump.
_DIFF_CAP_BYTES = 256 * 1024
_STATUS_CAP_LINES = 10_000


class CrashClass(str, Enum):
    """How the observed child ended, in the vocabulary of honest causes."""

    CLEAN_EXIT = "clean_exit"
    ERROR_EXIT = "error_exit"
    SIGNALLED = "signalled"
    TIMED_OUT = "timed_out"
    RESOURCE_KILLED = "resource_killed"


@dataclass(frozen=True, slots=True)
class CrashClassification:
    """The state a finished (possibly crashed) run left behind.

    ``recoverable`` is always ``False`` and exists to be read: a consumer
    that asks "can I resume this?" gets the spec's answer from the data,
    not from a doc it did not read.
    """

    state: CrashClass
    exit_code: int | None
    signal: int | None
    duration_ms: float
    workspace_changed: bool
    files_created: int
    files_modified: int
    files_deleted: int
    git_dirty: bool | None
    git_available: bool
    commits: int
    notes: str
    recoverable: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "signal_name": _SIGNAL_NAMES.get(self.signal) if self.signal else None,
            "duration_ms": round(self.duration_ms, 3),
            "workspace_changed": self.workspace_changed,
            "files_created": self.files_created,
            "files_modified": self.files_modified,
            "files_deleted": self.files_deleted,
            "git_available": self.git_available,
            "git_dirty": self.git_dirty,
            "commits": self.commits,
            "notes": self.notes,
            "resume": "none",
            "recoverable": self.recoverable,
        }


def classify_outcome(
    result: SandboxResult, diff: ManifestDiff, git: GitChanges
) -> CrashClassification:
    """Classify the state after the child ended.

    The precedence is the sandbox's own honesty: ``killed_reason`` is
    the backend's first-hand account of *why it* killed the tree, so it
    outranks the exit code; a negative exit code on POSIX is the kernel's
    account of a signal from elsewhere; only a code the child chose
    itself says the child ended on its own terms.
    """
    if result.killed_reason == "timeout":
        state = CrashClass.TIMED_OUT
        signal = None
    elif result.killed_reason == "memory":
        state = CrashClass.RESOURCE_KILLED
        signal = None
    elif result.exit_code is None:
        state = CrashClass.SIGNALLED
        signal = None
    elif result.exit_code < 0:
        state = CrashClass.SIGNALLED
        signal = -result.exit_code
    elif result.exit_code == 0:
        state = CrashClass.CLEAN_EXIT
        signal = None
    else:
        state = CrashClass.ERROR_EXIT
        signal = None

    workspace_changed = diff.changed
    files_created = len(diff.created)
    files_modified = len(diff.modified)
    files_deleted = len(diff.deleted)

    change_bits: list[str] = []
    if files_created:
        change_bits.append(f"{files_created} created")
    if files_modified:
        change_bits.append(f"{files_modified} modified")
    if files_deleted:
        change_bits.append(f"{files_deleted} deleted")
    changes = ", ".join(change_bits) if change_bits else "no file changes"

    if state is CrashClass.CLEAN_EXIT:
        cause = "the observed CLI exited cleanly (0)"
    elif state is CrashClass.ERROR_EXIT:
        cause = f"the observed CLI exited non-zero ({result.exit_code})"
    elif state is CrashClass.TIMED_OUT:
        cause = (
            f"the observer's wall clock ({result.duration_ms:.0f} ms) killed the "
            "whole tree mid-run"
        )
    elif state is CrashClass.RESOURCE_KILLED:
        cause = "the sandbox's resource ceiling killed the whole tree mid-run"
    else:
        how = (
            f"signal {signal} ({_SIGNAL_NAMES.get(signal, '?')})"
            if signal
            else "a signal or unaccounted kill"
        )
        cause = f"the observed CLI died to {how} — not an exit it chose"

    git_bit = (
        "git reports a dirty worktree"
        if git.dirty
        else "git reports a clean worktree"
        if git.available
        else "no git evidence (not a repo or git unavailable)"
    )
    commits_bit = f", {len(git.commits)} new commit(s)" if git.commits else ""

    notes = (
        f"{cause} after {result.duration_ms:.0f} ms; {changes}; {git_bit}{commits_bit}. "
        "The state is classified for a human decision — resume is 'none' by "
        "declaration, and nothing re-runs the command."
    )

    return CrashClassification(
        state=state,
        exit_code=result.exit_code,
        signal=signal,
        duration_ms=result.duration_ms,
        workspace_changed=workspace_changed,
        files_created=files_created,
        files_modified=files_modified,
        files_deleted=files_deleted,
        git_dirty=git.dirty,
        git_available=git.available,
        commits=len(git.commits),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# The git observation — git's own view, asked through the same sandbox
# ---------------------------------------------------------------------------


def _cap_text(text: str, *, cap_bytes: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap_bytes:
        return text
    return (
        encoded[:cap_bytes].decode("utf-8", errors="ignore")
        + f"\n… [truncated at {cap_bytes} bytes by the observer]"
    )


@dataclass(frozen=True, slots=True)
class _GitBefore:
    available: bool
    reason: str = ""
    head: str | None = None


class ObserverCore:
    """The sequencing: skeleton, snapshots, child, snapshots, verdict."""

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        bundle_home: Path,
        observer_version: str = "4.1.0",
    ) -> None:
        self._sandbox = sandbox
        self._bundle_home = bundle_home
        self._observer_version = observer_version

    def bundle_path(self, run_id: str) -> Path:
        return self._bundle_home / "observe" / run_id / "bundle.json"

    async def _git(
        self, workspace: Path, args: tuple[str, ...]
    ) -> SandboxResult:
        return await self._sandbox.run(
            SandboxSpec(
                command=("git", *args),
                workspace_path=workspace,
                timeout_seconds=60,
                memory_mb=256,
            )
        )

    async def _git_before(self, workspace: Path) -> _GitBefore:
        """Is this a repo, and where is its HEAD? Asked before the child runs."""
        try:
            inside = await self._git(workspace, ("rev-parse", "--is-inside-work-tree"))
        except SandboxError:
            # A host without a git executable must still produce a final
            # bundle. Git is an optional witness, not the observed subject.
            return _GitBefore(available=False, reason="git unavailable in sandbox")
        if inside.exit_code != 0:
            return _GitBefore(
                available=False,
                reason="not a git worktree (or git unavailable)",
            )
        head: str | None = None
        resolved = await self._git(workspace, ("rev-parse", "HEAD"))
        if resolved.exit_code == 0:
            head = resolved.stdout.strip() or None
        return _GitBefore(available=True, head=head)

    async def _git_after(self, workspace: Path, before: _GitBefore) -> GitChanges:
        """The full git view after the child ran: status, diff, new commits."""
        if not before.available:
            return GitChanges(available=False, reason=before.reason)

        status = await self._git(workspace, ("status", "--porcelain=v1"))
        status_text = "\n".join(status.stdout.splitlines()[:_STATUS_CAP_LINES])

        diff_text = ""
        if before.head is not None:
            diff = await self._git(workspace, ("diff", "HEAD"))
            diff_text = diff.stdout
        else:
            # A repo with no commits before the run: `git diff HEAD` has no
            # HEAD to diff against; everything the child staged or left in
            # the worktree is already in status + the manifest diff.
            diff_text = ""

        head_after = before.head
        resolved = await self._git(workspace, ("rev-parse", "HEAD"))
        if resolved.exit_code == 0:
            head_after = resolved.stdout.strip() or None

        commits: tuple[dict[str, str], ...] = ()
        if head_after is not None and head_after != before.head:
            if before.head is not None:
                log = await self._git(
                    workspace, ("log", "--oneline", f"{before.head}..{head_after}")
                )
            else:
                # Fresh history: the run made the first commit(s); count is
                # all of it, stated as such.
                log = await self._git(workspace, ("log", "--oneline", head_after))
            commits = tuple(
                {"line": line}
                for line in log.stdout.splitlines()
                if line.strip()
            )

        return GitChanges(
            available=True,
            status=status_text,
            diff=_cap_text(diff_text, cap_bytes=_DIFF_CAP_BYTES),
            head_before=before.head,
            head_after=head_after,
            commits=commits,
        )

    # -- the one public action ------------------------------------------------

    async def observe(
        self,
        command: Sequence[str],
        *,
        workspace: Path,
        timeout_s: int = 600,
        memory_mb: int = 512,
        transcript_cap_kb: int = 2048,
        env: Mapping[str, str] | None = None,
        run_id: str | None = None,
    ) -> ObserveOutcome:
        """Run ``command`` in ``workspace`` and produce the complete T1 bundle.

        The skeleton is on disk before the child is spawned, so a crash
        of the observer or the child leaves an honest ``phase="started"``
        artifact rather than silence. The child itself runs under the
        sandbox's one-shot contract: containment, resource ceilings and
        the whole-tree kill are the backend's, in force for the entire
        run — including the kill the wall clock performs at ``timeout_s``.
        """
        if not command:
            raise SandboxError("observe needs a non-empty command")
        rid = run_id or uuid.uuid4().hex
        bundle = ObserveBundle(
            run_id=rid,
            command=tuple(command),
            workspace=str(workspace),
            observer_version=self._observer_version,
        )
        path = self.bundle_path(rid)
        bundle.write(path)  # phase="started" — the crash marker

        cap = transcript_cap_kb * 1024

        manifest_before = snapshot_manifest(workspace)
        git_before = await self._git_before(workspace)

        result = await self._sandbox.run(
            SandboxSpec(
                command=tuple(command),
                workspace_path=workspace,
                env=dict(env) if env else {},
                timeout_seconds=float(timeout_s),
                memory_mb=memory_mb,
            )
        )

        manifest_after = snapshot_manifest(workspace)
        diff = ManifestDiff.between(manifest_before, manifest_after)
        git_after = await self._git_after(workspace, git_before)
        classification = classify_outcome(result, diff, git_after)

        bundle.child = {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "killed_reason": result.killed_reason,
            "duration_ms": round(result.duration_ms, 3),
            "peak_rss_mb": result.peak_rss_mb,
            "backend": result.backend,
            "isolation_level": result.isolation_level.name,
            "truncated_output": result.truncated_output,
        }
        bundle.transcript_stdout = capped_stream(
            "stdout", result.stdout, cap_bytes=cap
        )
        bundle.transcript_stderr = capped_stream(
            "stderr", result.stderr, cap_bytes=cap
        )
        bundle.manifest = {
            "before_count": manifest_before.count,
            "after_count": manifest_after.count,
            **diff.to_json(),
        }
        bundle.git = git_after
        bundle.classification = classification.to_json()
        bundle.finalise()
        bundle.write(path)

        return ObserveOutcome(
            run_id=rid,
            bundle=bundle,
            result=result,
            classification=classification,
            bundle_path=path,
        )


@dataclass(frozen=True, slots=True)
class ObserveOutcome:
    """Everything one observe run produced: the artifact and the verdict."""

    run_id: str
    bundle: ObserveBundle
    result: SandboxResult
    classification: CrashClassification
    bundle_path: Path

    @property
    def evidence_tier(self) -> str:
        return EVIDENCE_TIER
