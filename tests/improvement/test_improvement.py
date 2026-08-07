"""Self-improvement: optimizer, reflection (with the div-by-zero fix), meta, distillation."""

from __future__ import annotations

import uuid
from datetime import timedelta

import numpy as np
import pytest

from paa.improvement.distillation import SkillDistiller, generalize_arguments
from paa.improvement.meta import PrototypicalClassifier
from paa.improvement.optimizer import (
    RunMetrics,
    WeightOptimizer,
    optimize_tool_ranking_weights,
    performance_score,
    update_weight,
)
from paa.improvement.reflection import operational_friction
from paa.storage.relational.database import Database, to_iso, utc_now


class TestPerformanceScore:
    def test_perfect_run_scores_one(self) -> None:
        assert performance_score(succeeded=True, user_corrected=False, latency_seconds=0.0) == 1.0

    def test_worst_run_scores_low(self) -> None:
        s = performance_score(succeeded=False, user_corrected=True, latency_seconds=60.0)
        assert s == 0.0

    def test_correction_lowers_score(self) -> None:
        clean = performance_score(succeeded=True, user_corrected=False, latency_seconds=1.0)
        corrected = performance_score(succeeded=True, user_corrected=True, latency_seconds=1.0)
        assert corrected < clean


class TestWeightUpdate:
    def test_ewma_moves_toward_score(self) -> None:
        # A run scoring 1.0 should raise a 0.5 weight.
        assert update_weight(0.5, 1.0, alpha=0.5) == 0.75

    def test_weight_never_hits_zero(self) -> None:
        w = 0.5
        for _ in range(100):
            w = update_weight(w, 0.0, alpha=0.5)  # relentless failure
        assert w >= 0.01, "a weight of 0 makes a skill permanently unrankable"

    def test_weight_capped_at_one(self) -> None:
        w = 0.9
        for _ in range(100):
            w = update_weight(w, 1.0, alpha=0.5)
        assert w <= 1.0

    def test_corrected_run_yields_lower_weight_than_clean(self) -> None:
        clean = optimize_tool_ranking_weights(
            [RunMetrics("s", succeeded=True, user_corrected=False, latency_seconds=1.0)]
        )
        dirty = optimize_tool_ranking_weights(
            [RunMetrics("s", succeeded=True, user_corrected=True, latency_seconds=1.0)]
        )
        assert dirty["s"] < clean["s"]

    def test_invalid_alpha_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\[0,1\]"):
            update_weight(0.5, 1.0, alpha=2.0)


class TestWeightOptimizerPersistence:
    async def test_optimize_from_history_persists(self, db: Database) -> None:
        now = to_iso(utc_now())
        for exit_code in (0, 0, 1):
            await db.execute(
                "INSERT INTO hot_serving_execution_runs "
                "(trace_id, correlation_id, agent_role, modality, permission_mode, "
                " started_at, exit_code, duration_ms, skill_name) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), str(uuid.uuid4()), "worker", "COMPLEX", "AUTO",
                 now, exit_code, 1200, "google_search"),
            )
        weights = await WeightOptimizer(db).optimize_from_history()
        assert "google_search" in weights
        stored = await db.fetch_value(
            "SELECT reliability FROM improvement_skill_weights WHERE skill_name = ?",
            ("google_search",),
        )
        assert stored is not None


class TestOperationalFriction:
    def test_known_value(self) -> None:
        # 2 corrections * 1.5 + 1 rollback * 3.0 = 6.0 over 3 successes = 2.0
        assert operational_friction(2, 1, 3) == 2.0

    def test_zero_successes_does_not_divide_by_zero(self) -> None:
        """The fix (ADR-0016): a domain that only ever failed must score high,
        not crash or vanish."""
        score = operational_friction(4, 2, 0)  # 4*1.5 + 2*3.0 = 12.0 / max(0,1) = 12.0
        assert score == 12.0

    def test_clean_domain_scores_zero(self) -> None:
        assert operational_friction(0, 0, 10) == 0.0

    def test_negative_counts_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            operational_friction(-1, 0, 1)


