"""The eval task set — one task's fixture, suite, intent and probe (C7.4).

A task is *data*, not code: a directory under the repository's ``evals/``
tree that the loader turns into an :class:`EvalTask`. The directory's
shape is the contract::

    evals/tasks/<task-id>/
      task.json          the manifest (schema ``paa-eval-task/1``)
      workspace/...      the fixture files laid into the run's workspace
      tests/...          the task's own pytest suite, laid in beside them
      solution/...       the reference diff — the intended final files

The manifest names the human intent (one sentence), the task's *kind*,
and the deterministic probes:

``kind: "exact"``
    The intended diff is byte-exact: every file under ``solution/`` must
    exist in the scored workspace with exactly those bytes, and the
    workspace diff (created + modified) must be exactly that file set
    with nothing deleted. "Diff matches intent" is a content hash
    comparison — no judgement anywhere in it.

``kind: "semantics"``
    The intent is *declared semantics*: the manifest's ``touched`` list
    names the surfaces a correct solve must modify, and the semantics
    themselves are pinned by the task's own pytest suite (the
    ``tests_pass`` check). "Diff matches intent" means mechanically:
    every ``touched`` path appears in the workspace diff, and nothing
    was deleted. Multiple byte-different diffs can pass — that is the
    point of the kind.

``forbidden_created`` / ``forbidden_changed``
    The forbidden-effects probe. ``forbidden_created`` paths must not
    exist after the run; ``forbidden_changed`` paths are fixture files
    whose bytes must still be the baseline's. Both are decided by the
    runner's own manifest snapshots, never by the harness and never by
    a model.

Two invariants the loader holds, cheaply, at load:

* **the manifest's ``id`` is the directory's name** — a task's identity
  is where it lives, so a renamed directory cannot keep answering to
  its old name in reports and ledger rows;
* **every fixture/test/solution path is relative and jail-safe** — no
  absolute paths, no ``..`` — because :meth:`EvalTask.layout` writes
  them into a workspace the sandbox will jail.

The task set is repository data (like ``tests/``), deliberately not
packaged in the wheel: an installed runtime discovers it from a source
checkout (walking up from this module) or from ``PAA_EVAL_TASKS_DIR``.
The eval corpus is meant to travel with the repo it scores.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.core.errors import PaaError
from paa.ledger.events import canonical_json

__all__ = [
    "TASKS_DIRNAME",
    "TASK_KINDS",
    "TASK_SCHEMA",
    "EvalError",
    "EvalTask",
    "load_task",
    "load_tasks",
    "task_set_root",
]

#: The manifest's schema tag — the task set's format identity.
TASK_SCHEMA = "paa-eval-task/1"

#: The directory name the discovery walk looks for at a repository root.
TASKS_DIRNAME = "evals"

#: The task kinds, and what each makes "diff matches intent" mean.
TASK_KINDS: tuple[str, ...] = ("exact", "semantics")

#: How far up from this module the discovery walk will look for the
#: repository root. Four covers ``<repo>/src/paa/evals/tasks.py`` with a
#: level to spare; unbounded walks are how a stale ancestor directory
#: silently becomes somebody's task set.
_MAX_DISCOVER_LEVELS = 5


class EvalError(PaaError):
    """The eval harness refused something it can name: task set, task, harness."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EvalError(f"{path} is not readable UTF-8 text: {exc}") from exc


def _collect_files(root: Path) -> dict[str, str]:
    """Every regular file under ``root``, keyed by POSIX-relative path.

    Directories are laid down implicitly by :meth:`EvalTask.layout`, so
    an empty directory in the fixture tree carries no information and is
    skipped rather than invented a representation for.
    """
    if not root.is_dir():
        return {}
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            files[path.relative_to(root).as_posix()] = _read_text(path)
    return files


