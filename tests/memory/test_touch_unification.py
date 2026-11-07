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
import structlog

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


#: The four recall surfaces that must call the one helper (R-W2-101).
_TOUCH_CONSUMERS = (
    SRC_ROOT / "api" / "app.py",
    SRC_ROOT / "cli" / "main.py",
    SRC_ROOT / "coder" / "runtime_tools.py",
    SRC_ROOT / "agents" / "context_agents.py",
)


def _touch_import_and_call(path: Path) -> tuple[bool, bool]:
    """Whether ``path`` imports ``touch_recalled`` from ``paa.memory.touch`` and calls it."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    has_import = False
    has_call = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "paa.memory.touch"
            and any(alias.name == "touch_recalled" for alias in node.names)
        ):
            has_import = True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "touch_recalled"
        ):
            has_call = True
    return has_import, has_call


def test_every_recall_surface_imports_and_calls_the_one_helper() -> None:
    """R-W2-101: the definition count alone does not pin the call sites.

    A surface that inlined ``DecaySweeper.touch_many`` again (behavior-
    identical today, drift tomorrow — the failure mode this suite exists
    to prevent) would pass the definition-count lock, so each of the four
    consumers must both import ``touch_recalled`` from ``paa.memory.touch``
    and call it.
    """
    for path in _TOUCH_CONSUMERS:
        has_import, has_call = _touch_import_and_call(path)
        assert has_import, f"{path.relative_to(SRC_ROOT)} does not import touch_recalled"
        assert has_call, f"{path.relative_to(SRC_ROOT)} does not call touch_recalled"


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


async def test_touch_failure_log_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """R-W2-102: the failure warning's ``error`` is redacted, not verbatim.

    ``touch_recalled`` logs ``error=redact(str(exc))``; a secret-shaped
    exception must reach the log as ``***REDACTED***``. Removing
    ``redact()`` at that line must fail this test (mirrors the W2-GEN-01
    redaction pin for the semantic hydration path).
    """

    async def failing(self: DecaySweeper, ids: list[str]) -> None:
        raise RuntimeError("sk-ABCDEF1234567890")

    monkeypatch.setattr(DecaySweeper, "touch_many", failing)
    with structlog.testing.capture_logs() as captured:
        await touch_recalled(cast(RelationalBackend, object()), ["a", "b"])
    entries = [e for e in captured if e.get("event") == "memory.recall_touch_failed"]
    assert entries
    for entry in entries:
        error_text = str(entry.get("error", ""))
        assert "***REDACTED***" in error_text
        assert "sk-ABCDEF1234567890" not in error_text
