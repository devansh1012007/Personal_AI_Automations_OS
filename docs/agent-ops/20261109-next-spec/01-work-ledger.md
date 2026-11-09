# Work ledger — 20261109-next-spec (orchestrator-owned)

## Plan coverage
Plan items P-N0..G1 from docs/plans/2026-11-09-next-spec-revised.md §E → mapping in Tasks below.

## Tasks
| Task | Plan items | Domain | Class | Root cause (1 line) | Write-set | Contract-set | Depends on | Wave | Agent | Status | Code commits | Report |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| T-N0-0 | N0.0 | hygiene | SURFACE | Deleted VISION + spaces research filename | branch + restore + rename research (orchestrator) | none | none | 0 | ORCH | DONE (restore+branch+baseline) | — | 00-mission.md |
| T-N0-1 | N0.1 | hygiene | DEEP | 12 markdown links + anchor broken, no link lock | docs/VISION.md, docs/README.md, docs/ROADMAP.md, README.md, tests/conventions/ | link-target semantics + ADR allowlist | T-N0-0 | 0 | coder | DONE (d22f0d4 move+lock 657 lines; IMPR 2f26fb8 MAJOR-1/2 fix; REV-01 APPROVE-WITH-CHANGES → IMPR → REV-03 APPROVE) | d22f0d4 + 2f26fb8 | reports/w0-investigation/W0-IMPL-01-N0-1.md, w0-review/W0-REV-01-review.md, W0-IMPR-01-improve.md, W0-REV-03-delta.md |
| T-N0-2 | N0.2 | hygiene | SURFACE | Research note never committed (spaces filename) | docs/research/2026-11-deepseek-super-agent.md | link target for N0.1 | T-N0-0 | 0 | coder | DONE (be51bba byte-identical, REV-02 APPROVE) | be51bba | reports/w0-investigation/W0-IMPL-02-N0-2.md |
| T-N0-3 | N0.3 | hygiene | SURFACE | Composability appendix unreviewed, paper dated 2026 | docs/reviews/, docs/VISION.md banner | review verdict table | N0.1 | 0b | coder | DONE e9e5bbc+b3ce8bc | e9e5bbc |
| T-N0-4 | N0.4 | hygiene | DEEP | Hand-typed README numbers, no generator | scripts/status.py, README, .github/workflows/ci.yml | status block markers + method pin | N0.1 | 0b | coder | DONE c09b0f1 | c09b0f1 |
| T-N0-5 | N0.5 | hygiene | SURFACE | Baseline failures un-triaged | report only (docs/agent-ops/.../reports/) | none | none | 0 | general | DONE (c90d835 triage; REV-02 APPROVE; TASK-1..5 filed → see Deferred) | c90d835 | reports/w0-investigation/N0-5-triage.md |
| T-N1-0 | N1.0 | coder tools | SURFACE | No ADR, no frozen jail contract | docs/adr/0045-*.md, docs/coder-agent.md | workspace API (frozen) | Wave0 | 1a | coder | DONE c9253c8 | c9253c8 |
| T-N1-0a | N1.0a | coder tools | DEEP | 4 tools need shared jail | src/paa/coder/workspace.py | Workspace signature | N1.0 | 1a | coder | DONE 7c33723 | 7c33723 |
| T-N1-1 | N1.1 | coder tools | SURFACE | read_file/glob/grep absent | src/paa/coder/fs_read_tools.py, tests/coder/test_fs_read_tools.py | Tool protocol | N1.0a | 1b | coder | DONE a74f5f0+dbff769 | a74f5f0 |
| T-N1-2 | N1.2 | coder tools | SURFACE | write/edit/apply absent; PatchApplier not hash-verified | src/paa/coder/fs_write_tools.py, validation/patch.py, tests/coder/test_fs_write_tools.py | patch SHA contract | N1.0a | 1b | coder | DONE b97ff73+1c3d0bf | b97ff73 |
| T-N1-3 | N1.3 | coder tools | DEEP | bash absent; subprocess network not enforceable | src/paa/coder/shell_tools.py, tests/coder/test_shell_tools.py | Sandbox.run timeout/isolation | N1.0a | 1b | coder | DONE 3a99108 | 3a99108 |
| T-N1-4 | N1.4 | coder tools | SURFACE | git tools absent; git_add escapes PLAN | src/paa/coder/git_tools.py (git_stage), tests/coder/test_git_tools.py | DEFAULT_MUTATING_TOOLS | N1.0a | 1b | coder | DONE 481a8d3+1c3d0bf | 481a8d3 |
| T-N1-5 | N1.5 | coder tools | DEEP | Registration/permissions + --workspace wrong file | coder/agent.py, runtime_tools.py, cli/coder.py, __init__.py, tests/agents/test_coding_specialist_p011.py | mutating-coverage test | N1.1-N1.4 | 1c | coder | DONE 0d02ea8+ded7cf4+3d24a38 | 0d02ea8 |
| T-N2-1 | N2.1 | ledger | DEEP | 8 EventTypes absent, replay/sweep semantics | core/types.py, ledger/replay.py, coder/loop.py (ToolJournal stub), docs/adr/0046-*.md | ToolJournal signature, _NO_PHASE_CHANGE | Wave1 | 2a | coder | DONE 51a8c61 NEED-1 fb14fac | 51a8c61 |
| T-N2-2 | N2.2 | ledger | DEEP | Coder loop not in ledger; order breaks redaction | coder/journal.py, coder/loop.py, coder/agent.py | PROPOSED persisted intent, idempotency | N1.5,N2.1 | 2b | coder | DONE 9a4a519 preview-shape fix (W2-IMPL-04 leak closed) | 9a4a519 |
| T-N2-4 | N2.4 | ledger | DEEP | No mandate; requester/policy_version absent | ledger/mandate.py, runtime.py, agents/orchestrator.py, cli/main.py, cli/coder.py, api/app.py, api/schemas.py | mandate payload v1, doctor counts | N2.1 | 2b | coder | DONE 00d2e67 ORCH NEEDs 4dbcc68 (config+tests+docs) | 00d2e67 |
| T-N2-5 | N2.5 | security | SURFACE | Secret leak surface untested | tests/coder/test_secret_hygiene.py | redact contract | N1.3,N2.2 | 2b | coder | DONE 3ffb3d2 22 pass/3 skip (leak closed by 9a4a519) | 3ffb3d2 |
| T-N2-3 | N2.3 | recovery | DEEP | Crash classification impossible w/o persisted intent | ledger/recovery.py, coder/session.py, coder/agent.py | recovery classification | N2.2 | 2c | coder | DONE aac0a48 | aac0a48 |
| T-N2-6 | N2.6 | ledger | SURFACE | Ledger bloat, no owner for >8KB | coder/journal.py, ledger/events.py | blob SHA contract | N2.2 | 2c | coder | DONE c2d25e3 | c2d25e3 |
| T-N3-1 | N3.1 | demo | DEEP | S1 never run | examples/fixtures/buggy_calc/, docs/operations/2026-11-09-next-spec/s1-report.md | S1 exit | Wave2 | 3 | coder/operator | DONE (dry-run rung 47/47; real-model NOT RUN — Linux+key) | 2d7f9f6 + 269cbb0 | reports/w3-impl/W3-IMPL-01-N3-1.md |
| T-N3-2 | N3.2 | drill | DEEP | No kill-resume harness | scripts/drill_kill_resume.py, tests/coder/test_recovery_chaos.py | drill oracle | N3.1 | 3 | coder | DONE (Windows 5x + seed14 6/6; Linux 20x NOT-RUN) | 4c999a7 | reports/w3-impl/W3-IMPL-02-N3-2.md |
| T-N3-3 | N3.3 | stretch | SURFACE | Cancellation missing | coder/loop.py, coder/shell_tools.py, sandbox/subprocess_backend.py | cancel token | N3.1 | 3 | coder | DONE (seam + tree-kill arm, 4 MINOR deferred) | eb9a68b + 5caddcf | reports/w3-impl/W3-IMPL-03-N3-3.md |

