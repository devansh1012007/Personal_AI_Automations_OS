<!-- ORCH-DIGEST:START -->
agent: REV-W3 | scope: wave 3 (T-009c sweeper, T-010 vault, T-006 panels, T-004b fallback) | verdict: REJECT
findings: BLOCKER 1 | MAJOR 2 | MINOR 5 | NIT 4
claims_audit: 15 claims checked / 0 unsupported or false
band_aids_found: none
top_3_risks: docs-lock BLOCKER (11 citations, W3 shifts) blocks gate; sweeper lineage becomes a permanent phantom open task at the first live tick (recovery requeues it every boot); run_weekly crashes on any human edit inside playbooks.md (unguarded VaultConflictError)
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WORTH-IT 2
reversal_check: done
confidence: high (full diff read; all scoped suites re-run; fence bypass + recovery paths probed live)
<!-- ORCH-DIGEST:END -->

# Code review — REV-W3

Independent reviewer (did not write the code). Read-only; edited only this report.

## 0. Method

Diff reviewed: `f31fb4f..HEAD` (code commits `cda40f5` tier sweeper, `dd7c3d6` dual vault,
`dd1df98` panels, `d06011a` coder fallback; plus 4 report commits). Baseline `f31fb4f` is the
W2-close commit; the lock was green there.

Order followed: blind pass first (full src diff + surrounding code — `ledger/store.py`,
`ledger/replay.py`, `ledger/recovery.py`, `memory/domains.py`, `memory/decay.py`, `memory/facts.py`,
`runtime.py`, `storage/vector/*`, `config.py`, ADR-0037/0038/0039, W0-INV-01/04), then the four
implementer reports for the claims audit (§2).

Commands run (Windows venv, `.venv\Scripts\python.exe`):
- [RAN ruff check src tests -> All checks passed (0 findings)] — matches gate fact.
- [RAN mypy src -> 3 errors, all `sandbox/subprocess_backend.py:548` baseline] — matches gate fact.
- [RAN pytest 7 W3 files (tier_sweeper, dual_vault, counts, e2e_recall, runtime_tools, health, dashboard)
  --junitxml -> tests=163 failures=1 errors=0 skipped=0] — the 1 failure is
  `tests/tui/test_dashboard.py::test_ledger_with_events` (strftime `dashboard.py:346`), pre-existing
  category B (verified identical at `f31fb4f` via `git show`). So 162 passed.
- [RAN pytest touch_unification + curator + tier_sweeper + tier_vocab + tier_migration + tier_replay +
  improvement -> tests=116 failures=0 errors=0 skipped=1] (skip = PG-gated, expected).
- [RAN pytest tests/api/ -> tests=43 failures=0] ; [RAN tests/memory/test_world_model.py + tests/cli/ ->
  tests=80 failures=1] — the 1 failure is baseline category A
  (`test_continue_resumes_the_most_recent_transcript`), not W3.
- [RAN pytest tests/memory/test_decay.py + tests/ledger/test_replay.py -> tests=75 failures=0] (W3-IMPL-01 backstop exact).
- [RAN pytest tests/conventions/test_import_conventions.py -> 21 passed] (runtime-contract lock green;
  no new `TYPE_CHECKING` classification needed).
- [RAN pytest tests/conventions/test_docs_references.py::test_every_referenced_line_is_not_blank ->
  FAILED, 11 blank-line citations] (see R-W3-001).
- [RAN live fence probes (python, tmp vault) -> block-deletion silently recreates; hash-record deletion
  silently overwrites; plain inside-block edit raises VaultConflictError] (see R-W3-004).

Last COMMS line read at start: `2026-11-07T12:00:00Z W3-IMPL-03 -> ORCH | DONE | T-006-P006` (COMMS.md:93).

## 1. Blind-pass findings (before reading implementer reports)

1. **Sweeper order/union/dry-run correct.** Event-before-UPDATE with `tier = ?` guard
   [VERIFIED `src/paa/memory/tier.py`]; sweeper never DELETEs (union structural); `dry_run` path
   `continue`s before append/UPDATE (zero writes). One open question: the single deterministic
   lineage is a permanently-open head — checked recovery: `recover_correlation` treats it as a
   RESUMED task and `_requeue` enqueues it (recovery.py:243-308,441-468). Flagged as R-W3-002.
