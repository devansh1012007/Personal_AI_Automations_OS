# Brief — W0-INV-02 (INV) · wave 0 · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Verify decay/touch, curator scheduling, and config hygiene (plan C03, C04, C06, tests T04, T05, T07) — why does retention never see use, who should run the curator, and is `prune_confidence_floor`/`decay_lambda` drift real? Failure looks like: used facts still expire, maintenance phases never run.
Role: investigator (read-only on source).

## 2. Required reading (in this order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (memory/ledger sections)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — items C03, C04, C06, T04, T05, T07, Q1
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-investigation.md
6. COMMS.md from start

## 3. Task and acceptance criteria
Verify with evidence: (a) `DecaySweeper.touch/touch_many` contract, batching, failure modes and why no production caller exists; (b) `MemoryCurator.run_maintenance` phases, budget param, resume behavior, and how the daemon maintenance loop is structured today (`daemon.py`); (c) DEPLOY SIDE Q1: grep compose/deploy/systemd/cron configs for any `MemoryCurator` scheduling; (d) `MemorySettings` fields vs `domains.py` `domain_policy` — is `decay_lambda` read anywhere? is `prune_confidence_floor` read anywhere? (e) `DecaySweeper.sweep` floor source today.
Acceptance: report with `[VERIFIED path:line]` per claim, WRITE-SET (exact files for C03/C04/C06), CONTRACT-SET, test plan failing before fix, SURFACE vs DEEP.

## 4. Ownership
WRITE-SET: only `docs/agent-ops/20261106-memory-remembering-agent/reports/w0-investigation/INV-decay-curator-config.md`
READ-ONLY: everything else. FORBIDDEN: source edits, installs, full test suite.

## 5. Siblings
W0-INV-01 (creator/runtime), W0-INV-03 (recall/extractor/tests).

## 6. Deliverables
Report: `docs/agent-ops/20261106-memory-remembering-agent/reports/w0-investigation/INV-decay-curator-config.md` (ORCH-DIGEST first)
Final message (≤8 lines): STATUS | agent | task | report path | blockers
