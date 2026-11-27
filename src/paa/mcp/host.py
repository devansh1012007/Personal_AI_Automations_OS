"""``builtin.mcp-host`` — the externally-driven MCP session as a pipeline (W9-C9.4).

P6's last adapter, per ``docs/plans/2026-11-21-pipeline-platform-spec.md``
§6: when an external harness (a Claude Code, a Codex, a Cursor, any MCP
client) drives PAA through ``paa mcp serve``, **that session is a pipeline
run on the platform**. It gets a lineage, ``PIPELINE_RUN_*`` rows and one
``mcp.session`` call row — the same accounting wrapper
``builtin.observe`` (pipeline #4) wraps around a foreign CLI, restated
for a foreign *client*: the run opens when the session starts and
completes when the client closes the pipe (EOF) or the transport dies.

What this module is *not* is a second server. :class:`McpHostPipeline`
wraps :class:`paa.mcp.server.MCPServer` UNCHANGED — the server keeps its
own policy gate (``manifest ∩ mandate ∩ policy`` per tool call), its own
per-call ``TOOL_CALL_*`` lineages and its chat-session journaling; this
module adds only the session-shaped rows around ``serve()``. The split,
stated honestly:

* **The evidence is the operator's transcript, not a reproducible
  in-process run.** The declaration therefore carries
  ``evidence_tier = "T2"`` — the spec §6 row for ``mcp-host``: T2 *for
  the mediated tool calls only* (every call the harness makes crosses
  the server's gate and is journalled), while the session's outer shape
  is whatever the operator's harness did, which PAA observes but does
  not control.
* **``resume = "none"``**, the spec §6 rule for every external-harness
  adapter: an MCP session is not journalled for replay — each turn is
  its own request/response — and the host refuses to mark such a run
  recoverable. A re-run is a new session with a new client, never a
  resume of the old one.
* **The session's call row is boundary accounting, not call
  accounting.** One ``mcp.session`` call at seq 1 carries the served
  catalogue (the six tools, the pinned protocol version) and resolves
  with the one thing the wrapper can measure: how many requests it
  answered. What the client *did* with the tools lives on the server's
  own lineages — per-call ``TOOL_CALL_*`` rows and the chat-session
  events — which are deliberately NOT correlated to this run's lineage
  in v1 (stated as the known gap; a session-to-call correlation is
  follow-up work, and faking it here would be worse than its absence).
* **The capability ask is the six tools' own.** The ask restates the
  union of :data:`paa.mcp.server.TOOL_MANIFEST`'s per-tool capability
  sets, so ``paa pipeline inspect builtin.mcp-host`` shows what a
  session's tools may exercise. The *enforcement* of that ask is the
  server's per-call gate, not this facade: nothing in the accounting
  wrapper calls a capability, so the facade mints no tokens and its
  capability doors refuse by naming where the real door is. A host that
  wants to narrow a session below the declared ask wires the server's
  existing ``mandate_capabilities`` seam; both CLI doors pass it as
  ``None`` (unconstrained) today, the same P1 semantics the seam ships
  with.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from paa.core.errors import PaaError
from paa.core.types import ComplexityModality, Permission, PermissionMode
from paa.mcp.protocol import PROTOCOL_VERSION, SERVER_NAME, FrameSink
from paa.mcp.server import TOOL_MANIFEST, MCPServer
from paa.pipelines.base import (
    Capability,
    CapabilityFacade,
    PipelineResult,
    PipelineStatus,
    PipelineTask,
    RunLimits,
)
from paa.pipelines.journal import CallJournal

if TYPE_CHECKING:
    from asyncio import StreamReader

    from paa.ledger.store import LedgerStore

__all__ = [
    "MCP_HOST_CAPABILITIES",
    "SESSION_METHOD",
    "McpHostFacade",
    "McpHostOutcome",
    "McpHostPipeline",
    "run_mcp_host_session",
]

#: The one call the wrapper journals, at seq 1 — the observe pattern's
#: ``observe.run``, restated: the whole externally-driven session is one
#: call from the host's point of view, PROPOSED before the first frame is
#: read and resolved when the pipe closes.
SESSION_METHOD = "mcp.session"

#: The session's capability ask: the union of the six tools' own asks,
#: derived from the one source of truth (``TOOL_MANIFEST``) so it cannot
#: drift — a tool whose manifest row grows a capability widens this set
#: and the declaration's restatement is pinned against it by test.
MCP_HOST_CAPABILITIES: frozenset[Capability] = frozenset[Capability]().union(
    *(spec.capabilities for spec in TOOL_MANIFEST.values())
)


@dataclass(frozen=True, slots=True)
class McpHostOutcome:
    """One served session's measurable fate — the wrapper's own facts only.

    ``requests_answered`` counts the response frames the server emitted
    (one per request; notifications answer nothing), which is the one
    number the accounting wrapper can own: what the harness asked is its
    business, recorded on the tool-call lineages the server writes.
    """

    requests_answered: int


class _CountingSink:
    """A :class:`~paa.mcp.protocol.FrameSink` that counts what it emitted.

    The wrapper's one measurement, taken at the only seam it owns: the
    server hands every response frame to its sink, so the count of
    ``write`` calls is the count of answered requests — no server change
    needed, the structural hole the protocol already defines for tests.
    """

    def __init__(self, inner: FrameSink) -> None:
        self._inner = inner
        self.frames = 0

    def write(self, data: bytes) -> None:
        self.frames += 1
        self._inner.write(data)

    async def drain(self) -> None:
        await self._inner.drain()


class McpHostFacade:
    """The host-minted accounting wrapper one ``builtin.mcp-host`` run serves through.

    One typed operation, journalled as ``mcp.session`` at seq 1:
    :meth:`serve_session` PROPOSEs the call (the served catalogue and the
    operator's harness label are the call's identity — the fingerprint
    covers them, the preview renders shapes only), serves the external
    client to EOF through the UNCHANGED :class:`MCPServer`, and resolves
    COMPLETED with the answered-request count. A transport death or a
    cancel resolves FAILED first — shielded, so an audit row survives
    even a mid-serve kill — and then propagates.

    The rest of the :class:`~paa.pipelines.base.CapabilityFacade` surface
    is implemented and refused, not absent — but unlike the observe
    facade, whose empty grant set closes its doors, these doors refuse by
    *naming the real one*: the external client's tool calls cross the
    server's own per-call gate (``MCPServer._require_grants``), never
    this facade, so a caller reaching for a capability here is a wiring
    bug worth a sentence, not a vocabulary refusal pretending a grant
    was ever minted.
    """

    required_permissions: frozenset[Permission] = frozenset()
    capabilities = MCP_HOST_CAPABILITIES

    def __init__(
        self,
        *,
        server: MCPServer,
        journal: CallJournal,
        reader: StreamReader,
        sink: FrameSink,
    ) -> None:
        self._server = server
        self._journal = journal
        self._reader = reader
        self._sink = _CountingSink(sink)
        # Seeded from the run id (see ``random``): per-run determinism at
        # the contract's own door, the coder facade's discipline — even
        # though nothing in this wrapper ever draws.
        self._rng_state = int(journal.run_id.hex[:16], 16)

    # -- run scope (the facade contract's own carrier) ---------------------

    @property
    def correlation_id(self) -> uuid.UUID:
        return self._journal.run_id

    @property
    def session_id(self) -> uuid.UUID | None:
        return None  # an MCP session is not a chat session

    @property
    def modality(self) -> ComplexityModality:
        return ComplexityModality.STANDARD

    @property
    def permission_mode(self) -> PermissionMode:
        return PermissionMode.AUTO  # the operator spawned the server on purpose

    @property
    def tokens_budget(self) -> int:
        return 0  # the wrapper calls no model; a budget of nothing, stated

    @property
    def requests_answered(self) -> int:
        """Frames emitted so far — live, for the driver's summary line."""
        return self._sink.frames

    # -- the one typed operation -------------------------------------------

    async def serve_session(self, harness: str = "") -> McpHostOutcome:
        """Serve the external client to EOF, journalled as one call.

        ``harness`` is the operator's label for the client they are
        attaching (``paa pipeline run builtin.mcp-host --goal
        "claude-code"``); the ``paa mcp serve`` door passes none. It rides
        the PROPOSED row's params — fingerprinted, shape-previewed, never
        echoed into a metric — and nowhere else.
        """
        params: dict[str, Any] = {
            "server": SERVER_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "tools": list(TOOL_MANIFEST),
            "harness": harness,
        }
        await self._journal.call_proposed(1, SESSION_METHOD, params)
        try:
            await self._server.serve(self._reader, self._sink)
        except asyncio.CancelledError:
            # Shielded: a session cancelled mid-serve must still leave the
            # audit that the call never resolved — the sweep later closes
            # an open PROPOSED as interrupted, and this row says more: it
            # says cancelled, by name.
            await asyncio.shield(
                self._journal.call_failed(1, SESSION_METHOD, "cancelled")
            )
            raise
        except BaseException as exc:
            # The FAILED row is shielded for the same reason, and its text
            # is the fault's *type*, never its message: a transport error
            # can carry frame text, and frame text is the client's words.
            await asyncio.shield(
                self._journal.call_failed(
                    1, SESSION_METHOD, f"session did not resolve ({type(exc).__name__})"
                )
            )
            raise
        outcome = McpHostOutcome(requests_answered=self._sink.frames)
        await self._journal.call_completed(
            1, SESSION_METHOD, {"requests_answered": outcome.requests_answered}
        )
        return outcome

    # -- refused capabilities (the door is elsewhere, and says so) --------

    async def think(
        self,
        prompt: str,
        *,
        schema: Any = None,
        role: str = "worker",
        budget: int | None = None,
    ) -> dict[str, Any]:
        raise PaaError(
            "builtin.mcp-host's facade is the session's accounting wrapper; "
            "the external client's model calls cross the server's own "
            "per-call gate, not this door",
            actual="builtin.mcp-host",
        )

    async def call_agent(self, role: Any, message: Any, agent_ctx: Any) -> Any:
        raise PaaError(
            "builtin.mcp-host's facade is the session's accounting wrapper; "
            "agent delegation is not a door it owns",
            actual="builtin.mcp-host",
        )

    async def use_skill(self, name: str, args: Any) -> str:
        raise PaaError(
            "builtin.mcp-host's facade is the session's accounting wrapper; "
            "the external client's tool calls cross the server's own "
            "per-call gate, not this door",
            actual="builtin.mcp-host",
        )

    async def recall(self, query: str, *, k: int = 10) -> list[dict[str, Any]]:
        raise PaaError(
            "builtin.mcp-host's facade is the session's accounting wrapper; "
            "the external client's memory reads cross the server's own "
            "per-call gate, not this door",
            actual="builtin.mcp-host",
        )

    async def remember(self, fact: Any) -> None:
        raise PaaError(
            "builtin.mcp-host's facade is the session's accounting wrapper; "
            "the external client's memory writes cross the server's own "
            "per-call gate, not this door",
            actual="builtin.mcp-host",
        )

    async def gate(self, role: Any, payload: Any) -> Any:
        raise PaaError(
            "builtin.mcp-host's facade is the session's accounting wrapper; "
            "it opens no human gates",
            actual="builtin.mcp-host",
        )

    async def submit_plan(
        self,
        steps: Any,
        *,
        requirements: Any = None,
        bypassed_planner: bool = False,
    ) -> None:
        raise PaaError(
            "builtin.mcp-host's facade is the session's accounting wrapper; "
            "it submits no plans",
            actual="builtin.mcp-host",
        )

    async def check_policy(self, state: Any) -> Any:
        raise PaaError(
            "builtin.mcp-host's facade is the session's accounting wrapper; "
            "the server's per-call gate is the policy door",
            actual="builtin.mcp-host",
        )

    async def commit(self, *, steps_completed: int, tokens_consumed: int) -> None:
        raise PaaError(
            "builtin.mcp-host's facade is the session's accounting wrapper; "
            "it commits nothing",
            actual="builtin.mcp-host",
        )

    async def sub_run(self, name: str, task_input: Any) -> PipelineResult:
        raise PaaError(
            "builtin.mcp-host's facade is the session's accounting wrapper; "
            "an externally-driven session delegates nothing",
            actual="builtin.mcp-host",
        )

    # -- determinism aids and heartbeat ------------------------------------

    async def now(self) -> datetime:
        """Wall time. The wrapper reads no clock for replay (``resume="none"``)."""
        return datetime.now(UTC)

    async def random(self) -> float:
        """The door exists because the contract has it; the wrapper never draws.

        Seeded from the run id with the coder's tiny LCG anyway — observe's
        rule: a door on the facade contract that behaves differently per
        run would be a determinism hole the next adapter could fall into.
        """
        self._rng_state = (
            self._rng_state * 6364136223846793005 + 1442695040888963407
        ) % (1 << 64)
        return self._rng_state / (1 << 64)

    async def progress(self, status: str) -> None:
        """Heartbeat; never journalled."""

    # -- in-process transport reads -----------------------------------------

    @property
    def agent_ctx(self) -> Any:
        raise PaaError(
            "builtin.mcp-host drives no agent lineage; there is no agent context",
            actual="builtin.mcp-host",
        )

    async def stage_question(self, role: Any) -> None:
        return None  # in-process transport read; the wrapper asks no gates

    async def project(self) -> Any:
        raise PaaError(
            "builtin.mcp-host drives no task lineage; there is no projection to read",
            actual="builtin.mcp-host",
        )


