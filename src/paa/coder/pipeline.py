"""The coder tool loop re-expressed as ``coder.tool-loop`` on the facade (C6.1 / P1.3).

The split the pipeline spec draws, made true for the second reference loop:

* the **mind** — :class:`CoderToolLoopPipeline` — is the loop's control
  flow: window-driven steps, the step ceiling, the token budget, final
  detection. It touches nothing but the facade it is handed;
* the **hands** — :class:`CoderLoopFacade`, minted by the host for one
  run — are the loop's effects: the model call (``think``) and the gated
  tool call (``use_skill``), each host-validated and host-journalled.

``think`` journals through the generalised :class:`~paa.pipelines.journal.
CallJournal` (``PIPELINE_CALL_*``, C6.2): the fingerprint covers the
rendered window's digest plus the role and budget, and the recorded
result is the model's proposed turn, which replay serves back without
re-invoking the model. ``use_skill`` maps to the **existing** coder
journal (ADR-0051's decision — ``TOOL_CALL_*`` rows, never a second
vocabulary) and reproduces :class:`~paa.coder.loop.AgentLoop`'s safety
ordering exactly: PRE hook (may redact or block) → journal PROPOSED with
the post-hook args and persisted intent → permission check → human
prompt on ASK → execute → journal COMPLETED/FAILED → POST hook. A denied
or failed call feeds an explanatory result back to the model, never an
abort — the loop's own contract, restated.

``AgentLoop`` itself is untouched behaviour-wise: it stays the classic
in-process path (``paa agent``), pinned by ``tests/coder/`` unchanged.
The proof that the re-expression is faithful is
``tests/pipelines/test_coder_pipeline.py``'s equivalence tests: the same
scripted model and tools driven through both paths produce the same
metrics, the same session transcript, and the same ``TOOL_CALL_*`` rows
on the session lineage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog

from paa.agents.base import AgentContext, AgentMessage, AgentResult
from paa.coder.hooks import HookContext, HookEvent, HookRegistry
from paa.coder.journal import (
    args_preview_redacted,
    args_sha256_of,
    compute_persisted_intent,
)
from paa.coder.loop import (
    ConfirmCallback,
    Tool,
    ToolCallingModel,
    ToolCtx,
    ToolJournal,
    _deny_confirm,
    isolation_level_of,
)
from paa.coder.permissions import PermissionOutcome, ToolPermissionPolicy
from paa.coder.session import Session, TurnRole
from paa.coder.workspace import Workspace
from paa.core.errors import PaaError
from paa.core.types import (
    AgentRole,
    ComplexityModality,
    Permission,
    PermissionMode,
)
from paa.ledger.replay import TaskProjection
from paa.pipelines.base import (
    Capability,
    CapabilityNotGrantedError,
    CapabilitySet,
    InvalidCallParamsError,
    PipelineArtifact,
    PipelineResult,
    PipelineStatus,
    PipelineTask,
    RunLimits,
)
from paa.pipelines.base import CapabilityFacade as _FacadeProtocol
from paa.pipelines.journal import CallJournal

__all__ = [
    "CODER_CAPABILITIES",
    "CoderLoopFacade",
    "CoderToolLoopPipeline",
]

log = structlog.get_logger(__name__)

#: What the coder pipeline asks for. Two capabilities, both exercised every
#: run: the model turn through ``think``, every tool call through
#: ``use_skill``. Everything else the facade offers is refused by the grant
#: set — a coder loop that grew a ``recall`` reach would fail loudly rather
#: than silently widening (ADR-0051's only door).
CODER_CAPABILITIES = frozenset({Capability.THINK, Capability.USE_SKILL})


def _render_window(session: Session) -> list[dict[str, Any]]:
    """Render the session window into the model's message shape.

    Identical to :meth:`AgentLoop._to_window` — the same renderer, stated
    once more where the facade lives so the two paths cannot drift apart
    without the equivalence tests noticing (they pin the transcripts).
    """
    window: list[dict[str, Any]] = []
    for turn in session.window():
        if turn.role is TurnRole.TOOL_CALL:
            window.append(
                {"role": "tool_call", "name": turn.tool_name, "arguments": turn.tool_arguments}
            )
        elif turn.role is TurnRole.TOOL_RESULT:
            window.append(
                {"role": "tool_result", "name": turn.tool_name, "content": turn.content}
            )
        else:
            window.append({"role": turn.role.value, "content": turn.content})
    return window


class CoderLoopFacade:
    """The host-minted facade for one ``coder.tool-loop`` run.

    Holds everything the classic loop's constructor holds — model, tools,
    session, permissions, hooks, confirm callback, the coder journal, the
    workspace — because those are the host's services, not the mind's. The
    two capability methods are the loop's effects, journalled where they
    happen: ``think`` through the run's :class:`CallJournal`
    (``PIPELINE_CALL_*``), ``use_skill`` through the coder session's
    :class:`~paa.coder.loop.ToolJournal` (``TOOL_CALL_*``, per ADR-0051 —
    no double journaling, so the CallJournal's ``seq`` numbers only the
    calls it journals and the tool calls keep their ``session:call_id``
    identity).

    Counters (``steps`` never counted here — the mind owns the ceilings)
    record what a run's result metrics need: tool calls issued, calls that
    executed, calls denied or failed.
    """

    def __init__(
        self,
        *,
        model: ToolCallingModel,
        tools: dict[str, Tool],
        session: Session,
        run_id: uuid.UUID | None = None,
        call_journal: CallJournal | None = None,
        permissions: ToolPermissionPolicy | None = None,
        hooks: HookRegistry | None = None,
        confirm: ConfirmCallback | None = None,
        tool_journal: ToolJournal | None = None,
        workspace: Workspace | None = None,
        modality: ComplexityModality = ComplexityModality.STANDARD,
        permission_mode: PermissionMode = PermissionMode.ASK,
        tokens_budget: int = 0,
    ) -> None:
        self._model = model
        self._tools = tools
        self._session = session
        self._perms = permissions or ToolPermissionPolicy()
        self._hooks = hooks or HookRegistry()
        self._confirm = confirm or _deny_confirm
        self._tool_journal = tool_journal
        self._workspace = workspace
        self._call_journal = call_journal
        self._run_id = run_id or uuid.uuid4()
        self._grants = CapabilitySet.mint(CODER_CAPABILITIES, run_id=self._run_id)
        self._agent_ctx = AgentContext(
            correlation_id=self._run_id,
            session_id=None,
            modality=modality,
            permission_mode=permission_mode,
            tokens_budget=tokens_budget,
        )
        self._seq = 0
        self._rng_state = self._run_id.int
        self._recorded_user_row = False
        self.tool_calls = 0
        self.executed_calls = 0

    # -- run scope -----------------------------------------------------------

    @property
    def correlation_id(self) -> uuid.UUID:
        return self._run_id

    @property
    def session_id(self) -> uuid.UUID | None:
        return None  # the coder session's id is session-scoped, not run-scoped

    @property
    def modality(self) -> ComplexityModality:
        return self._agent_ctx.modality

    @property
    def permission_mode(self) -> PermissionMode:
        return self._agent_ctx.permission_mode

    @property
    def tokens_budget(self) -> int:
        return self._agent_ctx.tokens_budget

    @property
    def agent_ctx(self) -> AgentContext:
        return self._agent_ctx

    @property
    def session_tokens(self) -> int:
        """The session's token count — a run-scope read for the mind's budget."""
        return self._session.total_tokens()

    @property
    def final_text(self) -> str:
        """The last assistant text the session recorded."""
        for turn in reversed(self._session.turns):
            if turn.role is TurnRole.ASSISTANT:
                return turn.content
        return ""

    # -- determinism aids and heartbeat --------------------------------------

    async def now(self) -> datetime:
        """Recorded time — the clock a replayable pipeline is allowed to read."""
        return datetime.now(UTC)

    async def random(self) -> float:
        """Seeded per run, so a replayed run draws the same values.

        The coder loop never draws randomness today, but the facade's
        contract is that a replayable pipeline may only read randomness
        here — so the door exists and is seeded, not absent.
        """
        self._rng_state = (
            self._rng_state * 6364136223846793005 + 1442695040888963407
        ) % (1 << 64)
        return self._rng_state / (1 << 64)

    # -- capabilities ----------------------------------------------------------

    async def think(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
        role: str = "worker",
        budget: int | None = None,
    ) -> dict[str, Any]:
        """One model turn: propose the next step given the window.

        ``prompt`` carries new user content only — the goal, on the run's
        first call (recorded as the session's user row and announced to the
        hooks exactly as the classic loop's ``run`` does); empty afterwards,
        because everything else the model needs is the window the host
        renders. A schema is the task pipeline's shape, not a tool loop's:
        refused, the chat facade's rule.
        """
        self._grants.require(Capability.THINK)
        if schema is not None:
            raise InvalidCallParamsError(
                "coder think proposes a free-form turn; schema-bearing calls are "
                "not tool-loop-shaped"
            )
        if prompt and not self._recorded_user_row:
            self._session.user(prompt)
            self._recorded_user_row = True
            await self._hooks.fire(
                HookContext(
                    event=HookEvent.USER_PROMPT_SUBMIT,
                    session_id=self._session.session_id,
                    prompt=prompt,
                )
            )

        window = _render_window(self._session)
        tool_schemas = [
            {"name": t.name, "description": t.description, "input_schema": t.schema()}
            for t in self._tools.values()
        ]
        params: dict[str, Any] = {
            "role": role,
            "budget": budget,
            "window_sha256": hashlib.sha256(
                json.dumps(window, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
        if self._call_journal is not None:
            self._seq += 1
            await self._call_journal.call_proposed(self._seq, "think", params)
        try:
            turn = await self._model.propose(window, tool_schemas)
        except asyncio.CancelledError:
            if self._call_journal is not None:
                await asyncio.shield(
                    self._call_journal.call_failed(self._seq, "think", "cancelled")
                )
            raise
        except Exception as exc:
            if self._call_journal is not None:
                await self._call_journal.call_failed(
                    self._seq, "think", f"{type(exc).__name__}: {exc}"
                )
            raise

        if turn.text:
            self._session.assistant(turn.text, tokens=turn.tokens)
        if self._call_journal is not None:
            await self._call_journal.call_completed(
                self._seq,
                "think",
                {
                    "text": turn.text,
                    "tool_calls": [
                        {"name": c.name, "arguments": c.arguments, "call_id": c.call_id}
                        for c in turn.tool_calls
                    ],
                    "tokens": turn.tokens,
                },
            )
        return {
            "text": turn.text,
            "tool_calls": [
                {"name": c.name, "arguments": c.arguments, "call_id": c.call_id}
                for c in turn.tool_calls
            ],
            "tokens": turn.tokens,
        }

    async def use_skill(
        self, name: str, args: Mapping[str, Any], *, call_id: str = ""
    ) -> str:
        """One gated tool call — the classic loop's ``_handle_call`` order.

        PRE hook → journal PROPOSED (post-hook args, persisted intent) →
        permission check → human prompt on ASK → execute → journal
        COMPLETED/FAILED → POST hook. A denial at any gate is a *result*,
        not an exception: the model must learn it cannot do that and try
        another way, exactly as a human collaborator would on being told
        no. Cancellation is the one exception — the journal's FAILED row
        is shielded, the session row lands, and the cancel propagates.

        ``call_id`` carries the model's stable id for this invocation
        (``ToolCall.call_id``); the keyword-only default keeps the frozen
        facade protocol satisfied for callers that have none — the
        journal then mints a fresh id, the legacy rule.
        """
        self._grants.require(Capability.USE_SKILL)
        self.tool_calls += 1
        self._session.tool_call(name, dict(args))

        tool = self._tools.get(name)
        if tool is None:
            self._session.tool_result(name, f"error: no such tool '{name}'")
            return f"error: no such tool '{name}'"

        # 1. PRE hook — may modify args or block.
        ctx = HookContext(
            event=HookEvent.PRE_TOOL_USE,
            session_id=self._session.session_id,
            tool_name=name,
            arguments=dict(args),
            mutable_arguments=dict(args),
        )
        hook_result = await self._hooks.fire(ctx)
        arguments = ctx.mutable_arguments
        journal_ctx: ToolCtx | None = None
        if self._tool_journal is not None:
            journal_ctx = self._journal_ctx_for(tool, name, arguments, call_id)
        if hook_result.block:
            if self._tool_journal is not None and journal_ctx is not None:
                await self._tool_journal.denied(
                    journal_ctx, hook_result.reason or "blocked by hook"
                )
            result = f"blocked by hook: {hook_result.reason}"
            self._session.tool_result(name, result)
            return result

        # 2. Journal PROPOSED, before any permission decision or execution.
        if self._tool_journal is not None and journal_ctx is not None:
            await self._tool_journal.before(journal_ctx)

        # 3-5. Permission, ASK, execute — one CancelledError arm, the
        # classic loop's contract: the FAILED row is shielded, then the
        # cancel propagates (a cancelled run is not a completed one).
        try:
            decision = self._perms.evaluate(name, arguments)
            if decision.outcome is PermissionOutcome.DENY:
                if self._tool_journal is not None and journal_ctx is not None:
                    await self._tool_journal.denied(
                        journal_ctx, decision.reason or "permission denied"
                    )
                result = f"permission denied: {decision.reason}"
                self._session.tool_result(name, result)
                return result

            if decision.outcome is PermissionOutcome.ASK:
                approved = await self._confirm(name, arguments, decision.reason)
                if not approved:
                    if self._tool_journal is not None and journal_ctx is not None:
                        await self._tool_journal.denied(journal_ctx, "declined by user")
                    self._session.tool_result(name, "declined by user")
                    return "declined by user"

            try:
                result = await tool.run(arguments)
            except Exception as exc:
                if self._tool_journal is not None and journal_ctx is not None:
                    await self._tool_journal.after(journal_ctx, False, None)
                error = f"error: {exc}"
                self._session.tool_result(name, error)
                return error
        except asyncio.CancelledError:
            if self._tool_journal is not None and journal_ctx is not None:
                await asyncio.shield(self._tool_journal.after(journal_ctx, False, None))
            self._session.tool_result(name, "cancelled by user")
            raise

        # 6. Journal COMPLETED, then the POST hook observes.
        if self._tool_journal is not None and journal_ctx is not None:
            await self._tool_journal.after(
                journal_ctx, True, hashlib.sha256(result.encode("utf-8")).hexdigest()
            )
        self._session.tool_result(name, result)
        self.executed_calls += 1

        await self._hooks.fire(
            HookContext(
                event=HookEvent.POST_TOOL_USE,
                session_id=self._session.session_id,
                tool_name=name,
                arguments=arguments,
                result=result,
            )
        )
        return result

    # -- refused capabilities (protocol shape; the grant set refuses first) ----

    async def call_agent(
        self, role: AgentRole, message: AgentMessage, agent_ctx: AgentContext
    ) -> AgentResult[Any]:
        self._grants.require(Capability.CALL_AGENT)
        raise CapabilityNotGrantedError(Capability.CALL_AGENT.value)  # pragma: no cover

    async def recall(self, query: str, *, k: int = 10) -> list[dict[str, Any]]:
        self._grants.require(Capability.RECALL)
        raise CapabilityNotGrantedError(Capability.RECALL.value)  # pragma: no cover

    async def remember(self, fact: Mapping[str, Any]) -> None:
        self._grants.require(Capability.REMEMBER)
        raise CapabilityNotGrantedError(Capability.REMEMBER.value)  # pragma: no cover

    async def gate(self, role: AgentRole, payload: Mapping[str, Any]) -> TaskProjection:
        self._grants.require(Capability.GATE)
        raise CapabilityNotGrantedError(Capability.GATE.value)  # pragma: no cover

    async def submit_plan(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        requirements: Mapping[str, Any] | None = None,
        bypassed_planner: bool = False,
    ) -> None:
        self._grants.require(Capability.PLAN_SUBMIT)
        raise CapabilityNotGrantedError(Capability.PLAN_SUBMIT.value)  # pragma: no cover

    async def check_policy(self, state: TaskProjection) -> TaskProjection:
        self._grants.require(Capability.POLICY_CHECK)
        raise CapabilityNotGrantedError(Capability.POLICY_CHECK.value)  # pragma: no cover

    async def commit(self, *, steps_completed: int, tokens_consumed: int) -> None:
        self._grants.require(Capability.COMMIT)
        raise CapabilityNotGrantedError(Capability.COMMIT.value)  # pragma: no cover

    async def sub_run(self, name: str, task_input: Mapping[str, Any]) -> PipelineResult:
        self._grants.require(Capability.SUB_RUN)
        raise CapabilityNotGrantedError(Capability.SUB_RUN.value)  # pragma: no cover

    async def progress(self, status: str) -> None:
        """Heartbeat; never journalled."""
        log.debug(
            "coder.progress",
            run_id=str(self._run_id),
            steps_status=status,
        )

    # -- in-process transport reads -------------------------------------------

    async def stage_question(self, role: AgentRole) -> None:
        return None  # in-process transport read; the coder loop asks no gates

    async def project(self) -> TaskProjection:
        raise PaaError(
            "the coder tool loop drives no task lineage; there is no projection to read",
            actual="coder.tool-loop",
        )

    # -- helpers ---------------------------------------------------------------

    def _journal_ctx_for(
        self,
        tool: Tool,
        name: str,
        arguments: Mapping[str, Any],
        call_id: str,
    ) -> ToolCtx:
        """The frozen journal context for one call, post-hook.

        The model's ``call_id`` is used as-is when present — it is the
        stable id the classic loop attests — and an empty legacy id is
        minted fresh, the loop's rule: an empty id would collapse every
        journalled call in the session to one discriminator, so the
        journal can never be handed "".
        """
        if not call_id:
            call_id = uuid.uuid4().hex
        pre_sha, post_sha, patch_sha = compute_persisted_intent(
            self._workspace, name, arguments
        )
        return ToolCtx(
            session_id=self._session.session_id,
            call_id=call_id,
            tool=name,
            args_sha256=args_sha256_of(arguments),
            args_preview_redacted=args_preview_redacted(name, arguments),
            isolation_level=isolation_level_of(tool),
            pre_sha256=pre_sha,
            post_sha256=post_sha,
            patch_sha256=patch_sha,
        )


class CoderToolLoopPipeline:
    """``coder.tool-loop``: the agentic tool-use loop as pipeline #3.

    The mind half of the split: it owns the loop's control flow (steps,
    budget, final detection) and touches nothing but the facade. Its
    mirrors of the classic loop's ceilings come from the run task's
    params (``max_steps``, ``max_tokens``) with the classic defaults.
    """

    name = "coder.tool-loop"
    version = "4.1.0"
    requires_runtime = ">=4.1,<5"
    required_permissions: frozenset[Permission] = frozenset()
    capabilities = CODER_CAPABILITIES
    limits = RunLimits()

    async def run(self, task: PipelineTask, ctx: _FacadeProtocol) -> PipelineResult:
        facade = self._narrow(ctx)
        max_steps = _int_param(task, "max_steps", 25)
        max_tokens_raw = task.params.get("max_tokens")
        max_tokens = (
            max_tokens_raw
            if isinstance(max_tokens_raw, int) and not isinstance(max_tokens_raw, bool)
            else None
        )

        steps = 0
        while steps < max_steps:
            steps += 1
            completion = await facade.think(
                task.goal if steps == 1 else "", role="worker", budget=max_tokens
            )
            tool_calls = completion.get("tool_calls")
            if not tool_calls:
                return self._result(
                    facade,
                    PipelineStatus.COMPLETED,
                    "model_finished",
                    completion,
                    steps,
                )

            for call in tool_calls:
                name = call.get("name")
                arguments = call.get("arguments")
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    continue  # the turn dict is the facade's own shape; malformed means ours
                raw_call_id = call.get("call_id")
                await facade.use_skill(
                    name,
                    arguments,
                    call_id=raw_call_id if isinstance(raw_call_id, str) else "",
                )

            if max_tokens is not None and facade.session_tokens >= max_tokens:
                return self._result(
                    facade,
                    PipelineStatus.FAILED,
                    "token_budget_exhausted",
                    completion,
                    steps,
                )

        return self._result(facade, PipelineStatus.FAILED, "max_steps_reached", {"text": ""}, steps)

    def _result(
        self,
        facade: CoderLoopFacade,
        status: PipelineStatus,
        stopped_reason: str,
        completion: Mapping[str, Any],
        steps: int,
    ) -> PipelineResult:
        final_text = completion.get("text")
        summary = final_text if isinstance(final_text, str) else facade.final_text
        metrics: dict[str, Any] = {
            "stopped_reason": stopped_reason,
            "steps": steps,
            "tool_calls": facade.tool_calls,
            "executed_calls": facade.executed_calls,
            "denied_calls": facade.tool_calls - facade.executed_calls,
            "tokens": facade.session_tokens,
        }
        artifacts: list[PipelineArtifact] = []
        if summary:
            artifacts.append(
                PipelineArtifact(
                    kind="coder_reply",
                    sha256=hashlib.sha256(summary.encode("utf-8")).hexdigest(),
                )
            )
        return PipelineResult(status=status, summary=summary, metrics=metrics, artifacts=artifacts)

    def _narrow(self, ctx: _FacadeProtocol) -> CoderLoopFacade:
        """Narrow the protocol to the facade the coder host mints.

        A foreign facade means the host wired the wrong door; that is a
        host bug worth naming, not a duck type to tolerate (the chat
        pipeline's rule, restated).
        """
        if not isinstance(ctx, CoderLoopFacade):
            raise PaaError(
                "coder.tool-loop requires the host-minted CoderLoopFacade",
                actual=type(ctx).__name__,
            )
        return ctx


def _int_param(task: PipelineTask, key: str, default: int) -> int:
    """A positive int param, or the default. ``True`` is not 1."""
    value = task.params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value
