"""tdd-loop's own checks - run:  python -m pytest test_pipeline.py

The two proofs every scaffolded pipeline keeps green while editing (the
manifest still parses through the closed contract; the entrypoint still
answers the result contract, including the smoke probe's empty-args
shape), plus the two this worked example adds: the stage order and the
refactor knob's effect.

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
    assert declaration.name == "tdd-loop"
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


def test_every_cycle_runs_red_then_green_then_refactor() -> None:
    """The stage order the example exists to teach, pinned by its counts."""
    metrics = pipeline.run("add a retry to the client", {"max_cycles": 4})["metrics"]
    assert metrics["cycles"] == 4
    assert metrics["tests_written"] == 4  # RED: one failing test named per cycle
    assert metrics["refactors"] == 4  # strict default: REFACTOR every cycle
    assert metrics["duplication_removed"] == 3  # the fastest green duplicates


def test_the_refactor_knob_collapses_once_at_the_end_when_off() -> None:
    """A bool param with a metrics-visible effect, the tighten-only layer 1."""
    metrics = pipeline.run("add a retry to the client", {
        "max_cycles": 4,
        "strict_refactor": False,
    })["metrics"]
    assert metrics["refactors"] == 1
    assert metrics["duplication_removed"] == 3  # the same duplication, merged once
