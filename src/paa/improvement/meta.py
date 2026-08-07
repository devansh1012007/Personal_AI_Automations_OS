"""Prototypical few-shot task classification — the honest meta-learning piece.

You asked about MAML, prototypical/matching networks, memory-augmented nets, and
neuro-symbolic approaches. This module implements the one that genuinely fits a
runtime like this, and the module docstring is the place to be honest about why
the others are not here.

**Why not gradient-based MAML.** MAML learns an initialisation that adapts in a
few gradient steps. It needs a differentiable model whose weights you control, a
labelled task *distribution* split into support/query sets, and a training loop
with second-order gradients. This runtime does not own model weights (it calls
provider APIs), has no labelled task distribution, and has no training
infrastructure. Shipping a file called ``maml.py`` that did not do MAML would be
worse than not shipping it (ADR-0017).

**Why prototypical networks fit.** The metric/similarity family does not need
training at all when you already have an embedder. You represent each task class
by the *mean embedding* of its past exemplars (its "prototype"), and classify a
new task by nearest prototype in cosine space. That delivers exactly the property
that was actually wanted — *adapt from a few examples, immediately* — with no
gradient step, no labelled query set, and no weights to own. New exemplars just
shift the prototype. It degrades gracefully: when no class is close enough, it
returns ``unknown`` and a confidence margin, so the router can fall back to
asking rather than guess wrong.

Memory-augmented and neuro-symbolic ideas are already present elsewhere in the
runtime in their pragmatic forms — the fact/graph stores are the external memory,
and the deterministic policy/validation layers are the symbolic half of a
neuro-symbolic whole. This module adds the missing metric-learning capability.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import structlog

__all__ = ["Classification", "Prototype", "PrototypicalClassifier"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class Prototype:
    """A task class represented by the mean of its exemplar embeddings."""

    label: str
    vector: np.ndarray
    exemplar_count: int = 0

    def updated_with(self, embedding: np.ndarray) -> Prototype:
        """Return a new prototype folding in one more exemplar (running mean).

        Incremental so a class adapts as new examples arrive without recomputing
        from the whole history — the "learn from few, keep learning" property.
        """
        n = self.exemplar_count
        new_vec = (self.vector * n + embedding) / (n + 1)
        return Prototype(self.label, _normalise(new_vec), n + 1)


@dataclass(slots=True)
class Classification:
    """The result of classifying a new task."""

    label: str
    confidence: float
    margin: float
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def is_confident(self) -> bool:
        return self.label != "unknown"


class PrototypicalClassifier:
    """Nearest-prototype few-shot classifier over unit embeddings.

    No training loop. ``fit`` builds prototypes from labelled exemplars;
    ``add_exemplar`` folds in one more at any time; ``classify`` returns the
    nearest prototype with a confidence and a margin, or ``unknown`` when
    nothing is close enough or the top two are too similar to call.
    """

    def __init__(
        self,
        *,
        min_confidence: float = 0.55,
        min_margin: float = 0.05,
    ) -> None:
        self._prototypes: dict[str, Prototype] = {}
        self._min_confidence = min_confidence
        self._min_margin = min_margin

    def fit(self, exemplars: Sequence[tuple[str, np.ndarray]]) -> None:
        """Build prototypes from ``(label, embedding)`` pairs.

        Each class's prototype is the L2-normalised mean of its exemplar
        embeddings — the definition of a prototypical-network prototype.
        """
        grouped: dict[str, list[np.ndarray]] = {}
        for label, emb in exemplars:
            grouped.setdefault(label, []).append(_normalise(np.asarray(emb, dtype=np.float32)))
        self._prototypes = {
            label: Prototype(label, _normalise(np.mean(vecs, axis=0)), len(vecs))
            for label, vecs in grouped.items()
        }
        log.debug("meta.fitted", classes=len(self._prototypes))

    def add_exemplar(self, label: str, embedding: np.ndarray) -> None:
        """Fold one new exemplar into its class prototype (or create the class)."""
        emb = _normalise(np.asarray(embedding, dtype=np.float32))
        existing = self._prototypes.get(label)
        if existing is None:
            self._prototypes[label] = Prototype(label, emb, 1)
        else:
            self._prototypes[label] = existing.updated_with(emb)

    def classify(self, embedding: np.ndarray) -> Classification:
        """Nearest prototype by cosine similarity, with an abstain option.

        Returns ``unknown`` when the best similarity is below ``min_confidence``
        or the gap to the runner-up is below ``min_margin`` — abstaining instead
        of guessing is the whole point, so a thinly-supported call falls back to
        asking rather than mislabelling.
        """
        if not self._prototypes:
            return Classification("unknown", 0.0, 0.0)

        query = _normalise(np.asarray(embedding, dtype=np.float32))
        scores = {
            label: float(query @ proto.vector) for label, proto in self._prototypes.items()
        }
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_label, best_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else -1.0
        margin = best_score - runner_up

        if best_score < self._min_confidence or (len(ranked) > 1 and margin < self._min_margin):
            return Classification("unknown", round(best_score, 4), round(margin, 4), scores)
        return Classification(best_label, round(best_score, 4), round(margin, 4), scores)

    @property
    def labels(self) -> list[str]:
        return sorted(self._prototypes)


def _normalise(vector: np.ndarray) -> np.ndarray:
    """L2-normalise so the dot product is cosine similarity."""
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return vector.astype(np.float32)
    return (vector / norm).astype(np.float32)
