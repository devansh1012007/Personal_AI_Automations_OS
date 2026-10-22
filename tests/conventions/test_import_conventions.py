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

The lock has seven halves, and each fails for a different reason:

* the **source** half says the contract is not imported behind
  ``if TYPE_CHECKING:`` anywhere in ``src/paa``, except a documented deferral
  that names the task that owns the file;
* the **runtime** half says every module that *mentions* the contract — in
  any import form — binds it in its own namespace, to the same object, which
  is what "resolved at runtime" means, as opposed to "annotated at runtime";
* the **deferral** half says a deferral that has been **acted on** is a
  failure, so an entry cannot outlive the work that made it unnecessary;
* the **annotation** half says the family — and the package that owns the
  contract — can have their annotations *read* at runtime, which is the thing
  the first two halves exist for;
* the **sweep** half says every module under ``src/paa`` imports cleanly in
  an interpreter that has imported nothing, and that the sweep's coverage is
  **counted** against the files on disk rather than claimed in prose;
* the **extra** half says each module the sweep tolerates a failure for
  really does need the extra it is listed under, so the tolerance list is a
  set of facts about the tree and not a set of claims about it;
* the **classification** half says every ``if TYPE_CHECKING:`` block left in
  the package is **classified** — a cycle, an optional extra, deliberate
  laziness, a guard that buys nothing, a cheap first-party import, or the
  family — so a block that was left behind was left behind for a stated
  reason, and the classification is checked against the tree rather than
  claimed in prose.

The **sweep** half exists because the other halves cannot see a cycle. A
module that is part of an import cycle imports fine inside a test session — the
session's ``sys.modules`` is already populated, so the cycle's back edge finds
a half-initialised module and takes the shortcut that hides the problem. A
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
import importlib
import inspect
import os
import re
import shutil
import subprocess
import sys
import typing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

import paa

#: The package under the convention.
PACKAGE_ROOT = Path(paa.__file__).parent

#: Its dotted name. Annotated because ``paa.__name__`` is ``Any`` to a type
#: checker, and an ``Any`` in a path expression turns the whole expression
#: into one.
PACKAGE_NAME: str = paa.__name__

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
#:
#: The entry is also **self-invalidating**: a module listed here must *still*
#: resolve the contract lazily. The moment its owner converts it, the entry
#: stops being true, the lock goes red, and the entry has to be deleted with
#: the change that made it unnecessary. A deferral that silently outlives its
#: deferral is the drift this map exists to prevent, and the map alone cannot
#: prevent it — only a check can.
DEFERRED: dict[str, str] = {
    "paa.observability.runs": (
        "T-008 (wave 2) owns paa/observability/runs.py and converts this block there; "
        "this wave does not own the file"
    ),
}

#: The class of every ``if TYPE_CHECKING:`` block left in the package, keyed by
#: module, with the class as the value.
#:
#: This classification used to live in a paragraph in a run-scoped report — a
#: directory the next run will not read. It lives here because it is a **fact
#: about the tree**, and a fact about the tree can be *checked*: the table is
#: compared against the same AST sweep it was derived from, so a block that
#: appears without a class fails the lock, and a block that is converted
#: without being removed from the table fails it too. The table is the
#: durable form of the plan's "documented and deferred, with the cycle proof".
#:
#: The classes are:
#:
#: ``A`` — **a real import cycle.** The block cannot be converted: a runtime
#:     import closes a cycle and the import breaks. Proven rather than
#:     asserted, by converting the block in a *copy* of the package and
#:     watching the import fail.
#: ``B`` — **an optional extra.** The block names a package this project
#:     provides as an extra. Converting it makes the module need that extra
#:     at module scope, which a minimal install does not have.
#: ``C`` — **deliberate laziness.** The block names a first-party module the
#:     author wanted to keep out of this module's import, and the price of
#:     the laziness is measured and recorded in :data:`TYPE_CHECKING_NOTES`.
#: ``D`` — **the guard buys nothing.** The block names only the standard
#:     library, so there is nothing to defer.
#: ``E`` — **first-party, cheap, inert.** The block names only first-party
#:     modules and the standard library, and converting it breaks no import.
#: ``F`` — **the family.** The block resolves the relational contract, and
#:     :data:`DEFERRED` owns it: the deferral names the task that converts it.
#:
#: Five of the six are **mechanical** properties of the block, and the checks
#: below enforce them: ``A`` and ``B`` are proved by converting the block in
#: a copy of the tree, ``D`` and ``E`` are read off the AST, and ``F`` is the
#: deferral map. ``C`` is the one **judgement** — it says the guard was
#: written on purpose, and the measured price of every block is recorded so
#: the judgement can be re-examined rather than taken on trust. The price
#: does not separate the two classes by itself: ``paa.models`` is classed
#: ``E`` and costs +15.1 ms to convert, more than four of the ten ``C``
#: members cost. What separates them is the *intent* the guard records.
TYPE_CHECKING_CLASSES: dict[str, str] = {
    "paa.agents.base": "C",
    "paa.agents.orchestrator": "C",
    "paa.api.app": "B",
    "paa.cli.main": "C",
    "paa.coder.agent": "C",
    "paa.coder.model_adapter": "E",
    "paa.coder.runtime_tools": "C",
    "paa.context.budget": "D",
    "paa.daemon": "C",
    "paa.improvement.reflection": "C",
    "paa.memory.creator": "E",
    "paa.memory.world_model": "D",
    "paa.models": "E",
    "paa.models.embeddings": "C",
    "paa.models.registry": "E",
    "paa.models.router": "C",
    "paa.observability.logging": "E",
    "paa.observability.runs": "F",
    "paa.observability.tracing": "E",
    "paa.runtime": "C",
    "paa.skills.adapters.base": "E",
    "paa.skills.adapters.claw_hub": "E",
    "paa.skills.adapters.mcp": "E",
    "paa.skills.adapters.native": "E",
    "paa.skills.usa": "E",
    "paa.storage.coldlake": "E",
    "paa.storage.coldlake.cas": "A",
    "paa.storage.graph": "E",
    "paa.storage.relational.connection": "D",
    "paa.storage.relational.postgres": "D",
    "paa.storage.vector": "E",
    "paa.storage.vector.numpy_backend": "E",
    "paa.storage.vector.qdrant_backend": "E",
}

