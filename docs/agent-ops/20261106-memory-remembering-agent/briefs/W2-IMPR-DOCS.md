# Brief — W2-IMPR-DOCS (IMPR) · wave 2 docs-reference fix · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Fix R-W2-DOCS (21 broken `path:line` citations) so `tests/conventions/test_docs_references.py` passes, without touching source.
Role: improver (docs only), parallel with W2-IMPR-01 (code) — disjoint write-sets.

## 2. Required reading
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (docs-references rule: read, not re-number)
3. docs/agent-ops/20261106-memory-remembering-agent/gates/gate-w2-1.md (exact broken list)
4. COMMS.md (read at start)

## 3. Task
Run: `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` to get current list (expected: 1 past-end `docs/data/data-lifecycle.md:126 'README.md:351'` (README has 345 lines) + 20 blank: 12 × `daemon.py` (T-004 shift), 8 × `README.md` (operator rewrite)). For EACH: read doc context, read target around cited line, find correct current line, verify non-blank and holds what the sentence claims, update. If cited content no longer exists, record explicitly instead of guessing.
Note: W2-IMPL-03 noted api-reference.md:134/:159 as already-stale (non-blank stale — same category as IMPR-02's 2 leftovers). Include those two in your report as FIXED or DEFERRED-with-reason.
Do NOT edit README.md or VISION.md (operator's files).

## 4. Ownership
WRITE-SET (docs only; may edit these only):
- docs/architecture/component-interactions.md
- docs/architecture/overview.md
- docs/architecture/repository-map.md
- docs/backend/overview.md
- docs/backend/request-lifecycle.md
- docs/data/data-lifecycle.md
- docs/development/developer-workflow.md
- docs/flows/workflow-03-crash-recovery.md
- docs/glossary.md
- docs/interfaces/integrations-and-communication.md
- docs/modules/cli.md
- docs/modules/daemon.md
- docs/operations/deployment-and-runtime.md
- docs/runtime/background-jobs-and-events.md
- docs/security/security-model-and-trust-boundaries.md
- docs/interfaces/api-reference.md (for the :134/:159 stales above)
- docs/agent-ops/20261106-memory-remembering-agent/reports/w2-improve/IMPR-DOCS.md
READ-ONLY: everything else. FORBIDDEN: source edits (src/), config, tests (except running).
Need a docs file outside the list? Post NEED; DEFERRED-DEPENDENCY.

## 5. Tests & commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q`
For each fixed citation, cited line must be non-blank and hold the claimed content.

## 6. Deliverables
Report at docs/agent-ops/20261106-memory-remembering-agent/reports/w2-improve/IMPR-DOCS.md (ORCH-DIGEST first; old→new table for all 21 + api-ref 2, with verification).
Commits: docs, then report, trailers Task: T-003/T-004 / Agent: W2-IMPR-DOCS / Wave: 2 / Report: <path>. Final ≤8 lines.
