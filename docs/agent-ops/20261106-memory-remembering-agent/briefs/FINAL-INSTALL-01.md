# Brief — FINAL-INSTALL-01 (INSTALL) · final install/launch · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Execute plan §K — install + launch smoke for the memory-remembering-agent branch (C01–C07). This is the ONLY install/launch step of the run; plan §K gates it on explicit operator approval, which the operator has now given ("spawn subagent for install/launch").
Role: implementer (coder, ops-only — NO feature code). You run the preflight, install, gate, and smoke, and report evidence. You do NOT fix code defects you find; you report them as BLOCKED with output.
Branch/worktree: work in the current working directory on branch `ops/20261106-memory-remembering-agent` and commit any report to that branch. Do NOT create a new branch or worktree.

## 2. NON-NEGOTIABLE STOP CONDITIONS (check in this exact order)
1. DIRTY TREE: run `git status --porcelain` first. At brief time the tree held 9 modified files (FINAL-IMPR-02's uncommitted work: coder/runtime_tools, daemon, creator, maintenance/e2e tests, operator docs). If the tree is still dirty, OR `docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md` still has status IN-PROGRESS, OR COMMS has no FINAL-IMPR-02 DONE line: STOP IMMEDIATELY, write your report below as BLOCKED ("tree not clean, FINAL-IMPR-02 pending"), commit ONLY the report, final message ≤8 lines. Do NOT stash, clean, or commit anyone else's work. Do NOT install or launch against uncommitted code — plan §K requires a clean tree.
2. If FINAL-IMPR-02 IS done and the tree is clean, proceed to §4.

## 3. Context sources (read in FULL before acting, after the stop check)
1. Plan §K: `C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md` (lines 123-128: install/launch sequence, rollout/rollback, observability) and AGENTS.md (gate order, baseline).
2. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: ruff 0, mypy 3 win32 pre-existing, pytest 12 pre-existing)
3. docs/agent-ops/20261106-memory-remembering-agent/01-work-ledger.md (wave dispositions)
4. docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md (final improvement report — must be DONE, not IN-PROGRESS)
5. docs/operations/deployment-and-runtime.md + docs/modules/daemon.md (actual CLI/API/daemon commands — verify, do not guess)
6. docs/agent-ops/20261106-memory-remembering-agent/COMMS.md (read at start; append your lines)

## 4. Execution (only if the tree is clean)
1. Preflight: `git status --porcelain` (clean), `git log --oneline -3`, `.venv\Scripts\python.exe --version` (expect 3.12.x; do NOT delete/recreate `.venv` — only create it if missing, `uv venv --python 3.12`).
2. Install: `uv pip install -e ".[dev]"` (use `$env:PIP_RETRIES=10; $env:PIP_TIMEOUT=120` per the repo's network-fragility convention; retry once on network failure only, never loop). Record real output.
3. Gate (AGENTS.md order): `.venv\Scripts\python.exe -m ruff check src tests` (expect 0) → `.venv\Scripts\python.exe -m mypy src` (expect baseline 3) → `.venv\Scripts\python.exe -m pytest tests/ -q` (expect baseline 12 pre-existing; any NEW failure = BLOCKED with the test output).
4. Smoke (isolated, never the operator's real home): set `PAA_HOME` to a fresh temp dir. Discover the real commands first (`paa --help`, `paa memory --help`, `paa memory drain --help` if present; API entry per deployment doc). Minimum proof per plan §A/K: (a) write a fact via the CLI/API surface that exists (`remember_fact` path, verify from `--help`/docs), (b) `paa memory drain` (or API equivalent) succeeds and pendings clear (pending-count evidence), (c) paraphrase recall succeeds via the re-scoped proof already in `tests/memory/test_e2e_remember_recall.py` (do not re-prove differently), (d) daemon starts, curates on schedule, and logs `daemon.curation_completed` — start it, capture logs, then STOP the process (no daemon left running). If a command does not exist, STOP and report BLOCKED with `--help` output — do not invent flags.
5. Rollback note: confirm `git status` unchanged by the smoke (all state under the temp `PAA_HOME`); report how to roll back (per-change revert, no migration — plan §K).

## 5. Ownership
WRITE-SET (may edit ONLY these):
- docs/agent-ops/20261106-memory-remembering-agent/reports/final/INSTALL-01.md (create; ORCH-DIGEST first)
- docs/agent-ops/20261106-memory-remembering-agent/COMMS.md (append-only lines only)
- `.venv/` (install artifact only; gitignored, never committed)
READ-ONLY: everything else, ESPECIALLY `src/`, `tests/`, `docs/modules/`, `docs/operations/` (no edits — if smoke finds a defect, report it, do not fix it here). FORBIDDEN: hotspot schema, pyproject, CI.
Untracked `docs/A Disciplined System...md` and `docs/Core Architecture...md`: do NOT track/move/delete (operator decision pending).

## 6. Deliverables
Report at docs/agent-ops/20261106-memory-remembering-agent/reports/final/INSTALL-01.md: status DONE or BLOCKED, exact commands run with real outputs (install, gate, each smoke step), pending-count evidence, curator log excerpt, rollback confirmation. Commit ONLY the report + COMMS lines: explicit `git add -- <paths>`, never `git add -A`. Trailers: Task: FINAL-INSTALL / Agent: FINAL-INSTALL-01 / Wave: FINAL / Report: docs/agent-ops/20261106-memory-remembering-agent/reports/final/INSTALL-01.md. Final message ≤8 lines: STATUS|agent|task|report|SHAs|blockers.
If any command needs a decision (missing flag, missing server, dirty tree), STOP and report BLOCKED — do not guess, do not loop (same error 3x = stop per standing orders).
