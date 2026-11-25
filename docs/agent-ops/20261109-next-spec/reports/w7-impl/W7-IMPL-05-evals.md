# W7-IMPL-05 — C7.4: the cross-harness eval harness

**Task:** SPEC 2 §6 row C7.4 (H3.6) — same tasks, N harnesses, deterministic
scoring, per-harness report, results as ledger rows.
**Status:** landed. All three Wave 7 evidence rows now have their harness.

## What landed

* `src/paa/evals/` — the package:
  * `tasks.py` — `EvalTask` loading + validation against `TASK_SCHEMA`;
    five repository tasks under `evals/tasks/`: `reverse-util`,
    `fizzbuzz-off-by-one`, `greet-cli-flag`, `inventory-report`,
    `palindrome-guard` — each a `task.json` manifest, a `workspace/`
    fixture, a `tests/` suite, a `solution/`, and where relevant a
    forbidden-effects probe. `palindrome-guard` is the designed-to-fail
    discriminator (a naive direct-write harness trips its forbidden path);
    `fizzbuzz-off-by-one` separates harnesses that run the tests they were
    given from ones that ship a plausible-looking wrong fix.
  * `harnesses.py` — the adapter registry (`register_adapter` /
    `get_adapter` / `known_adapters`). Two shipped adapters, both scripted
    fakes (deterministic, offline, no model key): `recorded-fake`, whose
    tool calls route through the `paa record proxy` MCP loop (mediated —
    policy-checked, journalled, evidence bundle), and `scripted-direct`,
    which writes files with its own hands (unmediated, T1-observed only).
  * `runner.py` — `EvalRunner`: runs task × harness pairs inside PAA's
    evidence boundary, journals the matrix, writes the canonical report
    (`REPORT_SCHEMA`, sorted keys, hash-stable).
  * `scoring.py` — the four checks, machine verdicts end to end:
    `tests-pass`, `bundle-verifies` (honestly N/A for plain adapters, never
    silently passed), `diff-matches-intent` (expected file set + content
    hashes + the task's own tests + forbidden paths), `no-forbidden-effects`.
* `src/paa/cli/eval.py` — `paa eval run` (matrix or selection, `--json`
  path) and `paa eval tasks`; registered in `cli/main.py`.
* `tests/evals/` — 62 tests: task loading and manifest validation, registry
  resolution, scoring determinism (byte-identical verdicts on re-run), the
  2 × 5 matrix end-to-end (the spec's own acceptance — the report shows a
  real per-harness distinction), ledger payload shape, CLI smoke.

## Design decisions

* **No new EventType member.** The matrix journals
  `PIPELINE_RUN_COMPLETED` on its own lineage rather than minting an
  eval-specific event type: the vocabulary rule (ADR-0046) prices a new
  member at goldens + replay coverage, and an eval run *is* a governed
  pipeline run over the matrix — reusing the member keeps the ledger
  vocabulary closed and the corpus queryable with the existing folds.
* **Scoring is never a model judgment** (spec rule 2). Every check is a
  deterministic comparison; "diff matches intent" is defined mechanically
  per task kind in the manifest, not by reading the diff with a model.

## Gates

`pytest tests/evals` 62 passed; `tests/conventions` 34 green; ruff 0 and
mypy 0 on `src/paa/evals`, `src/paa/cli/eval.py`.

## Honest residuals

Both shipped harnesses are scripted fakes — that is the offline sandbox's
honest ceiling, and the registry is the plug-in point where claude-code /
codex adapters land when keys exist. `bundle-verifies` is N/A for plain
adapters by design. The matrix runs each pair in sequence; parallelism is
a later optimisation, not a correctness gap.
