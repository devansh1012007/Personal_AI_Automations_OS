"""P-010 dual vault (ADR-0038, PLAN:integration-fix:P010) — T11.

The managed set is the 15-file union (13 RFC §9 + 2 live-only) plus two
governance-only files under ``vault/governance/``. The fence raises a
named-path error when a human edit lands *inside* the managed block since
the last atomic write; edits outside the block merge-preserve as before.
Reflection writes ``playbooks.md`` through ``WorldModel`` only (single
fence implementation). All vaults are ``tmp_path`` — never the real vault.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from paa.config import Settings
from paa.ledger.store import LedgerStore
from paa.memory import world_model as _wm_module
from paa.memory.world_model import BeliefDocument, VaultConflictError, WorldModel

_IPaa = _wm_module._HASHES_FILENAME

#: ADR-0038: 13 RFC §9 + strategic_risks + active_constraints (live-only).
EXPECTED_MANAGED = {
    "current_focus.md",
    "strategic_risks.md",
    "active_constraints.md",
    "operating_themes.md",
    "constitution.md",
    "principles.md",
    "goals.md",
    "anti_goals.md",
    "projects.md",
    "skills.md",
    "playbooks.md",
    "reflections.md",
    "risk_rules.md",
    "agent_hierarchy.md",
    "prompt_contracts.md",
}

ORIGINAL_FOUR = {
    "current_focus.md",
    "strategic_risks.md",
    "active_constraints.md",
    "operating_themes.md",
}

GOVERNANCE = {"identity.md", "strategy.md"}


@pytest.fixture
def world(tmp_path: Path) -> WorldModel:
    return WorldModel(tmp_path / "vault")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir(parents=True, exist_ok=True)
    return v


def _vault_of(world: WorldModel) -> Path:
    """The vault dir via the public path_for (managed docs live flat in it)."""
    return world.path_for(BeliefDocument.CURRENT_FOCUS).parent


def _seed_block(path: Path, body: str, *, section: str = "state") -> None:
    """Operator-style pre-existing file: a fence block, no write record."""
    path.write_text(
        f"# Title\n\n<!-- paa:managed:BEGIN {section} -->\n{body}\n"
        f"<!-- paa:managed:END {section} -->\n",
        encoding="utf-8",
    )


class TestEnumSet:
    def test_len_is_15(self) -> None:
        assert len(BeliefDocument) == 15

    def test_values_are_the_15_file_union(self) -> None:
        assert {d.value for d in BeliefDocument} == EXPECTED_MANAGED

    def test_original_four_preserved_no_removals(self) -> None:
        assert {d.value for d in BeliefDocument} >= ORIGINAL_FOUR

    def test_cli_coercion_accepts_new_names(self) -> None:
        for name in EXPECTED_MANAGED - ORIGINAL_FOUR:
            assert BeliefDocument(name).value == name  # closed set, additive

    def test_cli_coercion_rejects_unknown_and_traversal(self) -> None:
        for bad in ("unknown.md", "../constitution.md", "a/b.md", "", "/etc/passwd"):
            with pytest.raises(ValueError):
                BeliefDocument(bad)

    def test_governance_files_are_not_enum_members(self) -> None:
        values = {d.value for d in BeliefDocument}
        assert "identity.md" not in values
        assert "strategy.md" not in values


class TestGovernancePath:
    def test_governance_files_resolve_under_subdir(self, world: WorldModel) -> None:
        vault = _vault_of(world)
        assert world.governance_path("identity.md") == vault / "governance" / "identity.md"
        assert world.governance_path("strategy.md") == vault / "governance" / "strategy.md"

    def test_governance_rejects_unknown_and_traversal(self, world: WorldModel) -> None:
        for bad in ("foo.md", "../identity.md", "governance/identity.md", "identity.md/x", ""):
            with pytest.raises(ValueError):
                world.governance_path(bad)

    def test_managed_paths_stay_flat(self, world: WorldModel) -> None:
        for doc in BeliefDocument:
            assert world.path_for(doc) == _vault_of(world) / doc.value
            assert "governance" not in str(world.path_for(doc))


class TestMaterialization:
    def test_all_15_materialize_via_write(self, world: WorldModel) -> None:
        for doc in BeliefDocument:
            world.update_belief_state(doc, f"seed for {doc.value}")
        for name in EXPECTED_MANAGED:
            assert (_vault_of(world) / name).is_file(), name
        assert world.read_managed(BeliefDocument.CONSTITUTION) == "seed for constitution.md"

    def test_governance_files_operator_placed_and_readable(self, world: WorldModel) -> None:
        """Seed decision (ADR-0038 silent): the operator places governance
        files; WorldModel resolves their paths but never seeds them."""
        gov = _vault_of(world) / "governance"
        gov.mkdir(parents=True, exist_ok=True)
        (gov / "identity.md").write_text("operator identity\n", encoding="utf-8")
        (gov / "strategy.md").write_text("operator strategy\n", encoding="utf-8")
        assert world.governance_path("identity.md").read_text(encoding="utf-8") == (
            "operator identity\n"
        )
        assert world.governance_path("strategy.md").read_text(encoding="utf-8") == (
            "operator strategy\n"
        )

    def test_fresh_worldmodel_does_not_seed_files(self, vault: Path) -> None:
        """Construction must not spray 17 files into a fresh vault: seeding
        an anti_goals.md header here would feed header prose to the line
        reader (runtime.py::_load_anti_goals)."""
        WorldModel(vault)
        assert [p.name for p in vault.iterdir() if p.suffix == ".md"] == []


class TestFenceConflict:
    def test_edit_inside_block_raises_named_path_error(self, world: WorldModel) -> None:
        doc = BeliefDocument.CURRENT_FOCUS
        world.update_belief_state(doc, "system-v1")
        path = world.path_for(doc)
        edited = path.read_text(encoding="utf-8").replace("system-v1", "HUMAN-EDIT")
        path.write_text(edited, encoding="utf-8")
        with pytest.raises(ValueError) as excinfo:
            world.update_belief_state(doc, "system-v2")
        assert isinstance(excinfo.value, VaultConflictError)
        assert "current_focus.md" in str(excinfo.value)
        # Never silent overwrite: the human edit stands, v2 never landed.
        final = path.read_text(encoding="utf-8")
        assert "HUMAN-EDIT" in final
        assert "system-v2" not in final

    def test_edit_outside_block_merges_and_preserves(self, world: WorldModel) -> None:
        doc = BeliefDocument.OPERATING_THEMES
        world.update_belief_state(doc, "system-v1")
        path = world.path_for(doc)
        path.write_text(
            path.read_text(encoding="utf-8") + "\n## My notes\noutside-edit-xyz\n",
            encoding="utf-8",
        )
        world.update_belief_state(doc, "system-v2")
        final = path.read_text(encoding="utf-8")
        assert "outside-edit-xyz" in final
        assert world.read_managed(doc) == "system-v2"

    def test_first_write_adopts_existing_block_then_detects(self, world: WorldModel) -> None:
        """Upgrade path: a block that predates tracking is adopted once
        (no last-write record exists), then conflicts are detected."""
        doc = BeliefDocument.CURRENT_FOCUS
        _seed_block(world.path_for(doc), "seeded-by-human")
        world.update_belief_state(doc, "system-now")  # adopt, no conflict
        assert world.read_managed(doc) == "system-now"
        path = world.path_for(doc)
        path.write_text(
            path.read_text(encoding="utf-8").replace("system-now", "HUMAN"),
            encoding="utf-8",
        )
        with pytest.raises(VaultConflictError):
            world.update_belief_state(doc, "system-next")

    def test_corrupt_write_record_is_rebuilt_not_bricked(self, world: WorldModel) -> None:
        doc = BeliefDocument.CURRENT_FOCUS
        world.update_belief_state(doc, "v1")
        (_vault_of(world) / _IPaa).write_text("not-json{{{", encoding="utf-8")
        world.update_belief_state(doc, "v2")  # corrupt bookkeeping must not brick writes
        assert world.read_managed(doc) == "v2"
        json.loads((_vault_of(world) / _IPaa).read_text(encoding="utf-8"))  # valid again


class TestIdempotencyAndCrash:
    def test_idempotent_double_write_is_byte_identical(self, world: WorldModel) -> None:
        doc = BeliefDocument.GOALS
        world.update_belief_state(doc, "same content")
        first = world.path_for(doc).read_bytes()
        first_hashes = (_vault_of(world) / _IPaa).read_bytes()
        world.update_belief_state(doc, "same content")
        assert world.path_for(doc).read_bytes() == first
        assert (_vault_of(world) / _IPaa).read_bytes() == first_hashes

    def test_crash_mid_write_leaves_old_intact_and_no_temp(
        self, world: WorldModel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        doc = BeliefDocument.CURRENT_FOCUS
        world.update_belief_state(doc, "before-crash")
        path = world.path_for(doc)
        before = path.read_bytes()

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated crash during os.replace")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            world.update_belief_state(doc, "after-crash")
        monkeypatch.undo()
        assert path.read_bytes() == before
        assert list(_vault_of(world).glob(".*.tmp")) == []
        # Bookkeeping consistent: the next write proceeds (no false conflict).
        world.update_belief_state(doc, "recovered")
        assert world.read_managed(doc) == "recovered"


class TestMarkerInjection:
    def test_duplicate_begin_markers_rejected(self, world: WorldModel) -> None:
        doc = BeliefDocument.CURRENT_FOCUS
        world.update_belief_state(doc, "v1")
        path = world.path_for(doc)
        path.write_text(
            path.read_text(encoding="utf-8") + "\n<!-- paa:managed:BEGIN state -->\nfake\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            world.update_belief_state(doc, "v2")
        assert "fake" in path.read_text(encoding="utf-8")  # unchanged, not rewritten

    def test_begin_without_end_rejected(self, world: WorldModel) -> None:
        doc = BeliefDocument.CURRENT_FOCUS
        path = world.path_for(doc)
        path.write_text("# Title\n\n<!-- paa:managed:BEGIN state -->\n", encoding="utf-8")
        with pytest.raises(ValueError):
            world.update_belief_state(doc, "v1")

    def test_content_containing_markers_rejected(self, world: WorldModel) -> None:
        doc = BeliefDocument.CURRENT_FOCUS
        with pytest.raises(ValueError):
            world.update_belief_state(doc, "evil <!-- paa:managed:BEGIN state --> nested")
        assert not world.path_for(doc).exists()

    def test_marker_spelling_pinned_once(self, world: WorldModel) -> None:
        doc = BeliefDocument.CURRENT_FOCUS
        world.update_belief_state(doc, "body")
        text = world.path_for(doc).read_text(encoding="utf-8")
        assert text.count("<!-- paa:managed:BEGIN state -->") == 1
        assert text.count("<!-- paa:managed:END state -->") == 1


class TestSourceHeader:
    def test_source_written_into_header(self, world: WorldModel) -> None:
        doc = BeliefDocument.CONSTITUTION
        world.update_belief_state(doc, "body", source="probe-source")
        assert "probe-source" in world.path_for(doc).read_text(encoding="utf-8")

    def test_default_source_written_into_header(self, world: WorldModel) -> None:
        doc = BeliefDocument.PRINCIPLES
        world.update_belief_state(doc, "body")
        assert "memory_curator" in world.path_for(doc).read_text(encoding="utf-8")


class TestSyncGate:
    def test_sync_disabled_leaves_files_intact(self, vault: Path) -> None:
        off = WorldModel(vault, sync_enabled=False)
        off.update_belief_state(BeliefDocument.CURRENT_FOCUS, "must not land")
        assert [p.name for p in vault.iterdir()] == []  # no file, no write record

    def test_sync_disabled_preserves_existing_bytes(self, vault: Path) -> None:
        on = WorldModel(vault)
        on.update_belief_state(BeliefDocument.CURRENT_FOCUS, "v1")
        before = vault.joinpath("current_focus.md").read_bytes()
        WorldModel(vault, sync_enabled=False).update_belief_state(
            BeliefDocument.CURRENT_FOCUS, "v2"
        )
        assert vault.joinpath("current_focus.md").read_bytes() == before

    def test_sync_enabled_writes_normally(self, vault: Path) -> None:
        wm = WorldModel(vault, sync_enabled=True)
        wm.update_belief_state(BeliefDocument.CURRENT_FOCUS, "v1")
        assert wm.read_managed(BeliefDocument.CURRENT_FOCUS) == "v1"


class TestSingleWriter:
    SRC = Path(__file__).resolve().parents[2] / "src" / "paa"

    def test_no_duplicate_fence_writer_in_src(self) -> None:
        offenders = [
            str(p)
            for p in self.SRC.rglob("*.py")
            if "def _write_managed_block" in p.read_text(encoding="utf-8")
        ]
        assert offenders == []

    def test_reflection_module_has_no_fence_writer(self) -> None:
        text = (self.SRC / "improvement" / "reflection.py").read_text(encoding="utf-8")
        assert "_write_managed_block" not in text


class TestReflectionDelegation:
    async def test_apply_to_playbook_writes_through_world_model(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        from paa.improvement.reflection import ReflectionEngine

        engine = ReflectionEngine(ledger, vault_path=tmp_path)
        engine.apply_to_playbook("rule one")
        engine.apply_to_playbook("rule two")
        playbook = WorldModel(tmp_path).read_managed(BeliefDocument.PLAYBOOKS, "reflections")
        assert "rule one" in playbook and "rule two" in playbook

    async def test_apply_to_playbook_preserves_human_text(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        from paa.improvement.reflection import ReflectionEngine

        (tmp_path / "playbooks.md").write_text(
            "# Playbooks\n\nMy own note: always run pytest.\n", encoding="utf-8"
        )
        engine = ReflectionEngine(ledger, vault_path=tmp_path)
        engine.apply_to_playbook("rule one")
        final = (tmp_path / "playbooks.md").read_text(encoding="utf-8")
        assert "always run pytest" in final
        assert "rule one" in final

    async def test_delegation_inherits_conflict_semantics(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        from paa.improvement.reflection import ReflectionEngine

        engine = ReflectionEngine(ledger, vault_path=tmp_path)
        engine.apply_to_playbook("rule one")
        path = tmp_path / "playbooks.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("rule one", "HUMAN-INSIDE"),
            encoding="utf-8",
        )
        with pytest.raises(ValueError) as excinfo:
            engine.apply_to_playbook("rule two")
        assert "playbooks.md" in str(excinfo.value)

    async def test_apply_to_playbook_rejects_non_playbook_filename(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        from paa.improvement.reflection import ReflectionEngine

        engine = ReflectionEngine(ledger, vault_path=tmp_path)
        for bad in ("other.md", "../escape.md"):
            with pytest.raises(ValueError):
                engine.apply_to_playbook("rule", filename=bad)
        assert [p.name for p in tmp_path.glob("*.md")] == []


class TestAntiGoalsAgreement:
    """Verify-only (runtime.py is outside the write-set): how the fence
    interacts with the plain line reader. Disagreement is pinned, not fixed.
    """

    def test_human_only_file_agrees_with_line_reader(self, settings: Settings) -> None:
        from paa.runtime import _load_anti_goals

        vault_path = settings.storage.vault_path
        vault_path.mkdir(parents=True, exist_ok=True)
        (vault_path / "anti_goals.md").write_text(
            "- never deploy on friday\n- never skip backups\n", encoding="utf-8"
        )
        assert _load_anti_goals(settings) == [
            "never deploy on friday",
            "never skip backups",
        ]

    def test_managed_block_injection_pinned_as_finding(self, settings: Settings) -> None:
        from paa.runtime import _load_anti_goals

        vault_path = settings.storage.vault_path
        WorldModel(vault_path).update_belief_state(
            BeliefDocument.ANTI_GOALS, "never deploy on friday"
        )
        goals = _load_anti_goals(settings)
        # FINDING: the fence-unaware line reader ingests marker + header
        # prose as anti-goals once the block is governed. Pinned here so a
        # fence-aware fix must update this test deliberately.
        assert "<!-- paa:managed:BEGIN state -->" in goals
