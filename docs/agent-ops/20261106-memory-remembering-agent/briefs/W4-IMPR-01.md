# Brief — W4-IMPR-01 (IMPR) · wave 4 improvement · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Close Wave 4 review findings + fix the 3 stale daemon tests. Improve code AND tests.
Role: improver.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (gate, testing conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — C07/T08 (re-scoped)
5. Implementer report: docs/agent-ops/20261106-memory-remembering-agent/reports/w4-impl/W4-IMPL-07-T-007.md
6. Reviews in FULL: docs/agent-ops/20261106-memory-remembering-agent/reports/w4-review/REV-code.md, TREV-tests.md
7. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-improvement.md
8. COMMS.md + gates/gate-w4-1.md (stale-test triage)

## 3. Findings to disposition (address EVERY one)

### Gate stale tests (already triaged — FIX)
- Two fallback tests (construction-failure + import-failure): update to preview contract — first tick dry_run (fact remains), second tick (preview consumed) deletes. Assert both ticks.
- Orphan duplicate test (`test_orphan_purge_skips_when_entity_index_empty`): align to Wave 3 refined semantics (empty-index orphans ARE swept when provenance says dangling, per test_curator). Either correct the assertion (expect removal) or remove the duplicate (test_curator owns it) — choose one, record why, update docstring.

### CODE (R-W4-001–006)
- R-W4-001 [MAJOR] Fallback preview never consumed → FIX: construction-failure fallback tick must eventually consume preview when curator returns (e.g. set `_curate_preview_done` only on the curator path, not fallback; make fallback mark preview consumed after a successful dry-run preview then delete on next tick). Permanent breakage must degrade to preview-forever with loud warning + logged counts (safe direction), or delete after N previews with operator warning — choose the safe direction and test it.
- R-W4-002 [MINOR] Single erroring phase blocks deletes forever → FIX or DEFER honestly: if you gate on `errors==0`, one broken phase blocks all. Investigate per-phase error isolation — if curator already isolates per phase (report.errors increments, continues), errors==0 may be too strict; consider completed_fully-only gate. Decide with evidence, add test proving one erroring phase does/does not block deletes.
- R-W4-003 [MINOR] pending_count COUNT on every drain incl. idle → FIX (smallest): skip COUNT when queue known empty (reuse pending probe: only COUNT when pending_embeddings returned non-empty, or keep COUNT but document cost + drop idle cost). Test both.
- R-W4-004 [MINOR] outcome asymmetry IGNORED vs PROCESSED → FIX: add `outcome` to PROCESSED (or remove from IGNORED with evidence dashboards rely on it — prefer additive).
- R-W4-005 [NIT] e2e bypasses drain path → FIX: add one e2e variant writing WITHOUT sink (or with sink cleared) then drain→indexed→_semantic hit (proves the backlog leg).
- R-W4-006 [NIT] failure-path gauge → FIX if trivial: include pending_count on `embedding_drain_failed` log line.

### TESTS (TREV R-W4-T-01–05 + P1 sketches §6)
- T-01: add `_build_memory_creator`-threaded e2e variant (option i) or narrow docstring honestly (option ii) — prefer (i) if cheap, else (ii) + note.
- T-02: 2-fact decoy ranking test (P1).
- P1 (§6): implement both P1 items (second: drain→recall path).
- T-03 stale note ("no vector sink wired" in runtime_tools.py): update the note text? `runtime_tools.py` is OUTSIDE your write-set — do NOT edit it. POST NEED + DEFERRED-DEPENDENCY listing the one-line fix, and (if trivial) assert `note` in test 2 of your file? Test 2 is in your write-set file — adding the note assertion there is allowed; the note text fix itself is not. Choose: add assertion pinning current note (flexible) or leave; record.
- T-04/T-05 NITs: fix if trivial (dimensions import/assert; failure gauge — already R-W4-006).

## 4. Ownership
WRITE-SET (may edit ONLY these):
- src/paa/daemon.py
- src/paa/memory/creator.py
- tests/memory/test_e2e_remember_recall.py
- tests/daemon/test_maintenance_loop.py
- docs/agent-ops/20261106-memory-remembering-agent/reports/w4-improve/IMPR-01.md
READ-ONLY: everything else (including docs/, schema, config, curator.py, context_agents.py, runtime_tools.py).
FORBIDDEN: hotspot schema, pyproject, CI.
Need outside write-set? Don't edit; post NEED; DEFERRED-DEPENDENCY.

## 5. Tests & commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py tests/memory/ tests/daemon/ -q`
`.venv\Scripts\python.exe -m ruff check src tests` · `.venv\Scripts\python.exe -m mypy src` (expect baseline 3)
New/strengthened tests must fail without the corresponding fix; note reversal evidence.

## 6. Deliverables
Improvement report at docs/agent-ops/20261106-memory-remembering-agent/reports/w4-improve/IMPR-01.md (ORCH-DIGEST first; finding-by-finding FIXED/REJECTED/DEFERRED + evidence + commit).
Commits: code, then report, trailers Task: T-007 / Agent: W4-IMPR-01 / Wave: 4 / Report: <path>. Final ≤8 lines.
