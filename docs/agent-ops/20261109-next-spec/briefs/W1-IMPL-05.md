# Brief — W1-IMPL-05 (IMPLEMENTER) · wave 1b · run 20261109-next-spec

## 1. Role and mission
Mission: bash tool through Sandbox.run (N1.3). Why: agent needs to run tests through real sandbox, isolation reported every call. Failure looks like: shell injection, ambient secrets, unbounded output/hang, subprocess network silently unsealed.
Your role: implementer (coder).

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N1.3 + Wave-1 shell tests
3. ADR-0045 (shell definition) + src/paa/coder/workspace.py; src/paa/sandbox/base.py Sandbox.run/SandboxSpec/SandboxResult (timeout_seconds, allow_network, isolation_level) + subprocess_backend behavior (cannot enforce allow_network=False, logs)
4. src/paa/coder/loop.py Tool protocol; src/paa/validation/test_runner.py if exists for test-run pattern
5. Template report-implementation.md, COMMS.md
6. Triage TASK-1 context (Docker test): do not touch test_docker_backend.py unless NEED says so.

## 3. Task and acceptance criteria
Task T-N1-3: src/paa/coder/shell_tools.py with bash. Shell argv: ("pwsh","-NoProfile","-Command",…) Windows, ("bash","-c",…) POSIX (owner). Timeout default 120s cap 600s; combined output capped 64KB, truncation marked; env allowlisted (no ambient secrets — echo-env must NOT leak canary; verify with canary probe); result exposes exit_code, timed_out, isolation_level. Subprocess cannot enforce allow_network=False — expose network_severed=False via preflight or require NAMESPACE+ for allow_network=False calls (honest surfaced flag, no silent claim). Every call jail-then-protect cwd. Tests tests/coder/test_shell_tools.py (slow where needed): timeout kills process tree, cap holds, isolation reported, env allowlist (canary absent from output/ledger), PLAN/ASK/LOCKDOWN bash is mutating (prompt). Acceptance: tests pass, ruff 0, mypy 3.
Commands: pytest tests/coder/test_shell_tools.py -q, pytest tests/conventions -q.

## 4. Ownership
WRITE-SET: src/paa/coder/shell_tools.py, tests/coder/test_shell_tools.py
READ-ONLY: workspace.py, sandbox/base.py (do not edit), ADR-0045
FORBIDDEN: fs_read/write/git tools, validation/*, agent.py, cli*

## 5. Siblings (parallel 1b)
W1-IMPL-03 (read), W1-IMPL-04 (write), W1-IMPL-06 (git). Disjoint.

## 6. Skip list
Do NOT expose free-form shell string to model that bypasses sandbox argv. Do NOT widen sandbox PATH.

## 7. Environment
Scoped pytest single-file only. Do NOT bind fixed ports, Docker.

## 8. Deliverables
Report: reports/w1-impl/W1-IMPL-05-N1-3.md. Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Role notes
Implementer: expose isolation+network honestly, never claim sealed when not.
