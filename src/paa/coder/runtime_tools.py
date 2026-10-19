"""Concrete tools that let the coder agent exercise the runtime it is wired to.

The README is blunt about this: the orchestrator "has no hands" — there are no
built-in tools for file editing, bash, or the network, and :mod:`paa.coder` has
never been composed into a :class:`~paa.runtime.Runtime` at all. Until the real
tool suite lands, this module supplies the smallest set that makes the harness
*testable from the outside*: read a lineage's state, read its events, submit a
task, drive it, and verify the hash chain.

That is deliberately the ledger surface and not the filesystem. It is the one
part of the system where "what actually happened" is answerable, and answering
it is what a testing agent is for. A tool suite that mutated files would be
adding capability — which the RFC's §13 gates and this module's callers have not
asked for.

Memory joined the catalogue later, for a different reason. ``remember_fact`` /
``recall_facts`` expose the subsystem the daemon already exercises
(:class:`paa.memory.creator.MemoryCreator`): rule-based extraction, no model
call, contradiction quarantined rather than auto-resolved. What they add is
continuity — the agent can hold a fact across turns and sessions — not new
capability the runtime does not already have on its ingest path.

Two conventions every tool here follows:

* **Read the projection, not the caches.** ``task_status`` calls
  :func:`paa.ledger.replay.project`, the same left fold the recovery sweep and
  :class:`~paa.runtime.Runtime` use. A tool that queried a derived index could
  report a state that replay would contradict.
* **Return JSON, and return errors as values.** The loop feeds a tool's return
  string straight back to the model, and a raised exception becomes
  ``"error: ..."`` with no structured detail. So a bad correlation id is
  reported *as* a JSON payload the model can reason about, not as a traceback
  it has to interpret. Mutations still raise on genuine faults — silently
  swallowing a failed write would be the one failure mode a test agent must not
  have.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from paa.ledger.replay import project
from paa.memory.creator import MemoryCreator, ProcessingOutcome, SignalEnvelope
from paa.memory.facts import Fact, FactRepository
from paa.storage.coldlake.cas import ContentAddressedStore
from paa.storage.coldlake.signals import Signal, SignalRepository
from paa.storage.relational.database import dumps

if TYPE_CHECKING:
    from collections.abc import Mapping

    from paa.runtime import Runtime

__all__ = ["READ_ONLY_TOOLS", "as_modality", "build_runtime_tools"]

#: The catalogue's non-mutating tools. :class:`paa.coder.agent.CoderAgent`
#: standing-allows these in ASK mode so a permission prompt fires only for
#: calls that change state — prompting for ``check_runtime({})`` is friction
#: that trains operators to approve everything, including the calls that
#: matter. They stay deniable by name because an explicit deny rule is
#: precedence step 1 in :class:`~paa.coder.permissions.ToolPermissionPolicy`
#: and outranks any allow.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "check_runtime",
        "list_tasks",
        "task_status",
        "read_ledger",
        "verify_ledger",
        "recall_facts",
    }
)

#: The cold-lake channel the coder agent's assertions arrive on. A channel of
#: its own so the agent's writes are attributable in the cold lake, and so
#: their idempotency key cannot collide with another channel's.
_CODER_CHANNEL = "coder"


def build_runtime_tools(rt: Runtime) -> dict[str, Any]:
    """The tool catalogue, bound to one live runtime.

    Bound rather than global because every tool reaches through the runtime's
    single :class:`~paa.ledger.store.LedgerStore` — sharing the runtime means
    the agent's reads and the daemon's writes land in the same transaction
    boundary, and closing the runtime closes the agent's access with it.
    """

    return {
        t.name: t
        for t in (
            _CheckRuntime(rt),
            _ListTasks(rt),
            _TaskStatus(rt),
            _SubmitTask(rt),
            _RunTask(rt),
            _ReadLedger(rt),
            _VerifyLedger(rt),
            _RememberFact(rt),
            _RecallFacts(rt),
        )
    }


class _RuntimeTool:
    """Shared plumbing: the ``Tool`` protocol's three members plus JSON output."""

    name: str
    description: str

    def __init__(self, rt: Runtime) -> None:
        self._rt = rt

    def schema(self) -> dict[str, Any]:  # pragma: no cover - overridden everywhere
        raise NotImplementedError

    async def run(self, arguments: Mapping[str, Any]) -> str:  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    def _emit(payload: dict[str, Any]) -> str:
        return json.dumps(payload, indent=2, sort_keys=True, default=str)


