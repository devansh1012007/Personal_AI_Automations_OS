# tree-of-thought — the worked branching example

The authoring kit's worked reference for a **loop that branches and
prunes**: propose (branch the current thought into `branching` candidate
continuations), critique (score every candidate on a stable rule, mark
it keep or prune), select (carry the best survivor to the next round) —
`depth` rounds, with the counts in the metrics. Where `tdd-loop` shows
ordered stages, this one shows a search whose width and depth are
layer-1 params.

| file | what it is |
|---|---|
| `pipeline.toml` | the declaration — identity, resume, the (empty) capability ask, limits, two typed params, tier |
| `pipeline.py` | the entrypoint the host runs under the sandbox — `run()` holds the loop, `main()` holds the contract |
| `test_pipeline.py` | the checks you keep green while editing — the closed-contract parse, the result shape, the smoke probe, the counts, the stability |
| `README.md` | this file |

## The stage map

```
thought = goal
for round in 1..depth:
    PROPOSE   _propose(thought, branching)   branching candidates, from
                                            a fixed table of angles
    CRITIQUE  _critique(candidates)          a stable score per candidate;
                                            score % 3 == 0 prunes
    SELECT    _select(scored)                best survivor by
                                            (score, candidate); the best
                                            overall when nothing survives
    thought = the selection
```

`depth` (int, 1–4) and `branching` (int, 1–5) are the two knobs, and
the metrics are their arithmetic: `proposals == critiques ==
depth × branching`, with `pruned` somewhere in between.

## What is real, and what is a stand-in

The **shape** is the example: the branch-prune-carry loop, the total
order selection keeps, and the counts are real code you can read. The
**critique** is a deterministic stand-in — a digest of the candidate's
text — because a sandboxed entrypoint holds no facade tokens in v1: an
installed pipeline cannot call `ctx.think`. A real critique asks a
model or a checker of its own, which is the RPC transport's row (the
pipeline protocol's P3, W9-C9.3 on `feat/rpc`). Replace the stand-in;
keep the total order and the determinism.

## The three doors

1. **List and inspect** — this directory is a local manifest shape, so
   a copy (or symlink) under `PAA_HOME/pipelines/` is listed the
   moment it lands, trust `local`, listed and inspected, not executed:

       paa pipeline list
       paa pipeline inspect tree-of-thought

2. **Test while you edit** — from this directory:

       python -m pytest test_pipeline.py

3. **Install** — the door that makes it runnable, straight from the
   repo root:

       paa pipeline install examples/tree-of-thought --allow-unverified

   Five gates (signature, content hash, AST scan, permissions, smoke),
   then the six-check conformance standard, then a capability review a
   human answers at a prompt. `--allow-unverified` is the opt-in an
   unsigned local directory needs; the other gates, the conformance run
   and the review still run.

## Determinism

The same `(goal, params)` produce the same result — the score is a
SHA-256 digest (stable across platforms and runs, unlike `hash()`),
the prune rule is fixed, and the selection order is total
(`(score, candidate)` lexicographic) so ties cannot flip. This is not
style: the `double-run-sequence-equality` conformance check fails
anything else, and replay (which is what `resume = "journal"` buys) is
only sound for a pipeline whose second half matches its first.

## Where the knobs live

- **Limits** — `[limits]` in `pipeline.toml`: all four, concrete
  positive ints; a branching loop trades calls for wall time, so the
  clock is tighter than `tdd-loop`'s and the call budget is larger. An
  overlay may tighten per run, never widen.
- **Params** — `[params]`: `depth` (int, 1–4) and `branching`
  (int, 1–5); typed, ranged, `--set`-able, tighten-only.
- **Evidence tier** — `[evidence] declares_tier = "unrated"`: a claim,
  not a grant; the host recomputes what a run earned.

## Related documents

- `docs/modules/authoring.md` — the authoring kit: the scaffold, this
  example and `tdd-loop`, the protocol compatibility matrix
- `docs/plans/2026-11-21-pipeline-platform-spec.md` §4.1 — the manifest
  contract this pipeline declares against
- `examples/tdd-loop/` — the other worked reference: a loop with
  ordered stages (red → green → refactor)
