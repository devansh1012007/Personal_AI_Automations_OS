"""Post-recall retention touch — the single home for advancing recall clocks.

PLAN:integration-fix:P004

Every recall surface (API routes, coder tools, context builders, CLI
commands) advances ``use_count`` / ``last_queried_at`` for the facts it just
returned, and each used to carry its own copy of that helper. The copies
drifted in annotation, import binding and docstring wording while the
executable core stayed identical; this module is the one implementation
they all now call.

:func:`touch_recalled` is exported from :mod:`paa.memory` (see its
``__all__``) because the callers are outside this package — leaving it
internal would force deep imports at four sites and invite a fifth copy.
"""

from __future__ import annotations

import structlog

from paa.memory.decay import DecaySweeper
from paa.models.base import redact
from paa.storage.relational.base import RelationalBackend

__all__ = ["touch_recalled"]

log = structlog.get_logger(__name__)


async def touch_recalled(db: RelationalBackend | None, fact_ids: list[str]) -> None:
    """Advance retention clocks for the facts one recall just returned.

    ``DecaySweeper.touch_many`` is the only write a recall performs, and it
    writes only derived retention state (``use_count``/``last_queried_at``):
    response payloads are unchanged. This is the documented derived-state
    exception — retention clocks are not derivable from cold-lake replay (a
    rebuild resets them), the same way the decay sweep itself mutates hot
    serving state outside the ledger.
    """
    if db is None or not fact_ids:
        return
    ids = list(dict.fromkeys(fact_ids))
    try:
        await DecaySweeper(db).touch_many(ids)
    except Exception as exc:
        log.warning("memory.recall_touch_failed", count=len(ids), error=redact(str(exc)))
        return
    log.debug("memory.recall_touched", count=len(ids))
