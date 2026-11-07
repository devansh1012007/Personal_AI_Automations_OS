<!-- ORCH-DIGEST:START -->
agent: W2-IMPL-02 | domain: P-009 tier vocab/policy/replay (T-009a-P009a, pure layer) | status: DONE
items: 3 impl (types.py +2 EventTypes, replay.py _NO_PHASE_CHANGE + apply_tier_event, domains.py TierPolicy) / 0 NOT-REPRODUCIBLE
proposed_tasks: n/a (implementer)
top_risks: replay.py/types.py hotspot (sole writer this wave); helper purity contract (pure copy vs mutate) must be fixed in code/docstring so sweeper (W3) + T10b agree
cross_domain_conflicts: none (DDL W2-IMPL-03 disjoint; sweeper W3 depends on dict key shape)
out_of_plan_discoveries: 0
confidence: high (ADR-0037 read; line numbers re-verified at HEAD)
last_comms_line_read: 2026-11-07T08:48:33Z ORCH -> ALL | DECISION | W2 | wave 2 start
<!-- ORCH-DIGEST:END -->

# Implementation report — W2-IMPL-02 — P-009 tier vocab/policy/replay

## 0. Scope and method

- Brief: `docs/agent-ops/20261107-integration-fix/briefs/W2-IMPL-02.md`. Task `T-009a-P009a`.
- Required reading in order: STANDING-ORDERS.md; AGENTS.md (ledger-is-truth; "Adding an EventType means touching…" checklist); 00-mission.md; plan rev5 §E P09 + §H T10b + §J; ADR-0037 (full); W0-INV-01 §P-009 + W0-INV-02 §P-009 (skim of DDL half — NOT mine); implementation-report template (ORCH-DIGEST above).
- Code verified at HEAD before changing: `src/paa/core/types.py` (EventType 22 members at :122-174; TERMINAL_EVENTS 6 at :187-196); `src/paa/ledger/replay.py` (_PHASE_MAP 17 at :149-167; _NO_PHASE_CHANGE 5 at :291-299; apply_event :170-287; replay :302-319); `src/paa/memory/domains.py` (DomainPolicy :98-132); dedupe: `src/paa/ledger/events.py:68-102` `compute_idempotency_key(correlation_id, event_type, attempt, discriminator)` and `LedgerEvent.idempotency_key` :185-193; `src/paa/ledger/recovery.py:1-60` (task-lineage boot sweep; no tier/unknown-event branch — vacuous verified for P09).
- ADR-0037 decided shape (followed, not redesigned): tier state folded by minimal tier-dict helper `apply_tier_event(state, event)` with `state: dict[fact_id, tier]`; events carry `fact_id`, `from_tier`, `to_tier`, `reason`; `TaskProjection` gains no tier fields; `TierPolicy.ttl_hours = {T0:4, T1:24, T2:2160, T3:17520, T4:infinity}`; backfill T2; discriminator `= fact_id + to_tier`; event-before-row-move + sweeper-side already-done check (W3); `dry_run=True` default (P14/W1 owns `TierSettings`).
- Branch verified: [RAN git branch --show-current -> `ops/20261107-integration-fix`]. Pre-existing uncommitted changes from other agents (01-work-ledger.md, COMMS.md) left untouched.
- Scoped checks only; no full suite, no containers, no shared-DB migration.

## 1. What will be changed (phased)

### Phase 1 — vocab (core/types.py)
PLAN:integration-fix:P009 — add `TIER_DEMOTED = "TIER_DEMOTED"` and `TIER_ARCHIVED = "TIER_ARCHIVED"` under a memory/tier comment group after lifecycle. Do NOT add to TERMINAL_EVENTS. Docstring documents payload contract `{fact_id, from_tier, to_tier, reason}` + ADR-0008 discriminator contract (sweeper W3 must construct `LedgerEvent(discriminator=fact_id + to_tier, attempt=...)` so `compute_idempotency_key` dedupes; no new dedupe mechanism here).