2. **Vault conflict mechanism sound, with two bypass shapes.** Content-hash record + adopt-on-missing
   means deleting the record file or deleting the whole block defeats detection (probed live).
   Documented tradeoff, but ADR's absolute "never overwritten" overstates it (R-W3-004).
   `run_weekly` calls `apply_to_playbook` with no guard — a human playbook edit becomes a weekly
   crash (R-W3-003).
3. **Panels: volume controlled, isolation real, one cost concern.** `_PANEL_SCAN_LIMIT=50` bounds
   scans; `build_panel_fields` isolates each panel; `attention_top` is honestly `None`;
   all panel text goes through `Text` (no markup parsing of data). But `/health` now pays up to
   ~150 sequential ledger projections per request (R-W3-006). New runner log lines are unredacted
   `str(exc)` (R-W3-005).
4. **Fallback: degrade real, import safe.** Function-local vector import inside `try`; numpy is a
   core dep (`pyproject.toml:24`), qdrant is the only extra, so the import degrades cleanly.
   Hydration via `get_fact` (derived confidence, not payload snapshot), superseded skipped,
   `touch_recalled` reused (single def re-verified). Hydration outside the `try` is parity with
   keyword tiers (NIT).
5. **Docs lock broken by W3 line shifts** — 11 citations (R-W3-001, BLOCKER by W1/W2 precedent).
6. **Suspected invalid-tier stuck-row bug — NOT a bug.** `TierPolicy.resolve` folds unknown tiers to
   T2, which would make the `tier = ?` UPDATE match 0 rows for a stored `BOGUS` tier. But the
   schema CHECK forbids non-T0..T4 values [VERIFIED `schema_sqlite.sql:288-290`], so the resolved
   tier always equals the stored tier on real rows. Retracted.
7. **Suspected pg/`vault_sync` vacuity — confirmed cosmetic.** Nothing reads
   `settings.vault_sync.enabled` in production (R-W3-008).

## 2. Claims audit

| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| W3-IMPL-01: event-before-move, spy-tested | Yes | [VERIFIED tier.py append→UPDATE; test spy in suite green in my 163-run] |
| W3-IMPL-01: dry-run zero events + zero row mutations | Yes | [VERIFIED `if effective_dry: continue` before append; test green] |
| W3-IMPL-01: union precedence both halves | Yes | [VERIFIED sweeper has no DELETE; both union tests green] |
| W3-IMPL-01: 96 passed (21 new + 75 backstop) | Yes | [RAN decay+replay -> 75/0; tier suite 21 in 163-run] exact |
| W3-IMPL-01: crash-between recovers without re-emit | Yes | Test green; mechanism matches store dedupe [VERIFIED store.py:116-144] |
| W3-IMPL-01: sweeper lineage = recovery phantom (disclosed risk) | Yes | [VERIFIED recovery.py:243-308,441-474] — R-W3-002 |
| W3-IMPL-01: D-DEMOTE stepwise (ARCHIVED only to T4) | Yes (decision sound) | Only stepwise model makes all 5 TTLs live + matches W2 vocab pins |
| W3-IMPL-02: enum 4→15, governance closed set, conflict named-path, reflection single-writer | Yes | [VERIFIED world_model.py; grep `_write_managed_block` src/ -> 0; 35 tests green in my 163-run] |
| W3-IMPL-02: 35 tests + improvement/world/cli suites green | Yes | [RAN improvement (116-run, 0 fail); world_model+cli (80-run, 1 baseline-A fail in test_cli)] |
| W3-IMPL-02: FINDING-2 anti_goals marker ingestion (pinned) | Yes | Real residual — R-W3-007 |
| W3-IMPL-02: sync_enabled gate | Partially | Param works; `vault_sync.enabled` still read by nothing — R-W3-008 |
| W3-IMPL-03: 10 new tests green; tui+api 81/82 (1 strftime) | Yes | [RAN api 43/0; dashboard 12/13; counts 3 green] matches |
| W3-IMPL-03: `attention_top` always None; per-panel isolation | Yes | [VERIFIED runner.py fields init + try/except per panel; tests green] |
| W3-IMPL-04: keyword-first, degrade-never-raise, derived confidence, superseded skipped | Yes | [VERIFIED runtime_tools.py:785-921; 10 fallback tests green in my 163-run] |
| W3-IMPL-04: touch still single-def; import-safe without extra | Yes | [RAN test_touch_unification green; numpy core (pyproject:24); function-local import + try] |
| W3-IMPL-04: minimal-import check | Simulated only (honestly disclosed) | My independent check (numpy core + try-wrap) is stronger than their simulated block; Qdrant path unexercised (disclosed) — accepted |

