"""The import conventions this substrate holds itself to.

There is one, stated once: **a contract is resolved the same way everywhere it
is used.**

``RelationalBackend`` is the relational substrate's contract
(:mod:`paa.storage.relational.base`). Commit ``1c9ffa3`` made
:mod:`paa.memory.creator` import it at runtime and left **eight** siblings with
the same dependency behind ``if TYPE_CHECKING:`` — the same contract resolved
two different ways in nine modules that one commit changed. The difference is
invisible until something needs the *class* rather than the *annotation*: a
runtime ``isinstance``, a pydantic model resolving its annotations at
class-creation time, or a ``get_type_hints`` call. Nothing in the tree needed
the class yet, which is exactly why the inconsistency survived a review.

The lock has three halves, and each fails for a different reason:

* the **source** half says the contract is not imported behind
  ``if TYPE_CHECKING:`` anywhere in ``src/paa``, except a documented deferral
  that names the task that owns the file;
* the **runtime** half says every module which imports the contract binds it in
  its own namespace, to the same object — which is what "resolved at runtime"
  means, as opposed to "annotated at runtime";
* the **fresh-interpreter** half says every module under ``src/paa`` imports
  cleanly in an interpreter that has imported nothing.

The third half exists because the other two cannot see a cycle. A module that
is part of an import cycle imports fine inside a test session — the session's
``sys.modules`` is already populated, so the cycle's back edge finds a
half-initialised module and takes the shortcut that hides the problem. A
fresh interpreter has no such shortcut: the cycle breaks on the first module
that closes it, every time. That is why the check is a **subprocess** and not
an ``importlib.import_module`` in this process.

The source half is checked by parsing the AST rather than by grepping the
text, because the difference between the two spellings is structural — the same
import statement, at module scope or nested in a guard — and a text search
cannot tell them apart without also matching the word in a comment.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

import paa

#: The package under the convention.
PACKAGE_ROOT = Path(paa.__file__).parent
PACKAGE_NAME = paa.__name__

#: The contract, and the module that owns it.
CONTRACT_MODULE = "paa.storage.relational.base"
CONTRACT_NAME = "RelationalBackend"

#: The eight siblings commit ``1c9ffa3`` left behind, in the order the plan
#: names them. Seven are converted here; the eighth belongs to wave 2 and is
#: recorded in :data:`DEFERRED`.
FAMILY = (
    "paa.memory.contradiction",
    "paa.memory.decay",
    "paa.memory.curator",
    "paa.marketplace.installer",
    "paa.skills.registry",
    "paa.agents.policy",
    "paa.agents.context_agents",
    "paa.observability.runs",
)

#: Modules allowed to keep the contract behind ``if TYPE_CHECKING:``.
#:
#: Every entry names the task that owns the file, so a deferral is a handover
#: and not an excuse: a reader who finds the entry knows who to ask, and the
#: entry is deleted when that task lands. An entry with no reason is a drift
#: nobody is coming back to, so the test below refuses one.
DEFERRED: dict[str, str] = {
    "paa.observability.runs": (
        "T-008 (wave 2) owns paa/observability/runs.py and converts this block there; "
        "this wave does not own the file"
    ),
}

#: Modules that cannot import in a minimal install because they need an
#: optional extra. A missing extra is an environment fact, not a defect, and
#: the failure names the extra — so it is tolerated, and only for these
#: modules. A module outside this list that fails to import is a failure.
OPTIONAL_EXTRA_MODULES = frozenset(
    {
        "paa.api.app",  # fastapi — paa[api]
        "paa.storage.graph.kuzu_backend",  # kuzu — paa[graph]
        "paa.storage.relational.postgres",  # psycopg — paa[postgres]
        "paa.storage.queue.redis_backend",  # redis — paa[redis]
    }
)

#: How long one module may take to import before the check calls it hung.
IMPORT_TIMEOUT_SECONDS = 120

#: How many interpreters run at once. The probes are independent processes, so
#: this is wall-clock, not concurrency of anything shared.
PROBE_WORKERS = min(8, (os.cpu_count() or 2))

#: Markers of a genuine import cycle. A cycle is never an optional extra and
#: never an environment fact, so it is checked before either is considered.
_CYCLE_MARKERS = ("partially initialized", "circular import")

#: ``No module named 'paa.something'`` — a first-party module that is not
#: there. Distinct from a third-party package this minimal install lacks.
_MISSING_FIRST_PARTY = re.compile(r"No module named ['\"](paa[\w.]*)['\"]")

#: The probe, run once per module in a fresh interpreter. It imports the
#: module and then reports whether the contract is bound in that module's
#: namespace. The bound check is by *identity* over the module's globals, not
#: by name, so an aliased import still counts as resolved — what is being
#: asked is whether the module holds the class, not whether it spells it the
#: way this file does. Three tab-separated fields, one line, so the parent
#: never has to parse a traceback.
_PROBE = """\
import importlib
import sys

