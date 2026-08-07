"""Interactive coding-agent features — clean-room, not derived from any product.

This package implements the *capabilities* an interactive coding agent needs,
built from how such systems work publicly rather than from anyone's source:

``permissions``
    Per-tool allow/deny/ask policy with modes and deny-wins precedence.
``hooks``
    Lifecycle hooks that can observe, modify, or block tool calls.
``commands``
    Slash commands, built-in and markdown-defined.
``session``
    Append-only, resumable transcript with compaction.
``loop``
    The think→act→observe tool-use loop that ties them together, gating every
    tool call through hooks and permissions.

The loop is model-agnostic (a narrow :class:`~paa.coder.loop.ToolCallingModel`
protocol) and tool-agnostic (a narrow :class:`~paa.coder.loop.Tool` protocol
onto which the Unified Skill Adapter's registry adapts), so the whole thing —
including its safety behaviour — is testable without a model or a live tool.
"""

from __future__ import annotations

from paa.coder.commands import (
    CommandRegistry,
    CommandResult,
    SlashCommand,
    default_registry,
    parse_command_line,
)
from paa.coder.hooks import (
    Hook,
    HookContext,
    HookEvent,
    HookRegistry,
    HookResult,
    command_hook,
    deny_tool_hook,
)
from paa.coder.loop import AgentLoop, LoopResult, ModelTurn, Tool, ToolCall, ToolCallingModel
from paa.coder.permissions import (
    PermissionDecision,
    PermissionMode,
    PermissionOutcome,
    PermissionRule,
    ToolPermissionPolicy,
)
from paa.coder.session import Session, Turn, TurnRole

__all__ = [
    "AgentLoop",
    "CommandRegistry",
    "CommandResult",
    "Hook",
    "HookContext",
    "HookEvent",
    "HookRegistry",
    "HookResult",
    "LoopResult",
    "ModelTurn",
    "PermissionDecision",
    "PermissionMode",
    "PermissionOutcome",
    "PermissionRule",
    "Session",
    "SlashCommand",
    "Tool",
    "ToolCall",
    "ToolCallingModel",
    "ToolPermissionPolicy",
    "Turn",
    "TurnRole",
    "command_hook",
    "default_registry",
    "deny_tool_hook",
    "parse_command_line",
]
