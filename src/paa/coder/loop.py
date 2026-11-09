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

Safety ordering per tool call, and why:

1. **Hook PRE_TOOL_USE** — may modify args or block. Runs first so a hook can
   redact a secret out of the args *before* the permission check even sees them.
2. **Permission check** — allow / deny / ask. The deny here is authoritative.
3. **Human prompt** (only on ASK) — via an injected async callback, so the loop
   has no opinion about the UI.
4. **Execute** the tool.
5. **Hook POST_TOOL_USE** — observe the result (e.g. run a formatter).

A denied or refused call feeds an explanatory tool-result back to the model
rather than aborting: the agent should learn it cannot do that and try another
way, exactly as a human collaborator would on being told "no".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from paa.coder.hooks import HookContext, HookEvent, HookRegistry
from paa.coder.permissions import PermissionOutcome, ToolPermissionPolicy
from paa.coder.session import Session, TurnRole

__all__ = [
    "AgentLoop",
    "LoopResult",
    "ModelTurn",
    "Tool",
    "ToolCall",
    "ToolCallingModel",
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
        if hook_result.block:
            self._session.tool_result(call.name, f"blocked by hook: {hook_result.reason}")
            return False
        arguments = ctx.mutable_arguments

        # 2. Permission check.
        decision = self._perms.evaluate(call.name, arguments)
        if decision.outcome is PermissionOutcome.DENY:
            self._session.tool_result(call.name, f"permission denied: {decision.reason}")
            return False

        # 3. Human prompt on ASK.
        if decision.outcome is PermissionOutcome.ASK:
            approved = await self._confirm(call.name, arguments, decision.reason)
            if not approved:
                self._session.tool_result(call.name, "declined by user")
                return False

        # 4. Execute.
        try:
            result = await tool.run(arguments)
        except Exception as exc:
            self._session.tool_result(call.name, f"error: {exc}")
            return False

        self._session.tool_result(call.name, result)

        # 5. POST hook — observe.
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


# ``IsolationLevel`` is referenced by ToolCtx above and imported for the type
# checker only: the value a ToolCtx carries is produced by the ledger journal
# (N2.2), which binds the enum in its own module, and this module never
# resolves the annotation at runtime (no isinstance, no pydantic model, no
# get_type_hints on ToolCtx). A runtime import here would cost every importer
# of the loop the sandbox tree for one annotation. Placed at the end so the
# lines above — cited by the docs-references lock — do not move.
if TYPE_CHECKING:
    from paa.sandbox.base import IsolationLevel