# ---------------------------------------------------------------------------


class _CheckRuntime(_RuntimeTool):
    name = "check_runtime"
    description = (
        "Report which backends, model provider and sandbox are actually live, "
        "and how many tasks are open. Use this first to establish what the "
        "runtime really is before drawing conclusions from any other tool."
    )

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def run(self, arguments: Mapping[str, Any]) -> str:
        settings = self._rt.settings
        router = self._rt.model_router
        worker = self._rt.agents.get("worker")
        sandbox = getattr(worker, "_sandbox", None)
        return self._emit(
            {
                "permission_mode": settings.policy.mode.value,
                "home": str(settings.home),
                "relational_backend": settings.storage.backend_relational,
                "vector_backend": type(self._rt.vector_store).__name__
                if self._rt.vector_store
                else None,
                "graph_backend": type(self._rt.graph_store).__name__
                if self._rt.graph_store
                else None,
                "queue_backend": type(self._rt.queue).__name__ if self._rt.queue else None,
                "model_router": type(router).__name__ if router else None,
                "local_provider": type(router.local).__name__ if router else None,
                "frontier_provider": type(router.frontier).__name__
                if router and router.frontier
                else None,
                "sandbox": sandbox.name if sandbox is not None else None,
                "sandbox_isolation": str(getattr(sandbox, "isolation_level", "none")),
                "open_tasks": len(await self._rt.ledger.open_correlations()),
            }
        )


class _ListTasks(_RuntimeTool):
    name = "list_tasks"
    description = (
        "List task lineages that have not reached a terminal state, oldest "
        "first. These are the ones boot recovery would re-queue."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum lineages to return. Default 20.",
                }
            },
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        limit = _clamp_int(arguments.get("limit"), default=20, low=1, high=200)
        heads = await self._rt.ledger.open_correlations(limit=limit)
        return self._emit(
            {
                "count": len(heads),
                "tasks": [
                    {
                        "correlation_id": str(h.correlation_id),
                        "latest_event": h.latest_event_type.value,
                        "state_version": h.latest_state_version,
                        "updated_at": h.updated_at.isoformat() if h.updated_at else None,
                    }
                    for h in heads
                ],
            }
        )


