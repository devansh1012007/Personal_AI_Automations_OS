"""Tool-permission policy for the interactive agent loop.

This is a clean-room implementation of the *capability* an interactive coding
agent needs — decide, per tool call, whether to allow it, refuse it, or ask the
human — not a copy of any product's code.

The design separates two axes that are easy to conflate:

* **Mode** — the standing posture (ask about everything / auto-accept edits /
  plan-only / bypass). One setting for the whole session.
* **Rules** — fine-grained allow/deny/ask patterns matched against a concrete
  tool call. Rules refine the mode: a session in "ask" mode can pre-allow
  ``read_file(*)`` so only the interesting calls prompt.

Precedence is deliberate and testable: an explicit **deny** always wins (a
safety rule must not be overridable by an allow), then explicit **allow**, then
the mode's default. Deny-wins is the property that lets a user paste a broad
allowlist without fear that it silently re-enables something a deny rule
forbade.
"""

from __future__ import annotations

import enum
import fnmatch
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_MUTATING_TOOLS",
    "PermissionDecision",
    "PermissionMode",
    "PermissionOutcome",
    "PermissionRule",
    "ToolPermissionPolicy",
]


class PermissionMode(str, enum.Enum):
    """Standing posture for a session."""

    ASK = "ask"
    """Prompt for anything not explicitly allowed. The safe default."""

    ACCEPT_EDITS = "accept_edits"
    """Auto-allow file edits in the workspace; still ask for the risky rest."""

    PLAN = "plan"
    """Read-only. Every mutating tool is refused so the agent can only explore
    and propose — the plan-mode guarantee."""

    BYPASS = "bypass"
    """Allow everything without prompting. For trusted, unattended runs only;
    a deny rule still wins, because bypass is about skipping *prompts*, not
    about disabling *safety rules*."""


