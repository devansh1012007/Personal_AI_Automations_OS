# Brief — W2-IMPL-04 (IMPL) · wave 2 · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Implement plan C04 (T-004, test T05): schedule `MemoryCurator.run_maintenance` in the daemon maintenance loop so decay/prune/consolidate/orphans/reconcile/analyze actually run. Failure looks like: curator phases never execute in production.
Role: implementer (coder). ROOT CAUSE only; no band-aids.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (ledger invariant, gate, conventions; snapshot/cold-lake rules for R3)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — item C04, T05, risks R3/Q1
5. Investigation: docs/agent-ops/20261106-memory-remembering-agent/reports/w0-investigation/INV-decay-curator-config.md §C04 (read in FULL — Q1 confirmed no deploy cron; curator phases + budget + resume behavior + Runtime wiring gaps)
6. Wave-1 reports (context): gates/gate-w1-2.md
7. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-implementation.md
8. COMMS.md from last WAVE-START
9. Source to verify (don't trust): src/paa/daemon.py maintenance loop (currently DecaySweeper.sweep only), src/paa/memory/curator.py run_maintenance signature/budget/resume, Runtime fields (settings, vector_store, archive_writer availability on rt — read runtime.py), docs/operations/deployment-and-runtime.md + background-jobs docs for interval claims

## 3. Task and acceptance criteria
Task T-004 (C04): daemon maintenance loop calls `MemoryCurator.run_maintenance(budget_seconds=...)` instead of (or in addition to, documented choice) bare `DecaySweeper.sweep()`. Budget: use existing `MemorySettings.curation_max_runtime_hours` (2.0h → 7200s) — do NOT add new config. Wire constructor args from the real Runtime (settings? vector_store? archive_writer? — read them; do not guess attribute names).
Rules:
- R3 safety: curator delete phases (consolidate/orphans/prune-edges) have NEVER run in prod before. Enable them but keep them observable: log phase starts/ends + counts; if curator construction/wiring fails, fall back to bare `DecaySweeper.sweep()` with a warning (degrade-never-raise, and document it — no silent double-run).
- Never let a curator exception kill the daemon maintenance loop; never block ingestion loop (maintenance runs in its own loop/task, not inline in `_ingestion_loop`).
- Respect existing `maintenance_interval`/`decay_sweep_interval_hours` cadence (verify which drives the loop; document).
- Docs-reference lock: daemon.py line shifts break docs citations (Wave 1 proved it). Run `pytest tests/conventions/test_docs_references.py -q`; if broken, POST NEED with exact files (docs are outside your write-set) and report DEFERRED-DEPENDENCY — do not edit docs.
Acceptance:
- T05: maintenance tick drives all curator phases under budget (recorded/mocked phases or real run on seeded DB, asserting orphan/duplicate/vector-reconcile effects).
- Daemon still boots when vector store is None (curator gets None or degraded, sweep still runs).
- Existing daemon tests stay green; scoped tests §7 pass; ruff clean; mypy = baseline 3 only.

## 4. Ownership
WRITE-SET (may edit ONLY these):
- src/paa/daemon.py
- tests/daemon/test_drain_e2e.py
- tests/daemon/test_maintenance_loop.py (new test file — allowed to create)
- docs/agent-ops/20261106-memory-remembering-agent/reports/w2-impl/W2-IMPL-04-T-004.md
READ-ONLY: everything else (especially src/paa/memory/curator.py, src/paa/memory/decay.py, src/paa/runtime.py, src/paa/config.py, schema, docs). If curator.py needs a change, POST NEED + DEFERRED-DEPENDENCY, do not edit.

## 5. Siblings (same wave, parallel)
| Agent | Task | Owns | Contracts changed |
| W2-IMPL-03 | T-003 | context_agents, runtime_tools, api, cli + their tests | touch-on-recall |
Disjoint write-sets. If a scoped test fails because of sibling files, wait/retry once, then FINDING/CONFLICT — do not fix sibling files.

## 6. Skip list and traps
- Q1 is confirmed (no deploy cron) — C04 daemon wiring carries no double-run risk. Do not add CLI/cron.
- Cursor phase order + resume: `run_maintenance(..., start_phase=None, now=None)` — preserve it.
- `MemoryCurator` signature — read it; it takes `settings=None, sweeper=None, vector_store=None, archive_writer=None, batch_size=500`. Reuse one sweeper instance if needed.
- Pre-existing: mypy 3 win32, pytest 12 Windows, 2 non-blocking doc stales (IMPR-02) — not yours.

## 7. Commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/daemon/ -q`
`.venv\Scripts\python.exe -m ruff check src tests`
`.venv\Scripts\python.exe -m mypy src` (expect baseline 3)
`.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` (watch your own shifts)
New tests must fail without the fix; note reversal evidence.

## 8. Deliverables
Report: docs/.../reports/w2-impl/W2-IMPL-04-T-004.md (ORCH-DIGEST first). Commits: code, then report, trailers Task: T-004 / Agent: W2-IMPL-04 / Wave: 2 / Report: <path>. Final ≤8 lines.