#: The classes a block can be in. A class with no member is a class the tree
#: has outgrown, and the check below refuses one.
TYPE_CHECKING_CLASS_KEYS: frozenset[str] = frozenset({"A", "B", "C", "D", "E", "F"})

#: Why each block is in the class it is in — the heavy module it keeps out, the
#: standard library it defers for nothing, the cycle it closes.
#:
#: The prices are **measured**: each block was converted to a runtime import
#: in a *copy* of the tree, and the importing module's own import time was
#: timed in a fresh interpreter, five times, with the minimum taken (minimum
#: rather than mean, because a scheduler hiccup can only ever add). The
#: measurement is from one host on 2026-10-22 and the numbers move with the
#: machine; the *class* does not depend on the number, and a reader who
#: disagrees with a class can re-run the measurement rather than argue with
#: the table.
TYPE_CHECKING_NOTES: dict[str, str] = {
    "paa.agents.base": "names paa.ledger.store, which it does not otherwise import: +39.7 ms",
    "paa.agents.orchestrator": (
        "names paa.ledger.store, which the module already pulls in by another route: +1.1 ms"
    ),
    "paa.api.app": "names fastapi (the paa[api] extra) and paa.runtime: +176.9 ms",
    "paa.cli.main": (
        "names paa.runtime: +12.1 ms, and AGENTS.md forbids paa.runtime in a CLI that "
        "prints its version"
    ),
    "paa.coder.agent": "names paa.runtime among others: +11.4 ms",
    "paa.coder.model_adapter": "names paa.core.types and paa.models.router, both loaded: -1.8 ms",
    "paa.coder.runtime_tools": "names paa.runtime: +13.4 ms",
    "paa.context.budget": "names only collections.abc.Iterable",
    "paa.daemon": "names paa.runtime: +128.8 ms, the largest price in the tree",
    "paa.improvement.reflection": (
        "names paa.ledger.store, which it does not otherwise import: +7.5 ms"
    ),
    "paa.memory.creator": (
        "names paa.memory.contradiction, which it already imports at runtime: -0.5 ms"
    ),
    "paa.memory.world_model": "names only datetime.datetime",
    "paa.models": "names paa.config and paa.ledger.store: +15.1 ms",
    "paa.models.embeddings": "names paa.storage.vector.embeddings: +48.0 ms",
    "paa.models.registry": "names paa.config.ModelSettings: +0.2 ms",
    "paa.models.router": "names paa.ledger.store, which it does not otherwise import: +14.4 ms",
    "paa.observability.logging": "names paa.config, already loaded: -0.1 ms",
    "paa.observability.runs": (
        "the eighth member of the contract's family; T-008 (wave 2) owns the file and "
        "converts the block there"
    ),
    "paa.observability.tracing": "names paa.config and paa.observability.metrics: +0.9 ms",
    "paa.runtime": "names paa.models.router, which it does not otherwise import: +32.7 ms",
    "paa.skills.adapters.base": "names paa.sandbox.base, paa.skills.contracts and secrets: +2.5 ms",
    "paa.skills.adapters.claw_hub": "names paa.sandbox.base and paa.skills.adapters.base: +0.7 ms",
    "paa.skills.adapters.mcp": "names collections.abc and paa.sandbox.base: -0.5 ms",
    "paa.skills.adapters.native": "names the sandbox, the adapters and the contracts: -0.4 ms",
    "paa.skills.usa": "names the sandbox, the contracts, the registry and the secrets: +2.2 ms",
    "paa.storage.coldlake": "names paa.config.StorageSettings, already loaded: -4.3 ms",
    "paa.storage.coldlake.cas": (
        "names paa.storage.coldlake.base.BlobStore, and base.py imports this module for "
        "BlobRef at runtime, so a runtime import here closes the cycle"
    ),
    "paa.storage.graph": "names paa.config.StorageSettings, already loaded: -0.4 ms",
    "paa.storage.relational.connection": "names only collections.abc.{Coroutine,Iterable,Sequence}",
    "paa.storage.relational.postgres": "names only pathlib.Path",
    "paa.storage.vector": "names paa.config, already loaded: -0.7 ms",
    "paa.storage.vector.numpy_backend": "names paa.config, already loaded: +1.9 ms",
    "paa.storage.vector.qdrant_backend": "names paa.config, already loaded: +0.1 ms",
}

