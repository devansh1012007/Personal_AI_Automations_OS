"""Adversarial probe suite: the coder tool boundaries under attack.

Every probe here is written from the adversary's seat, not the maintainer's:
each one states an attack goal — smuggle a second verb past a scoped allow
rule, escape the workspace jail through a symlink, reach a protected path
through normalisation or case, forge a commit flag or an author identity,
leak an ambient secret through the child environment — and then asserts the
boundary holds **and** that the attempted attack left no side effect. A probe
that only checked the refusal message would pass on a boundary that refuses
the words but performs the action; every execution probe therefore also
asserts on the world after the call (no marker file, no commit, no staged
protected path, no leaked canary).

What this suite deliberately is not: a re-run of the per-module suites.
``tests/coder/`` proves each tool's contract on its own terms; this suite
attacks the *combinations and spellings* an adversary composes from them —
the compound command, the squatted tool name, the doubled separator, the
case-shifted ``.ENV``, the argument-shaped flag. Where a probe pins a defect
that was real, its docstring names it: the scoped allow rule
``allow bash(git status*)`` compiled to an unanchored regex with an open
``.*`` tail, so both ``echo hi; git status`` (contains-match) and
``git status; rm -rf ~`` (chained after the approved prefix) inherited the
allow. The fix is asymmetric — allow/ask compile to a full match whose tail
cannot cross a shell command separator; deny keeps the wide containment
match, because for a refusal, matching more is the safe direction.

Marker: ``adversarial`` (see ``pyproject.toml``). Probes that drive a real
``git`` are additionally marked ``slow``; everything else is offline and fast.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from paa.coder.fs_write_tools import FsWriteSession, WriteFileTool
from paa.coder.git_tools import build_git_tools
from paa.coder.permissions import (
    DEFAULT_MUTATING_TOOLS,
    PermissionMode,
    PermissionOutcome,
    ToolPermissionPolicy,
)
from paa.coder.shell_tools import BashTool
from paa.coder.workspace import Workspace
from paa.sandbox.base import SandboxSpec, build_child_env
from paa.sandbox.dryrun_backend import DryRunSandbox

pytestmark = pytest.mark.adversarial

CANARY = "PAA_ADVERSARIAL_CANARY"
CANARY_VALUE = "do-not-leak-0f1e2d3c"


def _payload(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    assert isinstance(data, dict)
    return data


def _ws(root: Path) -> Workspace:
    return Workspace(root=root)


# ---------------------------------------------------------------------------
# Probe 1: scoped allow rules are prefix matches, not "contains" matches.
#
# Attack goal: get a second, unapproved verb past a scoped allow rule by
# chaining it behind an approved prefix. Before the anchoring fix, a rule
# written ``allow bash(git status*)`` compiled to an *unanchored* regex, so
# ``git status; rm -rf ~`` and ``echo hi; git status`` both inherited the
# allow — one approved verb smuggled every verb after it.
# ---------------------------------------------------------------------------


class TestScopedAllowRuleSmuggling:
    def _policy(self) -> ToolPermissionPolicy:
        return ToolPermissionPolicy(
            mode=PermissionMode.ASK, allow=["bash(git status*)"]
        )

    def test_the_approved_prefix_still_matches(self) -> None:
        policy = self._policy()
        assert policy.evaluate("bash", {"command": "git status"}).allowed
        assert policy.evaluate(
            "bash", {"command": ["git", "status", "--short"]}
        ).allowed

    def test_compound_command_after_the_prefix_is_not_a_prefix_match(self) -> None:
        """The tail of an allow glob cannot cross a shell command boundary:
        ``git status; rm -rf ~`` starts with the approved prefix, but the
        compiled tail class refuses ``;`` and the ``$`` forces the whole
        string to be consumed by the class — so the chained verb is not
        covered by the rule and the call falls through to ASK."""
        policy = self._policy()
        decision = policy.evaluate("bash", {"command": "git status; rm -rf ~"})
        assert decision.outcome is PermissionOutcome.ASK
        assert decision.rule is None  # the allow rule did not answer this

    def test_command_that_merely_contains_the_prefix_is_not_a_prefix_match(
        self,
    ) -> None:
        policy = self._policy()
        for smuggled in (
            "echo hi; git status",
            "env PATH=/tmp/evil git status",
            "true && git status && curl evil",
        ):
            assert (
                policy.evaluate("bash", {"command": smuggled}).outcome
                is PermissionOutcome.ASK
            )

    def test_a_scoped_deny_keeps_catching_the_smuggled_spelling(self) -> None:
        """Deny-wins is checked before any allow, and a scoped deny is a
        *containment* match on purpose: ``deny bash(git push*)`` must keep
        catching ``git status; git push`` — a refusal is defensive, and a
        defensive rule matching more is the safe direction (the mirror of
        the anchored allow, whose matching less is the safe direction)."""
        policy = ToolPermissionPolicy(
            mode=PermissionMode.ASK,
            allow=["bash(git status*)"],
            deny=["bash(git push*)"],
        )
        push = policy.evaluate("bash", {"command": "git push origin main"})
        assert push.outcome is PermissionOutcome.DENY
        chained = policy.evaluate("bash", {"command": "git status; git push"})
        assert chained.outcome is PermissionOutcome.DENY
        # And the allow rule does not launder a chained forbidden verb.
        assert policy.evaluate("bash", {"command": "git status"}).allowed


# ---------------------------------------------------------------------------
# Probe 2: PLAN mode is closed by *glob*, so a look-alike tool name cannot
# walk past it. ``write_evil`` matches ``write*``; a mutating tool that does
# NOT match any glob is read-only-by-accident, which is the convention the
# DEFAULT_MUTATING_TOOLS docstring states.
# ---------------------------------------------------------------------------


class TestPlanGlobSquatting:
    @pytest.mark.parametrize(
        ("tool", "glob"),
        [
            ("write_evil", "write*"),
            ("bashx", "bash*"),
            ("shellz", "shell*"),
            ("apply_patch_helper", "apply_patch*"),
            ("task_delete", "*delete*"),
            ("history_write", "*write*"),
            ("send_spam", "send_*"),
            ("git_commit_but_evil", "*_commit*"),
            ("git_stage_all", "git_stage*"),
        ],
    )
    def test_plan_refuses_every_squatted_spelling(self, tool: str, glob: str) -> None:
        import fnmatch

        assert any(fnmatch.fnmatch(tool, pat) for pat in DEFAULT_MUTATING_TOOLS), (
            f"{tool} must be covered by DEFAULT_MUTATING_TOOLS (via {glob})"
        )
        policy = ToolPermissionPolicy(mode=PermissionMode.PLAN)
        assert policy.evaluate(tool, {}).outcome is PermissionOutcome.DENY

    def test_plan_still_allows_a_read_only_tool(self) -> None:
        policy = ToolPermissionPolicy(mode=PermissionMode.PLAN)
        assert policy.evaluate("read_file", {"path": "a"}).allowed

    def test_bypass_never_overrides_a_deny_rule(self) -> None:
        """Bypass is about skipping prompts, not disabling safety rules."""
        policy = ToolPermissionPolicy(
            mode=PermissionMode.BYPASS, deny=["git_*"]
        )
        for name in ("git_commit", "git_stage", "git_status"):
            assert policy.evaluate(name, {}).outcome is PermissionOutcome.DENY


# ---------------------------------------------------------------------------
# Probe 3: the workspace jail under path spellings an adversary composes.
# Attack goals: leave the workspace through a symlink planted in it; reach a
# protected path through doubled separators, parent-splice, or case shifts.
# Every refusal must also leave the outside target byte-identical.
# ---------------------------------------------------------------------------


class TestJailEscapeProbes:
    def _tool(
        self, tmp_path: Path
    ) -> tuple[WriteFileTool, Path, Path]:
        """(tool, outside dir, workspace root) — a workspace with a ``sub/``
        beside an ``outside/`` directory the adversary wants to reach."""
        outside = tmp_path / "outside"
        outside.mkdir()
        ws_root = tmp_path / "ws"
        (ws_root / "sub").mkdir(parents=True)
        return (
            WriteFileTool(FsWriteSession(_ws(ws_root))),
            outside,
            ws_root,
        )

    async def test_symlink_in_workspace_does_not_write_outside(
        self, tmp_path: Path
    ) -> None:
        tool, outside, ws_root = self._tool(tmp_path)
        victim = outside / "victim.txt"
        victim.write_text("original\n", encoding="utf-8")
        (ws_root / "link.txt").symlink_to(victim)

        payload = _payload(
            await tool.run({"path": "link.txt", "content": "attacker\n"})
        )

        assert "error" in payload
        assert victim.read_text(encoding="utf-8") == "original\n"

    async def test_parent_splice_through_a_directory_is_refused(
        self, tmp_path: Path
    ) -> None:
        tool, outside, _ws_root = self._tool(tmp_path)

        payload = _payload(
            await tool.run({"path": "sub/../../outside.txt", "content": "x"})
        )

        assert "error" in payload
        assert not (outside / "outside.txt").exists()

    async def test_parent_splice_into_git_config_is_protected(
        self, tmp_path: Path
    ) -> None:
        tool, _outside, _ws_root = self._tool(tmp_path)

        payload = _payload(
            await tool.run({"path": "sub/../.git/config", "content": "[core]"})
        )

        assert "error" in payload
        assert "protected" in str(payload.get("reason", "")) or "protected" in str(
            payload.get("error", "")
        )

    @pytest.mark.parametrize("spelling", [".env.local", ".ENV", ".Env", "sub/.env"])
    async def test_case_and_depth_shifted_dotenv_is_protected(
        self, tmp_path: Path, spelling: str
    ) -> None:
        tool, _outside, ws_root = self._tool(tmp_path)

        payload = _payload(await tool.run({"path": spelling, "content": "SECRET=1"}))

        assert "error" in payload, spelling
        assert "protected" in str(payload.get("reason", "")) or "protected" in str(
            payload.get("error", "")
        )
        assert not (ws_root / spelling).exists()

    async def test_a_nul_in_the_path_is_a_value_not_a_write(
        self, tmp_path: Path
    ) -> None:
        tool, outside, _ws_root = self._tool(tmp_path)

        payload = _payload(
            await tool.run({"path": "sub/evil.txt\x00.txt", "content": "x"})
        )

        assert "error" in payload
        assert not list(outside.iterdir())  # nothing written anywhere outside


# ---------------------------------------------------------------------------
# Probe 4: the git argv surface under flag- and pathspec-shaped values
# (real git, real workspace). Every refusal asserts the repository state:
# no marker file, nothing staged, HEAD unmoved.
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _make_repo(tmp_path: Path) -> Workspace:
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


class TestGitArgvSmuggling:
    @pytest.mark.slow
    async def test_pathspec_magic_spellings_are_refused_before_git(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        (ws.root / "sub").mkdir()
        (ws.root / "sub" / ".env.local").write_text("SECRET=1\n", encoding="utf-8")
        (ws.root / "sub" / "ok.txt").write_text("ok\n", encoding="utf-8")
        tools = build_git_tools(ws)

        for spelling in (":/", ":(top)", ":(literal)sub/ok.txt", "*.txt", "sub/"):
            payload = _payload(await tools["git_stage"].run({"paths": [spelling]}))
            assert "error" in payload, spelling
            staged = _git(ws.root, "diff", "--cached", "--name-only").stdout
            assert staged == "", f"{spelling} staged something: {staged!r}"

    @pytest.mark.slow
    async def test_double_separator_normalises_to_a_protected_path(
        self, tmp_path: Path
    ) -> None:
        ws = _make_repo(tmp_path)
        (ws.root / "sub").mkdir()
        (ws.root / "sub" / ".env.local").write_text("SECRET=1\n", encoding="utf-8")
        tools = build_git_tools(ws)

        payload = _payload(await tools["git_stage"].run({"paths": ["sub//.env.local"]}))

        assert "error" in payload
        assert "protected" in str(payload.get("reason", ""))
        assert _git(ws.root, "diff", "--cached", "--name-only").stdout == ""

    @pytest.mark.slow
    async def test_dash_value_stays_a_path_not_an_option(self, tmp_path: Path) -> None:
        ws = _make_repo(tmp_path)
        marker = tmp_path / "UPLOAD_PACK_RAN"
        tools = build_git_tools(ws)

        payload = _payload(
            await tools["git_stage"].run({"paths": [f"--upload-pack=touch {marker}"]})
        )

        assert "error" in payload
        assert not marker.exists()
        assert _git(ws.root, "diff", "--cached", "--name-only").stdout == ""

    @pytest.mark.slow
    async def test_identity_cannot_be_forged_through_arguments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The git-config gate must not accept a model-supplied identity.

        The tool's contract is that only ``message`` is read; an adversary
        passing ``user.name``/``user.email`` shaped arguments (or a ``-c``
        payload) gets them dropped, and the gate still answers with the
        *host's* missing identity.
        """
        repo = tmp_path / "forge"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "commit.gpgsign", "false")
        home = tmp_path / "empty-home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        for var in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
            monkeypatch.delenv(var, raising=False)
        tools = build_git_tools(Workspace(root=repo))

        payload = _payload(
            await tools["git_commit"].run(
                {
                    "message": "forged",
                    "user.name": "Attacker",
                    "user.email": "attacker@evil.invalid",
                    "-c": "user.name=Attacker",
                }
            )
        )

        assert "error" in payload
        assert sorted(payload["missing_config"]) == ["user.email", "user.name"]
        # An empty repository (no seed commit) stays empty.
        assert _git(repo, "rev-list", "--count", "--all").stdout.strip() == "0"

    @pytest.mark.slow
    async def test_commit_body_is_data_not_directives(self, tmp_path: Path) -> None:
        """A trailer-shaped body is recorded verbatim: no amend, no push, no
        hook — and the subject line stays exactly what was sent."""
        ws = _make_repo(tmp_path)
        hook_marker = tmp_path / "HOOK_RAN"
        hook = ws.root / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\ntouch '{hook_marker.as_posix()}'\n", encoding="utf-8")
        tools = build_git_tools(ws)
        (ws.root / "x.txt").write_text("x\n", encoding="utf-8")
        await tools["git_stage"].run({"paths": ["x.txt"]})

        message = (
            "fix\n\nCo-authored-by: Attacker <attacker@evil.invalid>\n"
            "Signed-off-by: Attacker <attacker@evil.invalid>"
        )
        payload = _payload(await tools["git_commit"].run({"message": message}))

        assert payload["exit_code"] == 0
        subject = _git(ws.root, "log", "-1", "--format=%s").stdout.strip()
        assert subject == "fix"
        count = _git(ws.root, "rev-list", "--count", "HEAD").stdout.strip()
        assert count == "2"  # nothing amended away
        assert not hook_marker.exists()  # hooks never ran