15 claims checked, 0 false. Fail-before proofs (tier file-copy revert, fallback tier removal,
vault worktree collection error) were not re-executed by me; the failure modes are certain from
code inspection (missing module / ImportError of new names / count 0-vs-1), so I do not count them
as unsupported.

## 3. Long-run test (independent answers)

- **Source:** tier moves derive from `tier_updated_at`+`TierPolicy` with `created_at` fallback for
  backfilled NULL stamps; fallback confidence derives from live rows via `get_fact`, never payload
  snapshots; panel counts derive from one GROUP BY. All pinned by tests that fail if the source
  changes (boundary 4h/4h+1s, derived-vs-snapshot, group counts).
- **Load (100x, concurrency, partial failure):** sweeper is keyset-batched (500) with a guarded
  UPDATE — two ticks racing produce one move + one event (dedupe by full ADR-0008 key). Fallback
  bounded by clamped limit ≤50 (+≤50 single-row hydrations, sequential — see §7). Panels bound
  scans at 50/lineage but repeat projections per panel (R-W3-006); 100 sequential `open_correlations`
  scans at health-poll frequency is the weakest load point. Partial failure: per-panel isolation
  (panels) and degrade-[] (fallback) hold; sweeper aborts a tick on first append/parse failure
  (curator counts the phase error and continues — parity with decay).
- **Hostile input:** NUL refused before any vector call (counting-embedder asserts 0 calls);
  fence markers rejected in managed content and duplicate/dangling markers refused; panel hostile
  strings render literally (Text); `governance_path`/`apply_to_playbook` closed sets block
  traversal; query never interpolates into SQL. Residual: marker pasting *outside* the fence is
  preserved by design (and feeds `_load_anti_goals` — R-W3-007).
- **Operability:** `tier.moves`/`tier.sweep_completed`, `coder.vector_search_failed` (redacted),
  `world_model.fence_conflict`/`hashes_unreadable`, `tui.panel_failed` (unredacted — R-W3-005).
  Rollback: sweeper `dry_run=True` (default), fallback = remove tier call, vault =
  `sync_enabled=False` (param only — R-W3-008). New state is ledger-derivable (tier fold == live
  column, T10b test).
- **Reversal:** revert `tier.py` → collection ModuleNotFoundError (all 21 fail); flip `>`→`>=` →
  boundary test fails; remove fallback call → 3 count tests fail; add a second `touch_recalled`
  def → AST lock fails (green now); remove conflict check → inside-edit test fails; revert note
  text → two verbatim-equality tests fail. Every W3 behavior is reversal-pinned except the
  attention_top null (P-07 will pin the fill).

## 4. Findings

### R-W3-001 [BLOCKER] Docs lock broken by 11 W3-shifted citations
- Location: `docs/README.md:53`, `docs/architecture/component-interactions.md:86,232`,
  `docs/architecture/overview.md:85,96(x2)`, `docs/architecture/repository-map.md:126`,
  `docs/backend/overview.md:23`, `docs/modules/memory.md:100,116`,
  `docs/operations/deployment-and-runtime.md:29` — cited lines are blank in
  `src/paa/runtime.py` (260/359/423), `src/paa/memory/curator.py:62`,
  `src/paa/memory/world_model.py:185`.
- Evidence: [RAN docs lock -> 11 entries]; [RAN `git show f31fb4f` -> all 5 target lines were
  non-blank there]. W3 inserted lines (runtime +12, curator +20, world_model +254) and the
  citations kept their numbers.
- Why it matters: the lock was green at `f31fb4f`; the tree's own gate ("do not make it non-zero",
  AGENTS.md) is now red. W1/W2 precedent rated this BLOCKER/REJECT.
- Recommended fix: docs re-point task only (W1/W2-FIX-DOCS precedent); code unchanged. **Gate
  cannot pass until re-pointed.**

### R-W3-002 [MAJOR] Sweeper lineage becomes a permanent open phantom task at the first live tick
- Location: `src/paa/memory/tier.py:56-60` (single deterministic CID) × `src/paa/ledger/recovery.py:243-308,441-474`.
- Evidence: TIER_* events are non-terminal → head stays open; a TIER-only lineage projects to
  phase `CREATED`, attempts 0 → `recover_correlation` returns RESUMED (workspace reconcile is a
  no-op without a checkpoint) and `_requeue` enqueues it to `orchestrator:core` on **every boot**.
  TUI `_gather_rows` will also render it as a phantom task and `/health` `open_tasks` inflates by 1.
