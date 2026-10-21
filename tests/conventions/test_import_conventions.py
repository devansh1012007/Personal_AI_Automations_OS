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

The lock has six halves, and each fails for a different reason:

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
  set of facts about the tree and not a set of claims about it.

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

    return _read_probe(module, completed)


def _probe_without(module: str, packages: frozenset[str]) -> Probe:
    """Import one module with some top-level packages made unimportable.

    This is how the tolerance list is checked against the tree: the packages
    an entry names are blocked, and the module must then fail to import — or
    the entry is describing a constraint the module does not have.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _BLOCKED_PROBE, module, ",".join(sorted(packages))],
            capture_output=True,
            text=True,
            timeout=IMPORT_TIMEOUT_SECONDS,
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
