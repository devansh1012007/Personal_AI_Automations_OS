# Brief — W2-IMPL-06 (IMPLEMENTER) · wave 2c · run 20261109-next-spec
Mission: N2.6 8KB blobbing – ledger payloads >8KB offloaded, hash-verified back (SHA contract), replay never reads blob directly without verification.
Read first: STANDING-ORDERS, 00-mission, plan N2.6, src/paa/coder/journal.py (preview/intent sizes), src/paa/ledger/events.py (canonical_json, payload), src/paa/ledger/store.py, ADR-0046 v1 contract.
Write-set: src/paa/coder/journal.py (size check only), src/paa/ledger/events.py, tests/coder/test_blobbing.py (new). Do NOT touch recovery.py/agent.py/session.py (N2.3 parallel).
Acceptance: >8KB payload blobbed + SHA pinned, replay/verify reads back byte-identical, tamper detected, ≤8KB unchanged, ruff 0, mypy 3, conventions 34.
Report: reports/w2-impl/W2-IMPL-06-N2-6.md. Commits code then report to ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS, explicit paths. Final ≤8 lines.