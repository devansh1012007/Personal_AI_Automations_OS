"""The T1 observation bundle — the honest artifact of ``paa observe`` (C7.1).

The bundle is what an observer can honestly say about a run it watched
but never controlled. The spec's evidence ladder (pipeline-platform
spec §6) fixes the tiers: **T1** observed from outside, T2 mediated, T3
contained. T1 is the *cheapest honest* evidence — it does not claim the
harness behaved, only that these bytes came out of it, these files
changed under it, and this is the state it left the workspace in.

Two properties the format holds by construction:

* **It is complete or it says it is not.** The bundle is written twice:
  a skeleton with ``phase="started"`` before the child is spawned, and
  the final artifact with ``phase="final"`` after classification. A
  bundle that still says ``started`` is the crash marker for an observer
  or child that never came back — the file itself is the evidence, and
  nothing silently upgrades it.
* **It never overstates.** ``resume`` is ``"none"`` on every observe
  bundle (the spec's own rule for external harnesses): the artifact
  classifies the state a crash left behind; it does not promise that
  re-running the command would resume anything. Free-text payloads are
  capped (``transcript_cap_kb``) with an explicit ``truncated`` marker
  rather than silently dropped.

The JSON shape (``paa-observe-bundle/1``) is documented for consumers
in ``docs/interfaces/observe-bundle.md``; this module is its only
writer and reader.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "BUNDLE_SCHEMA",
    "EVIDENCE_TIER",
    "FileFacts",
    "GitChanges",
    "ManifestDiff",
    "ManifestSnapshot",
    "ObserveBundle",
    "read_bundle",
]

#: The bundle format's identity. Additive changes only within ``/1``.
BUNDLE_SCHEMA = "paa-observe-bundle/1"

#: The tier this artifact honestly claims (spec §6: observed = T1).
EVIDENCE_TIER = "T1"

#: The spec's rule for every external-harness adapter, restated where
#: a reader will actually meet it: no observe bundle promises a resume.
_RESUME_NONE = "none"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The manifest — every file under the workspace, by content identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileFacts:
    """One file's content identity: relative path, size, SHA-256.

    ``mtime`` is deliberately absent: the manifest answers "what is the
    content", and a timestamp answers nothing a copy preserves. Two
    checkouts of the same tree must produce the same manifest.
    """

    path: str
    size: int
    sha256: str

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> FileFacts:
        return cls(
            path=str(raw["path"]), size=int(raw["size"]), sha256=str(raw["sha256"])
        )


#: The one directory the manifest walk skips: git's own object store.
#: Its churn is the *git changes* section's business (status/diff/commits),
#: and letting it into the file manifest would swamp the diff with
#: pack internals that say nothing about the work.
_MANIFEST_SKIP_DIRS = frozenset({".git"})


def snapshot_manifest(root: Path) -> ManifestSnapshot:
    """Walk ``root`` and record every regular file's content identity.

    Symlinks are recorded as their own fact (path + the link text is
    *not* followed — the sandbox's own workspace rule) and sockets/FIFOs
    are skipped: the manifest is about files a diff can speak for.
    """
    files: list[FileFacts] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in _MANIFEST_SKIP_DIRS for part in rel.parts):
            continue
        if path.is_symlink():
            target = path.readlink().as_posix()
            digest = hashlib.sha256(("link:" + target).encode("utf-8")).hexdigest()
            files.append(FileFacts(rel.as_posix(), len(target), digest))
        elif path.is_file():
            files.append(
                FileFacts(rel.as_posix(), path.stat().st_size, _sha256_file(path))
            )
    return ManifestSnapshot(files=tuple(files))


@dataclass(frozen=True, slots=True)
class ManifestSnapshot:
    """The workspace's file manifest at one instant."""

    files: tuple[FileFacts, ...]

    def by_path(self) -> dict[str, FileFacts]:
        return {facts.path: facts for facts in self.files}

    @property
    def count(self) -> int:
        return len(self.files)


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    """What changed between two snapshots, by content identity.

    A file whose SHA-256 changed is ``modified`` even when only the
    mode flipped and the bytes did not — no, it is not: the manifest
    records content, and a mode-only change is invisible to it. That is
    the honest limit of a content manifest, and the git section is
    where mode changes become visible.
    """

    created: tuple[FileFacts, ...] = ()
    modified: tuple[tuple[FileFacts, FileFacts], ...] = ()
    deleted: tuple[FileFacts, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.created or self.modified or self.deleted)

    @classmethod
    def between(cls, before: ManifestSnapshot, after: ManifestSnapshot) -> ManifestDiff:
        old = before.by_path()
        new = after.by_path()
        created = tuple(new[p] for p in sorted(new.keys() - old.keys()))
        deleted = tuple(old[p] for p in sorted(old.keys() - new.keys()))
        modified = tuple(
            (old[p], new[p])
            for p in sorted(old.keys() & new.keys())
            if old[p].sha256 != new[p].sha256
        )
        return cls(created=created, modified=modified, deleted=deleted)

    def to_json(self) -> dict[str, Any]:
        return {
            "created": [f.to_json() for f in self.created],
            "modified": [
                {"before": a.to_json(), "after": b.to_json()} for a, b in self.modified
            ],
            "deleted": [f.to_json() for f in self.deleted],
        }


# ---------------------------------------------------------------------------
# The git section — what git itself says changed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitChanges:
    """The git view of the workspace, before and after the run.

    ``available=False`` with a ``reason`` is an honest answer, not a
    failure: a workspace that is not a git repo (or a host without git)
    simply has no git evidence to give, and the bundle says so rather
    than inventing a diff.
    """

    available: bool
    reason: str = ""
    status: str = ""
    diff: str = ""
    head_before: str | None = None
    head_after: str | None = None
    commits: tuple[dict[str, str], ...] = ()

    @property
    def dirty(self) -> bool | None:
        """Whether the worktree is dirty; ``None`` when git was unavailable."""
        if not self.available:
            return None
        return bool(self.status.strip())

    def to_json(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "status": self.status,
            "diff": self.diff,
            "head_before": self.head_before,
            "head_after": self.head_after,
            "commits": list(self.commits),
            "dirty": self.dirty,
        }


# ---------------------------------------------------------------------------
# The transcript — what the child actually said
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TranscriptStream:
    """One output stream of the observed child, whole and hashed.

    The full text rides the bundle (it is the evidence); the hash makes
    the ledger-side attestation possible without the ledger carrying
    the bytes. ``truncated`` is the honest marker when the cap bit.
    """

    stream: str
    text: str
    byte_length: int
    truncated: bool = False

    @property
    def sha256(self) -> str:
        return _sha256_text(self.text)

    def to_json(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "sha256": self.sha256,
            "bytes": self.byte_length,
            "truncated": self.truncated,
            "text": self.text,
        }


def capped_stream(stream: str, text: str, *, cap_bytes: int) -> TranscriptStream:
    """Build a stream fact, honestly capped.

    The cap is applied to the *encoded* length (what the child actually
    produced), and the kept text is re-encoded safely: a cap that lands
    mid-codepoint keeps the whole codepoint, because a mojibake
    transcript is evidence about the cap, not the child.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap_bytes:
        return TranscriptStream(stream=stream, text=text, byte_length=len(encoded))
    kept = encoded[:cap_bytes].decode("utf-8", errors="ignore")
    return TranscriptStream(
        stream=stream, text=kept, byte_length=len(encoded), truncated=True
    )


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ObserveBundle:
    """The complete T1 artifact: run, transcript, manifest, git, verdict.

    Mutable only because the observer fills it in two phases (the
    ``started`` skeleton, then the ``final`` record); once written final
    it is never rewritten. ``write`` is atomic (temp file + rename) so a
    crash mid-write cannot leave a half-valid bundle — the reader sees
    either the old skeleton or the new final, never a splice.
    """

    run_id: str
    command: tuple[str, ...]
    workspace: str
    started_at: str = field(default_factory=_utc_now_iso)
    ended_at: str | None = None
    phase: str = "started"
    resume: str = _RESUME_NONE
    evidence_tier: str = EVIDENCE_TIER
    observer_version: str = "4.1.0"
    child: dict[str, Any] = field(default_factory=dict)
    transcript_stdout: TranscriptStream | None = None
    transcript_stderr: TranscriptStream | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    git: GitChanges | None = None
    classification: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": BUNDLE_SCHEMA,
            "phase": self.phase,
            "run": {
                "run_id": self.run_id,
                "command": list(self.command),
                "workspace": self.workspace,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "resume": self.resume,
                "evidence_tier": self.evidence_tier,
                "observer_version": self.observer_version,
            },
            "child": self.child,
            "transcript": {
                "stdout": self.transcript_stdout.to_json()
                if self.transcript_stdout
                else None,
                "stderr": self.transcript_stderr.to_json()
                if self.transcript_stderr
                else None,
            },
            "manifest": self.manifest,
            "git": self.git.to_json() if self.git else None,
            "classification": self.classification,
        }

    def write(self, path: Path) -> None:
        """Write the bundle atomically (``<path>.tmp`` then rename)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(self.to_json(), indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(path)

    def finalise(self) -> ObserveBundle:
        """Mark the bundle final. One-way by convention; tests pin it."""
        self.phase = "final"
        self.ended_at = _utc_now_iso()
        return self

    def sha256(self) -> str:
        """The bundle's own content hash, for ledger-side attestation."""
        return _sha256_text(json.dumps(self.to_json(), sort_keys=True))


def read_bundle(path: Path) -> ObserveBundle:
    """Read a bundle from disk, validating the schema tag.

    Raises ``ValueError`` for a foreign schema — a file that is not a
    ``paa-observe-bundle/1`` must not be silently reinterpreted.
    """
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema") != BUNDLE_SCHEMA:
        raise ValueError(
            f"{path} is not a {BUNDLE_SCHEMA} bundle (schema={raw.get('schema')!r})"
        )
    run = raw.get("run") or {}
    transcript = raw.get("transcript") or {}

    def _stream(key: str) -> TranscriptStream | None:
        s = transcript.get(key)
        if not s:
            return None
        return TranscriptStream(
            stream=str(s["stream"]),
            text=str(s["text"]),
            byte_length=int(s["bytes"]),
            truncated=bool(s["truncated"]),
        )

    git_raw = raw.get("git")
    git = (
        GitChanges(
            available=bool(git_raw["available"]),
            reason=str(git_raw.get("reason") or ""),
            status=str(git_raw.get("status") or ""),
            diff=str(git_raw.get("diff") or ""),
            head_before=git_raw.get("head_before"),
            head_after=git_raw.get("head_after"),
            commits=tuple(dict(c) for c in git_raw.get("commits") or ()),
        )
        if git_raw
        else None
    )

    bundle = ObserveBundle(
        run_id=str(run.get("run_id") or ""),
        command=tuple(str(c) for c in run.get("command") or ()),
        workspace=str(run.get("workspace") or ""),
        started_at=str(run.get("started_at") or ""),
        ended_at=run.get("ended_at"),
        phase=str(raw.get("phase") or "started"),
        resume=str(run.get("resume") or _RESUME_NONE),
        evidence_tier=str(run.get("evidence_tier") or EVIDENCE_TIER),
        observer_version=str(run.get("observer_version") or ""),
        child=dict(raw.get("child") or {}),
        transcript_stdout=_stream("stdout"),
        transcript_stderr=_stream("stderr"),
        manifest=dict(raw.get("manifest") or {}),
        git=git,
        classification=dict(raw.get("classification") or {}),
    )
    return bundle
