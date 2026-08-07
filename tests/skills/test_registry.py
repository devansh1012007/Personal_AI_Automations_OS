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
