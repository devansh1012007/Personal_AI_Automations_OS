"""Composition for the chat surfaces — one wiring, two front-ends.

:class:`ChatWiring` is everything a chat surface needs, held as one immutable
value: a :class:`~paa.chat.service.ChatService` built on the runtime's
substrate with the model seam already wrapped in
:class:`~paa.tui.chat.resilience.ResilientChatModel` (retry + breaker live
*inside* the seam, so a retry never writes a second ledger row). Both
front-ends — the Textual app (:mod:`paa.tui.chat.app`) and the plain loop
(:mod:`paa.tui.chat.plain`) — drive the same wiring, so a turn's ledger
footprint cannot tell which surface sent it. The service is constructed here,
with the resilience wrapper already on the seam, so callers can never wire a
chat service without the breaker.

The read-only substrate views (:meth:`ChatWiring.memory_snapshot`, and the
ledger/db fields) are what the dashboards read; writes go through the service
only, on either surface.

This module imports no Textual: ``paa chat --plain`` runs with the ``[tui]``
extra not installed (see the package docstring for the rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from paa.chat.loop import build_chat_model
from paa.chat.service import ChatService
from paa.config import ChatSettings
from paa.core.errors import PaaError
from paa.core.types import PermissionMode
from paa.ledger.store import LedgerStore
from paa.memory.contradiction import ContradictionDetector
from paa.memory.creator import MemoryCreator
from paa.memory.facts import FactRepository
from paa.storage.relational.base import RelationalBackend
from paa.storage.relational.database import to_iso
from paa.tui.chat.resilience import (
    DEFAULT_ATTEMPTS,
    DEFAULT_BACKOFF,
    ResilientChatModel,
)

if TYPE_CHECKING:
    from paa.memory.facts import Fact
    from paa.runtime import Runtime
    from paa.storage.coldlake.signals import SignalRepository

__all__ = ["ChatWiring"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChatWiring:
    """Everything the chat surface needs, held as one immutable wiring.

    Built by :meth:`from_runtime` (the CLI path) or :meth:`from_parts` (tests,
    and the app's own model switches).
    """

    service: ChatService
    ledger: LedgerStore
    db: RelationalBackend
    settings: ChatSettings
    creator: MemoryCreator
    signals: SignalRepository | None
    home: Path | None
    permission_mode: PermissionMode
    resilient: ResilientChatModel

    @classmethod
    def from_runtime(
        cls,
        runtime: Runtime,
        *,
        settings: ChatSettings | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: tuple[float, ...] = DEFAULT_BACKOFF,
    ) -> ChatWiring:
        """Wire the chat surface off a built runtime (the CLI path).

        The cold-lake signal repository is built the way
        :meth:`paa.chat.service.ChatService.from_runtime` builds its own —
        through the blob-store factory, degrading to ``None`` — so the UI and
        the service agree on whether episodic recall exists.
        """
        signals = _build_signals(runtime)
        resolved = settings if settings is not None else runtime.settings.chat
        creator = runtime.memory_creator
        if creator is None:
            raise PaaError("chat requires a MemoryCreator; build the runtime with one")
        resilient = ResilientChatModel(
            build_chat_model(resolved), attempts=attempts, backoff=backoff
        )
        service = ChatService(
            ledger=runtime.ledger,
            db=runtime.db,
            creator=creator,
            settings=resolved,
            permission_mode=runtime.settings.policy.mode,
            context_settings=runtime.settings.context,
            model=resilient,
            signals=signals,
        )
        return cls(
            service=service,
            ledger=runtime.ledger,
            db=runtime.db,
            settings=resolved,
            creator=creator,
            signals=signals,
            home=runtime.settings.home,
            permission_mode=runtime.settings.policy.mode,
            resilient=resilient,
        )

    @classmethod
    def from_parts(
        cls,
        *,
        ledger: LedgerStore,
        db: RelationalBackend,
        creator: MemoryCreator,
        settings: ChatSettings,
        signals: SignalRepository | None = None,
        home: Path | None = None,
        permission_mode: PermissionMode = PermissionMode.ASK,
        model: Any | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: tuple[float, ...] = DEFAULT_BACKOFF,
    ) -> ChatWiring:
        """Wire from explicit pieces (tests, or a hand-assembled host)."""
        inner = model if model is not None else build_chat_model(settings)
        resilient = ResilientChatModel(inner, attempts=attempts, backoff=backoff)
        service = ChatService(
            ledger=ledger,
            db=db,
            creator=creator,
            settings=settings,
            permission_mode=permission_mode,
            model=resilient,
            signals=signals,
        )
        return cls(
            service=service,
            ledger=ledger,
            db=db,
            settings=settings,
            creator=creator,
            signals=signals,
            home=home,
            permission_mode=permission_mode,
            resilient=resilient,
        )

    def with_model(self, name: str) -> ChatWiring:
        """The same substrate, a new model seam (``/model``)."""
        resolved = self.settings.model_copy(update={"model": name})
        resilient = ResilientChatModel(
            build_chat_model(resolved),
            attempts=self.resilient.attempts,
            backoff=self.resilient.backoff,
        )
        service = ChatService(
            ledger=self.ledger,
            db=self.db,
            creator=self.creator,
            settings=resolved,
            permission_mode=self.permission_mode,
            model=resilient,
            signals=self.signals,
        )
        return ChatWiring(
            service=service,
            ledger=self.ledger,
            db=self.db,
            settings=resolved,
            creator=self.creator,
            signals=self.signals,
            home=self.home,
            permission_mode=self.permission_mode,
            resilient=resilient,
        )

    # -- read-only substrate views --------------------------------------------

    async def memory_snapshot(self) -> tuple[str, str, str, list[Fact]]:
        """KPI strip, funnel, curator line, and the fact list — real numbers only.

        Both memory dashboards render from this one query set: the TUI screen
        and the plain ``/memory`` print the same numbers, computed the same
        way, so the two surfaces cannot disagree about what memory holds.
        """
        repo = FactRepository(self.db)
        total = await repo.count_facts()
        tier_rows = await self.db.fetch_all(
            "SELECT tier, COUNT(*) AS n FROM hot_serving_active_facts "
            "WHERE superseded_by IS NULL GROUP BY tier"
        )
        tiers = {str(row["tier"]): int(row["n"]) for row in tier_rows}
        hot = tiers.get("T0", 0) + tiers.get("T1", 0)
        cold = total - hot
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        signals_today = int(
            await self.db.fetch_value(
                "SELECT COUNT(*) FROM cold_lake_signals WHERE received_at >= ?",
                (to_iso(midnight),),
            )
            or 0
        )
        open_conflicts = len(await ContradictionDetector(self.db).open_conflicts())
        tier_text = " ".join(f"{tier}:{count}" for tier, count in sorted(tiers.items()))
        kpis = (
            f"facts {total} · hot {hot} / cold {cold} (tiers: {tier_text or '—'}) · "
            f"signals today {signals_today} · quarantine {open_conflicts}"
        )
        funnel = (
            f"funnel — signals today {signals_today} → facts {total} → hot {hot} · "
            f"quarantine (open contradictions) {open_conflicts}"
        )
        dry_run = True
        try:
            from paa.config import get_settings

            dry_run = get_settings().tier.dry_run
        except Exception as exc:
            # Degrade-never-raise: the curator line still renders, it just
            # reports the default rather than the deployment's own flag.
            log.warning("chat_tui.tier_settings_unavailable", error=str(exc))
        curator = (
            "curator: not running inside chat (decay/consolidation are the nightly "
            f"curator's phases) · tier sweeper dry_run={dry_run}"
        )
        facts = await repo.query_facts(limit=200)
        return kpis, funnel, curator, facts


def _build_signals(runtime: Runtime) -> SignalRepository | None:
    """The cold-lake signal repo, degrade-never-raise (the service's own rule)."""
    try:
        from paa.storage.coldlake import get_blob_store
        from paa.storage.coldlake.signals import SignalRepository

        return SignalRepository(runtime.db, get_blob_store(runtime.settings.storage))
    except Exception as exc:
        log.warning("chat_tui.signal_repo_unavailable", error=str(exc))
        return None
