# Brief — W4-FIX-DOCS (IMPL) · wave 4 fix · run 20261107-integration-fix

## 1. Role and mission
Fix REV-W4 R-W4-001 (BLOCKER: docs-reference lock red — citations past EOF + on blank lines, from Wave 4 shifts) + the 22 lock-blind stale cites W4-IMPL-01 measured. Why: CI enforces the lock; past-EOF citations are the "wrong without being broken" class the lock now CATCHES (within_file test) — re-point by reading.
Your role: implementer (coder), docs-only. You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` §"A `path:line` in the documentation is a promise"
3. `docs/agent-ops/20261107-integration-fix/reports/w4-review/REV-W4.md` §4 R-W4-001 (past-EOF example `dialect.py:588` vs 583 lines, blank cites in docs/adr/0029, facts.py:203, daemon.py:130), §8
4. `docs/agent-ops/20261107-integration-fix/reports/w4-implementation/W4-IMPL-01.md` F6 (22 lock-blind count — read its method: it verified lock-visible 0 from its own files and counted blind companions; reproduce its list from COMMS/report), `reports/w1-review/W1-FIX-DOCS.md` (precedent method)
5. `tests/conventions/test_docs_references.py` (which tests run: not_blank + within_file; exclusions)

## 3. Task and acceptance criteria
Task W4-FIX-DOCS. Same method as W1/W2/W3-FIX-DOCS:
- Reproduce both failing lock tests; re-point EVERY citation they name (past-EOF and blank) by reading the citing sentence (def line, not decorator; never blind +N). The known 22 lock-blind companions: read W4-IMPL-01 F6 for the list; re-point each by reading (a stale-but-non-blank citation is still wrong — the sentence must land on the line holding its claim).
- Sequencing: W4-FIX-01 concurrently edits 6 src files × 7 test files + runner/dashboard/allocator. Your final lock re-run follows a `git log --oneline -3` check for FIX-01 landing; exact residual list if any.
- Do NOT edit `docs/agent-ops/**`, `docs/plans/**`, `src/**`, `tests/**`. Target gone = UNRESOLVABLE + NEED.
- Acceptance: `pytest tests/conventions/test_docs_references.py -q` all pass; docs-only diff; scratch files deleted.

## 4. Ownership
WRITE-SET: paths under `docs/` EXCEPT `docs/agent-ops/**` and `docs/plans/**`. FORBIDDEN: `src/**`, `tests/**`.
Branch: `ops/20261107-integration-fix`. Worktree: current directory.

## 5. Parallel siblings
W4-FIX-01 (code/tests — disjoint).

## 6. Skip list
Exactly lock-visible + F6 lock-blind citations. No prose rewrites; past-EOF targets must be re-pointed to real def lines (reading required).

## 7. Environment and tests
Scoped only: the lock test file (+ the within_file test). No full suite.
Commit protocol: docs commit first, then report in a separate docs-only commit. Trailers `Task: W4-FIX-DOCS`, `Agent: W4-FIX-DOCS`, `Wave: W4-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w4-review/W4-FIX-DOCS.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w4-review/W4-FIX-DOCS.md` (ORCH-DIGEST first; citation-by-citation mapping; residual list if any). COMMS ≤6 appends. Final message ≤8 lines.
