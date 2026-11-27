"""The authoring scaffold — `paa pipeline new` (P7, W9-C9.5).

What this module is: the one place that renders the four files a new
pipeline starts from, and refuses to name a directory it will not write.
The files it writes are not documentation *about* the contract — they
are the contract, in the smallest shape that already runs:

* ``pipeline.toml`` — a manifest the closed parser
  (:func:`paa.pipelines.registry.declaration_from_manifest`) accepts as
  written, pinned by the parse-back lock at the bottom of
  :func:`scaffold_pipeline`: the scaffold never reports success on a
  manifest its own registry would name in ``local_errors``;
* ``pipeline.py`` — the entrypoint skeleton for the shape the tree
  actually runs today, the **sandboxed entrypoint contract** of
  :mod:`paa.pipelines.sandboxed` (``--paa-args`` JSON in, one JSON
  result object on stdout), with the fill-in sections marked;
* ``README.md`` — the author's own door map: the three doors, the
  six-check standard, where the limits and the evidence tier live;
* ``test_pipeline.py`` — the checks the author keeps green while
  editing, written against the real parser and the real result reader
  (``run_result_shape``), never a copy of either.

The target decision, stated once: **the local shape.** The default
parent is ``PAA_HOME/pipelines`` — the directory
:class:`paa.pipelines.registry.PipelineRegistry` scans — so
``paa pipeline new my-loop`` is followed immediately by
``paa pipeline list`` seeing the row (trust ``local``, listed and
inspected). That is the zero-infrastructure start the spec's
distribution order names first. The same directory is also a valid
``paa pipeline install`` source (the package flow reads any local
directory), which is the door that makes the pipeline *runnable*: the
install's five gates, six-check conformance run and capability review
stage the bytes under ``installed/<name>/<version>/``, where the
registry row is runnable through the sandboxed adapter. A scaffold
placed under ``--path`` anywhere else keeps both doors — list needs the
directory moved (or symlinked) under the local dir, install accepts it
where it stands.

What v1 deliberately does **not** scaffold (the honest boundary, mirrored
in ``docs/modules/authoring.md``): no TypeScript SDK — the spec's P7
list carries one, and the first non-Python language proof is the RPC
transport's row (W9-C9.3); no slot wiring beyond the commented example;
no capability ask beyond the empty set — the manifest teaches the
vocabulary and asks for nothing, because every key asked for is surface
a human reviews.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from paa.core.errors import PaaError
from paa.pipelines.base import FACADE_RUNTIME_VERSION
from paa.pipelines.registry import (
    INSTALLED_DIR_NAME,
    LocalManifestError,
    declaration_from_manifest,
)

__all__ = [
    "SCAFFOLD_FILES",
    "ScaffoldError",
    "ScaffoldReport",
    "scaffold_pipeline",
]

#: The four files a scaffold writes, in the order the README explains
#: them. A scaffold never writes anything else, and ``--force`` never
#: overwrites anything else: the list is the whole of both promises.
SCAFFOLD_FILES = (
    "pipeline.toml",
    "pipeline.py",
    "README.md",
    "test_pipeline.py",
)

#: The version a fresh scaffold declares. ``0.1.0`` — the first
#: component is the author's to move, and a scaffold that shipped
#: ``1.0.0`` would be claiming a stability nobody has earned yet.
SCAFFOLD_VERSION = "0.1.0"

#: The closed name rule: a pipeline name becomes a directory name under
#: the local dir, so it must be safe as one on every platform the tree
#: runs on — no separators, no ``.``/``..`` prefixes, bounded length —
#: and it must stay inside the charset the registry's name/directory
#: match can compare verbatim.
_NAME_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: The sentinel every template carries where the pipeline's name lands.
#: A plain replace rather than ``str.format`` because the templates are
#: full of literal braces (TOML inline tables, JSON examples) that a
#: format call would try to interpret.
_NAME_SENTINEL = "__PIPELINE_NAME__"


class ScaffoldError(PaaError):
    """A scaffold could not be written: a named refusal, before any file.

    Every refusal names the reason — a bad name, a taken target, a
    ``--force`` that would clobber somebody else's pipeline — so the
    operator never has to guess which of the three went wrong. The
    scaffold's own exit contract (0 created, 1 refused, 2 usage) maps
    this class to 1.
    """

    wire_code = -32016


@dataclass(frozen=True, slots=True)
class ScaffoldReport:
    """What one successful scaffold wrote, and where.

    ``files`` is exactly :data:`SCAFFOLD_FILES` — the report is the
    receipt a CLI prints, so it cannot claim a file the scaffold did
    not write.
    """

    name: str
    directory: Path
    files: tuple[str, ...]


def _runtime_range() -> str:
    """The manifest's ``requires_runtime``, derived from the live facade.

    ``>=X.Y,<X+1`` over :data:`FACADE_RUNTIME_VERSION`, so the scaffold
    writes a range the running runtime satisfies *by construction* — a
    scaffold that pinned yesterday's range would be born off-contract
    the day the facade moves.
    """
    parts = FACADE_RUNTIME_VERSION.split(".")
    major = parts[0]
    minor = parts[1] if len(parts) > 1 else "0"
    return f">={major}.{minor},<{int(major) + 1}"


_MANIFEST_TEMPLATE = """\
# The pipeline manifest — the closed contract ADR-0052 pins and the one
# parser the tree runs (paa.pipelines.registry.declaration_from_manifest).
# Anything outside these sections refuses at load, by name, before any
# code runs. `paa pipeline list` reads this file as it stands.

