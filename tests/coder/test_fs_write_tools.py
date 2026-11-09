"""The filesystem write tools — write_file, edit_file, apply_patch (N1.2).

Every write must go through ``dry_run`` → ``PatchApplier.apply``, with
the path jailed before it is protected and both before the disk is
touched (ADR-0045). The tests pin the guarantees a caller relies on:
exact bytes on disk, the returned ``patch_sha256`` matching
``compute_patch_sha256``, edit matching exactly once with no write on
zero/multi-match, protected/jail denials with the frozen reason
strings, binary/non-UTF-8 refusals, and the applier's prior-hash
re-check refusing a plan whose file changed underneath it.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

import pytest

from paa.coder.fs_write_tools import (
    MAX_CONTENT_BYTES,
    MAX_PATCH_BYTES,
    FsWriteSession,
    build_fs_write_tools,
)
from paa.coder.permissions import (
    DEFAULT_MUTATING_TOOLS,
    PermissionMode,
    PermissionOutcome,
    ToolPermissionPolicy,
)
from paa.coder.workspace import Workspace
from paa.config import reset_settings_cache
from paa.core.errors import ValidationError
from paa.validation.patch import PatchApplier, UnifiedDiffValidator, compute_patch_sha256


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root=root)


@pytest.fixture
def session(ws: Workspace) -> FsWriteSession:
    return FsWriteSession(ws)


def _tools(session: FsWriteSession) -> dict[str, Any]:
    return build_fs_write_tools(session.workspace, session=session)


async def _run(tool: Any, **arguments: Any) -> dict[str, Any]:
    result = await tool.run(arguments)
    assert isinstance(result, str)
    return json.loads(result)  # type: ignore[no-any-return]


def _sha_of(session: FsWriteSession) -> str:
    assert session.last_patch_text is not None
    return compute_patch_sha256(session.last_patch_text)


# ---------------------------------------------------------------------------
# write_file — creation and overwrite
# ---------------------------------------------------------------------------


class TestWriteFile:
    async def test_create_writes_exact_bytes(self, ws: Workspace, session: FsWriteSession) -> None:
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="a.txt", content="hello\nworld\n")
        assert payload["ok"] is True
        assert payload["path"] == "a.txt"
        assert (ws.root / "a.txt").read_bytes() == b"hello\nworld\n"
        assert payload["patch_sha256"] == _sha_of(session)
        assert len(payload["patch_sha256"]) == 64
        assert session.journals[-1].patch_sha256 == payload["patch_sha256"]

    async def test_create_in_nested_directory(self, ws: Workspace, session: FsWriteSession) -> None:
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="pkg/sub/b.txt", content="x\n")
        assert payload["ok"] is True
        assert (ws.root / "pkg" / "sub" / "b.txt").read_bytes() == b"x\n"

    async def test_overwrite_replaces_fully(self, ws: Workspace, session: FsWriteSession) -> None:
        (ws.root / "a.txt").write_bytes(b"old\n")
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="a.txt", content="new\n")
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == b"new\n"
        assert payload["patch_sha256"] == _sha_of(session)

    async def test_empty_content_creates_empty_file(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        """An empty write is an empty file — the planner phantom-line bug
        (TASK-2) must not reappear through the write path."""
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="empty.txt", content="")
        assert payload["ok"] is True
        assert (ws.root / "empty.txt").read_bytes() == b""

    async def test_content_without_trailing_newline_is_preserved(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="a.txt", content="no-newline")
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == b"no-newline"

    async def test_overwrite_empty_file_with_trailing_newline_is_exact(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        """MAJOR-1 regression: an empty prior file must not eat the
        trailing newline the new content carries."""
        (ws.root / "a.txt").write_bytes(b"")
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="a.txt", content="hello\n")
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == b"hello\n"

    async def test_overwrite_no_trailing_file_gains_newline(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        """MAJOR-1 regression: overwriting a no-trailing-newline prior
        with newline-terminated content keeps the newline."""
        (ws.root / "a.txt").write_bytes(b"old")
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="a.txt", content="new\n")
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == b"new\n"

    async def test_unicode_round_trips(self, ws: Workspace, session: FsWriteSession) -> None:
        content = "héllo — ünïcode ✓\n"
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="a.txt", content=content)
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == content.encode("utf-8")

    async def test_bom_content_writes_bom_bytes(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="a.txt", content="\ufeffhello\n")
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == b"\xef\xbb\xbfhello\n"

    async def test_overwrite_of_crlf_file_preserves_crlf(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        """The planner re-joins a modified file with its own newline, so
        rewriting a CRLF file keeps CRLF (the diff cannot do better)."""
        (ws.root / "a.txt").write_bytes(b"old\r\nlines\r\n")
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="a.txt", content="new\nlines\n")
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == b"new\r\nlines\r\n"

    async def test_new_file_with_crlf_input_is_lf(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        """Documents the wire-format limit: a diff line cannot end in a CR
        (the parser normalises line endings), so a created file is LF even
        when the content arrived CRLF. Modifying that file afterwards
        preserves LF the same way."""
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="a.txt", content="a\r\nb\r\n")
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == b"a\nb\n"

    async def test_rollback_all_undoes_the_session(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        (ws.root / "a.txt").write_bytes(b"before\n")
        tools = _tools(session)
        await _run(tools["write_file"], path="a.txt", content="after\n")
        await _run(tools["write_file"], path="b.txt", content="new\n")
        touched = session.rollback_all()
        assert "a.txt" in touched and "b.txt" in touched
        assert (ws.root / "a.txt").read_bytes() == b"before\n"
        assert not (ws.root / "b.txt").exists()

    async def test_write_to_existing_directory_is_refused(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        (ws.root / "dir").mkdir()
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="dir", content="x\n")
        assert payload["ok"] is False
        assert (ws.root / "dir").is_dir()


class TestEditFile:
    async def test_exact_once_edit(self, ws: Workspace, session: FsWriteSession) -> None:
        (ws.root / "a.txt").write_bytes(b"one\ntwo\nthree\n")
        tools = _tools(session)
        payload = await _run(
            tools["edit_file"], path="a.txt", old_str="two", new_str="2"
        )
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == b"one\n2\nthree\n"
        assert payload["patch_sha256"] == _sha_of(session)

    async def test_zero_match_fails_without_write(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        original = b"one\ntwo\n"
        (ws.root / "a.txt").write_bytes(original)
        tools = _tools(session)
        payload = await _run(
            tools["edit_file"], path="a.txt", old_str="absent", new_str="x"
        )
        assert payload["ok"] is False
        assert "not found" in payload["error"]
        assert (ws.root / "a.txt").read_bytes() == original
        assert session.journals == []

    async def test_multi_match_fails_without_write(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        original = b"x\nx\n"
        (ws.root / "a.txt").write_bytes(original)
        tools = _tools(session)
        payload = await _run(tools["edit_file"], path="a.txt", old_str="x", new_str="y")
        assert payload["ok"] is False
        assert "exactly once" in payload["error"]
        assert "2" in payload["error"]
        assert (ws.root / "a.txt").read_bytes() == original
        assert session.journals == []

    async def test_empty_old_str_is_refused(self, ws: Workspace, session: FsWriteSession) -> None:
        (ws.root / "a.txt").write_bytes(b"one\n")
        tools = _tools(session)
        payload = await _run(tools["edit_file"], path="a.txt", old_str="", new_str="y")
        assert payload["ok"] is False
        assert (ws.root / "a.txt").read_bytes() == b"one\n"

    async def test_edit_of_missing_file_fails(self, ws: Workspace, session: FsWriteSession) -> None:
        tools = _tools(session)
        payload = await _run(tools["edit_file"], path="nope.txt", old_str="a", new_str="b")
        assert payload["ok"] is False
        assert "does not exist" in payload["error"]
        assert not (ws.root / "nope.txt").exists()

    async def test_edit_of_directory_fails(self, ws: Workspace, session: FsWriteSession) -> None:
        (ws.root / "dir").mkdir()
        tools = _tools(session)
        payload = await _run(tools["edit_file"], path="dir", old_str="a", new_str="b")
        assert payload["ok"] is False
        assert "not a file" in payload["error"]

    async def test_edit_deleting_a_line(self, ws: Workspace, session: FsWriteSession) -> None:
        (ws.root / "a.txt").write_bytes(b"a\nb\nc\n")
        tools = _tools(session)
        payload = await _run(tools["edit_file"], path="a.txt", old_str="b\n", new_str="")
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == b"a\nc\n"

    async def test_crlf_file_edit_preserves_crlf(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        (ws.root / "a.txt").write_bytes(b"one\r\ntwo\r\n")
        tools = _tools(session)
        payload = await _run(tools["edit_file"], path="a.txt", old_str="two", new_str="2")
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == b"one\r\n2\r\n"

    async def test_bom_file_edit_preserves_bom(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        """old_str is matched in BOM-free space; the BOM stays on write."""
        (ws.root / "a.txt").write_bytes(b"\xef\xbb\xbfhello\nworld\n")
        tools = _tools(session)
        payload = await _run(
            tools["edit_file"], path="a.txt", old_str="world", new_str="there"
        )
        assert payload["ok"] is True
        assert (ws.root / "a.txt").read_bytes() == b"\xef\xbb\xbfhello\nthere\n"

    async def test_edit_result_over_size_is_refused(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        """MAJOR-2 regression: the *result* of an edit is capped, not just
        write_file's content. One char replaced by 300 KiB would previously
        have been written (past the 256 KiB journal/memory bound) and left
        the file uneditable afterwards."""
        original = b"x\n"
        (ws.root / "a.txt").write_bytes(original)
        tools = _tools(session)
        payload = await _run(
            tools["edit_file"], path="a.txt", old_str="x", new_str="y" * (300 * 1024)
        )
        assert payload["ok"] is False
        assert payload["limit_exceeded"] is True
        assert int(payload["size"]) > MAX_CONTENT_BYTES
        assert (ws.root / "a.txt").read_bytes() == original
        assert session.journals == []


# ---------------------------------------------------------------------------
# Jail + protection (every path, before the disk is touched)
# ---------------------------------------------------------------------------


class TestJailAndProtect:
    async def test_parent_traversal_denied(self, tmp_path: Path, session: FsWriteSession) -> None:
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="../escape.txt", content="x\n")
        assert payload["ok"] is False
        assert payload["error"] == "path escapes the workspace jail"
        assert not (tmp_path / "escape.txt").exists()

    async def test_absolute_path_outside_denied(
        self, tmp_path: Path, session: FsWriteSession
    ) -> None:
        outside = tmp_path / "outside.txt"
        tools = _tools(session)
        payload = await _run(tools["write_file"], path=str(outside), content="x\n")
        assert payload["ok"] is False
        assert payload["error"] == "path escapes the workspace jail"
        assert not outside.exists()

    async def test_symlink_escape_denied(self, tmp_path: Path, session: FsWriteSession) -> None:
        outside = tmp_path / "secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        (session.workspace.root / "link.txt").symlink_to(outside)
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="link.txt", content="x\n")
        assert payload["ok"] is False
        assert payload["error"] == "path escapes the workspace jail"
        assert outside.read_text(encoding="utf-8") == "secret\n"

    async def test_git_subtree_denied(self, ws: Workspace, session: FsWriteSession) -> None:
        tools = _tools(session)
        payload = await _run(tools["write_file"], path=".git/config", content="x\n")
        assert payload["ok"] is False
        assert payload["error"] == "protected path: git"
        assert not (ws.root / ".git").exists()

    async def test_env_denied_at_any_depth(self, ws: Workspace, session: FsWriteSession) -> None:
        tools = _tools(session)
        for relative in (".env", ".env.local", "sub/.env.local"):
            payload = await _run(tools["write_file"], path=relative, content="x\n")
            assert payload["ok"] is False, relative
            assert payload["error"] == "protected path: env", relative
        assert not (ws.root / ".env").exists()
        assert not (ws.root / "sub").exists()

    async def test_paa_home_denied(
        self, ws: Workspace, session: FsWriteSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = ws.root / "paahome"
        home.mkdir()
        monkeypatch.setenv("PAA_HOME", str(home))
        reset_settings_cache()
        try:
            tools = _tools(session)
            payload = await _run(tools["write_file"], path="paahome/data.txt", content="x\n")
            assert payload["ok"] is False
            assert payload["error"] == "protected path: paa-home"
            assert not (home / "data.txt").exists()
        finally:
            reset_settings_cache()

    async def test_edit_of_protected_denied_without_write(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        (ws.root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        tools = _tools(session)
        payload = await _run(
            tools["edit_file"], path=".env", old_str="SECRET", new_str="X"
        )
        assert payload["ok"] is False
        assert payload["error"] == "protected path: env"
        assert (ws.root / ".env").read_text(encoding="utf-8") == "SECRET=1\n"

    async def test_apply_patch_touching_protected_denied_as_a_whole(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        patch = (
            "--- /dev/null\n+++ b/.env\n@@ -0,0 +1,1 @@\n+SECRET=1\n"
            "--- /dev/null\n+++ b/ok.txt\n@@ -0,0 +1,1 @@\n+fine\n"
        )
        tools = _tools(session)
        payload = await _run(tools["apply_patch"], patch=patch)
        assert payload["ok"] is False
        assert payload["error"] == "protected path: env"
        assert not (ws.root / ".env").exists()
        assert not (ws.root / "ok.txt").exists()

    async def test_apply_patch_with_traversal_rejected(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        patch = "--- /dev/null\n+++ b/../../escape.txt\n@@ -0,0 +1,1 @@\n+x\n"
        tools = _tools(session)
        payload = await _run(tools["apply_patch"], patch=patch)
        assert payload["ok"] is False
        assert "rejected" in payload["error"]
        assert not (ws.root / "ok.txt").exists()


# ---------------------------------------------------------------------------
# Encoding boundaries: binary refused, never patched as text
# ---------------------------------------------------------------------------


class TestBinaryRefused:
    async def test_edit_of_nul_file_refused(self, ws: Workspace, session: FsWriteSession) -> None:
        original = b"a\x00b\n"
        (ws.root / "a.bin").write_bytes(original)
        tools = _tools(session)
        payload = await _run(tools["edit_file"], path="a.bin", old_str="a", new_str="z")
        assert payload["ok"] is False
        assert "binary" in payload["error"]
        assert (ws.root / "a.bin").read_bytes() == original

    async def test_overwrite_of_non_utf8_refused(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        original = b"\xff\xfe not utf-8\n"
        (ws.root / "a.txt").write_bytes(original)
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="a.txt", content="x\n")
        assert payload["ok"] is False
        assert "UTF-8" in payload["error"]
        assert (ws.root / "a.txt").read_bytes() == original

    async def test_nul_content_refused(self, ws: Workspace, session: FsWriteSession) -> None:
        tools = _tools(session)
        payload = await _run(tools["write_file"], path="a.txt", content="a\x00b\n")
        assert payload["ok"] is False
        assert "binary" in payload["error"]
        assert not (ws.root / "a.txt").exists()


# ---------------------------------------------------------------------------
# Limits: refused and marked, never silently truncated
# ---------------------------------------------------------------------------


class TestLimits:
    async def test_oversized_content_refused(self, ws: Workspace, session: FsWriteSession) -> None:
        tools = _tools(session)
        payload = await _run(
            tools["write_file"], path="a.txt", content="x" * (MAX_CONTENT_BYTES + 1)
        )
        assert payload["ok"] is False
        assert payload["limit_exceeded"] is True
        assert payload["limit"] == str(MAX_CONTENT_BYTES)
        assert not (ws.root / "a.txt").exists()

    async def test_oversized_prior_refused_for_edit(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        big = b"x" * (MAX_CONTENT_BYTES + 1) + b"\n"
        (ws.root / "a.txt").write_bytes(big)
        tools = _tools(session)
        payload = await _run(tools["edit_file"], path="a.txt", old_str="x", new_str="y")
        assert payload["ok"] is False
        assert payload["limit_exceeded"] is True
        assert (ws.root / "a.txt").read_bytes() == big

    async def test_oversized_patch_refused(self, ws: Workspace, session: FsWriteSession) -> None:
        patch = "--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1,1 @@\n+" + ("x" * MAX_PATCH_BYTES) + "\n"
        tools = _tools(session)
        payload = await _run(tools["apply_patch"], patch=patch)
        assert payload["ok"] is False
        assert payload["limit_exceeded"] is True
        assert not (ws.root / "a.txt").exists()


# ---------------------------------------------------------------------------
# apply_patch — caller-supplied diffs
# ---------------------------------------------------------------------------


class TestApplyPatch:
    async def test_create_patch_sha_matches_the_supplied_patch(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        patch = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,1 @@\n+created\n"
        tools = _tools(session)
        payload = await _run(tools["apply_patch"], patch=patch)
        assert payload["ok"] is True
        assert payload["patch_sha256"] == compute_patch_sha256(patch)
        assert (ws.root / "new.txt").read_bytes() == b"created\n"

    async def test_multi_file_patch_applies_all(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        (ws.root / "a.txt").write_bytes(b"a\n")
        patch = (
            "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,2 @@\n a\n+more\n"
            "--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1,1 @@\n+fresh\n"
        )
        tools = _tools(session)
        payload = await _run(tools["apply_patch"], patch=patch)
        assert payload["ok"] is True
        assert payload["files_changed"] == ["a.txt", "b.txt"]
        assert (ws.root / "a.txt").read_bytes() == b"a\nmore\n"
        assert (ws.root / "b.txt").read_bytes() == b"fresh\n"

    async def test_hunk_mismatch_refused_without_write(
        self, ws: Workspace, session: FsWriteSession
    ) -> None:
        original = b"line1\nline2\n"
        (ws.root / "file.txt").write_bytes(original)
        patch = "--- a/file.txt\n+++ b/file.txt\n@@ -1,1 +1,1 @@\n nothing-like-it\n"
        tools = _tools(session)
        payload = await _run(tools["apply_patch"], patch=patch)
        assert payload["ok"] is False
        assert "does not apply cleanly" in payload["error"]
        assert (ws.root / "file.txt").read_bytes() == original
        assert session.journals == []


# ---------------------------------------------------------------------------
# Prior-hash re-check: a plan whose file changed underneath it refuses
# ---------------------------------------------------------------------------


def _modify_patch(path: str = "file.txt") -> str:
    return (
        f"--- a/{path}\n+++ b/{path}\n"
        "@@ -1,2 +1,3 @@\n line1\n line2\n+line3\n"
    )


class TestPriorHashRecheck:
    def test_changed_file_refuses_instead_of_clobbering(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "file.txt"
        target.write_bytes(b"line1\nline2\n")
        plan = UnifiedDiffValidator().dry_run(_modify_patch(), workspace)
        assert plan.ok
        target.write_bytes(b"line1\nline2\nconcurrent\n")
        with pytest.raises(ValidationError) as excinfo:
            PatchApplier().apply(plan)
        assert "stale" in str(excinfo.value)
        assert target.read_bytes() == b"line1\nline2\nconcurrent\n"

    def test_file_created_after_planning_refuses(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        patch = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,1 @@\n+created\n"
        plan = UnifiedDiffValidator().dry_run(patch, workspace)
        assert plan.ok
        (workspace / "new.txt").write_bytes(b"someone else\n")
        with pytest.raises(ValidationError) as excinfo:
            PatchApplier().apply(plan)
        assert "stale" in str(excinfo.value)
        assert (workspace / "new.txt").read_bytes() == b"someone else\n"

    def test_deleted_target_refuses(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "file.txt"
        target.write_bytes(b"line1\nline2\n")
        plan = UnifiedDiffValidator().dry_run(_modify_patch(), workspace)
        assert plan.ok
        target.unlink()
        with pytest.raises(ValidationError) as excinfo:
            PatchApplier().apply(plan)
        assert "stale" in str(excinfo.value)
        assert not target.exists()

    def test_stale_second_file_unwinds_the_first(self, tmp_path: Path) -> None:
        """A staleness discovered mid-apply unwinds files already written
        in that apply — the workspace never holds a half-applied patch."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        first = workspace / "first.txt"
        second = workspace / "second.txt"
        first_original = b"line1\nline2\n"
        first.write_bytes(first_original)
        second.write_bytes(b"other\n")
        patch = _modify_patch("first.txt") + (
            "--- a/second.txt\n+++ b/second.txt\n"
            "@@ -1,1 +1,2 @@\n other\n+more\n"
        )
        plan = UnifiedDiffValidator().dry_run(patch, workspace)
        assert plan.ok, plan.failures()
        second.write_bytes(b"other\nconcurrent\n")
        with pytest.raises(ValidationError) as excinfo:
            PatchApplier().apply(plan)
        assert "rolled back" in str(excinfo.value)
        assert first.read_bytes() == first_original

    def test_unmutated_plan_still_applies(self, tmp_path: Path) -> None:
        """The re-check refuses only real staleness, not clean plans."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "file.txt"
        target.write_bytes(b"line1\nline2\n")
        plan = UnifiedDiffValidator().dry_run(_modify_patch(), workspace)
        assert plan.ok
        PatchApplier().apply(plan)
        assert target.read_bytes() == b"line1\nline2\nline3\n"


# ---------------------------------------------------------------------------
# Naming: the permissions contract N1.5 registers against
# ---------------------------------------------------------------------------


class TestNaming:
    def test_tool_names_exist_and_match_mutating_globs(self) -> None:
        tools = build_fs_write_tools(Workspace(root=Path(".")))
        assert set(tools) == {"write_file", "edit_file", "apply_patch"}
        for name in tools:
            assert any(fnmatch.fnmatch(name, pat) for pat in DEFAULT_MUTATING_TOOLS)

    def test_plan_mode_denies_all_three(self) -> None:
        policy = ToolPermissionPolicy(mode=PermissionMode.PLAN)
        for name in ("write_file", "edit_file", "apply_patch"):
            decision = policy.evaluate(name, {})
            assert decision.outcome is PermissionOutcome.DENY, name

    def test_schemas_are_objects_with_required_fields(self) -> None:
        tools = build_fs_write_tools(Workspace(root=Path(".")))
        expected = {
            "write_file": ["path", "content"],
            "edit_file": ["path", "old_str", "new_str"],
            "apply_patch": ["patch"],
        }
        for name, required in expected.items():
            schema = tools[name].schema()
            assert schema["type"] == "object"
            assert schema["required"] == required
            assert schema["additionalProperties"] is False