### Phase 2 — replay totality + fold helper (ledger/replay.py)
PLAN:integration-fix:P009 — add both members to `_NO_PHASE_CHANGE` (never `_PHASE_MAP`). Add `apply_tier_event(state: dict[str, str], event: LedgerEvent) -> dict[str, str]`:
- pure: never mutates the input dict; returns a copy (matches `apply_event` purity at replay.py:170-174);
- TIER_* with `fact_id` present in payload: sets `new_state[fact_id] = payload.get("to_tier") or "T2"`;
- events lacking tier fields but carrying `fact_id` default that fact to `T2` (documented old-events path);
- events without `fact_id` (incl. all task-lineage events): return unchanged copy — never raises, replay stays total for old lineages.
`TaskProjection` fields unchanged. `__all__` gains `apply_tier_event`. Union precedence (retention=survival, prune_floor=hot membership) is sweeper policy (W3) per ADR-0037 — the helper docstring states this boundary explicitly and does not pre-build the sweeper.
Design decision recorded here: pure copy (not in-place mutation). Alternative rejected: in-place mutation would let two callers fold the same dict in different orders and diverge silently; pure copies match module purity and make T10b's replay-equals-live-column check a value equality.

### Phase 3 — policy (memory/domains.py)
PLAN:integration-fix:P009 — frozen dataclass `TierPolicy` with `ttl_hours` field + `DEFAULT_TIER = "T2"` (ClassVar) + `resolve(tier)` and `ttl_for(tier)`, both defaulting unknown/absent/None to T2 / 2160.0. TTL values exactly `{T0:4.0, T1:24.0, T2:2160.0, T3:17520.0, T4:float("inf")}` per ADR-0037:62. No signature breaks; `DomainPolicy` gains no tier field (ADR says the sweeper SELECTs tier itself; Fact unchanged).

### Phase 4 — tests
- `tests/memory/test_tier_vocab_p009.py` (new): (PLAN:integration-fix:P009) vocab exists + non-terminal; replay totality (TIER_* stream → `unknown_events` empty, no raise); fold helper builds dict over sequence; old events lacking tier → T2; totality on missing fact_id / hostile payloads (non-string fact_id coerced via str(), huge payload tolerated); idempotent double-emit semantics (same event folded twice → same dict); TierPolicy exact ttl mapping (incl. `math.isinf` for T4) + T2 default for unknown/absent/None/case-mismatch.
- `tests/ledger/test_tier_replay_p009.py` (new): T10b-core — replay of normal task events unchanged (phase/committed identical with vs without interleaved TIER_* ), TIER_* do not affect task phase, fold-helper `apply_tier_event` sequence rebuild incl. demote-then-archive ordering, discriminator contract: `LedgerEvent(..., discriminator=fact_id + to_tier)` produces `idempotency_key` equal to `compute_idempotency_key(...)` with same discriminator (W3 contract pinned; double-emit with same key dedupes by store semantics — helper fold is idempotent).
- Fail-before: run new tests before src edits (expect ImportError/AttributeError); then implement; re-run green. Backstops: `tests/ledger/test_replay.py` and `tests/memory/test_decay.py` green unmodified.

## 2. Verification log

### Fail-before (new tests vs unmodified src)
[RAN .venv\Scripts\python.exe -m pytest tests/memory/test_tier_vocab_p009.py tests/ledger/test_tier_replay_p009.py -q -> 2 collection errors: `ImportError: cannot import name 'apply_tier_event' from 'paa.ledger.replay'` (both modules; `TierPolicy` likewise absent). Confirms tests exercise the new code, not pre-existing behavior.]

