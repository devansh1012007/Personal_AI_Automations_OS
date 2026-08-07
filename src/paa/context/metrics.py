"""Bounded-context metrics — RFC §5.1 and the §15 invariant equations.

Every public callable here is a **pure function of its arguments**: no I/O, no
clock reads, no globals, no hidden state. That is deliberate. These equations
decide whether a task proceeds to the planner, triggers background hydration,
or hard-stops to the user, and a routing decision that cannot be recomputed
from a ledger row months later is a decision that cannot be audited.

Two of the RFC's published equations are mathematically unsound as written and
are corrected here. Both corrections preserve the *monotonicity* the RFC
intended — the direction each variable pushes the score — while making the
result finite and bounded:

* :func:`attention_allocation_score` — SPEC DEVIATION (docs/adr/0012)
* :func:`hybrid_retrieval_score` — SPEC DEVIATION (docs/adr/0013)

Domain validation is aggressive on purpose. Every function raises
:class:`ValueError` on an out-of-domain input rather than returning ``nan``,
``inf``, or a silently clamped value. A metric that lies is strictly worse than
a metric that stops the task: a NaN utility score propagates into a routing
comparison, every comparison against NaN is ``False``, and the gatherer would
silently choose the ``else`` branch. Failing loudly at the boundary keeps that
class of bug out of the runtime entirely.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Protocol

import structlog

__all__ = [
    "ScoredFact",
    "attention_allocation_score",
    "context_density",
    "context_entropy",
    "context_pollution_ratio",
    "context_utility_score",
    "hybrid_retrieval_score",
    "memory_importance_index",
    "narrative_coherence_score",
    "planning_cost",
    "provider_reliability",
    "skill_rank",
    "token_efficiency",
]

log = structlog.get_logger(__name__)


class ScoredFact(Protocol):
    """Structural contract for anything :func:`context_utility_score` can score.

    A :class:`~typing.Protocol` rather than a concrete class so that this module
    stays free of dependencies on the gatherer's models. Metrics are the bottom
    of the dependency graph; nothing in the package may import upwards into it.
    """

    @property
    def relevance(self) -> float:
        """Retrieval score of this fact against the task, in ``[0, 1]``."""

    @property
    def confidence(self) -> float:
        """The memory substrate's belief in this fact, in ``[0, 1]``."""


# ---------------------------------------------------------------------------
# Domain guards
#
# Written as small helpers rather than inline conditionals so that the error
# text is uniform across a dozen functions. The messages name the offending
# parameter and echo the value, because these surface in ledger payloads where
# a bare "value out of range" is useless for post-hoc debugging.
# ---------------------------------------------------------------------------


def _finite(name: str, value: float) -> float:
    """Reject ``nan`` and infinities before they poison a comparison."""
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return numeric


def _unit(name: str, value: float) -> float:
    """Require a value in the closed unit interval ``[0, 1]``."""
    numeric = _finite(name, value)
    if not 0.0 <= numeric <= 1.0:
        raise ValueError(f"{name} must lie in [0, 1], got {value!r}")
    return numeric


def _non_negative(name: str, value: float) -> float:
    numeric = _finite(name, value)
    if numeric < 0.0:
        raise ValueError(f"{name} must be non-negative, got {value!r}")
    return numeric


def _positive(name: str, value: float) -> float:
    numeric = _finite(name, value)
    if numeric <= 0.0:
        raise ValueError(f"{name} must be strictly positive, got {value!r}")
    return numeric


