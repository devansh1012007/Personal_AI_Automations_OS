"""tree-of-thought - the worked branching example (the authoring kit's reference, P7).

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

WHAT IS REAL HERE, AND WHAT IS A STAND-IN. The loop's *shape* is the
example: propose branches the current thought into `branching`
candidate continuations, critique scores every candidate on a stable
rule and marks it keep or prune, select carries the best survivor to
the next round - `depth` rounds, with the counts in the metrics. The
*scoring* is a deterministic stand-in (a digest of the candidate's
text), because a sandboxed entrypoint holds no facade tokens in v1: a
real critique asks a model or a checker of its own, which is the RPC
transport's row (the pipeline protocol's P3). Replace the stand-in;
keep the total order and the determinism.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

#: The angles each round proposes from - the branching knob picks a
#: prefix, so the candidate set is a function of the knob, never of the
#: clock or the platform.
_ANGLES = (
    "direct",
    "decompose-first",
    "work-backwards",
    "by-analogy",
    "edge-cases-first",
)


def _score(candidate: str) -> int:
    """A stable critique score: the first 8 hex digits of the candidate's digest.

    Deterministic by construction - the same candidate always scores the
    same on every platform, which is what the double-run check demands.
    This is the stand-in an author replaces with a real critique (a
    model call once the RPC transport lands, or a checker of its own).
    """
    return int(hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:8], 16)


def _propose(thought: str, branching: int) -> list[str]:
    """PROPOSE - branch the current thought into candidate continuations."""
    return [f"{thought} [{angle}]" for angle in _ANGLES[:branching]]


def _critique(candidates: list[str]) -> list[tuple[int, str, str]]:
    """CRITIQUE - score every candidate; a third of the space prunes, stably.

    The verdict rule is fixed and total (``score % 3 == 0`` prunes), so
    the same candidate set always prunes the same candidates.
    """
    scored: list[tuple[int, str, str]] = []
    for candidate in candidates:
        score = _score(candidate)
        verdict = "prune" if score % 3 == 0 else "keep"
        scored.append((score, candidate, verdict))
    return scored


def _select(scored: list[tuple[int, str, str]]) -> str:
    """SELECT - the best survivor; the best overall when nothing survives.

    The order is total (``(score, candidate)`` lexicographic), so the
    choice is stable even when scores tie - and it never depends on the
    order ``critique`` happened to visit, though that is fixed too.
    """
    survivors = [entry for entry in scored if entry[2] == "keep"]
    pool = survivors or list(scored)
    return max(pool, key=lambda entry: (entry[0], entry[1]))[1]


def run(goal: str, params: dict[str, Any]) -> dict[str, Any]:
    """One run of the loop: propose, critique, select - ``depth`` rounds.

    Pure and deterministic - the same (goal, params) in, the same result
    out. The test skeleton drives this function in-process; main()
    drives it under the sandbox.
    """
    depth = int(params.get("depth", 2))
    branching = int(params.get("branching", 3))

    thought = goal or "the empty probe"
    proposals = 0
    critiques = 0
    pruned = 0
    for _round in range(1, depth + 1):
        candidates = _propose(thought, branching)
        proposals += len(candidates)
        scored = _critique(candidates)
        critiques += len(scored)
        pruned += sum(1 for entry in scored if entry[2] == "prune")
        thought = _select(scored)

    return {
        "status": "completed",
        "summary": f"selected after {critiques} critique(s): {thought}",
        "metrics": {
            "depth": depth,
            "branching": branching,
            "proposals": proposals,
            "critiques": critiques,
            "pruned": pruned,
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
