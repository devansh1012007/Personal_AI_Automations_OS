"""Composition root — the one place the whole runtime is wired together.

Every other module is a part. This is where the parts become a system: it
constructs the ledger, storage substrates, model router, the agent hierarchy and
the orchestrator, hands each collaborator its dependencies, and exposes a single
handle to drive a task from request to commit.

Two design rules make this tractable:

**Everything is injected.** :meth:`Runtime.build` accepts overrides for every
collaborator, so a test can swap a real Qdrant store for a fake, or the model
router for an :class:`~paa.models.EchoProvider`-backed one, without touching the
wiring. The integration suite is the proof this pays off.

**Optional backends degrade, they do not crash.** A minimal install has no
qdrant, no kuzu, no redis. Each is attempted and, on ImportError or connection
failure, replaced by its in-process fallback with a logged warning. The runtime
must boot on a laptop with nothing but the core dependencies installed.

The known interface seam is the model layer. The reasoning agents speak a narrow
:class:`~paa.agents.reasoning.ModelLike` protocol (``complete_structured(prompt,
schema)``); the router speaks ``CompletionRequest`` plus routing context.
:class:`RouterModelAdapter` bridges the two and is the only place that coupling
lives.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from paa.agents.context_agents import PlannerContextBuilder, WorkerContextBuilder
from paa.agents.orchestrator import ChiefOrchestrator, TaskRequest
from paa.agents.policy import PolicyRiskAgent
from paa.agents.reasoning import CriticReviewer, StrategicPlanner, TaskRouter, WorkerCell
from paa.config import Settings, get_settings
from paa.core.types import ComplexityModality, CorrelationId, PermissionMode
from paa.ledger.recovery import RecoveryEngine
from paa.ledger.store import LedgerStore
from paa.models.base import redact
from paa.storage.relational import get_database
from paa.storage.relational.base import RelationalBackend

if TYPE_CHECKING:
    from paa.agents.base import Agent
    from paa.ledger.replay import TaskProjection
    from paa.models.router import EscalatingModelRouter

__all__ = ["CriticValidatorAdapter", "RouterModelAdapter", "Runtime"]

log = structlog.get_logger(__name__)

#: How long boot waits for the sandbox probe to answer.
#:
#: It has to be **longer** than the Docker backend's own probe timeout
#: (``_HEALTHCHECK_TIMEOUT_SECONDS``), so the inner probe is what gives out
#: first and it can kill the daemon child it started. A shorter outer bound
#: would cancel the inner wait instead, and a cancelled wait leaves that
#: child running — a leaked process per boot.
_SANDBOX_PROBE_BOUND_SECONDS = 30.0


class _ValidationVerdict:
    """The minimal shape the critic reads back: ``.passed`` and ``.findings``."""

    __slots__ = ("findings", "passed")

    def __init__(self, passed: bool, findings: list[dict[str, Any]]) -> None:
        self.passed = passed
        self.findings = findings


class CriticValidatorAdapter:
    """Adapts the deterministic validation engine to the critic's call shape.

    The critic calls ``validate(output_dict)`` and reads ``.passed`` /
    ``.findings``; the engine wants a :class:`ValidationArtifact`. This maps the
    worker's output dict onto an artifact (patch, source files, payload+schema)
    and flattens the report back down. It is the seam that lets the critic's
    LLM verdict sit *behind* deterministic checks — RFC §13's requirement that
    a model can never override a failing AST scan or schema check.
    """

    def __init__(self, engine: Any, *, workspace_root: Any = None) -> None:
        self._engine = engine
        self._workspace_root = workspace_root

    async def validate(self, output: dict[str, Any]) -> _ValidationVerdict:
        from paa.validation.engine import ValidationArtifact

        artifact = ValidationArtifact(
            source_files=dict(output.get("source_files", {})),
            patch=output.get("patch") or None,
            payload=output.get("payload"),
            payload_schema=output.get("payload_schema"),
            workspace_path=self._workspace_root,
        )
        report = await self._engine.validate(artifact)
        findings: list[dict[str, Any]] = []
        for check in getattr(report, "checks", []):
            if not check.passed:
                findings.append(check.to_payload())
        return _ValidationVerdict(passed=bool(report.passed), findings=findings)


class RouterModelAdapter:
    """Presents the agents' :class:`ModelLike` surface over the escalation router.

    The agents call ``complete_structured(prompt, schema, **kwargs)`` with a
    plain string prompt. The router needs a ``CompletionRequest`` and the
    routing context (modality, permission mode, correlation id) that decides
    *whether* to escalate. The agents forward that context as keyword arguments;
    this adapter unpacks it, builds the request, and calls the router.

    A provider that never escalates (Echo, or a lone local model) ignores the
    routing kwargs entirely, so the same adapter works with or without a
    frontier tier configured.
    """

    def __init__(
        self,
        router: EscalatingModelRouter,
        *,
        permission_mode: PermissionMode = PermissionMode.ASK,
    ) -> None:
        self._router = router
        self._default_mode = permission_mode

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        from paa.models.base import CompletionRequest, Message

        request = CompletionRequest(
            messages=(Message.user(prompt),),
            max_tokens=int(kwargs.get("max_tokens", 1024)),
        )
        return await self._router.complete_structured(
            request,
            schema,
            modality=kwargs.get("modality", ComplexityModality.STANDARD),
            permission_mode=kwargs.get("permission_mode", self._default_mode),
            correlation_id=kwargs.get("correlation_id"),
            reason=kwargs.get("reason"),
        )


@dataclass
class Runtime:
    """A fully wired, running cognitive runtime.

    Obtain one from :meth:`build`; it is an async context manager, so the usual
    shape is::

        async with await Runtime.build() as rt:
            cid = await rt.submit("summarise today's signals")
            outcome = await rt.run(cid)
    """

    settings: Settings
    db: RelationalBackend
    ledger: LedgerStore
    orchestrator: ChiefOrchestrator
    recovery: RecoveryEngine
    model_router: EscalatingModelRouter | None = None
    vector_store: Any = None
    graph_store: Any = None
    queue: Any = None
    agents: dict[str, Agent] = field(default_factory=dict)
    sandbox: Any = None
    """The containment backend the worker executes plan steps in.

    Exposed on the runtime rather than reached for through the worker's
    private ``_sandbox``: the sandbox is a *runtime* property, not an agent
    implementation detail, and the diagnostic surface (``paa doctor``) needs
    to ask it what containment it would actually deliver. The tool layer
    needs the same instance for the same reason.
    """
    memory_creator: Any = None
    """Memory Creator (RFC §2.1 agent 8): cold lake -> hot serving, in real time.

    Built in :meth:`build` with its extractor wired from
    ``settings.memory.extraction_model``:
    an LLM-backed extractor when the flag is non-empty (supplied by T-003's
    ``LLMFactExtractor``), else the rule-based default.
    """
    _boot_report: Any = None

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Runtime:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Release every resource, in reverse dependency order."""
        for closer, name in (
            (getattr(self.graph_store, "close", None), "graph"),
            (getattr(self.vector_store, "close", None), "vector"),
            (getattr(self.model_router, "aclose", None), "model_router"),
            (self.db.close, "db"),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # teardown must not mask the real error
                log.warning("runtime.close_error", component=name, error=str(exc))

    # -- driving tasks -----------------------------------------------------

    async def submit(
        self,
        goal: str,
        *,
        modality: ComplexityModality | None = None,
        required_slots: list[str] | None = None,
        workspace_path: str | None = None,
        target_agent: str | None = None,
    ) -> CorrelationId:
        """Record a task and return its lineage id. Does not execute it."""
        return await self.orchestrator.submit(
            TaskRequest(
                goal=goal,
                modality=modality,
                required_slots=required_slots,
                workspace_path=workspace_path,
                target_agent=target_agent,
            )
        )

    async def run(self, correlation_id: CorrelationId | uuid.UUID) -> Any:
        """Drive a submitted task to a terminal phase."""
        return await self.orchestrator.run(correlation_id)

    async def submit_and_run(self, goal: str, **kwargs: Any) -> Any:
        """Convenience: submit then run, the common one-shot path."""
        cid = await self.submit(goal, **kwargs)
        return await self.run(cid)

    async def project(self, correlation_id: CorrelationId | uuid.UUID) -> TaskProjection:
        """Current replayed state of a lineage."""
        from paa.ledger.replay import project

        return await project(self.ledger, correlation_id)

    async def boot_recovery(self) -> Any:
        """Run the post-crash sweep. Called by :meth:`build`; safe to re-run."""
        self._boot_report = await self.orchestrator.boot()
        return self._boot_report

    # -- construction ------------------------------------------------------

    @classmethod
    async def build(
        cls,
        settings: Settings | None = None,
        *,
        model_router: EscalatingModelRouter | None = None,
        model_adapter: Any = None,
        run_recovery: bool = True,
        enable_optional_backends: bool = True,
    ) -> Runtime:
        """Construct and start a runtime from configuration.

        ``model_router=None`` builds the configured provider (Ollama local,
        Anthropic escalation by default).

        ``model_adapter`` overrides the model seam directly with anything
        satisfying :class:`~paa.agents.reasoning.ModelLike`. This is the seam
        the agents actually depend on, so a test injects a *scripted* model
        here — one that returns real plans and verdicts — rather than an Echo
        router, whose minimal-schema-instance outputs cannot drive a plan. When
        given, the router is not built at all.

        ``enable_optional_backends=False`` skips vector/graph/queue entirely —
        used by tests that want the leanest possible wiring.
        """
        settings = settings or get_settings()
        settings.ensure_directories()

        # The substrate is *selected by configuration*, never by code: the
        # factory reads ``storage.backend_relational`` and builds the SQLite
        # or the PostgreSQL backend. Hard-coding the SQLite class here is what
        # made ``PAA_STORAGE__BACKEND_RELATIONAL=postgres`` a setting the
        # runtime silently ignored — the deployment advertised a Postgres
        # topology and used a local file, and the diagnostic that reported
        # the backend read the *setting* rather than the object, so it
        # agreed with the advertisement and not with the process.
        db = get_database(settings)
        await db.connect()
        ledger = LedgerStore(db)

        # The model seam. An explicit adapter wins; otherwise build the
        # configured router and wrap it. Building the router is skipped entirely
        # when an adapter is injected, so a test never touches a provider.
        if model_adapter is None:
            if model_router is None:
                from paa.models import get_model_router

                model_router = get_model_router(settings, ledger_store=ledger)
            model_adapter = RouterModelAdapter(
                model_router, permission_mode=settings.policy.mode
            )

        vector_store = graph_store = queue = None
        embedder = None
        if enable_optional_backends:
            vector_store, embedder = _try_vector(settings)
            graph_store = _try_graph(settings, db)
            queue = _try_queue(settings, db)

        # The containment backend is acquired here rather than inside
        # ``_build_agents`` so the runtime owns it: the diagnostic surface
        # (``paa doctor``) has to ask the sandbox what containment it would
        # actually deliver, and reaching into the worker's private attribute
        # to do that is how a diagnostic ends up lying.
        sandbox = await _acquire_sandbox(settings)

        agents = await _build_agents(
            settings=settings,
            db=db,
            model_adapter=model_adapter,
            vector_store=vector_store,
            graph_store=graph_store,
            embedder=embedder,
            sandbox=sandbox,
        )
        memory_creator = _build_memory_creator(settings, db, model_adapter)

        recovery = RecoveryEngine(
            ledger,
            db,
            requeuer=queue,
            max_attempts=settings.queue.max_delivery_attempts,
        )

        backpressure = None
        if queue is not None:
            from paa.storage.queue.backpressure import BackpressureController

            backpressure = BackpressureController(settings.queue)

        orchestrator = ChiefOrchestrator(
            ledger,
            context_builder=agents["context_builder_planner"],
            worker_context_builder=agents["context_builder_worker"],
            planner=agents["strategic_planner"],
            policy=agents["policy_risk"],
            worker=agents["worker"],
            critic=agents["critic"],
            router=agents.get("router"),
            queue=queue,
            backpressure=backpressure,
            recovery=recovery,
            permission_mode=settings.policy.mode,
        )

        runtime = cls(
            settings=settings,
            db=db,
            ledger=ledger,
            orchestrator=orchestrator,
            recovery=recovery,
            model_router=model_router,
            vector_store=vector_store,
            graph_store=graph_store,
            queue=queue,
            agents=agents,
            sandbox=sandbox,
            memory_creator=memory_creator,
        )

        if run_recovery:
            await runtime.boot_recovery()

        log.info(
            "runtime.built",
            vector=type(vector_store).__name__ if vector_store else None,
            graph=type(graph_store).__name__ if graph_store else None,
            queue=type(queue).__name__ if queue else None,
            mode=settings.policy.mode.value,
        )
        return runtime


# ---------------------------------------------------------------------------
# Optional-backend construction. Each returns a working object or None/fallback,
# never raises — a missing extra must degrade the runtime, not stop it booting.
# ---------------------------------------------------------------------------


def _try_vector(settings: Settings) -> tuple[Any, Any]:
    try:
        from paa.models import get_embedder
        from paa.storage.vector import get_vector_store

        store = get_vector_store(settings)
        embedder = get_embedder(settings.models)
        return store, embedder
    except Exception as exc:
        log.warning("runtime.vector_unavailable", error=str(exc))
        return None, None


def _try_graph(settings: Settings, db: RelationalBackend) -> Any:
    try:
        from paa.storage.graph import get_graph_store

        return get_graph_store(settings.storage, db)
    except Exception as exc:
        log.warning("runtime.graph_unavailable", error=str(exc))
        return None


def _try_queue(settings: Settings, db: RelationalBackend) -> Any:
    try:
        from paa.storage.queue import get_queue

        return get_queue(settings, db)
    except Exception as exc:
        log.warning("runtime.queue_unavailable", error=str(exc))
        return None


def _build_memory_creator(
    settings: Settings,
    db: RelationalBackend,
    model_adapter: Any,
) -> Any:
    """Build the Memory Creator with its extractor wired from configuration.

    Extraction is rule-based by default (``settings.memory.extraction_model``
    empty, unchanged behavior). When the flag is non-empty the LLM-backed
    extractor (``LLMFactExtractor``, supplied by T-003) is used; it is resolved
    lazily so a configured-but-not-yet-implemented extractor degrades to the
    rule-based default rather than crashing boot.
    """
    import paa.memory.creator as _creator_mod

    MemoryCreator = _creator_mod.MemoryCreator
    extractor: Any = None
    if settings.memory.extraction_model:
        try:
            extractor_cls = getattr(_creator_mod, "LLMFactExtractor", None)
            if extractor_cls is not None:
                extractor = extractor_cls(
                    model=model_adapter,
                    max_retries=settings.models.max_transport_retries,
                    timeout=settings.models.request_timeout_seconds,
                )
        except Exception as exc:  # degradation path: never crash boot on a model
            log.warning("runtime.llm_extractor_unavailable", error=redact(str(exc)))
    return MemoryCreator(db, extractor=extractor)


async def _acquire_sandbox(settings: Settings) -> Any:
    """Select a sandbox backend without letting a slow probe stall boot.

    ``get_sandbox("auto")`` probes docker then WSL; a cold WSL VM can take
    10-15s to answer its healthcheck. Boot must not wait for that
    indefinitely, so the probe is bounded — on timeout or any failure it
    degrades to the always-available subprocess backend. This mirrors the
    factory's own philosophy (auto degrades, never fails) while making the
    degradation *fast*.

    The bound is **longer than the backend's own probe timeout**, so the
    inner probe is what gives out first and it can clean up after itself. A
    shorter outer bound would cancel the inner wait instead, and a cancelled
    wait leaves the daemon's child process running.
    """
    import asyncio

    from paa.config import SandboxSettings
    from paa.sandbox import get_sandbox

    try:
        return await asyncio.wait_for(
            get_sandbox(settings.sandbox), timeout=_SANDBOX_PROBE_BOUND_SECONDS
        )
    except TimeoutError:
        # A bare TimeoutError carries no message, and this log line is the
        # one that announces a containment downgrade. It has to say *why*.
        log.warning(
            "runtime.sandbox_probe_fell_back_to_subprocess",
            error=(
                f"the sandbox probe did not answer within "
                f"{_SANDBOX_PROBE_BOUND_SECONDS}s"
            ),
        )
    except Exception as exc:
        log.warning("runtime.sandbox_probe_fell_back_to_subprocess", error=str(exc))
    try:
        return await get_sandbox(SandboxSettings(backend="subprocess"))
    except Exception as inner:
        log.warning("runtime.sandbox_unavailable", error=str(inner))
        return None


async def _build_agents(
    *,
    settings: Settings,
    db: RelationalBackend,
    model_adapter: Any,
    vector_store: Any,
    graph_store: Any,
    embedder: Any,
    sandbox: Any = None,
) -> dict[str, Agent]:
    """Construct the core agent hierarchy, each with its dependencies."""
    anti_goals = _load_anti_goals(settings)

    planner_ctx = PlannerContextBuilder(
        db=db, vector_store=vector_store, graph_store=graph_store, embedder=embedder
    )
    worker_ctx = WorkerContextBuilder(
        db=db, vector_store=vector_store, graph_store=graph_store, embedder=embedder
    )
    planner = StrategicPlanner(model=model_adapter)
    policy = PolicyRiskAgent(
        db=db,
        vector_store=vector_store,
        embedder=embedder,
        settings=settings.policy,
        workspace_root=settings.storage.workspace_root,
        anti_goals=anti_goals,
    )
    # The critic runs deterministic validation first, model second. The
    # engine's verdict is authoritative and can only downgrade (RFC §13).
    validator = None
    try:
        from paa.validation.engine import DeterministicValidationEngine

        validator = CriticValidatorAdapter(
            DeterministicValidationEngine(),
            workspace_root=settings.storage.workspace_root,
        )
    except Exception as exc:
        log.warning("runtime.validator_unavailable", error=str(exc))
    critic = CriticReviewer(model=model_adapter, validation_engine=validator)
    router = TaskRouter(model=model_adapter, min_agents=3)

    worker = WorkerCell(sandbox=sandbox)
    return {
        "context_builder_planner": planner_ctx,
        "context_builder_worker": worker_ctx,
        "strategic_planner": planner,
        "policy_risk": policy,
        "critic": critic,
        "router": router,
        "worker": worker,
    }


def _load_anti_goals(settings: Settings) -> list[str]:
    """Read anti-goal lines from the vault, if the file exists.

    Missing file is not an error — a fresh install has no anti-goals yet, and
    the policy agent simply has nothing extra to refuse.
    """
    path = settings.storage.vault_path / "anti_goals.md"
    if not path.exists():
        return []
    lines = [
        line.strip().lstrip("-* ").strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return [line for line in lines if line]