class _TaskStatus(_RuntimeTool):
    name = "task_status"
    description = (
        "Replay one task's lineage and return its current phase, plan progress, "
        "attempts, token spend, and any policy or awaiting reason. This is "
        "derived from the ledger by replay, not read from a cache."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "correlation_id": {"type": "string", "description": "The task's UUID."}
            },
            "required": ["correlation_id"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        cid = _as_uuid(arguments.get("correlation_id"))
        if isinstance(cid, str):
            return cid  # an error payload, already JSON
        projection = await project(self._rt.ledger, cid)
        return self._emit(
            {
                "correlation_id": str(projection.correlation_id),
                "phase": projection.phase.value,
                "is_terminal": projection.is_terminal,
                "needs_recovery": projection.needs_recovery,
                "modality": projection.modality.value,
                "state_version": projection.state_version,
                "steps_completed": len(projection.completed_steps),
                "steps_total": len(projection.plan_steps),
                "attempts": projection.attempts,
                "tokens_consumed": projection.tokens_consumed,
                "escalation_count": projection.escalation_count,
                "rollback_count": projection.rollback_count,
                "policy_decision": projection.policy_decision,
                "policy_reason": projection.policy_reason,
                "awaiting_reason": projection.awaiting_reason,
                "validation_verdict": projection.validation_verdict,
                "errors": projection.errors,
                "unknown_events": projection.unknown_events,
            }
        )


class _SubmitTask(_RuntimeTool):
    name = "submit_task"
    description = (
        "Submit a new task to the orchestrator and return its correlation id. "
        "Records the request in the ledger; does not execute it. Call run_task "
        "with the returned id to drive it."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "minLength": 1, "description": "What to do."},
                "modality": {
                    "type": "string",
                    "enum": ["SIMPLE", "STANDARD", "COMPLEX", "MAX"],
                    "description": "Complexity. Default STANDARD.",
                },
                "target_agent": {
                    "type": "string",
                    "description": "Bypass routing and target one agent.",
                },
            },
            "required": ["goal"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        goal = arguments.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return self._emit({"error": "submit_task requires a non-empty 'goal'"})
        cid = await self._rt.submit(
            goal.strip(),
            modality=as_modality(arguments.get("modality")),
            target_agent=arguments.get("target_agent") or None,
        )
        return self._emit({"correlation_id": str(cid), "goal": goal.strip()})


class _RunTask(_RuntimeTool):
    name = "run_task"
    description = (
        "Drive a submitted task to a terminal state. Returns its final phase "
        "and any error. This writes to the ledger and may execute work, so it "
        "is the tool most likely to be gated by a permission prompt."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "correlation_id": {"type": "string", "description": "A submitted task's UUID."}
            },
            "required": ["correlation_id"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        cid = _as_uuid(arguments.get("correlation_id"))
        if isinstance(cid, str):
            return cid  # an error payload, already JSON
        outcome = await self._rt.run(cid)
        return self._emit(
            {
                "correlation_id": str(outcome.correlation_id),
                "phase": outcome.phase.value,
                "ok": outcome.ok,
                "error": outcome.error,
                "steps_completed": len(outcome.projection.completed_steps),
                "steps_total": len(outcome.projection.plan_steps),
                "attempts": outcome.projection.attempts,
            }
        )


class _ReadLedger(_RuntimeTool):
    name = "read_ledger"
    description = (
        "Read one lineage's event chain in replay order. This is the "
        "explainability view: it shows what was recorded, by which agent, in "
        "which order. Use from_version to page forward."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "correlation_id": {"type": "string", "description": "The task's UUID."},
                "from_version": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Return events with state_version above this. Default 0.",
                },
            },
            "required": ["correlation_id"],
            "additionalProperties": False,
        }

    #: Events beyond this are summarised by count rather than dropped, so the
    #: model learns the chain continues instead of concluding it ended.
    _INLINE_LIMIT = 40

    async def run(self, arguments: Mapping[str, Any]) -> str:
        cid = _as_uuid(arguments.get("correlation_id"))
        if isinstance(cid, str):
            return cid  # an error payload, already JSON
        from_version = _clamp_int(arguments.get("from_version"), default=0, low=0, high=10**9)
        events = await self._rt.ledger.read_correlation(cid, from_version=from_version)
        payload: dict[str, Any] = {
            "correlation_id": str(cid),
            "event_count": len(events),
            "from_version": from_version,
        }
        if not events:
            payload["events"] = []
            payload["note"] = "no events at or after that version — check the correlation id"
            return self._emit(payload)
        shown = events[: self._INLINE_LIMIT]
        payload["events"] = [
            {
                "state_version": e.state_version,
                "event_type": e.event_type.value,
                "agent_role": e.agent_role,
                "attempt": e.attempt,
                "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
                "payload": e.payload,
            }
            for e in shown
        ]
        if len(events) > len(shown):
            payload["truncated"] = (
                f"showing first {len(shown)} of {len(events)}; "
                f"page forward with from_version={shown[-1].state_version}"
            )
        return self._emit(payload)