class TestReflectionEngine:
    async def test_high_friction_domain_flagged_and_playbook_written(
        self, ledger, tmp_path
    ) -> None:
        from paa.core.types import EventType, new_correlation_id
        from paa.improvement.reflection import ReflectionEngine
        from paa.ledger.events import LedgerEvent

        cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(cid, EventType.TASK_REQUESTED, payload={"request": {"goal": "deploy the service"}})
        )
        await ledger.append(LedgerEvent.create(cid, EventType.USER_CORRECTION))
        await ledger.append(LedgerEvent.create(cid, EventType.STATE_ROLLBACK_TRIGGERED, discriminator="r1"))

        engine = ReflectionEngine(ledger, vault_path=tmp_path)
        report = await engine.run_weekly()

        assert report.high_friction_domains
        assert report.rules_written
        playbook = (tmp_path / "playbooks.md").read_text(encoding="utf-8")
        assert "Anti-pattern" in playbook

    async def test_playbook_preserves_human_text(self, ledger, tmp_path) -> None:
        from paa.core.types import EventType, new_correlation_id
        from paa.improvement.reflection import ReflectionEngine
        from paa.ledger.events import LedgerEvent

        (tmp_path / "playbooks.md").write_text(
            "# Playbooks\n\nMy own note: always run pytest.\n", encoding="utf-8"
        )
        cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(cid, EventType.TASK_REQUESTED, payload={"request": {"goal": "docker deploy"}})
        )
        for i in range(3):
            await ledger.append(LedgerEvent.create(cid, EventType.USER_CORRECTION, discriminator=f"c{i}"))

        await ReflectionEngine(ledger, vault_path=tmp_path).run_weekly()
        final = (tmp_path / "playbooks.md").read_text(encoding="utf-8")
        assert "always run pytest" in final, "human text was clobbered"


class TestPrototypicalClassifier:
    def _emb(self, seed: float, dim: int = 16) -> np.ndarray:
        rng = np.random.default_rng(int(seed * 1000))
        return rng.standard_normal(dim).astype(np.float32)

    def test_classifies_held_out_exemplar(self) -> None:
        # Two well-separated clusters.
        base_a = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        base_b = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        clf = PrototypicalClassifier()
        clf.fit(
            [("a", base_a + 0.01), ("a", base_a - 0.01), ("b", base_b + 0.01), ("b", base_b - 0.01)]
        )
        assert clf.classify(base_a + 0.02).label == "a"
        assert clf.classify(base_b - 0.02).label == "b"

    def test_abstains_when_nothing_is_close(self) -> None:
        clf = PrototypicalClassifier(min_confidence=0.9)
        clf.fit([("a", np.array([1.0, 0.0], dtype=np.float32))])
        # Orthogonal query -> low similarity -> unknown, not a wrong guess.
        result = clf.classify(np.array([0.0, 1.0], dtype=np.float32))
        assert result.label == "unknown"

    def test_add_exemplar_shifts_prototype(self) -> None:
        clf = PrototypicalClassifier()
        clf.add_exemplar("a", np.array([1.0, 0.0], dtype=np.float32))
        clf.add_exemplar("a", np.array([0.0, 1.0], dtype=np.float32))
        # Prototype is now the mean direction; a diagonal query classifies as a.
        assert clf.classify(np.array([1.0, 1.0], dtype=np.float32)).label == "a"

    def test_empty_classifier_returns_unknown(self) -> None:
        assert PrototypicalClassifier().classify(np.array([1.0], dtype=np.float32)).label == "unknown"


class TestDistillation:
    def test_generalize_strips_filenames_and_literals(self) -> None:
        g = generalize_arguments({"cmd": ["refactor", "src/auth.py"], "msg": "'hello world'"})
        assert "<FILE>" in g["cmd"][1]
        assert "<STR>" in g["msg"]

    def test_should_skip_simple_and_toolless(self, db: Database) -> None:
        d = SkillDistiller(db)
        assert d.should_distil(modality="SIMPLE", tool_call_count=5) is False
        assert d.should_distil(modality="COMPLEX", tool_call_count=0) is False
        assert d.should_distil(modality="COMPLEX", tool_call_count=3) is True

    async def test_recipe_registered_only_after_passing_smoke_test(self, db: Database) -> None:
        class PassTester:
            async def smoke_test(self, recipe):  # noqa: ANN001, ANN202
                return True

        class FailTester:
            async def smoke_test(self, recipe):  # noqa: ANN001, ANN202
                return False

        d_ok = SkillDistiller(db, sandbox_tester=PassTester())
        recipe = d_ok.distil(
            capability_class="CODE_REFACTORING",
            tool_sequence=[{"tool": "ast_parse"}, {"tool": "diff_compile"}],
        )
        assert await d_ok.verify_and_register(recipe) is True
        assert await db.fetch_value(
            "SELECT COUNT(*) FROM hot_serving_skill_registry WHERE skill_name = ?",
            (recipe.recipe_name,),
        ) == 1

        d_bad = SkillDistiller(db, sandbox_tester=FailTester())
        bad = d_bad.distil(capability_class="X", tool_sequence=[{"tool": "t"}])
        assert await d_bad.verify_and_register(bad) is False
        assert await db.fetch_value(
            "SELECT COUNT(*) FROM hot_serving_skill_registry WHERE skill_name = ?",
            (bad.recipe_name,),
        ) == 0
