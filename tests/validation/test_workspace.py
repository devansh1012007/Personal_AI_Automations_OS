"""The workspace manifest — the drift detector's comparison point.

``src/paa/validation/workspace.py`` says: *"Determinism is the whole
contract."* The manifest hash must depend on content only — never on
directory iteration order, path separator, filesystem case behaviour, or the
order in which files were written. Every one of those varies between two
machines, or between two runs on the same machine, and a checkpoint hash
that varies is a false corruption alarm on every restart.

The real :class:`WorkspaceSnapshot` had no coverage at all before this file:
``tests/conftest.py`` deliberately uses a *fake* snapshotter, because the
real one hashes a whole tree and the ledger tests do not need that. So the
module's headline claim was, like the scanner's, a promise rather than a
fact — and the three normalisations its docstring names are asserted here
one by one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from paa.validation.workspace import (
    DEFAULT_EXCLUDE_SUFFIXES,
    DEFAULT_EXCLUDES,
    WorkspaceSnapshot,
    hash_file,
)

# ---------------------------------------------------------------------------
# The manifest is a function of content
# ---------------------------------------------------------------------------


def _tree(root: Path, files: dict[str, str]) -> Path:
    """Write ``files`` under ``root`` and return the root."""
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def test_two_captures_of_the_same_tree_agree(tmp_path: Path) -> None:
    """The same content, captured twice, is the same manifest."""
    root = _tree(tmp_path / "ws", {"a.txt": "alpha\n", "pkg/b.txt": "beta\n"})

    first = WorkspaceSnapshot.capture(root)
    second = WorkspaceSnapshot.capture(root)

    assert first.manifest_hash == second.manifest_hash
    assert first.entries == second.entries


def test_the_manifest_does_not_depend_on_the_write_order(tmp_path: Path) -> None:
    """The order files were written in is not part of the workspace's identity.

    Two runs that produce the same tree must produce the same hash, or the
    recovery engine reports drift where there is none.
    """
    files = {"a.txt": "alpha\n", "pkg/b.txt": "beta\n", "pkg/c.txt": "gamma\n"}
    first = WorkspaceSnapshot.capture(_tree(tmp_path / "one", files))
    second = WorkspaceSnapshot.capture(_tree(tmp_path / "two", dict(reversed(list(files.items())))))

    assert first.manifest_hash == second.manifest_hash


def test_the_manifest_does_not_depend_on_the_root(tmp_path: Path) -> None:
    """The hash is of the content, not of where the content lives.

    Two copies of the same tree are the same workspace as far as drift is
    concerned; a hash that included the path would make a moved workspace
    look corrupted.
    """
    files = {"a.txt": "alpha\n"}
    first = WorkspaceSnapshot.capture(_tree(tmp_path / "one", files))
    second = WorkspaceSnapshot.capture(_tree(tmp_path / "two", files))

    assert first.manifest_hash == second.manifest_hash


def test_the_manifest_hash_is_order_independent() -> None:
    """The hash sorts before it hashes.

    ``os.walk`` yields files in whatever order the filesystem gives them,
    which differs across filesystems and even across runs on a network
    mount, so the digest is taken over sorted paths.
    """
    forward = {"a.txt": "1", "b.txt": "2", "pkg/c.txt": "3"}
    backward = {"pkg/c.txt": "3", "b.txt": "2", "a.txt": "1"}

    assert WorkspaceSnapshot.hash_manifest(forward) == WorkspaceSnapshot.hash_manifest(backward)


def test_the_manifest_is_content_addressed_not_mtime_addressed(tmp_path: Path) -> None:
    """Touching a file does not change the workspace.

    A manifest that included the modification time would report drift
    every time a tool rewrote a file with identical content — which is
    what an editor, a formatter and a checkout all do.
    """
    root = _tree(tmp_path / "ws", {"a.txt": "alpha\n"})
    before = WorkspaceSnapshot.capture(root)

    os.utime(root / "a.txt", (1_000_000, 1_000_000))
    after = WorkspaceSnapshot.capture(root)

    assert before.manifest_hash == after.manifest_hash
    assert after.has_drifted(before.manifest_hash) is False


def test_changed_content_changes_the_manifest(tmp_path: Path) -> None:
    """The other half of the property: a real change is still detected."""
    root = _tree(tmp_path / "ws", {"a.txt": "alpha\n"})
    before = WorkspaceSnapshot.capture(root)

    (root / "a.txt").write_text("alpha!\n", encoding="utf-8")
    after = WorkspaceSnapshot.capture(root)

    assert after.manifest_hash != before.manifest_hash
    assert after.has_drifted(before.manifest_hash) is True


def test_crlf_and_lf_hash_differently(tmp_path: Path) -> None:
    """The bytes are hashed, not the text.

    Reading text on Windows translates CRLF to LF, so two byte-different
    files would hash the same — and drift detection that cannot see a
    line-ending change is drift detection that misses a whole class of
    real patch corruption.
    """
    unix = tmp_path / "unix.txt"
    unix.write_bytes(b"one\ntwo\n")
    windows = tmp_path / "windows.txt"
    windows.write_bytes(b"one\r\ntwo\r\n")

    assert hash_file(unix) != hash_file(windows)

    unix_root = _tree(tmp_path / "unix", {"f.txt": "one\ntwo\n"})
    windows_root = tmp_path / "windows"
    windows_root.mkdir()
    (windows_root / "f.txt").write_bytes(b"one\r\ntwo\r\n")

    assert (
        WorkspaceSnapshot.capture(unix_root).manifest_hash
        != WorkspaceSnapshot.capture(windows_root).manifest_hash
    )


def test_manifest_paths_use_forward_slashes(tmp_path: Path) -> None:
    """A Windows-written manifest matches a WSL-read one.

    The separator is normalised so the same tree produces the same keys on
    every platform — otherwise the manifest recorded on Windows would not
    compare equal to the one read on Linux.
    """
    root = _tree(tmp_path / "ws", {"pkg/sub/file.txt": "x\n"})

    entries = WorkspaceSnapshot.capture(root).entries

    assert list(entries) == ["pkg/sub/file.txt"]
    assert "\\" not in "".join(entries)


# ---------------------------------------------------------------------------
# What is left out
# ---------------------------------------------------------------------------


def test_derived_directories_are_excluded(tmp_path: Path) -> None:
    """``__pycache__`` alone would guarantee a false drift alarm after an import."""
    root = _tree(
        tmp_path / "ws",
        {
            "a.txt": "alpha\n",
            "__pycache__/a.cpython-312.pyc": "compiled\n",
            ".git/config": "[core]\n",
            ".venv/lib/thing.py": "vendored\n",
            "node_modules/pkg/index.js": "vendored\n",
            ".pytest_cache/v/cache/lastfailed": "stale\n",
        },
    )

    entries = WorkspaceSnapshot.capture(root).entries

    assert list(entries) == ["a.txt"]


def test_excluded_suffixes_are_excluded(tmp_path: Path) -> None:
    """A compiled artefact is not part of the source's identity."""
    root = _tree(
        tmp_path / "ws",
        {"a.txt": "alpha\n", "b.pyc": "compiled\n", "c.pyo": "compiled\n"},
    )

    entries = WorkspaceSnapshot.capture(root).entries

    assert list(entries) == ["a.txt"]
    assert ".pyc" in DEFAULT_EXCLUDE_SUFFIXES