#: Modules that cannot import in a minimal install because they need an
#: optional extra, and the packages that extra provides.
#:
#: A missing extra is an environment fact, not a defect, and the failure
#: names the extra — so it is tolerated, and only for these modules. A module
#: outside this list that fails to import is a failure.
#:
#: The mapping is **checkable**, and that is the point. An entry that names an
#: extra the module does not need is a claim about the tree that nothing
#: verifies, and a list of claims nobody verifies is a list nobody trusts —
#: it is exactly how three of the four entries this list used to carry came to
#: be false. ``test_every_tolerated_module_really_needs_its_extra`` blocks each
#: entry's packages and asserts the module then fails to import, so an entry
#: that stops being true goes red instead of sitting there.
#:
#: Measured, by blocking the import rather than by reading the comment:
#: ``paa.api.app`` imports FastAPI inside ``create_app`` and not at module
#: scope, ``paa.storage.relational.postgres`` imports psycopg inside
#: ``_require_psycopg()``, and ``paa.storage.queue.redis_backend`` imports
#: redis inside ``_redis_asyncio()`` — all three import cleanly with the
#: extra blocked. Only the kuzu backend raises at import time, because it
#: needs the driver to build its store.
OPTIONAL_EXTRA_MODULES: dict[str, frozenset[str]] = {
    "paa.storage.graph.kuzu_backend": frozenset({"kuzu"}),
}

#: How long one module may take to import before the check calls it hung.
IMPORT_TIMEOUT_SECONDS = 120

#: How long the whole package may take to import in one interpreter before the
#: aggregate check calls it hung. Longer than :data:`IMPORT_TIMEOUT_SECONDS`
#: because the aggregate imports *every* module, not one — and it is a
#: separate constant because the two timeouts answer different questions: one
#: is "this module hung", the other is "the package hung". A failure message
#: that names the wrong one would send a reader to the wrong module.
AGGREGATE_IMPORT_TIMEOUT_SECONDS = 300

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

#: A second probe, run once per *tolerated* module, in a fresh interpreter
#: where some top-level packages have been made unimportable. It answers the
#: question the tolerance list exists to answer: **does this module really
#: need the extra it is listed under?** The packages are blocked by a
#: ``meta_path`` finder, so the block is total — no ``sys.path`` trick, no
#: uninstalling anything, and the answer does not depend on what this host
#: happens to have installed. Same three tab-separated fields, same contract.
_BLOCKED_PROBE = """\
import importlib
import importlib.abc
import sys

blocked = set(sys.argv[2].split(","))


class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise ModuleNotFoundError("No module named %r" % fullname, name=fullname)
        return None


sys.meta_path.insert(0, Blocker())
name = sys.argv[1]
try:
    importlib.import_module(name)
except BaseException as exc:
    print("FAIL\\t{}\\t{}".format(type(exc).__name__, str(exc).replace("\\n", " ")))
    raise SystemExit(0)
print("OK\\timported\\t{}".format(name))
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


def _read_probe(module: str, completed: subprocess.CompletedProcess[str]) -> Probe:
    """Turn one probe's output into a :class:`Probe`.

    Both probes speak the same three-field protocol — ``status``, ``detail``,
    ``message``, tab-separated, one line — so the reading is shared and the
    two scripts cannot drift apart in how they report.
    """
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


def _probe(
    module: str,
    *,
    extra_path: Path | None = None,
    cwd: Path | None = None,
) -> Probe:
    """Import one module in a fresh interpreter and report what happened.

    ``extra_path`` is prepended to ``PYTHONPATH`` for the child, which is how
    the cycle fixture below is made importable without the repository
    containing a deliberate cycle.

    ``cwd`` is the child's working directory, and it exists for the same
    reason: a child started in a directory that *contains* a ``paa`` package
    would import that one instead of the copy on ``PYTHONPATH``, and the
    copy is the thing under test. The default is the parent's, which is what
    every other probe wants.
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
            cwd=None if cwd is None else str(cwd),
        )
    except subprocess.TimeoutExpired:
        return Probe(
            module=module,
            ok=False,
            bound=False,
            error_type="TimeoutExpired",
            error_message=f"import did not finish within {IMPORT_TIMEOUT_SECONDS}s",
        )

    return _read_probe(module, completed)


