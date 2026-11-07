# Brief — W5-FIX-DOCS2 (IMPL) · wave 5 fix-2 · run 20261107-integration-fix

## 1. Role and mission
Re-point the residual docs citations W5-FIX-01's edits broke (its report names `sandbox.md:193` + lock-blind shifts; use the RUN list at HEAD `2d62a44`). Why: the wave gate (lock green) must hold on final HEAD.
Your role: implementer (coder), docs-only. You edit only your WRITE-SET. Commit only your files.

## 2. Required reading
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` §"A `path:line` in the documentation is a promise"
3. `docs/agent-ops/20261107-integration-fix/reports/w5-review/W5-FIX-01.md` (its shift report + residual list)
4. `tests/conventions/test_docs_references.py` (lock + exclusions)

## 3. Task and acceptance criteria
Task W5-FIX-DOCS2. Re-point EVERY currently-broken citation at HEAD by reading the citing sentence (def line, never blind-shift); also re-point lock-blind companions in files FIX-01 shifted (`sandbox.md` + any others named in the FIX-01 report). Acceptance: `pytest tests/conventions/test_docs_references.py -q` all pass; docs-only diff; scratch files deleted. Do NOT edit `docs/agent-ops/**`, `docs/plans/**`, `src/**`, `tests/**`. Target gone = UNRESOLVABLE + NEED.

## 4. Ownership
WRITE-SET: paths under `docs/` EXCEPT `docs/agent-ops/**` and `docs/plans/**`. FORBIDDEN: `src/**`, `tests/**`.
Branch: `ops/20261107-integration-fix`. Worktree: current directory.

## 5. Parallel siblings
None running (W5-FIX-01 landed). Re-review (REV-W5b/TREV-W5b) runs after you.

## 6. Skip list
Exactly the RUN-list citations (+ named lock-blind companions). No prose rewrites; scope W5 only.

## 7. Environment and tests
Scoped only: `pytest tests/conventions/test_docs_references.py -q`.
Commit protocol: docs commit first, then report in a separate docs-only commit. Trailers `Task: W5-FIX-DOCS2`, `Agent: W5-FIX-DOCS2`, `Wave: W5-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w5-review/W5-FIX-DOCS2.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w5-review/W5-FIX-DOCS2.md` (ORCH-DIGEST first; mapping old→new + evidence). COMMS ≤6 appends. Final message ≤8 lines.
