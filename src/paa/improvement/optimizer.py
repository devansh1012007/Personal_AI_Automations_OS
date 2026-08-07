"""Tool-weight optimisation — the runtime tuning its own routing over time.

RFC §3.3. Each skill carries a ``reliability_weight`` that ranks it against
alternatives; this module updates that weight from observed outcomes so the
system prefers what has actually worked.

SPEC DEVIATION (docs/adr/0020): the RFC's ``optimize_tool_ranking_weights`` is
labelled "gradient-based weight optimization", but the body is a plain
exponential moving average — there is no gradient. We keep the EWMA (it is the
right tool: online, bounded, no training loop) and correct the description. The
per-run performance score keeps the RFC's weighting (success 0.5, correction
0.3, latency 0.2) but is documented honestly and clamped to ``[0.01, 1.0]`` so a
weight can never reach zero (which would make a skill permanently unrankable and
therefore unrecoverable) or exceed one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from paa.storage.relational.database import Database

__all__ = ["RunMetrics", "WeightOptimizer", "performance_score", "update_weight"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class RunMetrics:
    """One execution's outcome, distilled to what the optimiser needs."""

    skill_name: str
    succeeded: bool
    user_corrected: bool
    latency_seconds: float
    current_weight: float = 1.0


def performance_score(
    *,
    succeeded: bool,
    user_corrected: bool,
    latency_seconds: float,
    latency_ceiling: float = 30.0,
) -> float:
    """Score one run in ``[0, 1]``. RFC §3.3 component weighting.

    ``success`` contributes 0.5, *not being corrected* 0.3, and *being fast*
    0.2. A clean, fast, uncorrected run scores 1.0; a failed, slow, corrected
    one scores 0.0. Latency is normalised against a ceiling and clamped, so a
    pathologically slow run cannot drive the score negative.
    """
    success_term = 0.5 if succeeded else 0.0
    correction_term = 0.3 * (0.0 if user_corrected else 1.0)
    latency_factor = min(1.0, max(0.0, latency_seconds) / max(latency_ceiling, 1e-9))
    latency_term = 0.2 * (1.0 - latency_factor)
    return round(success_term + correction_term + latency_term, 6)


def update_weight(current: float, score: float, *, alpha: float = 0.1) -> float:
    """One EWMA step: ``w = w + alpha*(score - w)``, clamped to ``[0.01, 1.0]``.

    The floor of 0.01 is deliberate: a skill that hits exactly 0 would sort
    below everything forever and never be tried again, so a single bad streak
    would permanently retire it. A small positive floor lets it recover if it
    later performs.
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must be in [0,1], got {alpha}")
    updated = current + alpha * (score - current)
    return round(min(1.0, max(0.01, updated)), 3)


def optimize_tool_ranking_weights(
    runs: Sequence[RunMetrics], *, alpha: float = 0.1
) -> dict[str, float]:
    """Fold a batch of runs into updated per-skill weights.

    Runs are applied in order, so the most recent outcome has the most
    influence — the point of an EWMA. Returns the final weight per skill.
    """
    weights: dict[str, float] = {}
    for run in runs:
        base = weights.get(run.skill_name, run.current_weight)
        score = performance_score(
            succeeded=run.succeeded,
            user_corrected=run.user_corrected,
            latency_seconds=run.latency_seconds,
        )
        weights[run.skill_name] = update_weight(base, score, alpha=alpha)
    return weights


class WeightOptimizer:
    """Persists optimised weights to ``improvement_skill_weights`` and the registry."""

    def __init__(self, db: Database, *, alpha: float = 0.1) -> None:
        self._db = db
        self._alpha = alpha

    async def optimize_from_history(self, *, since_days: int = 7) -> dict[str, float]:
        """Read recent execution runs, recompute weights, and persist them.

        Converges toward each skill's real reliability without a training loop —
        every completed run nudges its weight, so the ranking self-tunes as the
        system is used.
        """
        rows = await self._db.fetch_all(
            "SELECT skill_name, exit_code, duration_ms, escalated, telemetry "
            "FROM hot_serving_execution_runs "
            "WHERE skill_name IS NOT NULL "
            "  AND started_at >= datetime('now', ?) "
            "ORDER BY started_at ASC",
            (f"-{int(since_days)} days",),
        )
        current = await self._current_weights()
        runs: list[RunMetrics] = []
        for row in rows:
            telemetry = _loads(row["telemetry"])
            runs.append(
                RunMetrics(
                    skill_name=row["skill_name"],
                    succeeded=(row["exit_code"] == 0),
                    user_corrected=bool(telemetry.get("user_corrected", False)),
                    latency_seconds=(row["duration_ms"] or 0) / 1000.0,
                    current_weight=current.get(row["skill_name"], 1.0),
                )
            )

        weights = optimize_tool_ranking_weights(runs, alpha=self._alpha)
        await self._persist(weights, runs)
        log.info("optimizer.weights_updated", skills=len(weights))
        return weights

    async def _current_weights(self) -> dict[str, float]:
        rows = await self._db.fetch_all(
            "SELECT skill_name, reliability FROM improvement_skill_weights"
        )
        return {r["skill_name"]: r["reliability"] for r in rows}

    async def _persist(self, weights: dict[str, float], runs: Sequence[RunMetrics]) -> None:
        from paa.storage.relational.database import to_iso, utc_now

        counts: dict[str, int] = {}
        for run in runs:
            counts[run.skill_name] = counts.get(run.skill_name, 0) + 1

        now = to_iso(utc_now())
        for skill, weight in weights.items():
            await self._db.execute(
                "INSERT INTO improvement_skill_weights "
                "(skill_name, reliability, sample_count, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(skill_name) DO UPDATE SET "
                "  reliability = excluded.reliability, "
                "  sample_count = improvement_skill_weights.sample_count + excluded.sample_count, "
                "  updated_at = excluded.updated_at",
                (skill, weight, counts.get(skill, 0), now),
            )
            # Mirror into the live registry so retrieval ranking sees it.
            await self._db.execute(
                "UPDATE hot_serving_skill_registry SET reliability_weight = ? "
                "WHERE skill_name = ?",
                (weight, skill),
            )


def _loads(value: Any) -> dict[str, Any]:
    from paa.storage.relational.database import loads

    return loads(value, {}) or {}
