# Brief — W1-IMPL-02 (IMPLEMENTER) · wave 1a · run 20261109-next-spec

## 1. Role and mission
Mission: Implement src/paa/coder/workspace.py per frozen ADR-0045 API (N1.0 just merged c9253c8). Why: N1.1-N1.4 need shared jail; security-critical. Failure looks like: path escape via .. / symlink / Windows drive/UNC.
Your role: implementer (coder), code.

## 2. Required reading
1. STANDING-ORDERS (binding)
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N1.0a + §C + Wave-1 tests
3. ADR-0045 just written: docs/adr/0045-coder-work-tools.md (frozen signatures — implement VERBATIM, WorkspaceViolation -> PaaError, get_settings().home for PAA_HOME)
4. src/paa/sandbox/base.py:495-532, src/paa/validation/patch.py:17-322 safe_relative_path, src/paa/config.py Settings.home (:805 get_settings().home not .paa_home)
5. Template: report-implementation.md
6. COMMS.md + W1-IMPL-01 report (frozen API details)

## 3. Task and acceptance criteria
Task T-N1-0a: src/paa/coder/workspace.py implementing ADR-0045 frozen API exactly: `Workspace {root: Path}` frozen dataclass, `resolve_workspace(raw: str) -> Path` (resolves vs cwd, canonicalise, deny on doubt for .., absolute, Windows drive/UNC, mixed separators, symlink), `assert_inside_workspace(ws, target) -> Path` ws-first (raises WorkspaceViolation which extends PaaError per ADR), `is_protected(ws, target) -> str|None` (returns denial reason for .git top-level, .env* any-depth, PAA_HOME via Settings.home). Reuse sandbox helpers / safe_relative_path where possible, extend for Windows. Acceptance: property-test-ready (Hypothesis-compatible pure functions, no I/O beyond stat), type-clean (mypy strict for src/, ruff 0), existing callers unaffected (new file only).
Also capture ADR README index row for 0045 deferred by W1-IMPL-01 (outside its write-set) — either add it now (ADR index) or note DEFERRED-DEPENDENCY in report if outside write-set. Check docs/adr/README.md index row.
Commands: ruff check src tests, mypy src (exactly 3 pre-existing), pytest tests/conventions -q (34).

## 4. Ownership
WRITE-SET: src/paa/coder/workspace.py (new), docs/adr/README.md (one-row index fix only if needed; else note deferred)
READ-ONLY: docs/adr/0045-*.md (frozen, do not edit), src/paa/sandbox/base.py, validation/patch.py (do not edit — reuse)
FORBIDDEN: fs_read/write/shell/git tools, coder/agent.py, cli/coder.py

## 5. Siblings
W1-IMPL-01 (N1.0 ADR c9253c8) just merged. Next N1.1-N1.4 parallel after you.

## 6. Skip list
Do NOT implement tools. Do NOT edit ADR text (if frozen API is wrong, ESCALATE, do not silently diverge).

## 7. Environment
Scoped: ruff, mypy. Do NOT run full pytest, docker.

## 8. Deliverables
Report: reports/w1-impl/W1-IMPL-02-N1-0a.md (ORCH-DIGEST). Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Role notes
Implementer: security-critical, deny on doubt, frozen API verbatim.