### Pass-after (post-implementation, post-self-review-fix)
[RAN .venv\Scripts\python.exe -m ruff check src tests -> `All checks passed!` RUFF_EXIT:0]
[RAN .venv\Scripts\python.exe -m mypy src -> `Found 3 errors in 1 file (checked 139 source files)` — exactly the pre-existing baseline at `sandbox/subprocess_backend.py:548` (killpg/getpgid/SIGKILL), unchanged; gate holds (3 stays 3).]
[RAN .venv\Scripts\python.exe -m pytest tests/memory/test_tier_vocab_p009.py tests/ledger/test_tier_replay_p009.py tests/ledger/test_replay.py tests/memory/test_decay.py -> `107 passed in 4.40s` PYTEST_EXIT:0]
  - New: 32 tests ([RAN --collect-only -> 26 in test_tier_vocab_p009.py, 6 in test_tier_replay_p009.py]), incl. parametrized unknown-tier defaults and the 2 resurrection/containment tests.
  - Backstops unmodified and green: 75 tests in `tests/ledger/test_replay.py` + `tests/memory/test_decay.py` (107 total).
  - tmp fixtures only; no shared DB, no containers, no migrations run.

### Reversal proof (mutation check, no file edits)
[RAN python -c via importlib (paa/ledger/__init__ re-exports the `replay` function, shadowing the module attr — pre-existing) -> with fix: `unknown: []`; after removing TIER_* from `_NO_PHASE_CHANGE` in-process: `unknown: ['TIER_DEMOTED']` + `replay.unknown_event` warning. Proves the totality test fails for the right reason if the `_NO_PHASE_CHANGE` addition is reverted.]
### DDL / config untouched
[RAN git diff --name-only -- src/paa/storage/ src/paa/config.py -> empty. No DDL, no migration, no config change.]

## 3. Out-of-scope findings

(appending as work proceeds)

## 4. Assumptions / unknowns

