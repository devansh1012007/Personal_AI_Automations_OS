# Brief — W4-IMPR-DOCS (IMPR) · wave 4 docs-reference fix · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Fix 2 remaining broken citations after Wave 4 creator.py shifts so conventions pass.
Role: improver (docs only).

## 2. Required reading
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (docs-references rule: read, not re-number)
3. docs/agent-ops/20261106-memory-remembering-agent/reports/w4-improve/IMPR-01.md (deferred docs-refs note)
4. COMMS.md (read at start)

## 3. Task
Run `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` — expected 2 broken: `docs/data/persistence-and-storage.md:102 'creator.py:1453'` and `docs/memory-polish/plans/Plan1.md:203 'creator.py:1273-1300'`. For each: read doc context + target around cited line, find correct current line(s), verify non-blank + holds claimed content, update citation. If content no longer exists, record explicitly.

## 4. Ownership
WRITE-SET (may edit ONLY these):
- docs/data/persistence-and-storage.md
- docs/memory-polish/plans/Plan1.md
- docs/agent-ops/20261106-memory-remembering-agent/reports/w4-improve/IMPR-DOCS.md
READ-ONLY: everything else. FORBIDDEN: source edits (src/), config, tests (except running).

## 5. Tests & commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q`

## 6. Deliverables
Report at docs/agent-ops/20261106-memory-remembering-agent/reports/w4-improve/IMPR-DOCS.md (ORCH-DIGEST first; old→new table).
Commits: docs, then report, trailers Task: T-007 / Agent: W4-IMPR-DOCS / Wave: 4 / Report: <path>. Final ≤8 lines.