[pipeline]
name        = "__PIPELINE_NAME__"
version     = "0.1.0"
description = "One sentence: what this loop does."
# The facade's version range, never the product's. The scaffold derives
# it from the running runtime, so it is satisfied as written.
requires_runtime = "{runtime_range}"
protocol    = "paa-pipeline/1"

[run]
# v1's execution shape is the sandboxed entrypoint contract
# (paa.pipelines.sandboxed.py): the host runs `python pipeline.py
# --paa-args <args.json>` under the sandbox. `resume` is the one fact
# this section carries today:
#   "journal" - the call journal replays a crashed run (the code default)
#   "none"    - the pipeline cannot honour replay; the host refuses to
#               mark such a run recoverable (the adapter shape)
resume = "journal"

[capabilities]
# What this pipeline asks for; the mandate decides what it gets. The
# declarable keys are the closed set: think, use_skill, recall,
# remember, call_agent, sub_run, commit. Ask for nothing until you need
# it - every key here is surface a human reviews at install time.
# think     = { roles = ["worker"] }
# use_skill = ["read_file", "bash"]

[limits]
# All four, concrete positive ints - an unbounded manifest is off
# contract. These are the host's enforcement knobs; an overlay may
# tighten them per run, never widen.
max_tokens   = 200000
max_wall_s   = 1800
max_calls    = 400
max_sub_runs = 2

[params]
# Layer-1 customisation: typed, ranged, `--set`-able, tighten-only.

[params.max_cycles]
type = "int"
default = 3
min = 1
max = 20
description = "Loop cycles the entrypoint runs."

[evidence]
# A claim, not a grant - the host recomputes what a run actually earned.
# "unrated" is the honest value until a run has evidence behind it.
declares_tier = "unrated"

# Layer 2, when a part of the loop should be swappable without forking:
#
# [slots.critic]
# interface = "agent/1"
# default   = "agent:critic"
#
# The interface must be `<name>/<major>` and the default must resolve to
# one of agent:, builtin:, skill: or out: - both are refused at load.
"""

_ENTRYPOINT_TEMPLATE = '''\
"""__PIPELINE_NAME__ - the pipeline entrypoint (scaffolded by `paa pipeline new`).

THE CONTRACT (paa/pipelines/sandboxed.py, pinned v1). The host runs:

    python pipeline.py --paa-args <args.json>

* the args file holds {"goal": str, "params": {...}}; the install-time
  smoke probe holds {} - survive BOTH shapes;
