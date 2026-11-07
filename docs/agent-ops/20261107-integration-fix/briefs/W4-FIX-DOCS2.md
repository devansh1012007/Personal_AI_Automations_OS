# Brief — W4-FIX-DOCS2 (IMPL) · wave 4 fix-2 · run 20261107-integration-fix

## 1. Role and mission
Re-point the residual docs citations W4-FIX-DOCS left after W4-FIX-01 landed (8971e83): the lock currently shows ~56 broken citations, all reported as orchestrator.py cites broken by FIX-01's in-flight +1 import + its other edits. Why: the wave gate (lock green) must hold on final HEAD.
Your role: implementer (coder), docs-only. You edit only your WRITE-SET. Commit only your files.

## 2. Required reading
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` §"A `path:line` in the documentation is a promise"
3. `docs/agent-ops/20261107-integration-fix/reports/w4-review/W4-FIX-DOCS.md` (residual reasoning: 50 blanks all orchestrator.py; your target list = the CURRENT lock output — use the RUN list, not this brief's summary; also read W4-FIX-01.md for which files it edited: `orchestrator.py` import +1, `context/allocator.py`, `graph/sqlite_backend.py`, `migrations.py`, `dashboard.py`, `runner.py`)
4. `tests/conventions/test_docs_references.py` (lock + exclusions)

## 3. Task and acceptance criteria
Task W4-FIX-DOCS2. Specifically:
- Run the lock; re-point EVERY currently-broken citation by reading the citing sentence (verify numbers at HEAD, never blind-shift). Expect orchestrator.py cites (+ any sibling file FIX-01 shifted and the RUN list shows).
- Do NOT edit `docs/agent-ops/**`, `docs/plans/**`, `src/**`, `tests/**`. Target gone = UNRESOLVABLE + NEED.
- Acceptance: `pytest tests/conventions/test_docs_references.py -q` all pass; docs-only diff; scratch files deleted.

## 4. Ownership
WRITE-SET: paths under `docs/` EXCEPT `docs/agent-ops/**` and `docs/plans/**`. FORBIDDEN: `src/**`, `tests/**`.
Branch: `ops/20261107-integration-fix`. Worktree: current directory.

## 5. Parallel siblings
None running (W4-FIX-01 landed). Re-review (REV-W4b/TREV-W4b) runs after you.

## 6. Skip list
Exactly the RUN-list citations. No prose rewrites.

## 7. Environment and tests
Scoped only: `pytest tests/conventions/test_docs_references.py -q`.
Commit protocol: docs commit first, then report in a separate docs-only commit. Trailers `Task: W4-FIX-DOCS2`, `Agent: W4-FIX-DOCS2`, `Wave: W4-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w4-review/W4-FIX-DOCS2.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w4-review/W4-FIX-DOCS2.md` (ORCH-DIGEST first; mapping old→new + evidence). COMMS ≤6 appends. Final message ≤8 lines.