def _probe_without(
    module: str,
    packages: frozenset[str],
    *,
    extra_path: Path | None = None,
    cwd: Path | None = None,
) -> Probe:
    """Import one module with some top-level packages made unimportable.

    This is how the tolerance list is checked against the tree: the packages
    an entry names are blocked, and the module must then fail to import — or
    the entry is describing a constraint the module does not have.

    ``extra_path`` and ``cwd`` have the same meaning as in :func:`_probe`:
    they make the probe run against a *copy* of the package rather than the
    one in the repository.
    """
    env: dict[str, str] | None = None
    if extra_path is not None:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(extra_path) + (os.pathsep + existing if existing else "")

    try:
        completed = subprocess.run(
            [sys.executable, "-c", _BLOCKED_PROBE, module, ",".join(sorted(packages))],
            capture_output=True,
            text=True,
            timeout=IMPORT_TIMEOUT_SECONDS,
            env=env,
            cwd=None if cwd is None else str(cwd),
        )
    except subprocess.TimeoutExpired:
        return Probe(
            module=module,
            ok=False,
            bound=False,
            error_type="TimeoutExpired",
            error_message=f"import did not finish within {IMPORT_TIMEOUT_SECONDS}s",
        )

    return _read_probe(module, completed)


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

    The half covers every module that **mentions** the contract, in *any*
    import form, and not only the ones that import it at runtime. That is
    what gives it teeth: on a tree where the conversion has been reverted,
    every one of the seven siblings mentions the contract behind
    ``if TYPE_CHECKING:``, and a runtime-only reading of this half would skip
    all of them and pass. Asking "does this module hold the class?" of every
    module that names it asks the question that does not change its answer
    when the import moves.
    """
    unbound: list[str] = []
    for name, path in _module_files():
        runtime, type_checking = _imports_of(path)
        mentions = (CONTRACT_MODULE, CONTRACT_NAME)
        if mentions not in runtime and mentions not in type_checking:
            continue
        if name in DEFERRED:
            continue
        probe = probes[name]
        assert probe.ok, (
            f"{name} imports the contract at runtime but does not import: {probe.describe()}"
        )
        if not probe.bound:
            unbound.append(name)

    assert unbound == [], (
        f"{unbound} mention {CONTRACT_NAME} but do not bind it in their namespace — "
        "the contract is resolved lazily there, or the import is not reaching the "
        "module's own globals."
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
    """A deferral without an owner is a drift nobody is coming back to.

    And a deferral whose owner has **landed** is the same drift, one task
    later. The second assertion is the one that makes the map self-invalidating:
    a deferred module must still resolve the contract behind
    ``if TYPE_CHECKING:``, so the moment its owner converts it the entry stops
    being true, this check goes red, and the entry has to be deleted in the
    same change that made it unnecessary. Without it, the map keeps saying
    "T-008 owns this" about a file that T-008 finished three waves ago, and
    the lock — the artefact whose whole job is to keep the convention true —
    does not notice.
    """
    known = {name: path for name, path in _module_files()}
    for module, reason in DEFERRED.items():
        assert reason.strip(), f"{module} is deferred without a reason"
        assert module in known, f"{module} is deferred but no such module exists"
        assert re.search(r"T-\d{3}", reason), (
            f"{module} is deferred without naming a task that owns it: {reason!r}"
        )
        _, type_checking = _imports_of(known[module])
        assert (CONTRACT_MODULE, CONTRACT_NAME) in type_checking, (
            f"{module} is deferred but no longer resolves {CONTRACT_NAME} behind "
            "`if TYPE_CHECKING:` — the deferral has been acted on, so the entry "
            "is stale and has to be deleted with the change that made it "
            "unnecessary."
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
    unexpected = {probe.module for probe in tolerated} - set(OPTIONAL_EXTRA_MODULES)
    assert not unexpected, (
        f"{sorted(unexpected)} do not import, and the failure is not an optional extra "
        "this install happens to lack"
    )


def test_the_sweep_covers_every_module_under_the_package(
    probes: dict[str, Probe],
) -> None:
    """The sweep's coverage is **counted**, not claimed.

    The lock's own prose used to say "every module under ``src/paa``", and the
    implementer's report turned that into a number — "137/137 modules import" —
    which was wrong three ways: 137 is the number of *files*, 136 is the
    number of *modules* the sweep probes, and one of those 136 does not import
    in a minimal install. A number in prose is a number that drifts; this
    assertion is a number that cannot.

    The package's own ``__init__.py`` is the one file that is not a probe
    *target*, and deliberately so: every probe imports it as the parent
    package, so it is exercised by all of them, and probing it separately
    would only assert that ``import paa`` works — which every probe already
    does. Everything else under the package is a target, and the count says
    so.
    """
    package_root = PACKAGE_ROOT / "__init__.py"
    source_files = set(PACKAGE_ROOT.rglob("*.py"))
    targets = {path for _, path in _module_files()}

    assert targets == source_files - {package_root}, (
        "the sweep does not cover the package's modules exactly: "
        f"{sorted(str(p) for p in source_files - targets - {package_root})} are not "
        f"targets, and {sorted(str(p) for p in targets - source_files)} are not files."
    )
    assert len(targets) == len(source_files) - 1
    assert len(probes) == len(targets), (
        f"{len(probes)} probes ran for {len(targets)} modules — a module was not "
        "probed, so the sweep's coverage is smaller than it claims."
    )


def _as_text(raw: bytes | str | None) -> str:
    """A child's captured output as text, whichever form it arrives in.

    ``subprocess.run`` re-raises ``TimeoutExpired`` with whatever the pipe
    reader had collected — and on POSIX that is **bytes**, even when the call
    asked for text, because the decoding happens in ``Popen.communicate`` and
    the timeout path never gets there. So the form is checked rather than
    assumed: a caller that assumed ``str`` would raise ``AttributeError``
    inside the very handler that exists to report a hung child.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _last_module_started(output: str) -> str:
    """The last module the aggregate child said it was about to import.

    The child announces each import before it makes it, so the last
    announcement is the module that was in flight when the child died — which
    is the only thing that makes a timeout actionable. "The aggregate import
    timed out" names a symptom; "it hung on ``paa.storage.vector``" names a
    module.
    """
    started = [
        line.split("\t", 1)[1]
        for line in output.splitlines()
        if line.startswith("importing\t") and "\t" in line
    ]
    if not started:
        return "none — the child announced nothing"
    return started[-1]