def _safe_rel(path: str, where: str) -> str:
    """A fixture path that cannot escape the workspace when laid down."""
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts or not path.strip():
        raise EvalError(f"{where} path {path!r} must be relative and jail-safe")
    return candidate.as_posix()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvalTask:
    """One eval task: fixture, suite, intent, and the deterministic probes."""

    task_id: str
    intent: str
    kind: str
    files: dict[str, str]
    tests: dict[str, str]
    expected: dict[str, str]
    touched: tuple[str, ...]
    forbidden_created: tuple[str, ...]
    forbidden_changed: tuple[str, ...]
    source_dir: Path

    def layout(self, workspace: Path) -> None:
        """Lay the fixture and the suite into ``workspace``.

        The workspace is the sandbox's jail for the whole run, so the
        layout happens before the harness spawns and nothing here may
        reach outside it — every path came through :func:`_safe_rel` at
        load.
        """
        for rel, content in sorted(self.files.items()) + sorted(self.tests.items()):
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def manifest_digest(self) -> str:
        """The task's data identity: which version of the task this was.

        Covers every input scoring reads — intent, kind, fixture, suite,
        solution, probes — as content hashes in canonical form, so two
        structurally equal tasks always digest identically and a ledger
        row citing the digest names exactly which task scored, even
        after the task has since been edited.
        """
        payload = {
            "schema": TASK_SCHEMA,
            "id": self.task_id,
            "kind": self.kind,
            "intent": self.intent,
            "files": {p: _sha256_text(c) for p, c in sorted(self.files.items())},
            "tests": {p: _sha256_text(c) for p, c in sorted(self.tests.items())},
            "expected": {p: _sha256_text(c) for p, c in sorted(self.expected.items())},
            "touched": list(self.touched),
            "forbidden_created": list(self.forbidden_created),
            "forbidden_changed": list(self.forbidden_changed),
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _parse_manifest(raw: Any, path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EvalError(f"{path} is not a JSON object")
    if raw.get("schema") != TASK_SCHEMA:
        raise EvalError(
            f"{path} is not a {TASK_SCHEMA} manifest "
            f"(schema={raw.get('schema')!r})"
        )
    return raw


def _require_str(manifest: dict[str, Any], key: str, path: Path) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvalError(f"{path}: field {key!r} must be a non-empty string")
    return value


def _require_str_list(manifest: dict[str, Any], key: str, path: Path) -> tuple[str, ...]:
    value = manifest.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise EvalError(f"{path}: field {key!r} must be a list of strings")
    return tuple(_safe_rel(v, f"{path}: {key}") for v in value)


def load_task(task_dir: Path) -> EvalTask:
    """Load and validate one task directory; refuses off-contract manifests.

    Every refusal names the file and the field, because a task set is
    authored data: the author should never have to guess which of the
    three probes mis-spelled a path.
    """
    task_dir = Path(task_dir)
    manifest_path = task_dir / "task.json"
    if not manifest_path.is_file():
        raise EvalError(f"{task_dir} has no task.json — not a task directory")
    try:
        raw: Any = json.loads(_read_text(manifest_path))
    except json.JSONDecodeError as exc:
        raise EvalError(f"{manifest_path} is not valid JSON: {exc}") from exc
    manifest = _parse_manifest(raw, manifest_path)

    task_id = _require_str(manifest, "id", manifest_path)
    if task_id != task_dir.name:
        raise EvalError(
            f"{manifest_path}: id {task_id!r} does not match its directory "
            f"name {task_dir.name!r} — a task's identity is where it lives"
        )
    intent = _require_str(manifest, "intent", manifest_path)
    kind = _require_str(manifest, "kind", manifest_path)
    if kind not in TASK_KINDS:
        raise EvalError(
            f"{manifest_path}: kind {kind!r} is not one of {TASK_KINDS}"
        )

    files = _collect_files(task_dir / "workspace")
    tests = {
        # Suite files are keyed by their workspace path — the tests/
        # directory is part of the suite's identity, not a prefix the
        # layout step has to remember to add.
        f"tests/{rel}": content
        for rel, content in _collect_files(task_dir / "tests").items()
    }
    expected = _collect_files(task_dir / "solution")
    touched = _require_str_list(manifest, "touched", manifest_path)
    forbidden_created = _require_str_list(manifest, "forbidden_created", manifest_path)
    forbidden_changed = _require_str_list(manifest, "forbidden_changed", manifest_path)

    if not tests:
        raise EvalError(
            f"{task_dir}: no pytest suite under tests/ — the tests_pass check "
            "needs positive evidence, and a task without a suite cannot "
            "produce any"
        )
    if kind == "exact" and not expected:
        raise EvalError(
            f"{task_dir}: kind 'exact' needs files under solution/ — the "
            "intent check is a content comparison against them"
        )
    if kind == "semantics" and not touched:
        raise EvalError(
            f"{task_dir}: kind 'semantics' needs a non-empty 'touched' list — "
            "the intent check is that the declared surfaces were modified"
        )
    for probe in forbidden_changed:
        if probe not in files:
            raise EvalError(
                f"{task_dir}: forbidden_changed names {probe!r}, which is not "
                "a fixture file — a must-not-change probe must have baseline "
                "bytes to be checked against"
            )
    for probe in touched:
        if probe not in files and probe not in expected:
            raise EvalError(
                f"{task_dir}: touched names {probe!r}, which is neither a "
                "fixture file nor a solution file — a declared surface must "
                "exist on one side of the intended diff"
            )

    return EvalTask(
        task_id=task_id,
        intent=intent,
        kind=kind,
        files=files,
        tests=tests,
        expected=expected,
        touched=touched,
        forbidden_created=forbidden_created,
        forbidden_changed=forbidden_changed,
        source_dir=task_dir,
    )


def _looks_like_task_set(root: Path) -> bool:
    """Whether ``root`` holds this repository's task set.

    The marker is structural, not just a directory name: at least one
    subdirectory whose ``task.json`` carries the schema tag. A directory
    that merely happens to be called ``evals`` must not become a task
    set, and neither must an empty one.
    """
    tasks = root / "tasks"
    if not tasks.is_dir():
        return False
    for child in sorted(tasks.iterdir()):
        manifest = child / "task.json"
        if not manifest.is_file():
            continue
        try:
            raw: Any = json.loads(_read_text(manifest))
        except (EvalError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict) and raw.get("schema") == TASK_SCHEMA:
            return True
    return False


def task_set_root() -> Path:
    """Where the repository's task set lives, or raise :class:`EvalError`.

    ``PAA_EVAL_TASKS_DIR`` wins when set — the installed-runtime door.
    Otherwise the walk goes up from this module looking for a directory
    named ``evals`` that structurally is a task set, bounded so a stale
    ancestor never silently qualifies.
    """
    override = os.environ.get("PAA_EVAL_TASKS_DIR")
    if override:
        root = Path(override)
        if not _looks_like_task_set(root):
            raise EvalError(
                f"PAA_EVAL_TASKS_DIR={override} does not hold a task set "
                "(no tasks/*/task.json with a paa-eval-task/1 schema)"
            )
        return root

    here = Path(__file__).resolve()
    for depth, ancestor in enumerate(here.parents):
        if depth > _MAX_DISCOVER_LEVELS:
            break
        if _looks_like_task_set(ancestor / TASKS_DIRNAME):
            return ancestor / TASKS_DIRNAME
    raise EvalError(
        f"no eval task set found — looked up to {_MAX_DISCOVER_LEVELS} levels "
        f"above {here}; set PAA_EVAL_TASKS_DIR to the repository's evals/ "
        "directory"
    )


def load_tasks(root: Path | None = None) -> tuple[EvalTask, ...]:
    """Every task under ``root`` (default: the discovered task set), by id.

    Sorted by task id so a report's row order is a property of the task
    set, not of the filesystem's mood.
    """
    base = Path(root) if root is not None else task_set_root()
    if not base.is_dir():
        raise EvalError(f"task set root {base} is not a directory")
    tasks_dir = base / "tasks" if (base / "tasks").is_dir() else base
    tasks: list[EvalTask] = []
    for child in sorted(tasks_dir.iterdir()):
        if (child / "task.json").is_file():
            tasks.append(load_task(child))
    return tuple(sorted(tasks, key=lambda t: t.task_id))
