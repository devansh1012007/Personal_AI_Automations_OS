"""Host-side git tools must never execute commands that repository config names.

``git_status`` / ``git_diff`` / ``git_stage`` / ``git_commit`` run ``git`` on the
HOST (isolation NONE), not in the sandbox. Git will happily run programs named in
the repository's own config and attributes. If anything inside the workspace can
write that config, the sandbox boundary is bypassed by the next host-side git call.

Who can write it? ``bash`` can: the protected-path rule (``.git``, ``.env*``) is only
enforced by the fs tools. Under a containment backend (Docker, namespaces) this is a
container escape. Under the default subprocess backend ``bash`` already has the user's
full privileges, so the mechanism is demonstrated but not itself an escalation.

These tests plant the config directly, so they hold regardless of what wrote it.

EXPECTED STATE ON ``main`` @ 5e0a0d8:
    test_fsmonitor_not_executed      FAIL   (marker created on the host)
    test_clean_filter_not_executed   FAIL   (marker created on the host)
    test_commit_hook_not_executed    PASS   (core.hooksPath= already neutralises hooks)

Drop into ``tests/coder/``. After the fix (ROADMAP task A1), all three must pass and
``test_refusal_is_explicit`` must pass too.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from paa.coder.git_tools import build_git_tools
from paa.coder.workspace import Workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed argv in a test
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(root.parent)},
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("PAA_HOME", str(tmp_path / "paa_home"))
    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "init")
    return root.resolve(), tmp_path / "marker"


def _run(tools: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    return asyncio.run(tools[name].run(args))


def test_fsmonitor_not_executed(repo: tuple[Path, Path]) -> None:
    root, marker = repo
    _git(root, "config", "core.fsmonitor", f"touch {marker}; echo")
    tools = build_git_tools(Workspace(root=root))
    try:
        _run(tools, "git_status", {})
    except Exception:  # a refusal is acceptable; execution is not
        pass
    assert not marker.exists(), "git_status executed core.fsmonitor on the host"


def test_clean_filter_not_executed(repo: tuple[Path, Path]) -> None:
    root, marker = repo
    (root / ".gitattributes").write_text("*.txt filter=probe\n", encoding="utf-8")
    _git(root, "config", "filter.probe.clean", f"touch {marker}; cat")
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    tools = build_git_tools(Workspace(root=root))
    try:
        _run(tools, "git_stage", {"paths": ["a.txt"]})
    except Exception:
        pass
    assert not marker.exists(), "git_stage executed a clean filter on the host"


def test_commit_hook_not_executed(repo: tuple[Path, Path]) -> None:
    """Control: this one is already safe on main (core.hooksPath= is forced empty)."""
    root, marker = repo
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    tools = build_git_tools(Workspace(root=root))
    _run(tools, "git_stage", {"paths": ["a.txt"]})
    _run(tools, "git_commit", {"message": "x"})
    assert not marker.exists(), "git_commit ran a repository hook"


def test_refusal_is_explicit(repo: tuple[Path, Path]) -> None:
    """After the fix: tampered config yields a clear refusal, not silent success."""
    root, marker = repo
    _git(root, "config", "core.fsmonitor", f"touch {marker}; echo")
    tools = build_git_tools(Workspace(root=root))
    with pytest.raises(Exception) as err:
        _run(tools, "git_status", {})
    assert "config" in str(err.value).lower() or "refus" in str(err.value).lower()
    assert not marker.exists()