* stdout must end with ONE JSON object, exactly this shape:

      {"status": "completed" | "failed" | "needs_human",
       "summary": "one line; the host keeps 200 chars",
       "metrics": {"cycles": 1}}

  The status vocabulary is closed. Anything else - no JSON, an unknown
  status, a non-zero exit - is a FAILED run by rule, never a parse
  error the child chooses. Report a failure as status "failed" and
  exit 0.
* execution is sandboxed: no network, scratch-only filesystem, the
  staged directory read-only, wall-clock and memory caps;
* determinism is the contract: the same (goal, params) must produce the
  same result - the double-run conformance check fails anything else.
  No clock, no random, no environment reads.

Fill in the marked sections; delete the demo behaviour.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def run(goal: str, params: dict[str, Any]) -> dict[str, Any]:
    """One run of the pipeline: the result object the contract names.

    Pure and deterministic - the same (goal, params) in, the same result
    out. The test skeleton drives this function in-process; main()
    drives it under the sandbox.
    """
    max_cycles = int(params.get("max_cycles", 3))

    # -- FILL IN: stage 1 - read the goal, decide the first move -------
    steps: list[str] = []

    # -- FILL IN: the loop - replace the demo cycle with the real one --
    for cycle in range(1, max_cycles + 1):
        steps.append(f"cycle {cycle}: worked on {goal or 'the empty probe'}")

    # -- FILL IN: stage last - decide the terminal status --------------
    return {
        "status": "completed",
        "summary": f"ran {len(steps)} cycle(s) on the goal",
        "metrics": {"cycles": len(steps)},
    }


