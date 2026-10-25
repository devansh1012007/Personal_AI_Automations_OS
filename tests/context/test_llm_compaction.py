"""Tests for the optional LLM summarizer seam on ``ContextCompactor``.

The compactor is mechanical and deterministic by default; an optional injected
``summarizer`` callable (``str -> str``) may further compress text after the
mechanical fixed point. This file proves both paths:

* with a summarizer: it is called exactly once per ``compact()`` on the
  mechanically-reduced text, and its output is returned;
* without a summarizer (default): behavior is unchanged — purely mechanical,
  deterministic, idempotent.

A call-counting summarizer is used so the "exactly once" and
"used-when-injected / not-used-when-absent" claims are asserted structurally.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import pytest

from paa.context.budget import CharEstimator
from paa.context.compaction import ContextCompactor
from paa.context.gatherer import (
    BoundedContextGatherer,
    ContextElement,
    ContextPacket,
)


class _RecordingSummarizer:
    """A ``str -> str`` summarizer that records every input it receives."""

    def __init__(self, output: str = "SUMMARY") -> None:
        self.output = output
        self.calls: list[str] = []

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        return self.output


@pytest.fixture
def summarizer() -> _RecordingSummarizer:
    return _RecordingSummarizer()


# ---------------------------------------------------------------------------
# Injected summarizer path
# ---------------------------------------------------------------------------


def test_compact_returns_summarizer_output(summarizer: _RecordingSummarizer) -> None:
    compactor = ContextCompactor(summarizer=summarizer)
    result = compactor.compact("any input text at all")
    assert result == "SUMMARY"


def test_summarizer_is_called_exactly_once_per_compact(
    summarizer: _RecordingSummarizer,
) -> None:
    compactor = ContextCompactor(summarizer=summarizer)
    compactor.compact("first call")
    compactor.compact("second call")
    assert summarizer.calls == ["first call", "second call"]
    assert len(summarizer.calls) == 2


def test_summarizer_receives_the_mechanical_fixed_point(
    summarizer: _RecordingSummarizer,
) -> None:
    """The summarizer gets the mechanically-reduced text, not the raw input."""
    compactor = ContextCompactor(summarizer=summarizer)
    compactor.compact("**bold**   line\n\n**bold**   line\n   extra   spaces   ")
    assert summarizer.calls == ["bold line\n\nextra spaces"]


def test_summarizer_is_invoked_once_on_empty_input(
    summarizer: _RecordingSummarizer,
) -> None:
    compactor = ContextCompactor(summarizer=summarizer)
    assert compactor.compact("") == "SUMMARY"
    assert summarizer.calls == [""]


def test_summarizer_output_passes_through_even_when_longer(
    summarizer: _RecordingSummarizer,
) -> None:
    """With a summarizer, non-expansion is waived: longer output passes through."""
    summarizer.output = "x" * 500
    compactor = ContextCompactor(summarizer=summarizer)
    result = compactor.compact("short")
    assert result == "x" * 500


# ---------------------------------------------------------------------------
# Mechanical default (summarizer is None)
# ---------------------------------------------------------------------------


def test_default_is_purely_mechanical_and_idempotent() -> None:
    """No summarizer: unchanged behavior, idempotent mechanical output."""
    compactor = ContextCompactor()
    raw = "# Heading\n\n\n**bold**   text\n**bold**   text\n> quote\n- item\n"
    once = compactor.compact(raw)
    assert compactor.compact(once) == once
    assert once == "Heading\n\nbold text\nquote\nitem"


def test_no_summarizer_means_no_model_like_call() -> None:
    """Default construction stores None and never invokes a summarizer."""
    compactor = ContextCompactor()
    assert compactor.summarizer is None
    assert compactor.compact("still mechanical") == "still mechanical"


# ---------------------------------------------------------------------------
# Construction validation is unchanged
# ---------------------------------------------------------------------------


def test_summarizer_parameter_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        ContextCompactor(lambda x: x)  # type: ignore[call-arg]


def test_repr_unchanged_when_summarizer_absent() -> None:
    compactor = ContextCompactor()
    assert "max_line_chars=" in repr(compactor)


# ---------------------------------------------------------------------------
# Optional LLM ranking on BoundedContextGatherer (T-007)
#
# The gatherer ranks mechanically (tier, relevance x confidence, importance, id).
# An injected `ranker` callable may re-order the mechanically-sorted pool after
# the sort in `_compile`; its output sequence is what gets packed.
# ---------------------------------------------------------------------------

SLOTS = ["s0", "s1", "s2"]


def element(
    element_id: str,
    *,
    tokens: int = 10,
    slot: str | None = None,
    relevance: float = 1.0,
    confidence: float = 1.0,
    importance: float = 0.0,
    domain: str = "semantic",
) -> ContextElement:
    """Terse builder. `token_cost` is set explicitly so packing maths is exact."""
    return ContextElement(
        id=element_id,
        content=f"content-{element_id}",
        token_cost=tokens,
        slot=slot,
        relevance=relevance,
        confidence=confidence,
        importance=importance,
        memory_domain=domain,
    )


def _ids(packet: ContextPacket) -> list[str]:
    return [item.id for item in packet.elements]


def _make_gatherer(ranker=None) -> BoundedContextGatherer:
    return BoundedContextGatherer(estimator=CharEstimator(4.0), ranker=ranker)


@dataclass
class RecordingRanker:
    """A ranker that records every call and returns a scripted order."""

    calls: list[tuple[list[ContextElement], frozenset[str], float]] = field(
        default_factory=list
    )
    scripted_order: list[str] | None = None
    raise_on_call: bool = False

    def __call__(
        self,
        args: tuple[list[ContextElement], frozenset[str], float],
    ) -> list[ContextElement]:
        pool, required, invariant_floor = args
        self.calls.append((list(pool), required, invariant_floor))
        if self.raise_on_call:
            raise RuntimeError("ranker blew up")
        if self.scripted_order is not None:
            by_id = {item.id: item for item in pool}
            return [by_id[name] for name in self.scripted_order if name in by_id]
        return list(pool)


# ---------------------------------------------------------------------------
# ranker is None (default): purely mechanical, unchanged behaviour
# ---------------------------------------------------------------------------


def test_default_ranker_is_none_is_purely_mechanical() -> None:
    """No ranker injected => mechanical ranking stands, identical to before."""
    resolver = element("resolver", slot="s0", relevance=0.5, confidence=0.8)
    colour = element("colour", relevance=1.0, confidence=1.0)
    packet = _make_gatherer().compile([resolver, colour], [], ["s0"])
    assert _ids(packet) == ["resolver", "colour"]


def test_no_ranker_means_no_model_is_consulted() -> None:
    """A default gatherer performs only mechanical ranking."""
    gatherer = _make_gatherer()
    packet = gatherer.compile([element("a")], [], [])
    assert _ids(packet) == ["a"]


def test_mechanical_order_is_deterministic_total_ordering() -> None:
    """Tier, then score, then importance, then id: total and reproducible."""
    candidates = [
        element("colour", relevance=1.0, confidence=1.0),
        element("invariant", importance=0.9),
        element("resolver", slot="s0", relevance=0.8, confidence=0.75),
    ]
    packet = _make_gatherer().compile(candidates, [], ["s0"])
    assert _ids(packet) == ["resolver", "invariant", "colour"]


# ---------------------------------------------------------------------------
# ranker provided: called exactly once per compile() with the sorted pool
# ---------------------------------------------------------------------------


def test_ranker_is_called_exactly_once_per_compile() -> None:
    ranker = RecordingRanker()
    gatherer = _make_gatherer(ranker=ranker)
    facts = [element(f"f{i}", slot=f"s{i % 3}") for i in range(6)]
    gatherer.compile(facts, [], ["s0", "s1", "s2"])
    gatherer.compile(facts, [], ["s0", "s1", "s2"])
    assert len(ranker.calls) == 2


def test_ranker_receives_the_mechanically_sorted_pool() -> None:
    """The ranker must see the post-sort pool, not the raw candidate order."""
    ranker = RecordingRanker()
    gatherer = _make_gatherer(ranker=ranker)
    facts = [
        element("low", relevance=0.1, confidence=0.8),
        element("high", slot="s0", relevance=0.9, confidence=0.9),
    ]
    gatherer.compile(facts, [], ["s0"])
    pool, required, invariant_floor = ranker.calls[0]
    assert [item.id for item in pool] == ["high", "low"]
    assert required == frozenset({"s0"})
    assert invariant_floor == gatherer.settings.invariant_importance


def test_ranker_input_is_the_post_sort_pool_not_raw_arrival_order() -> None:
    """A shuffled arrival must still present the ranker the same sorted pool."""
    ranker = RecordingRanker()
    gatherer = _make_gatherer(ranker=ranker)
    facts = [element(f"f{i}", relevance=float(i) / 4.0, confidence=1.0) for i in range(5)]
    gatherer.compile(facts, [], [])
    shuffled = list(facts)
    random.Random(7).shuffle(shuffled)
    gatherer.compile(shuffled, [], [])
    assert len(ranker.calls) == 2
    assert [item.id for item in ranker.calls[0][0]] == [
        item.id for item in ranker.calls[1][0]
    ]


# ---------------------------------------------------------------------------
# ranker output order drives packing
# ---------------------------------------------------------------------------


def test_ranker_output_order_is_used_for_packing() -> None:
    """The ranker's returned sequence, not the mechanical one, fills the budget."""
    ranker = RecordingRanker(scripted_order=["colour", "resolver"])
    gatherer = _make_gatherer(ranker=ranker)
    resolver = element("resolver", slot="s0", relevance=0.5, confidence=0.8)
    colour = element("colour", relevance=1.0, confidence=1.0)
    packet = gatherer.compile([resolver, colour], [], ["s0"], token_ceiling=10)
    assert _ids(packet) == ["colour"]


