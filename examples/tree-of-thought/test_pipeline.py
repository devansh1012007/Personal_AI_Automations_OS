"""tree-of-thought's own checks - run:  python -m pytest test_pipeline.py

The two proofs every scaffolded pipeline keeps green while editing (the
manifest still parses through the closed contract; the entrypoint still
answers the result contract, including the smoke probe's empty-args
shape), plus the two this worked example adds: the counts follow the
knobs, and the selection is stable across identical runs.

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
    assert declaration.name == "tree-of-thought"
    # All four [limits] are concrete positive ints - the parse above
    # already refuses anything else; this keeps the intent visible.
    assert declaration.limits.max_calls >= 1
    assert declaration.evidence_tier in ("T1", "T2", "T3", "unrated")


def test_a_run_answers_the_result_contract() -> None:
    result = pipeline.run("a probe goal", {"depth": 2, "branching": 3})
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


def test_the_counts_follow_the_knobs() -> None:
    """The shape the example exists to teach, pinned by its counts."""
    metrics = pipeline.run("harden the parser", {"depth": 2, "branching": 3})["metrics"]
    assert metrics["depth"] == 2
    assert metrics["branching"] == 3
    assert metrics["proposals"] == 2 * 3  # PROPOSE: one set per round
    assert metrics["critiques"] == 2 * 3  # CRITIQUE: every candidate, every round
    assert 0 <= metrics["pruned"] <= 2 * 3  # SELECT: only survivors can win


def test_selection_is_stable_across_identical_runs() -> None:
    """The double-run rule at the author's own door.

    The whole result - chosen thought included - must be a pure function
    of (goal, params). The install-time conformance check drives this
    through the sandbox; this is the in-process proof an author runs
    after every edit.
    """
    first = pipeline.run("harden the parser", {"depth": 3, "branching": 4})
    second = pipeline.run("harden the parser", {"depth": 3, "branching": 4})
    assert first == second
    assert first["status"] == "completed"
