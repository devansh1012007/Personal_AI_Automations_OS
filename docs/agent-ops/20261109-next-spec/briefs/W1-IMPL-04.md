# Brief — W1-IMPL-04 (IMPLEMENTER) · wave 1b · run 20261109-next-spec

## 1. Role and mission
Mission: Write tools write_file/edit_file/apply_patch via PatchApplier (N1.2). Why: agent needs hands that are journalled, hash-verified, rollback-capable. Failure looks like: TOCTOU (apply never checks prior hash), phantom newline bug (triage TASK-2), second write path bypassing journal.
Your role: implementer (coder).

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N1.2 + Wave-1 tests + triage TASK-2 (patch phantom newline in UnifiedDiffValidator._plan_file)
3. ADR-0045 + src/paa/coder/workspace.py (jail then protect order)
4. src/paa/validation/patch.py PatchApplier (apply/rollback, _atomic_write, compute_patch_sha256, dry_run, safe_relative_path), UnifiedDiffValidator
5. coder/loop.py Tool protocol
6. Template report-implementation.md, COMMS.md
7. Triage report docs/agent-ops/20261109-next-spec/reports/w0-investigation/N0-5-triage.md TASK-2/TASK-3/TASK-4 (TASK-2 first-priority, TASK-3/4 file as NEED if you touch same files)

## 3. Task and acceptance criteria
Task T-N1-2: src/paa/coder/fs_write_tools.py with write_file, edit_file (old_str exactly once — zero/multi-match fail without write), apply_patch. All via UnifiedDiffValidator.dry_run → PatchApplier.apply(plan); each returns patch SHA matching compute_patch_sha256; journal kept per session (in-memory PatchJournal; ledger persistence is Wave 2). MUST add prior-hash re-check in PatchApplier.apply (INV: apply never compares prior_sha256 today — plans without check inherit TOCTOU). Also fix triage TASK-2 phantom-newline while you are in _plan_file (empty prior file must NOT gain trailing newline) — verifier: test_a_created_file_contains_exactly_what_the_diff_describes passes without xfail (remove marker in same commit). Every path jail-then-protect. Tests tests/coder/test_fs_write_tools.py: encoding/CRLF/BOM/binary refused by read side, edit semantics (0/multi/exact + SHA), limits truncated+marked, protected denied, sleep? acceptance: tests pass, ruff 0, mypy 3.
Commands: pytest tests/coder/test_fs_write_tools.py -q, pytest tests/validation/test_patch.py -q, pytest tests/conventions -q, ruff, mypy.

## 4. Ownership
WRITE-SET: src/paa/coder/fs_write_tools.py, src/paa/validation/patch.py (prior-hash fix + TASK-2 only), tests/coder/test_fs_write_tools.py, tests/validation/test_patch.py (remove xfail for TASK-2 only)
READ-ONLY: workspace.py, ADR-0045
FORBIDDEN: fs_read/shell/git tools, workspace.py, agent.py, validation/engine.py (TASK-3), validation/ast_scanner.py (TASK-4), agent policy lock decision (TASK-5)

## 5. Siblings (parallel 1b)
W1-IMPL-03 (N1.1 read), W1-IMPL-05 (N1.3 shell), W1-IMPL-06 (N1.4 git). Disjoint files.

## 6. Skip list
TASK-3/4/5 are out of write-set — if you notice them, NEED in report, don't fix here. Do NOT weaken PatchApplier safety.

## 7. Environment
Scoped pytest single-file only.

## 8. Deliverables
Report: reports/w1-impl/W1-IMPL-04-N1-2.md. Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Role notes
Implementer: prior-hash check is mandatory — without it ledger claims are false.
