"""Git tools: fixed argv, jail-then-protected, no remote surface, permissions.

Execution tests drive a real ``git`` in a ``tmp_path`` repository (marked
``slow``); setup helpers use plain ``subprocess`` argv to init/config the
repo because the tools under test are never their own fixture. The tools
themselves only ever call the four fixed tuples in ``paa.coder.git_tools``.

The PLAN/ASK matrix is written against the policy N1.5 will build:
``mutating_tools=(*DEFAULT_MUTATING_TOOLS, "git_stage*")`` — the owner fix
additive on top of today's literal (``git_commit`` already matches the
existing ``*_commit*`` glob [VERIFIED src/paa/coder/permissions.py:137]).
Until that literal gains ``git_stage*``, ``git_stage`` alone is PLAN-exempt
under the bare default policy; that gap is N1.5's, not this suite's — the
suite proves the *name* is designed for the pattern.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from paa.coder.git_tools import (
    GIT_MUTATING_TOOLS,
    GIT_READ_ONLY_TOOLS,
    build_git_tools,
)
from paa.coder.loop import AgentLoop, ModelTurn, ToolCall
from paa.coder.permissions import (
    DEFAULT_MUTATING_TOOLS,
    PermissionMode,
    PermissionOutcome,
    ToolPermissionPolicy,
)
from paa.coder.session import Session
from paa.coder.workspace import Workspace

pytestmark = pytest.mark.slow


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(tmp_path: Path) -> Workspace:
    """A real git repository at ``tmp_path/repo`` with local identity."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "--", "seed.txt")
    _git(repo, "commit", "-m", "seed")
    return Workspace(root=repo)


def _head(ws: Workspace) -> str:
    return _git(ws.root, "rev-parse", "HEAD").stdout.strip()


def _payload(text: str) -> dict[str, Any]:
    return json.loads(text)


# ---------------------------------------------------------------------------
# The tool surface: names, schemas, classification constants.
# ---------------------------------------------------------------------------


class TestSurface:
    def test_build_returns_exactly_the_four_tools(self) -> None:
        tools = build_git_tools(Workspace(root=Path("somewhere")))
        assert sorted(tools) == ["git_commit", "git_diff", "git_stage", "git_status"]

    def test_schemas_reject_unknown_arguments(self) -> None:
        tools = build_git_tools(Workspace(root=Path("somewhere")))
        for tool in tools.values():
            assert tool.schema()["additionalProperties"] is False

    def test_classification_constants(self) -> None:
        assert GIT_MUTATING_TOOLS == ("git_stage", "git_commit")
        assert frozenset({"git_status", "git_diff"}) == GIT_READ_ONLY_TOOLS


# ---------------------------------------------------------------------------
# Permissions: PLAN refuses mutating, ASK prompts only for mutating,
# LOCKDOWN (deny-all rules) refuses everything.
# ---------------------------------------------------------------------------


def _policy(mode: PermissionMode, **kw: Any) -> ToolPermissionPolicy:
    """The policy shape N1.5's agent builds once the ``git_stage*`` glob
    lands in the literal: standing-allow the read-only half, extended
    mutating set."""
    return ToolPermissionPolicy(
        mode=mode,
        allow=[*GIT_READ_ONLY_TOOLS, *(kw.pop("allow", []) or [])],
        mutating_tools=(*DEFAULT_MUTATING_TOOLS, "git_stage*"),
        **kw,
    )


class TestPermissions:
    def test_plan_allows_reads_denies_mutating(self) -> None:
        policy = _policy(PermissionMode.PLAN)
        assert policy.evaluate("git_status", {}).allowed
        assert policy.evaluate("git_diff", {}).allowed
        assert policy.evaluate("git_stage", {"paths": ["a"]}).outcome is PermissionOutcome.DENY
        assert policy.evaluate("git_commit", {"message": "x"}).outcome is PermissionOutcome.DENY

    def test_ask_prompts_only_for_mutating(self) -> None:
        policy = _policy(PermissionMode.ASK)
        assert policy.evaluate("git_status", {}).allowed
        assert policy.evaluate("git_diff", {}).allowed
        assert policy.evaluate("git_stage", {"paths": ["a"]}).outcome is PermissionOutcome.ASK
        assert policy.evaluate("git_commit", {"message": "x"}).outcome is PermissionOutcome.ASK

    def test_lockdown_deny_all_refuses_everything(self) -> None:
        """``LOCKDOWN`` in the coder loop is deny-wins at precedence 1: an
        operator lockdown is a deny rule per tool, and no mode/allow can
        override it."""
        policy = ToolPermissionPolicy(mode=PermissionMode.ASK, deny=["git_*"])
        for name in ("git_status", "git_diff", "git_stage", "git_commit"):
            assert policy.evaluate(name, {}).outcome is PermissionOutcome.DENY

    def test_git_commit_matches_default_mutating_globs_today(self) -> None:
        """Under today's literal, git_commit's PLAN refusal already holds
        (matches ``*_commit*``); git_status/git_diff match nothing."""
        import fnmatch

        assert any(fnmatch.fnmatch("git_commit", pat) for pat in DEFAULT_MUTATING_TOOLS)
        assert not any(fnmatch.fnmatch("git_status", pat) for pat in DEFAULT_MUTATING_TOOLS)
        assert not any(fnmatch.fnmatch("git_diff", pat) for pat in DEFAULT_MUTATING_TOOLS)

    def test_git_stage_name_matches_the_proposed_pattern(self) -> None:
        """The owner fix is one glob: ``git_stage*``. The tool name is
        spelled for it (never ``git_add``, which matches zero patterns)."""
        import fnmatch

        assert fnmatch.fnmatch("git_stage", "git_stage*")


