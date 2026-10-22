# Brief — W2-IMPL-05 (implementer) · wave 2 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** make `src/paa/observability/` trustworthy by test — the module
the ROADMAP says must be trusted *before* the integration test can rely on
telemetry.

**Why this task matters:** the ROADMAP's 0.4 asks for three things:
*"metric percentiles against hand-computed values; run repository round-trips;
a failing exporter never breaks the traced operation."* The third is **fully
covered**. The first is **labelled but not valued** — one test compares the
percentile function against *itself*, so it pins the label→value wiring and
not the value. The second is **half covered** — `finish_run` is used as
*setup* in one test and its returned row is **never asserted**. And
`observability/logging.py` — 169 lines — has **zero** tests, while its
docstring makes claims about correlation-id propagation that nothing checks.

**What failure looks like:** a test file that asserts the labels again.

## 2. Required reading

1. `docs/agent-ops/20261022-w6-w7-w9/briefs/W2-CONTEXT.md` — **read first**.
2. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
3. `docs/plans/w6-w7-w9.md` — **§4 (0.4)** (your task, clause by clause), **§0**
   (the evidence).
4. `src/paa/observability/metrics.py` — `percentile`, `Histogram`,
   `MetricsRegistry`, `METRIC_SPECS` (17), `DEFAULT_PERCENTILES`, the ring
   buffer's capacity.
5. `src/paa/observability/runs.py` — `ExecutionRunRepository`: `start_run`,
   `finish_run`, `get_run`, `query_runs`, `skill_stats`, `count`,
   `purge_older_than`, and `_canonical_trace_id`.
6. `src/paa/observability/tracing.py` — `Tracer.span`, `_safely`, the
   retained-span ring.
7. `src/paa/observability/logging.py` — `bind_correlation`, `get_correlation_id`,
   `get_trace_id`, `configure_logging`, `clear_correlation`.
8. `tests/observability/` — the three existing files (11 + 26 + 13 tests) and
   the house style in them.
9. `docs/agent-ops/20261022-w6-w7-w9/templates/report-implementation.md`.

## 3. The task

**T-008.** The percentiles are labelled but not valued; `finish_run` is never
asserted; `logging.py` has no tests.

### The three ROADMAP clauses

| Clause | State today | What you add |
|---|---|---|
| metric **percentiles against hand-computed values** | the *labels* are pinned; one test compares `summary["p95"]` to `percentile(sorted(values), 0.95)` — **the function against itself** | the **values**: p95 of `[1..5]` = **4.8**; the p99 of `1..100`; the even-count interpolation; and the module's own argument — *"the nearest-rank alternative can only ever return an observed sample"* — turned into an **assertion** that the result is **not** an observed sample. Then the **algorithm**, not just a value: a **hypothesis** property over the rank formula `rank = (n-1)·q` with linear interpolation, drawn from the strategy. And the summary's numeric fields (`count`, `sum`, `min`, `max`, `mean`, `window`) — untested today. And the ring's semantics: *count and sum are exact for the lifetime; the percentiles describe only the retained window* |
| **run repository round-trips** | identifiers and the row→model coercion are covered; `finish_run`'s **returned row** is not | `finish_run` end to end: every field it accepts, the `status` (`OK` / `ERROR` / `IN_FLIGHT`), the `duration_ms`, the `telemetry` JSON round trip, and the `COALESCE` promise that a **partial** finish does not destroy the values already written. Then `query_runs` — **all six** filters — `count`, and `purge_older_than`, whose docstring claims *"Unfinished runs are never purged regardless of age"* — an explicit claim with **zero** tests |
| a failing exporter never breaks the traced operation | **covered** (`test_tracing.py:158`) | the **boundary** the coverage misses: `_safely` catches `Exception`, not `BaseException`, so an exporter raising `KeyboardInterrupt` **propagates** — undocumented and untested. Pin the boundary, and document the asymmetry. Plus the retained-span ring's bound, and `clear()` |

### And the module with no tests at all

