"""Manifest-shaped declarations for the in-process pipelines (C6.4 / P1.5).

ADR-0052 fixes the manifest's parse contract for *installed* pipelines;
the in-process built-ins have no ``pipeline.toml`` to parse, but the
overlay loader (:mod:`paa.pipelines.overlay`) needs exactly the shapes
that contract fixes — the capability ask, the limits, the typed params,
the declared slots — to check an overlay against. This module states
those declarations in code, one per registered in-process pipeline, in
the manifest's own vocabulary, so "tighten-only" means the same thing
for a built-in configured by overlay and for an installed package
configured by overlay: the overlay is validated against the pipeline's
declared ask, never against what it happens to reach at runtime.

The declarations are deliberately *declarations*, not a second source of
truth about behaviour: ``builtin.chat``'s ``recall_k`` default restates
``ChatSettings.recall_k``'s, and a drift between them is a bug in this
file, pinned by the tests. What the declaration adds that the class
attributes (``capabilities``, ``limits``) cannot: the params schema with
ranges, the slot interfaces, and the manifest-shaped capability ask
(roles for ``think``, tool names for ``use_skill``) that an overlay
narrows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from paa.pipelines.base import RunLimits

__all__ = [
    "BUILTIN_DECLARATIONS",
    "ParamSpec",
    "PipelineDeclaration",
    "SlotDeclaration",
    "builtin_context_provider",
    "declaration_for",
    "register_context_provider",
    "resolve_context_provider",
]


# ---------------------------------------------------------------------------
# The declaration shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """One declared ``[params]`` entry: a typed, ranged knob.

    ``type`` is the manifest's closed set (``int``/``bool``/``str``);
    ``minimum``/``maximum`` bound the int range the way the manifest's
    ``min``/``max`` do. A param without a range accepts any value of its
    type — the declaration is the contract, and a knob the pipeline
    genuinely does not bound says so by leaving the bounds ``None``.
    """

    type: str
    default: Any
    minimum: int | None = None
    maximum: int | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class SlotDeclaration:
    """One declared ``[slots]`` entry: a named, typed part to swap.

    ``interface`` is the contract the replacement must satisfy
    (``agent/1``, ``context-provider/1``); ``default`` is the ref the
    pipeline ships with, in the manifest's ref syntax (``builtin:packet``,
    ``agent:critic``, ``none``).
    """

    name: str
    interface: str
    default: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class PipelineDeclaration:
    """One in-process pipeline's manifest-shaped ask.

    The closed vocabulary of ADR-0052's ``[pipeline]``/``[capabilities]``
    /``[limits]``/``[params]``/``[slots]`` sections, stated in code:
    the overlay loader validates against this and only this.

    ``resume`` and ``evidence_tier`` carry the two facts the external-
    harness spec (§6) adds to that vocabulary: whether the pipeline can
    honour the replay contract (``journal``) or honestly cannot
    (``none`` — the host refuses to mark such a run recoverable), and
    the honesty tier of the evidence it produces (``T1`` observed,
    ``T2`` mediated, ``T3`` contained; the in-process built-ins are
    ``in-process`` — they are not adapters over a foreign mind).
    """

    name: str
    version: str
    requires_runtime: str
    description: str
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    limits: RunLimits = field(default_factory=RunLimits)
    params: Mapping[str, ParamSpec] = field(default_factory=dict)
    slots: Mapping[str, SlotDeclaration] = field(default_factory=dict)
    trust: str = "builtin"
    resume: str = "journal"
    evidence_tier: str = "in-process"


# ---------------------------------------------------------------------------
# The context-provider registry (chat's swappable slot, resolved by ref)
# ---------------------------------------------------------------------------

#: The provider signature chat's slot demands: the packet compiler the
#: loop hands candidates to. Same shape as
#: :func:`paa.chat.context_bundle.build_context_bundle`, stated as a
#: callable so an overlay's replacement is a function, not a subclass.
ContextProvider = Callable[..., Any]


def builtin_context_provider(
    *,
    persona: str,
    recalled_facts: list[dict[str, Any]],
    recent_turns: list[dict[str, Any]],
    token_ceiling: int,
) -> Any:
    """The default ``builtin:packet`` ref: the shipped bundle compiler."""
    from paa.chat.context_bundle import build_context_bundle

    return build_context_bundle(
        persona=persona,
        recalled_facts=recalled_facts,
        recent_turns=recent_turns,
        token_ceiling=token_ceiling,
    )


_PROVIDERS: dict[str, ContextProvider] = {"builtin:packet": builtin_context_provider}


def register_context_provider(ref: str, provider: ContextProvider) -> None:
    """Register a context provider under a manifest-shaped ref.

    The host (or a test) owns what a ref resolves to; the registry only
    holds the mapping. Registering an existing ref replaces it — the
    operator's later word wins, the same rule the settings cascade uses.
    """
    if not ref or ":" not in ref:
        raise ValueError(
            f"a context provider ref must be manifest-shaped ('prefix:name'), got {ref!r}"
        )
    _PROVIDERS[ref] = provider


def resolve_context_provider(ref: str) -> ContextProvider:
    """Resolve a ref, or raise naming the ref and the registered ones."""
    try:
        return _PROVIDERS[ref]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS)) or "(none)"
        raise ValueError(
            f"unknown context provider ref {ref!r}; registered: {known}"
        ) from None


# ---------------------------------------------------------------------------
# The declarations
# ---------------------------------------------------------------------------


def _builtin_declaration() -> PipelineDeclaration:
    """``builtin.plan-execute-critique``: five agent slots, no params.

    The five capabilities restate ``_BUILTIN_CAPABILITIES`` in the
    manifest's vocabulary; the slot defaults restate the orchestrator's
    construction wiring (``StageAgents``'s fields). A ``none`` default is
    the honest statement for a stage the shipped configuration disables.
    """

    def _agent_slot(role: str) -> SlotDeclaration:
        return SlotDeclaration(
            name=role,
            interface="agent/1",
            default=f"agent:{role}",
            description=f"the {role} stage agent",
        )

    return PipelineDeclaration(
        name="builtin.plan-execute-critique",
        version="4.1.0",
        requires_runtime=">=4.1,<5",
        description="hydrate, plan, policy, execute steps with critique and retry, commit",
        capabilities={
            "call_agent": ["context_builder", "planner", "policy", "worker", "critic"],
            "commit": True,
        },
        limits=RunLimits(),
        slots={
            "context_builder": _agent_slot("context_builder"),
            "planner": _agent_slot("planner"),
            "policy": _agent_slot("policy"),
            "worker": _agent_slot("worker"),
            "critic": _agent_slot("critic"),
        },
    )


def _chat_declaration() -> PipelineDeclaration:
    """``builtin.chat``: the context-provider slot and the turn knobs."""
    return PipelineDeclaration(
        name="builtin.chat",
        version="4.1.0",
        requires_runtime=">=4.1,<5",
        description="one conversational turn: recall, bundle, think, remember",
        capabilities={
            "think": {"roles": ["assistant"]},
            "recall": True,
            "remember": True,
        },
        params={
            "recall_k": ParamSpec(
                type="int", default=5, minimum=0, maximum=50,
                description="semantic facts a turn's recall may consult",
            ),
            "last_k_turns": ParamSpec(
                type="int", default=6, minimum=0, maximum=100,
                description="how many of this session's recent turns the bundle carries",
            ),
            "tokens_budget": ParamSpec(
                type="int", default=0, minimum=0,
                description=(
                    "hard token ceiling for the context bundle; "
                    "0 = the context engine's planner ceiling"
                ),
            ),
        },
        slots={
            "context_provider": SlotDeclaration(
                name="context_provider",
                interface="context-provider/1",
                default="builtin:packet",
                description="assembles the turn's context packet from persona, facts and turns",
            ),
        },
    )


def _coder_declaration() -> PipelineDeclaration:
    """``coder.tool-loop``: the step/token knobs, tool allowlist by host.

    The tool names an overlay may narrow are the coder loop's standard
    set; the host's per-run tool wiring is the superset an overlay trims.
    """
    return PipelineDeclaration(
        name="coder.tool-loop",
        version="4.1.0",
        requires_runtime=">=4.1,<5",
        description="the window-driven coder tool loop: think, gated tool calls, budget",
        capabilities={
            "think": {"roles": ["worker"]},
            "use_skill": [
                "read_file", "glob", "grep", "write_file", "edit_file",
                "bash", "git_commit",
            ],
        },
        params={
            "max_steps": ParamSpec(
                type="int", default=25, minimum=1, maximum=100,
                description="the step ceiling per run",
            ),
            "max_tokens": ParamSpec(
                type="int", default=0, minimum=0,
                description="the token budget per run; 0 = unbounded at this layer",
            ),
        },
    )


def _observe_declaration() -> PipelineDeclaration:
    """``builtin.observe``: the T1 observer adapter, pipeline #4 (P1.4/C7.1).

    The external-harness spec's honesty row, in manifest vocabulary: an
    observed CLI is a pipeline whose mind PAA does not control, so the
    declaration asks for **no** capabilities (there is nothing to grant —
    the host runs the child and records what it sees), the evidence tier
    is the adapter's own ``T1``, and ``resume = "none"`` is the honest
    statement the spec §6 requires: the host refuses to mark such a run
    recoverable, and the crash classifier's job is to *name the state the
    workspace was left in*, never to promise a resume.
    """
    return PipelineDeclaration(
        name="builtin.observe",
        version="4.1.0",
        requires_runtime=">=4.1,<5",
        description=(
            "the T1 observer: run any CLI in a workspace, record the manifest "
            "diff, git changes and transcript, classify the state after a crash"
        ),
        capabilities={},
        params={
            "timeout_s": ParamSpec(
                type="int", default=600, minimum=1, maximum=86400,
                description="the observed run's wall-clock budget in seconds",
            ),
            "memory_mb": ParamSpec(
                type="int", default=512, minimum=16, maximum=65536,
                description="the observed child's memory ceiling in MB",
            ),
            "transcript_cap_kb": ParamSpec(
                type="int", default=2048, minimum=1, maximum=65536,
                description=(
                    "per-stream transcript cap in KiB; beyond it the bundle "
                    "carries the head and says it was truncated"
                ),
            ),
        },
        resume="none",
        evidence_tier="T1",
    )


BUILTIN_DECLARATIONS: dict[str, PipelineDeclaration] = {
    _builtin_declaration().name: _builtin_declaration(),
    _chat_declaration().name: _chat_declaration(),
    _coder_declaration().name: _coder_declaration(),
    _observe_declaration().name: _observe_declaration(),
}


def declaration_for(name: str) -> PipelineDeclaration | None:
    """The declaration of a registered in-process pipeline, or ``None``."""
    return BUILTIN_DECLARATIONS.get(name)
