# Brief — W1-IMPL-03 (IMPLEMENTER) · wave 1b · run 20261109-next-spec

## 1. Role and mission
Mission: Read tools read_file/glob/grep using frozen Workspace jail (N1.0a 7c33723 approved). Why: agent needs eyes before hands. Failure looks like: jail bypass, binary leak, uncapped read, symlink escape, protected path read.
Your role: implementer (coder).

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N1.1 + Wave-1 tests
3. ADR-0045 docs/adr/0045-coder-work-tools.md + src/paa/coder/workspace.py (frozen API, use ws-first assert then is_protected — order matters, see reviewer F3)
4. src/paa/coder/loop.py Tool protocol (name/description/schema()/run()), runtime_tools.py READ_ONLY_TOOLS pattern, coder/__init__.py export pattern
5. Template: report-implementation.md
6. COMMS.md + W1-REV-01 (jail invariant: assert then protect; NIT-4 committed regression test lands in T-N1-1 — your tests ARE that)

## 3. Task and acceptance criteria
Task T-N1-1 (N1.1): src/paa/coder/fs_read_tools.py with read_file, glob, grep. Size-capped (64 KB read, truncated+marked, never silent), UTF-8 round-trip, CRLF preserved on read passthrough, BOM handled, binary refused with clear error message. Every path: assert_inside_workspace then is_protected (denied with reason string), symlink escape denied. Export names frozen: read_file/glob/grep exactly. Tests tests/coder/test_fs_read_tools.py covering: Hypothesis path-jail property (generated .., abs, drive/UNC, mixed sep, symlink never resolve outside ws + explicit symlink-escape), protected (.git/.env*/PAA_HOME) read denied with reason, UTF-8/CRLF/BOM/binary, limits truncation marked, read-only correct in PLAN/ASK/LOCKDOWN (read tools allow in all three). Acceptance: new tests pass, ruff 0, mypy 3 pre-existing, conventions 34 (link lock unaffected).
Commands: pytest tests/coder/test_fs_read_tools.py -q, pytest tests/conventions -q, ruff check src tests script? etc.

## 4. Ownership
WRITE-SET: src/paa/coder/fs_read_tools.py, tests/coder/test_fs_read_tools.py
READ-ONLY: src/paa/coder/workspace.py, docs/adr/0045-*.md, coder/loop.py
FORBIDDEN: fs_write/shell/git tools, workspace.py, coder/agent.py, cli/coder.py, validation/patch.py

## 5. Siblings (parallel wave 1b)
W1-IMPL-04 (N1.2 fs_write, owns fs_write_tools.py + validation/patch.py), W1-IMPL-05 (N1.3 shell, owns shell_tools.py), W1-IMPL-06 (N1.4 git, owns git_tools.py). Disjoint files; all depend on frozen workspace API. No touch of siblings' files.

## 6. Skip list
Do NOT implement write/shell/git. Do NOT edit workspace.py (if jail gap found, ESCALATE with probe).

## 7. Environment
Scoped pytest single-file only.

## 8. Deliverables
Report: reports/w1-impl/W1-IMPL-03-N1-1.md (ORCH-DIGEST). Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Role notes
Implementer: jail-then-protect order, committed regression tests (NIT-4 closes here).