## Waves
### Wave 0a (N0.0-N0.1/N0.2/N0.5)
- WAVE_BASE_SHA: b8570a6479ea27485c33ac1b2de3ccc6074e82c0
- Agents: T-N0-1 coder (d22f0d4), T-N0-2 coder (be51bba), T-N0-5 general (c90d835), W0-REV-01 (1ccf3f0 APPROVE-WITH-CHANGES 3 MAJOR), W0-REV-02 (b1779e3 APPROVE 0 MAJOR), W0-IMPR-01 (2f26fb8 MAJOR-1/2 fix), W0-REV-03 (8555bb0 delta APPROVE) (ownership file ownership/W0.tsv)
- Pre-mortem: README hotspot collision (N0.1→N0.4 serial), ADR allowlist omission sinks N0.1, research rename mishap. Mitigations: single-writer README, explicit allowlist, N0.0 rename first.
- Gate results: gates/gate-w0-1.md (34 passed after ORCH NEED fix) → gates/gate-w0-2.md (34 passed after IMPR, ruff 0, mypy 3 baseline)
- Review findings → dispositions: R-W0-01 MAJOR-1 FIXED (2f26fb8), MAJOR-2 FIXED (2f26fb8), MAJOR-3 DEFERRED-DEPENDENCY follow-up (../ containment, probe #9), MINOR-1 DEFERRED (nested brackets), MINOR-2 DEFERRED (root-fallback docstring), MINOR-3 DEFERRED (dedup), MINOR-4 FIXED via _existence_defect unification (2f26fb8), NIT-1 DEFERRED (errors=replace), NIT-2 DEFERRED, NIT-3 FIXED (_UNPARSEABLE), NIT-4 DEFERRED. REV-02 MINOR F-1/F-2 noted, NIT N-1/N-2 drafting.
- Review/improve cycles used: 1/2
- Status: CLOSED (Wave 0a DONE)

### Wave 0b (N0.3/N0.4)
- WAVE_BASE_SHA: 8555bb0
- Agents: T-N0-3 coder W0-IMPL-03 (e9e5bbc), T-N0-4 coder W0-IMPL-04 (c09b0f1 + fixups), W0-REV-04 (7a81404 APPROVE-WITH-CHANGES 1 MAJOR), W0-REV-05 (8387ae4 APPROVE 0 MAJOR), W0-IMPR-02 (b3ce8bc +4 refs), W0-REV-06 (d84ce4f delta APPROVE) — ownership file ownership/W0b.tsv
- Gate results: gates/gate-w0-3.md (34 passed before review) → gates/gate-w0-4.md (34 passed after IMPR, ruff 0, mypy 3)
- Review findings → dispositions: REV-04 MAJOR-1 (+4 refs) FIXED by W0-IMPR-02 (b3ce8bc), MINOR-1 (lock-scope misstatement) NOTED for ORCH annotation, NIT-1/NIT-2 folded into b3ce8bc where trivial. REV-05 MINOR-1 (plan N0.4 wording stale vs method) NOTED for plan addendum, NIT-1 (subprocess timeout) DEFERRED.
- Review/improve cycles used: 1/2
- Status: CLOSED (Wave 0b DONE — every Wave 0 task has independent review + gate)

### Wave 1 (overall)
- Status: CLOSED 2026-11-09 — N1.0–N1.5 all DONE with independent reviews + gates. Final gate gate-w1-4. Next: Wave 2a (N2.1 DONE 51a8c61, NEED-1 fb14fac) then Wave 2b.

### Wave 2a (N2.1)
- WAVE_BASE_SHA: 3d24a38
- Agents: W2-IMPL-01 (51a8c61 + dbf420e report). NEED-1 fixed fb14fac (classification). NEED-2 ADR index + NEED-3 docs sweep committed 55ea344. Reviewer: W2-REV-01 6736ec7 APPROVE-WITH-CHANGES 0 BLOCKER/MAJOR, 4 MINOR deferred to N2.2.
- Status: CLOSED 2026-11-09

### Wave 2b (N2.2/N2.4/N2.5)
- WAVE_BASE_SHA: 6736ec7
- Agents: W2-IMPL-02 (9a4a519 journal), W2-IMPL-03 (00d2e67 mandate), W2-IMPL-04 (3ffb3d2 canary PARTIAL→DONE via 9a4a519 shape-summary preview), ORCH 4dbcc68 (config field + 3 mandate-head tests + 57+~40 docs re-points, conventions 34).
- Gate: gate-w2-2 (ruff 0, mypy 3 baseline, conventions 34, ledger 161, journal+canary 91 incl 3 skip, api 46 minus daemon host-flake).
- Status: CODE DONE (W2-REV-02 APPROVE-WITH-CHANGES, W2-REV-03 APPROVE-WITH-CHANGES, 0 BLOCKER/MAJOR); Wave 2c dispatched.

### Wave 2c (N2.3/N2.6)
- WAVE_BASE_SHA: 4c3826e
- Agents: W2-IMPL-05 (aac0a48 recovery), W2-IMPL-06 (c2d25e3 blobbing), W2-REV-04 (MAJOR-1), W2-REV-05 (MAJOR-1), W2-IMPR-05 (357134a), W2-IMPR-06 (837ba17), ORCH fa43a3e+5fcf2a8 docs sweeps.
- Gate: gate-w2-3 (conventions 34, ruff 0, mypy 3, 265 scoped pass per W2-REV-06).
- Status: CLOSED 2026-11-09 (W2-REV-06 APPROVE delta).

### Wave 3 (N3.1/N3.2/N3.3)
- WAVE_BASE_SHA: e640481
- Agents: W3-IMPL-01 (N3.1 S1: fixture + drill dry-run 47/47 + s1-report; real-model NOT RUN — Linux+key), W3-REV-01 APPROVE-WITH-CHANGES; W3-IMPL-02 (N3.2 kill driver: Windows 5x + seed14 done, Linux 20x NOT-RUN), W3-REV-02 APPROVE; W3-IMPL-03 (N3.3 cancel seam) + W3-IMPR-01 (sandbox arm), W3-REV-03 APPROVE. ORCH docs d578a30.
- Gate: gate-w3-1, gate-w3-2.
- Status: CLOSED 2026-11-10 (all three tasks independently reviewed, 0 BLOCKER/MAJOR).
- Remaining: G1 decision note (operator), final review, merge approval (operator).

## Deep dives
(none yet — classification in table; DEEP rows get Spotlight in SUMMARY.md)

## Deferred / open items
- R10 ROADMAP anchor #status → decided #testing (DONE N0.1 d22f0d4)
- R11 arXiv 2608.25512 → keep + UNVERIFIED if unfetchable (N0.3 pending)
- docs/agents-refresh-20261108 + docs/user-guide-20261108 left unmerged (separate branches)
- PG/S3/Docker live suites NOT-RUN locally (CI only)
- Link-lock follow-ups (REV-01): MAJOR-3 ../ containment (`tests/conventions/test_markdown_links.py` probe #9 regression test), MINOR-1 nested brackets, MINOR-2 root-fallback docstring, MINOR-3 dedup collision, NIT-1 errors="replace", NIT-2 IGNORED_DIRECTORIES comment, NIT-4 case-sensitive glob
- Triage TASK-1 (test getuid robustness, preferred monkeypatch per test_docker_backend.py:542-544), TASK-2 patch phantom newline (Wave-1 N1.2 affinity, owner coder), TASK-3 empty-artifact PASS (validation engine owner, pre-Wave-1 hygiene), TASK-4 AST walk RecursionError (validation owner, depth-bounded walk preferred), TASK-5 LOCKDOWN refusal-order OPERATOR decision (AGENTS.md open item) — all from N0-5-triage.md, scheduled as Wave-1 hygiene/integration as noted by REV-02

## Out-of-plan discoveries
(none yet)

## Temporary mitigations
(none — stop rule is a cut, not a mitigation)

## Assumptions → see 00-mission.md
A-001: Windows mypy 3 pre-existing, never grow. A-002: N0.4 method pin is physical src lines + collect-only count.
