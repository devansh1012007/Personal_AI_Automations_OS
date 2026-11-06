# Brief — W1-REV-01 (REV) · wave 1 review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Independently audit Wave 1 (T-001: C01+C01b+C02) — try to break it. Your job is to find bugs/regressions/band-aids before the improver fixes them, not to confirm the implementer.
Role: code reviewer (READ-ONLY on source). You write only your report file.

## 2. Required reading (in this order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding, esp. truth/evidence + no band-aids)
2. AGENTS.md (gate, ledger invariant, conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline, pre-existing failures — do NOT re-report them as new)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — items C01, C02, C01b (synthesis), T01–T03
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-code.md
6. COMMS.md (read at start)

## 3. Task
Review diff: `git diff eb6606e24d33f56b1dd7d2d811110b77940b8318..HEAD -- src/paa/runtime.py src/paa/memory/creator.py src/paa/daemon.py src/paa/api/app.py src/paa/cli/main.py tests/` plus code commits c5766c8 (and report commits 3568013/008258a for context only).
Method (strict order): BLIND PASS first (read diff + surrounding code, form findings BEFORE opening implementer report), THEN claims audit (read implementer report: docs/agent-ops/20261106-memory-remembering-agent/reports/w1-impl/W1-IMPL-01-T-001.md — check every claim against code).
Check dimensions: correctness vs acceptance criteria, root cause vs symptom, regression/compat, concurrency/idempotency, error handling, security/abuse, performance/scalability, observability, maintainability/conventions (AGENTS.md), docs impact. Try to break: failing embedder, store unavailable, drain with huge backlog, double-drain, concurrent drain, hostile payload (NUL/oversized statement), empty facts, exception in enqueue after partial upsert, signature backward compat for other `_build_memory_creator` callers.
You may run scoped tests only (single test files/names). If you run a reversal check (tests against pre-fix code), use a temporary worktree, never the shared tree.

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/w1-review/REV-code.md
READ-ONLY: everything else. FORBIDDEN: editing source, fixing findings, full test suite.

## 5. Deliverables
Report with ORCH-DIGEST verdict (APPROVE / APPROVE-WITH-CHANGES / REJECT), findings with severity (BLOCKER/MAJOR/MINOR/NIT), evidence (file:line + command output), suggested root-cause fix. List optimizations with DO NOW / DO LATER / NOT WORTH IT.
Final message (≤8 lines): STATUS | agent | scope | report path | verdict | findings counts.
