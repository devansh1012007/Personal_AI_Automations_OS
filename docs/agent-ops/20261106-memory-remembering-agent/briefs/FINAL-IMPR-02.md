# Brief — FINAL-IMPR-02 (IMPR, RECOVERY) · final implementation · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Complete the final improvement pass. A prior agent (FINAL-IMPR-01) self-reported a doom loop in polling commands and left 9 modified files UNCOMMITTED. Audit that diff, keep what is correct, finish the remaining steps, run the scoped gate, and commit. Do NOT start from scratch.
Role: implementer (coder). ROOT CAUSE only.
Branch/worktree: work in the current working directory on branch `ops/20261106-memory-remembering-agent` and commit to that branch. Do NOT create a new branch or worktree.

## 2. Context sources (read in FULL before editing)
1. docs/agent-ops/20261106-memory-remembering-agent/briefs/FINAL-IMPR-01.md (original findings + write-set — still the authority on WHAT to fix)
2. docs/agent-ops/20261106-memory-remembering-agent/reports/final/FINAL-01-correctness.md (FINAL-01..05)
3. docs/agent-ops/20261106-memory-remembering-agent/reports/final/FINAL-02-operability.md (F-01 MAJOR + F-02..05)
4. docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md (prior agent's skeleton — you OVERWRITE it with your final report)
5. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
6. AGENTS.md (gate, docs conventions)
7. docs/agent-ops/20261106-memory-remembering-agent/COMMS.md (read at start; append your lines)

## 3. Prior agent's claimed progress (UNVERIFIED — audit, do not trust)
The prior agent claimed, in its last status message:
- Code fixes applied: stale coder note/description in `src/paa/coder/runtime_tools.py`; `redact(...)` on the drain error in `src/paa/memory/creator.py`; injected `prune_confidence_floor` forwarded in the daemon fallback in `src/paa/daemon.py`.
- Tests added/strengthened: corrected-note assertions, new `handle()` based touch test in `tests/memory/test_e2e_remember_recall.py`, floor-forwarding tests in `tests/daemon/test_maintenance_loop.py`; reversal checks claimed to have confirmed those tests fail when the corresponding fix is reverted.
- Docs: authoritative curator runbook added in `docs/operations/deployment-and-runtime.md`; `docs/modules/daemon.md` updated; embeddings caveat added to `docs/deployment/docker.md`.
- Remaining steps it named: fix a found unused import in the new tests, finishing `docs/modules/memory.md`/report if needed, running the scoped pytest gate + conventions + ruff/mypy, and committing the write-set files.
Verify each claim against `git diff` (see §4). Keep correct edits; fix or remove incorrect ones. Do not re-apply the same edits blindly.

## 4. First actions (time-boxed, anti-doom-loop — NON-NEGOTIABLE)
- Minute 0-5: Append a COMMS `START` line for FINAL-IMPR-02 in the append-only format used in COMMS.md, and run `git diff --stat` + `git diff` on the 9 modified files. This diff is your working set — read it in full.
- NEVER write a polling/retry loop (no repeated greps, no sleep+recheck, no re-emitting the same command). Run each verification command ONCE; if it fails, read the output, fix the cause, run it once more. If the same error repeats 3 times, STOP and report BLOCKED with the exact output.
- No new investigation beyond the lines in the diff. If you find a design contradiction, STOP and report ESCALATE with evidence (do not redesign silently).
- Minute ≤45: First code/test commit (explicit paths, see §6). If no commit by minute 45, STOP and report BLOCKED with the exact blocking error.

## 5. Ownership
WRITE-SET (may edit ONLY these):
- src/paa/coder/runtime_tools.py
- src/paa/daemon.py
- src/paa/memory/creator.py
- src/paa/memory/decay.py
- tests/memory/test_e2e_remember_recall.py
- tests/daemon/test_maintenance_loop.py
- docs/modules/daemon.md
- docs/modules/memory.md
- docs/operations/deployment-and-runtime.md
- docs/deployment/docker.md
- docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md (OVERWRITE the skeleton with your final report)
- docs/agent-ops/20261106-memory-remembering-agent/COMMS.md (append-only lines only)
READ-ONLY: everything else (including the plan file outside the repo, schema, config.py, context_agents.py, docs/README.md). FORBIDDEN: hotspot schema, pyproject, CI.
Untracked `docs/A Disciplined System...md` and `docs/Core Architecture...md` at docs/ root: DO NOT track, move, or delete — operator decision pending. Need something outside write-set? Don't edit; post NEED; DEFERRED-DEPENDENCY.
The prior agent FINAL-IMPR-01 is stood down and holds no ownership.

## 6. Deliverables
1. Complete every finding from the original FINAL-IMPR-01 brief (F-01 MAJOR operator docs/runbook + snapshot/rollback guidance; F-02 redaction; F-03..F-05 and FINAL-01..05) with disposition FIXED / REJECTED-with-evidence / DEFERRED-with-reason, finding-by-finding in the report.
2. Fix the unused import in the new tests (verify with ruff, do not guess).
3. Final report overwriting docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md with ORCH-DIGEST first, finding-by-finding table, code/test changes, commands run with real output, reversal evidence for new tests (fail without fix → pass with fix), and residual risks. Update the digest counts honestly (the skeleton's zeros are wrong).
4. Commits: code/tests first, then docs/report. Use explicit `git add -- <paths>` (never `git add -A`/`git add .`). Trailers: Task: FINAL / Agent: FINAL-IMPR-02 / Wave: FINAL / Report: docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md. Append COMMS DONE line when finished.
5. Final message to ORCH: ≤8 lines, STATUS|agent|task|report|SHAs|blockers. Include the real scoped-gate numbers.

## 7. Commands to verify (scoped only — run once each, read output)
```
.venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py tests/daemon/test_maintenance_loop.py tests/memory/ -q
.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src
```
Baseline to compare against (00-mission.md): ruff 0 findings; mypy 3 pre-existing errors in `sandbox/subprocess_backend.py:548` (win32); pytest 12 pre-existing Windows failures outside this scope. Any failure vs baseline is YOUR regression — fix it before committing.
Docs `path:line` references: conventions must pass; read each citation you add/keep and verify the line actually holds the claimed content (re-numbering alone breaks the lock's intent).
