# Brief — W5-IMPL-01 (IMPL) · wave 5 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-008 (T-008) — pure `total_memory_cost(storage,retrieval,noise,maintenance,utility)` + curator ANALYZE deprecation SUGGESTION (never auto-delete) + `CostGateSettings` consumption. Why: the curator has no advisory signal beyond eviction floors; the plan's cost model was never ported; the never-auto-resolve invariant is the guardrail.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (never-auto-resolve invariant — curator NEVER auto-resolves; zero-baseline)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (baseline; A-005)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P08 row + §H T09 + §I (TOTAL_COST hides good facts → advisory only) + §K (`cost_gate.enabled=False`, `threshold=1.0`, `cost_gate.suggestions` observability) + §J (200 char? no — cost gate is O(1) per fact)
5. ADR `docs/adr/0040-total-memory-cost-advisory-gate.md` (FULLY — five-term derivations or explicit unavailable; `utility=0 → inf`; threshold ratio-vs-absolute; read it before designing the pure function — where ADR-0040 names a derivation, implement it; where it names "unavailable", the function takes the term as input/0 per ADR)
6. Investigation: `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-03.md` §P-008 (pure `total_memory_cost` + curator `ANALYZE` suggestion + `CostGateSettings`; `_phase_analyze` is last in `_PHASE_ORDER` — hook after RECONCILE_VECTORS; 2 of 5 terms lack sources until ADR-0040 defines them; `run_weekly` guard precedent; Fix A recommended; T09 shape; hostile huge inputs → finite/inf never NaN via `_finite` metrics.py:80-85; 100× ticks no growth), `W0-INV-04.md`? (no), `W0-INV-05.md` §P-014 (CostGateSettings declared W1).
7. W-wave evidence: `reports/w3-implementation/W3-IMPL-01.md` (tier sweeper ordered after DECAY — your ANALYZE suggestion runs AFTER RECONCILE_VECTORS; verify phase order DECAY→…→ANALYZE in curator.py and do not disturb it), `reports/w4-implementation/W4-IMPL-01.md` (allocator scoring context — `total_memory_cost` is curator-side, not allocator-side).
8. Template: implementation report ORCH-DIGEST first.
9. Code at HEAD (verify line numbers yourself): `src/paa/context/metrics.py` (`_finite` guard pattern, module conventions — pure, no I/O), `src/paa/memory/curator.py` (`_phase_analyze`, `analyze()`, `CurationReport` shape — where suggestions surface), `src/paa/config.py` `CostGateSettings` (read-only — CONSUME only), existing `tests/context/test_metrics.py` + `tests/memory/` curator tests.

## 3. Task and acceptance criteria
Task T-008-P008. Specifically (Fix A from INV-03 §P-008):
- `total_memory_cost(storage, retrieval, noise, maintenance, utility)` in `context/metrics.py` (pure, module conventions): formula EXACTLY per ADR-0040 (read it — do not improvise a ratio). `utility=0` → `inf` (never ZeroDivision, never NaN — `_finite` guard pattern). Hostile huge/negative/NaN inputs → finite/`inf` or `ValueError` per ADR-0040 (assert the ADR's rule; if ADR-0040 is silent on a class, ValueError — the bandwidth: never NaN).
- Curator `_phase_analyze`: emit a deprecation SUGGESTION per fact above the ADR-0040 threshold ONLY when `CostGateSettings.enabled` (consume the W1 Settings; disabled → zero suggestions, zero cost function calls beyond the gate check). Suggested shape per ADR-0040 (suggestion = log/metric line `cost_gate.suggestions` + report field; fact stays in the hot set — NEVER delete/never move on cost alone). `CostGateSettings.enabled=False` default keeps production inert.
- Observability: `cost_gate.suggestions` metric/log lines (plan §K). Curator report gains a suggestions list (append-only; existing report fields untouched).
- Tests (must FAIL before, pass after): T09 — `total_memory_cost(…, utility=0)` → `inf` + curator logs suggestion + fact STILL in hot set (assert the fact row exists after analyze); boundary storage/maintenance=0; hostile negative/NaN → the ADR's verdict (ValueError or inf); idempotency — same cost twice = same suggestion, no double execution side-effect (no delete at all); disabled flag → no suggestions. New `tests/memory/test_cost_gate_p008.py` (+ metrics unit cases inside it or `tests/context/test_metrics.py` append — follow existing placement; if appended to test_metrics.py that file is in your write-set for the append).
- Trace tag `PLAN:integration-fix:P008`.
- Acceptance: T09 green with fail-before shown; `tests/context/test_metrics.py` (152) green (append-only); `tests/memory/test_curator*` green; ruff 0, mypy total 3; no deletes anywhere in the diff (`git diff | grep -i "delete"` audit in your report); no config/schema edits.

## 4. Ownership
WRITE-SET (only these): `src/paa/context/metrics.py`, `src/paa/memory/curator.py` (ANALYZE suggestion only), `tests/context/test_metrics.py` (append only), `tests/memory/test_cost_gate_p008.py` (new). READ-ONLY: everything else. FORBIDDEN: `src/paa/config.py`, `docs/adr/*`, `src/paa/core/types.py`, `src/paa/ledger/*`, any DELETE in curator.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: COMMS CLAIM before editing `curator.py` (W3 tier-seam history — different phase, same file).

## 5. Parallel siblings this wave
W5-IMPL-02 (coding pilot — `agents/specialists/*`, `types.py`, `runtime.py`, `orchestrator.py`), W5-IMPL-03 (allocator scheduling — `daemon.py`, `runner.py`, `creator.py`), W5-DOCS (docs only). Disjoint; you own metrics + curator ANALYZE.

## 6. Skip list and known traps
- Auto-delete above threshold is REJECTED (violates never-auto-resolve) — the suggestion is the whole feature.
- `curator` phases: DECAY→…→RECONCILE_VECTORS→ANALYZE — your hook is ANALYZE only (W3's tier seam is DECAY-tail; do not disturb).
- ADR-0040 is authoritative on formula/threshold/term derivations; where it says "unavailable" the term is an input, not a TODO.
- Baseline failures are not yours.

## 7. Environment and tests
Scoped only: your test file(s) + `tests/context/test_metrics.py` + curator tests + ruff/mypy scoped. Tmp fixtures only. No full suite.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: T-008-P008`, `Agent: W5-IMPL-01`, `Wave: W5`, `Report: docs/agent-ops/20261107-integration-fix/reports/w5-implementation/W5-IMPL-01.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w5-implementation/W5-IMPL-01.md` (create at start, ORCH-DIGEST first, append as you work; long-run answers incl. 100× ticks no growth). COMMS ≤6 appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
