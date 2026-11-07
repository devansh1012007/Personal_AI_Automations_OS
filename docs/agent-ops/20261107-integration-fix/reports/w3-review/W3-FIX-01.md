<!-- ORCH-DIGEST:START -->
agent: W3-FIX-01 | domain: wave 3 fix: recovery CID exemption + run_weekly guard + runner redact + panel/P2 tests | status: DONE
items: R-W3-002 phantom lineage fixed (recovery skip, exact CID + TIER-only guard); R-W3-003 run_weekly conflict guard fixed (per-domain VaultConflictError skip + log); R-W3-005 7 new runner logs redacted; TREV P1 panel happy-path tests (2 lineages, real ledger) green; cheap P2s: curator tier-failure pin, dry_run=True override pin, invalid-tier pin, R-W3-001 two-fact limit=1 pin
proposed_tasks: 0 (this is the fix implementation)
top_risks: (1) TUI/API open_tasks still counts sweeper CID — replay.py outside write-set, recorded as FINDING (requeue stopped, count not); (2) 4 pre-existing unredacted runner logs untouched per scope — FINDING count recorded; (3) R-W3-004/006/007/008/009/010 DO LATER — seen, untouched
cross_domain_conflicts: none; W3-FIX-DOCS disjoint except 3 recovery.py citation shifts from my +10-line enum/helper insert, covered by its final lock re-run (their COMMS FINDING line ec0c986)
out_of_plan_discoveries: FINDING-1 open_tasks inflation residual; FINDING-2 4 surviving unredacted runner logs (+1 diagnostics line); FINDING-3 RecoveryOutcome gains SKIPPED_SYSTEM_LINEAGE (additive enum member)
confidence: high (all scoped suites green; ruff 0; mypy total 3 pre-existing; import conventions 21 green)
code_commit: 037a8d6ca3a72fd00b1e3fc8f8606f7ff3dc23bd
last_comms_line_read: 2026-11-07T12:45:00Z W3-FIX-DOCS -> ORCH | FINDING | W3-FIX-DOCS
<!-- ORCH-DIGEST:END -->

# Fix report — W3-FIX-01 (IMPL) — wave 3 fix · run 20261107-integration-fix

