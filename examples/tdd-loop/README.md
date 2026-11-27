# tdd-loop — the worked TDD example

The authoring kit's worked reference for a **loop with ordered stages**:
red (name the failing test before anything exists to pass it), green
(the smallest change that could pass it), refactor (merge the
duplication the fastest green leaves behind) — `max_cycles` times, with
the counts in the metrics. Start from `paa pipeline new <name>` and
this is the shape you should land on; start from this directory and
you have a pipeline that already runs.

| file | what it is |
|---|---|
| `pipeline.toml` | the declaration — identity, resume, the (empty) capability ask, limits, two typed params, tier |
| `pipeline.py` | the entrypoint the host runs under the sandbox — `run()` holds the loop, `main()` holds the contract |
| `test_pipeline.py` | the checks you keep green while editing — the closed-contract parse, the result shape, the smoke probe, the stage order |
| `README.md` | this file |

## The stage map

```
for cycle in 1..max_cycles:
    RED       _red(goal, cycle)      one failing test, named first
    GREEN     _green(goal)           the smallest change; deliberately
                                      duplicates (fastest green)
    REFACTOR  _refactor(implementation)  merge the duplicates; skipped
                                      mid-loop when strict_refactor=false
```

`strict_refactor` (a `bool` param, layer 1) is the knob with a
metrics-visible effect: on (the default) the merge runs every cycle;
off, the duplication accumulates and collapses once at the end. Either
way `duplication_removed` is `max_cycles - 1` — the same debt, paid at
different times.

## What is real, and what is a stand-in

The **stages** are the example: their order, their per-cycle counts,
and the refactor's measurable effect are real code you can read. The
**work inside** each stage is a deterministic stand-in — string work on
the goal — because a sandboxed entrypoint holds no facade tokens in v1:
an installed pipeline cannot call `ctx.think`. Wiring a model into RED
(the test author) or REFACTOR (the critic) is the RPC transport's row
(the pipeline protocol's P3, W9-C9.3 on `feat/rpc`). Replace the
stand-ins; keep the order and the determinism.

## The three doors

1. **List and inspect** — this directory is a local manifest shape, so
   a copy (or symlink) under `PAA_HOME/pipelines/` is listed the
   moment it lands, trust `local`, listed and inspected, not executed:

       paa pipeline list
       paa pipeline inspect tdd-loop

2. **Test while you edit** — from this directory:

       python -m pytest test_pipeline.py

3. **Install** — the door that makes it runnable, straight from the
   repo root:

       paa pipeline install examples/tdd-loop --allow-unverified

   Five gates (signature, content hash, AST scan, permissions, smoke),
   then the six-check conformance standard, then a capability review a
   human answers at a prompt. `--allow-unverified` is the opt-in an
   unsigned local directory needs; the other gates, the conformance run
   and the review still run.

## Determinism

The same `(goal, params)` produce the same result — no clock, no
random, no environment reads inside `run()`. This is not style: the
`double-run-sequence-equality` conformance check fails anything else,
and replay (which is what `resume = "journal"` buys) is only sound for
a pipeline whose second half matches its first.

## Where the knobs live

- **Limits** — `[limits]` in `pipeline.toml`: all four, concrete
  positive ints; an overlay may tighten per run, never widen.
- **Params** — `[params]`: `max_cycles` (int, 1–10) and
  `strict_refactor` (bool); typed, ranged, `--set`-able, tighten-only.
- **Evidence tier** — `[evidence] declares_tier = "unrated"`: a claim,
  not a grant; the host recomputes what a run earned.

## Related documents

- `docs/modules/authoring.md` — the authoring kit: the scaffold, this
  example and `tree-of-thought`, the protocol compatibility matrix
- `docs/plans/2026-11-21-pipeline-platform-spec.md` §4.1 — the manifest
  contract this pipeline declares against
- `examples/tree-of-thought/` — the other worked reference: a
  branching loop (propose → critique → select)
