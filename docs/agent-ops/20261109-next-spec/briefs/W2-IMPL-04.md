# Brief — W2-IMPL-04 (IMPLEMENTER) · wave 2b · run 20261109-next-spec

## 1. Role and mission
Mission: Secret hygiene canary test (N2.5) — canary never in ledger/log/context/tool output including bash echo. Why: prevention test for credential broker (H2.5) + N2.2 redaction. Failure looks like: env secret leaks through bash echo or args_preview.
Your role: implementer (coder), test-only.

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N2.5 + §B/§H secret hygiene
3. src/paa/models/base.py redact (reuse), src/paa/coder/shell_tools.py env allowlist, src/paa/coder/journal.py (N2.2 may exist by now — check if present; if it does, your test should exercise its args_preview_redacted field; do not edit journal.py)
4. W1-REV-02 canary-in-ledger half was unsupported (no ledger writer in N1.3) — your test closes that with N2.2 present or notes gap if journal absent
5. Template: report-implementation.md, COMMS.md

## 3. Task and acceptance criteria
Task T-N2-5 (N2.5): tests/coder/test_secret_hygiene.py — canary secret in env + in .env (via tmp_path + reset_settings_cache/reset_dotenv_cache, not repo .env) never appears in ledger payloads, logs, context packets, or tool output including bash that echoes its environment (e.g. pwsh/bash `Write-Output $env:CANARY`). Reuse paa.models.base.redact where payloads are built. Paths: also exercise reading protected path denied (redacts denial reason, not secret). Acceptance: canary test passes across every tool; test fails if secret leaks (prove by temporary leak injection, then restore); ruff 0; mypy 3 (tests are disallow_untyped_defs relaxed per pytest.ini? follow repo rule: tests.* exempt from disallow_untyped_defs per AGENTS, so untyped defs okay).
Commands: pytest tests/coder/test_secret_hygiene.py -q, pytest tests/conventions -q, ruff check src tests.

## 4. Ownership
WRITE-SET: tests/coder/test_secret_hygiene.py (new)
READ-ONLY: src/paa/models/base.py, src/paa/coder/journal.py (if exists, test only), src/paa/coder/shell_tools.py
FORBIDDEN: src/** (no code changes), ledger/**, api/*

## 5. Siblings (parallel 2b)
W2-IMPL-02 (journal) + W2-IMPL-03 (mandate) parallel — you own only test file, disjoint. If journal.py exists when you run, test its redaction; if not yet present (race), write test to cover tools + redact contract and note journal coverage deferred to review (do NOT implement journal).

## 6. Skip list
Do NOT implement credential broker (out of scope H2.5). Do NOT write repo .env.

## 7. Environment
Scoped pytest single-file only. No full suite, no docker.

## 8. Deliverables
Report: reports/w2-impl/W2-IMPL-04-N2-5.md. Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final ≤8 lines.

## 9. Role notes
Test-only task; canary is the proof, not the fix (N2.2 owns redaction logic).