class McpHostPipeline:
    """``builtin.mcp-host``: serve the six tools, journal the session.

    The control flow is one call and a verdict, the observe shape: the
    harness label comes from the task's goal, the streams come from the
    host-minted facade, and a session that reaches EOF is ``completed``
    — the fate of the individual tool calls inside it is their own
    lineages' story (a failed tool result is still a completed protocol
    operation, the server's ``isError`` contract), so the wrapper never
    pretends a bumpy session was a failed run.
    """

    name = "builtin.mcp-host"
    version = "4.1.0"
    requires_runtime = ">=4.1,<5"
    required_permissions: frozenset[Permission] = frozenset()
    capabilities = MCP_HOST_CAPABILITIES
    limits = RunLimits()

    async def run(self, task: PipelineTask, ctx: CapabilityFacade) -> PipelineResult:
        facade = self._narrow(ctx)
        outcome = await facade.serve_session(harness=task.goal)
        return self._result(outcome)

    def _result(self, outcome: McpHostOutcome) -> PipelineResult:
        return PipelineResult(
            status=PipelineStatus.COMPLETED,
            summary=f"served {outcome.requests_answered} request(s) to EOF",
            metrics={
                "requests_answered": outcome.requests_answered,
                "evidence_tier": "T2",
                "resume": "none",
            },
        )

    def _narrow(self, ctx: CapabilityFacade) -> McpHostFacade:
        """Narrow the protocol to the facade the mcp-host host mints.

        The coder's rule, restated: a foreign facade is a host bug worth
        naming, not a duck type to tolerate.
        """
        if not isinstance(ctx, McpHostFacade):
            raise PaaError(
                "builtin.mcp-host requires the host-minted McpHostFacade",
                actual=type(ctx).__name__,
            )
        return ctx


