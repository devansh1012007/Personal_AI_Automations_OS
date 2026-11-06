# Brief — W0-INV-03 (INV) · wave 0 · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Verify recall-path touch insertion points, extractor drop behavior, and the test-gap map (plan C03, C05, C07, tests T04, T06, T08) — where exactly is recall invoked (5 named paths), what does the rule extractor silently drop, and which e2e tests are missing? Failure looks like: recall works but retention/touch never sees the read, and extraction silently loses facts.
Role: investigator (read-only on source).

## 2. Required reading (in this order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (memory/ledger sections)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — items C03, C05, C07, T04, T06, T08
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-investigation.md
6. COMMS.md from start

## 3. Task and acceptance criteria
Verify with evidence: (a) the 5 recall call sites with exact symbol names and returned fact IDs (so a later `touch_many` can use them): `context_agents._hot_facts/_semantic`, `coder/runtime_tools.recall_facts`, `api.list_facts/search_facts`, `cli.facts/search` inner `_do`; (b) `RuleBasedExtractor` drop behavior for pronoun subjects (`I/we`), hedges, questions — with line evidence, and what `ProcessingResult` does/doesn't report today; (c) test map: which of T04/T06/T08 already exist and which are genuinely new, and the existing `remember_fact`→recall test at `tests/coder/test_runtime_tools.py` — what restart/recall gap remains; (d) `recall_facts` tool contract (limit, entity resolution, semantic fallback note).
Acceptance: report with `[VERIFIED path:line]` per claim, WRITE-SET (exact files for C03/C05/C07), CONTRACT-SET, test plan failing before fix, SURFACE vs DEEP.

## 4. Ownership
WRITE-SET: only `docs/agent-ops/20261106-memory-remembering-agent/reports/w0-investigation/INV-recall-extractor-tests.md`
READ-ONLY: everything else. FORBIDDEN: source edits, installs, full test suite.

## 5. Siblings
W0-INV-01 (creator/runtime), W0-INV-02 (decay/curator/config).

## 6. Deliverables
Report: `docs/agent-ops/20261106-memory-remembering-agent/reports/w0-investigation/INV-recall-extractor-tests.md` (ORCH-DIGEST first)
Final message (≤8 lines): STATUS | agent | task | report path | blockers
