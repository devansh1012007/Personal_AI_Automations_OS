"""The world model: marker-safe belief writes and episodic compression.

The load-bearing property is that the runtime rewrites only its own managed
block and never touches the human's text — markdown is the strategic human
interface (RFC §9), and silently overwriting the user's notes would be a serious
regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paa.memory.world_model import BeliefDocument, WorldModel


@pytest.fixture
def world(tmp_path: Path) -> WorldModel:
    return WorldModel(tmp_path / "vault")


class TestManagedWrites:
    def test_creates_document_with_managed_block(self, world: WorldModel) -> None:
        world.update_belief_state(BeliefDocument.CURRENT_FOCUS, "Shipping the API")
        assert world.read_managed(BeliefDocument.CURRENT_FOCUS) == "Shipping the API"

    def test_update_replaces_only_managed_content(self, world: WorldModel) -> None:
        world.update_belief_state(BeliefDocument.CURRENT_FOCUS, "first")
        world.update_belief_state(BeliefDocument.CURRENT_FOCUS, "second")
        assert world.read_managed(BeliefDocument.CURRENT_FOCUS) == "second"

    def test_human_text_outside_the_block_survives_updates(self, world: WorldModel) -> None:
        """The core guarantee: write human prose around the managed block, then
        let the system update the block, and the human prose must remain."""
        path = world.path_for(BeliefDocument.OPERATING_THEMES)
        world.update_belief_state(BeliefDocument.OPERATING_THEMES, "system view v1")

        # Human appends their own note below the managed block.
        text = path.read_text(encoding="utf-8")
        human_note = "\n## My own notes\nRemember: prefer boring technology.\n"
        path.write_text(text + human_note, encoding="utf-8")

        # System updates its block again.
        world.update_belief_state(BeliefDocument.OPERATING_THEMES, "system view v2")

        final = path.read_text(encoding="utf-8")
        assert "prefer boring technology" in final, "human text was clobbered"
        assert world.read_managed(BeliefDocument.OPERATING_THEMES) == "system view v2"

    def test_human_text_above_the_block_survives(self, world: WorldModel) -> None:
        path = world.path_for(BeliefDocument.STRATEGIC_RISKS)
        path.write_text("# Risks\n\nMy hand-written intro.\n", encoding="utf-8")
        world.update_belief_state(BeliefDocument.STRATEGIC_RISKS, "auto risk 1")

        final = path.read_text(encoding="utf-8")
        assert "My hand-written intro." in final
        assert world.read_managed(BeliefDocument.STRATEGIC_RISKS) == "auto risk 1"

    def test_write_is_atomic_no_temp_left_behind(self, world: WorldModel) -> None:
        world.update_belief_state(BeliefDocument.CURRENT_FOCUS, "x")
        leftovers = list((world.path_for(BeliefDocument.CURRENT_FOCUS)).parent.glob(".*.tmp"))
        assert leftovers == []

    def test_distinct_sections_coexist(self, world: WorldModel) -> None:
        world.update_belief_state(BeliefDocument.CURRENT_FOCUS, "focus", section="focus")
        world.update_belief_state(BeliefDocument.CURRENT_FOCUS, "milestones", section="milestones")
        assert world.read_managed(BeliefDocument.CURRENT_FOCUS, "focus") == "focus"
        assert world.read_managed(BeliefDocument.CURRENT_FOCUS, "milestones") == "milestones"

    def test_missing_document_reads_empty(self, world: WorldModel) -> None:
        assert world.read_managed(BeliefDocument.ACTIVE_CONSTRAINTS) == ""


class TestEpisodicCompression:
    def test_recurring_observations_become_a_pattern(self, world: WorldModel) -> None:
        obs = [
            "docker-compose task failed on env var",
            "docker-compose task failed on env var",
            "docker-compose task failed on env var",
            "wrote some docs",
        ]
        summaries = world.compress_episodes(obs, min_pattern_support=2)
        levels = {s.level for s in summaries}
        assert "pattern" in levels
        assert "principle" in levels
        pattern = next(s for s in summaries if s.level == "pattern")
        assert pattern.evidence_count == 3

    def test_no_spurious_pattern_below_support(self, world: WorldModel) -> None:
        summaries = world.compress_episodes(["a", "b", "c"], min_pattern_support=2)
        assert all(s.level == "observation" for s in summaries)

    def test_empty_input_yields_nothing(self, world: WorldModel) -> None:
        assert world.compress_episodes([]) == []

    def test_injected_summarizer_is_used(self, world: WorldModel) -> None:
        summaries = world.compress_episodes(["x", "y"], summarizer=lambda obs: "distilled insight")
        assert any("distilled insight" in s.text for s in summaries)

    def test_summarizer_failure_falls_back_to_deterministic(self, world: WorldModel) -> None:
        def broken(_: list[str]) -> str:
            raise RuntimeError("model down")

        summaries = world.compress_episodes(["a", "a"], summarizer=broken, min_pattern_support=2)
        # Fell back: still produced deterministic pattern extraction.
        assert any(s.level == "pattern" for s in summaries)

    def test_render_produces_markdown(self, world: WorldModel) -> None:
        summaries = world.compress_episodes(["z", "z"], min_pattern_support=2)
        md = world.render_summaries(summaries)
        assert "###" in md
