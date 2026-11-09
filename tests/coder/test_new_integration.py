"""N1.5 integration: registration + permissions (the BLOCKER gate for wiring).

Two locks, both of which fail if the wiring forgets a mutating tool:

1. **Mutating coverage.** Every mutating tool name is classified by exactly
   one of the policy's two nets — a :data:`~paa.coder.permissions.DEFAULT_MUTATING_TOOLS`
   glob or the :data:`~paa.coder.agent.MUTATING_RUNTIME_TOOLS` tuple. A
   mutating name matching neither becomes PLAN-exempt by accident (the
   ``git_add`` accident of plan B-3, and the latent ``git_stage`` half of
   W1-REV-03 MAJOR-3). The test derives the mutating set the way the policy
   does (glob match or tuple membership) over the real catalogue names, then
   pins its *value* — so a new mutating tool added anywhere fails here until
   a human classifies it.
2. **Read-only union.** :data:`~paa.coder.runtime_tools.READ_ONLY_TOOLS` is
   exactly the eleven read names (six runtime reads plus the
   filesystem-read and git-read halves), so the ASK standing-allow in
   ``CoderAgent.build`` and the specialist pin compare the same set.

Plus the N1.5 policy matrix (PLAN denies every mutating name; ASK
standing-allows every read and prompts for every mutating name) and one real
``CoderAgent.build`` with a workspace proving the five catalogues merge to
the twenty-name tools dict.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from paa.agents.specialists.coding import CODING_TOOL_NAMES
from paa.coder.agent import MUTATING_RUNTIME_TOOLS, CoderAgent
from paa.coder.fs_read_tools import FS_READ_TOOLS, build_fs_read_tools
from paa.coder.fs_write_tools import build_fs_write_tools
from paa.coder.git_tools import GIT_MUTATING_TOOLS, GIT_READ_ONLY_TOOLS, build_git_tools
from paa.coder.loop import ModelTurn
from paa.coder.permissions import (
    DEFAULT_MUTATING_TOOLS,
    PermissionMode,
    PermissionOutcome,
    ToolPermissionPolicy,
)
from paa.coder.runtime_tools import READ_ONLY_TOOLS
from paa.coder.workspace import Workspace

#: The nine mutating names, spelled out as literals (not imported) so the
#: reader sees the full policy surface without opening three modules. The
#: runtime triple matches no glob; the six work-tool names each match one
#: (including ``git_stage`` under the N1.5 ``git_stage*`` glob).
EXPECTED_MUTATING: frozenset[str] = frozenset(
    {
        "submit_task",
        "run_task",
        "remember_fact",
        "write_file",
        "edit_file",
        "apply_patch",
        "bash",
        "git_stage",
        "git_commit",
    }
)

#: The eleven read-only names: six runtime reads plus the filesystem-read
#: and git-read halves ``READ_ONLY_TOOLS`` aggregates.
EXPECTED_READ_ONLY: frozenset[str] = frozenset(
    {
        "check_runtime",
        "list_tasks",
        "task_status",
        "read_ledger",
        "verify_ledger",
        "recall_facts",
        "read_file",
        "glob",
        "grep",
        "git_status",
        "git_diff",
    }
)


def _matches_any_glob(name: str, globs: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in globs)


def _policy(mode: PermissionMode, **kw: Any) -> ToolPermissionPolicy:
    """The policy *exactly as* :meth:`CoderAgent.build` constructs it."""

    return ToolPermissionPolicy(
        mode=mode,
        allow=[*READ_ONLY_TOOLS, *(kw.pop("allow", []) or [])],
        deny=kw.pop("deny", None),
        mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS),
        **kw,
    )


class TestMutatingCoverage:
    def test_every_mutating_tool_is_covered_by_policy(self) -> None:
        """The coverage lock: every mutating name matches a glob or the tuple.

        The mutating set is derived over the specialist's offered surface
        (the full 20-name catalogue) the same way the policy derives it, then
        pinned by value. A mutating tool matching neither net fails, loudly,
        until a human classifies it.
        """
        mutating = {
            n
            for n in CODING_TOOL_NAMES
            if _matches_any_glob(n, DEFAULT_MUTATING_TOOLS) or n in MUTATING_RUNTIME_TOOLS
        }
        assert mutating == EXPECTED_MUTATING

    def test_each_expected_mutating_matches_directly(self) -> None:
        """No name in the pin escapes both nets (defence in depth)."""
        for name in sorted(EXPECTED_MUTATING):
            assert _matches_any_glob(name, DEFAULT_MUTATING_TOOLS) or (
                name in MUTATING_RUNTIME_TOOLS
            ), f"{name} escapes both nets"

    def test_git_stage_matches_default_mutating_globs(self) -> None:
        """W1-REV-03 MAJOR-3 regression guard, against the real literal.

        Under the pre-N1.5 literal ``git_stage`` was PLAN-*allowed*; the
        ``git_stage*`` glob is what makes the PLAN refusal hold.
        """
        assert _matches_any_glob("git_stage", DEFAULT_MUTATING_TOOLS)
        assert _policy(PermissionMode.PLAN).evaluate("git_stage", {}).outcome is (
            PermissionOutcome.DENY
        )

    def test_git_commit_matches_default_mutating_globs(self) -> None:
        assert _matches_any_glob("git_commit", DEFAULT_MUTATING_TOOLS)
        assert _policy(PermissionMode.PLAN).evaluate("git_commit", {}).outcome is (
            PermissionOutcome.DENY
        )

    def test_work_catalogues_partition_into_reads_and_mutating(self) -> None:
        """Built keys, not recalled names: every factory tool is classified.

        Construction is side-effect free (each factory only binds classes to
        the workspace). A tool that is neither a known read nor a
        glob-matched mutating name is precisely the read-only-by-accident
        bug, so the unclassified remainder must be empty.
        """
        workspace = Workspace(root=Path("somewhere"))
        work_names: set[str] = set()
        work_names |= set(build_fs_read_tools(workspace))
        work_names |= set(build_fs_write_tools(workspace))
        work_names |= set(build_git_tools(workspace))
        work_names.add("bash")  # the shell factory needs a sandbox; its name is frozen

        assert work_names & set(READ_ONLY_TOOLS) == {
            "read_file",
            "glob",
            "grep",
            "git_status",
            "git_diff",
        }
        assert FS_READ_TOOLS <= READ_ONLY_TOOLS
        assert GIT_READ_ONLY_TOOLS <= READ_ONLY_TOOLS
        assert set(GIT_MUTATING_TOOLS) == {"git_stage", "git_commit"}
        for name in sorted(work_names):
            in_reads = name in READ_ONLY_TOOLS
            is_mutating = (
                _matches_any_glob(name, DEFAULT_MUTATING_TOOLS)
                or name in MUTATING_RUNTIME_TOOLS
            )
            assert in_reads != is_mutating, f"{name} is misclassified"


class TestReadOnlyUnion:
    def test_read_only_tools_are_exactly_the_eleven_reads(self) -> None:
        assert READ_ONLY_TOOLS == EXPECTED_READ_ONLY

    def test_read_only_tools_are_not_mutating(self) -> None:
        for name in sorted(READ_ONLY_TOOLS):
            assert not _matches_any_glob(name, DEFAULT_MUTATING_TOOLS), name
            assert name not in MUTATING_RUNTIME_TOOLS, name

    def test_specialist_read_half_equals_read_only_tools(self) -> None:
        read_half = set(CODING_TOOL_NAMES) - EXPECTED_MUTATING
        assert read_half == set(READ_ONLY_TOOLS)


class TestPolicyMatrix:
    def test_plan_allows_reads_denies_mutating(self) -> None:
        policy = _policy(PermissionMode.PLAN)
        for name in sorted(EXPECTED_READ_ONLY):
            assert policy.evaluate(name, {}).allowed, name
        for name in sorted(EXPECTED_MUTATING):
            assert policy.evaluate(name, {}).outcome is PermissionOutcome.DENY, name

    def test_ask_standing_allows_reads_prompts_mutating(self) -> None:
        """ASK never prompts for a read; it prompts (never silently
        allows) for every mutating call."""
        policy = _policy(PermissionMode.ASK)
        for name in sorted(EXPECTED_READ_ONLY):
            decision = policy.evaluate(name, {})
            assert decision.allowed and not decision.needs_prompt, name
        for name in sorted(EXPECTED_MUTATING):
            decision = policy.evaluate(name, {})
            assert decision.needs_prompt and decision.outcome is PermissionOutcome.ASK, name

    def test_bypass_still_denies_explicitly_denied(self) -> None:
        """Precedence step 1 under the N1.5 shape: an operator lockdown is a
        deny rule, and no mode or standing allow overrides it."""
        policy = _policy(PermissionMode.BYPASS, deny=["git_*", "bash"])
        for name in ("git_status", "git_diff", "git_stage", "git_commit", "bash"):
            assert policy.evaluate(name, {}).outcome is PermissionOutcome.DENY, name


class _ScriptedModel:
    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        return ModelTurn(text="done")


class TestBuildMerge:
    async def test_build_with_workspace_merges_all_twenty_tools(
        self, tmp_path: Path
    ) -> None:
        from paa.config import Settings

        home = tmp_path / "paa_home"
        home.mkdir(parents=True, exist_ok=True)
        settings = Settings(home=home)
        settings.ensure_directories()
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        agent = await CoderAgent.build(
            settings,
            model=_ScriptedModel(),
            mode=PermissionMode.BYPASS,
            workspace=Workspace(root=workspace),
        )
        try:
            assert set(agent.tools) == set(CODING_TOOL_NAMES)
            assert set(agent.tools) == EXPECTED_MUTATING | EXPECTED_READ_ONLY
            assert len(agent.tools) == 20
        finally:
            await agent.close()

    async def test_build_without_workspace_offers_runtime_tools_only(
        self, tmp_path: Path
    ) -> None:
        from paa.config import Settings

        home = tmp_path / "paa_home"
        home.mkdir(parents=True, exist_ok=True)
        settings = Settings(home=home)
        settings.ensure_directories()

        agent = await CoderAgent.build(
            settings,
            model=_ScriptedModel(),
            mode=PermissionMode.BYPASS,
        )
        try:
            assert set(agent.tools) == {
                "check_runtime",
                "list_tasks",
                "task_status",
                "read_ledger",
                "verify_ledger",
                "recall_facts",
                "submit_task",
                "run_task",
                "remember_fact",
            }
        finally:
            await agent.close()
