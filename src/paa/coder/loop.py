"""The agentic tool-use loop — think → act → observe, under permissions & hooks.

Clean-room implementation of the core interactive-coding-agent capability: give
a model a set of tools and a goal, and let it call tools in a loop until it
produces a final answer — with every call gated by the permission policy, run
through the hook chain, and recorded in the session transcript.

The loop is model-agnostic. It talks to a narrow :class:`ToolCallingModel`
protocol (``propose(window, tools) -> ModelTurn``); a real provider adapter or a
scripted test double both satisfy it. That is what lets the whole loop —
including the permission and hook behaviour that carries the safety guarantees —
be tested deterministically without a model server.

Safety ordering per tool call, and why (step 2 is the N2.2 journal point):

1. **Hook PRE_TOOL_USE** — may modify args or block. Runs first so a hook can
   redact a secret out of the args *before* anything else sees them.
2. **Journal PROPOSED** — ledger-before-side-effect, attesting the post-hook
   (redacted) args + persisted intent; on a hook block, journal DENIED here.
3. **Permission check** — allow / deny / ask. The deny here is authoritative.
4. **Human prompt** (only on ASK) — via an injected async callback, so the
   loop has no opinion about the UI; a decline journals DENIED. **Execute.**
5. **Journal COMPLETED/FAILED**, then **Hook POST_TOOL_USE** — observe the result.

A denied or refused call feeds an explanatory tool-result back to the model
rather than aborting: the agent should learn it cannot do that and try another
way, exactly as a human collaborator would on being told "no".
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from paa.coder.hooks import HookContext, HookEvent, HookRegistry
from paa.coder.permissions import PermissionOutcome, ToolPermissionPolicy
from paa.coder.session import Session, TurnRole
from paa.coder.workspace import Workspace
from paa.sandbox.base import IsolationLevel

__all__ = [
    "AgentLoop",
    "LoopResult",
    "ModelTurn",
    "Tool",
    "ToolCall",
    "ToolCallingModel",
    "ToolCtx",
    "ToolJournal",
]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ToolCall:
    """A tool invocation the model wants."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass(slots=True)
class ModelTurn:
    """One model step: either final text, or tool calls to run (or both)."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0

    @property
    def is_final(self) -> bool:
        return not self.tool_calls


class ToolCallingModel(Protocol):
    """The narrow model surface the loop needs."""

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        """Given the conversation window and tool schemas, propose the next step."""
        ...


class Tool(Protocol):
    """An executable tool. The USA skill registry adapts onto this."""

    name: str
    description: str

    def schema(self) -> dict[str, Any]:
        """JSON-schema describing the tool's arguments, for the model."""
        ...

    async def run(self, arguments: Mapping[str, Any]) -> str:
        """Execute and return a string result for the transcript."""
        ...


#: Called when a tool needs human confirmation. Returns True to allow.
ConfirmCallback = Callable[[str, dict[str, Any], str], Awaitable[bool]]


@dataclass(slots=True)
class LoopResult:
    """Outcome of running the loop to completion."""

    final_text: str
    steps: int
    tool_calls: int
    denied_calls: int
    stopped_reason: str
    tokens: int = 0


