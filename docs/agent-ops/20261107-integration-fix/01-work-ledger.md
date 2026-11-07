# Work ledger — 20261107-integration-fix   (orchestrator-owned; source of truth for resume)

## Plan coverage
| Plan item | Summary | Disposition (task / skip-reason / deferred-reason) |
|---|---|---|
| P-001 (plan P01) | Redact 2 unredacted `str(exc)` paths | pending |
| P-002 (plan P02) | Derive confidence at query time (kill frozen payload) | pending |
| P-003 (plan P03) | Non-ASCII parity convergence | pending |
| P-004 (plan P04) | Unify touch helper + coder semantic fallback | pending |
| P-005 (plan P05) | Derived `Dockerfile.embed` (pinned `paa/runtime:4.1`) | pending |
| P-006 (plan P06) | 5 TUI panels + API snapshot fields | pending |
| P-007 (plan P07) | Ledger-wired allocator + drain throttle owner | pending |
| P-008 (plan P08) | `TOTAL_MEMORY_COST` advisory gate | pending |
| P-009 (plan P09) | T0–T4 tier layer w/ migration + replay rule | pending |
| P-010 (plan P10) | Dual vault 15-file + governance sync fence | pending |
| P-011 (plan P11) | Pilot `coding` specialist (ADR-0041) | pending |
| P-012 (plan P12) | Edge-count phantom closure (ADR-0043) | pending |
| P-013 (plan P13) | Weekly reflection wiring + `/doctor` healthcheck | pending |
| P-014 (plan P14) | ADRs 0036–0043 + Settings surface | pending |

## Tasks
| Task | Plan items | Domain | Class | Root cause (1 line) | Write-set | Contract-set | Env-set | Depends on | Conflicts (C#) | Wave | Agent | Status | Code commits | Report |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## Waves
### Wave 0 (investigation)
- WAVE_BASE_SHA: 88e648bf88b187c9cb0007f07e4019a079daf2d5
- Agents: INV-01..INV-05 (planned)
- Ownership file: ownership/W0.tsv
- Pre-mortem: plan file:line claims may be stale vs HEAD 88e648b; investigators must re-verify, not copy. Watch P02 gatherer projection fields, P09 schema parity, P10 WorldModel API — all cited but never executed against.
- Gate results: pending
- Status: in progress

## Deep dives
| Task | Agent | Design approved by | Report | Reviewers |
|---|---|---|---|---|

## Deferred / open items
## Out-of-plan discoveries
- A-001: mypy 3 pre-existing errors on Windows host at `src/paa/sandbox/subprocess_backend.py:548` (POSIX `os.killpg`/`os.getpgid`/`signal.SIGKILL` in `else` of `IS_WINDOWS`); CI Linux gate unaffected. Pre-existing, not this run's change.

## Temporary mitigations (user-approved)
## Assumptions → see 00-mission.md
