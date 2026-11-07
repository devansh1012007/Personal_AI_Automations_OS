"""T05 structural lock + behaviour pin for the unified recall touch helper.

PLAN:integration-fix:P004

Before the unification there were four drifted copies of the same helper
(one per recall surface); this suite fails on that layout and passes only
when ``paa.memory.touch.touch_recalled`` is the single definition.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import cast

import pytest

from paa.memory import touch_recalled
from paa.memory.decay import DecaySweeper
from paa.memory.touch import touch_recalled as touch_recalled_from_module
from paa.storage.relational.base import RelationalBackend

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "paa"


def _touch_defs() -> list[tuple[Path, str]]:
    """Every definition of either touch-helper spelling under ``src/paa``."""
    found: list[tuple[Path, str]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
                "_touch_recalled",
                "touch_recalled",
            ):
                found.append((path, node.name))
    return found


def test_touch_recalled_exists_with_contract_signature() -> None:
    assert inspect.iscoroutinefunction(touch_recalled)
    assert touch_recalled is touch_recalled_from_module
    params = inspect.signature(touch_recalled).parameters
    assert list(params) == ["db", "fact_ids"]
    assert params["db"].annotation == "RelationalBackend | None"
    assert params["fact_ids"].annotation == "list[str]"


def test_exactly_one_touch_helper_definition_in_src() -> None:
    defs = _touch_defs()
    assert defs == [(SRC_ROOT / "memory" / "touch.py", "touch_recalled")]


@pytest.mark.parametrize(
    ("fact_ids", "expected"),
    [
        (["a", "b", "a", "c", "b"], ["a", "b", "c"]),
        (["x"], ["x"]),
    ],
)
async def test_duplicate_ids_deduped_order_preserved(
    monkeypatch: pytest.MonkeyPatch, fact_ids: list[str], expected: list[str]
) -> None:
    calls: list[list[str]] = []

    async def fake_touch_many(self: DecaySweeper, ids: list[str]) -> None:
        calls.append(list(ids))

    monkeypatch.setattr(DecaySweeper, "touch_many", fake_touch_many)
    # ``db`` is never dereferenced: touch_many is patched on the class.
    await touch_recalled(cast(RelationalBackend, object()), fact_ids)
    assert calls == [expected]


async def test_db_none_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(self: DecaySweeper, ids: list[str]) -> None:
        raise AssertionError("touch_many must not run when db is None")

    monkeypatch.setattr(DecaySweeper, "touch_many", forbidden)
    await touch_recalled(None, ["a"])


async def test_empty_list_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(self: DecaySweeper, ids: list[str]) -> None:
        raise AssertionError("touch_many must not run for an empty id list")

    monkeypatch.setattr(DecaySweeper, "touch_many", forbidden)
    await touch_recalled(cast(RelationalBackend, object()), [])


async def test_touch_failure_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(self: DecaySweeper, ids: list[str]) -> None:
        raise RuntimeError("synthetic touch failure")

    monkeypatch.setattr(DecaySweeper, "touch_many", failing)
    # Must not raise: a touch failure degrades to a warning, never a recall failure.
    await touch_recalled(cast(RelationalBackend, object()), ["a", "b"])