def _aggregate_import(script: str) -> str:
    """Import the whole package in one interpreter and say what happened.

    Returns an **empty string** when the package imported as a whole, and a
    verdict otherwise.

    A **hung** child is a verdict, not an error. The timeout exists to catch
    a hang, and an uncaught ``TimeoutExpired`` reports the hang as a stack
    trace with the reader left guessing which module hung. So the child
    announces every import it starts, and this names the timeout, the module
    it was on, and whatever the child had written to stderr.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=AGGREGATE_IMPORT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as expired:
        return (
            f"the package did not import as a whole within "
            f"{AGGREGATE_IMPORT_TIMEOUT_SECONDS}s; the last module it started was "
            f"{_last_module_started(_as_text(expired.stdout))}. "
            f"The child wrote: {_as_text(expired.stderr).strip()[-2000:]}"
        )
    if completed.returncode != 0:
        return (
            "the package does not import as a whole; the last module it started was "
            f"{_last_module_started(completed.stdout)}:\n"
            + completed.stderr.strip()[-4000:]
        )
    return ""


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
    script = "\n".join(
        f"print('importing\\t{name}', flush=True)\nimport {name}" for name in importable
    )
    verdict = _aggregate_import(script)
    assert verdict == "", verdict


def test_a_hung_aggregate_import_is_a_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hang is reported as a verdict, naming the timeout and the module.

    The timeout exists to catch a hang. An uncaught ``TimeoutExpired`` reports
    the hang as a stack trace with the reader left guessing which module hung
    — so the timeout is shortened to a second here, a child that really does
    sleep is handed to the aggregate, and the verdict has to name both the
    timeout and the module it was on.

    The child is a real hang, produced by a real ``sleep``, not a mock of the
    probe: the thing being tested is what the parent does with a child that
    stops answering, and a mock would test the mock.

    The timeout is two seconds rather than one because the verdict can only
    name the module if the child **started** — it announces each import
    before it makes it, so a child that never got that far is honestly
    reported as "the child announced nothing" instead of being blamed on a
    module it never reached. Two seconds is three orders of magnitude more
    than the interpreter's startup cost on this host, which is what makes the
    announcement a fact rather than a race.
    """
    monkeypatch.setattr(sys.modules[__name__], "AGGREGATE_IMPORT_TIMEOUT_SECONDS", 2)

    verdict = _aggregate_import(
        "print('importing\\tpaa.sleeper', flush=True)\nimport time\ntime.sleep(30)"
    )

    assert verdict != "", "a hung child produced no verdict at all"
    assert "2s" in verdict, f"the verdict does not name the timeout: {verdict}"
    assert "paa.sleeper" in verdict, f"the verdict does not name the module: {verdict}"


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


# ---------------------------------------------------------------------------
# The annotations, read at runtime
# ---------------------------------------------------------------------------


def _names_the_contract(callable_: object) -> bool:
    """Whether a callable's own annotations mention the contract by name.

    Under ``from __future__ import annotations`` the annotations are strings,
    so this is a search of the source text of the annotation, not of a type.
    """
    annotations = getattr(callable_, "__annotations__", {})
    if not isinstance(annotations, dict):
        return False
    return any(CONTRACT_NAME in str(value) for value in annotations.values())


def _annotation_arguments(hints: dict[str, object]) -> list[object]:
    """The types named by a resolved hint mapping, flattened one level.

    A hint can be a single class or a union of them, and the only question
    this file asks about it is whether the contract is among the names.
    """
    arguments: list[object] = []
    for hint in hints.values():
        arguments.extend(getattr(hint, "__args__", (hint,)))
    return arguments