`src/paa/observability/logging.py` — **zero** coverage. Its docstring makes
claims: *"the correlation id [is] a field rather than a substring of a
sentence"*; *"An explicit keyword at the call site always wins"*; *"Restores
the previous value on exit rather than clearing it"*; the timestamps are
ISO-8601 UTC; JSON rendering is opt-in. Every one of those is a test.

**Note the trap:** the module is named `logging` inside its package and still
imports the standard library's `logging` without incident — Python 3 resolves
imports absolutely. A test that shadows `logging` in `sys.modules` will break
it.

## 4. Acceptance criteria (measurable)

* The percentile **values** are asserted against **hand-computed** numbers,
  and the **algorithm** is pinned by a hypothesis property (draw the
  adversarial value from the strategy — a quantile of exactly 0 or 1 must be
  in the strategy, not left to chance).
* The `Histogram.summary` numeric fields and the ring's lifetime-vs-window
  semantics are asserted.
* `finish_run`'s returned row is asserted field by field; the partial-finish
  non-destruction is asserted; `query_runs`' six filters, `count` and
  `purge_older_than` (including the unfinished-run rule) are asserted.
* `logging.py`'s five claims are asserted, and the module's name collision
  with the standard library is handled.
* The `BaseException` boundary of `_safely` is pinned and documented.
* **Commands that must pass (scoped):**
  ```bash
  .venv/bin/python -m pytest tests/observability -q
  .venv/bin/python -m ruff check src/paa/observability tests/observability
  .venv/bin/python -m mypy src/paa/observability
  ```
* The wave-1 gate must not regress: ruff 0, mypy 0 across `src`.

## 5. Ownership

**WRITE-SET (you may edit exactly these):**

```
src/paa/observability/runs.py
src/paa/observability/logging.py
src/paa/observability/metrics.py
src/paa/observability/tracing.py
tests/observability/__init__.py
tests/observability/test_metrics.py
tests/observability/test_runs.py
tests/observability/test_tracing.py
tests/observability/test_logging.py        (new)
```

**READ-ONLY:** `src/paa/observability/__init__.py`, `src/paa/config.py` (the
`ObservabilitySettings`), `src/paa/core/types.py`.

**FORBIDDEN:** `src/paa/**` outside `observability/`, `tests/**` outside
`tests/observability/`, `pyproject.toml`, `deploy/**`, `docs/**` (no ADR is
needed for a test suite — **say so in your report**; if you think one is
needed, `ESCALATE` and explain), `docs/adr/README.md`, `docs/plans/**`,
`AGENTS.md`.

**One thing is already yours, by deferral:** `src/paa/observability/runs.py:41`
keeps `RelationalBackend` under `if TYPE_CHECKING:` — the eighth member of
wave 1's family, deferred to this task. Wave 1's lock
(`tests/conventions/test_import_conventions.py`) records it as a **deferral
that is self-invalidating**: when you convert it, **the lock goes red until
the entry is deleted in the same commit.** So: convert it, and delete the
entry in the same change. `tests/conventions/**` is **not** in your
write-set, so if the lock's file needs the deletion, post a `NEED` with the
exact change — or, if the orchestrator grants it, edit only that one entry.

## 6. Environment

* Scoped checks only. **Never** the full suite, never a container, never a
  port.
* The `runs` fixture in `tests/observability/test_runs.py` is a real
  `ExecutionRunRepository` over the `db` fixture — a real SQLite file in
  `tmp_path`. Use it.
* The repo's logging is **structlog**; `caplog` is used nowhere. Use
  `structlog.testing.capture_logs()` and the `logging.disable(logging.WARNING)`
  the root `tests/conftest.py` already applies.
* A hypothesis test that calls a function which can raise must handle the
  raising input — and must **draw** it from the strategy.
* Kill any process you start.

## 7. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/W2-IMPL-05-T-008.md`
  — `templates/report-implementation.md`, ORCH-DIGEST first, appended as you
  work.
* **Commits:** code first, then the report in a separate docs-only commit.
  Trailers: `Task: T-008`, `Agent: W2-IMPL-05`, `Wave: 2`, `Report: <path>`.
  Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- src/paa/observability/ tests/observability/
  git commit -m "<msg>" -- src/paa/observability/ tests/observability/
  ```
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | blockers`.
  **It must not be empty.**
