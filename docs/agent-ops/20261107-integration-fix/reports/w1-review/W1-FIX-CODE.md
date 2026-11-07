<!-- ORCH-DIGEST:START -->
agent: W1-FIX-CODE | task: W1-FIX-CODE (wave 1 fix) | status: COMPLETE
findings_fixed: R-W1-002 (MAJOR, redacted+renamed hydration log to context.hydration_failed, module-level redact import) | R-W1-006 (MINOR, per-hit corrupt-row payload fallback) | R-W1-007 (MINOR, superseded_by IS NULL filter) | TREV P1-a/P2-a/P2-b tests
code_commit: 10989e16ec98a019e8e22636fd46ba9ace6f2585 | report_commit: (separate docs-only commit)
top_risks: none known; scoped checks only (91 passed; ruff 0; mypy total 3 pre-existing baseline)
last_comms_line_read: 2026-11-07T08:31:18Z ORCH -> ALL | CORRECTION | W1
<!-- ORCH-DIGEST:END -->

## Status: COMPLETE

## 0. Brief and method
Task W1-FIX-CODE per `briefs/W1-FIX-CODE.md`. WRITE-SET only:
`src/paa/agents/context_agents.py`, `tests/agents/test_semantic_derivation_p002.py`.
Required reading done in brief order: STANDING-ORDERS, AGENTS.md, REV-W1 §§4–5/7/8,
TREV-W1 §6 P1-a/P2-a/P2-b, W1-IMPL-03 report, `_semantic` code + `models/base.py::redact` +
test file. COMMS read at start; last line: `2026-11-07T08:31:18Z ORCH -> ALL | CORRECTION | W1`.

## Context Report (pre-implementation, from source evidence)
- R-W1-002: [VERIFIED context_agents.py:283] hydration failure logs
  `log.warning("context.vector_search_failed", error=str(exc))` — new unredacted line (P-001
  class regrowth) + wrong event (DB outage indistinguishable from vector outage). REDACT via
  `models/base.py::redact` [VERIFIED base.py:256], import at MODULE level (file already imports
  `effective_confidence` at module level :54; `redact` currently function-local in `_touch_recalled`
  :478 — the drift INV-04 flagged). `models/base.py` imports stdlib+pydantic+`paa.core` only
  [VERIFIED base.py:67-78] → module-level import is cycle-safe. The vector-path log
  (:251 `context.vector_search_failed`) stays untouched. Tested by updating
  `test_hydration_db_failure_degrades_to_payload_snapshot`.
- R-W1-006: [VERIFIED context_agents.py:291-311] per-hit `float(row[...])`, `from_iso`,
  `effective_confidence`, `float(importance)` sit outside any try — only `KeyError` (unknown
  domain, via `domain_policy` in decay.py) is caught. `effective_confidence` raises ValueError
  on out-of-range confidence [VERIFIED decay.py:62-63]; `from_iso` → `datetime.fromisoformat`
  raises ValueError on garbage text [VERIFIED database.py:72]. A corrupt row thus crashes the
  whole tier, contradicting the "hydration failure must never empty the tier" comment (:268).
  Fix: outer try/except `(ValueError, TypeError)` per hit → payload snapshot fallback for that
  hit + warning, never propagate. Keep inner KeyError branch (unknown domain passthrough).
- R-W1-007: [VERIFIED context_agents.py:276-280] hydration SELECT has no `superseded_by IS NULL`,
  unlike `_hot_facts` (:200). Fix: add the filter so "live" means the same in both tiers.
  Superseded hit then takes the payload-fallback branch (row absent from `hydrated`).
- TREV P1-a/P2-a/P2-b: absent coverage [VERIFIED test file has 9 tests, none partial/db=None/
  empty-hits]. Sketches in TREV-W1 §6.
- Invariants preserved: payload snapshot = provenance only; one batched IN query per search;
  `now` kwarg; vector-failure `[]` path; unknown-domain passthrough; `PLAN:integration-fix:P002`
  tag kept; `_semantic` signature unchanged.
- `_touch_recalled` callers (grep, unchanged by me): context_agents.py:390,456 (same file),
  cli/main.py:541,567, api/app.py:210,233, coder/runtime_tools.py:794 — only the function-local
  import inside context_agents.py:478 is touched; the call sites are not.