name = sys.argv[1]
try:
    module = importlib.import_module(name)
except BaseException as exc:
    print("FAIL\\t{}\\t{}".format(type(exc).__name__, str(exc).replace("\\n", " ")))
    raise SystemExit(0)
try:
    from paa.storage.relational.base import RelationalBackend
except BaseException as exc:
    print("FAIL\\t{}\\t{}".format(type(exc).__name__, str(exc).replace("\\n", " ")))
    raise SystemExit(0)
bound = any(value is RelationalBackend for value in vars(module).values())
print("OK\\t{}\\t{}".format("bound" if bound else "unbound", name))
"""


@dataclass(frozen=True, slots=True)
class Probe:
    """What one fresh interpreter said about one module."""

    module: str
    ok: bool
    bound: bool
    error_type: str
    error_message: str

    def describe(self) -> str:
        """One line a reviewer can read."""
        if self.ok:
            return f"{self.module}: imported, contract {'bound' if self.bound else 'NOT bound'}"
        return f"{self.module}: {self.error_type}: {self.error_message}"


# ---------------------------------------------------------------------------
# Enumeration and parsing


def _module_files() -> list[tuple[str, Path]]:
    """Every importable module under the package, with its source file."""
    found: list[tuple[str, Path]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT)
        parts = list(relative.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][: -len(".py")]
        if not parts:
            continue
        found.append((f"{PACKAGE_NAME}.{'.'.join(parts)}", path))
    return found


def _is_type_checking_guard(node: ast.If) -> bool:
    """Whether this ``if`` is the ``TYPE_CHECKING`` guard."""
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _imports(tree: ast.Module) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """The module's imports, split into runtime and type-checking.

    An import is *type-checking* when it is nested inside an ``if
    TYPE_CHECKING:`` guard, and *runtime* otherwise — including an import
    inside a ``try:``/``except ImportError:`` block, which is executed at
    import time, and one inside a function body, which is executed when the
    function runs. Neither is a type-checking import, and treating them as one
    would be a false positive in the lock.
    """
    runtime: set[tuple[str, str]] = set()
    type_checking: set[tuple[str, str]] = set()

    def walk(node: ast.AST, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            nested = guarded or (isinstance(child, ast.If) and _is_type_checking_guard(child))
            if isinstance(child, ast.ImportFrom):
                module = child.module or ""
                for alias in child.names:
                    (type_checking if nested else runtime).add((module, alias.name))
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    (type_checking if nested else runtime).add((alias.name, ""))
            walk(child, nested)

    walk(tree, False)
    return runtime, type_checking


def _imports_of(path: Path) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Parse one source file and split its imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _imports(tree)


# ---------------------------------------------------------------------------
# Probing


def _probe(module: str, *, extra_path: Path | None = None) -> Probe:
    """Import one module in a fresh interpreter and report what happened.

    ``extra_path`` is prepended to ``PYTHONPATH`` for the child, which is how
    the cycle fixture below is made importable without the repository
    containing a deliberate cycle.
    """
    env: dict[str, str] | None = None
    if extra_path is not None:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(extra_path) + (os.pathsep + existing if existing else "")

    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE, module],
            capture_output=True,
            text=True,
            timeout=IMPORT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return Probe(
            module=module,
            ok=False,
            bound=False,
            error_type="TimeoutExpired",
            error_message=f"import did not finish within {IMPORT_TIMEOUT_SECONDS}s",
        )

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return Probe(
            module=module,
            ok=False,
            bound=False,
            error_type="NoOutput",
            error_message=completed.stderr.strip()[-2000:],
        )

    status, detail, message = lines[-1].split("\t", 2)
    if status == "OK":
        return Probe(
            module=module,
            ok=True,
            bound=detail == "bound",
            error_type="",
            error_message="",
        )
    return Probe(
        module=module,
        ok=False,
        bound=False,
        error_type=detail,
        error_message=message,
    )


def _is_optional_extra_failure(probe: Probe) -> bool:
    """Whether a failure is an environment fact rather than a defect.

    Three shapes are tolerated: a third-party package this install does not
    have, and an ``ImportError`` that names the missing extra. A cycle is
    never tolerated, and neither is a missing *first-party* module — that one
    means the tree itself is broken.
    """
    message = probe.error_message
    if any(marker in message for marker in _CYCLE_MARKERS):
        return False
    if probe.error_type == "ModuleNotFoundError":
        return _MISSING_FIRST_PARTY.search(message) is None
    if probe.error_type == "ImportError":
        return "extra" in message
    return False


@pytest.fixture(scope="module")
def probes() -> dict[str, Probe]:
    """Import every module under ``src/paa`` in its own fresh interpreter.

    Module-scoped because the probes are expensive (one interpreter per
    module) and read-only: nothing here writes state, and the result is the
    same on every call. The alternative — re-running the sweep per test —
    would triple the cost of a suite that already spends a minute of its
    budget on subprocesses.
    """
    names = [name for name, _ in _module_files()]
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        results = list(pool.map(_probe, names))
    return {result.module: result for result in results}


@pytest.fixture(scope="module")
def importable(probes: dict[str, Probe]) -> list[str]:
    """The modules that imported cleanly, in reverse-alphabetical order."""
    return sorted((name for name, probe in probes.items() if probe.ok), reverse=True)


# ---------------------------------------------------------------------------
# The lock


def test_the_contract_is_not_imported_behind_type_checking() -> None:
    """No module resolves the relational contract lazily.

    This is the convention itself, stated as a check over the whole package
    rather than over the family: a contract that is imported at runtime in one
    place and lazily in another is two contracts, and the second one is
    missing at exactly the moment a runtime check needs the class.
    """
    offenders: list[str] = []
    for name, path in _module_files():
        _, type_checking = _imports_of(path)
        if (CONTRACT_MODULE, CONTRACT_NAME) in type_checking and name not in DEFERRED:
            offenders.append(name)

    assert offenders == [], (
        f"{offenders} resolve {CONTRACT_NAME} behind `if TYPE_CHECKING:`, which is how "
        "one contract came to be expressed two ways in a single commit. Import it at "
        "runtime, or record the deferral in DEFERRED with the task that owns the file."
    )


def test_every_module_that_imports_the_contract_binds_it_at_runtime(
    probes: dict[str, Probe],
) -> None:
    """An import that binds nothing is not an import.

    ``from __future__ import annotations`` makes an annotation a string, so a
    module can name a class it never binds — and every check that wants the
    class (an ``isinstance``, a pydantic model resolving its annotations, a
    ``get_type_hints``) then fails at the moment it runs rather than at the
    moment the inconsistency was written.
    """
    unbound: list[str] = []
    for name, path in _module_files():
        runtime, _ = _imports_of(path)
        if (CONTRACT_MODULE, CONTRACT_NAME) not in runtime or name in DEFERRED:
            continue
        probe = probes[name]
        assert probe.ok, (
            f"{name} imports the contract at runtime but does not import: {probe.describe()}"
        )
        if not probe.bound:
            unbound.append(name)

    assert unbound == [], (
        f"{unbound} import {CONTRACT_NAME} at runtime but do not bind it in their "
        "namespace — the import is not reaching the module's own globals."
    )


def test_the_family_resolves_the_contract_one_way(probes: dict[str, Probe]) -> None:
    """The eight siblings ``1c9ffa3`` left behind, and the model they follow.

    Named explicitly so a reader can see the family this convention is about,
    and so a regression points at the sibling that drifted rather than at the
    package as a whole.
    """
    unresolved = [
        name
        for name in FAMILY
        if name not in DEFERRED and not (probes[name].ok and probes[name].bound)
    ]
    assert unresolved == [], f"the family does not resolve the contract at runtime: {unresolved}"

    model = probes["paa.memory.creator"]
    assert model.ok and model.bound, (
        f"the model the family follows does not resolve the contract: {model.describe()}"
    )


def test_every_deferral_names_the_task_that_owns_the_file() -> None:
    """A deferral without an owner is a drift nobody is coming back to."""
    known = {name for name, _ in _module_files()}
    for module, reason in DEFERRED.items():
        assert reason.strip(), f"{module} is deferred without a reason"
        assert module in known, f"{module} is deferred but no such module exists"
        assert re.search(r"T-\d{3}", reason), (
            f"{module} is deferred without naming a task that owns it: {reason!r}"
        )


# ---------------------------------------------------------------------------
# The fresh interpreter


def test_every_module_under_src_paa_imports_in_a_fresh_interpreter(
    probes: dict[str, Probe],
) -> None:
    """Every module imports cleanly in an interpreter that imported nothing.

    A cycle is invisible to a test session: the session's ``sys.modules`` is
    already populated, so the back edge of the cycle finds a half-initialised
    module and the import succeeds. A fresh interpreter has no such
    half-initialised module to find, so the cycle breaks — every time, and
    only here.
    """
    failures = [probe for probe in probes.values() if not probe.ok]
    fatal = [probe for probe in failures if not _is_optional_extra_failure(probe)]
    tolerated = [probe for probe in failures if _is_optional_extra_failure(probe)]

    assert fatal == [], "modules that do not import in a fresh interpreter:\n" + "\n".join(
        probe.describe() for probe in fatal
    )
    unexpected = {probe.module for probe in tolerated} - OPTIONAL_EXTRA_MODULES
    assert not unexpected, (
        f"{sorted(unexpected)} do not import, and the failure is not an optional extra "
        "this install happens to lack"
    )


def test_every_module_imports_together_in_one_interpreter(
    importable: list[str],
) -> None:
    """The whole package imports in one interpreter, in reverse order.

    The per-module probe proves each module can be the *first* thing imported.
    This proves they can also share one interpreter — which is the state the
    application is actually in — and it imports them in reverse-alphabetical
    order, because alphabetical order is the order a developer tends to try,
    and a cycle that only breaks in one of the two orders is still a cycle.
    """
    script = "\n".join(f"import {name}" for name in importable)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, (
        "the package does not import as a whole:\n" + completed.stderr.strip()[-4000:]
    )


def test_the_probe_would_notice_a_cycle(tmp_path: Path) -> None:
    """The check itself is sound: it fails on a cycle, by construction.

    A lock that cannot fail is a description. This proves the probe reports a
    cycle as a failure rather than as a tolerated missing extra, using two
    modules that really do form a cycle — written into ``tmp_path``, so the
    repository never contains a deliberate cycle.
    """
    package = tmp_path / "cycle_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "a.py").write_text(
        "from cycle_fixture.b import B\n\nA = 1\n",
        encoding="utf-8",
    )
    (package / "b.py").write_text(
        "from cycle_fixture.a import A\n\nB = 2\n",
        encoding="utf-8",
    )

    probe = _probe("cycle_fixture.a", extra_path=tmp_path)
    assert not probe.ok, "a cycle imported cleanly — the probe cannot see cycles"
    assert any(marker in probe.error_message for marker in _CYCLE_MARKERS), (
        f"the failure does not read as a cycle: {probe.describe()}"
    )
    assert not _is_optional_extra_failure(probe), (
        "a cycle was mistaken for a missing optional extra"
    )