def main(argv: list[str] | None = None) -> int:
    """The host's door: --paa-args <args.json>, one JSON result on stdout."""
    args = list(sys.argv[1:] if argv is None else argv)
    goal = ""
    params: dict[str, Any] = {}
    if "--paa-args" in args:
        path = Path(args[args.index("--paa-args") + 1])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            goal = str(payload.get("goal") or "")
            if isinstance(payload.get("params"), dict):
                params = payload["params"]
    print(json.dumps(run(goal, params), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_TEST_TEMPLATE = '''\
"""__PIPELINE_NAME__'s own checks - run:  python -m pytest test_pipeline.py

The two proofs an author keeps green while editing:

1. the manifest still parses through the closed contract - the same
   parse `paa pipeline list` and `paa pipeline install` run; and
2. the entrypoint still answers the result contract, including the
   smoke probe's empty-args shape.

Deliberately plain: stdlib plus paa, and NO subprocess and NO importlib
- every .py file in this directory is AST-scanned at install time, and
those modules are denied in a sandboxed artifact. The entrypoint is
imported in-process instead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pipeline  # noqa: E402 - the entrypoint under test, beside this file

_STATUSES = ("completed", "failed", "needs_human")


def test_the_manifest_parses_through_the_closed_contract() -> None:
    from paa.pipelines.registry import declaration_from_manifest

    declaration = declaration_from_manifest(HERE / "pipeline.toml")
    assert declaration.name == "__PIPELINE_NAME__"
    # All four [limits] are concrete positive ints - the parse above
    # already refuses anything else; this keeps the intent visible.
    assert declaration.limits.max_calls >= 1
    assert declaration.evidence_tier in ("T1", "T2", "T3", "unrated")


def test_a_run_answers_the_result_contract() -> None:
    result = pipeline.run("a probe goal", {"max_cycles": 2})
    assert result["status"] in _STATUSES
    assert isinstance(result["summary"], str)
    assert isinstance(result["metrics"], dict)


def test_the_entrypoint_survives_the_smoke_probe_shape(tmp_path, capsys) -> None:
    from paa.pipelines.sandboxed import run_result_shape

    args = tmp_path / "args.json"
    args.write_text(json.dumps({}), encoding="utf-8")
    assert pipeline.main(["--paa-args", str(args)]) == 0
    payload = run_result_shape(capsys.readouterr().out)
    assert payload is not None
    assert payload["status"] in _STATUSES
'''

_README_TEMPLATE = """\
# __PIPELINE_NAME__ (a scaffolded pipeline)

Four files, one contract:

| file | what it is |
|---|---|
| `pipeline.toml` | the declaration — identity, resume, the capabilities ask, limits, params, tier |
| `pipeline.py` | the entrypoint the host runs under the sandbox |
| `test_pipeline.py` | the checks you keep green while editing |
| `README.md` | this file |

## The three doors

1. **List and inspect** — the registry reads this directory as it stands:

       paa pipeline list
       paa pipeline inspect __PIPELINE_NAME__

   The row shows trust `local`: listed and inspected, not executed.
   That is the boundary of a local manifest — running is what the next
   door buys.

2. **Test while you edit** — from this directory:

       python -m pytest test_pipeline.py

   The checks parse the manifest through the closed contract and drive
   the entrypoint against the result shape the host reads.

3. **Install** — the door that makes the pipeline runnable:

       paa pipeline install <this-directory> --allow-unverified

   Five gates (signature, content hash, AST scan, permissions, smoke),
   then the six-check conformance standard, then a capability review a
   human answers at a prompt. What lands is staged under
   `PAA_HOME/pipelines/installed/__PIPELINE_NAME__/0.1.0/` — a registry
   row the sandboxed adapter runs. `--allow-unverified` is the opt-in a
   local directory needs because it is unsigned; the other gates, the
   conformance run and the review still run.

## The six-check standard

Every install runs these against the staged bytes, and every registered
pipeline is held to the same page:

| check | what it means |
|---|---|
| double-run-sequence-equality | two runs of the same goal fold to the same call sequence |
| divergence-halt | a re-run that stops matching its journal halts loudly — never resyncs |
| unknown-outcome-never-auto-repeats | a call a crash left unclassified is never auto-repeated |
| cancel-within-grace | a cancelled run closes its rows coherently |
| budget-exhaustion | an exhausted clock fails the run by rule, not by crash |
| no-secrets-in-journal | the goal rides the fingerprint, never a payload text |

Determinism is the one of these your code owns: the same
`(goal, params)` must produce the same result. No clock, no random, no
environment reads inside `run()`.

## Where the knobs live

- **Limits** — `[limits]` in `pipeline.toml`: all four, concrete
  positive ints. An overlay may tighten them per run, never widen.
- **Evidence tier** — `[evidence] declares_tier`: a claim, not a grant;
  the host recomputes what a run earned. `unrated` until it did.
- **Params** — `[params]`: typed, ranged, `--set`-able at the run door,
  tighten-only.
- **Capabilities** — `[capabilities]`: the ask. Empty until you need
  one; every key you add is surface a human reviews at install.

## The honest boundaries (v1)

- A local manifest is listed and inspected, not run. Running is the
  install door's: staged bytes under the sandboxed adapter.
- The sandboxed entrypoint holds no facade tokens — `ctx.think` and the
  other capabilities are the RPC transport's work (the pipeline
  protocol's P3), landing on its own branch this wave.
- No TypeScript SDK yet; Python's entrypoint contract above is the
  whole surface today.
- `paa pipeline test`, the standalone conformance door, arrives with
  the same transport work; until then the install flow runs the six
  checks.
"""


def _check_name(name: str) -> None:
    """Refuse a name that cannot become a safe, matchable directory."""
    if not _NAME_SHAPE.match(name):
        raise ScaffoldError(
            f"pipeline name {name!r} is not scaffoldable: it must start with a "
            "letter or digit and carry only letters, digits, '.', '_' or '-', "
            "at most 64 characters",
            name=name,
        )
    if name == INSTALLED_DIR_NAME:
        raise ScaffoldError(
            "the name 'installed' is reserved: it is the installer's staging "
            "root (PAA_HOME/pipelines/installed/<name>/<version>), and a "
            "local manifest directory with that name would be read as the "
            "root itself",
            name=name,
        )


def _refuse_taken(name: str, target: Path) -> None:
    """The no-force refusal: name the target and everything it holds."""
    if target.is_dir():
        held = ", ".join(sorted(entry.name for entry in target.iterdir())) or "nothing"
        raise ScaffoldError(
            f"the scaffold target already exists: {target} (holds {held}); pass "
            "--force to overwrite the scaffold's own four files",
            name=name,
            target=str(target),
        )
    raise ScaffoldError(
        f"a file already exists at the scaffold target: {target}",
        name=name,
        target=str(target),
    )


def _check_force_target(name: str, target: Path) -> None:
    """The force guard: never clobber a directory that is not this pipeline's.

    ``--force`` overwrites the scaffold's own four files and nothing
    else — and it refuses outright when the directory carries a
    ``pipeline.toml`` that belongs to somebody else or that does not
    parse, because in both cases the operator's existing bytes are not
    this scaffold's to replace.
    """
    manifest = target / "pipeline.toml"
    if not manifest.is_file():
        return
    try:
        declaration = declaration_from_manifest(manifest)
    except LocalManifestError as exc:
        raise ScaffoldError(
            f"--force refuses to overwrite {manifest}: it does not parse "
            f"({exc.message}); remove it first if you are sure",
            name=name,
            target=str(target),
        ) from exc
    if declaration.name != name:
        raise ScaffoldError(
            f"--force refuses to overwrite {manifest}: it declares the "
            f"pipeline {declaration.name!r}, not {name!r}",
            name=name,
            target=str(target),
        )


def scaffold_pipeline(
    name: str, parent: Path, *, force: bool = False
) -> ScaffoldReport:
    """Write the four scaffold files into ``parent / name`` and prove them.

    The proof is the point: the manifest the scaffold just wrote is
    parsed back through the real closed parser before success is
    reported, so a scaffold can never announce a directory the registry
    would immediately name in ``local_errors``. The entrypoint and the
    test skeleton are not executed here — running the author's code is
    the install door's job, behind the gates.

    :param name: the pipeline's name; becomes the directory name, and
        must match the manifest's ``[pipeline] name`` for the registry
        to accept the row.
    :param parent: the directory to scaffold under. The CLI passes
        ``PAA_HOME/pipelines`` by default (the registry's local dir) or
        the operator's ``--path``.
    :param force: overwrite the scaffold's own four files when the
        target exists — never anything else, and never a directory
        whose manifest belongs to a different pipeline.
    :return: the report the CLI prints: what was written, and where.
    """
    _check_name(name)
    parent = Path(parent)
    if parent.exists() and not parent.is_dir():
        raise ScaffoldError(
            f"the scaffold parent is not a directory: {parent}", parent=str(parent)
        )
    target = parent / name
    if target.exists():
        if not force:
            _refuse_taken(name, target)
        _check_force_target(name, target)

    renders = {
        "pipeline.toml": _MANIFEST_TEMPLATE.replace(_NAME_SENTINEL, name).replace(
            "{runtime_range}", _runtime_range()
        ),
        "pipeline.py": _ENTRYPOINT_TEMPLATE.replace(_NAME_SENTINEL, name),
        "README.md": _README_TEMPLATE.replace(_NAME_SENTINEL, name),
        "test_pipeline.py": _TEST_TEMPLATE.replace(_NAME_SENTINEL, name),
    }
    parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(exist_ok=True)
    for filename in SCAFFOLD_FILES:
        (target / filename).write_text(renders[filename], encoding="utf-8")

    # The parse-back lock: success is only reported for a manifest the
    # one closed parser accepts, whose name matches its directory — the
    # same two facts the registry's local scan checks. A template that
    # drifted off contract fails here, loudly, instead of shipping a
    # scaffold that is born broken.
    try:
        declaration = declaration_from_manifest(target / "pipeline.toml")
    except LocalManifestError as exc:
        raise ScaffoldError(
            f"the scaffolded manifest failed its own closed contract: "
            f"{exc.message}",
            name=name,
            target=str(target),
        ) from exc
    if declaration.name != name or declaration.version != SCAFFOLD_VERSION:
        raise ScaffoldError(
            "the scaffolded manifest does not round-trip: it declares "
            f"{declaration.name!r} v{declaration.version!r} where the scaffold "
            f"wrote {name!r} v{SCAFFOLD_VERSION!r}",
            name=name,
            target=str(target),
        )
    return ScaffoldReport(name=name, directory=target, files=SCAFFOLD_FILES)
