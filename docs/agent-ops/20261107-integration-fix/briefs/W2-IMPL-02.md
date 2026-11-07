# Brief — W2-IMPL-02 (IMPL) · wave 2 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-009 tier vocabulary/policy/replay half (T-009a) — the pure layer: EventTypes + replay arm + TierPolicy. Why: tier state must be ledger-derivable before any DDL exists; T10b's replay-rebuild needs the vocab + fold helper now.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (ledger-is-truth; "Adding an EventType means touching…" checklist verbatim; `class X(str,enum.Enum)`; canonical-JSON; non-terminal events stay out of `TERMINAL_EVENTS`)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (hotspots: `core/types.py`, `ledger/replay.py` single-writer this wave)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P09 row (replay rule, idempotency, `_NO_PHASE_CHANGE` placement, precedence, Settings dry_run) + §H T10b + §J (tier sweeper after curator DECAY; fact-scoped moves idempotent by full ADR-0008 key)
5. ADR `docs/adr/0037-tier-migration-and-replay-rule.md` (READ FULLY — projection-target decision: minimal tier-dict fold `apply_tier_event(state: dict[fact_id, tier], event)`, NOT TaskProjection fields; union precedence; T2 backfill; dry_run) + how `TierPolicy`/`ttl_hours` must be shaped there
6. Investigation (authoritative): `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-01.md` §P-009 (EventType/replay mechanics, `_PHASE_MAP` 17 mappings, `_NO_PHASE_CHANGE` semantics, totality guarantee, recovery.py vacuous for P09, `Fact` unchanged, unknown-domain skip) + §P-002's tier interaction; `W0-INV-02.md` §P-009 (DDL half — NOT yours; SCHEMA_VERSION=4 → v5 is W2-IMPL-03)
7. Template: implementation report ORCH-DIGEST first
8. Code (verify all line numbers at HEAD yourself in your context report): `src/paa/core/types.py` (`EventType` 22 members, `TERMINAL_EVENTS`), `src/paa/ledger/replay.py` (`_PHASE_MAP`, `_NO_PHASE_CHANGE`, `match`, `project()/replay()` totality, `unknown_events`), `src/paa/memory/domains.py` (`DomainPolicy`, `domain_policy()`, `is_stale`), ADR-0008 idempotency key shape (`correlation_id,event_type,attempt,discriminator` — find the helper/enforcement in `ledger/store.py` or wherever dedupe lives).

