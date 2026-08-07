"""FastAPI ingestion app — the runtime's edge.

RFC §9 boundary: this layer is a **type-safe parser and validator only**. It
does not hold application state, make model calls, or write to operational
tables directly. A request becomes either a ledger event (via the orchestrator)
or a cold-lake row (via the signal repository), and nothing more.

Two guarantees enforced here:

* **Loopback only.** ``Settings.api_host`` is validated to be loopback unless
  ``PAA_ALLOW_NON_LOOPBACK=1``, so the RFC's "no public edge ingestion" rule is
  a startup check, not a deployment note.
* **Backpressure, not collapse.** When the queue is deep, non-essential
  ingestion returns HTTP 429 rather than accepting work the runtime cannot keep
  up with (RFC §6.2).

FastAPI is imported lazily inside :func:`create_app` so the rest of the package
imports without the ``api`` extra installed.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog

from paa import __version__
from paa.api.schemas import (
    GateRequest,
    HealthResponse,
    LedgerEventView,
    SignalRequest,
    SignalResponse,
    TaskRequestBody,
    TaskStatusResponse,
    TaskSubmitResponse,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from paa.runtime import Runtime

__all__ = ["create_app"]

log = structlog.get_logger(__name__)


def create_app(runtime: Runtime) -> FastAPI:
    """Build the FastAPI app bound to an already-built :class:`Runtime`.

    The runtime is injected rather than built here so the app and the daemon
    share one runtime (one database, one ledger) and so tests can pass a
    runtime wired with fakes.
    """
    from fastapi import FastAPI, HTTPException

    app = FastAPI(
        title="PAA Ingestion API",
        version=__version__,
        summary="Local-first cognitive runtime — ingestion edge",
    )

    # -- health --------------------------------------------------------
    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        open_tasks = len(await runtime.ledger.open_correlations(limit=10_000))
        return HealthResponse(
            status="ok",
            version=__version__,
            backends={
                "vector": type(runtime.vector_store).__name__ if runtime.vector_store else None,
                "graph": type(runtime.graph_store).__name__ if runtime.graph_store else None,
                "queue": type(runtime.queue).__name__ if runtime.queue else None,
                "model_router": (
                    type(runtime.model_router).__name__ if runtime.model_router else None
                ),
            },
            open_tasks=open_tasks,
        )

    # -- task submission ----------------------------------------------
    @app.post("/tasks", response_model=TaskSubmitResponse, status_code=201)
    async def submit_task(body: TaskRequestBody) -> TaskSubmitResponse:
        if not await _admit(runtime, essential=False):
            raise HTTPException(
                status_code=429, detail="runtime under backpressure; retry shortly"
            )
        cid = await runtime.submit(
            body.goal,
            modality=body.modality,
            required_slots=body.required_slots,
            workspace_path=body.workspace_path,
            target_agent=body.target_agent,
        )
        if body.run:
            outcome = await runtime.run(cid)
            return TaskSubmitResponse(
                correlation_id=str(cid), phase=outcome.phase.value, queued=False
            )
        return TaskSubmitResponse(correlation_id=str(cid), phase="QUEUED", queued=True)

    @app.get("/tasks/{correlation_id}", response_model=TaskStatusResponse)
    async def task_status(correlation_id: str) -> TaskStatusResponse:
        cid = _parse_uuid(correlation_id)
        head = await runtime.ledger.head(cid)
        if head is None:
            raise HTTPException(status_code=404, detail="no such task")
        p = await runtime.project(cid)
        return TaskStatusResponse(
            correlation_id=p.correlation_id,
            phase=p.phase.value,
            modality=p.modality.value,
            plan_steps=len(p.plan_steps),
            completed_steps=p.completed_steps,
            current_step_index=p.current_step_index,
            attempts=p.attempts,
            tokens_consumed=p.tokens_consumed,
            awaiting_reason=p.awaiting_reason,
            policy_reason=p.policy_reason,
            errors=p.errors,
            is_terminal=p.is_terminal,
        )

    @app.post("/tasks/{correlation_id}/gate", response_model=TaskStatusResponse)
    async def clear_gate(correlation_id: str, body: GateRequest) -> TaskStatusResponse:
        cid = _parse_uuid(correlation_id)
        from paa.core.errors import PaaError

        try:
            await runtime.orchestrator.clear_human_gate(
                cid, approved=body.approved, note=body.note
            )
        except PaaError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await task_status(correlation_id)

    @app.post("/tasks/{correlation_id}/correction", status_code=202)
    async def record_correction(correlation_id: str, body: dict[str, Any]) -> dict[str, str]:
        cid = _parse_uuid(correlation_id)
        await runtime.orchestrator.record_correction(cid, str(body.get("correction", "")))
        return {"status": "recorded"}

    # -- ledger (explainability) --------------------------------------
    @app.get("/ledger/{correlation_id}", response_model=list[LedgerEventView])
    async def ledger(correlation_id: str) -> list[LedgerEventView]:
        cid = _parse_uuid(correlation_id)
        events = await runtime.ledger.read_correlation(cid)
        if not events:
            raise HTTPException(status_code=404, detail="no such task")
        return [
            LedgerEventView(
                sequence_id=e.sequence_id,
                state_version=e.state_version,
                event_type=e.event_type.value,
                agent_role=e.agent_role,
                recorded_at=e.recorded_at.isoformat(),
                payload=e.payload,
            )
            for e in events
        ]

    # -- raw signal ingestion -----------------------------------------
    @app.post("/signals", response_model=SignalResponse, status_code=201)
    async def ingest_signal(body: SignalRequest) -> SignalResponse:
        # Signal ingestion is essential-ish but still shed under hard overload.
        if not await _admit(runtime, essential=False):
            raise HTTPException(status_code=429, detail="ingestion under backpressure")
        repo = _signal_repo(runtime)
        if repo is None:
            raise HTTPException(status_code=503, detail="cold lake not configured")
        # record() is idempotent on (channel, external_id); a pre-existing row
        # means this delivery is a duplicate. Reported via the `duplicate` flag
        # rather than a status code so the endpoint's return type stays a clean
        # model (dynamic status needs a Response injection, which
        # `from __future__ import annotations` + a lazily-imported FastAPI type
        # cannot express).
        pre_existing = (
            await repo.get_by_external_id(body.channel, body.external_id)
            if body.external_id
            else None
        )
        signal = await repo.record(body.channel, body.payload, body.external_id)
        return SignalResponse(
            signal_id=signal.id,
            channel=signal.channel,
            duplicate=pre_existing is not None,
        )

    log.info("api.app_created", version=__version__)
    return app


# ---------------------------------------------------------------------------


async def _admit(runtime: Runtime, *, essential: bool) -> bool:
    """Backpressure gate. Returns whether the request may proceed.

    Essential control-plane traffic is always admitted; only discretionary
    ingestion is shed, and only when the queue crosses the shed threshold.
    """
    if essential or runtime.queue is None:
        return True
    try:
        from paa.storage.queue.backpressure import BackpressureController
        from paa.storage.queue.base import StreamName

        depth = await runtime.queue.depth(StreamName.ORCHESTRATOR_CORE)
        controller = BackpressureController(runtime.settings.queue)
        state = controller.assess(depth)
        return controller.should_accept(StreamName.RAW_TELEMETRY, state)
    except Exception:
        # A backpressure probe failure must not block ingestion outright.
        return True


def _signal_repo(runtime: Runtime) -> Any:
    """Build a SignalRepository over the runtime's db + cold-lake CAS."""
    try:
        from paa.storage.coldlake.cas import ContentAddressedStore
        from paa.storage.coldlake.signals import SignalRepository

        cas = ContentAddressedStore(runtime.settings.storage.cold_lake_path)
        return SignalRepository(runtime.db, cas)
    except Exception as exc:
        log.warning("api.signal_repo_unavailable", error=str(exc))
        return None


def _parse_uuid(value: str) -> uuid.UUID:
    from fastapi import HTTPException

    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid correlation id") from exc
