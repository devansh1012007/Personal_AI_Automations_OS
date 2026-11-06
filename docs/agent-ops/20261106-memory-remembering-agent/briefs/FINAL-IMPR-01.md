# Brief — FINAL-IMPR-01 (IMPR) · final improvement · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Close final-review findings (F-01 MAJOR + FINAL-01..05, F-02..05 minors/nits) before final gate/SUMMARY.
Role: improver.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (gate, docs conventions, redact patterns)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — J (impact), K (rollout/rollback), L (risks)
5. Final reports in FULL: reports/final/FINAL-01-correctness.md (FINAL-01..05), FINAL-02-operability.md (F-01..05)
6. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-improvement.md
7. COMMS.md + gates/gate-w4-2.md

## 3. Findings to disposition (address EVERY one)
- F-01 [MAJOR] Operator docs stale (maintenance described as bare sweep) + snapshot/rollback guidance missing → FIX docs: update docs/modules/daemon.md, docs/operations/deployment-and-runtime.md, docs/modules/memory.md, docs/deployment/docker.md (embeddings caveat) to describe: daemon runs full curator (first tick dry-run preview 6h after boot, second deletes), snapshot cold lake before enabling, cold-lake replay restores deleted facts — versus code revert does NOT restore. One authoritative runbook section (pick deployment-and-runtime.md as home; others link). Verify every `path:line` you add/keep (conventions must pass). Cold-lake restore procedure must be truthful (re-run creator over signals; name commands from CLI/API that exist — verify).
- F-02 [MINOR] 3 new error lines unredacted → FIX redact them (use `redact` per models/base.py; verify with grep no new `error=str(exc)` in changed lines is acceptable or justify per convention seen in creator.py).
- F-03 [MINOR] Concurrent drain duplication + curator unbounded phase loads → FIX only if truly minimal (claim/lease for drain? skip — 5 concurrent drains are idempotent overwrite; DEFER with evidence if so). Curator memory: DEFER (current scale safe; note follow-up). Record choice.
- F-04 [MINOR] Drain throughput under load (hot-loop throttles ingestion) → FIX or DEFER with evidence: adding a yield/backoff sleep is a behavior change — prefer DEFER + operator note (backlog size + embedder speed bound) unless repro shows starvation. Do not add unbounded sleeps.
- F-05 [NIT] Plan §K rollback wording → FIX plan file? Plan is outside repo (C:\Users\devan\.opencode\plan) — do NOT edit plan file (plan mode artifact). Instead: note correction in SUMMARY + operator docs. REJECT-edit with reason.
- FINAL-01 [MINOR] Stale coder note ("no vector sink wired") → FIX: correct the user-facing note in runtime_tools.py (in your write-set) to reflect C01-wired state; add assertion in e2e test 2 (tests/memory/test_e2e_remember_recall.py) pinning the corrected note. READ the exact note text first.
- FINAL-02 [MINOR] Fallback floor divergence (injected vs global) → FIX or DEFER honestly: daemon fallback constructs `DecaySweeper` without floor injection; curators injected floor handled Wave 3. For the fallback, pass floor explicitly (read how explicit floor is stored post-Wave-3; reuse same path). Test both paths or defer with reason.
- FINAL-03 [MINOR] E2E bypasses touch → FIX test: extend e2e to call the planner handle path (packet.elements) or assert use_count advance via handle-level call — direct `_semantic` alone is insufficient for the FINAL-01 claim. Keep existing test, add touch assertion (or new test).
- FINAL-04 [NIT] Duplicated helpers → DEFER (refactor, not correctness), record.
- FINAL-05 [NIT] Drain limit never plumbed + reconcile preview 0 → DEFER or FIX trivially; record (reconcile preview 0 is truthful for stores lacking retain_only — verify + if so, document, not code).

## 4. Ownership
WRITE-SET (may edit ONLY these):
- src/paa/coder/runtime_tools.py
- src/paa/daemon.py
- src/paa/memory/creator.py
- src/paa/memory/decay.py
- tests/memory/test_e2e_remember_recall.py
- tests/daemon/test_maintenance_loop.py
- docs/modules/daemon.md
- docs/modules/memory.md
- docs/operations/deployment-and-runtime.md
- docs/deployment/docker.md
- docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md
READ-ONLY: everything else (including plan file outside repo, schema, config.py, context_agents.py, docs/README.md, VISION.md).
FORBIDDEN: hotspot schema, pyproject, CI.
Need outside write-set? Don't edit; post NEED; DEFERRED-DEPENDENCY.

## 5. Tests & commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py tests/daemon/ tests/memory/ -q`
`.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q`
`.venv\Scripts\python.exe -m ruff check src tests` · `.venv\Scripts\python.exe -m mypy src` (expect baseline 3)
New/strengthened tests must fail without the corresponding fix; note reversal evidence.

## 6. Deliverables
Improvement report at docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md (ORCH-DIGEST first; finding-by-finding FIXED/REJECTED/DEFERRED + evidence + commit).
Commits: code, then docs/report, trailers Task: FINAL / Agent: FINAL-IMPR-01 / Wave: FINAL / Report: <path>. Final ≤8 lines.
