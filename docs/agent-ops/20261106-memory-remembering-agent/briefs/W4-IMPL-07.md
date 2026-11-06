# Brief — W4-IMPL-07 (IMPL) · wave 4 · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Implement plan T-007/C07 (T08 e2e + observability) and close the 3 carried MINORs (R-W2-D-01, R-W2-D-02, W3 ignored-drop INFO). Failure looks like: paraphrase recall still unproven end-to-end, metrics/logs absent, curator preview edge cases.
Role: implementer (coder). ROOT CAUSE only; no band-aids. (Installing/launching is NOT part of this wave; launch smoke test is the final gate after Wave 4.)

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (gate, testing conventions, observability conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — item C07, T08 (re-scoped: planner `_semantic`, coder recall stays keyword-only per DECISION T-007)
5. Investigations + reviews (context): reports/w0-investigation/INV-*.md; reports/w1-review/REV-code.md (R-W1-002 backlog), reports/w2-review/REV-code.md (R-W2-005 budget wording), reports/w3-improve/IMPR-01.md (INFO counts), reports/w2-review/DELTA-review.md (R-W2-D-01/02)
6. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-implementation.md
7. COMMS.md from last WAVE-START
8. Source to verify (don't trust): creator.py signal_processed / IGNORED log sites, daemon.py preview flag + construction-fallback (lines ~250-280), cursor/curator phase logs, context_agents._semantic, storage vector NumpyVectorStore/HashEmbedder capabilities

## 3. Task and acceptance criteria
Task T-007a (T08 e2e): NEW file `tests/memory/test_e2e_remember_recall.py` proving: write fact → close/reopen DB (restart) → planner `_semantic` (via `PlannerContextBuilder` or direct `VectorStore.search`) retrieves it by paraphrase. Use a fake embedder that encodes synonyms/word-order (document why: `HashEmbedder` is lexical-only, no paraphrase — see 00-mission; real `sentence-transformers` is out of dev extra). Use a REAL extractable store path (Numpy or Fake store with real `upsert`/`search` semantics — add reasoning) and a REAL `MemoryCreator` (not hand-built points). Assert the retrieved element's `content` is non-empty and matches the stored object_value, and `embedding_status` is `indexed` after restart (pending drained before close or on reopen — document). Add a second test: synonym-only paraphrase via `recall_facts` is expected to MISS (documents keyword-only boundary) — or skip with explicit rationale if that test would be misleading. Tests must be deterministic (no sleeps, no network).
Task T-007b (observability): pending-embedding count + curator phase timing + embedding_status visibility, using EXISTING log conventions (`log.info` structured, no secrets/PII). Survey what Wave 1/2 already log; add only what is missing (likely: backlog gauge after drain, curator phase timings already in CurationReport — ensure they are logged, IGNORED-drop INFO below). Do NOT invent a metrics backend; use structlog only. If pending-count gauge exists, pin it with a test; if not, add the log and a test asserting it (via caplog).
Carried MINORs:
- R-W2-D-01 (daemon.py:258): construction-failure fallback bypasses preview. FIX: when curator construction fails, fallback must respect the preview contract (dry-run sweep or log-only, not full deletes) + test proving construction failure does NOT delete on first tick.
- R-W2-D-02 (daemon.py:271): preview flag consumed by partial/errored preview. FIX: only clear the preview flag on a COMPLETE successful preview; errored/partial preview keeps preview on for next tick + test.
- W3 INFO (R-DELTA-002): IGNORED signals carry drops at DEBUG. FIX: promote IGNORED drop counts to INFO `signal_processed` (or equivalent) + test.
Acceptance:
- New e2e test(s) fail without C01/C02/C01b fix (verify by reverting logically — run with `PYTHONPATH` shadowing or stash, note result) and pass with it.
- All 3 MINOR fixes have regression tests failing without them.
- Scoped tests §7 pass; ruff clean; mypy = baseline 3 only.
- Docs-reference lock: daemon.py/creator.py shifts may break docs. Run conventions test; if broken, POST NEED (docs outside write-set) + DEFERRED-DEPENDENCY — do not edit docs.

## 4. Ownership
WRITE-SET (may edit ONLY these):
- src/paa/daemon.py
- src/paa/memory/creator.py
- tests/memory/test_e2e_remember_recall.py (new file — allowed to create)
- docs/agent-ops/20261106-memory-remembering-agent/reports/w4-impl/W4-IMPL-07-T-007.md
READ-ONLY: everything else (especially docs/, schema, config, curator.py, context_agents.py).
FORBIDDEN: hotspot schema, pyproject, CI.
Need outside write-set? Don't edit; post NEED; DEFERRED-DEPENDENCY.

## 5. Siblings
None (single implementer by design).

## 6. Skip list and traps
- `HashEmbedder` will NOT do paraphrase (lexical). Don't write a paraphrase test against it and call that e2e; use a fake synonym-aware embedder and say so.
- `recall_facts` stays keyword-only (DECISION T-007) — do not wire vector search there.
- Wave 1/2 already log per-drain counts; don't duplicate logs — extend, don't add parallel log lines.
- Pre-existing: mypy 3, pytest 12, 2 non-blocking doc stales — not yours.

## 7. Commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py tests/memory/ tests/daemon/ -q`
`.venv\Scripts\python.exe -m ruff check src tests`
`.venv\Scripts\python.exe -m mypy src` (expect baseline 3)
`.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` (watch your own shifts)
New tests must fail without the fix; note reversal evidence.

## 8. Deliverables
Report: docs/.../reports/w4-impl/W4-IMPL-07-T-007.md (ORCH-DIGEST first). Commits: code, then report, trailers Task: T-007 / Agent: W4-IMPL-07 / Wave: 4 / Report: <path>. Final ≤8 lines.
