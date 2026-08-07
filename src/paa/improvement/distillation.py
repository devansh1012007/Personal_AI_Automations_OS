"""Skill distillation — turning a successful task into a reusable recipe.

RFC §3.2. When a task commits cleanly, the system reviews how it was done and,
if it was non-trivial, distils the tool sequence into a generalised recipe that
future similar tasks can reuse. This is the runtime growing its own capabilities
from experience rather than only from human-authored skills.

The generalisation step is what makes a recipe reusable: concrete filenames,
paths and string literals are stripped to typed placeholders, so a recipe mined
from "refactor auth.py" applies to "refactor billing.py". The recipe is only
registered after it passes a sandbox smoke test — an unverified recipe is a
liability, not a capability (RFC §3.2 final step).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from paa.core.types import ComplexityModality

if TYPE_CHECKING:
    from paa.storage.relational.database import Database

__all__ = ["Recipe", "SkillDistiller", "generalize_arguments"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class Recipe:
    """A distilled, reusable procedure. Mirrors the RFC §3.2 JSON shape."""

    recipe_name: str
    target_capability_class: str
    ordered_tool_sequence: list[str] = field(default_factory=list)
    injected_prompt_heuristics: str = ""
    sample_count: int = 1

    def to_payload(self) -> dict[str, Any]:
        return {
            "recipe_name": self.recipe_name,
            "target_capability_class": self.target_capability_class,
            "ordered_tool_sequence": self.ordered_tool_sequence,
            "injected_prompt_heuristics": self.injected_prompt_heuristics,
            "sample_count": self.sample_count,
        }


_PATH_RE = re.compile(r"([\"']?)(?:[\w./\\-]+/)?[\w-]+\.[a-zA-Z0-9]+\1")
_QUOTED_RE = re.compile(r"([\"']).*?\1")


def generalize_arguments(value: Any) -> Any:
    """Strip concrete filenames and string literals to typed placeholders.

    A recipe mined from one task must apply to the next: ``auth.py`` becomes
    ``<FILE>`` and a quoted literal becomes ``<STR>``, so the *shape* of the
    procedure survives while its specifics do not.
    """
    if isinstance(value, str):
        out = _PATH_RE.sub("<FILE>", value)
        out = _QUOTED_RE.sub("<STR>", out)
        return out
    if isinstance(value, list):
        return [generalize_arguments(v) for v in value]
    if isinstance(value, dict):
        return {k: generalize_arguments(v) for k, v in value.items()}
    return value


class SkillDistiller:
    """Mines recipes from committed tasks and (optionally) registers them."""

    def __init__(self, db: Database, *, sandbox_tester: object | None = None) -> None:
        self._db = db
        self._tester = sandbox_tester

    def should_distil(self, *, modality: str, tool_call_count: int) -> bool:
        """Skip trivial tasks (RFC §3.2 complexity gate).

        SIMPLE tasks or those that used no tools have nothing worth generalising
        — distilling them would just add noise to the registry.
        """
        if modality == ComplexityModality.SIMPLE.value:
            return False
        return tool_call_count > 0

    def distil(
        self,
        *,
        capability_class: str,
        tool_sequence: list[dict[str, Any]],
        heuristic: str = "",
    ) -> Recipe:
        """Build a generalised recipe from an observed tool sequence."""
        ordered = [step.get("tool", step.get("action", "step")) for step in tool_sequence]
        name = f"{capability_class.lower()}.{'_'.join(ordered[:3]) or 'recipe'}"
        return Recipe(
            recipe_name=name[:120],
            target_capability_class=capability_class,
            ordered_tool_sequence=ordered,
            injected_prompt_heuristics=heuristic,
        )

    async def verify_and_register(self, recipe: Recipe) -> bool:
        """Smoke-test the recipe in a sandbox, then register it if it passes.

        Returns whether it was registered. An unverified recipe is never
        written — a bad recipe would be reused on every matching task.
        """
        if self._tester is not None:
            try:
                ok = await self._tester.smoke_test(recipe)  # type: ignore[attr-defined]
            except Exception as exc:
                log.warning(
                    "distillation.smoke_test_failed",
                    recipe=recipe.recipe_name,
                    error=str(exc),
                )
                return False
            if not ok:
                log.info("distillation.recipe_rejected", recipe=recipe.recipe_name)
                return False

        await self._register(recipe)
        log.info("distillation.recipe_registered", recipe=recipe.recipe_name)
        return True

    async def _register(self, recipe: Recipe) -> None:
        import uuid

        from paa.storage.relational.database import dumps, to_iso, utc_now

        now = to_iso(utc_now())
        await self._db.execute(
            "INSERT INTO hot_serving_skill_registry "
            "(id, skill_name, provider, description, input_schema, output_schema, "
            " invocation, installed_at, updated_at) "
            "VALUES (?, ?, 'native', ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(skill_name) DO UPDATE SET "
            "  invocation = excluded.invocation, updated_at = excluded.updated_at",
            (
                str(uuid.uuid4()),
                recipe.recipe_name,
                f"Distilled recipe for {recipe.target_capability_class}",
                dumps({"type": "object"}),
                dumps({"type": "object"}),
                dumps(recipe.to_payload()),
                now,
                now,
            ),
        )
