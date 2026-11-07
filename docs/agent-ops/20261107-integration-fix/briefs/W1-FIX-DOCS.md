# Brief — W1-FIX-DOCS (IMPL) · wave 1 fix · run 20261107-integration-fix

## 1. Role and mission
Fix REV-W1 R-W1-001 (BLOCKER: docs-reference lock 3→63 broken citations), R-W1-003 (ADR-0036 stale creator.py citation), R-W1-004 (ADR-0036 §P01 wording). Why: the wave's mid-file insertions broke 60 citations; CI enforces the lock; SCHEDULING the re-point as part of the wave is the standing lesson (REV §8).
Your role: implementer (coder), docs-only. You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` §"A `path:line` in the documentation is a promise" (the lock cannot see a reference wrong-without-being-broken; re-pointing must be READ, not re-numbered) + review gates
3. `docs/agent-ops/20261107-integration-fix/reports/w1-review/REV-W1.md` §4 R-W1-001/003/004, §5 attempt 11 (base comparison method), §7 DO-NOW item 2, §8 (future waves: append-at-EOF lesson — informational only)
4. `tests/conventions/test_docs_references.py` (how the lock extracts/quotes; its exclusions: `docs/agent-ops/`, `docs/plans/` excluded — do NOT edit run-folder docs for the lock; only the citing docs outside them)
5. Code: every cited source line's NEW content (verify by reading, never blind +N shift).

## 3. Task and acceptance criteria
Task W1-FIX-DOCS. Specifically:
- Re-point EVERY broken citation the lock reports (reproduce first: run `pytest tests/conventions/test_docs_references.py -q` and save the broken list; baseline was 3 — your job is to get to 0, including fixing the 3 pre-existing `docs/plans and audits/w6-w7-w9.md` cites (`cas.py:62`, `api/app.py:42`, `tests/storage/conftest.py:55` — these files were NOT touched this wave; re-point by reading what the sentence claims and finding the line that holds it). **Method per citation (mandatory):** read the citing sentence, read the currently-cited (blank) line, find the actual line holding what the sentence claims (search by content/symbol), update the citation. A blind arithmetic shift (+N) is a FAIL — REV verified many citations land on decorator lines and need the def line. Range citations (`cli/main.py:68-98`) must be re-pointed as ranges whose lines exist and are non-blank.
- Affected citing docs (from the lock output — use the RUN list, not this summary): `docs/README.md`, `docs/architecture/{component-interactions,overview,repository-map}.md`, `docs/backend/{modules-and-services,overview,request-lifecycle}.md`, `docs/data/data-lifecycle.md`, `docs/development/testing-and-quality.md`, `docs/flows/workflow-01-submit-and-run.md`, `docs/flows/workflow-03-crash-recovery.md`, `docs/interfaces/integrations-and-communication.md`, `docs/memory-polish/plans/Plan1.md`, `docs/modules/{agents,cli,context,daemon,memory,models,sandbox,storage}.md`, `docs/operations/{configuration-and-environment,deployment-and-runtime}.md`, `docs/plans and audits/w6-w7-w9.md`, `docs/reviews/documentation-audit.md`, `docs/runtime/background-jobs-and-events.md`, `docs/security/security-model-and-trust-boundaries.md` (final list = the lock's output at execution time).
- Files whose line numbers shifted this wave (cite targets): `src/paa/cli/main.py` (+1 import etc.), `src/paa/config.py` (+137), `src/paa/agents/context_agents.py` (+1 import + hydration block; NOTE: W1-FIX-CODE concurrently edits this file — your gate re-run of the lock must run AFTER both fixes land; if the lock still fails only on `context_agents.py` cites due to FIX-CODE's final lines, that is a coordination note, not a failure — report the residual count for the orchestrator's final gate).
- **R-W1-003**: `docs/adr/0036-allocator-and-attention-loop.md` cites `creator.py:1490` twice where the def `drain_pending_embeddings` is now `:1497` (verify at execution time; the +7 P-002 comment moved it). Re-point both to the def line. Also scan ALL your ADR-0036–0043 citations semantically (read each cited line; the lock only checks non-blank) — any other semantically-stale citation gets re-pointed too, and listed in your report.
- **R-W1-004**: amend ADR-0036 §P01 in place (it is a same-wave ADR, not a supersede): state (a) the 200-char bound applies to RENDERED console text; structlog kwargs (runtime site) are redact-only, unbounded, mirroring `:474`; (b) the truncation marker is INSIDE the 200-char budget. One paragraph; do not change any other ADR-0036 content.
- Do NOT fix R-W1-005 (ANSI), R-W1-008 (inf). Do NOT touch `docs/agent-ops/**` (run folder) or `docs/plans/**` (lock-excluded) except the two ADR-0036 edits above. Do NOT edit any `src/` or `tests/` file.
- Acceptance: `pytest tests/conventions/test_docs_references.py -q` → ALL PASS (or: only citations targeting `context_agents.py` residual due to W1-FIX-CODE's concurrent final lines, with the exact residual list in your report); ruff on changed docs not applicable (md only); `git diff --stat` shows docs-only changes.

## 4. Ownership
WRITE-SET (only these): paths under `docs/` EXCEPT `docs/agent-ops/**` and `docs/plans/**`, plus `docs/adr/0036-allocator-and-attention-loop.md` (already under docs/). Explicitly FORBIDDEN: `src/**`, `tests/**`, `docs/agent-ops/**`, `docs/plans/**`, `pyproject.toml`, CI. If a re-point would require a src change, post NEED and stop that citation (it stays in the residual list).
Branch: `ops/20261107-integration-fix`. Worktree: current directory.

## 5. Parallel siblings this fix wave
W1-FIX-CODE (context_agents.py + semantic tests — disjoint: you never touch src/tests). Coordinate on gate timing: re-run the lock at the end, after checking whether FIX-CODE has committed (read `git log --oneline -3`; if FIX-CODE hasn't landed, note it).

## 6. Skip list and known traps
- Never blind-shift. Several citations point at decorator lines — find the def line. Ranges must stay ranges of real content.
- If a citation's target content NO LONGER EXISTS (deleted/renamed symbol), do not invent a line: record it in your report as UNRESOLVABLE with evidence and post NEED — the orchestrator decides (possible ADR-style note in the citing doc only if you can do it without inventing facts).
- The docs you edit are also read by W2 briefs — keep your edits to citation lines only; no prose rewrites.
- `docs-lock-now.txt` scratch file exists in the worktree (untracked, created by orchestrator/REV during gate) — delete it as part of your task if still present (it is not part of the repo; `git status` will show it).

## 7. Environment and tests
Scoped only: `pytest tests/conventions/test_docs_references.py -q` (the lock). No full suite. No commits to run-folder docs.
Commit protocol: docs commit first, then report in a separate docs-only commit. Trailers `Task: W1-FIX-DOCS`, `Agent: W1-FIX-DOCS`, `Wave: W1-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w1-review/W1-FIX-DOCS.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w1-review/W1-FIX-DOCS.md` (ORCH-DIGEST first; include the citation-by-citation mapping: old → new + one-line evidence each; residual list if any). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