class TestPermissionGateEndToEnd:
    """A PLAN-mode loop call never reaches git: HEAD is unchanged."""

    async def test_plan_denied_commit_leaves_head_unchanged(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        head_before = _head(ws)

        class ScriptedModel:
            def __init__(self) -> None:
                self._turns = [
                    ModelTurn(tool_calls=[ToolCall("git_commit", {"message": "evil"})]),
                    ModelTurn(text="done"),
                ]

            async def propose(
                self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
            ) -> ModelTurn:
                return self._turns.pop(0) if self._turns else ModelTurn(text="done")

        loop = AgentLoop(
            ScriptedModel(),
            build_git_tools(ws),
            session=Session(),
            permissions=_policy(PermissionMode.PLAN),
            max_steps=5,
        )
        result = await loop.run("commit whatever")
        assert result.denied_calls == 1
        assert _head(ws) == head_before


# ---------------------------------------------------------------------------
# Read-only execution against a real repository.
# ---------------------------------------------------------------------------


class TestExecution:
    async def test_status_shows_untracked_modified_and_staged(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)
        (ws.root / "a.txt").write_text("hello\n", encoding="utf-8")
        (ws.root / "seed.txt").write_text("seed changed\n", encoding="utf-8")

        before = _payload(await tools["git_status"].run({}))
        assert before["exit_code"] == 0
        assert "?? a.txt" in before["status"]
        assert " M seed.txt" in before["status"]

        await tools["git_stage"].run({"paths": ["a.txt"]})
        after = _payload(await tools["git_status"].run({}))
        assert "A  a.txt" in after["status"]

    async def test_diff_shows_working_tree_change(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)
        (ws.root / "seed.txt").write_text("seed changed\n", encoding="utf-8")

        payload = _payload(await tools["git_diff"].run({}))
        assert payload["exit_code"] == 0
        assert "-seed\n+seed changed" in payload["diff"]
        assert payload["staged"] is False

    async def test_diff_paths_scopes_the_output(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)
        (ws.root / "seed.txt").write_text("alpha\n", encoding="utf-8")
        (ws.root / "b.txt").write_text("bravo\n", encoding="utf-8")

        payload = _payload(await tools["git_diff"].run({"paths": ["seed.txt"]}))
        assert "alpha" in payload["diff"]
        assert "bravo" not in payload["diff"]

    async def test_staged_diff_after_stage(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)
        (ws.root / "x.txt").write_text("new file\n", encoding="utf-8")
        await tools["git_stage"].run({"paths": ["x.txt"]})

        staged = _payload(await tools["git_diff"].run({"staged": True}))
        assert "new file" in staged["diff"]
        unstaged = _payload(await tools["git_diff"].run({}))
        assert "new file" not in unstaged["diff"]

    async def test_stage_then_commit_advances_head(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)
        head_before = _head(ws)
        (ws.root / "work.txt").write_text("work\n", encoding="utf-8")

        staged = _payload(await tools["git_stage"].run({"paths": ["work.txt"]}))
        assert staged["exit_code"] == 0
        assert staged["staged"] == ["work.txt"]

        committed = _payload(await tools["git_commit"].run({"message": "add work"}))
        assert committed["exit_code"] == 0
        assert committed["commit"] == _head(ws)
        assert committed["commit"] != head_before

    async def test_commit_with_nothing_staged_fails_cleanly(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)
        head_before = _head(ws)

        payload = _payload(await tools["git_commit"].run({"message": "nothing"}))
        assert "error" in payload  # a value, not a raise
        assert payload["exit_code"] == 1
        assert _head(ws) == head_before  # no phantom commit

    async def test_empty_repository_refuses_commit_cleanly(self, tmp_path: Path) -> None:
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "Test")
        tools = build_git_tools(Workspace(root=repo))

        payload = _payload(await tools["git_commit"].run({"message": "first"}))
        assert "error" in payload
        assert payload["exit_code"] != 0

    async def test_non_repository_workspace_is_refused(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        tools = build_git_tools(Workspace(root=plain))

        for name in ("git_status", "git_diff", "git_stage", "git_commit"):
            payload = _payload(await tools[name].run({}))
            assert "error" in payload
            assert "not inside a git repository" in payload["error"]

    async def test_workspace_below_repo_root_is_refused(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        sub = ws.root / "sub"
        sub.mkdir()
        tools = build_git_tools(Workspace(root=sub))

        payload = _payload(await tools["git_status"].run({}))
        assert "not the git repository root" in payload["error"]
        assert payload["toplevel"] == str(ws.root.resolve())

    async def test_large_diff_is_truncated_and_marked(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)
        # Commit 200 KB, then rewrite every line: the diff is ~400 KB —
        # far over the 64 KB cap — and must arrive truncated and marked.
        (ws.root / "big.txt").write_text("line\n" * 40_000, encoding="utf-8")
        _git(ws.root, "add", "--", "big.txt")
        _git(ws.root, "commit", "-m", "big")
        (ws.root / "big.txt").write_text("changed\n" * 40_000, encoding="utf-8")

        payload = _payload(await tools["git_diff"].run({}))
        assert payload["exit_code"] == 0
        assert payload["truncated"] is True
        assert len(payload["diff"]) <= 64 * 1024 + 1024


# ---------------------------------------------------------------------------
# Jail then protection: no .git/.env writes, no escapes, all-or-nothing.
# ---------------------------------------------------------------------------


class TestJailAndProtection:
    async def test_staging_git_config_is_protected(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)

        payload = _payload(await tools["git_stage"].run({"paths": [".git/config"]}))
        assert payload["reason"] == "protected path: git"

    async def test_staging_dotenv_is_protected(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        (ws.root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        tools = build_git_tools(ws)

        payload = _payload(await tools["git_stage"].run({"paths": [".env"]}))
        assert payload["reason"] == "protected path: env"

    async def test_staging_escaping_path_is_refused(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)

        payload = _payload(await tools["git_stage"].run({"paths": ["../outside.txt"]}))
        assert "escapes the workspace jail" in payload["error"]

    async def test_batch_is_all_or_nothing(self, tmp_path: Path) -> None:
        """One protected path aborts the batch *before* git runs: the good
        path is not staged either."""
        ws = _make_repo(tmp_path)
        (ws.root / "good.txt").write_text("good\n", encoding="utf-8")
        (ws.root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        tools = build_git_tools(ws)

        payload = _payload(await tools["git_stage"].run({"paths": ["good.txt", ".env"]}))
        assert payload["reason"] == "protected path: env"
        status = _payload(await tools["git_status"].run({}))
        assert "?? good.txt" in status["status"]
        assert "A  good.txt" not in status["status"]

    async def test_diff_cannot_reach_protected_paths(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)

        payload = _payload(await tools["git_diff"].run({"paths": [".git/config"]}))
        assert payload["reason"] == "protected path: git"


# ---------------------------------------------------------------------------
# Hostile input: the argv is fixed, so flags and subcommands smuggled as
# values are data, never options; unknown argument keys never reach git.
# ---------------------------------------------------------------------------


class TestHostileInput:
    async def test_stage_with_dash_pathspec_is_a_file_not_a_flag(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)
        marker = tmp_path / "UPLOAD_PACK_RAN"

        payload = _payload(
            await tools["git_stage"].run({"paths": ["--upload-pack=touch " + str(marker)]})
        )
        assert "error" in payload
        assert not marker.exists()

    async def test_stage_with_force_flag_is_a_file_not_a_flag(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)

        payload = _payload(await tools["git_stage"].run({"paths": ["--force"]}))
        assert "error" in payload

    async def test_commit_message_that_looks_like_amend_is_a_subject(
        self, tmp_path: Path
    ) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)
        (ws.root / "x.txt").write_text("x\n", encoding="utf-8")
        await tools["git_stage"].run({"paths": ["x.txt"]})

        payload = _payload(await tools["git_commit"].run({"message": "--amend"}))
        assert payload["exit_code"] == 0
        subject = _git(ws.root, "log", "-1", "--format=%s").stdout.strip()
        assert subject == "--amend"
        count = _git(ws.root, "rev-list", "--count", "HEAD").stdout.strip()
        assert count == "2"  # seed + one new commit: nothing amended away

    async def test_extra_forbidden_arguments_are_ignored_not_forwarded(
        self, tmp_path: Path
    ) -> None:
        """A model passing push-shaped keys gets them dropped: the commit
        still happens locally and no push ever runs (proven by the bare
        remote staying empty in TestNoRemoteSurface)."""
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)
        (ws.root / "x.txt").write_text("x\n", encoding="utf-8")
        await tools["git_stage"].run({"paths": ["x.txt"]})

        payload = _payload(
            await tools["git_commit"].run(
                {"message": "local only", "remote": "origin", "refspec": "main:main"}
            )
        )
        assert payload["exit_code"] == 0
        assert _head(ws) != ""
        assert "remote" not in payload and "refspec" not in payload

    async def test_hooks_never_execute(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        marker = tmp_path / "HOOK_RAN"
        hook = ws.root / ".git" / "hooks" / "post-commit"
        # Forward slashes so the sh script parses the path on Windows Git Bash.
        hook.write_text(f"#!/bin/sh\ntouch '{marker.as_posix()}'\n", encoding="utf-8")
        failing = ws.root / ".git" / "hooks" / "pre-commit"
        failing.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        tools = build_git_tools(ws)
        (ws.root / "x.txt").write_text("x\n", encoding="utf-8")
        await tools["git_stage"].run({"paths": ["x.txt"]})

        payload = _payload(await tools["git_commit"].run({"message": "no hooks"}))
        assert payload["exit_code"] == 0  # the failing pre-commit hook did not gate us
        assert not marker.exists()  # and post-commit never ran

    async def test_ambient_git_env_is_never_inherited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GIT_DIR`` pointing at a bogus repository (and ``GIT_SSH_COMMAND``)
        must not redirect or run anything: the child environment is
        allowlisted, so git sees neither."""
        ws = _make_repo(tmp_path)
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "bogus.git"))
        monkeypatch.setenv("GIT_SSH_COMMAND", "touch " + str(tmp_path / "SSH_RAN"))
        tools = build_git_tools(ws)

        (ws.root / "x.txt").write_text("x\n", encoding="utf-8")
        payload = _payload(await tools["git_status"].run({}))
        assert "?? x.txt" in payload["status"]  # our repo, not GIT_DIR's
        assert not (tmp_path / "SSH_RAN").exists()


# ---------------------------------------------------------------------------
# No remote surface: a configured remote stays untouched.
# ---------------------------------------------------------------------------


class TestNoRemoteSurface:
    async def test_commit_never_reaches_the_remote(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        bare = tmp_path / "remote.git"
        _git(tmp_path, "init", "--bare", str(bare))
        _git(ws.root, "remote", "add", "origin", str(bare))
        tools = build_git_tools(ws)
        (ws.root / "x.txt").write_text("x\n", encoding="utf-8")
        await tools["git_stage"].run({"paths": ["x.txt"]})

        payload = _payload(await tools["git_commit"].run({"message": "local"}))
        assert payload["exit_code"] == 0

        bare_refs = await asyncio.to_thread(
            lambda: subprocess.run(
                ["git", f"--git-dir={bare}", "for-each-ref"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
        assert bare_refs == ""  # the remote gained nothing: no push happened
        assert payload["commit"] == _head(ws)


# ---------------------------------------------------------------------------
# Argument validation.
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    async def test_stage_requires_at_least_one_path(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)

        payload = _payload(await tools["git_stage"].run({"paths": []}))
        assert "error" in payload

    async def test_stage_rejects_blank_and_nonstring_paths(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)

        assert "error" in _payload(await tools["git_stage"].run({"paths": [""]}))
        assert "error" in _payload(await tools["git_stage"].run({"paths": ["  "]}))
        assert "error" in _payload(await tools["git_stage"].run({"paths": [123]}))

    async def test_stage_rejects_too_many_paths(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)

        payload = _payload(await tools["git_stage"].run({"paths": [f"f{i}" for i in range(101)]}))
        assert "error" in payload

    async def test_commit_requires_message(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)

        assert "error" in _payload(await tools["git_commit"].run({}))
        assert "error" in _payload(await tools["git_commit"].run({"message": "  "}))

    async def test_commit_rejects_overlong_message(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)

        payload = _payload(await tools["git_commit"].run({"message": "x" * 2049}))
        assert "error" in payload