def test_ranker_can_deprioritise_a_slot_resolver_below_colour() -> None:
    """An LLM may judge colour more decision-usable than a weak slot resolver."""
    ranker = RecordingRanker(scripted_order=["colour", "resolver"])
    gatherer = _make_gatherer(ranker=ranker)
    resolver = element("resolver", slot="s0", relevance=0.1, confidence=0.8)
    colour = element("colour", relevance=1.0, confidence=1.0)
    packet = gatherer.compile([resolver, colour], [], ["s0"], token_ceiling=10)
    assert _ids(packet) == ["colour"]
    assert packet.vacant_slots == ("s0",)


def test_ranker_returning_subset_packs_only_those() -> None:
    """A ranker may drop candidates; only its returned elements are packed."""
    ranker = RecordingRanker(scripted_order=["kept"])
    gatherer = _make_gatherer(ranker=ranker)
    facts = [element("kept", slot="s0"), element("dropped", slot="s1")]
    packet = gatherer.compile(facts, [], ["s0", "s1"], token_ceiling=100)
    assert _ids(packet) == ["kept"]
    assert packet.vacant_slots == ("s1",)


# ---------------------------------------------------------------------------
# Boundaries: empty pool, ranker with nothing to rank
# ---------------------------------------------------------------------------


def test_ranker_called_with_empty_pool() -> None:
    ranker = RecordingRanker()
    gatherer = _make_gatherer(ranker=ranker)
    packet = gatherer.compile([], [], ["s0"])
    assert packet.elements == ()
    assert len(ranker.calls) == 1
    assert ranker.calls[0][0] == []


