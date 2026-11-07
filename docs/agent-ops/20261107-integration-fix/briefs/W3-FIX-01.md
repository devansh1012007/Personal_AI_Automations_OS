# Brief — W3-FIX-01 (IMPL) · wave 3 fix · run 20261107-integration-fix

## 1. Role and mission
Fix REV-W3 MAJORs R-W3-002 (sweeper phantom lineage), R-W3-003 (`run_weekly` crash on VaultConflictError), MINOR R-W3-005 (new unredacted runner logs) + TREV-W3 P1 (panel-builder happy-path tests) and cheap P2 pins. Why: R-W3-002 blocks the first live sweep; R-W3-003 turns the fence's surface into a weekly-run crash; redaction must not re-grow.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (zero-baseline; redact via `models.base`; recovery sweep rules)
3. `docs/agent-ops/20261107-integration-fix/reports/w3-review/REV-W3.md` §§4–5 (R-W3-002/003/005 + residual R-W3-004/006/007/008/009/010 — DO LATER list, do not fix those), §8
4. `docs/agent-ops/20261107-integration-fix/reports/w3-review/TREV-W3.md` §§6–7 (P1 happy-path panel builders, P2 list — implement P1 + the cheap P2s named in §3 below; record the rest as seen)
5. `docs/agent-ops/20261107-integration-fix/reports/w3-implementation/W3-IMPL-01.md` (sweeper CID choice + recovery FINDING), `W3-IMPL-02.md` (VaultConflictError contract), `W3-IMPL-03.md` (runner panel builders)
6. Code at HEAD (verify line numbers yourself): `src/paa/memory/tier.py` (deterministic CID — read the exact constant), `src/paa/ledger/recovery.py:243-308,441-474` (`recover_correlation`, `_requeue`), `src/paa/improvement/reflection.py` (`run_weekly`, `apply_to_playbook`, VaultConflictError import), `src/paa/tui/runner.py` (`build_panel_fields`, the 7 unredacted log lines), `tests/tui/test_dashboard.py` (fake ledger patterns), `tests/ledger/` recovery test layout (glob it).

## 3. Task and acceptance criteria
Task W3-FIX-01. Specifically:
- **R-W3-002 (MAJOR, root cause)**: exempt the sweeper's deterministic lineage CID in `recover_correlation` (`recovery.py`) so a TIER-only head is NOT requeued and NOT treated as a resumable task (recommended shape in REV: exempt the CID; a sentinel terminal event is the alternative — if you choose the sentinel, state why in the module docstring + report; do not do both). TUI `/health open_tasks` inflation follows automatically once recovery stops requeueing — verify (assert in test that a TIER-only lineage does not project as an open task, if the projection layer exposes it; if it does and can't be contained here, that is a FINDING — do not touch replay.py in this task). Test: recovery with a TIER-only head → no requeue, no RESUMED (follow the existing recovery-test fixture pattern; tmp ledger only). Existing recovery tests must stay green untouched. Import: the CID constant lives in `tier.py` — import it function-locally or move-expose it (check for cycles: `recovery.py` must not import `memory.tier` at module scope if that cycles; prefer a shared constant location or a local import with a comment — the no-cycle precedent from W1 applies).
- **R-W3-003 (MAJOR)**: per-domain `try/except VaultConflictError` inside `run_weekly`'s loop (log warning with the rule/path, skip that rule, keep accumulating the report). Test: in-block-edited `playbooks.md` → `run_weekly` completes, other rules land, conflict logged (follow the dual-vault delegation test pattern). `apply_to_playbook` itself still raises (do not weaken the fence).
- **R-W3-005 (MINOR, root cause)**: align the 7 NEW `runner.py` log lines to `error=redact(str(exc))` (module-level `redact` import from `models.base` — verify cycle-safe as W1 did; if a cycle forces function-local, record it and follow the file's existing convention — the W1 W3-IMPL-04 note said numpy is core so import-safe; verify, don't assume). Pre-existing unredacted lines elsewhere in runner.py are NOT in scope (record count as FINDING if you find them).
- **TREV P1**: new `tests/tui/test_runner_panels.py` — drive `build_panel_fields` with fake ledger heads carrying real events: 2 lineages (one AWAITING_HUMAN with a POLICY decision, one EXECUTING) → assert `decision_tracker` surfaces policy_decision+reason, `autonomy_state` counts awaiting into `awaiting_ids`, `agent_monitor` collects `recent_roles`. Must fail if builders return empty forever (prove by construction).
- **Cheap P2s** (do these 4; the rest are seen-and-recorded in your report):
  - P2 curator sweep failure: exploding tier sweeper injected in curator → pin the decided behavior (complete-or-fail as implemented; read the curator seam first — if the seam has no handler, the pin documents propagate; your job is an explicit test, not a redesign).
  - P2 per-call `sweep(dry_run=True)` override on a live `TierSweeper` (constructor False, call True) → zero writes.
  - P2 invalid stored tier (`tier='TX'` row) → pin the sweeper's behavior (move-from-resolved or error-counted, as implemented; read `_due_move`/`TierPolicy.resolve` to pin truth, add the test).
  - TREV R-W3-001: two-fact `limit=1` truncation test in `tests/coder/test_runtime_tools.py` (clamp actually discriminated).
