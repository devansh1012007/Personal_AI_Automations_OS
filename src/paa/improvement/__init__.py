"""Self-improvement — the runtime tuning its own behaviour from its own history.

RFC §3 (the "Hermes-class" system), plus the metric-learning member of the
meta-learning family you asked about.

``optimizer``
    EWMA tool-weight optimisation so routing prefers what has worked.
``reflection``
    Weekly friction analysis that distils anti-patterns into the playbook.
``distillation``
    Turns a clean, non-trivial task into a reusable, sandbox-verified recipe.
``meta``
    Prototypical few-shot task classification (ADR-0017) — adapt from a few
    examples with no gradient training. The honest member of the MAML /
    prototypical / memory-augmented / neuro-symbolic set for this runtime.
"""

from __future__ import annotations

from paa.improvement.distillation import Recipe, SkillDistiller, generalize_arguments
from paa.improvement.meta import Classification, Prototype, PrototypicalClassifier
from paa.improvement.optimizer import (
    RunMetrics,
    WeightOptimizer,
    optimize_tool_ranking_weights,
    performance_score,
    update_weight,
)
from paa.improvement.reflection import (
    DomainFriction,
    ReflectionEngine,
    ReflectionReport,
    operational_friction,
)

__all__ = [
    "Classification",
    "DomainFriction",
    "Prototype",
    "PrototypicalClassifier",
    "Recipe",
    "ReflectionEngine",
    "ReflectionReport",
    "RunMetrics",
    "SkillDistiller",
    "WeightOptimizer",
    "generalize_arguments",
    "operational_friction",
    "optimize_tool_ranking_weights",
    "performance_score",
    "update_weight",
]
