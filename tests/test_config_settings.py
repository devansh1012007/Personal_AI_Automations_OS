"""Settings surface for the integration-fix flags (PLAN:integration-fix:P14).

Every flag the plan names in §E/§K is declared once, on ``Settings``, as a
pydantic sub-model with the plan default — no hardcoded default elsewhere.
This file pins three things per flag:

* the default equals the plan rollout value (§K / §E);
* the ``PAA_<SUB>__<FIELD>`` environment override round-trips (nested
  delimiter ``__``; the coding specialist is nested one level deeper so
  the pilot and the gated specialists stay symmetric);
* an invalid value fails validation at load time instead of degrading
  silently at use.

It also pins the T13 edge set: ``EDGE_TYPES`` must equal the ADR-0043
table exactly, so adding or removing a graph edge without updating the
ADR fails the build.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from paa.config import (
    AllocatorSettings,
    CodingSettings,
    CostGateSettings,
    ReflectionSettings,
    Settings,
    SpecialistsSettings,
    TierSettings,
    VaultSyncSettings,
    get_settings,
    reset_settings_cache,
)
from paa.storage.graph.base import EDGE_TYPES

# The ADR-0043 table: RFC §7.3's eleven plus MUTATES (RFC §1.3) = 12.
# Must stay byte-identical to docs/adr/0043-graph-edge-count-reconciliation.md
# and to src/paa/storage/graph/base.py EDGE_TYPES.
EXPECTED_EDGE_TYPES = frozenset(
    {
        "DEPENDS_ON",
        "PART_OF",
        "DERIVED_FROM",
        "INVOLVES",
        "BLOCKS",
        "CAUSES",
        "SUPPORTS",
        "CONTRADICTS",
        "REFERS_TO",
        "TRIGGERED_BY",
        "SIMILAR_TO",
        "MUTATES",
    }
)


class TestP014Defaults:
    """Each default equals the plan's §E/§K rollout value."""

    def test_allocator_defaults(self, paa_home: Path) -> None:
        s = Settings()
        assert s.allocator.enabled is False
        assert s.allocator.max_results == 5

    def test_tier_defaults(self, paa_home: Path) -> None:
        assert Settings().tier.dry_run is True

    def test_cost_gate_defaults(self, paa_home: Path) -> None:
        s = Settings()
        assert s.cost_gate.enabled is False
        assert s.cost_gate.threshold == 1.0

    def test_vault_sync_defaults(self, paa_home: Path) -> None:
        assert Settings().vault_sync.enabled is False

    def test_specialists_coding_defaults_nested(self, paa_home: Path) -> None:
        s = Settings()
        assert isinstance(s.specialists, SpecialistsSettings)
        assert isinstance(s.specialists.coding, CodingSettings)
        assert s.specialists.coding.enabled is False

    def test_reflection_defaults(self, paa_home: Path) -> None:
        # Plan P13 names the flag but no default; the conservative opt-in
        # value (False) is the documented choice (ADR/ReflectionSettings).
        assert Settings().reflection.enabled is False

    def test_drain_default(self, paa_home: Path) -> None:
        # Plan P07 names the Setting but no default; 100 is the current
        # drain batch size (ADR-0036), so unset == today's throughput.
        assert Settings().memory.drain_max_per_second == 100

    def test_submodel_types(self, paa_home: Path) -> None:
        s = Settings()
        assert isinstance(s.allocator, AllocatorSettings)
        assert isinstance(s.tier, TierSettings)
        assert isinstance(s.cost_gate, CostGateSettings)
        assert isinstance(s.vault_sync, VaultSyncSettings)
        assert isinstance(s.reflection, ReflectionSettings)

    def test_new_names_in_all(self) -> None:
        import paa.config as config

        for name in (
            "AllocatorSettings",
            "CodingSettings",
            "CostGateSettings",
            "ReflectionSettings",
            "SpecialistsSettings",
            "TierSettings",
            "VaultSyncSettings",
        ):
            assert name in config.__all__


