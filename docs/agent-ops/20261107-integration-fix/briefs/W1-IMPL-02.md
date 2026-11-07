# Brief — W1-IMPL-02 (IMPL) · wave 1 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-001 (redact the 2 unredacted `str(exc)` paths, T01). Why: safety-first Phase 1; panels/recall later render exception text, P-001 must land before P-006. This is the deferred F-02 remainder from the previous run.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (zero-baseline gate; do not add `# noqa`; redact-via-`models.base` convention)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (baseline 14 failures categories A–D; your scope must add zero new failures)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P01 row verbatim + §H T01 + §K (P01 default-on)
5. Investigation (authoritative for exact write-set): `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-04.md` §P-001 — sites `src/paa/runtime.py:481`, `src/paa/cli/main.py:649`; helper `redact` at `src/paa/models/base.py:256`, `redact_detail:278`; reachability analysis; existing test landscape; recommended Fix A (NOT B — no new helper, plan forbids inventing a redactor)
6. Template: implementation report with ORCH-DIGEST first (follow `templates/report-investigation.md` shape + `templates/gate-report.md` verification section)
7. Code: `src/paa/runtime.py:474-482` (one line above already redacts — mirror it), `src/paa/cli/main.py:642-649`, `src/paa/models/base.py:256-295`, `tests/runtime/test_memory_creator.py:151-195` (pattern for driving the except branch by monkeypatching `VectorEmbeddingSink` to raise), the `paa memory drain` CLI path test setup.

## 3. Task and acceptance criteria
Task T-001-P001. Specifically:
- `src/paa/runtime.py:481`: change `error=str(exc)` → `error=redact(str(exc))` (import already at `runtime.py:43`), mirroring :474. ONE line. Do not change the event name `memory.embedding_sink`.
- `src/paa/cli/main.py:649`: redact the printed `str(exc)` via the existing helper AND bound to 200 chars with an explicit truncation marker, per Fix A + ADR-0036 §P01 spec (owned by W1-IMPL-01 — write code to that spec: `redact(str(exc))` then bound ≤200 rendered chars + marker; name the 200 constant next to the call it serves, e.g. `_MAX_DRAIN_ERROR_CHARS = 200` with a comment citing ADR-0036 §P01). Import `redact` at module level if not already imported (check cli/main.py imports first; if a local import exists, follow the file's existing convention — do not create the function-local-import drift INV-04 flagged in touch copies). Never invent a second redactor.
- Do NOT touch the 6 other `str(exc)` sites INV-04 listed as out-of-plan discoveries — they are FINDINGS for the ledger, not this task. (If you believe one is in scope, post NEED and stop; do not fix silently.)
- Tests (must FAIL before the fix, pass after — prove by running them against reverted lines mentally or via stash-free manual revert-and-rerun in your report):
  - runtime: drive `_build_memory_creator`'s except branch (monkeypatch `VectorEmbeddingSink` to raise an exception whose text contains `sk-` + 12+ chars and `Bearer …`/`api_key:` fixtures); assert captured structlog warning kwargs contain NO `sk-` substring and no Bearer value. 
  - cli: drive `paa memory drain` path (or the narrowest function hosting :649) with a drain that raises a 10 KB exception string; assert rendered output ≤200 chars + explicit marker. Also boundary 200/201 chars, and non-ASCII/NUL/ANSI in exception text.
  - A test that only calls `redact()` directly does NOT count (INV-04 §P-001: must drive the two sites).
- Trace tag `PLAN:integration-fix:P001` as a comment at both changed lines (exact tag per 00-mission.md).
- Acceptance (measurable): `grep -n "error=str(exc)" src/paa/runtime.py` no longer hits :481 site; `cli/main.py:649` area greps `redact(`; T01 sk-fixture + 200-char cases green; existing `tests/models/test_model_errors.py` + `tests/runtime/test_memory_creator.py::` redaction cases still pass; `ruff` on changed files 0, `mypy` total stays 3 (your files 0).

## 4. Ownership
WRITE-SET (only these): `src/paa/runtime.py`, `src/paa/cli/main.py`, `tests/runtime/test_redaction_p001.py` (new), `tests/cli/test_redaction_p001.py` (new). READ-ONLY: everything else, including ADR-0036 (W1-IMPL-01 owns it; cite it, do not write it). FORBIDDEN: editing `docs/adr/*`, `config.py`, any other src file. Note: `runtime.py` is shared with W1-IMPL-03's DOMAIN but W1-IMPL-03 does not edit runtime.py this wave (embedder field is Wave 3 P-004) — if you find you need a runtime.py change beyond :481, post CONFLICT/NEED.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory.

## 5. Parallel siblings this wave
W1-IMPL-01 (P-014: owns ADR-0036 §P01 + config.py — disjoint files), W1-IMPL-03 (P-002 hydration in `context_agents.py`/`creator.py` — disjoint). Pre-mortem 8 resolved by ordering: you land before P-006 (Wave 3).

## 6. Skip list and known traps
- The plan P01 row says "spec in ADR-0036 §P01" — that ADR is being written concurrently by W1-IMPL-01. Code to the spec in INV-04 §P-001 (200 chars + marker); if the ADR text you see at commit time differs from the spec you coded to, post FINDING with the discrepancy — do not edit the ADR.
- `runtime.py:481` branch is near-unreachable today (INV-04) — that is why Fix C (skip) was rejected. Do not skip it.
- Keep `redact` semantics untouched: its tests (`test_model_errors.py:1165-1169` `__all__` etc.) pin it. No new helper (Fix B rejected).
- Baseline category B failure `tests/tui/test_dashboard.py::test_ledger_with_events` (strftime) is NOT yours. A scoped failure in your files from another agent's in-flight edit = FINDING, retry once.
- Truncation slicing must not split mid-`sk-` token and leak the remainder — truncate AFTER redaction, and the marker must make clear redaction happened (best-effort note per T01).

## 7. Environment and tests
Scoped only: your two new test files + `pytest tests/models/test_model_errors.py tests/runtime/test_memory_creator.py -q` regression; `ruff check src/paa/runtime.py src/paa/cli/main.py tests/runtime/test_redaction_p001.py tests/cli/test_redaction_p001.py`. No full suite, no containers. `PAA_TEST_VERBOSE=1` optional.
Commit protocol: code+tests commit first, then report in a separate docs-only commit listing code SHA. Trailers `Task: T-001-P001`, `Agent: W1-IMPL-02`, `Wave: W1`, `Report: docs/agent-ops/20261107-integration-fix/reports/w1-implementation/W1-IMPL-02.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w1-implementation/W1-IMPL-02.md` (create at start, ORCH-DIGEST first, append as you work; long-run test answers: source/load/hostile/operability/reversal — e.g. 10 KB exception, ANSI/NUL, unbounded payload). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