async def run_mcp_host_session(
    server: MCPServer,
    ledger: LedgerStore,
    *,
    reader: StreamReader,
    sink: FrameSink,
    harness: str = "",
) -> tuple[uuid.UUID, PipelineResult]:
    """One ``builtin.mcp-host`` run on its own lineage — the shared driver.

    Both doors (``paa mcp serve`` and ``paa pipeline run
    builtin.mcp-host``) come through here so the journal rows are
    identical whichever front door the operator chose — the observe
    pattern, restated for the session whose "command" is the client
    itself. The run opens before the first frame is read and completes
    at EOF; a transport death or a cancel closes the run FAILED (the
    fault's type only, never its message — frame text is the client's
    words) before the exception propagates.
    """
    from paa.core.types import new_correlation_id

    run_id = new_correlation_id()
    journal = CallJournal(ledger, run_id)
    await journal.run_started(
        McpHostPipeline.name,
        McpHostPipeline.version,
        run_kind="mcp-host",
        resume="none",
        evidence_tier="T2",
    )
    try:
        facade = McpHostFacade(
            server=server, journal=journal, reader=reader, sink=sink
        )
        task = PipelineTask(
            correlation_id=run_id, session_id=None, goal=harness, params={}
        )
        result = await McpHostPipeline().run(task, facade)
    except asyncio.CancelledError:
        await journal.run_failed("cancelled")
        raise
    except Exception as exc:
        reason = exc.message if isinstance(exc, PaaError) else type(exc).__name__
        await journal.run_failed(reason)
        raise
    await journal.run_completed(result.status.value, metrics=dict(result.metrics))
    return run_id, result
