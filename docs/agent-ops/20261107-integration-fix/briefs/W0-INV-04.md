# Brief — W0-INV-04 (INV) · wave 0 · run 20261107-integration-fix

## 1. Role and mission
Mission: verify plan items P-001 (redaction), P-004 (touch dedup + coder semantic fallback), P-006 (5 TUI panels) against live code at HEAD `88e648b`. Why: P-001/P-004 are Phase-1 surface fixes landing first — exact call sites and test seams must be pinned.
Your role: investigator (READ-ONLY on source; you edit only your report file).

## 2. Required reading (in this order)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md` (binding)
2. `AGENTS.md` (zero-baseline, redaction conventions, TUI conventions if any)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md`
4. Plan: `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — items P01, P04, P06 verbatim + tests T01/T04/T05/T07
5. Template: `templates/report-investigation.md`
6. Code: `src/paa/runtime.py` (:480-481), `src/paa/cli/main.py` (:646-649, :642 C02-tag area), `src/paa/coder/runtime_tools.py` (:746 recall_facts, :807 note, :901 touch), `src/paa/models/base.py` (:256,:278 redact), `src/paa/api/app.py` (:346 touch), `src/paa/agents/context_agents.py` (:197 touch), `src/paa/tui/{snapshot.py,dashboard.py,dispatcher.py,runner.py}`, `src/paa/memory/` (for new touch.py home)

## 3. Task and acceptance criteria
Task T-004: classify P-001, P-004, P-006 with evidence.
Specifically verify:
- P01: the exact 4-line context of `runtime.py:481` and `cli/main.py:649` — what exception, is any secret-shaped value plausibly in scope (provider errors wrapping URLs/keys)? What does `redact`/`redact_detail` already handle (models/base.py:256-278 — list its patterns)? Where would the 200-char truncation spec live? Existing redaction tests (grep `redact` in tests/) — which cover which sites?
- P04: all 4 `_touch_recalled` copies (api/app.py:346, context_agents, cli/main.py:714, coder/runtime_tools.py:901) — diff them line-by-line (are they truly identical? any drift like the stale `note:807`?). `recall_facts` current flow (keyword-only at :746) — what vector-search API exists for coder to call (storage/vector/store.py public surface), and how planner-side semantic recall does it (grep semantic recall in context/ or agents/). Where should `memory/touch.py` live vs existing memory module conventions.
- P06: `snapshot.py` current fields (:37-42), `dashboard.py` render loop (:83-118), `dispatcher.py` command kinds (:19-28), `runner.py` data sources (:56-121) — what data feeds each of the 5 planned panels (attention_top, memory_browser_counts, decision_tracker, autonomy_state, agent_monitor) and which are unavailable today (mark missing-data gaps). API side: which endpoints exist in api/app.py that could serve the same fields.
Acceptance: report at `reports/w0-investigation/W0-INV-04.md`, ORCH-DIGEST first, claims tagged.

## 4. Ownership
WRITE-SET: `reports/w0-investigation/W0-INV-04.md` only. READ-ONLY: all source. FORBIDDEN: src edits, commits, COMMS beyond one printf append.

## 5. Parallel siblings this wave
INV-01 (P-002 gatherer/vector read — P-004's fallback depends on the same vector store), INV-02 (relational LIKE for coder recall), INV-03 (allocator), INV-05 (CI for T06-style gates).

## 6. Skip list and known traps
- The 4 touch copies: plan says delete 3, keep 1 via `memory/touch.py` — verify they're actually identical first; if drifted, report the drift as a finding (do not assume).
- T01's "no secrets in scope" nuance — determine what IS in scope at those two sites (this was a reviewer finding; resolve it with evidence).
- TUI panels must reuse existing snapshot/API (D11: no new frontend).

## 7. Environment and tests
Scoped: grep, git, targeted `pytest tests/coder tests/tui tests/api -q` if present (tmp_path). No full suite, no containers, no server binds (api tests use loopback fixtures only).

## 8. Deliverables
Report: `reports/w0-investigation/W0-INV-04.md`. Final message ≤ 8 lines.
