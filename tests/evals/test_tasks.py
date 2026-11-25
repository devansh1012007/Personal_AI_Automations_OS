"""The task set: loading, manifest refusals, and the digest contract (C7.4).

A task is data, so its loader is a contract enforcer: everything the
scoring checks read later — fixture, suite, solution, probes — is
validated at load, and every refusal names the file and the field. These
tests pin the five shipped tasks (the acceptance matrix's subjects) and
the loader's refusal table, because a task set that silently tolerated
an off-contract manifest would score rows against inputs nobody authored.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paa.evals.tasks import (
    TASK_SCHEMA,
    EvalError,
    load_task,
    load_tasks,
    task_set_root,
)

#: The five shipped tasks, by id — the acceptance matrix's subjects.
SHIPPED_TASK_IDS = (
    "fizzbuzz-off-by-one",
    "greet-cli-flag",
    "inventory-report",
    "palindrome-guard",
    "reverse-util",
)


def _write_task(
    root: Path,
    task_id: str = "scratch-task",
    *,
    manifest_overrides: dict[str, object] | None = None,
    kind: str = "semantics",
    touched: list[str] | None = None,
    forbidden_created: list[str] | None = None,
    forbidden_changed: list[str] | None = None,
    with_solution: bool = True,
    with_tests: bool = True,
) -> Path:
    """Lay down one well-formed task directory, then let the caller bend it."""
    task_dir = root / "tasks" / task_id
    (task_dir / "workspace").mkdir(parents=True)
    (task_dir / "workspace" / "TASK.md").write_text("# Task\n", encoding="utf-8")
    (task_dir / "workspace" / "subject.py").write_text("x = 1\n", encoding="utf-8")
    if with_tests:
        (task_dir / "tests").mkdir()
        (task_dir / "tests" / "test_subject.py").write_text(
            "def test_subject() -> None:\n    assert True\n", encoding="utf-8"
        )
    if with_solution:
        (task_dir / "solution").mkdir()
        (task_dir / "solution" / "subject.py").write_text("x = 2\n", encoding="utf-8")
    manifest: dict[str, object] = {
        "schema": TASK_SCHEMA,
        "id": task_id,
        "intent": "a scratch task for refusal tests",
        "kind": kind,
        "touched": touched if touched is not None else ["subject.py"],
        "forbidden_created": forbidden_created or [],
        "forbidden_changed": forbidden_changed or [],
    }
    manifest.update(manifest_overrides or {})
    (task_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    return task_dir


class TestTheShippedSet:
    def test_the_five_tasks_load_from_the_repository_task_set(self) -> None:
        tasks = load_tasks(task_set_root())
        assert tuple(t.task_id for t in tasks) == SHIPPED_TASK_IDS

    def test_every_manifest_carries_the_schema_and_its_own_identity(self) -> None:
        for task in load_tasks(task_set_root()):
            manifest = json.loads(
                (task.source_dir / "task.json").read_text(encoding="utf-8")
            )
            assert manifest["schema"] == TASK_SCHEMA
            assert manifest["id"] == task.task_id == task.source_dir.name
            assert task.kind in ("exact", "semantics")

    def test_every_task_lays_a_suite_and_a_workspace_marker(self) -> None:
        # The tests_pass check needs positive evidence, and the observed
        # harness finds its workspace by TASK.md — both are load-time
        # facts the fixtures must actually carry.
        for task in load_tasks(task_set_root()):
            assert any(name.startswith("tests/") for name in task.tests)
            assert "TASK.md" in task.files

    def test_both_kinds_are_shipped(self) -> None:
        kinds = {t.task_id: t.kind for t in load_tasks(task_set_root())}
        assert kinds["reverse-util"] == "exact"
        assert kinds["fizzbuzz-off-by-one"] == "semantics"

    def test_the_manifest_digest_is_a_pure_function_of_the_task(self) -> None:
        again = {t.task_id: t for t in load_tasks(task_set_root())}
        for task in load_tasks(task_set_root()):
            assert task.manifest_digest() == again[task.task_id].manifest_digest()

    def test_the_digest_moves_when_the_intent_moves(self, tmp_path: Path) -> None:
        first = load_task(_write_task(tmp_path / "first"))
        bent = load_task(
            _write_task(tmp_path / "second", manifest_overrides={"intent": "a different ask"})
        )
        assert first.manifest_digest() != bent.manifest_digest()


class TestTheRefusalTable:
    """Every off-contract manifest is refused, naming the file and the field."""

    def test_a_directory_without_a_manifest_is_not_a_task(self, tmp_path: Path) -> None:
        bare = tmp_path / "tasks" / "not-a-task"
        bare.mkdir(parents=True)
        with pytest.raises(EvalError, match=r"no task\.json"):
            load_task(bare)

    def test_the_schema_tag_is_the_format_identity(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path, manifest_overrides={"schema": "paa-eval-task/0"})
        with pytest.raises(EvalError, match="not a paa-eval-task/1 manifest"):
            load_task(task_dir)

    def test_a_task_lives_where_it_says_it_lives(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path, manifest_overrides={"id": "somewhere-else"})
        with pytest.raises(EvalError, match="does not match its directory name"):
            load_task(task_dir)

    def test_an_unknown_kind_is_refused(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path, kind="vibes")
        with pytest.raises(EvalError, match="kind 'vibes' is not one of"):
            load_task(task_dir)

    def test_a_task_without_a_suite_cannot_claim_positive_evidence(
        self, tmp_path: Path
    ) -> None:
        task_dir = _write_task(tmp_path, with_tests=False)
        with pytest.raises(EvalError, match="no pytest suite under tests/"):
            load_task(task_dir)

    def test_an_exact_task_needs_reference_bytes(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path, kind="exact", with_solution=False)
        with pytest.raises(EvalError, match="kind 'exact' needs files under solution/"):
            load_task(task_dir)

    def test_a_semantics_task_needs_declared_surfaces(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path, touched=[])
        with pytest.raises(EvalError, match="needs a non-empty 'touched' list"):
            load_task(task_dir)

    def test_a_must_not_change_probe_needs_baseline_bytes(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path, forbidden_changed=["not-a-fixture.py"])
        with pytest.raises(EvalError, match="not a fixture file"):
            load_task(task_dir)

    def test_a_declared_surface_must_exist(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path, touched=["ghost.py"])
        with pytest.raises(EvalError, match="neither a fixture file nor a solution"):
            load_task(task_dir)

    def test_a_probe_path_cannot_escape_the_workspace(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path, forbidden_created=["../outside.txt"])
        with pytest.raises(EvalError, match="must be relative and jail-safe"):
            load_task(task_dir)


class TestTheDiscoveryWalk:
    def test_the_override_door_wins_when_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_task(tmp_path, task_id="override-task")
        monkeypatch.setenv("PAA_EVAL_TASKS_DIR", str(tmp_path))
        tasks = load_tasks()
        assert tuple(t.task_id for t in tasks) == ("override-task",)

    def test_an_empty_override_directory_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PAA_EVAL_TASKS_DIR", str(tmp_path / "nothing"))
        with pytest.raises(EvalError, match="does not hold a task set"):
            task_set_root()

    def test_a_directory_that_merely_matches_the_name_is_not_a_task_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The marker is structural: a task.json carrying the schema tag.
        # A directory called ``evals`` without one must not become the
        # task set just by existing.
        fake = tmp_path / "evals"
        (fake / "tasks" / "some-task").mkdir(parents=True)
        (fake / "tasks" / "some-task" / "task.json").write_text(
            json.dumps({"schema": "something-else"}), encoding="utf-8"
        )
        monkeypatch.setenv("PAA_EVAL_TASKS_DIR", str(fake))
        with pytest.raises(EvalError, match="does not hold a task set"):
            task_set_root()
