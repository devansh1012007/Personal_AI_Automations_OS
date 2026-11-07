# Brief — W2-FIX-DOCS (IMPL) · wave 2 fix · run 20261107-integration-fix

## 1. Role and mission
Fix REV-W2 R-W2-001 (BLOCKER: docs-reference lock 0→44 broken citations from Wave 2 line shifts). Why: CI enforces the lock; W1 proved the re-point task is the standing wave-gate item for any wave inserting mid-file into heavily-cited files.
Your role: implementer (coder), docs-only. You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` §"A `path:line` in the documentation is a promise" + review gates
3. `docs/agent-ops/20261107-integration-fix/reports/w2-review/REV-W2.md` §4 R-W2-001, §5 attempts 11–12 pattern, §8 (standing wave-gate lesson)
4. `docs/agent-ops/20261107-integration-fix/reports/w1-review/W1-FIX-DOCS.md` (the W1 precedent: citation-by-citation mapping format, 63→0 method, blind-shift forbidden, final HEAD re-run after sibling fix)
5. `tests/conventions/test_docs_references.py` (lock extraction + exclusions: `docs/agent-ops/`, `docs/plans/` excluded — do NOT edit run-folder docs).

## 3. Task and acceptance criteria
Task W2-FIX-DOCS. Specifically (identical method to W1-FIX-DOCS):
- Reproduce the broken list (`pytest tests/conventions/test_docs_references.py -q`, save output; expect ~44, all into `api/app.py`, `cli/main.py`, `coder/runtime_tools.py`, `context_agents.py`, `ledger/replay.py`, `memory/domains.py` — use the RUN list, not this summary).
- Re-point EVERY broken citation by reading the citing sentence + finding the line that holds what it claims (def line, not decorator; range stays a range of real content). Never blind +N. Check for semantically-stale-but-non-blank citations in the same files (lock-blind class — fix any you find while reading, record them).
- Affected citing docs: whatever the lock output lists (expect `docs/README.md`, `docs/architecture/*`, `docs/backend/*`, `docs/data/*`, `docs/flows/*`, `docs/modules/*`, `docs/operations/*`, `docs/runtime/*`, `docs/security/*`, `docs/reviews/*`, `docs/memory-polish/plans/Plan1.md` — the W1 method applies).
- **Sequencing coordination**: W2-FIX-01 concurrently edits `replay.py`, `__init__.py`, `migrations.py`, `Dockerfile.embed`, test files + ci.yml. Your final lock re-run must happen AFTER checking whether FIX-01 has landed (`git log --oneline -3`); if residual failures remain, record the exact residual list for the orchestrator's final gate (do not edit src/tests to fix them).
- Do NOT edit `docs/agent-ops/**`, `docs/plans/**`, `src/**`, `tests/**`. A re-point needing a src change = NEED + residual list.
- Acceptance: `pytest tests/conventions/test_docs_references.py -q` → all pass (or exact residual list with cause); `git diff --stat` docs-only.

## 4. Ownership
WRITE-SET (only these): paths under `docs/` EXCEPT `docs/agent-ops/**` and `docs/plans/**`. FORBIDDEN: `src/**`, `tests/**`, `Dockerfile.embed`, `.github/**`, `Dockerfile`, compose. 
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory.

## 5. Parallel siblings this fix wave
W2-FIX-01 (code/tests/Dockerfile/CI — disjoint: you edit zero of its files). Gate timing per §3.

## 6. Skip list and known traps
- Never blind-shift. Ranges must stay ranges of real content.
- Target content deleted/renamed = UNRESOLVABLE + NEED, not an invented line.
- Keep edits to citation lines only; no prose rewrites.
- Delete any stray scratch file (`w2-lock.txt`/`w2-check.txt`/`docs-lock-now.txt` leftovers) if present in the worktree (untracked, check with `git status`).

## 7. Environment and tests
Scoped only: `pytest tests/conventions/test_docs_references.py -q`. No full suite.
Commit protocol: docs commit first, then report in a separate docs-only commit. Trailers `Task: W2-FIX-DOCS`, `Agent: W2-FIX-DOCS`, `Wave: W2-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w2-review/W2-FIX-DOCS.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w2-review/W2-FIX-DOCS.md` (create at start, ORCH-DIGEST first, append as you work; citation-by-citation old→new mapping + one-line evidence each; residual list if any). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