def test_ranker_on_empty_input_yields_empty_packet() -> None:
    ranker = RecordingRanker(scripted_order=[])
    gatherer = _make_gatherer(ranker=ranker)
    packet = gatherer.compile([], [], [])
    assert packet.elements == ()
    assert packet.allocated_tokens == 0


def test_ceiling_still_enforced_after_ranker_reorder() -> None:
    """The DoD bound survives an LLM re-ordering the pool."""
    ranker = RecordingRanker(scripted_order=[f"f{i}" for i in range(20)])
    gatherer = _make_gatherer(ranker=ranker)
    facts = [element(f"f{i}", tokens=400, slot=f"s{i}") for i in range(20)]
    packet = gatherer.compile(facts, [], [f"s{i}" for i in range(20)])
    assert packet.allocated_tokens <= 1500
    assert len(packet.elements) == 3


# ---------------------------------------------------------------------------
# ranker exceptions propagate (no swallowing)
# ---------------------------------------------------------------------------


def test_ranker_failure_propagates_instead_of_being_swallowed() -> None:
    ranker = RecordingRanker(raise_on_call=True)
    gatherer = _make_gatherer(ranker=ranker)
    with pytest.raises(RuntimeError, match="ranker blew up"):
        gatherer.compile([element("a")], [], [])


# ---------------------------------------------------------------------------
# Both compile() and compile_worker_packet() route through _compile
# ---------------------------------------------------------------------------


def test_worker_packet_also_uses_the_ranker() -> None:
    ranker = RecordingRanker(scripted_order=["fact"])
    gatherer = _make_gatherer(ranker=ranker)
    facts = [element("prose", tokens=10, domain="narrative"), element("fact", tokens=10)]
    packet = gatherer.compile_worker_packet(facts, [], [])
    assert _ids(packet) == ["fact"]
    assert len(ranker.calls) == 1