- Trace: keep existing `PLAN:integration-fix:P00x` tags; no new tags.
- Acceptance: your test files green + `tests/ledger/` recovery suite green + `tests/improvement/` green + `tests/tui/` green EXCEPT the known strftime failure (quote counts); ruff 0, mypy total 3; docs untouched (W3-FIX-DOCS owns docs).

## 4. Ownership
WRITE-SET (only these): `src/paa/ledger/recovery.py`, `src/paa/improvement/reflection.py`, `src/paa/tui/runner.py` (log lines only), `tests/tui/test_runner_panels.py` (new), `tests/ledger/test_recovery_tier_phantom_p009.py` (new), `tests/memory/test_tier_sweeper_p009.py` (P2 additions only), `tests/memory/test_dual_vault_p010.py` (run_weekly conflict test only), `tests/coder/test_runtime_tools.py` (limit test only). READ-ONLY: everything else. FORBIDDEN: `docs/**`, `src/paa/memory/tier.py` (CID constant read-only — if the exemption needs a change there, post NEED and stop that half), `src/paa/tui/dashboard.py`, `src/paa/tui/snapshot.py`.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: COMMS CLAIM before editing `recovery.py` + `reflection.py` (hotspot/shared files).

## 5. Parallel siblings this fix wave
W3-FIX-DOCS (docs re-point — disjoint: you edit zero docs). Your line shifts are covered by its final lock re-run.

## 6. Skip list and known traps
- R-W3-004 (fence bypass docs), R-W3-006 (/health 3× projections), R-W3-007 (anti_goals ingestion), R-W3-008 (vault_sync vacuity), R-W3-009/010 NITs: DO LATER — do not fix (record as seen).
- Vault record-window recovery test (P2 #3) + hydration-DB-failure pin (P2 #4) + dict-attention test (P2 #6): seen-and-recorded in your report (out of this task's cheap set — the brief names exactly which P2s are in scope).
- Recovery exemption must not exempt REAL tasks: the CID match must be exact (one deterministic CID), never a prefix/pattern.
- Baseline categories A/B/D are not yours.

## 7. Environment and tests
Scoped only: your test files + `tests/ledger/` (recovery) + `tests/improvement/` + `tests/tui/` (known-failure quote) + `tests/coder/test_runtime_tools.py` + ruff/mypy scoped. Tmp fixtures only. No full suite.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: W3-FIX-01`, `Agent: W3-FIX-01`, `Wave: W3-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w3-review/W3-FIX-01.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w3-review/W3-FIX-01.md` (create at start, ORCH-DIGEST first, append as you work; long-run test answers). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