- [ASSUMED] Hot tier column stores `TEXT` tier strings "T0".."T4" (matches INV-01 contract-set and ADR-0037 backfill-T2 language; W2-IMPL-03 DDL defines the actual CHECK — if DDL uses different spellings, `resolve()` must be adjusted by that owner; the dict in `TierPolicy.ttl_hours` follows the plan's "T0" spelling).
- [UNKNOWN] Sweeper (W3) will consume `apply_tier_event` as pure-copy; if W3 expected mutation it must assign the return value. Documented in helper docstring.
- Recovery.py: verified vacuous for P09 (ADR-0037:72-79 sweeper-side check; recovery.py holds no tier branch) — no edit, see §5.

## 5. Recovery.py verification (plan write-set line is vacuous)

[VERIFIED src/paa/ledger/recovery.py:1-60] module is a task-lineage boot sweep (open correlations → replay TaskProjection → workspace reconcile → STATE_ROLLBACK_TRIGGERED); no event-type dispatch over tier events, no `unknown_events` handling of its own (defers to replay). ADR-0037:72-79 places the already-done check in the sweeper, not a recovery sweep. Therefore no `recovery.py` change is required for P09; non-terminal, `_NO_PHASE_CHANGE` placement is the only recovery-interaction surface and it needs none. No edit made.

## 6. Long-run test (Source / Load / Hostile / Operability / Reversal)

- Source: vocab + helper + policy are the single source for tier state (ADR-0037:31-50 tier-dict fold); verified by T10b-core rebuild equality.
- Load: helper is O(events) dict copies — one copy per folded event (64-byte-ish entries); even 100× a sweeper tick's moves stays trivial; no unbounded structures.
- Hostile: payload fields are untrusted dict values → helper must never raise: missing/non-string fact_id → no-op copy; missing to_tier → T2; arbitrarily large payloads accepted or rejected, never crash. Pinned by tests.
- Operability: rollback = revert the two-member vocab + helper (no DDL here — DDL is W2-IMPL-03's one-way column); events emitted before rollback remain readable by replay totality (unknown-events fallback) even if helper is removed (helper removal reclassifies TIER_* to `unknown_events` — visible, not fatal).
- Reversal: removing the `_NO_PHASE_CHANGE` additions must fail the totality test (TIER_* land in `unknown_events`); removing EventType members must fail vocab + LedgerEvent construction tests; helper revert must fail the T10b-core rebuild test.

## 7. Self-review log

Per-phase and final self-review of the full diff (read fresh from `git diff`, not from memory). Findings:

- **MAJOR (fixed): non-tier events could resurrect an archived fact.** First draft of `apply_tier_event` applied the T2-default to *any* event carrying `fact_id`, overwriting an existing tier (e.g. `T4`) back to `T2` on an unrelated event. Correct semantics per ADR-0037 (tier writes come only from tier events; tier state is keyed by fact): TIER_* events always set `state[fact_id] = to_tier` (T2 fallback for malformed/legacy payloads); non-tier events only default an *unseen* fact to T2 and never overwrite. Fixed + pinned by `test_non_tier_event_does_not_resurrect_an_archived_fact` and `test_non_tier_event_cannot_set_a_tier` (fail on the first draft: they returned `T2` for the archived fact / honored payload `to_tier=T0`).
- **MINOR (fixed): `_NO_PHASE_CHANGE` comment said "folded by `apply_tier_event` below"** after the helper was placed above the constant. Reworded to "above".
- **MINOR (fixed): `DEFAULT_TIER` as dataclass field vs ClassVar.** Initial draft used `Final[str]` which dataclasses treat as an init field (caller could construct `TierPolicy(DEFAULT_TIER="T0")` and silently move the default). Changed to `ClassVar[str]` with a DECISION comment. [Verified: `TierPolicy().resolve('T9')=='T2'`, `ttl_for('T4')==inf` via python -c.]
- **MINOR (fixed): `ttl_for` could KeyError** for a hand-built `TierPolicy(ttl_hours=<partial>)`. Now `.get(..., 2160.0)`; documented as unreachable with the canonical table.
- **MINOR (fixed): ruff 7 findings** (E501 ×6 long lines, RUF005 list-concat) in the first test draft → rewrote tests with a `_tier_event` helper and iterator unpacking. ruff now 0.
- **NOTED (not a defect): `from paa.ledger import replay` binds the re-exported *function*, shadowing the module attr** — pre-existing `__init__.py` behavior; only affects console one-liners, not tests (which import from the full path).
- **NOTED (boundary kept): union precedence not implemented.** ADR-0037 states it as a rule for the sweeper; the helper docstring explicitly declines to compose retention×floor and names W3 the sole place for that composition. Pre-building the sweeper would violate brief scope + ADR layering.

Independent review: self-review only (brief's mandatory-review gate is the orchestrator's W2 review wave; no separate reviewer dispatched from this seat). Cross-phase interaction: none — three disjoint files + two new test files; no other module imports the new symbols yet.

## 8. Git state

- Branch: `ops/20261107-integration-fix` (verified immediately before commit).
- Code commit: `21030ce9bdc869f9c20208bead6286f36f188ebe` — 5 files exactly (my write-set): `src/paa/core/types.py`, `src/paa/ledger/replay.py`, `src/paa/memory/domains.py`, `tests/memory/test_tier_vocab_p009.py`, `tests/ledger/test_tier_replay_p009.py`. Nothing else staged or committed by me (siblings' uncommitted diffs — `context_agents.py`, `api/app.py`, `cli/main.py`, `coder/runtime_tools.py`, `memory/__init__.py`, `storage/relational/schema_*.sql`, `memory/touch.py`, COMMS/work-ledger — left untouched throughout).
- Report commit: separate docs-only commit listing the code SHA above (see git log).
- Deviation recorded: the code commit message is a one-liner without the `Task:/Agent:/Wave:/Report:` trailers, because `git commit --amend` is forbidden by standing orders and the message was already written; the subject carries the task ID (`T-009a-P009a`). The docs-only report commit carries full trailers.
