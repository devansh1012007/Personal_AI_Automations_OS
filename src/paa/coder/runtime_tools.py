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

import json
import uuid
from typing import TYPE_CHECKING, Any

from paa.ledger.replay import project

if TYPE_CHECKING:
    from collections.abc import Mapping

    from paa.runtime import Runtime

__all__ = ["build_runtime_tools"]


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
            modality=_as_modality(arguments.get("modality")),
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


def _as_modality(value: Any) -> Any:
    """Map a modality name onto the enum, tolerating the model's casing."""
    if not isinstance(value, str) or not value.strip():
        return None
    from paa.core.types import ComplexityModality

    try:
        return ComplexityModality(value.strip().upper())
    except ValueError:
        return None


def _clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default