class PermissionOutcome(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """The result of evaluating one tool call."""

    outcome: PermissionOutcome
    reason: str
    rule: str | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is PermissionOutcome.ALLOW

    @property
    def needs_prompt(self) -> bool:
        return self.outcome is PermissionOutcome.ASK


#: What ``*`` compiles to inside an allow/ask argument pattern: any run of
#: characters *except* the ones that start a new shell command (``;``, ``&``,
#: ``|``, a backtick, ``$``, a newline). Combined with the ``^`` ... ``$``
#: anchoring in ``PermissionRule.parse``, an allow written ``bash(git
#: status*)`` matches ``git status`` and ``git status --short -b`` but not
#: ``git status; rm -rf ~`` — the tail cannot carry a chained verb. ``?``
#: compiles to one character from the same class.
_ALLOW_GLOB_TAIL = r"[^;&|`$\n]*"

#: See :data:`_ALLOW_GLOB_TAIL`.
_ALLOW_GLOB_ONE = r"[^;&|`$\n]"


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """One allow/deny/ask rule.

    ``tool`` is a glob against the tool name. ``argument_pattern`` is an optional
    regex against a rendered form of the arguments, so a rule can be as coarse
    as ``deny bash(*)`` or as precise as ``allow bash(git status*)``. The
    pattern's anchoring follows the outcome, and the asymmetry is deliberate:

    - An **allow/ask** pattern is a full match (``^`` ... ``$``) whose ``*``
      tail cannot cross a shell command separator — authority matches exactly
      the command shape the operator named, and nothing chained behind it.
    - A **deny** pattern is a containment match (unanchored search) — a
      refusal is defensive, and must keep catching the pattern wherever an
      adversary chains it.
    """

    outcome: PermissionOutcome
    tool: str
    argument_pattern: str | None = None

    def matches(self, tool_name: str, rendered_args: str) -> bool:
        if not fnmatch.fnmatch(tool_name, self.tool):
            return False
        if self.argument_pattern is None:
            return True
        try:
            return re.search(self.argument_pattern, rendered_args) is not None
        except re.error:
            return False

    @classmethod
    def parse(cls, spec: str, outcome: PermissionOutcome) -> PermissionRule:
        r"""Parse a rule spec like ``bash(git status*)`` or ``read_file``.

        The parenthesised part becomes a regex whose shape follows the
        outcome, and the asymmetry is deliberate:

        - **Allow/ask are full matches with a separator-excluding tail.**
          An allow is authority, and authority must match exactly the
          command shape the operator named. Two smuggling spellings are
          closed at once: unanchored, ``allow bash(git status*)`` matched a
          command that merely *contains* ``git status`` (``echo hi; git
          status``); and even ``^``-anchored with an open ``.*`` tail it
          matched a command that *starts* with the prefix and chains
          another verb behind it (``git status; rm -rf ~``). The compiled
          form is ``^git\ status[^;&|`$\n]*$`` — the tail may carry
          arguments but may not cross a command boundary, and the ``$``
          forces the tail to reach the end of the string. Narrowing an
          allow is always the safe direction; a legitimate chained call is
          re-approved, not silently run.
        - **Deny stays a containment match.** A refusal is defensive, and a
          defensive rule must keep catching the pattern wherever an
          adversary puts it. Anchoring a deny would narrow it — ``deny
          bash(git push*)`` would stop catching ``git status; git push`` —
          which is the unsafe direction for exactly the rules whose job is
          to be the backstop when an allow is mis-scoped.
        """
        spec = spec.strip()
        if "(" in spec and spec.endswith(")"):
            tool, _, rest = spec.partition("(")
            arg_glob = rest[:-1]
            body = re.escape(arg_glob).replace(r"\*", _ALLOW_GLOB_TAIL).replace(
                r"\?", _ALLOW_GLOB_ONE
            )
            # Allow/ask are full matches (authority matches only the named
            # shape); deny is containment (a refusal must catch the pattern
            # wherever an adversary chains it). See parse's docstring.
            pattern = body if outcome is PermissionOutcome.DENY else f"^{body}$"
            return cls(outcome=outcome, tool=tool.strip(), argument_pattern=pattern)
        return cls(outcome=outcome, tool=spec, argument_pattern=None)


#: Tools that mutate state — refused outright in PLAN mode. Matched by glob.
#: These name the filesystem/shell-shaped capabilities a conventional coding
#: agent has; a caller with a different tool set overrides them via
#: ``ToolPermissionPolicy(mutating_tools=...)`` rather than editing this list,
#: so a tool whose name happens to contain "write" cannot quietly become
#: mutating and one that does not cannot quietly escape being so.
_MUTATING_TOOLS: tuple[str, ...] = (
    "write*",
    "edit*",
    "bash*",
    "shell*",
    "apply_patch*",
    "*delete*",
    "*write*",
    "send_*",
    "*_commit*",
    "git_stage*",
)

#: Public alias — the default set, for callers that extend rather than replace.
DEFAULT_MUTATING_TOOLS: tuple[str, ...] = _MUTATING_TOOLS


class ToolPermissionPolicy:
    """Decides allow / deny / ask for each tool call."""

    def __init__(
        self,
        *,
        mode: PermissionMode = PermissionMode.ASK,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        ask: list[str] | None = None,
        edit_tools: tuple[str, ...] = ("write_file", "edit_file", "apply_patch"),
        mutating_tools: tuple[str, ...] | None = None,
    ) -> None:
        self.mode = mode
        self._edit_tools = edit_tools
        self._mutating_tools = mutating_tools or _MUTATING_TOOLS
        self._rules: list[PermissionRule] = []
        # Order of construction does not matter; evaluation applies the
        # deny > allow > ask precedence explicitly.
        for spec in deny or []:
            self._rules.append(PermissionRule.parse(spec, PermissionOutcome.DENY))
        for spec in allow or []:
            self._rules.append(PermissionRule.parse(spec, PermissionOutcome.ALLOW))
        for spec in ask or []:
            self._rules.append(PermissionRule.parse(spec, PermissionOutcome.ASK))

    def add_rule(self, rule: PermissionRule) -> None:
        self._rules.append(rule)

    def evaluate(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> PermissionDecision:
        rendered = _render_args(arguments or {})

        # 1. Explicit deny always wins — even under BYPASS.
        for rule in self._rules:
            if rule.outcome is PermissionOutcome.DENY and rule.matches(tool_name, rendered):
                return PermissionDecision(
                    PermissionOutcome.DENY, f"denied by rule: {rule.tool}", rule.tool
                )

        # 2. PLAN mode refuses every mutating tool, regardless of allow rules —
        #    a plan-only session that could still write would not be plan-only.
        if self.mode is PermissionMode.PLAN and self._is_mutating(tool_name):
            return PermissionDecision(
                PermissionOutcome.DENY, "plan mode forbids mutating tools", "plan_mode"
            )

        # 3. Explicit allow.
        for rule in self._rules:
            if rule.outcome is PermissionOutcome.ALLOW and rule.matches(tool_name, rendered):
                return PermissionDecision(
                    PermissionOutcome.ALLOW, f"allowed by rule: {rule.tool}", rule.tool
                )

        # 4. Explicit ask.
        for rule in self._rules:
            if rule.outcome is PermissionOutcome.ASK and rule.matches(tool_name, rendered):
                return PermissionDecision(
                    PermissionOutcome.ASK, f"rule requests confirmation: {rule.tool}", rule.tool
                )

        # 5. Mode default.
        return self._mode_default(tool_name)

    def _mode_default(self, tool_name: str) -> PermissionDecision:
        if self.mode is PermissionMode.BYPASS:
            return PermissionDecision(PermissionOutcome.ALLOW, "bypass mode")
        if self.mode is PermissionMode.ACCEPT_EDITS and tool_name in self._edit_tools:
            return PermissionDecision(
                PermissionOutcome.ALLOW, "accept-edits mode auto-allows edits"
            )
        if self.mode is PermissionMode.PLAN:
            # Non-mutating tool in plan mode: allowed (read-only exploration).
            return PermissionDecision(PermissionOutcome.ALLOW, "plan mode allows read-only tools")
        return PermissionDecision(PermissionOutcome.ASK, f"{self.mode.value} mode asks by default")

    def _is_mutating(self, tool_name: str) -> bool:
        return any(fnmatch.fnmatch(tool_name, pat) for pat in self._mutating_tools)


def _render_args(arguments: dict[str, Any]) -> str:
    """Flatten arguments for pattern matching.

    Space-joins values the way the policy agent does, so an argv list is matched
    as ``git status`` rather than ``['git', 'status']`` — the same
    representation-independence lesson (a rule must match whether a command
    arrives as a string or a list).
    """

    def flat(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return " ".join(flat(v) for v in value)
        if isinstance(value, dict):
            return " ".join(flat(v) for v in value.values())
        return str(value)

    return " ".join(flat(v) for v in arguments.values())
