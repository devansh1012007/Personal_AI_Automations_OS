# Brief — W0-INV-01 (INV) · wave 0 · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Verify embedding-sink wiring (plan C01/C02, tests T01–T03) — why are facts stuck `pending` and can `VectorEmbeddingSink` + `pending_embeddings`/`mark_embedded` actually serve semantic recall? Failure looks like: new facts never get vector rows.
Role: investigator (read-only on source).

## 2. Required reading (in this order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (memory/ledger sections)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — items C01, C02, T01–T03
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-investigation.md
6. COMMS.md from start

## 3. Task and acceptance criteria
Verify with evidence: (a) `Runtime._build_memory_creator` signature and why no sink is passed; (b) `VectorEmbeddingSink` construction requirements (vector store, embedder, collection) and what factory/embedder helpers exist (`_try_vector`, embedder factory, `HashEmbedder` fallback, `settings.models` fields); (c) `pending_embeddings`/`mark_embedded` contract and whether `MemoryCreator` can drain them; (d) the 3 drain callers (`daemon._ingestion_loop`, `api.drain_memory`, `cli.drain`) — exact signatures; (e) whether semantic recall (`context_agents._semantic`, `coder recall_facts`) would work once rows exist.
Acceptance: report with `[VERIFIED path:line]` per claim, WRITE-SET (exact files for C01/C02), CONTRACT-SET, test plan that fails before the fix, SURFACE vs DEEP classification.

## 4. Ownership
WRITE-SET: only `docs/agent-ops/20261106-memory-remembering-agent/reports/w0-investigation/INV-creator-runtime.md`
READ-ONLY: everything else. FORBIDDEN: source edits, installs, full test suite, container restarts.

## 5. Siblings
W0-INV-02 (decay/curator/touch/config), W0-INV-03 (recall/extractor/tests). Do not investigate their domains beyond overlap notes.

## 6. Deliverables
Report: `docs/agent-ops/20261106-memory-remembering-agent/reports/w0-investigation/INV-creator-runtime.md` (ORCH-DIGEST first, start early)
Final message (≤8 lines): STATUS | agent | task | report path | blockers
