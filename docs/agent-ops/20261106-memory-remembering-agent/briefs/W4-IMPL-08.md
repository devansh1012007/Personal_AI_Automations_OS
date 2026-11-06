# Brief — W4-IMPL-08 (IMPL, RECOVERY) · wave 4 · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Implement T-007 + 3 MINORs, RECOVERY from W4-IMPL-07 (BLOCKED after 32 min investigation-only, no edits). Your investigation is DONE — do NOT re-investigate. Start from the prior agent's verified findings and implement.
Role: implementer (coder). ROOT CAUSE only. (Installing/launching is NOT part of this wave.)

## 2. Prior agent's verified findings (read this file in FULL — it is your primary source)
docs/agent-ops/20261106-memory-remembering-agent/reports/w4-impl/W4-IMPL-07-T-007.md — §2/§3/§4 contain complete verified evidence + exact approach for every item. Do NOT redo source tracing; spot-check only the lines you edit.
Approved approaches (from §4 — implement exactly these unless you find counter-evidence in the code you touch):
- R-W2-D-01: fallback `sweep()` gets `dry_run=dry_run` (one-line, additive; sweep already supports `dry_run=False` default, `decay.py` dry-run branch lists would-delete counts).
- R-W2-D-02: gate `self._curate_preview_done` on `report.completed_fully and report.errors == 0` (curator `completed_fully = stopped_early_at is None`, `curator.py:91-93`; per-phase errors in `report.errors`). Do NOT build per-phase tracking (curator read-only).
- W3 INFO: IGNORED branch emits INFO `memory.signal_processed` with PROCESSED-shaped keys (`outcome=ignored, facts=0, dropped, drop_counts`). Keep single event name for dashboards.
- T-007b pending_count: bounded `COUNT(*) ... WHERE embedding_status='pending'` probe inside `creator.py` (facts.py read-only); surface as `pending_count` on every `memory.embedding_drain` INFO line. Parameterized/constant SQL only.
- Curator timing: pin EXISTING `duration_seconds` already logged (`daemon.curation_completed` INFO) via caplog test. Do NOT edit curator.py to add per-phase durations (read-only; document boundary).
- T-007a e2e: fake synonym/word-order-aware embedder (HashEmbedder lexical-only, brief forbids it for paraphrase), real store semantics + real `MemoryCreator`, assert `_semantic` content matches `object_value` + indexed after close/reopen. Second test: synonym `recall_facts` expected MISS (document boundary) — include only if deterministic with your embedder.
DO NOT re-litigate these choices without new counter-evidence.

## 3. Time-boxed execution (NON-NEGOTIABLE — the prior agent failed this)
- Minute 0-5: POST COMMS `START` line (append-only protocol) before first edit. If no START in 5 min, the wave fails again.
- Minute 5-15: Write the test file `tests/memory/test_e2e_remember_recall.py` FIRST (failing tests), plus failing tests for the 3 MINORs in the same file or a second new test file if needed (still in write-set — see §4).
- Minute ≤45: First code edit + code commit (explicit paths). If no commit by minute 45, STOP and report BLOCKED with the exact blocking error — do not keep tracing.
- No new investigation beyond the lines you edit. If you find a design contradiction, STOP and report ESCALATE with evidence (do not redesign silently).

## 4. Ownership
WRITE-SET (may edit ONLY these):
- src/paa/daemon.py
- src/paa/memory/creator.py
- tests/memory/test_e2e_remember_recall.py (new — create it)
- docs/agent-ops/20261106-memory-remembering-agent/reports/w4-impl/W4-IMPL-07-T-007.md — OVERWRITE the BLOCKED skeleton with your own final report (supersede it; your ORCH-DIGEST replaces it)
READ-ONLY: everything else (especially docs/, schema, config, curator.py, context_agents.py).
FORBIDDEN: hotspot schema, pyproject, CI.
Need outside write-set? Don't edit; post NEED; DEFERRED-DEPENDENCY.

## 5. Siblings
None (single implementer). Prior agent W4-IMPL-07 is STOPPED/BLOCKED and holds no ownership.

## 6. Deliverables
Report: overwrite docs/.../reports/w4-impl/W4-IMPL-07-T-007.md (ORCH-DIGEST first, status DONE/PARTIAL/BLOCKED + SHAs). Commits: code first, then report, trailers Task: T-007 / Agent: W4-IMPL-08 / Wave: 4 / Report: <path>. Final ≤8 lines: STATUS|agent|task|report|SHAs|blockers.

## 7. Commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py tests/memory/ tests/daemon/ -q`
`.venv\Scripts\python.exe -m ruff check src tests`
`.venv\Scripts\python.exe -m mypy src` (expect baseline 3)
`.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` (watch your own shifts; if broken POST NEED)
New tests must fail without the fix; note reversal evidence.
