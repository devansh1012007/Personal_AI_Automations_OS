"""The composition root for the coder agent — the piece that was never wired.

Every other module in :mod:`paa.coder` is a capability in isolation: the loop
does not know what a tool is, the tools do not know what a model is, and neither
knows what a :class:`~paa.runtime.Runtime` is. That separation is what makes
them testable, and it is also why nothing runs. This module is the seam — one
place that builds a real runtime, hands the runtime's own ledger to the tools,
wraps the configured model router in a :class:`~paa.coder.loop.ToolCallingModel`,
and drops the result into an :class:`~paa.coder.loop.AgentLoop`.

It is deliberately thin. The interesting decisions — what a model may call, and
whether that is allowed — are not made here. They stay in the permission policy
and the router, so that swapping the model, adding a tool, or tightening the
mode changes configuration rather than this file.

**The escalation router is not bypassed.** The agent is built from
:class:`~paa.runtime.Runtime`, which constructs its router through
:func:`paa.models.get_model_router` with the ledger attached, so every model
call in the loop passes the same escalation policy as a planner call and writes
a ``MODEL_ESCALATED`` event when reasoning leaves the machine. An agent that
reached around the router for its own inference would be the one component in the
runtime whose egress is unlogged, which is exactly the property the ledger exists
to deny.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from paa.coder.hooks import HookRegistry
from paa.coder.loop import AgentLoop, LoopResult
from paa.coder.model_adapter import RouterToolCallingModel
from paa.coder.permissions import DEFAULT_MUTATING_TOOLS, PermissionMode, ToolPermissionPolicy
from paa.coder.runtime_tools import build_runtime_tools
from paa.coder.session import Session
from paa.core.errors import PaaError
from paa.core.types import ComplexityModality

if TYPE_CHECKING:
    from collections.abc import Sequence

    from paa.coder.hooks import Hook, HookEvent
    from paa.coder.loop import ConfirmCallback
    from paa.config import Settings
    from paa.core.types import CorrelationId
    from paa.runtime import Runtime

__all__ = ["MUTATING_RUNTIME_TOOLS", "CoderAgent", "TranscriptDir"]

log = structlog.get_logger(__name__)

TranscriptDir = str | Path

#: The runtime tools that change state. ``submit_task`` writes a lineage and
#: ``run_task`` executes work, but neither name matches the conventional
#: filesystem/shell globs in :data:`~paa.coder.permissions.DEFAULT_MUTATING_TOOLS`
#: — so without this the plan-mode guarantee would silently exempt exactly the
#: two tools that matter most. ``--mode plan`` has to mean *this* runtime does
#: not change, not just *this runtime's files do not change*.
MUTATING_RUNTIME_TOOLS: tuple[str, ...] = ("submit_task", "run_task")


@dataclass
class CoderAgent:
    """A runtime plus the tools to drive it plus a loop that calls them."""

    runtime: Runtime
    model: RouterToolCallingModel
    loop: AgentLoop
    session: Session
    tools: dict[str, Any] = field(default_factory=dict)

    async def ask(self, prompt: str) -> LoopResult:
        """Run one user turn through the tool-use loop."""
        result = await self.loop.run(prompt)
        log.info(
            "coder_agent.turn_complete",
            session_id=self.session.session_id,
            steps=result.steps,
            tool_calls=result.tool_calls,
            denied_calls=result.denied_calls,
            stopped_reason=result.stopped_reason,
        )
        return result

    async def close(self) -> None:
        await self.runtime.close()

    async def __aenter__(self) -> CoderAgent:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- construction ------------------------------------------------------

    @classmethod
    async def build(
        cls,
        settings: Settings | None = None,
        *,
        session: Session | None = None,
        model: Any | None = None,
        transcript_dir: TranscriptDir | None = None,
        instructions: str | None = None,
        mode: PermissionMode = PermissionMode.ASK,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        hooks: Sequence[tuple[HookEvent, Hook]] | None = None,
        hook_registry: HookRegistry | None = None,
        confirm: ConfirmCallback | None = None,
        max_steps: int = 25,
        max_tokens: int | None = None,
        modality: ComplexityModality = ComplexityModality.STANDARD,
        force_escalate: bool = False,
        correlation_id: CorrelationId | None = None,
        run_recovery: bool = False,
        model_max_tokens: int = 2048,
    ) -> CoderAgent:
        """Build an agent over a fresh runtime.

        ``run_recovery`` defaults to ``False``, matching the CLI's other
        one-shot commands: a recovery sweep re-queues work, which is a decision
        an operator should make explicitly, not a side effect of asking a
        question.

        ``mode`` is the agent's standing posture and is enforced by
        :class:`~paa.coder.permissions.ToolPermissionPolicy` on every call —
        ``PermissionMode.PLAN`` makes the agent read-only, and an explicit deny
        rule wins even under ``BYPASS``. With no ``confirm`` callback the loop
        auto-*denies* rather than auto-approving, so an unattended run stops at
        the first gated call instead of quietly proceeding.

        ``model`` replaces the router-backed adapter with any
        ``ToolCallingModel``. It is the same seam
        :meth:`~paa.runtime.Runtime.build` offers as ``model_adapter``, kept
        here so this composition can be tested end-to-end without a provider —
        the loop, permissions, hooks and tools are all still real.

        ``hooks`` are ``(HookEvent, hook)`` pairs rather than bare hooks, so the
        signature matches :meth:`~paa.coder.hooks.HookRegistry.register`: a
        hook with no event has nowhere to fire. Pass ``hook_registry`` instead
        to hand in a pre-wired registry.
        """

        from paa.runtime import Runtime

        runtime = await Runtime.build(settings, run_recovery=run_recovery)

        # ``Runtime.build`` always constructs a router unless a model adapter was
        # injected instead; this agent cannot be handed one of those, because it
        # is the thing that adapts the router. Failing loudly beats an adapter
        # over ``None`` that would only surface on the first tool call.
        router = runtime.model_router
        if router is None:
            await runtime.close()
            raise PaaError(
                "runtime has no model router — build the runtime with "
                "Runtime.build() rather than constructing it directly"
            )

        registry = hook_registry or HookRegistry()
        for event, hook in hooks or []:
            registry.register(event, hook)

        session = session or Session(
            transcript_dir=transcript_dir or (runtime.settings.home / "sessions")
        )

        model = model or RouterToolCallingModel(
            router,
            instructions=instructions,
            modality=modality,
            permission_mode=runtime.settings.policy.mode,
            correlation_id=correlation_id,
            force_escalate=force_escalate,
            max_tokens=model_max_tokens,
        )
        tools = build_runtime_tools(runtime)
        loop = AgentLoop(
            model,
            tools,
            session=session,
            permissions=ToolPermissionPolicy(
                mode=mode,
                allow=allow,
                deny=deny,
                mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS),
            ),
            hooks=registry,
            confirm=confirm,
            max_steps=max_steps,
            max_tokens=max_tokens,
        )
        return cls(
            runtime=runtime,
            model=model,
            loop=loop,
            session=session,
            tools=tools,
        )