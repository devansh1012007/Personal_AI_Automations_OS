# Brief — W2-IMPL-03 (IMPLEMENTER) · wave 2b · run 20261109-next-spec

## 1. Role and mission
Mission: Mandate v0 (N2.4) — requester_asserted, policy_version SHA-256, basis explicit/inferred/implicit, doctor counts. Why: tasks must record who requested them under which policy. Failure looks like: implicit counted as coverage (greenwash), asserted identity over-read as authority, 88 test call sites break.
Your role: implementer (coder), mandate.

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N2.4 + D-4/D-5/D-8 (owner fixes) + §C config conventions
3. src/paa/ledger/mandate.py (does NOT exist — you create), src/paa/runtime.py (Runtime.submit signature — add mandate: Mandate|None=None optional), src/paa/agents/orchestrator.py TaskRequest/mandate flow, src/paa/cli/main.py (ledger shows mandate head, doctor counts), src/paa/cli/coder.py (paa agent creates mandate), src/paa/api/app.py + schemas.py (mandate in API)
4. ADR-0046 mandate payload v1 pin (MANDATE_GRANTED/REFUSED contracts), src/paa/config.py (policy.require_explicit_mandate default false, forced true LOCKDOWN), models/base.redact for hygiene
5. Template: report-implementation.md, COMMS.md
6. W2-REV-01 (MINOR-3 wording, principal for correlation ID discipline for coder lineages)

## 3. Task and acceptance criteria
Task T-N2-4: ledger/mandate.py Mandate dataclass (requester_asserted {kind,id} — renamed per D-5 fix, identity_assurance:"asserted", basis explicit/inferred/implicit, policy_version {sha256 canonical effective policy + label}, scope {workspace, permission_mode, budget}). CLI/API/paa agent create explicit mandate; Runtime.submit accepts mandate: Mandate|None=None optional (D-8 backward-compatible); policy.require_explicit_mandate (default false, forced true LOCKDOWN) refuses no-explicit mandate. Policy change mid-run = event (not silent). paa ledger shows mandate at head. paa doctor prints explicit/inferred/implicit counts separately (never collapses to single % per M-4 fix). Tests tests/ledger/test_mandate.py + tests/api/test_mandate_api.py: strict refuses, implicit counted separate, doctor output check, goldens pinned v1. Acceptance: new tests pass, existing Runtime call sites unchanged (test suite still 133 ledger pass), ruff 0, mypy 3, conventions 34.
Commands: pytest tests/ledger/test_mandate.py tests/api/test_mandate_api.py -q, pytest tests/conventions -q, ruff, mypy.

## 4. Ownership
WRITE-SET: src/paa/ledger/mandate.py (new), src/paa/runtime.py, src/paa/agents/orchestrator.py, src/paa/cli/main.py, src/paa/cli/coder.py, src/paa/api/app.py, src/paa/api/schemas.py, tests/ledger/test_mandate.py (new), tests/api/test_mandate_api.py (new)
READ-ONLY: src/paa/core/types.py (mandate event types already added, do not change), src/paa/coder/loop.py, src/paa/coder/journal.py (N2.2 parallel)
FORBIDDEN: coder/journal.py + loop.py wiring (N2.2), ledger/recovery.py (N2.3), validation/*

## 5. Siblings (parallel 2b)
W2-IMPL-02 (journal — owns journal.py/loop.py/agent.py, disjoint), W2-IMPL-04 (canary — owns test_secret_hygiene.py only). Disjoint.

## 6. Skip list
Do NOT implement N2.3 recovery or N2.5 canary test. Do NOT change EventType enum (N2.1 frozen).

## 7. Environment
Scoped pytest ledger+api only.

## 8. Deliverables
Report: reports/w2-impl/W2-IMPL-03-N2-4.md. Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final ≤8 lines.

## 9. Role notes
Immutable basis counting is honest metric; never collapse implicit into coverage.
