"""The chat service: the host callers talk to (TUI, MCP, CLI — later waves).

C4.1's caller-facing half. :class:`ChatService` owns the session lifecycle
(create, resume, list, end), the per-turn drive
(:meth:`ChatService.send` — streaming-capable through an optional delta
callback), and the wiring that turns a :class:`~paa.runtime.Runtime` into a
chat host. The loop (:mod:`paa.chat.loop`) owns the flow; the facade it
runs on is minted here, per turn, because *a facade is a grant and the host
is what grants* (ADR-0051) — the service is the chat grant's source.

The service imports nothing optional and talks to no provider directly: the
model seam resolves from ``ChatSettings.model`` (``echo`` by default, so the
whole product demos offline), and the cold-lake signal repository degrades
to ``None`` — losing episodic recall, never the turn — when the blob store
is unavailable, exactly the degrade-never-raise posture every factory in
``runtime.py`` holds.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from paa.chat.loop import (
    ChatLoop,
    ChatModel,
    ChatTurnFacade,
    ContextProvider,
    build_chat_model,
)
from paa.chat.session import (
    ChatSessionJournal,
    ChatSessionRecord,
    SessionNotFoundError,
    list_sessions,
    session_record,
)
from paa.config import ChatSettings, ContextSettings
from paa.core.errors import PaaError
from paa.core.types import PermissionMode
from paa.ledger.store import LedgerStore
from paa.memory.creator import MemoryCreator
from paa.memory.facts import FactRepository
from paa.models.base import redact
from paa.pipelines.base import PipelineResult, PipelineStatus, PipelineTask
from paa.runtime import Runtime
from paa.storage.coldlake.signals import SignalRepository
from paa.storage.relational.base import RelationalBackend

__all__ = ["ChatService", "DeltaSink", "TurnResult"]

log = structlog.get_logger(__name__)

#: The streaming surface: an async callback per reply delta. ``None`` means
#: "collect nothing, just give me the final reply" — the ledger records the
#: final reply either way.
DeltaSink = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TurnResult:
    """The caller-facing outcome of one turn."""

    turn_id: str
    status: str
    """``PipelineStatus`` value: ``completed`` or ``failed``. A cancelled
    turn raises :class:`asyncio.CancelledError` instead of returning — the
    caller that cancelled already has the deltas it streamed."""

    reply: str
    error: str | None
    recalled_fact_ids: tuple[str, ...]
    signal_ids: tuple[str, ...]
    tokens_consumed: int
    bundle_tokens: int
    token_ceiling: int


class ChatService:
    """Sessions in, replies out. Everything else is the ledger's problem."""

    def __init__(
        self,
        *,
        ledger: LedgerStore,
        db: RelationalBackend,
        creator: MemoryCreator,
        settings: ChatSettings,
        permission_mode: PermissionMode = PermissionMode.ASK,
        context_settings: ContextSettings | None = None,
        model: ChatModel | None = None,
        signals: SignalRepository | None = None,
        context_provider: ContextProvider | None = None,
    ) -> None:
        self._ledger = ledger
        self._db = db
        self._creator = creator
        # A second FactRepository instance over the same backend: the class
        # is a stateless door to the hot tables (the creator holds its own
        # instance for its writes), and the recall path needs the same door
        # without reaching through the creator's private attribute.
        self._facts = FactRepository(db)
        self._settings = settings
        self._permission_mode = permission_mode
        self._context_settings = context_settings
        self._model = model if model is not None else build_chat_model(settings)
        self._signals = signals
        # The context-provider slot (C6.4 / P1.5): ``None`` means the
        # shipped compiler — the facade resolves the default itself, so
        # this stays "the host may resolve an overlay's ref and hand the
        # result here", nothing more.
        self._context_provider = context_provider

    @classmethod
    def from_runtime(
        cls, runtime: Runtime, *, settings: ChatSettings | None = None
    ) -> ChatService:
        """Wire a chat host off a built runtime.

        The cold-lake signal repository is built the way the ingestion API
        builds its own (``paa.api.app._signal_repo``): through the blob-store
        *factory*, so a deployment configured for S3 does not quietly keep
        chat payloads on local disk — and, like every factory in this tree,
        it degrades to ``None`` rather than failing boot.
        """
        signals: SignalRepository | None = None
        try:
            from paa.storage.coldlake import get_blob_store

            signals = SignalRepository(runtime.db, get_blob_store(runtime.settings.storage))
        except Exception as exc:
            log.warning("chat.signal_repo_unavailable", error=redact(str(exc)))

        creator = runtime.memory_creator
        if creator is None:
            raise PaaError(
                "chat requires a MemoryCreator; build the runtime with one",
            )
        return cls(
            ledger=runtime.ledger,
            db=runtime.db,
            creator=creator,
            settings=settings or runtime.settings.chat,
            permission_mode=runtime.settings.policy.mode,
            context_settings=runtime.settings.context,
            signals=signals,
        )

    # -- sessions -------------------------------------------------------------

    async def create_session(
        self, *, scope: str = "personal", mandate_ref: str | None = None
    ) -> uuid.UUID:
        """Start a session: one lineage, one ``CHAT_SESSION_STARTED`` row."""
        session_id = uuid.uuid4()
        journal = ChatSessionJournal(self._ledger, session_id)
        await journal.session_started(
            scope=scope,
            mandate_ref=mandate_ref,
            model=self._settings.model,
            cross_session_episodic=self._settings.cross_session_episodic,
        )
        log.info(
            "chat.session_started",
            session_id=str(session_id),
            scope=scope,
            model=self._settings.model,
        )
        return session_id

    async def resume_session(self, session_id: uuid.UUID) -> ChatSessionRecord:
        """Fold a session's lineage and hand back its record.

        Resume is the substrate's native trick: the lineage is the truth, so
        resuming is re-reading it. A session id that names no started
        lineage raises :class:`SessionNotFoundError` — a resumed session that
        never existed must fail loudly, not silently start over.
        """
        record = await session_record(self._ledger, session_id)
        if record is None:
            raise SessionNotFoundError(
                f"no chat session lineage for {session_id}",
                session_id=str(session_id),
            )
        return record

    async def list_sessions(self, *, limit: int = 100) -> list[ChatSessionRecord]:
        """Every session, newest first — folded from the ledger, never indexed."""
        return await list_sessions(self._ledger, limit=limit)

    async def end_session(
        self, session_id: uuid.UUID, *, stopped_reason: str = "user"
    ) -> ChatSessionRecord:
        """Close a session with the fold's own counters on the ``ENDED`` row."""
        record = await self.resume_session(session_id)
        journal = ChatSessionJournal(self._ledger, session_id)
        await journal.session_ended(
            stopped_reason=stopped_reason,
            turns_proposed=record.turns_proposed,
            turns_completed=record.turns_completed,
            turns_failed=record.turns_failed,
            turns_cancelled=record.turns_cancelled,
            tokens_consumed=record.tokens_consumed,
        )
        return await self.resume_session(session_id)

    # -- turns ----------------------------------------------------------------

    async def send(
        self,
        session_id: uuid.UUID,
        text: str,
        *,
        on_delta: DeltaSink | None = None,
    ) -> TurnResult:
        """Send one user message and drive one turn to a terminal outcome.

        ``on_delta`` receives reply chunks as they stream (echo chunks the
        finished reply; a provider with a native stream would feed real
        tokens). Cancelling the caller's task mid-stream records
        ``CHAT_TURN_CANCELLED`` and re-raises :class:`asyncio.CancelledError`
        — the caller keeps the deltas it already collected.
        """
        if not text.strip():
            raise PaaError("chat message must not be empty", session_id=str(session_id))
        await self.resume_session(session_id)  # a send to an unknown session fails here

        turn_id = str(uuid.uuid4())
        journal = ChatSessionJournal(self._ledger, session_id)
        facade = ChatTurnFacade(
            session_id=session_id,
            turn_id=turn_id,
            journal=journal,
            ledger=self._ledger,
            model=self._model,
            creator=self._creator,
            facts=self._facts,
            db=self._db,
            settings=self._settings,
            permission_mode=self._permission_mode,
            signals=self._signals,
            context_settings=self._context_settings,
            context_provider=self._context_provider,
        )
        task = PipelineTask(
            correlation_id=session_id,
            session_id=session_id,
            goal=text,
            params={"turn_id": turn_id, "on_delta": on_delta},
        )
        result = await ChatLoop(journal).run(task, facade)
        return self._turn_result(turn_id, result)

    # -- introspection ----------------------------------------------------------

    @property
    def settings(self) -> ChatSettings:
        return self._settings

    async def session_record(self, session_id: uuid.UUID) -> ChatSessionRecord | None:
        return await session_record(self._ledger, session_id)

    def _turn_result(self, turn_id: str, result: PipelineResult) -> TurnResult:
        metrics = result.metrics
        return TurnResult(
            turn_id=turn_id,
            status=result.status.value,
            reply=result.summary,
            error=result.summary if result.status is PipelineStatus.FAILED else None,
            recalled_fact_ids=tuple(metrics.get("recalled_fact_ids", ())),
            signal_ids=tuple(metrics.get("signal_ids", ())),
            tokens_consumed=int(metrics.get("tokens_consumed", 0)),
            bundle_tokens=int(metrics.get("bundle_tokens", 0)),
            token_ceiling=int(metrics.get("token_ceiling", 0)),
        )