def _count(name: str, value: int) -> int:
    """Require a non-negative integer. Rejects bools-as-counts and floats."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {type(value).__name__} {value!r}")
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# §15.9 / §15.1 — context quality
# ---------------------------------------------------------------------------


def context_pollution_ratio(unreferenced_tokens: int, total_tokens: int) -> float:
    r"""Fraction of a context packet that no downstream step actually used.

    RFC §15.9.

    .. math:: P = \frac{T_{unref}}{T_{total}}

    :param unreferenced_tokens: :math:`T_{unref}` — tokens present in the packet
        that resolve no required slot and are carried "just in case".
        Non-negative, and never greater than ``total_tokens``.
    :param total_tokens: :math:`T_{total}` — every token in the packet.
        Non-negative.
    :returns: :math:`P \in [0, 1]`. ``0.0`` means every token earned its place;
        ``1.0`` means the packet is pure noise.

    An empty packet returns ``0.0`` rather than raising :class:`ZeroDivisionError`.
    Zero tokens carry zero pollution — the vacuous case is clean, not undefined —
    and this function sits directly on the gatherer's hot path where an
    exception on the trivially-valid empty input would be a liability.
    """
    unreferenced = _count("unreferenced_tokens", unreferenced_tokens)
    total = _count("total_tokens", total_tokens)
    if unreferenced > total:
        raise ValueError(
            f"unreferenced_tokens ({unreferenced}) cannot exceed total_tokens ({total})"
        )
    if total == 0:
        return 0.0
    return unreferenced / total


def context_utility_score(facts: Iterable[ScoredFact], pollution_ratio: float) -> float:
    r"""Total decision-usable signal carried by a context packet.

    RFC §15.1.

    .. math:: U = \left(\sum_{i=1}^{n} r_i \cdot c_i\right) \cdot (1 - P)

    :param facts: the packet's elements. Each contributes ``relevance *
        confidence`` — a fact that is highly relevant but barely believed, or
        certain but off-topic, is worth little either way, and the product
        captures that far better than a sum would.
    :param pollution_ratio: :math:`P \in [0, 1]` — from
        :func:`context_pollution_ratio`. Scales the whole sum down, because
        padding does not merely fail to help, it actively crowds the attention
        window (RFC §11.1 "context flooding").
    :returns: :math:`U \in [0, n]` where *n* is the number of facts. Not
        normalised by *n* on purpose: two independent corroborating facts are
        genuinely worth more than one, so the score is a total rather than a
        mean.

    An empty iterable scores ``0.0``.
    """
    pollution = _unit("pollution_ratio", pollution_ratio)
    total = 0.0
    for index, fact in enumerate(facts):
        relevance = _unit(f"facts[{index}].relevance", fact.relevance)
        confidence = _unit(f"facts[{index}].confidence", fact.confidence)
        total += relevance * confidence
    return total * (1.0 - pollution)


def context_density(filled_slots: int, required_slots: int) -> float:
    r"""Fraction of the task's required information slots that are filled.

    RFC §5.1(3). This is *the* routing variable: the gatherer compares it
    against ``ContextSettings.density_proceed`` and ``density_hydrate`` to
    decide between planning, hydrating and escalating.

    .. math:: D = \frac{S_{filled}}{S_{required}}

    :param filled_slots: :math:`S_{filled}` — required slots resolved by at
        least one surviving fact. Non-negative, never more than
        ``required_slots``.
    :param required_slots: :math:`S_{required}` — slots the task declared it
        needs. Non-negative.
    :returns: :math:`D \in [0, 1]`.

    **Zero required slots returns 1.0, not 0.0.** A task that declares no
    information requirements is *vacuously* fully satisfied — there is nothing
    outstanding to fetch. Returning ``0.0`` would be the defensible-looking
    choice and it is wrong: it routes every requirement-free task (a trivial
    file read, a fixed command) straight to ``HARD_STOP_ESCALATE_TO_USER``,
    demanding a human resolve slots that do not exist. The failure mode of the
    ``1.0`` convention is far milder — a task with genuinely under-declared
    requirements proceeds and the planner discovers the gap — and it is caught
    upstream by requiring callers to declare slots honestly.
    """
    filled = _count("filled_slots", filled_slots)
    required = _count("required_slots", required_slots)
    if filled > required:
        raise ValueError(f"filled_slots ({filled}) cannot exceed required_slots ({required})")
    if required == 0:
        return 1.0
    return filled / required


def context_entropy(token_probabilities: Iterable[float]) -> float:
    r"""Shannon entropy of a context packet's token distribution, in bits.

    RFC §15.4. Read operationally: high entropy means the packet spreads
    attention thinly over many unrelated things, low entropy means it is
    focused. It is a diagnostic companion to
    :func:`context_pollution_ratio` — pollution counts wasted tokens, entropy
    describes how scattered the surviving ones are.

    .. math:: H = -\sum_{i=1}^{n} p_i \log_2 p_i

    :param token_probabilities: :math:`p_i` — each in ``[0, 1]``.
    :returns: :math:`H \in [0, \log_2 n]` bits, where *n* is the count of
        strictly-positive probabilities. ``0.0`` for an empty input, for a
        single-element input, and for an all-zero input.

    Two conventions, both load-bearing:

    **Zero probabilities are skipped.** :math:`\log_2 0` is undefined, but the
    limit :math:`\lim_{p \to 0^+} p \log_2 p = 0`, so information theory takes
    :math:`0 \log 0 \equiv 0`. Skipping is the numerically safe encoding of
    that identity — an impossible token contributes no surprise.

    **The distribution is normalised if it does not sum to 1.** Callers
    routinely arrive with unnormalised weights (raw attention mass, term
    frequencies, per-element relevance) and normalising is what they always
    meant. Refusing them would push identical division logic into every call
    site, which is where it would eventually be got wrong. The inputs are still
    required individually to be in ``[0, 1]``, so a negative weight or a
    stray count is caught rather than quietly rescaled.
    """
    weights = [
        _unit(f"token_probabilities[{index}]", value)
        for index, value in enumerate(token_probabilities)
    ]
    positive = [weight for weight in weights if weight > 0.0]
    if not positive:
        return 0.0

    total = math.fsum(positive)
    if total <= 0.0:  # pragma: no cover — unreachable given positive filtering
        return 0.0

    entropy = 0.0
    for weight in positive:
        probability = weight / total
        if probability <= 0.0:
            # A subnormal weight can underflow to exactly 0.0 once divided by
            # the normalising total (e.g. 5e-324 alongside a weight of 1.0).
            # Same identity as the pre-normalisation skip above: 0 log 0 == 0.
            # Without this guard math.log2 raises a domain error on an input
            # that is, semantically, a perfectly valid near-zero probability.
            continue
        entropy -= probability * math.log2(probability)

    # Floating-point error can push a uniform distribution a hair below zero
    # (e.g. -1.1e-16). Entropy is non-negative by definition; clamp the noise
    # so the documented range holds exactly for every caller and property test.
    return max(0.0, entropy)


def token_efficiency(density: float, total_tokens: int) -> float:
    r"""Context density purchased per 1000 tokens spent.

    RFC §5.1(5). The unit that makes two gatherer strategies comparable: a
    packet reaching ``D = 0.9`` in 400 tokens is a better packet than one
    reaching ``D = 0.9`` in 1400, and only this ratio says so.

    .. math:: E = \frac{D}{T_{total}} \cdot 1000

    :param density: :math:`D \in [0, 1]` — from :func:`context_density`.
    :param total_tokens: :math:`T_{total}` — tokens the packet actually spent.
        Non-negative.
    :returns: :math:`E \in [0, 1000]` for :math:`T_{total} \geq 1`. Exactly
        ``1000`` would mean full density from a single token.

    ``total_tokens == 0`` returns ``0.0``. The ratio is genuinely undefined
    there (spending nothing to achieve something is not an efficiency, it is a
    division by zero), and ``0.0`` keeps the value orderable against real
    measurements instead of poisoning every downstream comparison with an
    infinity that always sorts first.
    """
    unit_density = _unit("density", density)
    total = _count("total_tokens", total_tokens)
    if total == 0:
        return 0.0
    return (unit_density / total) * 1000.0


# ---------------------------------------------------------------------------
# §15.2 — attention allocation  (SPEC DEVIATION, docs/adr/0012)
# ---------------------------------------------------------------------------


def attention_allocation_score(
    task_criticality: float,
    information_density: float,
    cognitive_effort: float,
    context_pollution: float,
    hallucination_risk: float,
) -> float:
    r"""How much reasoning attention a task deserves relative to its peers.

    RFC §15.2.

    .. math::

        A = \frac{K \cdot I}{F \cdot (1 + P) \cdot (1 + R)}

    :param task_criticality: :math:`K \in [0, 1]` — how much the outcome
        matters.
    :param information_density: :math:`I \in [0, 1]` — how much usable signal
        the available context carries.
    :param cognitive_effort: :math:`F > 0` — estimated reasoning cost. Strictly
        positive; a task costing nothing does not need to be ranked.
    :param context_pollution: :math:`P \in [0, 1]` — from
        :func:`context_pollution_ratio`.
    :param hallucination_risk: :math:`R \in [0, 1]` — estimated probability the
        model fabricates on this task.
    :returns: :math:`A \in \left[0, \frac{K \cdot I}{F}\right]`, hence bounded
        above by :math:`1 / F`. Always finite.

    SPEC DEVIATION (docs/adr/0012)
        The RFC divides by :math:`F \cdot P \cdot R`. Both :math:`P` and
        :math:`R` are probabilities in ``[0, 1]``, which breaks the equation in
        two ways at once, and the second is worse than the first.

        The obvious failure: :math:`P = 0` or :math:`R = 0` is a
        :class:`ZeroDivisionError`. Those are not exotic inputs — they are the
        *best possible* inputs, a perfectly clean packet on a task with no
        fabrication risk. The published equation crashes precisely on its own
        ideal case.

        The subtle failure, and the reason a simple epsilon guard is not a fix:
        for any :math:`P, R < 1` the divisor is a *fraction*, so dividing by it
        **inflates** the score. As pollution falls from 0.5 to 0.01 the score
        rises 50x. The ranking direction is still nominally correct, but the
        magnitudes are unusable — one clean task swamps every other candidate
        by orders of magnitude, and the score cannot be thresholded, averaged
        or plotted against anything.

        Using :math:`(1 + P)` and :math:`(1 + R)` fixes both. The divisor is now
        in ``[1, 4]`` rather than ``(0, 1]``, so the score is bounded and never
        divides by zero, while remaining strictly monotonically decreasing in
        both :math:`P` and :math:`R` — more pollution and more risk still lower
        the score, which is the entire semantic intent of the equation. The
        transform is order-preserving in each variable, so any ranking the RFC's
        form would have produced over a fixed :math:`K, I, F` is preserved.
    """
    criticality = _unit("task_criticality", task_criticality)
    density = _unit("information_density", information_density)
    effort = _positive("cognitive_effort", cognitive_effort)
    pollution = _unit("context_pollution", context_pollution)
    risk = _unit("hallucination_risk", hallucination_risk)

    return (criticality * density) / (effort * (1.0 + pollution) * (1.0 + risk))


# ---------------------------------------------------------------------------
# §15.8 — hybrid retrieval  (SPEC DEVIATION, docs/adr/0013)
# ---------------------------------------------------------------------------


def hybrid_retrieval_score(
    sim_vector: float,
    match_relational: float,
    w_semantic: float,
    w_graph: float,
    decay_lambda: float,
    age_days: float,
) -> float:
    r"""Fused vector + graph retrieval score with temporal decay.

    RFC §15.8. Ranks candidates drawn from two substrates that do not share a
    scale: cosine similarity from the vector index and structural match
    strength from the property graph.

    .. math::

        S = \bigl(Sim \cdot w_{sem} + Match \cdot w_{graph}\bigr) \cdot e^{-\lambda t}

    :param sim_vector: :math:`Sim \in [0, 1]` — cosine similarity from the
        vector index. Negative cosines are semantic anti-matches and are
        discarded by the relevance floor upstream, so the domain here is the
        unit interval.
    :param match_relational: :math:`Match \in [0, 1]` — graph traversal match
        strength.
    :param w_semantic: :math:`w_{sem} \geq 0` — weight on the vector term.
    :param w_graph: :math:`w_{graph} \geq 0` — weight on the graph term.
    :param decay_lambda: :math:`\lambda \geq 0` — per-day decay coefficient,
        from ``MemorySettings.decay_lambda`` for the fact's domain.
    :param age_days: :math:`t \geq 0` — age of the fact in days.
    :returns: :math:`S \in [0, w_{sem} + w_{graph}]`.

    SPEC DEVIATION (docs/adr/0013)
        The RFC writes the equation as
        :math:`(Sim \cdot w_{sem}) + (Match \cdot w_{graph}) \cdot e^{-\lambda t}`.
        Multiplication binds tighter than addition, so as literally written the
        temporal decay applies to **the graph term only** and the semantic term
        never ages at all.

        That is almost certainly a missing pair of parentheses rather than a
        design decision, and taking it literally produces a concrete, damaging
        behaviour: a three-year-old embedding that happens to match the query
        text scores exactly as high as one written this morning, so stale
        semantic memories permanently outrank fresh ones and the decay
        machinery in RFC §4.1 — the whole point of which is that old beliefs
        should lose to new ones — is bypassed for every vector hit. Staleness
        is a property of the *fact*, not of the substrate that happened to
        retrieve it.

        Decay is therefore applied to the fused score. When
        :math:`\lambda = 0` or :math:`t = 0` the two forms coincide exactly, so
        non-decaying domains are unaffected by this change.
    """
    similarity = _unit("sim_vector", sim_vector)
    match = _unit("match_relational", match_relational)
    weight_semantic = _non_negative("w_semantic", w_semantic)
    weight_graph = _non_negative("w_graph", w_graph)
    lambda_ = _non_negative("decay_lambda", decay_lambda)
    age = _non_negative("age_days", age_days)

    fused = similarity * weight_semantic + match * weight_graph
    return fused * math.exp(-lambda_ * age)


# ---------------------------------------------------------------------------
# §15.12, §15.13 — memory + narrative
# ---------------------------------------------------------------------------


def memory_importance_index(
    use_count: int,
    delta: float,
    initial_importance: float,
    decay_lambda: float,
    idle_days: float,
) -> float:
    r"""Retention priority for one memory record.

    RFC §15.12. Drives the curator's eviction order: repeatedly-used memories
    earn their place, untouched ones decay out of hot serving.

    .. math:: I = (I_0 + \delta \cdot N) \cdot e^{-\lambda t_{idle}}

    :param use_count: :math:`N \geq 0` — times this record has been retrieved
        into a context packet.
    :param delta: :math:`\delta \geq 0` — reinforcement per use, from
        ``MemorySettings.use_count_reinforcement``.
    :param initial_importance: :math:`I_0 \in [0, 1]` — importance at creation.
    :param decay_lambda: :math:`\lambda \geq 0` — per-day decay coefficient for
        the record's memory domain.
    :param idle_days: :math:`t_{idle} \geq 0` — days since last retrieval.
    :returns: :math:`I \in [0, I_0 + \delta N]`.

    The result is **not** clamped to ``[0, 1]``. A heavily-reused record
    legitimately exceeds 1.0, and that headroom is exactly what lets the
    curator rank invariants above ordinary facts; clamping would flatten the
    top of the distribution into ties and destroy the ordering the index
    exists to provide.

    Note that reinforcement is applied *before* decay, so the two compose:
    idle time discounts everything a record has earned, rather than being
    cancelled out by a large use count.
    """
    uses = _count("use_count", use_count)
    reinforcement = _non_negative("delta", delta)
    base = _unit("initial_importance", initial_importance)
    lambda_ = _non_negative("decay_lambda", decay_lambda)
    idle = _non_negative("idle_days", idle_days)

    return (base + reinforcement * uses) * math.exp(-lambda_ * idle)


def narrative_coherence_score(
    theme_goal_similarities: Sequence[float],
    contradiction_count: int,
    total_facts: int,
) -> float:
    r"""How well a set of recalled themes hangs together around one goal.

    RFC §15.13. Used by the memory creator to decide whether a session's
    distilled narrative is coherent enough to persist, or whether it is a bag
    of unrelated fragments that should stay episodic.

    .. math::

        NCS = \left(\frac{1}{m}\sum_{i=1}^{m} s_i\right)
              \cdot \left(1 - \frac{X}{F}\right)

    :param theme_goal_similarities: :math:`s_i \in [0, 1]` — similarity of each
        recalled theme to the session goal. :math:`m` is their count.
    :param contradiction_count: :math:`X \geq 0` — mutually contradictory fact
        pairs found. Never more than ``total_facts``.
    :param total_facts: :math:`F \geq 0` — facts considered.
    :returns: :math:`NCS \in [0, 1]`.

    Two boundary conventions:

    **No themes returns 0.0.** Unlike :func:`context_density`, an empty input
    here is not vacuously coherent — there is simply no narrative, and scoring
    a non-existent narrative as perfectly coherent would let the creator
    persist empty distillations.

    **Zero facts leaves the penalty factor at 1.0.** With nothing to
    contradict, the contradiction term is inert rather than undefined, and the
    score reduces to the mean theme similarity.
    """
    similarities = [
        _unit(f"theme_goal_similarities[{index}]", value)
        for index, value in enumerate(theme_goal_similarities)
    ]
    contradictions = _count("contradiction_count", contradiction_count)
    facts = _count("total_facts", total_facts)
    if contradictions > facts:
        raise ValueError(
            f"contradiction_count ({contradictions}) cannot exceed total_facts ({facts})"
        )

    if not similarities:
        return 0.0

    mean_similarity = math.fsum(similarities) / len(similarities)
    penalty = 1.0 if facts == 0 else 1.0 - (contradictions / facts)
    return max(0.0, min(1.0, mean_similarity * penalty))


# ---------------------------------------------------------------------------
# §15.6, §15.10, §15.11 — planning + provider selection
# ---------------------------------------------------------------------------


def planning_cost(
    nodes_expanded: int,
    cost_per_token: float,
    depth: int,
    gamma: float,
) -> float:
    r"""Discounted cost of expanding a tree-of-thought plan.

    RFC §15.6. The planner compares this against its remaining budget before
    widening the search; :class:`~paa.core.types.ModalityProfile.max_plan_nodes`
    bounds :math:`N` from above.

    .. math:: C = N \cdot c \cdot \gamma^{d}

    :param nodes_expanded: :math:`N \geq 0` — reasoning nodes expanded.
    :param cost_per_token: :math:`c \geq 0` — cost of one token at this
        node's model tier.
    :param depth: :math:`d \geq 0` — nesting depth of the expansion.
    :param gamma: :math:`\gamma \geq 0` — per-level discount. Values below 1
        make deep expansions cheaper to *account for*, mirroring the halving
        token quota of RFC §15.7: a depth-3 sub-agent works with a small
        fraction of the root budget, so charging it at the root rate would
        overstate its cost and stop the planner descending at all.
    :returns: :math:`C \in [0, \infty)`.

    Following Python's :func:`pow`, :math:`\gamma = 0, d = 0` yields
    :math:`0^0 = 1` — a root expansion is charged in full even under a
    degenerate discount.
    """
    nodes = _count("nodes_expanded", nodes_expanded)
    unit_cost = _non_negative("cost_per_token", cost_per_token)
    level = _count("depth", depth)
    discount = _non_negative("gamma", gamma)

    return nodes * unit_cost * (discount**level)


def provider_reliability(runs: Sequence[float], beta: float) -> float:
    r"""Recency-weighted success rate of an inference provider.

    RFC §15.10. Feeds :func:`skill_rank` and the escalation decision.

    .. math:: R = \frac{\sum_{i=1}^{n} \beta^{\,n-i} \cdot s_i}
                       {\sum_{i=1}^{n} \beta^{\,n-i}}

    :param runs: :math:`s_i \in [0, 1]` — per-run outcomes in **chronological
        order, oldest first**. Booleans are accepted for the common
        success/failure case. The newest run therefore carries weight
        :math:`\beta^0 = 1` and older runs are discounted geometrically.
    :param beta: :math:`\beta \in (0, 1]` — recency weight. :math:`\beta = 1`
        degenerates to an unweighted mean; small :math:`\beta` makes the score
        track only the last few runs.
    :returns: :math:`R \in [0, 1]`.

    An empty history returns ``0.5``, a deliberately uninformative prior.
    Returning ``0.0`` would mark every newly-registered provider as maximally
    unreliable and it would never be selected, so it would never accumulate the
    history needed to prove otherwise — a cold-start deadlock. ``1.0`` has the
    opposite flaw, routing real traffic to something wholly unproven. A neutral
    prior lets the first few real runs dominate quickly.
    """
    weight_base = _finite("beta", beta)
    if not 0.0 < weight_base <= 1.0:
        raise ValueError(f"beta must lie in (0, 1], got {beta!r}")

    outcomes = [_unit(f"runs[{index}]", value) for index, value in enumerate(runs)]
    if not outcomes:
        return 0.5

    n = len(outcomes)
    weights = [weight_base ** (n - 1 - index) for index in range(n)]
    total_weight = math.fsum(weights)
    if total_weight <= 0.0:
        # Reachable only when beta is denormal-small and the history is long
        # enough that every weight underflows to 0.0. Fall back to the newest
        # observation, which is what beta -> 0 means in the limit anyway.
        return outcomes[-1]

    weighted = math.fsum(
        weight * outcome for weight, outcome in zip(weights, outcomes, strict=True)
    )
    return max(0.0, min(1.0, weighted / total_weight))


def skill_rank(
    reliability: float,
    risk_profile: float,
    w_reliability: float,
    w_risk: float,
) -> float:
    r"""Selection score for one skill among several that could serve a task.

    RFC §15.11.

    .. math::

        Rank = \frac{w_R \cdot R + w_K \cdot (1 - K)}{w_R + w_K}

    :param reliability: :math:`R \in [0, 1]` — historical success rate, e.g.
        from :func:`provider_reliability`.
    :param risk_profile: :math:`K \in [0, 1]` — declared blast radius of the
        skill's effects. Enters as :math:`(1 - K)` so that *safety* is what is
        being rewarded.
    :param w_reliability: :math:`w_R \geq 0` — weight on reliability.
    :param w_risk: :math:`w_K \geq 0` — weight on safety. Raised in
        ``SUPERVISED``/``SAFE`` modes to bias selection toward timid skills.
    :returns: :math:`Rank \in [0, 1]`.

    Expressed as a normalised convex combination rather than a bare weighted
    difference. A difference form (:math:`w_R R - w_K K`) produces negative
    scores whose scale shifts whenever the weights are retuned, which makes any
    absolute threshold configured against it silently wrong after a config
    change. Dividing by :math:`w_R + w_K` pins the output to ``[0, 1]`` for
    every weight pair, so thresholds stay meaningful and two differently-tuned
    deployments remain comparable.

    At least one weight must be non-zero: with both at zero there is no
    ranking criterion left and the caller has misconfigured the selector.
    """
    reliability_value = _unit("reliability", reliability)
    risk_value = _unit("risk_profile", risk_profile)
    weight_reliability = _non_negative("w_reliability", w_reliability)
    weight_risk = _non_negative("w_risk", w_risk)

    total_weight = weight_reliability + weight_risk
    if total_weight <= 0.0:
        raise ValueError("at least one of w_reliability / w_risk must be strictly positive")

    numerator = weight_reliability * reliability_value + weight_risk * (1.0 - risk_value)
    return max(0.0, min(1.0, numerator / total_weight))
