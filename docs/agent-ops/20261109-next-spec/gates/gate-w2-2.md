# Gate — wave 2b run 1 + gate-w2-2 — 2026-11-09
- HEAD: 4dbcc68 · Base: 6736ec7 · Scope: T-N2-2 (9a4a519), T-N2-4 (00d2e67), T-N2-5 (3ffb3d2), ORCH NEEDs 4dbcc68

| Step | Result |
|---|---|
| ruff | All checks passed! (0) |
| mypy | 3 errors src/paa/sandbox/subprocess_backend.py:548 pre-existing (149 files, +journal/mandate) |
| conventions | 34 passed (was 33/34 blank-citation; 57 blank + ~40 wrong-nonblank re-points verified) |
| ledger | 161 passed (133 baseline + 28 mandate) |
| journal+canary+mandate | 91 passed (33 journal + 28 mandate + 8 api-mandate + 22 canary, 3 skip pwsh-absent) |
| api minus daemon | 46 passed |
| integration 3 fixed | 3 passed (MANDATE_GRANTED head + LOCKDOWN explicit mandate) |
| tests/api/test_daemon.py | NOT-RUN/host-flake (hangs >3min at HYDRATING on this host; investigator: not mandate-caused, empty git log for daemon files) |

NEEDs closed: (1) config.require_explicit_mandate field + direct read (getattr fallback removed), hash unchanged; (2) docs sweep 4dbcc68; (3) 3 integration tests updated, daemon left as flake.
Secret hygiene: W2-IMPL-04 PARTIAL resolved – 9a4a519 shape-summary preview closes 2 journal leaks.
Next: W2-REV-02/W2-REV-03 independent reviews, then Wave 2c (N2.3 recovery, N2.6 blobbing).