class TestP014EnvOverrides:
    """``PAA_<SUB>__<FIELD>`` round-trips for every new flag."""

    @pytest.mark.parametrize(
        ("env_name", "raw_value", "expected"),
        [
            # Each raw value is the opposite of the flag's default, so a
            # pass proves the override was read (a default that already
            # matched would pass without reading anything).
            ("PAA_ALLOCATOR__ENABLED", "true", True),
            ("PAA_TIER__DRY_RUN", "false", False),
            ("PAA_COST_GATE__ENABLED", "true", True),
            ("PAA_VAULT_SYNC__ENABLED", "true", True),
            ("PAA_SPECIALISTS__CODING__ENABLED", "true", True),
            ("PAA_REFLECTION__ENABLED", "true", True),
        ],
    )
    def test_boolean_overrides(
        self,
        paa_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        env_name: str,
        raw_value: str,
        expected: bool,
    ) -> None:
        monkeypatch.setenv(env_name, raw_value)
        reset_settings_cache()
        try:
            s = Settings()
        finally:
            reset_settings_cache()
        actual = {
            "PAA_ALLOCATOR__ENABLED": s.allocator.enabled,
            "PAA_TIER__DRY_RUN": s.tier.dry_run,
            "PAA_COST_GATE__ENABLED": s.cost_gate.enabled,
            "PAA_VAULT_SYNC__ENABLED": s.vault_sync.enabled,
            "PAA_SPECIALISTS__CODING__ENABLED": s.specialists.coding.enabled,
            "PAA_REFLECTION__ENABLED": s.reflection.enabled,
        }[env_name]
        assert actual is expected

    def test_allocator_max_results_override(
        self, paa_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PAA_ALLOCATOR__MAX_RESULTS", "3")
        reset_settings_cache()
        try:
            assert Settings().allocator.max_results == 3
        finally:
            reset_settings_cache()

    def test_cost_gate_threshold_override(
        self, paa_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PAA_COST_GATE__THRESHOLD", "0.5")
        reset_settings_cache()
        try:
            assert Settings().cost_gate.threshold == 0.5
        finally:
            reset_settings_cache()

    def test_drain_max_per_second_override(
        self, paa_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PAA_MEMORY__DRAIN_MAX_PER_SECOND", "20")
        reset_settings_cache()
        try:
            assert Settings().memory.drain_max_per_second == 20
        finally:
            reset_settings_cache()

    def test_get_settings_picks_up_env_after_cache_reset(
        self, paa_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # get_settings() is lru_cached: mutation without a reset stays stale.
        assert get_settings().tier.dry_run is True
        monkeypatch.setenv("PAA_TIER__DRY_RUN", "false")
        assert get_settings().tier.dry_run is True
        reset_settings_cache()
        try:
            assert get_settings().tier.dry_run is False
        finally:
            reset_settings_cache()


class TestP014InvalidValues:
    """Hostile values fail validation at load, not silently at use."""

    @pytest.mark.parametrize(
        ("env_name", "bad_value"),
        [
            ("PAA_ALLOCATOR__MAX_RESULTS", "0"),
            ("PAA_ALLOCATOR__MAX_RESULTS", "-1"),
            ("PAA_ALLOCATOR__MAX_RESULTS", "not-a-number"),
            ("PAA_COST_GATE__THRESHOLD", "0"),
            ("PAA_COST_GATE__THRESHOLD", "-0.5"),
            ("PAA_COST_GATE__THRESHOLD", "not-a-number"),
            ("PAA_MEMORY__DRAIN_MAX_PER_SECOND", "0"),
            ("PAA_MEMORY__DRAIN_MAX_PER_SECOND", "-5"),
            ("PAA_ALLOCATOR__ENABLED", "not-a-bool"),
        ],
    )
    def test_invalid_value_raises(
        self,
        paa_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        env_name: str,
        bad_value: str,
    ) -> None:
        monkeypatch.setenv(env_name, bad_value)
        reset_settings_cache()
        try:
            with pytest.raises(ValidationError):
                Settings()
        finally:
            reset_settings_cache()


class TestT13EdgeTypes:
    """T13: EDGE_TYPES equals the ADR-0043 table, exactly."""

    def test_edge_types_equal_adr0043_table(self) -> None:
        assert EDGE_TYPES == EXPECTED_EDGE_TYPES

    def test_edge_types_count_is_twelve(self) -> None:
        # 11 RFC §7.3 + MUTATES (RFC §1.3). The plan's "9 extra edges" is a
        # documented phantom (ADR-0043) — neither 11 nor 12.
        assert len(EDGE_TYPES) == 12

    def test_mutates_is_the_non_rfc73_member(self) -> None:
        assert "MUTATES" in EDGE_TYPES
        assert EXPECTED_EDGE_TYPES - {"MUTATES"} == {
            "DEPENDS_ON",
            "PART_OF",
            "DERIVED_FROM",
            "INVOLVES",
            "BLOCKS",
            "CAUSES",
            "SUPPORTS",
            "CONTRADICTS",
            "REFERS_TO",
            "TRIGGERED_BY",
            "SIMILAR_TO",
        }