def test_the_family_can_have_its_annotations_read() -> None:
    """The family's annotations are **readable**, not just checkable.

    The first two halves of this lock say the contract is imported at runtime.
    This half says what that buys: the annotation can be *evaluated*. A name
    that is bound only for the type checker is absent from the module, and
    ``typing.get_type_hints`` — the call FastAPI's dependency resolution and
    pydantic's model construction both make — raises ``NameError`` on it.

    It fails if the conversion is reverted, because the reverted module names
    the contract and does not bind it, and the hint cannot be evaluated.
    """
    checked: set[str] = set()
    for name in FAMILY:
        if name in DEFERRED:
            continue
        module = importlib.import_module(name)
        for attribute, obj in vars(module).items():
            if not inspect.isclass(obj) or obj.__module__ != name:
                continue
            initialiser = obj.__init__
            if not _names_the_contract(initialiser):
                continue
            checked.add(name)
            hints = typing.get_type_hints(initialiser)
            assert CONTRACT_NAME in {
                getattr(argument, "__name__", "") for argument in _annotation_arguments(hints)
            }, (
                f"{name}.{attribute}.__init__ names {CONTRACT_NAME} in its annotation "
                f"but the resolved hints do not mention it: {hints}"
            )

    assert checked == {name for name in FAMILY if name not in DEFERRED}, (
        "no class in the family names the contract in its own annotations, so this "
        f"check is vacuous — it covered {sorted(checked)}"
    )


def test_the_contract_packages_factory_can_have_its_annotations_read() -> None:
    """The package that owns the contract can have **its own** annotations read.

    ``paa.storage.relational.get_database`` names the two backends it can
    hand back. While those names were bound only for the type checker, the
    annotation was a string that no runtime reader could evaluate:
    ``typing.get_type_hints(get_database)`` raised ``NameError: name 'Settings'
    is not defined``. The same defect class the lock exists to remove, one
    module over — in the package that owns the contract.

    The defect was latent: nothing in the tree resolved that function's
    annotations, so it never fired. It is a trap for the next person who puts
    the factory behind a FastAPI ``Depends()``, which resolves a dependency's
    signature with ``get_type_hints`` at route-registration time.
    """
    from paa.storage.relational import Database, PostgresDatabase, get_database

    hints = typing.get_type_hints(get_database)
    assert hints["return"] == Database | PostgresDatabase
    assert "settings" in hints


def test_the_relational_package_imports_without_the_postgres_extra() -> None:
    """The contract's own package imports on a machine with no ``postgres`` extra.

    This is the property the package's docstring used to get wrong. The old
    wording said the server backend was imported lazily *"so a laptop install
    without the postgres extra (psycopg) stays importable"* — and the extra was
    never the constraint: ``paa.storage.relational.postgres`` imports psycopg
    inside ``_require_psycopg()``, so it imports on a laptop with or without
    the extra. The constraint was the **annotation**, and unbinding the name
    is what made it unreadable.

    The property is worth keeping and worth locking, because the next person
    to "tidy up" the lazy import by hoisting ``import psycopg`` to the module
    scope would break every laptop install — and this is the check that says
    so.
    """
    probe = _probe_without("paa.storage.relational", frozenset({"psycopg", "psycopg_pool"}))
    assert probe.ok, (
        "paa.storage.relational does not import without the postgres extra: "
        f"{probe.describe()}"
    )


def test_the_factory_builds_only_the_backend_it_was_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binding the name at module scope is not a licence to build the client.

    The factory is lazy about the **backend**, not about the import: a laptop
    install must never touch the server backend, and the factory stays
    side-effect-free — it returns an *unconnected* backend either way. The
    tripwire below is how that is asserted without a server: if the factory
    ever builds a ``PostgresDatabase`` when it was not asked for one, the
    tripwire is what it builds.
    """
    import paa.storage.relational as relational
    from paa.config import StorageSettings

    class Tripwire:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("the factory built a postgres client it was not asked for")

    assert hasattr(relational, "PostgresDatabase"), (
        "paa.storage.relational does not bind PostgresDatabase — the factory's "
        "return annotation names a class the module never binds, so the "
        "annotation cannot be read at runtime"
    )
    monkeypatch.setattr(relational, "PostgresDatabase", Tripwire)
    backend = relational.get_database(StorageSettings())
    assert isinstance(backend, relational.Database)


# ---------------------------------------------------------------------------
# The tolerance list
# ---------------------------------------------------------------------------


def test_every_tolerated_module_really_needs_its_extra() -> None:
    """A tolerated module is one that **cannot** import without its extra.

    The tolerance list is the only place in this lock where a *failure* is
    allowed, so it has to be a list of facts about the tree rather than a list
    of assumptions about it. Each entry's packages are blocked in a fresh
    interpreter and the module must then fail to import; an entry that names
    an extra the module does not need is a claim nobody checks, and that is
    how three of the four entries this list used to carry came to be false.

    The converse is checked by the sweep itself: a module that is *not* on
    the list and fails to import fails the sweep.
    """
    for module, packages in OPTIONAL_EXTRA_MODULES.items():
        probe = _probe_without(module, packages)
        assert not probe.ok, (
            f"{module} is listed as needing {sorted(packages)}, but it imports "
            f"cleanly without them — the entry describes a constraint the module "
            "does not have, and has to go"
        )


# ---------------------------------------------------------------------------
# The classification of the blocks that are left
# ---------------------------------------------------------------------------


def _type_checking_guards(tree: ast.Module) -> list[ast.If]:
    """Every ``if TYPE_CHECKING:`` block in the module, at any depth.

    The sweep is deliberately wider than "a module-level guard": a guard
    nested inside a function or a class still defers an import, and a
    classification table that only saw the module-level ones would be
    incomplete against the tree it claims to describe.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and _is_type_checking_guard(node)
    ]