class AgentLoop:
    """Runs the think→act→observe cycle for one user request."""

    def __init__(
        self,
        model: ToolCallingModel,
        tools: dict[str, Tool],
        *,
        session: Session,
        permissions: ToolPermissionPolicy | None = None,
        hooks: HookRegistry | None = None,
        confirm: ConfirmCallback | None = None,
        max_steps: int = 25,
        max_tokens: int | None = None,
        journal: ToolJournal | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        self._model = model
        self._tools = tools
        self._session = session
        self._perms = permissions or ToolPermissionPolicy()
        self._hooks = hooks or HookRegistry()
        # Default confirm callback denies — a loop with no UI must not silently
        # auto-approve an ASK. An interactive caller injects a real prompt.
        self._confirm = confirm or _deny_confirm
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        # The ledger journal (N2.2): None means journalling is off, which is
        # the loop's pre-N2.2 behaviour and what every test that builds a
        # loop without one still gets. A non-None journal is consulted at
        # the step-2 point of _handle_call, always after the PRE hook.
        self._journal = journal
        # The jailed tree file intent is hashed from. None means the session
        # has no workspace, so every call's persisted intent is (None,)*3.
        self._workspace = workspace

    async def run(self, user_message: str) -> LoopResult:
        """Drive the loop for one user turn."""
        self._session.user(user_message)
        await self._hooks.fire(
            HookContext(
                event=HookEvent.USER_PROMPT_SUBMIT,
                session_id=self._session.session_id,
                prompt=user_message,
            )
        )

        tool_schemas = [
            {"name": t.name, "description": t.description, "input_schema": t.schema()}
            for t in self._tools.values()
        ]
        tool_calls = denied = 0

        for step in range(self._max_steps):
            turn = await self._model.propose(self._to_window(), tool_schemas)

            if turn.text:
                self._session.assistant(turn.text, tokens=turn.tokens)

            if turn.is_final:
                return LoopResult(
                    final_text=turn.text,
                    steps=step + 1,
                    tool_calls=tool_calls,
                    denied_calls=denied,
                    stopped_reason="model_finished",
                    tokens=self._session.total_tokens(),
                )

            for call in turn.tool_calls:
                tool_calls += 1
                executed = await self._handle_call(call)
                if not executed:
                    denied += 1

            if self._max_tokens and self._session.total_tokens() >= self._max_tokens:
                return LoopResult(
                    final_text=turn.text,
                    steps=step + 1,
                    tool_calls=tool_calls,
                    denied_calls=denied,
                    stopped_reason="token_budget_exhausted",
                    tokens=self._session.total_tokens(),
                )

        return LoopResult(
            final_text="",
            steps=self._max_steps,
            tool_calls=tool_calls,
            denied_calls=denied,
            stopped_reason="max_steps_reached",
            tokens=self._session.total_tokens(),
        )

    async def _handle_call(self, call: ToolCall) -> bool:
        """Run one tool call through the full gate. Returns whether it executed."""
        self._session.tool_call(call.name, call.arguments)

        tool = self._tools.get(call.name)
        if tool is None:
            self._session.tool_result(call.name, f"error: no such tool '{call.name}'")
            return False

        # 1. PRE hook — may modify args or block.
        ctx = HookContext(
            event=HookEvent.PRE_TOOL_USE,
            session_id=self._session.session_id,
            tool_name=call.name,
            arguments=dict(call.arguments),
            mutable_arguments=dict(call.arguments),
        )
        hook_result = await self._hooks.fire(ctx)
        arguments = ctx.mutable_arguments
        journal_ctx: ToolCtx | None = None
        if self._journal is not None:
            # The journal context is built from the post-hook args in
            # every case below — including a block — so a DENIED row
            # attests the same redacted call the permission check saw.
            journal_ctx = self._build_journal_ctx(tool, call, arguments)
        if hook_result.block:
            if self._journal is not None and journal_ctx is not None:
                await self._journal.denied(
                    journal_ctx, hook_result.reason or "blocked by hook"
                )
            self._session.tool_result(call.name, f"blocked by hook: {hook_result.reason}")
            return False

        # 2. Journal PROPOSED (post-hook args + persisted intent), before
        #    any permission decision or execution. If the journal refuses
        #    the call's ids it raises and the tool does not execute —
        #    there is no execute-without-a-row outcome.
        if self._journal is not None and journal_ctx is not None:
            await self._journal.before(journal_ctx)

        # 3. Permission check. Steps 3-5 sit inside one CancelledError arm:
        #    a cancel anywhere after PROPOSED exists must close the call as
        #    FAILED before propagating, or the recovery sweep later records
        #    the outcome-less PROPOSED as a crash ("interrupted") rather than
        #    the operator's cancel. The FAILED row is the frozen-vocabulary
        #    cancel record — ADR-0046 names no cancel event type, and the
        #    FAILED discriminator distinctness is what excludes this call
        #    from the sweep's interrupted set.
        try:
            decision = self._perms.evaluate(call.name, arguments)
            if decision.outcome is PermissionOutcome.DENY:
                if self._journal is not None and journal_ctx is not None:
                    await self._journal.denied(
                        journal_ctx, decision.reason or "permission denied"
                    )
                self._session.tool_result(call.name, f"permission denied: {decision.reason}")
                return False

            # 4. Human prompt on ASK.
            if decision.outcome is PermissionOutcome.ASK:
                approved = await self._confirm(call.name, arguments, decision.reason)
                if not approved:
                    if self._journal is not None and journal_ctx is not None:
                        await self._journal.denied(journal_ctx, "declined by user")
                    self._session.tool_result(call.name, "declined by user")
                    return False

            # 5. Execute.
            try:
                result = await tool.run(arguments)
            except Exception as exc:
                if self._journal is not None and journal_ctx is not None:
                    await self._journal.after(journal_ctx, False, None)
                self._session.tool_result(call.name, f"error: {exc}")
                return False
        except asyncio.CancelledError:
            # The journal write is shielded: the task is already unwinding,
            # and a second cancel (a second Ctrl-C) mid-write must not drop
            # the FAILED row and leave the PROPOSED outcome-less again.
            # The CancelledError is then re-raised — swallowing it would
            # report a cancelled run as a completed one to the caller.
            if self._journal is not None and journal_ctx is not None:
                await asyncio.shield(self._journal.after(journal_ctx, False, None))
            self._session.tool_result(call.name, "cancelled by user")
            raise

        # 6. Journal COMPLETED, then the POST hook observes.
        if self._journal is not None and journal_ctx is not None:
            await self._journal.after(
                journal_ctx, True, hashlib.sha256(result.encode("utf-8")).hexdigest()
            )

        self._session.tool_result(call.name, result)

        # 7. POST hook — observe.
        await self._hooks.fire(
            HookContext(
                event=HookEvent.POST_TOOL_USE,
                session_id=self._session.session_id,
                tool_name=call.name,
                arguments=arguments,
                result=result,
            )
        )
        return True

    def _build_journal_ctx(
        self, tool: Tool, call: ToolCall, arguments: Mapping[str, Any]
    ) -> ToolCtx:
        """Build the frozen journal context for one call, post-hook.

        The args hash and preview are computed from ``arguments`` as the
        PRE hook left them, so the ledger attests the redacted call, and
        the persisted intent is hashed from the actual jailed files by
        the journal module (the loop stays tool-agnostic). An empty
        legacy ``call_id`` is minted fresh here — an empty id would
        collapse every journalled call in the session to one
        discriminator, so the journal could never be handed "".
        """
        # Local import: journal.py imports ToolCtx from this module, so a
        # module-scope import here would be a cycle (class-A proof in
        # the import-conventions lock is about RelationalBackend; this
        # one is the same shape one level down).
        from paa.coder.journal import (
            args_preview_redacted,
            args_sha256_of,
            compute_persisted_intent,
        )

        pre_sha, post_sha, patch_sha = compute_persisted_intent(
            self._workspace, call.name, arguments
        )
        return ToolCtx(
            session_id=self._session.session_id,
            call_id=call.call_id or uuid.uuid4().hex,
            tool=call.name,
            args_sha256=args_sha256_of(arguments),
            args_preview_redacted=args_preview_redacted(call.name, arguments),
            isolation_level=self._isolation_for(tool),
            pre_sha256=pre_sha,
            post_sha256=post_sha,
            patch_sha256=patch_sha,
        )

    @staticmethod
    def _isolation_for(tool: Tool) -> IsolationLevel:
        """The containment this tool's execution actually runs under.

        Read-only and in-process tools run under ``NONE``. A tool that
        executes elsewhere declares it — either as an
        ``isolation_level`` attribute holding an :class:`IsolationLevel`,
        or through a no-argument ``preflight()`` returning a mapping with
        an ``"isolation_level"`` member name (the shell tool's shape).
        The ledger must attest the actual level, never a guess, so a
        tool that declares neither is ``NONE`` only because it never
        leaves the process — anything that does leave must use one of
        the two declared shapes.
        """
        level = getattr(tool, "isolation_level", None)
        if isinstance(level, IsolationLevel):
            return level
        preflight = getattr(tool, "preflight", None)
        if callable(preflight):
            info = preflight()
            if isinstance(info, Mapping):
                name = info.get("isolation_level")
                if isinstance(name, str) and name in IsolationLevel.__members__:
                    return IsolationLevel[name]
        return IsolationLevel.NONE

    def _to_window(self) -> list[dict[str, Any]]:
        """Render the session window into the model's message shape."""
        window = []
        for turn in self._session.window():
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


async def _deny_confirm(tool: str, arguments: dict[str, Any], reason: str) -> bool:
    log.warning("agent_loop.ask_auto_denied", tool=tool, reason="no confirm callback provided")
    return False


# ---------------------------------------------------------------------------
# ToolJournal protocol (N2.1 frozen; implemented by N2.2)
#
# The journal is the ledger-before-side-effect record of every tool call.
# N2.1 freezes this protocol — it does not implement it. The implementation
# lives in coder/journal.py (LedgerToolJournal, N2.2), which translates
# these three calls into the eight ADR-0046 coder/mandate events:
#
#     before(ctx)        -> TOOL_CALL_PROPOSED  (step 1 of the protocol)
#     denied(ctx, reason)-> TOOL_CALL_DENIED    (permission / ASK refusal)
#     after(ctx, ok, sha)-> TOOL_CALL_COMPLETED | TOOL_CALL_FAILED
#
# The ordering contract mirrors _handle_call's safety ordering: `before`
# is called after the PRE hook (so args_sha256 and args_preview_redacted
# attest the post-hook, redacted args, never the pre-hook originals) and
# before any permission decision or execution; `after` is called after
# the tool has run (or failed), with `result_sha` the SHA-256 of the
# tool's result — None when the tool raised, so a FAILED event never
# attests a partial result hash.
#
# Session start/end and mandate grant/refusal are not journal calls: they
# are emitted by the composition root that builds the agent and the policy
# that refuses a task, and are not shaped per tool call (see ADR-0046).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCtx:
    """The per-call context a ToolJournal records.

    Frozen: the journal handed a ToolCtx must not mutate it — the same
    value must describe the call at PROPOSED time and at outcome time, so
    any redaction or correction is a new value the caller constructs, not
    an in-place edit after the fact.

    Field contracts (ADR-0046):

    * ``session_id`` — the coder Session id; together with ``call_id`` it
      is the idempotency discriminator (`session:call_id:kind`).
    * ``call_id`` — the model's stable id for this invocation; an empty
      string is the loop's legacy "no id" and is allowed here only so a
      hybrid caller can still journal, but the journal MUST NOT dedupe on
      an empty call_id (a real call_id is required before execution).
    * ``tool`` — the tool name as dispatched (`ToolCall.name`).
    * ``args_sha256`` — SHA-256 of the canonical JSON of the post-hook
      arguments (what the journal attests, always post-redaction).
    * ``args_preview_redacted`` — a human-readable preview of the args
      (secret-shaped values already masked); kept short so the ledger
      stays readable. Never contains a raw secret (N2.5 canary test).
    * ``isolation_level`` — the containment the execution *actually* ran
      under (sandbox IsolationLevel actual, never requested); NONE for
      read-only tools that never leave the process.
    * ``pre_sha256`` — persisted intent: hash of the target file's bytes
      before the call, when the call touches a file; None otherwise.
    * ``post_sha256`` — persisted intent: hash of the target file's bytes
      after the call as computed *before* execution (the intended result),
      when the call is a file edit; None otherwise. Recovery (N2.3)
      compares the live file against pre/post to classify applied /
      not_applied / unknown.
    * ``patch_sha256`` — persisted intent: the patch digest
      (`compute_patch_sha256`) for an apply_patch edit; None otherwise.
    """

    session_id: str
    call_id: str
    tool: str
    args_sha256: str
    args_preview_redacted: str
    isolation_level: IsolationLevel
    pre_sha256: str | None = None
    post_sha256: str | None = None
    patch_sha256: str | None = None


class ToolJournal(Protocol):
    """Ledger-before-side-effect journal for tool calls (N2.1 freeze).

    Implementations are async because the journal performs ledger I/O;
    the loop's `_handle_call` awaits each hook. An implementation that
    needs to signal "do not execute" raises, it does not return a flag —
    there is no third outcome between PROPOSED and DENIED.
    """

    async def before(self, ctx: ToolCtx) -> None:
        """Record the PROPOSED event. Called once per call, before execution."""
        ...

    async def denied(self, ctx: ToolCtx, reason: str) -> None:
        """Record the DENIED event. Never executes the tool."""
        ...

    async def after(self, ctx: ToolCtx, ok: bool, result_sha: str | None) -> None:
        """Record COMPLETED (ok=True) or FAILED (ok=False)."""
        ...


# ``IsolationLevel`` is imported at runtime now (N2.2, W2-REV-01 MINOR-1):
# the loop resolves it — ``ToolCtx`` construction and the isinstance in
# ``_isolation_for`` — so a TYPE_CHECKING-only binding left the annotation
# unresolvable by ``get_type_hints`` (a frozen protocol whose annotation
# cannot be read at runtime is the wrong thing to freeze). Importing
# ``paa.coder.loop`` already loads ``paa.sandbox.base`` through
# ``paa.coder.__init__``, so the runtime import costs its importers
# nothing they did not already pay.
