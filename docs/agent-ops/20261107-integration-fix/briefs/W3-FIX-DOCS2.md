# Brief — W3-FIX-DOCS2 (IMPL) · wave 3 fix-2 · run 20261107-integration-fix

## 1. Role and mission
Re-point the residual docs citations W3-FIX-DOCS left after W3-FIX-01 landed (037a8d6): exactly the 3 broken recovery.py citations + 1 lock-blind stale citation it flagged. Why: the wave gate (lock green) must hold on the final HEAD.
Your role: implementer (coder), docs-only. You edit only your WRITE-SET. Commit only your files.

## 2. Required reading
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` §"A `path:line` in the documentation is a promise"
3. `docs/agent-ops/20261107-integration-fix/reports/w3-review/W3-FIX-DOCS.md` (residual list: comp-interactions:98 recovery.py:140, workflow-03:74 recovery.py:115, ledger.md:129 recovery.py:140; best-effort when landed: RecoveryEngine class 150, RecoveryReport class 125; FINDING memory.md:92 creator.py:1490→1497)
4. `tests/conventions/test_docs_references.py` (lock + exclusions)

## 3. Task and acceptance criteria
Task W3-FIX-DOCS2. Specifically:
- Run the lock; re-point each of the 3 broken citations by reading the citing sentence (expect RecoveryEngine/RecoveryReport def lines at current HEAD — verify, do not trust the report's numbers blindly). Never blind-shift.
- memory.md:92 `creator.py:1490→1497`: read the citing sentence + the line at :1490/:1497 at HEAD. If the sentence claims the drain def, re-point to the def line; if :1490 still holds what the sentence claims, leave it and record that you read it. Record your reading either way.
- Do NOT edit `docs/agent-ops/**`, `docs/plans/**`, `src/**`, `tests/**`. Target gone = UNRESOLVABLE + NEED.
- Acceptance: `pytest tests/conventions/test_docs_references.py -q` all pass; `git diff --stat` docs-only.

## 4. Ownership
WRITE-SET: paths under `docs/` EXCEPT `docs/agent-ops/**` and `docs/plans/**`. FORBIDDEN: `src/**`, `tests/**`.
Branch: `ops/20261107-integration-fix`. Worktree: current directory.

## 5. Parallel siblings
None running (W3-FIX-01 landed). Re-review (REV-W3b/TREV-W3b) runs after you.

## 6. Skip list
Exactly the 4 citations above. No prose rewrites.

## 7. Environment and tests
Scoped only: `pytest tests/conventions/test_docs_references.py -q`.
Commit protocol: docs commit first, then report in a separate docs-only commit. Trailers `Task: W3-FIX-DOCS2`, `Agent: W3-FIX-DOCS2`, `Wave: W3-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w3-review/W3-FIX-DOCS2.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w3-review/W3-FIX-DOCS2.md` (ORCH-DIGEST first; mapping old→new + evidence). COMMS ≤6 appends. Final message ≤8 lines.
