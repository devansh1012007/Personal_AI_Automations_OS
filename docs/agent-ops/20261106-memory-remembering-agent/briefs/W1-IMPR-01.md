# Brief — W1-IMPR-01 (IMPR) · wave 1 improvement · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Close Wave 1 review findings for T-001 (C01+C01b+C02) — every BLOCKER/MAJOR must end FIXED, REJECTED (with evidence), or DEFERRED (with reason). Improve code AND tests.
Role: improver. Same write-set as the original implementer, plus daemon test file.

## 2. Required reading (in this order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (gate, conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline — do not touch pre-existing failures)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — C01, C02, C01b, T01–T03
5. Implementer report: docs/agent-ops/20261106-memory-remembering-agent/reports/w1-impl/W1-IMPL-01-T-001.md
6. Reviews (read in FULL): docs/agent-ops/20261106-memory-remembering-agent/reports/w1-review/REV-code.md and TREV-tests.md
7. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-improvement.md
8. COMMS.md from last WAVE-START

## 3. Findings to disposition (exact list — address EVERY one)
CODE (R-W1-001–007):
- R-W1-001 [MAJOR] Production composition untested → FIX: add (a) `Runtime.build` with fake backends asserting non-None sink (TREV P1-2 sketch; monkeypatch `_try_vector`), (b) daemon ingestion-loop drain test (P1-1 sketch, reuse `tests/daemon/test_drain_e2e.py` harness).
- R-W1-002 [MINOR] Drain single-pass; backlog beyond 100 invisible → FIX honestly within write-set: log must not call batch size the backlog (rename log field + count remaining pending via existing `pending_embeddings` API, e.g. a second lightweight probe) OR check callers; do NOT loop-until-empty without a bound (unbounded loop banned). If remaining-count needs a new facts.py method, do it via existing API only and note limitation — do NOT edit facts.py.
- R-W1-003 [MINOR] Degraded warning duplicates `runtime.vector_unavailable` on intentional minimal installs → choose FIX (silence duplicate keeping one signal) or REJECT with evidence that the warning is intentional per D2a. Either way record rationale.
- R-W1-004 [MINOR] Frozen payload snapshots; superseded facts stay indexed → DEFER (planned: curator/reconcile in Wave 2 C04/T03). Mark DEFERRED with reason; no code change expected, but confirm no crash.
- R-W1-005 [NIT] Silent truncation on embedder short-return → FIX: warn when `len(vectors) != len(facts)` in `VectorEmbeddingSink.enqueue` (or prove existing handling covers it).
- R-W1-006 [NIT] Hand-rolled monkeypatch restore in test → FIX using `monkeypatch` fixture.
- R-W1-007 [NIT] Unredacted `str(exc)` in sink error logs → FIX if project convention redacts (check creator.py logs); else REJECT with convention evidence.
TESTS (R-W1-01–09 + P1/P2 sketches in TREV §6):
- R-W1-01 [MAJOR] Daemon drain untested → FIX (same as R-W1-001b).
- R-W1-02 [MAJOR] Runtime.build untested → FIX (same as R-W1-001a).
- R-W1-03 [MAJOR] Fakes ignore collection/vector/texts → FIX: record collection + embed texts in fake; assert `["active_facts"]` and embedder saw `[fact.statement]` (P1-5).
- R-W1-04 [MINOR] limit/partial semantics untested → FIX P1-3 (backlog+limit incl. 105-fact) and P1-4 (partial/empty sink return).
- R-W1-05 [MINOR] Boundary/error gaps → FIX P2-7 (real-sink embedder/store raise via drain + write path) and P2-8 (API/CLI drain-failure isolation). Add P2-6 (concurrent double-drain) if cheap and deterministic.
- R-W1-06 [MINOR] Payload ranking keys under-pinned → FIX P2-10 exact values + `_semantic` element match.
- R-W1-07 [MINOR] Two tests pass pre-fix (pinning) → REJECT or CLARIFY (rename/doc that they pin, not reverse). Record choice.
- R-W1-08 [NIT] White-box privates assertions → REJECT (consistent with file style) or justify each kept.
- R-W1-09 [NIT] `_settings()` leaks temp dirs; CLI cache reset → FIX if trivial (tmp_path fixture).
Leave P3 items (11–15) deferred unless trivial; list them as DEFERRED in report.

## 4. Ownership
WRITE-SET (may edit only these): src/paa/runtime.py, src/paa/memory/creator.py, src/paa/daemon.py, src/paa/api/app.py, src/paa/cli/main.py, tests/memory/test_creator.py, tests/runtime/test_memory_creator.py, tests/api/test_memory.py, tests/cli/test_memory.py, tests/daemon/test_drain_e2e.py, docs/agent-ops/20261106-memory-remembering-agent/reports/w1-improve/IMPR-01.md
READ-ONLY: everything else (especially src/paa/agents/context_agents.py, src/paa/memory/facts.py, src/paa/config.py, schema files, docs).
FORBIDDEN: hotspot schema files, pyproject.toml, CI config. Need outside write-set? Don't edit; post NEED; report DEFERRED-DEPENDENCY.

## 5. Tests & commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/memory/test_creator.py tests/runtime/test_memory_creator.py tests/daemon/test_drain_e2e.py tests/api/test_memory.py tests/cli/test_memory.py -q`
`.venv\Scripts\python.exe -m ruff check src tests`
`.venv\Scripts\python.exe -m mypy src` (expect only baseline 3)
New tests must fail without the fix; note reversal evidence.

## 6. Deliverables
Improvement report at docs/agent-ops/20261106-memory-remembering-agent/reports/w1-improve/IMPR-01.md (ORCH-DIGEST first; finding-by-finding table with FIXED/REJECTED/DEFERRED + evidence + commit).
Commits: code first, then report, trailers `Task: T-001` / `Agent: W1-IMPR-01` / `Wave: 1` / `Report: <path>`. Explicit paths only. Stay on ops/20261106-memory-remembering-agent.
Final message (≤8 lines): STATUS | agent | scope | report path | commit SHAs | open BLOCKER/MAJOR.