def test_the_default_excludes_are_the_documented_ones() -> None:
    """The exclude list is part of the contract, not an implementation detail."""
    assert ".git" in DEFAULT_EXCLUDES
    assert "__pycache__" in DEFAULT_EXCLUDES
    assert ".venv" in DEFAULT_EXCLUDES
    assert "node_modules" in DEFAULT_EXCLUDES


def test_a_symlink_is_skipped_not_followed(tmp_path: Path) -> None:
    """A symlink pointing outside the workspace is skipped, not hashed.

    Following it would pull unrelated host files into the manifest, which
    turns an unrelated change elsewhere on the machine into a spurious
    drift alarm — and leaks those paths into the ledger.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    root = _tree(tmp_path / "ws", {"a.txt": "alpha\n"})
    (root / "link").symlink_to(outside / "secret.txt")

    snapshot = WorkspaceSnapshot.capture(root)

    assert list(snapshot.entries) == ["a.txt"]
    assert snapshot.skipped == ("link",)


def test_a_symlinked_directory_is_not_walked(tmp_path: Path) -> None:
    """A symlinked directory must not pull a whole tree into the manifest."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    root = _tree(tmp_path / "ws", {"a.txt": "alpha\n"})
    (root / "linkdir").symlink_to(outside, target_is_directory=True)

    snapshot = WorkspaceSnapshot.capture(root)

    assert list(snapshot.entries) == ["a.txt"]
    assert "linkdir/secret.txt" not in snapshot.entries


def test_an_unreadable_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    """An unreadable file is a gap in the manifest, and it is surfaced.

    Dropping it silently would make the manifest look complete when it is
    not, and the caller should know the guarantee is partial.
    """
    if os.geteuid() == 0:
        pytest.skip("root can read a mode-000 file, so the branch is unreachable here")

    root = _tree(tmp_path / "ws", {"a.txt": "alpha\n", "secret.txt": "secret\n"})
    (root / "secret.txt").chmod(0o000)
    try:
        snapshot = WorkspaceSnapshot.capture(root)
    finally:
        (root / "secret.txt").chmod(0o600)

    assert "a.txt" in snapshot.entries
    assert "secret.txt" not in snapshot.entries
    assert snapshot.skipped == ("secret.txt",)


