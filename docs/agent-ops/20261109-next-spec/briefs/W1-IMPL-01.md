# Brief — W1-IMPL-01 (IMPLEMENTER) · wave 1a · run 20261109-next-spec

## 1. Role and mission
Mission: Write ADR-0045 (Coder work tools) defining frozen Workspace API before N1.0a implements it, and amend deliberate-separation statement in docs/coder-agent.md. Why: without frozen contract 4 parallel tool implementers diverge on jail (B-2). Failure looks like: divergent jails, security bug on Windows.
Your role: implementer (coder), docs + API definition only.

## 2. Required reading
1. STANDING-ORDERS (binding)
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md §C, N1.0/N1.0a rows, D-2/D-3/D-10, Wave-1 tests
3. src/paa/sandbox/base.py:495-532 (resolve_workspace, assert_inside_workspace), src/paa/validation/patch.py:17-322 (safe_relative_path), src/paa/coder/permissions.py:127-153, coder/loop.py Tool protocol 85-97
4. Template: report-implementation.md
5. COMMS.md from GATE wave-0-final
6. docs/adr/README.md (how ADRs are numbered; check next number 0045 free)

## 3. Task and acceptance criteria
Task T-N1-0 (N1.0): docs/adr/0045-coder-work-tools.md records D-2 (work tools in paa.coder not marketplace), D-3 (edits via PatchApplier, bash via Sandbox.run), path jail, protected paths (.git/, .env*, PAA_HOME), shell definition (pwsh Win / bash POSIX), and defines FROZEN Workspace public API that N1.0a will implement — exact signatures sketch: `Workspace {root: Path}`, `resolve_workspace(raw: str) -> Path`, `assert_inside_workspace(ws, target) -> Path` (raises WorkspaceViolation), `is_protected(ws, target) -> str|None`. Also amends deliberate-separation statement in docs/coder-agent.md (find the "Known limitations" deliberately-no-fs-tools lines, update to point at ADR-0045 + work tools). Acceptance: ADR reviewed into brief (future N1.0a coder can implement without inventing API), coder-agent.md amended line cited, pytest tests/conventions still green (docs links). Python spec only; no code change.
Commands: pytest tests/conventions -q

## 4. Ownership
WRITE-SET: docs/adr/0045-*.md, docs/coder-agent.md
READ-ONLY: src/**, tests/*
FORBIDDEN: src/paa/coder/workspace.py (W1-IMPL-02), scripts/status.py, .github/workflows/ci.yml

## 5. Siblings
No parallel implementers in 1a (serial). W1-IMPL-02 (N1.0a) runs after you merge.

## 6. Skip list
Do NOT implement workspace.py, tools, or registration — definitions only.

## 7. Environment
Scoped docs checks only (pytest tests/conventions -q).

## 8. Deliverables
Report: reports/w1-impl/W1-IMPL-01-N1-0.md (ORCH-DIGEST). Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Role notes
Implementer: freeze API in ADR; N1.0a implements verbatim.