## 3. Task and acceptance criteria
Task T-009a-P009a. Specifically:
- `src/paa/core/types.py`: add `TIER_DEMOTED` and `TIER_ARCHIVED` to `EventType` (`class X(str,enum.Enum)` style, values = member names). Do NOT add them to `TERMINAL_EVENTS` (non-terminal, task-scoped phase maps unaffected). Payload contract (document in code/ADR-consistent docstring): `{fact_id, from_tier, to_tier, reason}` + ADR-0008 discriminator = fact_id + to_tier (state where the discriminator is constructed — if the store's dedupe derives it from `(correlation_id,event_type,attempt,discriminator)`, say what the sweeper (W3) must pass; do not invent a new dedupe mechanism).
- `src/paa/ledger/replay.py`: add both to `_NO_PHASE_CHANGE` (NOT `_PHASE_MAP` — they are fact-scoped, not task phases). Add the tier-dict fold helper exactly as ADR-0037 decides (`apply_tier_event(state: dict[fact_id, tier], event)` or the ADR's named shape — follow the ADR, not your own design) incl. old-events-default-T2: events lacking tier fields default the fact to T2 (document). `TaskProjection` FIELDS DO NOT CHANGE (fact-scoped state lives in the helper's dict, not the task projection). Replay totality holds: `project()` over a stream containing TIER_* never lands them in `unknown_events` and never raises.
- `src/paa/memory/domains.py`: add `TierPolicy` + `ttl_hours` mapping `{T0:4, T1:24, T2:2160, T3:17520, T4:∞}` (use `float("inf")` or the ADR's representation — follow ADR-0037) + default-T2 resolution (unknown/absent → T2). Pure data, no signature breaks. The union precedence (retention decides survival, `prune_floor` decides hot-set membership) is POLICY TEXT + a pure predicate/helper if ADR-0037 defines one — implement only what the ADR names as code; if the ADR leaves precedence as sweeper policy (W3), document that boundary in the helper docstring, do not pre-build the sweeper.
- `src/paa/config.py`: NO CHANGES (`TierSettings` declared by W1/P-014 — consume nothing; you do not read settings here). If you find you need a Settings change, post NEED and stop that half.
- `src/paa/ledger/recovery.py`: NO CHANGES expected (INV-01 verified vacuous) — verify, and record the verification; do not edit.
- Tests (must FAIL before, pass after): (a) vocab: `EventType.TIER_DEMOTED/ARCHIVED` exist, non-terminal (assert not in `TERMINAL_EVENTS`), replay totality (stream with TIER_* → not in `unknown_events`); (b) T10b-core: fold helper over an event sequence builds a tier dict; old events without tier → T2 default; (c) `TierPolicy.ttl_hours` exact mapping + T2 default for unknown/absent; (d) backstop: replaying normal task events unchanged (existing `tests/ledger/test_replay.py` green unmodified). New test file `tests/memory/test_tier_vocab_p009.py` (vocab+policy+fold); a replay-fold test may live in `tests/ledger/` — follow the existing layout (glob it first).
- Trace tag `PLAN:integration-fix:P009` comments at each addition.
- Acceptance: new tests green + fail-before shown; `tests/ledger/test_replay.py` green unchanged; `tests/memory/test_decay.py` green unchanged; ruff 0, mypy total stays 3; no DDL/migration file touched (`git diff --name-only` proves it).

## 4. Ownership
WRITE-SET (only these): `src/paa/core/types.py`, `src/paa/ledger/replay.py`, `src/paa/memory/domains.py`, `tests/memory/test_tier_vocab_p009.py` (new), `tests/ledger/test_tier_replay_p009.py` (new, if the fold test needs a ledger-side home — else keep everything in the memory file and say why). READ-ONLY: everything else. FORBIDDEN: `src/paa/storage/relational/*` (W2-IMPL-03 owns DDL), `src/paa/config.py`, `docs/adr/*` (ADR-0037 is filed — cite it, don't edit), `src/paa/memory/decay.py` (sweeper is W3).
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: post COMMS CLAIM for `core/types.py`+`replay.py` before editing (hotspots; sole owner this wave).

## 5. Parallel siblings this wave
W2-IMPL-01 (touch unification — disjoint files), W2-IMPL-03 (tier DDL — `migrations.py`+SQL; your EventTypes are its DEPENDS-ON only at sweeper level (W3); DDL does not depend on your vocab — sequence is rollout order, not a code gate), W2-IMPL-04 (Dockerfile.embed), W2-GEN-01 (P-002 minor tests). Hotspot discipline: you are the ONLY writer of `core/types.py`/`ledger/replay.py` this wave.

## 6. Skip list and known traps
- Do NOT put TIER_* in `_PHASE_MAP` or `TERMINAL_EVENTS` (reviewer block B1 — plan says `_NO_PHASE_CHANGE` or match arm, never task-phase mapping).
- Do NOT add tier fields to `TaskProjection` (INV-01 major finding: task-lineage state cannot hold fact-scoped tier; ADR-0037's tier-dict fold is the decided shape).
- Do NOT write DDL or a sweeper. W2-IMPL-03 (DDL) and W3 (sweeper) own those. If ADR-0037's text and the plan row disagree on any code shape, the ADR wins and the discrepancy is a FINDING — do not edit the ADR.
- `recovery.py` write-set line in the plan is vacuous (INV-01) — verify and record, do not "fix" it.
- Baseline category-A/B/D failures are not yours.

## 7. Environment and tests
Scoped only: your new test files + `tests/ledger/test_replay.py` + `tests/memory/test_decay.py` + ruff/mypy scoped. No full suite, no migrations on shared DB (your tests use tmp fixtures only), no containers.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: T-009a-P009a`, `Agent: W2-IMPL-02`, `Wave: W2`, `Report: docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-IMPL-02.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-IMPL-02.md` (create at start, ORCH-DIGEST first, append as you work; long-run test answers: source/load/hostile/operability/reversal incl. unknown-domain skip + T2 default + idempotent double-emit semantics). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