def test_capturing_a_non_directory_is_refused(tmp_path: Path) -> None:
    """There is no manifest of a file."""
    path = tmp_path / "file.txt"
    path.write_text("x\n", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        WorkspaceSnapshot.capture(path)


# ---------------------------------------------------------------------------
# Comparing manifests
# ---------------------------------------------------------------------------


def test_the_diff_says_what_changed(tmp_path: Path) -> None:
    """Added, removed and modified are three different facts."""
    root = _tree(tmp_path / "ws", {"keep.txt": "k\n", "gone.txt": "g\n"})
    before = WorkspaceSnapshot.capture(root)
    (before.root / "gone.txt").unlink()
    (before.root / "keep.txt").write_text("changed\n", encoding="utf-8")
    (before.root / "new.txt").write_text("n\n", encoding="utf-8")

    after = WorkspaceSnapshot.capture(before.root)
    diff = before.diff_against(after)

    assert diff.added == ("new.txt",)
    assert diff.removed == ("gone.txt",)
    assert diff.modified == ("keep.txt",)
    assert diff.total == 3
    assert bool(diff) is True
    assert diff.is_empty is False


def test_an_unchanged_tree_has_an_empty_diff(tmp_path: Path) -> None:
    root = _tree(tmp_path / "ws", {"a.txt": "alpha\n"})

    snapshot = WorkspaceSnapshot.capture(root)

    assert snapshot.diff_against(WorkspaceSnapshot.capture(root)).is_empty is True


# ---------------------------------------------------------------------------
# Backup and restore
# ---------------------------------------------------------------------------


def test_backup_and_restore_round_trips(tmp_path: Path) -> None:
    """A restore puts back what the manifest describes, byte for byte.

    Every restored file's hash is verified against the manifest before the
    restore is reported clean, because a backup that has itself been
    corrupted is a real possibility during the incident this runs in.
    """
    root = _tree(tmp_path / "ws", {"a.txt": "alpha\n", "pkg/b.txt": "beta\n"})
    snapshot = WorkspaceSnapshot.capture(root)

    backup = snapshot.backup(tmp_path / "backup")
    (root / "a.txt").write_text("clobbered\n", encoding="utf-8")
    (root / "pkg" / "b.txt").unlink()

    report = WorkspaceSnapshot.restore(snapshot, backup, root)

    assert report.ok is True
    assert (root / "a.txt").read_text(encoding="utf-8") == "alpha\n"
    assert (root / "pkg" / "b.txt").read_text(encoding="utf-8") == "beta\n"


def test_a_restore_removes_files_the_manifest_does_not_list(tmp_path: Path) -> None:
    """A restore is a restore, not an overlay.

    A half-applied patch leaves *new* files behind, and leaving them would
    keep the workspace drifted after a "successful" rollback.
    """
    root = _tree(tmp_path / "ws", {"a.txt": "alpha\n"})
    snapshot = WorkspaceSnapshot.capture(root)
    backup = snapshot.backup(tmp_path / "backup")
    (root / "extraneous.txt").write_text("left over\n", encoding="utf-8")

    report = WorkspaceSnapshot.restore(snapshot, backup, root)

    assert report.deleted == ("extraneous.txt",)
    assert not (root / "extraneous.txt").exists()


def test_a_restore_reports_a_missing_backup_file(tmp_path: Path) -> None:
    """A backup that is not there is reported, not silently skipped."""
    root = _tree(tmp_path / "ws", {"a.txt": "alpha\n"})
    snapshot = WorkspaceSnapshot.capture(root)
    backup = snapshot.backup(tmp_path / "backup")
    (backup / "a.txt").unlink()

    report = WorkspaceSnapshot.restore(snapshot, backup, root)

    assert report.ok is False
    assert report.missing_from_backup == ("a.txt",)


def test_the_snapshot_payload_is_ledger_shaped(tmp_path: Path) -> None:
    """The full manifest is deliberately not in the payload.

    A large workspace would bloat every event; the hash is what recovery
    compares.
    """
    root = _tree(tmp_path / "ws", {"a.txt": "alpha\n"})
    snapshot = WorkspaceSnapshot.capture(root)

    payload = snapshot.to_payload()

    assert payload["manifest_hash"] == snapshot.manifest_hash
    assert payload["file_count"] == 1
    assert payload["skipped_count"] == 0
    assert "entries" not in payload