def _modules_with_type_checking_guards() -> set[str]:
    """The modules that still have a ``TYPE_CHECKING`` block.

    This is the sweep the classification table was derived from, and the
    check below compares the two, so the table is a fact about the tree
    rather than a claim about it.
    """
    found: set[str] = set()
    for name, path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _type_checking_guards(tree):
            found.add(name)
    return found


def _is_stdlib(module: str) -> bool:
    """Whether a dotted module path names the standard library."""
    return module.split(".")[0] in sys.stdlib_module_names


def _is_first_party(module: str) -> bool:
    """Whether a dotted module path names this project's own code.

    An empty path is a **relative** import — ``from . import x`` — which is
    this project's code by definition, and the AST records it as an empty
    module name.
    """
    return module == "" or module == PACKAGE_NAME or module.startswith(PACKAGE_NAME + ".")


def _guarded_modules(module: str) -> set[str]:
    """The modules a module's ``TYPE_CHECKING`` block imports."""
    for name, path in _module_files():
        if name != module:
            continue
        _, type_checking = _imports_of(path)
        return {imported for imported, _ in type_checking}
    return set()


def _third_party_packages(module: str) -> frozenset[str]:
    """The third-party packages a module's ``TYPE_CHECKING`` block names."""
    return frozenset(
        imported.split(".")[0]
        for imported in _guarded_modules(module)
        if not _is_stdlib(imported) and not _is_first_party(imported)
    )


def _relative_source_path(module: str) -> Path:
    """Where a module's source file lives, relative to the package root."""
    for name, path in _module_files():
        if name == module:
            return path.relative_to(PACKAGE_ROOT)
    raise KeyError(f"{module} is not a module of the package under the convention")


def _shadow_package(root: Path, module: str) -> Path:
    """A copy of the package, and the file in it that is about to be converted.

    The copy is what the probe imports. A conversion that breaks an import
    has to break it in a **copy**: the repository is not the thing under
    test — the tree would be, and the tree must not change.
    """
    package = root / PACKAGE_NAME
    shutil.copytree(PACKAGE_ROOT, package, ignore=shutil.ignore_patterns("__pycache__"))
    return package / _relative_source_path(module)


def _hoisted_source(source: str) -> str:
    """The module's source with its module-level ``TYPE_CHECKING`` guard removed.

    "Converting the block" means exactly this: the guarded imports become
    ordinary runtime imports. The result is written to a copy of the
    package, never to the repository — the point of the exercise is to watch
    what the tree *would* do, not to change what it does.
    """
    tree = ast.parse(source)
    body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            body.extend(node.body)
        else:
            body.append(node)
    tree.body = body
    return ast.unparse(tree)


def test_every_type_checking_block_is_classified() -> None:
    """The classification is a fact about the tree, not a claim about it.

    The table used to live in a paragraph in a run-scoped report — a
    directory the next run will not read. It lives here because it can be
    **checked**, and this is the check: the same AST sweep the table was
    derived from is run again, and the two sets have to be equal.

    So a new ``if TYPE_CHECKING:`` block has to be classified before the
    lock lets it through, and a block that is converted has to leave the
    table. Both directions are the same assertion, and a table that drifts
    from the tree in either direction is a failure with a message that
    names the modules on both sides.
    """
    guarded = _modules_with_type_checking_guards()
    classified = set(TYPE_CHECKING_CLASSES)

    assert classified == guarded, (
        "the classification and the tree disagree about which modules still have "
        f"an `if TYPE_CHECKING:` block.\n"
        f"  in the tree but not classified: {sorted(guarded - classified)}\n"
        f"  classified but no longer in the tree: {sorted(classified - guarded)}"
    )

    assert set(TYPE_CHECKING_NOTES) == classified, (
        "every classified block needs a note saying why it is in that class, and "
        "no note may outlive its block.\n"
        f"  without a note: {sorted(classified - set(TYPE_CHECKING_NOTES))}\n"
        f"  without a block: {sorted(set(TYPE_CHECKING_NOTES) - classified)}"
    )

    used = set(TYPE_CHECKING_CLASSES.values())
    unknown = sorted(used - TYPE_CHECKING_CLASS_KEYS)
    assert not unknown, f"the tree is classified with unknown class(es): {unknown}"

    empty = sorted(TYPE_CHECKING_CLASS_KEYS - used)
    assert not empty, (
        f"class(es) {empty} have no member left — the tree has outgrown them, so "
        "the classification has to say so"
    )


