# Brief — W0-INV-01 (INV) · wave 0 · run 20261107-integration-fix

## 1. Role and mission
Mission: verify plan items P-002, P-009, P-010 (memory core) against live code at HEAD `88e648b`, isolate exact write/contract sets, and propose task breakdown. Why: plan rev5 cites file:line that must be re-verified (staleness rule); a wrong write-set causes wave conflicts.
Your role: investigator (READ-ONLY on source; you edit only your report file).

## 2. Required reading (in this order)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md` (binding)
2. `AGENTS.md` (zero-baseline gate, ledger invariant, schema parity lock, lazy extras)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md`
4. Plan: `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — items P02, P09, P10 verbatim (§E table rows + §H tests T02/T10/T10b/T11 + §I edge cases)
5. Template: `docs/agent-ops/20261107-integration-fix/templates/report-investigation.md`
6. COMMS.md from the last WAVE-START
7. Code entry points: `src/paa/memory/{decay,curator,creator,domains,world_model}.py`, `src/paa/storage/relational/{migrations.py,schema_sqlite.sql,schema_postgres.sql}`, `src/paa/ledger/{replay.py,recovery.py,events.py}`

## 3. Task and acceptance criteria
Task T-001: For each of P-002 (query-time confidence derivation), P-009 (T0–T4 tier layer + migration + replay rule), P-010 (15-file dual vault + governance fence) — classify EXISTS/ALREADY-FIXED/NOT-REPRODUCIBLE/PARTIAL/MISCLASSIFIED, root-cause chain, blast radius (grep counts), fix options, classification SURFACE/DEEP, exact WRITE-SET, CONTRACT-SET, test plan (must-fail-before), conflicts/ordering.
Specifically verify:
- P02: does gatherer projection carry `domain` + `last_queried_at` (decay.py:56 basis)? Where does `confidence_at` get its inputs? Who reads Qdrant payload `confidence` today?
- P09: exact current `hot_serving_active_facts` columns; migration step numbering (MIGRATIONS list tail); `_NO_PHASE_CHANGE` vs `_PHASE_MAP` mechanics in replay.py; how recovery.py handles unknown events today.
- P10: `WorldModel` read/write API (world_model.py:89 `vault_path`), managed-block fence mechanics (:67-68, :177-185), who reads the 4 enum files (grep `BeliefDocument`), whether any code writes vault files besides world_model.
Acceptance: report at `reports/w0-investigation/W0-INV-01.md` using template, ORCH-DIGEST first, every claim tagged.

## 4. Ownership
WRITE-SET (may edit): `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-01.md` only.
READ-ONLY: everything else in the repo.
FORBIDDEN: any `src/` file, any schema file, COMMS.md (you append one line max via printf), git commits (investigators don't commit; ORCH commits reports).

## 5. Parallel siblings this wave
| Agent | Owns |
|---|---|
| W0-INV-02 | storage: like.py, schema/migrations, vector, queue, graph (P-003, P-007-queue, P-009-schema, P-012) |
| W0-INV-03 | context/agents: gatherer, metrics, allocator, orchestrator, specialists (P-007, P-008, P-011) |
| W0-INV-04 | surfaces: coder, api, cli, runtime, tui (P-001, P-004, P-006) |
| W0-INV-05 | build/ops: Dockerfile, pyproject, CI, reflection, config (P-005, P-013, P-014) |
Note overlap: P-009 spans your domain (memory+replay) and INV-02 (schema) — you own the replay/tier-policy half, INV-02 owns the DDL/migration half; split in your reports, flag CONFLICT if you disagree.

## 6. Skip list and known traps
- Plan claims already verified by 4 reviewer rounds (phantom counts 21/9 closed) — do not re-litigate; re-verify only file:line freshness.
- Pre-existing mypy 3 errors at `subprocess_backend.py:548` — not yours, record as finding if touched.
- `tests/memory/test_decay.py` must stay unchanged/green — note it as regression guard.

## 7. Environment and tests
Scoped commands only: grep/rg, `git log/blame`, targeted `python -m pytest tests/memory/test_decay.py -q` if needed to confirm a claim (read-only run OK; it uses tmp_path fixtures). Do NOT run the full suite, do NOT run migrations, do NOT start containers.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-01.md` (start early; ORCH-DIGEST first).
Final message (≤ 8 lines): STATUS | agent | tasks | report path | blockers.
