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

from paa.coder.fs_read_tools import build_fs_read_tools
from paa.coder.fs_write_tools import build_fs_write_tools
from paa.coder.git_tools import build_git_tools
from paa.coder.hooks import HookRegistry
from paa.coder.loop import AgentLoop, LoopResult
from paa.coder.model_adapter import RouterToolCallingModel
from paa.coder.permissions import DEFAULT_MUTATING_TOOLS, PermissionMode, ToolPermissionPolicy
from paa.coder.runtime_tools import READ_ONLY_TOOLS, build_runtime_tools
from paa.coder.session import Session
from paa.coder.shell_tools import build_shell_tools
from paa.coder.workspace import Workspace
from paa.core.errors import PaaError
from paa.core.types import ComplexityModality

if TYPE_CHECKING:
    from collections.abc import Sequence

    from paa.coder.hooks import Hook, HookEvent
    from paa.coder.loop import ConfirmCallback
    from paa.config import Settings
    from paa.core.types import CorrelationId
    from paa.runtime import Runtime

__all__ = [
    "MUTATING_RUNTIME_TOOLS",
    "CoderAgent",
    "TranscriptDir",
    "build_work_tools",
]

log = structlog.get_logger(__name__)

TranscriptDir = str | Path

#: The runtime tools that change state. ``submit_task`` writes a lineage,
#: ``run_task`` executes work, and ``remember_fact`` writes rows into the
#: memory tables (``hot_serving_entity_index`` / ``hot_serving_active_facts``)
#: — but none of those names matches the conventional filesystem/shell globs
#: in :data:`~paa.coder.permissions.DEFAULT_MUTATING_TOOLS`, so without this
#: the plan-mode guarantee would silently exempt exactly the tools that matter
#: most. ``--mode plan`` has to mean *this* runtime does not change, not just
#: *this runtime's files* do not change. A new mutating tool that escapes this
#: tuple becomes plan-mode-exempt by accident; add it here.
MUTATING_RUNTIME_TOOLS: tuple[str, ...] = ("submit_task", "run_task", "remember_fact")


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
        workspace: Workspace | None = None,
        journal: Any | None = None,
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

        ``workspace`` is the jailed working tree the filesystem, shell and
        git tools reach through. ``None`` (the default) builds only the
        ledger/memory catalogue — the work tools have nothing to jail to
        without an explicit workspace, so they are not offered rather than
        offered jailed to an implied directory.

        ``journal`` is a reserved Wave-2 (N2.2) slot: the ``ToolJournal``
        protocol does not exist yet, so the value is accepted and ignored.
        The name is explicit (not ``**kwargs``) so Wave 2 wires a known
        parameter instead of renaming one.
        """
        # The journal slot is a name reservation for Wave 2 (N2.2), not a
        # wire: a non-None journal fails fast, *before* any runtime is
        # built, rather than being accepted and silently dropped (which
        # would attest that journalling is on when nothing is journalled)
        # or raised after the build (which would leak the runtime).
        if journal is not None:
            raise NotImplementedError(
                "CoderAgent.build journal wiring lands in Wave 2 (N2.2); "
                "no ToolJournal protocol exists yet"
            )

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
        tools = build_work_tools(runtime, workspace)
        loop = AgentLoop(
            model,
            tools,
            session=session,
            permissions=ToolPermissionPolicy(
                mode=mode,
                # Standing-allow the catalogue's read-only tools so ASK mode
                # prompts for calls that *change state*, not for reads — a
                # prompt for ``check_runtime({})`` is friction that trains the
                # operator to approve everything, including the calls that
                # matter. An explicit ``--deny`` still wins: deny is precedence
                # step 1 in ToolPermissionPolicy and outranks any allow, and
                # PLAN still refuses the mutating tools regardless.
                allow=[*READ_ONLY_TOOLS, *(allow or [])],
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


def build_work_tools(runtime: Runtime, workspace: Workspace | None = None) -> dict[str, Any]:
    """The full coder catalogue: runtime tools plus the work tools.

    With ``workspace=None`` this is exactly :func:`build_runtime_tools` —
    the ledger/memory catalogue for a session with no jailed tree to
    work in. With a workspace it additionally merges the filesystem-read,
    filesystem-write, shell and git catalogues, each bound to that one
    workspace, into a single ``{name: Tool}`` dict for the loop. The shell
    tool runs on the runtime's own sandbox backend when one exists (so a
    ``NAMESPACE`` backend makes ``allow_network=False`` enforceable) and
    falls back to the subprocess default otherwise; the backend's actual
    isolation is what every call reports.

    Tool names must be disjoint across the catalogues: a name claimed by
    two factories would silently overwrite the first tool under the same
    key while its name stayed in the model's constrained enum — the loop
    would then dispatch it to the wrong tool. A collision therefore
    raises :class:`~paa.core.errors.PaaError` at composition time rather
    than being absorbed by ``dict.update``.
    """
    parts: list[dict[str, Any]] = [build_runtime_tools(runtime)]
    if workspace is not None:
        parts.append(build_fs_read_tools(workspace))
        parts.append(build_fs_write_tools(workspace))
        sandbox = runtime.sandbox if runtime.sandbox is not None else None
        parts.append(build_shell_tools(workspace, sandbox=sandbox))
        parts.append(build_git_tools(workspace))

    tools: dict[str, Any] = {}
    for part in parts:
        collisions = tools.keys() & part.keys()
        if collisions:
            raise PaaError(
                "duplicate tool name(s) across catalogues: "
                f"{sorted(collisions)} — a tool name must be claimed by "
                "exactly one factory"
            )
        tools.update(part)
    return tools