## Phase log
### Phase A — `context_agents.py` fixes (DONE, self-reviewed vs full diff)
- Module-level `from paa.models.base import redact` added (line 56, alphabetical position);
  function-local import in `_touch_recalled` removed (now resolves to module-level — the
  drift INV-04 flagged). Cycle check: `models/base.py` imports stdlib+pydantic+`paa.core`
  only [VERIFIED base.py:67-78]; import succeeds (91 tests green, imports at collection).
- Hydration log: `context.vector_search_failed` + `str(exc)` → `context.hydration_failed` +
  `redact(str(exc))` (batch path). Vector-path log at :252 untouched (pre-existing).
- `AND superseded_by IS NULL` added to hydration SELECT (mirrors `_hot_facts` :200).
- Per-hit outer `try/except (ValueError, TypeError)` around the whole hydrated derivation
  (initial/domain/`effective_confidence`/`importance`): on corrupt row, payload snapshot
  fallback for that hit only + redacted `context.hydration_failed` warning with `fact_id`.
  Inner `except KeyError` unknown-domain passthrough preserved verbatim. DECISION: all-or-
  nothing per hit (a hit with a corrupt importance falls back entirely, not partially) —
  the row is internally inconsistent, so "live row wins" cannot be honored per field.
  INVARIANT: one batched IN query per search preserved; `now` kwarg untouched; `_semantic`
  signature unchanged; `PLAN:integration-fix:P002` tag and the "must never empty the tier"
  comment kept — it is now true per-hit as well as batch.
- Payload-fallback block now appears twice (hydrated-corrupt + missing-row branches).
  DECISION: mirror the existing else-branch structure rather than extract a helper —
  minimal change, parallel shape; three scalar lines each, not buggy-logic duplication.

### Phase B — Tests (DONE)
- Degrade test updated: pins `context.hydration_failed` (+ absence of `vector_search_failed`)
  and redaction — DB exception now secret-shaped (`sk-ABCDEF1234567890`, matches
  `sk-[A-Za-z0-9_\-]{12,}` [VERIFIED base.py:244]); asserts `***REDACTED***` present and the
  raw token absent in the captured log entry.
- New tests: P1-a partial hydration (2 hits, live derives / deleted payload-exact, both
  present); P2-a `db=None` (payload fallback, no crash); P2-b empty hits (early `[]`,
  zero `fetch_all` calls); R-W1-006 corrupt row (`last_queried_at='garbage-not-a-date'` =>
  payload fallback + healthy sibling still derives + `hydration_failed` logged); R-W1-007
  superseded (superseded fact id in hit => payload fallback exactly, derived value differs).

### Phase C — Scoped verification (DONE, verbatim)
- [RAN pytest test_semantic_derivation_p002 + test_context_agents + test_decay -q -> 91 passed
  in 13.60s / rerun 91 passed in 17.16s after fix-restore]
- [RAN ruff check on both changed files -> All checks passed!]
- [RAN mypy context_agents.py -> Success: no issues] ; [RAN mypy src -> Found 3 errors in 1
  file (subprocess_backend.py:548 killpg/getpgid/SIGKILL, pre-existing baseline, unchanged)]
- Reversal proof (file-copy revert to HEAD, no stash/reset/checkout; restored after):
  [RAN pytest test_semantic_derivation_p002 on pre-fix code -> 3 failed, 11 passed]:
  degrade fails `assert 'context.hydration_failed' in ['context.vector_search_failed']`;
  corrupt-row fails with `ValueError: Invalid isoformat string: 'garbage-not-a-date'` raised
  out of `_semantic` (the R-W1-006 crash, reproduced); superseded fails with derived 0.5488
  vs payload 0.42 (R-W1-007 exactly). The 3 new pinning tests (P1-a/P2-a/P2-b) pass on
  pre-fix code by design (they pin already-correct behavior).
- Acceptance greps: hydration `str(exc)` all redacted [VERIFIED grep: lines 285, 318 use
  `redact(str(exc))`; vector-path :252 + expander :137 + graph :367 remain pre-existing
  unredacted — FINDING below, not fixed]; `_touch_recalled` grep unchanged by callers
  (only the local import line removed).

### Phase E — Self-review + final review (DONE, see Review Audit)
- Re-read full `git diff` from scratch (both files). No CRITICAL/MAJOR/MINOR open.
  Mutation mental check: remove `redact()` → degrade test fails (marker + token assertions);
  remove per-hit try → corrupt-row test errors (proven by reversal above); remove
  superseded filter → superseded test fails (proven); convert batch fallback to raise →
  degrade + corrupt tests fail; per-hit N+1 refactor → batch/load tests fail (unchanged code).
