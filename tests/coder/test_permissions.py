"""Tool-permission policy: modes, rules, and the deny-wins precedence."""

from __future__ import annotations

from paa.coder.permissions import (
    PermissionMode,
    PermissionOutcome,
    PermissionRule,
    ToolPermissionPolicy,
)


class TestModes:
    def test_ask_mode_asks_by_default(self) -> None:
        p = ToolPermissionPolicy(mode=PermissionMode.ASK)
        assert p.evaluate("bash", {"command": "ls"}).outcome is PermissionOutcome.ASK

    def test_bypass_allows_by_default(self) -> None:
        p = ToolPermissionPolicy(mode=PermissionMode.BYPASS)
        assert p.evaluate("bash", {"command": "ls"}).allowed

    def test_accept_edits_auto_allows_edit_tools_only(self) -> None:
        p = ToolPermissionPolicy(mode=PermissionMode.ACCEPT_EDITS)
        assert p.evaluate("write_file", {"path": "a"}).allowed
        # A non-edit tool still asks.
        assert p.evaluate("bash", {"command": "ls"}).outcome is PermissionOutcome.ASK

    def test_plan_mode_allows_reads_but_denies_writes(self) -> None:
        p = ToolPermissionPolicy(mode=PermissionMode.PLAN)
        assert p.evaluate("read_file", {"path": "a"}).allowed
        assert p.evaluate("write_file", {"path": "a"}).outcome is PermissionOutcome.DENY
        assert p.evaluate("bash", {"command": "rm x"}).outcome is PermissionOutcome.DENY


class TestPrecedence:
    def test_deny_beats_allow(self) -> None:
        p = ToolPermissionPolicy(
            mode=PermissionMode.BYPASS, allow=["bash(*)"], deny=["bash(rm*)"]
        )
        assert p.evaluate("bash", {"command": "rm -rf /"}).outcome is PermissionOutcome.DENY
        assert p.evaluate("bash", {"command": "ls"}).allowed

    def test_deny_wins_even_in_bypass(self) -> None:
        """Bypass skips prompts, not safety rules."""
        p = ToolPermissionPolicy(mode=PermissionMode.BYPASS, deny=["send_email(*)"])
        assert p.evaluate("send_email", {"to": "x"}).outcome is PermissionOutcome.DENY

    def test_deny_wins_even_in_plan_over_allow(self) -> None:
        p = ToolPermissionPolicy(mode=PermissionMode.PLAN, allow=["write_file(*)"])
        # Plan mode + explicit allow: deny-by-plan-mode still refuses the write.
        assert p.evaluate("write_file", {"path": "a"}).outcome is PermissionOutcome.DENY

    def test_allow_beats_mode_default(self) -> None:
        p = ToolPermissionPolicy(mode=PermissionMode.ASK, allow=["read_file"])
        assert p.evaluate("read_file", {"path": "a"}).allowed


class TestArgumentPatterns:
    def test_argv_list_matches_shell_string_pattern(self) -> None:
        """A command as an argv list must match a rule written as a string —
        the same representation-independence the policy agent enforces."""
        p = ToolPermissionPolicy(mode=PermissionMode.ASK, allow=["bash(git status*)"])
        assert p.evaluate("bash", {"command": ["git", "status", "--short"]}).allowed

    def test_prefix_glob(self) -> None:
        p = ToolPermissionPolicy(mode=PermissionMode.ASK, allow=["bash(git *)"])
        assert p.evaluate("bash", {"command": "git log"}).allowed
        assert p.evaluate("bash", {"command": "curl x"}).outcome is PermissionOutcome.ASK


class TestRuleParsing:
    def test_parse_bare_tool(self) -> None:
        rule = PermissionRule.parse("read_file", PermissionOutcome.ALLOW)
        assert rule.tool == "read_file"
        assert rule.argument_pattern is None

    def test_parse_tool_with_args(self) -> None:
        rule = PermissionRule.parse("bash(git status*)", PermissionOutcome.ALLOW)
        assert rule.tool == "bash"
        assert rule.matches("bash", "git status --short")
        assert not rule.matches("bash", "rm -rf")