class _VerifyLedger(_RuntimeTool):
    name = "verify_ledger"
    description = (
        "Verify one lineage's hash chain: that every stored digest recomputes, "
        "that prev_hash links, and that state_versions are dense and monotonic. "
        "Use to distinguish 'the task behaved unexpectedly' from 'the history "
        "was altered'."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "correlation_id": {"type": "string", "description": "The task's UUID."}
            },
            "required": ["correlation_id"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        cid = _as_uuid(arguments.get("correlation_id"))
        if isinstance(cid, str):
            return cid  # an error payload, already JSON
        ok, problems = await self._rt.ledger.verify_chain(cid)
        return self._emit({"correlation_id": str(cid), "chain_ok": ok, "problems": problems})


class _RememberFact(_RuntimeTool):
    """Write one free-text assertion into long-term memory.

    Extraction is the daemon's :class:`~paa.memory.creator.RuleBasedExtractor`
    — deterministic, rule-based, **no model call** — so what lands in memory is
    what the rules can parse, and nothing is fabricated when they cannot:
    conversational prose comes back ``IGNORED`` with ``facts_written: 0``.
    Contradicting an incumbent belief quarantines the whole signal (RFC §4.2):
    the runtime never picks a winner between conflicting facts, so
    ``requires_human_attestation`` is reported as a *value*, not an exception.

    **The write goes through the cold lake.** The assertion is filed as a
    ``cold_lake_signals`` row first — the same front door every other channel
    uses (RFC §1.2) — and only then turned into facts by
    :meth:`paa.memory.creator.MemoryCreator.process_signal`, bound to that
    row. Three things follow, and they are the point:

    * The fact carries a real ``source_signal_id`` foreign key, so its
      evidence is walkable: the id resolves to a row in ``cold_lake_signals``
      holding the verbatim assertion. **That row is where the evidence
      lives** — the fact's ``provenance`` blob only names the extractor and
      the rule that produced it.
    * Hot serving is derived from the cold lake, like every other memory in
      the system. Replaying the row through the creator re-derives the same
      facts, because extraction is deterministic, so the agent's own memories
      are rebuildable like everyone else's.
    * No ledger event is written, and none is needed: memory is not a task
      lineage, so there is no lineage to record it in. The cold-lake row is
      the durable record of the assertion, in the same way the ledger is the
      durable record of a task. The row is written *before* the facts — the
      event-before-side-effect ordering — and the creator's commit writes the
      facts and closes the row out in one transaction.

    **Why the daemon cannot write the same fact twice.**
    :meth:`paa.memory.creator.MemoryCreator.run_batch` claims the rows whose
    ``sync_status`` is ``unprocessed`` and flips them to ``processing``; the
    creator's commit then sets them to ``processed``. This tool takes that
    same claim for its own row — a conditional ``UPDATE`` that fires only
    while the row is still ``unprocessed`` — so exactly one of the two paths
    owns the row, and the loser writes no facts. If the daemon won the race,
    the tool waits for the row to settle and reports what the row says.
    See ``docs/adr/0022-coder-memory-writes.md``.

    **Idempotence.** The cold-lake external id is the digest of the
    assertion, so the same text is the same row: a retried call re-uses the
    signal it filed the first time instead of filing a second one, and the
    tool reports the fact that is already there rather than writing a
    duplicate of it.

    Genuine database faults raise, per this module's convention for mutations.
    """

    name = "remember_fact"
    description = (
        "Remember a fact in long-term memory, for recall across turns and "
        "sessions. Input is free text; deterministic rules extract "
        "'<subject> <predicate> <object>' sentences (e.g. 'Project Alpha is "
        "active') — no model call. Text that asserts nothing is honestly "
        "reported as ignored with zero facts. Contradicting a stored belief "
        "quarantines the write for human attestation rather than overwriting."
    )

    #: How long to wait for a signal another worker claimed to settle before
    #: reporting it as still in flight. The daemon's ingestion loop runs every
    #: second, so this is generous for the common case and short enough that a
    #: stuck row does not hang the agent's turn.
    _SETTLE_TIMEOUT_SECONDS = 2.0
    _SETTLE_POLL_SECONDS = 0.05

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "What to remember, e.g. 'Project Alpha is active'. "
                        "Conversational noise extracts no facts and is ignored."
                    ),
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return self._emit({"error": "text must be non-empty"})
        text = text.strip()

        repo = self._signal_repo()
        external_id = _external_id(text)
        signal = await repo.record(_CODER_CHANNEL, dumps(text), external_id=external_id)

        if signal.sync_status == "unprocessed" and await self._claim(signal.id):
            # We own the row. The creator's commit flips it to 'processed' in
            # the same transaction as the facts, so the daemon cannot re-claim
            # it afterwards.
            outcome, fact_ids, entity_ids = await self._process(repo, signal, text, external_id)
            settled = await repo.get(signal.id)
            if settled is not None:
                # The row the creator closed out, not the one we filed: its
                # status is the verdict the tool reports.
                signal = settled
            replay = False
        else:
            # Either this call is a retry of one that already settled, or the
            # daemon claimed the row first. Neither is a reason to write facts
            # a second time.
            signal = await self._await_settled(repo, signal.id)
            outcome, fact_ids, entity_ids = await self._settled_outcome(signal)
            replay = True

        return self._emit(self._report(outcome, fact_ids, entity_ids, signal, replay=replay))

    # -- the write path ----------------------------------------------------

    def _signal_repo(self) -> SignalRepository:
        """The cold-lake front door over this runtime's database.

        The content-addressed store is the one the ingestion API already uses
        (:func:`paa.api.app._signal_repo`): constructing it creates the two
        directories it needs and it is only *used* for a payload over 64 KB,
        which an assertion never is. The tool does not get to pick a different
        archive from the rest of the runtime.
        """
        cas = ContentAddressedStore(self._rt.settings.storage.cold_lake_path)
        return SignalRepository(self._rt.db, cas)

    async def _claim(self, signal_id: str) -> bool:
        """Take exclusive ownership of one cold-lake row.

        The daemon's claim (:meth:`paa.memory.creator.MemoryCreator._claim`)
        selects the rows whose ``sync_status`` is ``unprocessed`` and flips
        them to ``processing`` in one transaction, so a row that is already
        ``processing`` belongs to somebody else. This is the same claim,
        scoped to a single row: a conditional ``UPDATE`` that fires only while
        the row is still ``unprocessed``. Whoever wins writes the facts; the
        loser writes nothing.

        :meth:`paa.storage.coldlake.signals.SignalRepository.claim_unprocessed`
        cannot do this — it claims by arrival order, so it would take other
        channels' rows along with ours.
        """
        changed = await self._rt.db.execute(
            "UPDATE cold_lake_signals SET sync_status = 'processing' "
            "WHERE id = ? AND sync_status = 'unprocessed'",
            (signal_id,),
        )
        return changed == 1

    async def _process(
        self,
        repo: SignalRepository,
        signal: Signal,
        text: str,
        external_id: str,
    ) -> tuple[str, list[str], list[str]]:
        """Turn the claimed signal into facts, and report what happened.

        The envelope is the cold-lake row: ``persisted=True`` is what makes the
        creator write the fact's ``source_signal_id`` and close the row out.
        ``raw`` is the assertion as it was spoken, not the JSON the row stores,
        so the creator's containment check sees the original bytes — a NUL or
        an ANSI escape in the text is rejected here exactly as it is on the
        daemon's path.
        """
        envelope = SignalEnvelope(
            id=signal.id,
            channel=_CODER_CHANNEL,
            payload=text,
            raw=text,
            external_id=external_id,
            received_at=signal.received_at,
            persisted=True,
        )
        try:
            result = await MemoryCreator(self._rt.db).process_signal(envelope)
        except Exception:
            # The claim is ours and the facts are not written. Hand the row
            # back: a row stuck in 'processing' is invisible to every poller,
            # and the cold lake is the source of truth for this assertion.
            await repo.release(signal.id)
            raise
        return (
            result.outcome.value,
            list(result.facts_written),
            list(result.entities_touched),
        )

    async def _await_settled(self, repo: SignalRepository, signal_id: str) -> Signal:
        """Wait, briefly, for a signal another worker claimed to settle.

        The daemon claims a row by flipping it to ``processing`` and then
        processes it, which lands it on ``processed``, ``quarantined`` or
        ``malformed``. A row still ``processing`` when the wait runs out
        belongs to a worker that is still working on it, and the tool says so
        rather than pretending the fact is not there.
        """
        deadline = time.monotonic() + self._SETTLE_TIMEOUT_SECONDS
        while True:
            current = await repo.get(signal_id)
            if current is None:  # pragma: no cover - the row was just written
                raise KeyError(f"cold-lake signal {signal_id} vanished")
            if current.sync_status != "processing":
                return current
            if time.monotonic() >= deadline:
                return current
            await asyncio.sleep(self._SETTLE_POLL_SECONDS)

    async def _settled_outcome(self, signal: Signal) -> tuple[str, list[str], list[str]]:
        """What a signal that is not ours says about itself.

        The row's status is the verdict the creator recorded: ``processed``
        (facts written, or nothing worth writing), ``quarantined`` (a human
        must decide) or ``malformed`` (contained, nothing written).

        Two statuses are not verdicts. ``processing`` means another worker
        still owns the row; ``unprocessed`` means nobody does — the row was
        released, so the daemon will pick it up. Both report as ``pending``:
        the fact is not in memory yet, and the tool says so rather than
        dressing an unfinished write up as a result. The row's own status is
        in the report, so the model can tell the two apart.
        """
        status = signal.sync_status
        if status in ("processing", "unprocessed"):
            return "pending", [], []
        if status == "quarantined":
            return ProcessingOutcome.QUARANTINED.value, [], []
        if status == "malformed":
            return ProcessingOutcome.MALFORMED.value, [], []

        facts = await FactRepository(self._rt.db).facts_by_source_signal(signal.id)
        if not facts:
            return ProcessingOutcome.IGNORED.value, [], []
        return (
            ProcessingOutcome.PROCESSED.value,
            [f.id for f in facts],
            [f.entity_id for f in facts],
        )

    def _report(
        self,
        outcome: str,
        fact_ids: list[str],
        entity_ids: list[str],
        signal: Signal,
        *,
        replay: bool,
    ) -> dict[str, Any]:
        """The tool's answer. Honest about where the evidence lives.

        ``source_signal_id`` is the foreign key the fact carries, and it
        resolves to the ``cold_lake_signals`` row named in ``provenance`` —
        the raw assertion, verbatim. The tool does not claim a chain of
        evidence it cannot walk.

        ``facts_written`` counts the facts the assertion produced. On an
        idempotent replay the same facts are counted again and
        ``idempotent_replay`` says so, so the model can tell "I wrote it" from
        "it was already there".

        ``outcome`` is one of the :class:`~paa.memory.creator.ProcessingOutcome`
        values, plus ``pending`` for a signal another worker has not finished
        — the one case where the fact is not in memory yet and the tool says
        so rather than claiming a result it does not have.
        """
        return {
            "remembered": outcome == ProcessingOutcome.PROCESSED.value and bool(fact_ids),
            "facts_written": len(fact_ids),
            "outcome": outcome,
            "requires_human_attestation": outcome == ProcessingOutcome.QUARANTINED.value,
            "entities": list(entity_ids),
            "source_signal_id": signal.id,
            "provenance": {
                "source": "cold_lake_signals",
                "channel": signal.channel,
                "external_id": signal.external_id,
                "sync_status": signal.sync_status,
            },
            "idempotent_replay": replay,
        }


