"""The planner context builder's hot tier: what a search term is allowed to match.

``_hot_facts`` is the one place where a *word* meets a SQL ``LIKE`` pattern,
and a ``LIKE`` pattern is the one place where a value silently changes the
meaning of a query: ``%`` matches any run of characters and ``_`` matches any
one of them. A term that happens to contain one of them is a word, not a
pattern, and a search that compiles it as a pattern answers a different
question from the one that was asked.

The consequence here is worse than a noisy search. A retrieved fact becomes a
:class:`~paa.context.gatherer.ContextElement` with ``relevance=1.0`` and
``slot=`` the fact's *predicate*, and the gatherer resolves a required slot
from that predicate. A fact nobody asked for can therefore *resolve* a
required slot, which inflates the context density and silently retires the
vacancy that would have sent the task to a human. The hot tier is not
relevance-filtered — the relevance floor applies to the semantic tier only —
so nothing downstream removes a fact that should never have been retrieved.

Every test here runs against the real SQLite schema and lets the engine decide
what a pattern means. A test that asserted on a hand-written reading of what
``%`` means would be asserting this file's understanding of ``LIKE`` rather
than the database's, and the gap between the two is exactly the bug under
test.

Each test seeds a decoy — a row the old wildcard behaviour swept in — so a
regression cannot hide behind an assertion that only checks the happy path.

The file also covers the *worker* builder's half of the module, because the
one thing it must guarantee is the thing the planner builder does not: a
worker packet carries no narrative memory (RFC §2.2). That guarantee is a
security boundary rather than a size optimisation, so it is checked on the
compiled packet — the artefact the worker would actually receive — instead of
on the filter that fed it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

import pytest

from paa.agents.base import AgentContext, AgentMessage, MessageType
from paa.agents.context_agents import PlannerContextBuilder, WorkerContextBuilder
from paa.context.gatherer import BoundedContextGatherer, ContextElement, ContextPacket
from paa.core.types import ComplexityModality, PermissionMode
from paa.memory.facts import FactRepository
from paa.storage.relational.database import Database


@pytest.fixture
def repo(db: Database) -> FactRepository:
    return FactRepository(db)


@pytest.fixture
def builder(db: Database) -> PlannerContextBuilder:
    """A builder with nothing but the relational store.

    No model, no vector store, no graph store: the hot tier is a relational
    scan, so anything else here would be a dependency the test does not need.
    """
    return PlannerContextBuilder(db=db)


async def _seed(
    repo: FactRepository,
    entity_id: str,
    predicate: str,
    value: str,
    *,
    importance: float = 0.5,
) -> str:
    """Write one fact and return its id.

    ``importance`` is spelled out rather than left at the default so the
    ordering tests can put several facts on *exactly* the same value — a
    float, so "the same" has to mean the same bits, not merely "close".
    """
    fact_id: str = await repo.add_fact(
        entity_id, predicate, value, confidence=0.9, importance=importance
    )
    return fact_id


async def _pin_ids(db: Database, renames: Sequence[tuple[str, str]]) -> None:
    """Rewrite fact ids so the expected order is known.

    The tiebreaker is documented as ``f.id ASC`` and ids are UUIDs, so a test
    that asserted "the result is in id order" against random ids would pass by
    luck about one time in six. Pinning them makes the assertion about the
    query rather than about the id generator, and pinning them *against* the
    insertion order is what makes the untiebroken ordering fail the test
    rather than pass it by chance.
    """
    for new_id, old_id in renames:
        await db.execute(
            "UPDATE hot_serving_active_facts SET id = ? WHERE id = ?", (new_id, old_id)
        )


class TestTheSearchIsLiteral:
    async def test_an_underscore_in_a_term_matches_that_text_and_not_a_wildcard(
        self, builder: PlannerContextBuilder, repo: FactRepository
    ) -> None:
        entity_id = await repo.upsert_entity("concept", "Widget")
        await _seed(repo, entity_id, "is", "foo_bar")
        await _seed(repo, entity_id, "is", "fooXbar")

        elements = await builder._hot_facts(["foo_bar"])

        assert [element.content for element in elements] == ["Widget is foo_bar"]

    async def test_a_percent_in_a_term_matches_only_the_fact_that_contains_one(
        self, builder: PlannerContextBuilder, repo: FactRepository
    ) -> None:
        entity_id = await repo.upsert_entity("concept", "Widget")
        await _seed(repo, entity_id, "is", "100% complete")
        await _seed(repo, entity_id, "is", "quarterly report")

        elements = await builder._hot_facts(["%"])

        assert [element.content for element in elements] == ["Widget is 100% complete"]

    async def test_a_nul_byte_does_not_widen_the_search_to_every_fact(
        self, builder: PlannerContextBuilder, repo: FactRepository
    ) -> None:
        entity_id = await repo.upsert_entity("concept", "Widget")
        await _seed(repo, entity_id, "is", "widget")
        await _seed(repo, entity_id, "is", "unrelated")
        await _seed(repo, entity_id, "is", "gadget")

        elements = await builder._hot_facts(["\x00widget"])

        assert [element.content for element in elements] == ["Widget is widget"]

    async def test_a_term_that_matches_nothing_returns_no_elements(
        self, builder: PlannerContextBuilder, repo: FactRepository
    ) -> None:
        entity_id = await repo.upsert_entity("concept", "Widget")
        await _seed(repo, entity_id, "is", "noXsuch_fact")

        elements = await builder._hot_facts(["no_such_fact"])

        assert elements == []

    async def test_a_term_with_nothing_to_search_for_is_skipped_and_the_rest_still_match(
        self, builder: PlannerContextBuilder, repo: FactRepository
    ) -> None:
        """One unusable term among usable ones must not break the query.

        A term that was nothing but NUL bytes has no pattern to build, so it
        is skipped — and the SQL is built from the terms that *survived*, not
        from the terms that were offered. Building the clause list from the
        offered terms instead would leave the statement with more
        placeholders than parameters, and every call would fail with a
        binding error rather than with a wrong answer.
        """
        entity_id = await repo.upsert_entity("concept", "Widget")
        await _seed(repo, entity_id, "is", "foo_bar")

        elements = await builder._hot_facts(["foo_bar", "\x00"])

        assert [element.content for element in elements] == ["Widget is foo_bar"]

    async def test_terms_that_are_all_unsearchable_retrieve_nothing(
        self, builder: PlannerContextBuilder, repo: FactRepository
    ) -> None:
        entity_id = await repo.upsert_entity("concept", "Widget")
        await _seed(repo, entity_id, "is", "foo_bar")

        elements = await builder._hot_facts(["\x00", "\x00\x00"])

        assert elements == []

    async def test_the_elements_carry_the_fact_as_a_triple_and_its_predicate_as_the_slot(
        self, builder: PlannerContextBuilder, repo: FactRepository
    ) -> None:
        entity_id = await repo.upsert_entity("concept", "Widget")
        await _seed(repo, entity_id, "is", "foo_bar")
        await _seed(repo, entity_id, "was", "fooXbar")

        elements = await builder._hot_facts(["foo_bar"])

        assert len(elements) == 1
        element = elements[0]
        assert isinstance(element, ContextElement)
        assert element.content == "Widget is foo_bar"
        assert element.slot == "is"


class TestTheOrderingIsReproducible:
    async def test_facts_sharing_an_importance_come_back_in_the_same_order_every_time(
        self, builder: PlannerContextBuilder, repo: FactRepository, db: Database
    ) -> None:
        entity_id = await repo.upsert_entity("concept", "Widget")
        ids = [await _seed(repo, entity_id, "is", f"shared-{index}") for index in range(3)]
        await _pin_ids(db, [("id-c", ids[0]), ("id-b", ids[1]), ("id-a", ids[2])])

        first = await builder._hot_facts(["shared"])
        second = await builder._hot_facts(["shared"])

        assert [element.id for element in first] == ["id-a", "id-b", "id-c"]
        assert [element.id for element in second] == [element.id for element in first]

    async def test_the_limit_cut_takes_the_most_important_facts_and_breaks_ties_by_id(
        self, builder: PlannerContextBuilder, repo: FactRepository, db: Database
    ) -> None:
        entity_id = await repo.upsert_entity("concept", "Widget")
        important = await _seed(repo, entity_id, "is", "shared-z", importance=0.9)
        ids = [await _seed(repo, entity_id, "is", f"shared-{index}") for index in range(3)]
        await _pin_ids(
            db,
            [("id-z", important), ("id-c", ids[0]), ("id-b", ids[1]), ("id-a", ids[2])],
        )

        first = await builder._hot_facts(["shared"], limit=2)
        second = await builder._hot_facts(["shared"], limit=2)

        assert [element.id for element in first] == ["id-z", "id-a"]
        assert [element.id for element in second] == [element.id for element in first]


class _LeakyGatherer(BoundedContextGatherer):
    """A gatherer that lets a narrative element into the worker packet.

    The real gatherer drops every narrative domain before ranking, so this
    cannot happen on the happy path — which is exactly why the builder needs
    a check of its own. The check is the last line of defence, and a last
    line of defence that is never exercised is one that has never been
    tested.
    """

    def compile_worker_packet(
        self,
        hot_facts: Iterable[ContextElement],
        semantic_matches: Iterable[ContextElement],
        required_slots: Sequence[str],
        *,
        token_ceiling: int | None = None,
    ) -> ContextPacket:
        return ContextPacket(
            elements=(
                # A legitimate fact first, and the leaked one second: a check
                # that only looked at the head of the packet would pass this
                # by, and the whole point is that *every* element is checked.
                ContextElement(
                    id="factual-1",
                    content="the report lives at reports/q3.md",
                    memory_domain="operational",
                ),
                ContextElement(
                    id="narrative-1",
                    content="what the user said about the plan last Tuesday",
                    memory_domain="narrative",
                ),
            ),
            density=1.0,
            allocated_tokens=16,
            routing_directive="PROCEED_TO_PLANNER",
            pollution_ratio=0.0,
            utility_score=0.5,
            entropy=0.0,
            token_ceiling=1000,
        )


def _worker_msg() -> AgentMessage:
    return AgentMessage(
        task_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        sender="planner",
        recipient="context_builder_worker",
        intent=MessageType.CONTEXT_REQUEST,
        payload={"step": {"action": "write the report"}},
    )


def _worker_ctx() -> AgentContext:
    return AgentContext(
        correlation_id=uuid.uuid4(),
        modality=ComplexityModality.STANDARD,
        permission_mode=PermissionMode.AUTO,
    )


class TestTheWorkerPacketCarriesNoNarrative:
    """RFC §2.2's planner/worker split is a security boundary, not a size
    optimisation: the worker runs semi-trusted code, and the planner's
    long-horizon thinking must never reach it.

    The builder therefore checks the *compiled packet* — the artefact the
    worker would actually receive — rather than trusting the filter that fed
    it, and it refuses rather than shipping a packet that carries narrative.
    """

    async def test_a_packet_carrying_narrative_memory_is_refused(self) -> None:
        """The packet never reaches the worker: the task fails instead."""
        builder = WorkerContextBuilder(gatherer=_LeakyGatherer())

        result = await builder.run(_worker_msg(), _worker_ctx())

        assert not result.ok
        assert "narrative memory leaked" in str(result.error)

    async def test_the_refusal_is_an_explicit_raise_not_an_assert(self) -> None:
        """``python -O`` strips asserts, so the type of the refusal is part of
        the contract.

        An ``assert``-based check disappears when the interpreter is
        optimised, and with it the only guarantee that keeps narrative out of
        a sandbox. A raised ``ValueError`` is enforced either way, so the
        exception *type* is not a detail: it is what makes the check survive
        ``-O``.
        """
        builder = WorkerContextBuilder(gatherer=_LeakyGatherer())

        with pytest.raises(ValueError, match="narrative memory leaked"):
            await builder.handle(_worker_msg(), _worker_ctx())

    async def test_a_clean_worker_packet_is_delivered(self) -> None:
        """The refusal must not be the only thing the builder does.

        A check that refused everything would satisfy both tests above, so
        the ordinary path is pinned too: a packet with no narrative in it is
        compiled, returned, and reported with its token accounting.
        """
        builder = WorkerContextBuilder()

        result = await builder.run(_worker_msg(), _worker_ctx())

        assert result.ok
        assert result.value is not None
        assert "elements" in result.value
