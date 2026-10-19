"""SkillRegistry over the real hot_serving_skill_registry table."""

from __future__ import annotations

import pytest

from paa.core.types import Permission
from paa.skills.contracts import SkillContract, SkillInvocation
from paa.skills.registry import SkillRegistry
from paa.storage.relational.database import Database


def _contract(
    name: str = "deploy-pipeline",
    *,
    version: str = "1.0.0",
    provider: str = "native",
    description: str | None = None,
    reliability: float = 1.0,
    permissions: tuple[str, ...] = (),
) -> SkillContract:
    return SkillContract(
        skill_name=name,
        provider=provider,  # type: ignore[arg-type]
        version=version,
        description=description or f"A skill named {name} that does a useful thing well.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_profile=0.3,
        required_permissions=permissions,  # type: ignore[arg-type]
        reliability_weight=reliability,
        invocation=SkillInvocation(kind="native_callable", target=name),
    )


class TestRegister:
    async def test_register_then_get(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract())
        got = await registry.get("deploy-pipeline")
        assert got is not None
        assert got.skill_name == "deploy-pipeline"
        assert got.version == "1.0.0"

    async def test_register_is_idempotent_on_name_and_version(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract())
        # Push the reliability weight, then re-register the identical contract:
        # idempotent registration must NOT reset learned state.
        await registry.update_reliability("deploy-pipeline", -0.4)
        await registry.register(_contract())
        got = await registry.get("deploy-pipeline")
        assert got is not None
        assert got.reliability_weight == pytest.approx(0.6)
        rows = await db.fetch_all("SELECT id FROM hot_serving_skill_registry")
        assert len(rows) == 1

    async def test_register_new_version_upgrades_in_place(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract(version="1.0.0"))
        await registry.register(
            _contract(version="2.0.0", description="A much improved deploy pipeline skill.")
        )
        got = await registry.get("deploy-pipeline")
        assert got is not None
        assert got.version == "2.0.0"
        assert got.description == "A much improved deploy pipeline skill."
        rows = await db.fetch_all("SELECT id FROM hot_serving_skill_registry")
        assert len(rows) == 1  # UNIQUE on skill_name: one row, upgraded

    async def test_register_reactivates_a_deactivated_skill(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract())
        await registry.deactivate("deploy-pipeline")
        assert await registry.get("deploy-pipeline") is None
        await registry.register(_contract())
        assert await registry.get("deploy-pipeline") is not None

    async def test_permissions_round_trip(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(
            _contract(permissions=(Permission.SANDBOX_RUN.value, Permission.NET_EGRESS.value))
        )
        got = await registry.get("deploy-pipeline")
        assert got is not None
        assert set(got.required_permissions) == {Permission.SANDBOX_RUN, Permission.NET_EGRESS}


class TestGetAndList:
    async def test_get_absent_returns_none(self, db: Database) -> None:
        assert await SkillRegistry(db).get("nope") is None

    async def test_get_hides_inactive_by_default(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract())
        await registry.deactivate("deploy-pipeline")
        assert await registry.get("deploy-pipeline") is None
        assert await registry.get("deploy-pipeline", include_inactive=True) is not None

    async def test_list_active_excludes_deactivated(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract(name="alpha-skill"))
        await registry.register(_contract(name="beta-skill"))
        await registry.deactivate("alpha-skill")
        active = await registry.list_active()
        assert [c.skill_name for c in active] == ["beta-skill"]


class TestSearch:
    async def test_empty_intent_returns_nothing(self, db: Database) -> None:
        assert await SkillRegistry(db).search("   ") == []

    async def test_trigram_fallback_ranks_by_similarity(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(
            _contract(name="summarise-email", description="Summarise an email inbox into a digest.")
        )
        await registry.register(
            _contract(name="deploy-service", description="Deploy a service to production safely.")
        )
        results = await registry.search("summarise email")
        assert results
        assert results[0].skill_name == "summarise-email"

    async def test_trigram_fallback_finds_by_description_substring(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(
            _contract(name="widget-tool", description="Rotates a widget through the given angle.")
        )
        results = await registry.search("rotate")
        assert any(c.skill_name == "widget-tool" for c in results)

    async def test_search_excludes_inactive(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(
            _contract(name="summarise-email", description="Summarise an email inbox into a digest.")
        )
        await registry.deactivate("summarise-email")
        assert await registry.search("summarise email") == []

    async def test_semantic_search_uses_vector_store(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract(name="alpha-skill"))
        await registry.register(_contract(name="beta-skill"))

        class FakeStore:
            async def search(self, intent: str, *, limit: int) -> list[str]:
                return ["beta-skill", "ghost-skill"]  # ghost is a stale index entry

        results = await registry.search("anything", vector_store=FakeStore())
        # The stale name resolves to nothing and is silently dropped.
        assert [c.skill_name for c in results] == ["beta-skill"]


class TestReliabilityAndDeactivate:
    async def test_deactivate_returns_true_once(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract())
        assert await registry.deactivate("deploy-pipeline") is True
        assert await registry.deactivate("deploy-pipeline") is False

    async def test_update_reliability_clamps_to_unit_interval(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract(reliability=0.9))
        assert await registry.update_reliability("deploy-pipeline", 0.5) == pytest.approx(1.0)
        assert await registry.update_reliability("deploy-pipeline", -5.0) == pytest.approx(0.0)

    async def test_update_reliability_persists(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract(reliability=0.5))
        await registry.update_reliability("deploy-pipeline", 0.2)
        got = await registry.get("deploy-pipeline")
        assert got is not None
        assert got.reliability_weight == pytest.approx(0.7)

    async def test_update_reliability_absent_skill_does_not_raise(self, db: Database) -> None:
        assert await SkillRegistry(db).update_reliability("ghost", 0.1) == 0.0


class TestLiteralIntentSearch:
    """Discovery searches for the text the caller wrote, not for a pattern.

    Fallback recall is a ``LIKE`` prefilter followed by a trigram re-rank, and
    a ``LIKE`` pattern is the one place where a value silently changes the
    meaning of a query: ``%`` matches any run of characters and ``_`` matches
    any one of them. An intent is text, so an intent carrying one of those
    characters is that text and not a pattern.

    A wrong prefilter is worse here than a noisy answer, because the
    prefilter decides *which rows are ranked at all*. A wildcard always
    matches something, and a match suppresses the full trigram pass — so the
    skills the caller actually asked about, the ones whose names merely
    resemble the intent, were dropped rather than ranked. And a pattern that
    matches nothing at all widens into a scan of every active skill, which is
    the opposite of what a prefilter is for.

    Each test registers a decoy that the old wildcard behaviour swept in, so
    a regression cannot hide behind an assertion that only checks the happy
    path.
    """

    async def test_a_percent_in_an_intent_is_not_a_wildcard(self, db: Database) -> None:
        registry = SkillRegistry(db)
        for name in ("alpha", "beta", "gamma"):
            await registry.register(_contract(name=name))

        results = await registry.search("alpha%")

        # The literal intent matches nothing by substring, so the documented
        # fallback ranks the whole active set — rather than handing back the
        # subset the wildcard used to select.
        assert [c.skill_name for c in results] == ["alpha", "beta", "gamma"]

    async def test_an_underscore_in_an_intent_is_matched_literally(
        self, db: Database
    ) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract(name="alpha_beta"))
        await registry.register(_contract(name="alpha-beta"))

        results = await registry.search("alpha_beta")

        # "alpha-beta" is a different skill from "alpha_beta", and the intent
        # named the second one.
        assert [c.skill_name for c in results] == ["alpha_beta"]

    async def test_a_nul_byte_does_not_widen_the_prefilter_to_every_skill(
        self, db: Database
    ) -> None:
        registry = SkillRegistry(db)
        for name in ("alpha", "beta", "gamma"):
            await registry.register(_contract(name=name))

        results = await registry.search("\x00alpha")

        # SQLite reads a LIKE pattern through the C string API, so a NUL
        # *ends* it: the pattern degrades to a bare "%" and matches everything.
        assert [c.skill_name for c in results] == ["alpha"]

    async def test_a_misspelled_intent_is_not_silently_dropped(self, db: Database) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract(name="alpha-beta"))
        await registry.register(_contract(name="unrelated-tool"))

        results = await registry.search("alpha_beta")

        names = [c.skill_name for c in results]
        # The intent matches nothing by substring — the caller wrote an
        # underscore where the skill has a hyphen — and the full trigram pass
        # the docstring promises still runs, so the skill that was meant is
        # ranked rather than dropped, and the whole active set is ranked
        # rather than the subset a match would have selected.
        assert "alpha-beta" in names
        assert names == ["alpha-beta", "unrelated-tool"]

    async def test_an_intent_with_nothing_to_search_for_takes_the_fallback(
        self, db: Database
    ) -> None:
        """An intent that was nothing but NUL bytes has no pattern to build.

        A pattern of ``"%%"`` would match every active skill, and there is
        no LIKE pattern that matches nothing instead, so the prefilter is
        skipped and the documented fallback does the work: the whole active
        set, ranked by trigram similarity.
        """
        registry = SkillRegistry(db)
        for name in ("alpha", "beta", "gamma"):
            await registry.register(_contract(name=name))

        results = await registry.search("\x00")

        assert [c.skill_name for c in results] == ["alpha", "beta", "gamma"]


    async def test_a_skill_whose_name_contains_an_underscore_is_retrievable(
        self, db: Database
    ) -> None:
        registry = SkillRegistry(db)
        await registry.register(_contract(name="rotate_widget"))
        await registry.register(_contract(name="rotate-widget"))

        results = await registry.search("rotate_widget")

        # "rotate-widget" is the decoy the old wildcard behaviour swept in:
        # its underscore matched any one character.
        assert [c.skill_name for c in results] == ["rotate_widget"]

    async def test_a_skill_whose_description_contains_a_percent_is_retrievable(
        self, db: Database
    ) -> None:
        registry = SkillRegistry(db)
        await registry.register(
            _contract(name="scale-up", description="Scales the fleet to 100% capacity.")
        )
        await registry.register(
            _contract(name="other-tool", description="Handles 100 requests per second.")
        )

        results = await registry.search("100%")

        # "other-tool" is the decoy: the old pattern matched its "100" too.
        assert [c.skill_name for c in results] == ["scale-up"]
