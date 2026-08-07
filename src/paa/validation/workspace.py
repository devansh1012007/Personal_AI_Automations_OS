"""Content-addressed workspace manifests for crash-recovery drift detection.

RFC §1.5 replays the ledger to reconstruct what the runtime *believes* happened.
That belief is only useful if it can be checked against what is actually on
disk. A crash between "patch applied" and "MUTATION_COMMITTED appended" leaves
the filesystem ahead of the log; a crash the other way leaves it behind. Both
are silent, and both corrupt every later decision made from the projection.

:class:`WorkspaceSnapshot` is the comparison point:
``TaskProjection.checkpoint_manifest_hash`` holds the manifest hash at the last
verified checkpoint, and the recovery engine recomputes it after a restart. A
mismatch raises :class:`~paa.core.errors.ReplayIntegrityError` instead of
resuming onto a workspace that has silently drifted.

Determinism is the whole contract
---------------------------------
The manifest hash must depend on *content only* — never on directory iteration
order, path separator, filesystem case behaviour, or the order in which files
were written. Every one of those varies between two machines, or between two
runs on the same machine, and a checkpoint hash that varies is a false
corruption alarm on every restart. Three normalisations enforce it:

1. entries are sorted by path before hashing;
2. separators are normalised to ``/`` so a Windows-written manifest matches a
   WSL-read one;
3. files are hashed as **bytes**, with no text-mode newline translation — the
   thing that would otherwise make CRLF and LF versions of one file hash
   identically on Windows and differently on Linux.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

__all__ = [
    "DEFAULT_EXCLUDES",
    "ManifestDiff",
    "RestoreReport",
    "WorkspaceSnapshot",
    "hash_file",
]

log = structlog.get_logger(__name__)

#: Directories and patterns excluded from every manifest.
#:
#: These are all *derived* state: rebuilding them from source is deterministic,
#: so their contents carry no information about whether the workspace drifted.
#: Including them would make the hash change on every interpreter run —
#: ``__pycache__`` alone would guarantee a false drift alarm after any import.
DEFAULT_EXCLUDES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "node_modules",
        ".idea",
        ".vscode",
        ".DS_Store",
        "Thumbs.db",
        ".paa_journal",
    }
)

#: File suffixes excluded regardless of location.
DEFAULT_EXCLUDE_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo", ".pyd", ".so.tmp"})

_CHUNK = 1024 * 1024


def hash_file(path: Path) -> str:
    """SHA-256 of a file's exact bytes.

    Binary mode is not incidental. Reading text on Windows translates CRLF to
    LF, so two byte-different files would hash the same — and drift detection
    that cannot see a line-ending change is drift detection that misses a whole
    class of real patch corruption.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    """What changed between two manifests."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.modified)

    def to_payload(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "modified": list(self.modified),
            "total": self.total,
        }

    def __bool__(self) -> bool:
        """Truthy when something changed — reads naturally at call sites."""
        return not self.is_empty


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """Outcome of a restore."""

    restored: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    missing_from_backup: tuple[str, ...] = ()
    hash_mismatches: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """A restore is only clean if every file came back byte-identical."""
        return not self.missing_from_backup and not self.hash_mismatches


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """An immutable ``{relative_path: sha256}`` manifest of a directory tree."""

    root: Path
    entries: dict[str, str] = field(default_factory=dict)
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    skipped: tuple[str, ...] = ()
    """Paths that could not be hashed (permissions, a file deleted mid-walk).
    Surfaced rather than dropped: an unreadable file is a gap in the integrity
    guarantee and the caller should know the manifest is partial."""

    # -- capture -----------------------------------------------------------

    @classmethod
    def capture(
        cls,
        root: Path | str,
        *,
        excludes: frozenset[str] = DEFAULT_EXCLUDES,
        exclude_suffixes: frozenset[str] = DEFAULT_EXCLUDE_SUFFIXES,
        follow_symlinks: bool = False,
    ) -> WorkspaceSnapshot:
        """Build a manifest of every file under ``root``.

        ``follow_symlinks`` defaults to ``False`` for two independent reasons:
        a symlink loop makes the walk non-terminating, and a symlink pointing
        outside the workspace would pull unrelated host files into the
        manifest — turning an unrelated change elsewhere on the machine into a
        spurious drift alarm, and leaking those paths into the ledger.
        """
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise NotADirectoryError(f"workspace root is not a directory: {root_path}")

        entries: dict[str, str] = {}
        skipped: list[str] = []

        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=follow_symlinks):
            # In-place mutation is how os.walk is told not to descend; a new
            # list would be ignored and the excluded trees walked anyway.
            dirnames[:] = sorted(d for d in dirnames if d not in excludes)
            current = Path(dirpath)

            for filename in sorted(filenames):
                if filename in excludes:
                    continue
                file_path = current / filename
                if file_path.suffix in exclude_suffixes:
                    continue
                if not follow_symlinks and file_path.is_symlink():
                    skipped.append(cls._relative(file_path, root_path))
                    continue
                try:
                    digest = hash_file(file_path)
                except OSError as exc:
                    log.debug(
                        "validation.workspace.unreadable", path=str(file_path), error=str(exc)
                    )
                    skipped.append(cls._relative(file_path, root_path))
                    continue
                entries[cls._relative(file_path, root_path)] = digest

        return cls(root=root_path, entries=entries, skipped=tuple(sorted(skipped)))

    @staticmethod
    def _relative(path: Path, root: Path) -> str:
        """POSIX-style relative path, so manifests compare across platforms."""
        return path.relative_to(root).as_posix()

    # -- identity ----------------------------------------------------------

    @property
    def manifest_hash(self) -> str:
        """Order-independent digest of the whole manifest.

        Sorting before hashing is what makes this order-independent: two
        captures of identical content produce byte-identical input to the hash
        regardless of the order ``os.walk`` happened to yield files in, which
        differs across filesystems and even across runs on network mounts.

        Length-prefix-free but newline-delimited with an explicit ``\\0``
        separator between path and digest, so a path containing a newline
        cannot be crafted to collide with a different manifest.
        """
        return self.hash_manifest(self.entries)

    @staticmethod
    def hash_manifest(entries: dict[str, str]) -> str:
        digest = hashlib.sha256()
        for path in sorted(entries):
            digest.update(path.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(entries[path].encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def total_bytes(self) -> int:
        """Sum of on-disk sizes. Best-effort; a vanished file counts as zero."""
        total = 0
        for relative in self.entries:
            with_suppress = self.root / relative
            try:
                total += with_suppress.stat().st_size
            except OSError:
                continue
        return total

    def to_payload(self) -> dict[str, Any]:
        """Ledger form. The full manifest is deliberately excluded — a large
        workspace would bloat every event; the hash is what recovery compares."""
        return {
            "root": str(self.root),
            "manifest_hash": self.manifest_hash,
            "file_count": self.file_count,
            "captured_at": self.captured_at.isoformat(),
            "skipped_count": len(self.skipped),
        }

    # -- comparison --------------------------------------------------------

    @staticmethod
    def diff(
        a: WorkspaceSnapshot | dict[str, str],
        b: WorkspaceSnapshot | dict[str, str],
    ) -> ManifestDiff:
        """Compare two manifests: what ``b`` added, removed, and modified.

        Accepts snapshots or bare manifests so the recovery engine can compare
        a live capture against a manifest deserialised from the ledger without
        reconstructing a snapshot object around it.
        """
        left = a.entries if isinstance(a, WorkspaceSnapshot) else a
        right = b.entries if isinstance(b, WorkspaceSnapshot) else b

        left_keys, right_keys = set(left), set(right)
        return ManifestDiff(
            added=tuple(sorted(right_keys - left_keys)),
            removed=tuple(sorted(left_keys - right_keys)),
            modified=tuple(
                sorted(key for key in left_keys & right_keys if left[key] != right[key])
            ),
        )

    def diff_against(self, other: WorkspaceSnapshot | dict[str, str]) -> ManifestDiff:
        """Convenience: ``self`` as the baseline."""
        return WorkspaceSnapshot.diff(self, other)

    def has_drifted(self, expected_manifest_hash: str) -> bool:
        """Whether the live tree disagrees with a recorded checkpoint hash."""
        return self.manifest_hash != expected_manifest_hash

    # -- backup and restore ------------------------------------------------

    def backup(self, backup_dir: Path | str) -> Path:
        """Mirror every manifest file into ``backup_dir``.

        A mirrored tree rather than a content-addressed store: restore has to
        work when the ledger is the only other surviving artifact, and a human
        staring at a backup directory during an incident can read a mirrored
        tree without needing this code to interpret it.
        """
        target = Path(backup_dir).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        for relative in sorted(self.entries):
            source = self.root / relative
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source, destination)
            except OSError as exc:
                log.warning(
                    "validation.workspace.backup_failed", path=relative, error=str(exc)
                )
        (target / ".paa_manifest_hash").write_text(self.manifest_hash, encoding="utf-8")
        log.info(
            "validation.workspace.backed_up",
            files=len(self.entries),
            manifest_hash=self.manifest_hash[:12],
            backup_dir=str(target),
        )
        return target

    @staticmethod
    def restore(
        manifest: dict[str, str] | WorkspaceSnapshot,
        backup_dir: Path | str,
        target_root: Path | str,
        *,
        remove_extraneous: bool = True,
    ) -> RestoreReport:
        """Restore ``target_root`` to exactly the state ``manifest`` describes.

        Every restored file's hash is verified against the manifest before the
        restore is reported clean. A backup that has itself been corrupted is a
        real possibility during the incident this runs in, and silently
        restoring corrupted bytes would convert a recoverable crash into
        undetected data loss.

        ``remove_extraneous`` deletes files the manifest does not list, which
        is what makes this a true restore rather than an overlay — a
        half-applied patch leaves *new* files behind, and leaving them would
        keep the workspace drifted after a "successful" rollback.
        """
        entries = manifest.entries if isinstance(manifest, WorkspaceSnapshot) else manifest
        backup = Path(backup_dir).expanduser().resolve()
        root = Path(target_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)

        restored: list[str] = []
        missing: list[str] = []
        mismatched: list[str] = []
        deleted: list[str] = []

        for relative, expected_hash in sorted(entries.items()):
            source = backup / relative
            destination = root / relative
            if not source.is_file():
                missing.append(relative)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if hash_file(destination) != expected_hash:
                mismatched.append(relative)
            else:
                restored.append(relative)

        if remove_extraneous:
            live = WorkspaceSnapshot.capture(root)
            for relative in sorted(set(live.entries) - set(entries)):
                with_path = root / relative
                try:
                    with_path.unlink()
                    deleted.append(relative)
                except OSError as exc:
                    log.warning(
                        "validation.workspace.delete_failed", path=relative, error=str(exc)
                    )

        report = RestoreReport(
            restored=tuple(restored),
            deleted=tuple(deleted),
            missing_from_backup=tuple(missing),
            hash_mismatches=tuple(mismatched),
        )
        log.info(
            "validation.workspace.restored",
            restored=len(restored),
            deleted=len(deleted),
            missing=len(missing),
            mismatched=len(mismatched),
            ok=report.ok,
        )
        return report
