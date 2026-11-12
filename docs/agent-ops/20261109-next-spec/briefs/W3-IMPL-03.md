# Brief — W3-IMPL-03 (IMPLEMENTER) · wave 3 · run 20261109-next-spec
Mission: N3.3 stretch — cancellation token honoured by the coder loop's bash tool: Ctrl-C / cancel request during a long command cancels in <5s, process tree gone, cancel recorded as a ledger event (not a silent death).
Read first: STANDING-ORDERS, plan N3.3, src/paa/coder/loop.py (loop + cancel seam if any), src/paa/coder/shell_tools.py (bash run path), src/paa/sandbox/* (process kill/terminate API), ADR-0046 (event shape for cancel).
Write-set: src/paa/coder/loop.py, src/paa/coder/shell_tools.py, tests/coder/test_cancellation.py (new). Do NOT touch journal/events/recovery.
Acceptance: cancel a 10-min sleep-style command returns in <5s; child process tree verified gone (no orphan); ledger shows a cancel event for the lineage (not a FAILED hang); normal completion path unchanged (loop+shell tests green); ruff 0, mypy 3, conventions 34 (docs NEED if line shifts).
Report: reports/w3-impl/W3-IMPL-03-N3-3.md. Commits code then report to ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS, explicit paths. Final ≤8 lines.
