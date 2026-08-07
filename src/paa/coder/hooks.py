"""Lifecycle hooks for the interactive agent loop.

Clean-room implementation of the *hook capability*: user-supplied code that runs
at defined points in the loop and can observe, block, or modify what happens.
This is what lets an operator enforce policy the agent cannot talk its way out of
— run a formatter after every edit, block a tool on a condition, log every
prompt — without patching the runtime.

A hook is any async callable ``(HookContext) -> HookResult``. Hooks for an event
run in registration order; the first one that returns ``block=True`` stops the
action and no later hook for that event runs. That "first block wins, and short-
circuits" rule is what makes a deny hook trustworthy: a later, more permissive
hook cannot silently undo it.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

__all__ = [
    "Hook",
    "HookContext",
    "HookEvent",
    "HookRegistry",
    "HookResult",
]

log = structlog.get_logger(__name__)


class HookEvent(str, enum.Enum):
    """Points in the loop where hooks fire."""

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    SUBAGENT_STOP = "subagent_stop"
    STOP = "stop"


@dataclass(slots=True)
class HookContext:
    """What a hook is given.

    ``mutable_arguments`` starts as a copy of the tool arguments; a PRE_TOOL_USE
    hook may edit it in place (e.g. inject a flag, redact a value) and the loop
    uses the edited version. The original ``arguments`` is left untouched so a
    later hook and the audit log see what was proposed, not what was rewritten.
    """

    event: HookEvent
    session_id: str
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    mutable_arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    prompt: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HookResult:
    """What a hook returns."""

    block: bool = False
    reason: str | None = None
    #: When set on a PRE_TOOL_USE hook, replaces the tool arguments.
    modified_arguments: dict[str, Any] | None = None
    #: Extra context a hook wants surfaced to the model (e.g. a lint warning).
    injected_context: str | None = None


Hook = Callable[[HookContext], Awaitable[HookResult]]


class HookRegistry:
    """Holds hooks per event and dispatches them."""

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[Hook]] = {e: [] for e in HookEvent}

    def register(self, event: HookEvent, hook: Hook) -> None:
        self._hooks[event].append(hook)

    def count(self, event: HookEvent) -> int:
        return len(self._hooks[event])

    async def fire(self, ctx: HookContext) -> HookResult:
        """Run every hook for ``ctx.event`` until one blocks.

        Returns the blocking result, or a clean pass-through if none blocked.
        Argument modifications accumulate: each hook sees the previous hook's
        edits, so a chain of PRE_TOOL_USE hooks composes.

        A hook that raises is logged and treated as a non-block (fail-open on
        an *observer* hook is correct; a hook that wants to *enforce* must
        return ``block=True``, and a crash is not a block). The one exception is
        that a raised hook cannot silently allow something a prior hook already
        blocked, because dispatch has already returned by then.
        """
        aggregate = HookResult()
        for hook in self._hooks[ctx.event]:
            try:
                result = await hook(ctx)
            except Exception as exc:
                log.warning(
                    "hook.raised",
                    event=ctx.event.value,
                    hook=getattr(hook, "__name__", "?"),
                    error=str(exc),
                )
                continue

            if result.modified_arguments is not None:
                ctx.mutable_arguments = dict(result.modified_arguments)
                aggregate.modified_arguments = ctx.mutable_arguments
            if result.injected_context:
                aggregate.injected_context = (
                    (aggregate.injected_context or "") + result.injected_context + "\n"
                )
            if result.block:
                return HookResult(
                    block=True,
                    reason=result.reason or "blocked by hook",
                    modified_arguments=aggregate.modified_arguments,
                    injected_context=aggregate.injected_context,
                )
        return aggregate


# ---------------------------------------------------------------------------
# A couple of ready-made hooks, so the capability is useful out of the box.
# ---------------------------------------------------------------------------


def deny_tool_hook(tool_glob: str, reason: str = "blocked by policy") -> Hook:
    """A hook that blocks any tool whose name matches ``tool_glob``."""
    import fnmatch

    async def _hook(ctx: HookContext) -> HookResult:
        if ctx.tool_name and fnmatch.fnmatch(ctx.tool_name, tool_glob):
            return HookResult(block=True, reason=reason)
        return HookResult()

    _hook.__name__ = f"deny_{tool_glob}"
    return _hook


def command_hook(command: list[str], *, event: HookEvent = HookEvent.POST_TOOL_USE) -> Hook:
    """A hook that runs an external command (e.g. a formatter) and never blocks.

    Runs the command with a timeout; its failure is logged, not fatal — a
    post-edit formatter that errors should not abort the agent's work.
    """
    import asyncio

    async def _hook(ctx: HookContext) -> HookResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except Exception as exc:
            log.warning("hook.command_failed", command=command, error=str(exc))
        return HookResult()

    _hook.__name__ = f"command_{command[0]}"
    return _hook