class _RecallFacts(_RuntimeTool):
    """Read memory back: entity resolution first, keyword search as fallback.

    Two tiers because the memory subsystem offers two indexes and neither
    covers everything. ``resolve_entity`` handles "what do I know about
    Project Alpha" — exact, alias, then trigram-fuzzy, the ladder the fact
    repository already implements. When the query is a *phrase* rather than an
    entity name ("Acme", "deploy pipeline"), resolution misses and
    :meth:`~paa.memory.facts.FactRepository.search_facts_by_text` scans fact
    predicates and object values instead.

    Semantic (vector) recall is honestly absent in v1: a fact only reaches the
    vector index when the creator is built with an embedding sink
    (:class:`paa.memory.creator.EmbeddingSink`), and nothing in the runtime
    wires one, so the response says so in ``note`` rather than pretending the
    absence is a miss. Confidence is the decayed effective value, because
    :meth:`~paa.memory.facts.FactRepository.query_facts` decays at read time by
    design.
    """

    name = "recall_facts"
    description = (
        "Recall previously remembered facts. The query is matched against "
        "entity names first (exact, alias, then fuzzy), falling back to a "
        "keyword search over fact predicates and values. Confidence is "
        "time-decayed. Facts are not embedded, so recall is keyword-based, "
        "not semantic."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "An entity name ('Project Alpha') or keywords ('Acme').",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum facts to return. Default 10.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return self._emit({"error": "query must be non-empty"})
        query = query.strip()
        limit = _clamp_int(arguments.get("limit"), default=10, low=1, high=50)

        repo = FactRepository(self._rt.db)
        facts: list[Fact] = []
        entity = await repo.resolve_entity(query)
        if entity is not None:
            facts = await repo.query_facts(entity_id=entity.id, limit=limit)
        if not facts:
            facts = await repo.search_facts_by_text(query, limit=limit)

        return self._emit(
            {
                "query": query,
                "facts": [
                    {
                        "entity": f.entity_name or f.entity_id,
                        "predicate": f.predicate,
                        "object_value": f.object_value,
                        "confidence": f.confidence,
                        "domain": f.domain,
                    }
                    for f in facts
                ],
                "count": len(facts),
                "note": (
                    "semantic recall unavailable: facts are not embedded "
                    "(no vector sink wired)"
                ),
            }
        )


# ---------------------------------------------------------------------------
# Argument coercion. The loop hands us whatever the model produced, and the
# schema is best-effort (see model_adapter._as_object), so these normalise
# rather than trust.
# ---------------------------------------------------------------------------


def _as_uuid(value: Any) -> uuid.UUID | str:
    """Parse a correlation id, or return a JSON error payload as a string."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return json.dumps(
            {
                "error": "invalid correlation id",
                "received": str(value)[:120],
                "expected": "a UUID, e.g. from submit_task or list_tasks",
            },
            sort_keys=True,
        )


def as_modality(value: Any) -> Any:
    """Map a modality name onto the enum, tolerating the model's casing.

    Public because the CLI parses its own ``--modality`` flag with it: the
    operator's typing and the model's have to agree on what "complex" means,
    and a private name would be a cross-module import of somebody else's
    internals. Returns ``None`` for anything it cannot parse.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    from paa.core.types import ComplexityModality

    try:
        return ComplexityModality(value.strip().upper())
    except ValueError:
        return None


def _external_id(text: str) -> str:
    """The cold-lake idempotency key for one assertion.

    The digest of the text, so the same assertion is always the same row:
    ``cold_lake_signals`` is unique on ``(channel, external_id)``, so a retried
    call re-uses the signal it filed the first time instead of filing a
    second one — and the tool reports the fact that is already there rather
    than writing a duplicate of it.

    A per-turn id would have been the other option, but it would make the key
    depend on the caller remembering to supply one, and the model is not a
    reliable source of ids. The assertion itself is the thing being made
    idempotent, so it is what the key is derived from.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_CODER_CHANNEL}:{digest}"


def _clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default