## 0. Scope and method
- Brief: docs/agent-ops/20261107-integration-fix/briefs/W3-FIX-01.md. Task W3-FIX-01.
- Required reading done in order: STANDING-ORDERS.md; AGENTS.md; REV-W3.md §§4–5,8; TREV-W3.md §§6–7; W3-IMPL-01/02/03.md; code at HEAD (tier.py CID, recovery.py 243-308/441-474, reflection.py run_weekly/apply_to_playbook, runner.py builders + 7 log lines, test_dashboard.py fakes, tests/ledger layout).
- Branch verified: ops/20261107-integration-fix [VERIFIED git branch --show-current].
- WRITE-SET only: src/paa/ledger/recovery.py, src/paa/improvement/reflection.py, src/paa/tui/runner.py (log lines only), tests/tui/test_runner_panels.py (new), tests/ledger/test_recovery_tier_phantom_p009.py (new), tests/memory/test_tier_sweeper_p009.py (P2 additions), tests/memory/test_dual_vault_p010.py (run_weekly test), tests/coder/test_runtime_tools.py (limit test). No docs/** edits (W3-FIX-DOCS owns docs) except this report + COMMS appends (report is deliverable; COMMS is append-only shared log — do not commit COMMS).
- R-W3-004/006/007/008/009/010: DO LATER — seen, not fixed (see §7).

## 1. Context Report (pre-implementation, per mandate section 1)

**Root cause / entry points.**
- R-W3-002: `TIER_*` events are non-terminal (`_NO_PHASE_CHANGE`, not in `TERMINAL_EVENTS`) on one deterministic lineage `TIER_SWEEPER_CORRELATION_ID = uuid5(9e8c3d62-..., "paa:memory:tier-sweeper")` [VERIFIED src/paa/memory/tier.py:62-65]. A TIER-only head projects to phase `CREATED`, attempts 0 [VERIFIED src/paa/ledger/replay.py:150-168,379-389]. `RecoveryEngine.recover_correlation` treats it as resumable → `RESUMED` + `_requeue` enqueue to `orchestrator:core` on every boot [VERIFIED src/paa/ledger/recovery.py:267-308,441-474]. Dormant under `dry_run=True` (zero events), permanent once the first live tick fires. Per-fact CIDs would be strictly worse (one phantom per fact) — rejected.
- R-W3-003: `run_weekly` calls `apply_to_playbook` unguarded in the loop [VERIFIED src/paa/improvement/reflection.py:178-182]. `apply_to_playbook` delegates to `WorldModel.update_belief_state` which raises `VaultConflictError` on an in-block human edit (naming path, `ValueError` subclass) [VERIFIED src/paa/memory/world_model.py:129-143]. One edited playbook aborts the whole weekly pass and discards the accumulated report.
- R-W3-005: 7 new `runner.py` log lines use unredacted `error=str(exc)` [VERIFIED grep 11 hits: lines 71,92,174,198,225,258,262,266,270,378,386; new = 174,198,225,258,262,266,270; pre-existing = 71,92,378,386]. Coder fallback correctly uses `error=redact(str(exc))` [VERIFIED src/paa/coder/runtime_tools.py:53,883].
- TREV P1: `decision_tracker`, `autonomy_state` (awaiting count), `agent_monitor` (recent_roles) have no positive-path test — builders returning empty forever pass (mutation survivor) [VERIFIED tests/tui/test_dashboard.py fake ledger returns [] heads].

**Affected files / consumers.**
- `recovery.py`: `recover_correlation` exemption; `boot_sweep` scan counts; `/health` `open_tasks` + TUI rows still list the sweeper CID (residual — `replay.py`/`open_correlations` outside write-set; see FINDING).
- `reflection.py`: `run_weekly` loop; `apply_to_playbook` still raises (fence unchanged).
- `runner.py`: 7 log lines → `redact(str(exc))`; `build_panel_fields` consumers: TUI `Snapshot` + API `/health` (both call it).
- Tests: 2 new files + 3 additive test files; no existing tests edited except additive appends (dual-vault, tier sweeper, runtime_tools limit).

**Invariants preserved.**
- Ledger-before-side-effect; TIER fold totality (`apply_tier_event` unchanged); recovery idempotency (exemption appends nothing, so repeat sweeps stay no-op); `str,enum.Enum` (new `RecoveryOutcome` member, if added, follows same shape); no `TYPE_CHECKING` additions (conventions lock); `redact` via `models.base` (one secret-hygiene implementation); `line-length = 100`; ruff baseline 0, mypy total 3 (pre-existing `sandbox/subprocess_backend.py:548`).
- Exact-match exemption: one deterministic CID, never a prefix/pattern — a REAL task with any other CID is never exempted.

**Risks / edge cases.**
- Cycle: `recovery.py` must not import `memory.tier` at module scope if that cycles; `models.base` import in `runner.py` must be cycle-safe. Verify with fresh-import + conventions lock.
- `VaultConflictError` import in `reflection.py` must not create a cycle (`world_model` already imported there — safe).
- `build_panel_fields` requires real ledger: `project_lineage` needs `LedgerStore.read_correlation`; fake string heads work via `_correlation_id_of`, but happy-path test uses real `LedgerStore` + real events to prove end-to-end (not a mock of the unit under test).
- Invalid stored tier `tier='TX'`: schema `CHECK (tier IN ('T0','T1','T2','T3','T4'))` [VERIFIED src/paa/storage/relational/schema_sqlite.sql:288-289] makes such a row unreachable via SQL — the P2 must pin the truth (`_due_move`/`TierPolicy.resolve` behaviour) without pretending a `TX` row can be inserted.

**Expected behavior.**
- TIER-only head → recovery: no requeue, no `RESUMED`, no sweep audit event; outcome is an explicit skipped value. Real task heads still `RESUMED`+requeue. Existing recovery tests stay green untouched.
- `run_weekly` with in-block-edited `playbooks.md` → completes (no raise), conflict logged, human edit stands, report returned.
- Runner panel logs carry `redact(str(exc))` for the 7 new lines; 4 pre-existing lines untouched (FINDING count).
- Panels: 2 lineages (one AWAITING_HUMAN with POLICY decision, one EXECUTING) → `decision_tracker` surfaces decision+reason, `autonomy_state` awaiting_ids, `agent_monitor` recent_roles all non-empty and shaped.

**Required tests (fail-before, pass-after).**
- `tests/ledger/test_recovery_tier_phantom_p009.py` (new): TIER-only → no requeue/no RESUMED/no audit event; control: real task still RESUMED+requeue (proves exact-match).
- `tests/memory/test_dual_vault_p010.py` (run_weekly only): in-block edit → completes, human stands, conflict logged (capture_logs); mocked 2-domain continuation: first raises, second lands (per-domain isolation).
- `tests/tui/test_runner_panels.py` (new): 2 lineages via real ledger → all three panels non-empty/correctly shaped; fails if builders return empty forever (by construction: assertions require non-emptiness).
- Cheap P2s: curator exploding tier sweeper pin; per-call `sweep(dry_run=True)` on live sweeper → zero writes; invalid-tier pin (`_due_move` resolution + CHECK unreachability); `limit=1` with 2 facts → count 1 (clamp discriminated).

## 2. Phased Changes Executed (code commit `037a8d6ca3a72fd00b1e3fc8f8606f7ff3dc23bd`)

**Phase 1 — `src/paa/ledger/recovery.py` (R-W3-002).**
Added `RecoveryOutcome.SKIPPED_SYSTEM_LINEAGE` (additive enum member, `str,enum.Enum` per house convention) and `_is_system_tier_lineage(store, cid)` helper. `recover_correlation` projects, handles the terminal path as before, then returns `SKIPPED_SYSTEM_LINEAGE` early for the sweeper lineage — no `_reconcile_workspace`, no `_requeue`, no `_record_sweep`, no `_abandon`, no head-terminal marking. The helper requires **both**: (a) `cid == TIER_SWEEPER_CORRELATION_ID` exactly (one deterministic `uuid5` [VERIFIED `src/paa/memory/tier.py:62-65` → `6e3d7cd4-…`]), and (b) a non-empty event history that is 100% `TIER_DEMOTED`/`TIER_ARCHIVED`. The CID import is function-local inside the helper (comment documents the deferred-import precedent mirroring `_requeue`'s `StreamName` and keeps `ledger.recovery` importable without the tier module). Rejected alternative: a sentinel terminal event on the lineage (would still require the sweeper to emit it each cycle and would make `apply_tier_event` semantics depend on it; the brief recommends the exemption and allows one, not both). Rejected: per-fact CIDs (multiply the phantom). `boot_sweep`'s `scanned` still counts the lineage (it was scanned and skipped); the lineage stays in `open_correlations` — see FINDING-1.

**Phase 2 — `src/paa/improvement/reflection.py` (R-W3-003).**
`run_weekly` now wraps each `apply_to_playbook(rule)` in `try/except VaultConflictError`: on conflict it logs `reflection.playbook_conflict_skipped` (domain, `path=str(exc.path)`, `error=redact(str(exc))` via `models.base`) and continues to the next domain; `report.rules_written` only lists rules that actually landed (append moved after the successful apply; the `vault is None` path still lists the synthesized rule, preserving prior behaviour when no playbook is configured). `apply_to_playbook` itself still raises — the fence is unchanged. Imports: `VaultConflictError` (`world_model`, already a dependency) + `redact` (`models.base`, one secret-hygiene copy). No broad `except Exception`: a non-conflict failure (e.g. `OSError` from the atomic write) still propagates, as it did before.

**Phase 3 — `src/paa/tui/runner.py` (R-W3-005, log lines only).**
Module-level `from paa.models.base import redact` (cycle-verified: `models.base` imports only `core.*` + pydantic/structlog [VERIFIED `src/paa/models/base.py:65-79`]; no back-edge to `tui`; fresh `import paa.tui.runner` succeeds and the conventions subprocess sweep is green). The 7 new lines → `error=redact(str(exc))` (same shape as `coder.vector_search_failed`): `_decision_tracker`, `_autonomy_state`, `_agent_monitor` (1 each), `build_panel_fields` (4). Pre-existing 4 untouched — see FINDING-2. No other runner logic changed.

**Phase 4 — tests (13 new cases; existing tests untouched).**
- `tests/ledger/test_recovery_tier_phantom_p009.py` (new, 5): TIER-only skip; sweep idempotency (ledger count frozen); mixed `TIER_DEMOTED`+`TIER_ARCHIVED` still skipped; real-task control in the same sweep (RESUMED+requeue, requeue payload names the real CID); non-TIER event on the sweeper CID is not exempted (RESUMED). All on tmp `db`+`ledger`, following `test_recovery.py`'s fixture pattern.
- `tests/tui/test_runner_panels.py` (new, 2): 2 open lineages via the **real** `LedgerStore` (real heads + real `POLICY_CLEARED`/`AWAITING_HUMAN_ATTESTATION`/`EXECUTION_STARTED` events with `agent_role` on last events) → `decision_tracker` has both decisions+reasons by CID, `autonomy_state` (`ask`, 1 gate, awaiting = the parked CID), `agent_monitor` (`[planner, worker]`, both roles). Stronger than the brief's "fake heads with real events" (real heads): `project_lineage` needs `read_correlation`, so a fake double cannot drive the happy path without mocking the unit under test. Empty-forever builders fail by construction (assertions require len 2 / gates 1 / roles 2). Second test: POLICY-less lineage yields empty panels by construction, proving empty is data-driven, not a stub.
- `tests/memory/test_dual_vault_p010.py` (+2, run_weekly only): real in-block edit with 2 high-friction domains (deploy via "deploy the service", docker via "docker container restart" — goals chosen because `_domain_from_request`'s keyword tuple matches "deploy" before "docker", so "docker deploy" would collide onto deploy) → `run_weekly` completes, both rules skipped, human edit stands, 2 conflict logs. Failure-injected first domain (deploy raises `VaultConflictError`, docker goes through the real fence and lands) → `rules_written` has the docker rule only, file contains docker not deploy, 1 conflict log. The second test is what "other rules land" means with a single `playbooks.md` (both domains write one file, so against one real conflict both must skip; landing is proven by isolating one domain's failure).
- `tests/memory/test_tier_sweeper_p009.py` (+5 P2s): exploding `_ExplodingSweeper` (with `dry_run` attribute + `sweep(now, dry_run)` signature) → `errors==1`, `DECAY` uncompleted, `PRUNE_EDGES`+`ANALYZE` completed, no raise; per-call `sweep(..., dry_run=True)` on `TierSweeper(dry_run=False)` → `moved==1`, 0 events, row byte-identical, then no-override control moves; `tier='TX'` schema refusal + `_due_move('TX') == ('T2','T3',TIER_DEMOTED)` when due + `None` when fresh (5000h vs 1h against T2's 2160h TTL).
- `tests/coder/test_runtime_tools.py` (+1, `TestRecallVectorFallback`): `_seed` + second Borealis/`swift` fact through the same store/embedder (different entity, avoiding contradiction quarantine), query "rapid" (synonym, keyword-miss → vector tier) with `limit=1` → 1, `limit=50` → 2.

## 3. Review Audit (self-review per phase + final; fresh-context reviewer unavailable — see §6)

- **Phase 1 (recovery).** CORRECTNESS: the exemption runs *after* the terminal check and *before* `AWAITING_HUMAN`/retry/reconcile — a TIER-only lineage (`CREATED`, attempts 0) can never hit the later branches. Idempotency holds because the early return appends nothing (test pins ledger count frozen across two sweeps). SECURITY/INVARIANTS: exact-CID + TIER-only conjunction means no real task is exempted even under id reuse (control + reuse tests). No `TYPE_CHECKING` added; function-local import is classified nowhere (not a block) and the conventions subprocess sweep passes (21 green). MINOR: new enum member is additive to `RecoveryReport.summary()`'s outcomes map — downstream consumers summing counts still work; no exhaustive `match` over outcomes exists to break.
- **Phase 2 (reflection).** CORRECTNESS: only `VaultConflictError` is caught (no swallowing); other exceptions propagate as before. `rules_written` semantics for the success and no-vault paths are byte-identical to before; only the conflict case changes (skipped, not listed) — the pre-existing `test_high_friction_domain_flagged_and_playbook_written` stays green unchanged. Log hygiene: conflict log redacts via the one implementation and carries the named path as `exc.path`, not a parsed string.
- **Phase 3 (runner).** CORRECTNESS: all 7 new sites now redact; grep confirms exactly 7 `redact(` sites and 4 surviving `error=str(exc)` (plus 1 `diagnostic_error` value log) — see FINDING-2. Cycle safety proven by import + conventions sweep, not assumed.
- **Phase 4 (tests).** MUTATION: each new test fails without its fix — recovery (RESUMED+requeue+audit event), run_weekly (raise), panels (empty-forever → len/gates/roles asserts), limit (unclamped → 2 at limit=1), dry-override (missing override → row moves), curator-failure (swallowed differently → errors/completed assertions), invalid-tier (resolve drift → tuple assert). No existing test was deleted, skipped, or weakened; additions are additive only.
- **Final diff review.** 8 write-set files only (+672/−10): 3 src + 3 test additions + 2 new test files. No `docs/**` touched by the code commit (this report + the already-appended COMMS lines are separate). No debugging artifacts, no `noqa`/`type: ignore` introduced, no lockfile/schema/migration touched. `git diff --name-only` pre-commit showed exactly the 8 paths.
- **Independent review:** not run. This session is itself a subagent and cannot nest reviewer subagents (depth limit, same as W3 implementers). Self-review above substitutes; orchestrator should dispatch a fresh-context reviewer before gate. This report does not claim independent-review status.

## 4. Verification Evidence [RAN]

- [RAN `.venv\Scripts\python.exe -m pytest tests/ledger/test_recovery_tier_phantom_p009.py -v` → 5 passed]
- [RAN `pytest tests/tui/test_runner_panels.py -v` → 2 passed]
- [RAN `pytest tests/memory/test_dual_vault_p010.py -v` → 37 passed (35 before + 2 run_weekly)]
- [RAN `pytest tests/memory/test_tier_sweeper_p009.py -v` → 26 passed (21 before + 5 P2 pins)]
- [RAN `pytest tests/coder/test_runtime_tools.py::TestRecallVectorFallback::test_vector_limit_one_truncates_when_two_facts_exist -v` → 1 passed; full file [RAN `pytest tests/coder/test_runtime_tools.py` → 70 passed]]
- [RAN `pytest tests/ledger/` → 98 passed] (recovery suite + replay/store/tier-replay backstops green untouched)
- [RAN `pytest tests/improvement/` → 22 passed] (existing `test_high_friction…` green unchanged)
- [RAN `pytest tests/tui/` → 1 failed, 40 passed]. The 1 failure is the known pre-existing category-B `test_ledger_with_events` (`ValueError: Invalid format string` at `src/paa/tui/dashboard.py:346`, `"%H:%:%S"` — verified identical at `f31fb4f` by REV-W3; `dashboard.py` is forbidden in this task and untouched). The 40 passes include my 2 new panel tests.
- [RAN `pytest tests/conventions/test_import_conventions.py` → 21 passed] (fresh-subprocess sweep: no cycle from the new imports)
- [RAN `.venv\Scripts\python.exe -m ruff check src tests` → All checks passed (0 findings)] (scoped 8-file check also 0 after fixing one E501 in the new dual-vault test signature)
- [RAN `.venv\Scripts\python.exe -m mypy src` → Found 3 errors, all pre-existing `src/paa/sandbox/subprocess_backend.py:548` (`killpg`/`getpgid`/`SIGKILL` Windows host); scoped `mypy` on the 3 changed src files → no issues]. Total stays 3, never grows.
- [RAN import probes: `import paa.tui.runner`, `import paa.ledger.recovery; import paa.improvement.reflection` → ok (no cycle)]
- NOT-RUN (out of scoped-check scope): full `pytest tests/` suite, PostgreSQL/S3 live paths, container/CI. The docs-references lock is owned by W3-FIX-DOCS (its final re-run covers my 3 shifted `recovery.py` citations — see FINDING-3).

## 5. Long-run test (Source / Load / Hostile / Operability / Reversal)

- **Source:** recovery authority is the ledger head projection (`open_correlations` never from the queue — unchanged); the exemption derives from the ledger's own events (all-`TIER_*` check), not from a registry or a flag. `policy_decision`/`policy_reason` in the panel test derive from `POLICY_CLEARED` replay [VERIFIED `replay.py:215-217`], never synthesised.
- **Load (100×):** exemption adds O(1) CID compare + O(events) all-check per lineage *once per sweep*, but only for the sweeper CID (every other lineage returns after the CID compare). It appends nothing, so 100 boots add 0 events (idempotency test pins 2). `run_weekly` conflict path is O(1) per domain (log + skip). Runner redact is O(1) per exception.
- **Hostile input:** a hostile id cannot be exempted (exact `uuid5` match + TIER-only history); a reused sweeper CID with task events is treated as a task (pinned). Panel happy-path values are the same strings the dashboard renders via `Text` (no markup parsing — unchanged). Vault conflict log carries `exc.path` (a filesystem path, not file content) + redacted error string; no file content is logged.
- **Operability (observe/roll back/migrate):** `recovery.skipped_system_lineage` (info) + `reflection.playbook_conflict_skipped` (warning, domain+path+redacted error) are the new observable lines. Rollback: revert the exemption → old resume behaviour returns; revert the guard → old crash returns (no migration, no schema change, no new persisted state). `SKIPPED_SYSTEM_LINEAGE` is reporting-only — no event type added, no `replay.py` change.
- **Reversal:** revert `recovery.py` → all 5 phantom tests fail (RESUMED/requeue/audit/ledger growth). Revert `reflection.py` (drop the `try`) → both run_weekly tests error with `VaultConflictError`. Revert `runner.py` redact → no behavioral test fails (log content), but grep re-shows 7 unredacted new lines; the panel happy-path tests still pass (they pin data, not logs — correctly). Revert any panel builder body to `[]`/`0` → `test_runner_panels` fails (len 2 / gates 1 / roles 2). Remove the limit clamp → limit test fails (2 at limit=1). Hardcode `dry_run=False` in the curator seam → override + seam tests fail.

## 6. Unverified assumptions & remaining risks

- [ASSUMED] Sequential single-process ticks (existing posture): the curated-failure test assumes `run_maintenance`'s per-phase isolation holds for later phases — verified by the test run, not by a concurrency stress test (none attempted).
- [ASSUMED] The two `swift` facts both stay within vector-search top-k at `limit=50` given their identical embedding input — proven by the passing run on this host; vector-backend radius/threshold behaviour on a live Qdrant backend is NOT-RUN (numpy backend only, as in W3).
- [ASSUMED] `recorded_at` (defaults to now) lands inside the 7-day `events_since` window in the run_weekly tests — the default `utc_now` is used by every existing improvement test the same way.
- NOT-RUN: full suite, PG/S3, Docker/CI, Qdrant path, performance measurement (R-W3-006 remains a reasoned latency multiplier, not measured here).
- **Subagent nesting:** mandatory context/reviewer subagents could not run in this environment (this session IS a subagent, depth limit 1). Substituted with full self-investigation at `path:line` (§1) + per-phase + final self-review (§3). Recommend a fresh-context reviewer dispatch before the gate.

### Out-of-scope findings (reported, not fixed — outside the write-set)

- **FINDING-1 (R-W3-002 residual):** `/health` `open_tasks` and the TUI task table still count the sweeper CID as an open task, because `open_correlations` (head projection) + `project` do not know the lineage is not a task — `replay.py` / `store.py` / `api/app.py` are outside this write-set. The phantom *requeue* is stopped (the MAJOR), the count inflation is not (carried as a follow-up; R-W3-006's shared-projection work is the natural home). The recovery test documents it: after the sweep the projection is still `CREATED`/non-terminal and still in `open_correlations`.
- **FINDING-2 (redaction residual):** 4 pre-existing `error=str(exc)` lines remain unredacted in `runner.py` (`_gather_rows` `tui.project_failed` + `tui.rows_failed`, `_latest_projection` `tui.status_failed`, `_latest_events` `tui.ledger_failed`) plus 1 value log (`tui.sandbox_diagnostics_failed` carries `diagnostic_error` verbatim). Out of scope per the brief (7 NEW lines only) and untouched.
- **FINDING-3 (docs lock):** my recovery edits shift 3 `recovery.py` line citations (W3-FIX-DOCS's COMMS line already names them: `component-interactions.md:98`→`recovery.py:140`, `workflow-03.md:74`→`recovery.py:115`, `ledger.md:129`→`recovery.py:140`). Their final lock re-run covers this; I edit no docs.
- **Seen DO LATER (not fixed, per brief):** R-W3-004 (fence bypasses — docs), R-W3-006 (`/health` 3× projections — shared pass), R-W3-007 (`anti_goals` ingestion), R-W3-008 (`vault_sync.enabled` vacuity), R-W3-009/010 NITs. Out-of-scope P2s seen and recorded (not in this task's cheap set): vault record-window recovery (P2 #3), hydration-DB-failure pin (P2 #4), dict-attention test (P2 #6).

## 7. Git state

- Branch: `ops/20261107-integration-fix` [VERIFIED `git branch --show-current` before committing].
- Code+tests commit (explicit paths only, never `add -A`/`-a`): `037a8d6ca3a72fd00b1e3fc8f8606f7ff3dc23bd` — `W3-FIX-01: recovery tier-lineage exemption + run_weekly conflict guard + runner redact + panel/P2 pins`, 8 files, +672/−10, trailers `Task: W3-FIX-01`, `Agent: W3-FIX-01`, `Wave: W3-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w3-review/W3-FIX-01.md`. This report is committed separately (docs-only) listing `037a8d6`.
- Uncommitted at code-commit time and deliberately untouched: `01-work-ledger.md` (modified before I started — shared/ORCH-owned), `COMMS.md` (my 2 START/CLAIM lines — ORCH-committed at wave boundaries per STANDING-ORDERS §8), and this report (committed next). Sibling W3-FIX-DOCS's commits (`f3baf89`, `ec0c986`) landed on the branch while I worked — base for my commit is `ec0c986`, docs-only, no src overlap; verified via `git log`.
- Forbidden commands never used (no stash/reset/checkout/rebase/merge/push/`--no-verify`).