- Load/concurrency: unchanged — one IN query per search, bounded by limit; read-only.
- Independent review: not required by brief (single-file reader fix + tests); status COMPLETE.

## Review Audit
- Phase A self-review: edge cases — `float(None)` TypeError => fallback; `from_iso(None)`
  TypeError => fallback; out-of-range confidence ValueError => outer catch (not swallowed by
  inner KeyError); missing row key KeyError propagates exactly as pre-fix (rows always carry
  the SELECT keys; not a regression). Hostile payload (`float("abc")` in fallback) is the
  pre-existing class from REV attempt #7 — `else` branch identical, out of scope.
  Observability: two `context.hydration_failed` emission points (batch + per-hit) share one
  event name — intended; operators grep one event for all hydration degradation.
- Phase B self-review: all new tests deterministic (injected `now` where derivation asserted;
  `_FakeVectors`/`_FakeEmbedder` patterns from the file); superseded test discriminates
  (derived 0.5488 ≠ payload 0.42, proven by reversal); corrupt test discriminates (chunky
  crash vs fallback, proven); no mocks of the unit under test.
- Final review: cross-phase — `handle()` callers unaffected (signature unchanged); e2e
  paraphrase path not in scoped list (brief: scoped only) — NOT re-run. No leftovers:
  `_touch_recalled` callers in cli/api/coder untouched; `docs-lock-now.txt` (not mine)
  untouched; COMMS.md shows sibling W1-FIX-DOCS lines interleaved — not committed by me.

## Long-run test answers (per standing orders)
- **Source:** ranking truth still `effective_confidence` on hydrated rows (T02 approx 1e-6,
  unchanged); degradation truth is the payload snapshot — now per-hit, not just batch.
- **Load (100x):** unchanged — 100 searches = 100 hydration queries (count-asserted, green).
- **Hostile input:** corrupt `last_queried_at`/confidence/importance now degrade per-hit
  instead of crashing the tier; secret-shaped DB errors are redacted in the new event.
- **Operability:** observe via `context.hydration_failed` (DB) vs `context.vector_search_failed`
  (vector) — distinct events now; rollback = revert this diff (no migration, no payload change).
- **Reversal:** 3 fail on pre-fix code for the right reasons (proven above); pinning tests
  pass both ways by design and are labeled as such.

## Out-of-scope findings (not fixed; FINDING posted)
- Pre-existing unredacted `str(exc)` log lines remain at expander (:137), vector path (:252),
  graph traversal (:367) in this same file. The vector-path one is named in brief §6: FINDING,
  not a fix. A P-001-class sweep of `context_agents.py` would be a separate task.
- REV R-W1-005 (ANSI residual) and R-W1-008 (`inf` threshold) remain out of scope per brief.

## Unverified Assumptions & Remaining Risks
- [ASSUMED] Postgres `?`->`%s` translation of the extended SQL (unchanged pattern from
  W1-IMPL-03, still not run live — no DSN; scoped env per brief).
- NOT re-run: full suite, e2e remember/recall, live Qdrant/Postgres (scoped checks only,
  standing orders §16 + brief §7).

## Git State
- Branch: `ops/20261107-integration-fix` [RAN git branch --show-current]
- Code commit: `10989e16ec98a019e8e22636fd46ba9ace6f2585` (2 files, explicit paths,
  message "Wave-1 fix: redact+rename hydration log, per-hit corrupt-row fallback,
  superseded filter"). NOTE (honest disclosure): the code commit message was created
  without the brief-required trailers (Task/Agent/Wave/Report); `--amend` is forbidden
  by standing orders, so the message could not be corrected post-hoc. The report commit
  below carries full trailers, and the code SHA is recorded here and in the digest.
- Report commit: separate docs-only commit listing the code SHA.
- Files changed by this agent: `src/paa/agents/context_agents.py`,
  `tests/agents/test_semantic_derivation_p002.py`, this report.
- Left untouched: `docs-lock-now.txt` (unknown owner), COMMS.md sibling lines, W1-FIX-DOCS.md.
- Last COMMS line read at start: `2026-11-07T08:31:18Z ORCH -> ALL | CORRECTION | W1`