- Why it matters: dormant today (`dry_run=True` default → zero events → no head), but the first
  live tick (R3 gate) creates it permanently. Per-fact CIDs (considered by the implementer) would
  be strictly worse (one phantom per fact). This needs a recovery-side decision before go-live.
- Recommended fix (root-cause, recovery.py owner — not this wave's write-set): exempt the sweeper
  CID in `recover_correlation`, or terminate the lineage with a sentinel; add a recovery test with a
  TIER-only head asserting no requeue. **The R3/live-tick gate must not open until this is resolved.**

### R-W3-003 [MAJOR] `run_weekly` dies on a human edit inside `playbooks.md`
- Location: `src/paa/improvement/reflection.py:178-182` (`apply_to_playbook` unguarded in the loop).
- Evidence: delegation now raises `VaultConflictError` on an in-block human edit (test
  `test_delegation_inherits_conflict_semantics` proves the raise); `run_weekly` has no handler, so
  one edited playbook aborts the whole weekly run and discards the accumulated report. Previously
  the duplicate writer silently overwrote, so this is a new failure mode for the sole caller.
- Why it matters: the fence exists to surface human edits — but the surfacing currently crashes
  the learning loop instead of preserving it. `run_weekly` is not yet scheduled (W4/T-013), so the
  fix is one try/except, but it must land with the W4 wiring or the daemon tick inherits a crash.
- Recommended fix: per-domain `try/except VaultConflictError` in `run_weekly` (log, skip, keep the
  report) + a test with an in-block-edited playbook. Schedule with T-013 (W4).

### R-W3-004 [MINOR] Two fence bypasses, both probed live
- Location: `src/paa/memory/world_model.py:265-271` (no-block append branch), `:338-364` (hash
  record degrade).
- Evidence: [RAN probe -> (1) deleting the entire managed block → writer re-creates it, **no
  conflict**; (2) deleting `.paa-managed-hashes.json` after an inside-block human edit → adopt +
  **silent overwrite** of the edit; (3) plain inside-block edit → conflict, as designed].
- Why it matters: ADR-0038's "never overwritten" holds only while the record file and the block
  structure are intact. Both bypasses need vault write access (the human themselves or an
  editor), so this is not an attacker-reachable runtime overwrite — but the guarantee as written
  overstates itself, and a user who deletes the block intending to un-manage the file is silently
  re-managed.
- Recommended fix: document both shapes in ADR-0038 (the code already documents the record
  degrade); optionally treat a deleted-but-recorded block as a conflict. Not blocking.

### R-W3-005 [MINOR] New TUI log lines are unredacted `error=str(exc)`
- Location: `src/paa/tui/runner.py:174,198,225,258,262,266,270` (per-lineage + per-panel).
- Evidence: the coder fallback correctly logs `error=redact(str(exc))` (runtime_tools.py);
  the new runner lines copy this file's pre-existing unredacted pattern. These exceptions today
  carry ledger/DB messages, not payload secrets, and panels render no exception strings (W1
  pre-mortem 8 holds).
- Recommended fix: align to `redact(str(exc))` in the deferred P-001 harden task, with the other
  known siblings.

### R-W3-006 [MINOR] `/health` and the 2s tick pay ~3×50 sequential ledger projections
- Location: `src/paa/tui/runner.py:157-230` (three independent scans) + `src/paa/api/app.py:73-104`.
- Evidence: `decision_tracker`, `autonomy_state`, `agent_monitor` each call
  `open_correlations()` and then `project_lineage`/`read_correlation` per lineage (≤50 each),
  sequentially; `_gather_rows` repeats the same projections a third time in the TUI tick.
  `/health` inherits the full cost per request. Correctness is fine; this is a latency multiplier
  under health-poll frequency and open-task growth.
- Recommended fix (DO LATER): share one projection pass across the three panels. Not blocking.

### R-W3-007 [MINOR] `anti_goals.md` governance will pollute the fence-unaware line reader
- Location: `src/paa/runtime.py:607-621` (`_load_anti_goals`) × governed `anti_goals.md`.
- Evidence: pinned as W3-IMPL-02 FINDING-2 with a test asserting the marker lands in the goals
  list. No live writer for `anti_goals.md` exists yet, so this is dormant; the first governed
  write pollutes policy input with `<!-- paa:managed:... -->` lines.
- Recommended fix: fence-aware `_load_anti_goals` (strip the managed block) before any sync-loop
  write to `anti_goals.md`. Deferred-owner: spec implementer of the sync loop.

### R-W3-008 [MINOR] `vault_sync.enabled=False` currently changes nothing in production
- Location: `src/paa/config.py` (flag) × `src/paa/memory/world_model.py:169-184` (param only).
- Evidence: [RAN grep -> no production caller passes `sync_enabled`; reflection constructs
  `WorldModel` without it, so playbook writes proceed regardless of the flag]. ADR-0038's
  rollback ("`vault_sync.enabled=False`; the files stay") is vacuous until the sync loop lands.
- Recommended fix: say so in ADR-0038 (no sync loop exists yet, so nothing to gate) or wire the
  flag into the reflection construction. Cosmetic; not blocking.

### R-W3-009 [NIT] Tier moves invisible in `CurationReport`
- Location: `src/paa/memory/curator.py:251-253` (return value discarded).
- The moves are logged (`tier.moves`), so they are observable, but the curator summary that
  operators read carries no tier count. Consider two report fields.

### R-W3-010 [NIT] Custom `TierPolicy` with finite T4 TTL crashes `_due_move`
- Location: `src/paa/memory/tier.py` (`_LADDER.index(current)` with `current == "T4"`).
- Unreachable with the canonical `float("inf")` T4 (elapsed > inf is False). Only an injected
  policy with finite T4 reaches it. Document the policy constraint or guard the ladder end.

### R-W3-011 [NIT] Fallback hydration outside the `try`
- Location: `src/paa/coder/runtime_tools.py:917-924` — a DB error in `get_fact` propagates
  instead of degrading. Consistent with the keyword tiers (which also raise on DB failure), so
  this does not violate the ADR's degrade scope; noted for symmetry.

### R-W3-012 [NIT] Sweeper per-row parse failures abort the whole tick
- Location: `src/paa/memory/tier.py` (`_due_move` outside the domain try). Parity with
  `DecaySweeper` (also unguarded `from_iso`), so this is house-consistent. A corrupt timestamp
  kills the tick but the curator counts the phase error and continues. No change needed.

## 5. Attempts to break it

1. **Sweeper held tier (`t2`/NULL/`BOGUS`) stuck by the guarded UPDATE** — reasoned bug, then
   disproved: schema CHECK + NOT NULL make non-canonical stored tiers unreachable. Retracted.
2. **Fence bypass via record deletion / block deletion** — [RAN probe]: both silently overwrite
   (R-W3-004). Plain inside-block edit correctly conflicts.
3. **Fence bypass via marker smuggling in content** — blocked at the boundary (markers in
   `content` raise); duplicate/dangling markers refused. Tests green.
4. **Second touch copy at the fallback site** — [RAN test_touch_unification green; grep shows
   exactly one `def touch_recalled`] — held.
5. **Fallback as hard dependency** — absent store / absent embedder / throwing embedder /
   throwing store / zero-vector all degrade to the keyword result, no raise (tests green).
6. **Vector-first regression** — counting-embedder test asserts 0 calls on a keyword hit — held.
7. **Panel crash on hostile/malformed shapes** — hostile markup strings, mixed lists, scalar
   panels all render; `Text`-wrapped (tests green). `_panel_attention` NaN-score sort is
   deterministic enough (NIT, no crash).
8. **Crash between event and row move / double tick** — both recovery tests green; dedupe +
   guarded UPDATE make the second move a no-op rather than a clobber.
9. **Recovery requeue of the sweeper lineage** — traced (`recover_correlation` → `_requeue` →
   enqueue): confirmed real, dormant under `dry_run=True` (R-W3-002).
10. **API breakage from additive `/health`** — [RAN tests/api/ 43/0; old keys untouched].

## 6. Dimension checklist

| Dimension | Verdict | Notes |
|---|---|---|
| Correctness | PASS (code) | All four tasks do what their briefs specify; event order, union, degrade, isolation all hold |
| Root cause vs symptom | PASS | D-DEMOTE stepwise (not direct-to-T4 symptom), tag/reject semantics, closed sets — real fixes |
| Regression/compat | PASS | 162/163 + 116 + 43 + 75 + 80 (1 baseline-A) + import lock 21; only pre-existing failures remain |
| Concurrency/idempotency | PASS with note | Guarded UPDATE + full-key dedupe; vault last-writer on the record is documented single-node |
| Error handling / degrade | PASS with note | Per-panel isolation + degrade-[]; `run_weekly` unguarded (R-W3-003) |
| Security / abuse | PASS with note | Traversal/marker/NUL closed; fence bypass requires vault write (R-W3-004); unredacted TUI logs (R-W3-005) |
| Performance / scalability | MINOR | 3×50 sequential projections per health/tick (R-W3-006); else bounded everywhere |
| Migration / rollback | PASS | dry_run=True default; fallback removable; vault files stay; sweeper lineage caveat (R-W3-002) |
| Observability | MINOR | Good structured logs; tier moves absent from CurationReport (R-W3-009) |
| Maintainability / conventions | PASS | Runtime contract imports, `str,enum.Enum`, PLAN tags, no TYPE_CHECKING additions, single touch |
| Docs impact | BLOCKER | 11 citations (R-W3-001); ADR-0038 filename mismatch handled correctly by implementers |
| Scope creep | NONE | Diff files match `ownership/W3.tsv` exactly; no write-set violations |

## 7. Optimization and simplification opportunities

| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| O-1 | `tui/runner.py` panel builders | 3 independent ≤50-project scans per call + `_gather_rows` repeat; `/health` pays per request | Latency under poll/load | Small (one shared projection pass) | Low (additive) | DO LATER |
| O-2 | `coder/runtime_tools.py` hydration loop | ≤50 sequential `get_fact` per fallback call | Small N+1; bounded, miss-path only | Medium (needs batch-by-ids repo API) | Medium (new SQL) | DO LATER |
| O-3 | sweeper per-row parse guard | Parity with decay; curator already contains phase failure | None measurable | Small | Low | NOT WORTH IT |
| O-4 | `TierReport` into `CurationReport` | Moves already logged; report is a summary, not the log | Observability polish | Trivial | None | NOT WORTH IT (or fold into O-1) |

## 8. Cross-agent / cross-task interactions observed

- **Sweeper × recovery** (R-W3-002): the determinism that makes double-emit dedupe work (one
  CID) is exactly what makes recovery treat every tier event as one immortal task. The per-fact
  alternative would multiply the phantom. Fix belongs to recovery.py (not this wave's write-set);
  must land before the R3 live tick.
- **Reflection × vault fence** (R-W3-003): the delegation is correct, but the new raise semantics
  were not propagated to the caller. W4 (T-013) owns the wiring and the guard.
- **Vault governance × `_load_anti_goals`** (R-W3-007): governing `anti_goals.md` without a
  fence-aware reader pollutes policy input on the first governed write. No sync loop exists yet.
- **Runner × pre-existing `CorrelationHead` passing** (W3-IMPL-03 FINDING): `_gather_rows` passes
  head objects to `project_lineage` (works because `project` is loosely typed); the new builders
  correctly use `_correlation_id_of`. Pre-existing; noted, not W3's to fix.
- **W3 × W2 residuals:** nothing re-broke W2 locks (tier vocab/migration/replay import locks all
  green in my runs).

## 9. What I could not verify

- **Full suite** (scoped-checks rule): I did not run `pytest tests/ -q`. My scoped evidence covers
  every file W3 touched plus the immediate neighbors (api, cli, improvement, conventions import
  lock, decay/replay backstop). Baseline categories A–D all confirmed as the only failures seen.
- **Postgres/S3 live paths** — no server env (`PAA_TEST_POSTGRES_DSN`/`PAA_TEST_S3_*` unset);
  PG-tier/vault tests skip by design. The tier sweeper SQL is sqlite-dialect (`?` placeholders
  via the relational backend, which translates) — consistent with `decay.py`, but not exercised
  against PG here.
- **Qdrant vector path** — only the numpy backend is exercised by the fallback tests (disclosed
  by the implementer); the tool reads only `hit.id`, so backend shape cannot leak, but this is
  by-inspection, not by-run.
- **Docker image/CI** — untouched by W3 (no Dockerfile/CI changes in the diff).
- **Fail-before proofs** (tier file-copy revert, fallback tier removal) — verified by inspection,
  not re-executed; see §2.
- **Performance under load** — no load test run; R-W3-006 is reasoned from code, not measured.