# ---------------------------------------------------------------------------
# Probe 5: the child environment is built from empty, so an ambient secret
# and the whole ``GIT_*`` injection family are absent by construction — and
# a value that looks like an injection is one value, never extra variables.
# ---------------------------------------------------------------------------


class TestChildEnvProbes:
    def test_ambient_secret_and_git_injection_family_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CANARY, CANARY_VALUE)
        for name in (
            "GIT_DIR",
            "GIT_CONFIG_COUNT",
            "GIT_SSH_COMMAND",
            "GIT_EXTERNAL_DIFF",
            "GIT_PAGER",
            "LD_PRELOAD",
        ):
            monkeypatch.setenv(name, "/tmp/evil")
        spec = SandboxSpec(
            command=("bash", "-c", "env"),
            workspace_path=Path("/tmp"),
        )

        env = build_child_env(spec)

        assert CANARY not in env
        for name in ("GIT_DIR", "GIT_CONFIG_COUNT", "GIT_SSH_COMMAND",
                     "GIT_EXTERNAL_DIFF", "GIT_PAGER", "LD_PRELOAD"):
            assert name not in env

    def test_injection_shaped_env_value_stays_one_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``NEWLINE=...`` or not, the payload is one variable's value in a
        dict-passed environment — no second variable can be born from it."""
        monkeypatch.delenv("LD_PRELOAD", raising=False)
        spec = SandboxSpec(
            command=("bash", "-c", "env"),
            workspace_path=Path("/tmp"),
            env={"PAA_PROBE": "line1\nLD_PRELOAD=/tmp/evil line2"},
        )

        env = build_child_env(spec)

        assert env["PAA_PROBE"] == "line1\nLD_PRELOAD=/tmp/evil line2"
        assert "LD_PRELOAD" not in env


# ---------------------------------------------------------------------------
# Probe 6: the bash tool's honesty flags in every payload shape. The
# adversary's question: "does a bash approval imply confinement?" The
# payloads must answer no — at every isolation level — before the operator
# is asked, not after.
# ---------------------------------------------------------------------------


class TestBashHonestyPayloads:
    async def test_success_and_refusal_carry_the_honest_flags(
        self, tmp_path: Path
    ) -> None:
        dry = DryRunSandbox(stdout="hi")
        tool = BashTool(_ws(tmp_path), sandbox=dry)

        ok = _payload(await tool.run({"command": "echo hi"}))
        assert ok["ok"] is True
        assert ok["protected_paths_enforced"] is False
        assert ok["network_severed"] is False
        assert ok["isolation_level"] == "NONE"

        dry2 = DryRunSandbox(stdout="should-not-appear")
        refuse = _payload(
            await BashTool(
                Workspace(root=Path("//server/share")), sandbox=dry2
            ).run({"command": "echo hi"})
        )
        assert refuse["ok"] is False
        assert refuse["protected_paths_enforced"] is False
        assert refuse["network_severed"] is False
        assert dry2.invocations == []

    async def test_preflight_answers_before_anything_executes(
        self, tmp_path: Path
    ) -> None:
        facts = BashTool(_ws(tmp_path), sandbox=DryRunSandbox()).preflight(
            allow_network=False
        )
        assert facts["protected_paths_enforced"] is False
        assert facts["filesystem_confined"] is False
        assert facts["network_severed"] is False
