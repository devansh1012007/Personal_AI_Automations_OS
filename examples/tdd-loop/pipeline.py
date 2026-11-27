"""tdd-loop - the worked TDD example (the authoring kit's reference, P7).

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

WHAT IS REAL HERE, AND WHAT IS A STAND-IN. The loop's *stages* are the
example: red names the failing test before anything exists to pass it,
green writes the smallest change that could pass it, refactor merges
the duplication the fastest green leaves behind - in that order, every
cycle, with the counts in the metrics. The *work inside* each stage is
a deterministic stand-in (string work on the goal), because a sandboxed
entrypoint holds no facade tokens in v1: wiring `ctx.think` into a
stage is the RPC transport's row (the pipeline protocol's P3), not this
example's. Replace the stand-ins; keep the order and the determinism.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _slug(text: str) -> str:
    """The goal's words, lowercased and hyphen-joined - a stable name seed."""
    cleaned = "".join(c if c.isalnum() else " " for c in text.lower())
    return "-".join(cleaned.split()[:6]) or "the-goal"


def _red(goal: str, cycle: int) -> str:
    """RED - the failing test, named before anything exists to pass it."""
    return f"test_{_slug(goal)}_cycle_{cycle}: asserts the cycle-{cycle} slice of the goal"


def _green(goal: str) -> str:
    """GREEN - the smallest change that could turn the red test green.

    Deliberately the same handler every cycle: the fastest green for a
    growing test suite is one growing handler, and the duplication that
    leaves behind is what REFACTOR exists to merge.
    """
    return f"handle({_slug(goal)}): the smallest change that passes the red test"


def _refactor(implementation: list[str]) -> int:
    """REFACTOR - merge the duplicated handlers; returns how many went away.

    In place, because the list is this function's whole world. Order is
    preserved (first occurrence wins) so the merge is deterministic.
    """
    before = len(implementation)
    unique: list[str] = []
    for line in implementation:
        if line not in unique:
            unique.append(line)
    implementation[:] = unique
    return before - len(implementation)


def run(goal: str, params: dict[str, Any]) -> dict[str, Any]:
    """One run of the loop: red, green, refactor - ``max_cycles`` times.

    Pure and deterministic - the same (goal, params) in, the same result
    out. The test skeleton drives this function in-process; main()
    drives it under the sandbox.
    """
    max_cycles = int(params.get("max_cycles", 3))
    strict_refactor = bool(params.get("strict_refactor", True))

    subject = goal or "the empty probe"
    tests: list[str] = []
    implementation: list[str] = []
    refactors = 0
    duplication_removed = 0
    for cycle in range(1, max_cycles + 1):
        tests.append(_red(subject, cycle))  # RED: name the failure first
        implementation.append(_green(subject))  # GREEN: smallest change
        if strict_refactor or cycle == max_cycles:  # REFACTOR: merge
            duplication_removed += _refactor(implementation)
            refactors += 1

    return {
        "status": "completed",
        "summary": (
            f"{max_cycles} red-green-refine cycle(s): {len(tests)} test(s) written, "
            f"{refactors} refactor(s), {duplication_removed} duplicate(s) removed"
        ),
        "metrics": {
            "cycles": max_cycles,
            "tests_written": len(tests),
            "refactors": refactors,
            "duplication_removed": duplication_removed,
        },
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
