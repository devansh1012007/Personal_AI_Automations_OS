# Brief — W3-FIX-DOCS (IMPL) · wave 3 fix · run 20261107-integration-fix

## 1. Role and mission
Fix REV-W3 R-W3-001 (BLOCKER: docs-reference lock 0→11 broken citations from Wave 3 line shifts — runtime +12, curator +20, world_model +254). Why: CI enforces the lock; the re-point task is the standing wave-gate item.
Your role: implementer (coder), docs-only. You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` §"A `path:line` in the documentation is a promise"
3. `docs/agent-ops/20261107-integration-fix/reports/w3-review/REV-W3.md` §4 R-W3-001 (exact citation list: `docs/README.md:53`, `docs/architecture/component-interactions.md:86,232`, `docs/architecture/overview.md:85,96(x2)`, `docs/architecture/repository-map.md:126`, `docs/backend/overview.md:23`, `docs/modules/memory.md:100,116`, `docs/operations/deployment-and-runtime.md:29` — use the RUN list, not this summary), §5 patterns
4. `docs/agent-ops/20261107-integration-fix/reports/w1-review/W1-FIX-DOCS.md` (precedent method + mapping format)
5. `tests/conventions/test_docs_references.py` (lock + exclusions)

## 3. Task and acceptance criteria
Task W3-FIX-DOCS. Same method as W1/W2-FIX-DOCS:
- Reproduce the broken list; re-point EVERY citation by reading the citing sentence (def line not decorator; ranges stay ranges; never blind +N). Also scan ADR-0037/0038/0039 citations for lock-blind staleness (W2 lesson: citations to files edited this wave drift non-blank) — fix any found, list them.
- Sequencing coordination: W3-FIX-01 concurrently edits `recovery.py`, `reflection.py`, `runner.py`, test files. Your final lock re-run must follow a `git log --oneline -3` check for FIX-01 landing; residual failures (if any) = exact list in your report for the orchestrator's final gate (do not edit src/tests).
- Do NOT edit `docs/agent-ops/**`, `docs/plans/**`, `src/**`, `tests/**`. Target content gone = UNRESOLVABLE + NEED.
- Acceptance: `pytest tests/conventions/test_docs_references.py -q` all pass (or exact residual list); `git diff --stat` docs-only. Delete stray scratch files (`w3-check.txt` etc.) if present.

## 4. Ownership
WRITE-SET (only these): paths under `docs/` EXCEPT `docs/agent-ops/**` and `docs/plans/**`. FORBIDDEN: `src/**`, `tests/**`.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory.

## 5. Parallel siblings this fix wave
W3-FIX-01 (code/tests — disjoint).

## 6. Skip list and known traps
- Never blind-shift. Keep edits to citation lines only.
- Baseline lock was GREEN at f31fb4f — every breakage is wave-attributable; your target is 0.

## 7. Environment and tests
Scoped only: `pytest tests/conventions/test_docs_references.py -q`. No full suite.
Commit protocol: docs commit first, then report in a separate docs-only commit. Trailers `Task: W3-FIX-DOCS`, `Agent: W3-FIX-DOCS`, `Wave: W3-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w3-review/W3-FIX-DOCS.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w3-review/W3-FIX-DOCS.md` (create at start, ORCH-DIGEST first; citation-by-citation mapping; residual list if any). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