def test_the_classification_says_what_it_claims() -> None:
    """Each class is a property of the block it labels, and this enforces it.

    Five of the six classes are **mechanical**: what the block names decides
    the class, and the check reads the names off the AST. ``B`` is the block
    that names a third-party package — an optional extra of this project —
    and nothing else may name one. ``D`` is the block that names only the
    standard library, and nothing else may do that. ``F`` is the family, and
    the deferral map says which modules those are.

    ``C`` is the one judgement, and it is checked only for being a *known*
    class; what it asserts is the intent the guard records, and the measured
    price of every block is in :data:`TYPE_CHECKING_NOTES` so the judgement
    can be re-examined rather than taken on trust.
    """
    for name, _ in _module_files():
        klass = TYPE_CHECKING_CLASSES.get(name)
        if klass is None:
            continue
        guarded = _guarded_modules(name)
        third_party = sorted(
            imported
            for imported in guarded
            if not _is_stdlib(imported) and not _is_first_party(imported)
        )
        stdlib_only = bool(guarded) and all(_is_stdlib(imported) for imported in guarded)

        if klass == "B":
            assert third_party, (
                f"{name} is classified B (an optional extra) but names no "
                f"third-party package: {sorted(guarded)}"
            )
        else:
            assert not third_party, (
                f"{name} is classified {klass} but names the third-party "
                f"package(s) {third_party} — that is what class B is for"
            )

        if klass == "D":
            assert guarded, (
                f"{name} is classified D but has no `if TYPE_CHECKING:` block any "
                "more — the classification has to follow the tree"
            )
            assert stdlib_only, (
                f"{name} is classified D (the guard buys nothing) but names "
                f"{sorted(guarded)}, which is not the standard library"
            )
        else:
            assert not stdlib_only, (
                f"{name} is classified {klass} but names only the standard "
                f"library {sorted(guarded)} — that is what class D is for"
            )

    family = {name for name, klass in TYPE_CHECKING_CLASSES.items() if klass == "F"}
    assert family == set(DEFERRED), (
        "the family class and the deferral map disagree about which modules are "
        f"the family: {sorted(family)} vs {sorted(DEFERRED)}"
    )


def test_the_classified_cycle_is_still_a_cycle(tmp_path: Path) -> None:
    """The one block that cannot be converted, proven rather than asserted.

    A classification that says "this closes a cycle" is a claim until
    somebody converts the block and watches the import break. So this does
    exactly that, in a copy: the copy imports cleanly, the converted copy
    does not, and the failure reads as a cycle rather than as a missing
    module.

    The class is **self-invalidating**: the day the cycle is broken the
    import succeeds, this fails, and the table has to change — which is the
    point. A conversion that leaves a stale ``A`` behind is a table that has
    stopped describing the tree.
    """
    cycles = sorted(name for name, klass in TYPE_CHECKING_CLASSES.items() if klass == "A")
    assert cycles, "no block is classified A, so the cycle proof would prove nothing"

    for module in cycles:
        root = tmp_path / module.replace(".", "_")
        root.mkdir()
        target = _shadow_package(root, module)

        control = _probe(module, extra_path=root, cwd=root)
        assert control.ok, (
            f"the copy of the package does not import {module}, so converting the "
            f"block below would prove nothing: {control.describe()}"
        )

        target.write_text(
            _hoisted_source(target.read_text(encoding="utf-8")), encoding="utf-8"
        )
        converted = _probe(module, extra_path=root, cwd=root)
        assert not converted.ok, (
            f"{module} is classified A (a real import cycle) but it imports "
            "cleanly with the block converted — the cycle is gone, so the "
            "classification has to change"
        )
        assert any(marker in converted.error_message for marker in _CYCLE_MARKERS), (
            f"{module} is classified A, but converting its block fails for another "
            f"reason: {converted.describe()}"
        )


def test_the_classified_extra_still_needs_the_extra(tmp_path: Path) -> None:
    """The block that keeps a module importable without the extra.

    Converting it makes the module need the extra at module scope, so the
    proof runs the other way round: with the extra blocked the module still
    imports — that is what the guard is for — and with the block converted
    and the extra blocked it does not.
    """
    extras = sorted(name for name, klass in TYPE_CHECKING_CLASSES.items() if klass == "B")
    assert extras, "no block is classified B, so the extra proof would prove nothing"

    for module in extras:
        root = tmp_path / module.replace(".", "_")
        root.mkdir()
        target = _shadow_package(root, module)
        packages = _third_party_packages(module)
        assert packages, f"{module} is classified B but names no third-party package"

        control = _probe_without(module, packages, extra_path=root, cwd=root)
        assert control.ok, (
            f"{module} is classified B (an optional extra) but it does not import "
            f"without {sorted(packages)}: {control.describe()}"
        )

        target.write_text(
            _hoisted_source(target.read_text(encoding="utf-8")), encoding="utf-8"
        )
        converted = _probe_without(module, packages, extra_path=root, cwd=root)
        assert not converted.ok, (
            f"{module} is classified B, but it imports without {sorted(packages)} "
            "even with the block converted — the extra is not what the guard is "
            "keeping out, so the classification has to change"
        )
