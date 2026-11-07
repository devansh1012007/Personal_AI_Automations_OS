# Work ledger — 20261107-integration-fix   (orchestrator-owned; source of truth for resume)

## Plan coverage
| Plan item | Summary | Disposition (task / skip-reason / deferred-reason) |
|---|---|---|
| P-001 (plan P01) | Redact 2 unredacted `str(exc)` paths | T-001-P001 (W1, in flight) |
| P-002 (plan P02) | Derive confidence at query time (kill frozen payload) | T-002-P002 (W1, in flight) |
| P-003 (plan P03) | Non-ASCII parity convergence | W4 (full plan); phrase-fold ALREADY DONE (fold done per INV-02) — stored-normalisation cols (v6) deferred to W4, ADR-0029 amendment in W2 |
| P-004 (plan P04) | Unify touch helper + coder semantic fallback | W2 (touch unification 4a) + W3 (vector fallback 4b — needs Runtime.embedder + ADR-0039) |
| P-005 (plan P05) | Derived `Dockerfile.embed` (pinned `paa/runtime:4.1`) | W2 (ADR-0042 first; INV-05 offline-vs-derived contradiction must be resolved in ADR) |
| P-006 (plan P06) | 5 TUI panels + API snapshot fields | W3 (after P-001; attention_top null until P-007) |
| P-007 (plan P07) | Ledger-wired allocator + drain throttle owner | W4 (DEEP; EventType checklist; P13 reflection shares drain Settings) |
| P-008 (plan P08) | `TOTAL_MEMORY_COST` advisory gate | W5 (DEEP/borderline; advisory only) |
| P-009 (plan P09) | T0–T4 tier layer w/ migration + replay rule | W2 (vocab/policy/replay 9a + DDL v5 9b) → W3 (sweeper 9c) |
| P-010 (plan P10) | Dual vault 15-file + governance sync fence | W3 (DEEP; reflection delegation shared with P-013) |
| P-011 (plan P11) | Pilot `coding` specialist (ADR-0041) | W5 (DEEP; rollback event ALREADY wired — ADR says so) |
| P-012 (plan P12) | Edge-count phantom closure (ADR-0043) | W1 (ADR-0043 + T13 inside T-014-P014; no code change) |
| P-013 (plan P13) | Weekly reflection wiring + `/doctor` healthcheck | W4 (daemon 4th loop + file-based last-run; reflection.py sequenced after P-010 fence delegation) |
| P-014 (plan P14) | ADRs 0036–0043 + Settings surface | T-014-P014 (W1, in flight; single-writer config.py + ADR index) |

## Tasks
| Task | Plan items | Domain | Class | Root cause (1 line) | Write-set | Contract-set | Env-set | Depends on | Conflicts (C#) | Wave | Agent | Status | Code commits | Report |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| T-014-P014 | P-014, P-012 | ADRs + Settings | DEEP (hotspot) | 8 ADRs + 8 flag sub-models unbuilt; every later wave consumes them | config.py, docs/adr/0036–0043, docs/adr/README.md, tests/test_config_settings.py | PAA_* env names (8 prefixes), specialists nested shape | PAA_ALLOCATOR__, PAA_TIER__, PAA_COST_GATE__, PAA_VAULT_SYNC__, PAA_SPECIALISTS__CODING__, PAA_REFLECTION__, PAA_MEMORY drain (TBD) | — | C1 ADR README (sole owner) | W1 | W1-IMPL-01 | in flight | — | reports/w1-implementation/W1-IMPL-01.md |
| T-001-P001 | P-001 | redaction | SURFACE | C01/C02 wired two `str(exc)` sites by hand; F-02 remainder | runtime.py:481, cli/main.py:649, 2 new test files | log event names unchanged | none | — | none | W1 | W1-IMPL-02 | in flight | — | reports/w1-implementation/W1-IMPL-02.md |
| T-002-P002 | P-002 | vector derivation | SURFACE | vector payload freezes confidence at enqueue; _semantic trusts it | agents/context_agents.py (_semantic only), memory/creator.py (comment), new test | ContextElement unchanged; payload keys unchanged | none | — | C5 context_agents.py (sequenced by function) | W1 | W1-IMPL-03 | in flight | — | reports/w1-implementation/W1-IMPL-03.md |
| T-004a-P004a | P-004 touch | touch dedup | SURFACE | 4 copies of touch helper drifted (annotation/import/docstring) | memory/touch.py (new), 4 call sites, memory/__init__.py | touch_recalled(db: RelationalBackend \| None, fact_ids) | none | — | C5 (context_agents among the 4) | W2 | TBD | planned | — | — |
| T-009a-P009a | P-009 vocab | tier vocab/policy/replay | DEEP | no tier columns/events/policy; replay target under-specified (INV-01 major finding) | core/types.py (+2 EventTypes), ledger/replay.py (_NO_PHASE_CHANGE + tier-dict helper), memory/domains.py (TierPolicy), docs/adr/0037* | EventType.TIER_DEMOTED/TIER_ARCHIVED non-terminal; _NO_PHASE_CHANGE +2 | — | ADR-0037 (W1) | C2 types/replay hotspot; C4 config | W2 | TBD | planned | — | — |
| T-009b-P009b | P-009 DDL | tier DDL + v5 migration | DEEP (hotspot) | existing rows lack tier col; parity lock demands both files same commit | schema_sqlite.sql + schema_postgres.sql + migrations.py (v5: tier, tier_updated_at, T2 backfill) | 2 new cols both engines | — | ADR-0037 (W1) | C3 parity lock | W2 | TBD | planned | — | — |
| T-005-P005 | P-005 | derived embed image | DEEP | base image torch-free by design; embeddings need isolated derived image | Dockerfile.embed (new), CI job, pyproject (verify only) | FROM paa/runtime:4.1 pinned; online-install resolution in ADR-0042 | none | ADR-0042 (W1) | C1 ADR (single-writer already done) | W2 | TBD | planned | — | — |
| T-009c-P009c | P-009 sweeper | tier sweeper dry-run | DEEP | lifecycle binary (hot vs DELETE); tier moves need event-before-row-move | memory/decay.py or new memory/tier.py, config.py TierSettings use | TierSettings.dry_run=True; T10/T10b | PAA_TIER__DRY_RUN | T-009a + T-009b | C4 config | W3 | TBD | planned | — | — |
| T-010-P010 | P-010 | dual vault | DEEP | 4-file vault; 11 RFC files absent; fence has no conflict detection; reflection shadow writer | memory/world_model.py, improvement/reflection.py (delegate), vault seed, config VaultSyncSettings | BeliefDocument +11 additive; conflict ValueError(path) | PAA_VAULT_SYNC__ENABLED | ADR-0038 (W1) | C6 reflection.py (shared with P-013) | W3 | TBD | planned | — | — |
| T-006-P006 | P-006 | TUI/API panels | SURFACE | 5 named panels absent; 3 lack data sources (honest null) | tui/snapshot.py, dashboard.py, runner.py, api/app.py, api/schemas.py, memory/facts.py (counts query) | Snapshot +5 nullable fields; HealthResponse additive | none | T-001 (done W1) | C5 api/app.py (shared with touch) | W3 | TBD | planned | — | — |
| T-004b-P004b | P-004 fallback | coder vector fallback | DEEP | recall_facts keyword-only; Runtime has no embedder field | coder/runtime_tools.py, runtime.py (embedder field), ADR-0039 | recall_facts payload; Runtime.embedder | none | T-002 (W1) + ADR-0039 (W1) | C5 runtime.py (shared) | W3 | TBD | planned | — | — |
| T-007-P007 | P-007 | allocator + drain | DEEP | scorer pure with zero producers/consumers; workers:alloc unwired; drain no owner | context/allocator.py (new), core/types.py, ledger/replay.py, storage/queue/base.py (verify), memory/creator.py throttle, config AllocatorSettings | EventType pair + _PHASE_MAP; allocate_attention() | PAA_ALLOCATOR__*, PAA_MEMORY drain | T-004b (scorer correctness only) | C2 types/replay | W4 | TBD | planned | — | — |
| T-013-P013 | P-013 | reflection wiring | SURFACE | engine tested, never scheduled; no last-run storage | improvement/reflection.py, daemon.py, cli/main.py (doctor row), config ReflectionSettings | run_weekly() unchanged; doctor additive row | PAA_REFLECTION__ENABLED | T-010 fence delegation first | C6 reflection.py | W4 | TBD | planned | — | — |
| T-003-P003 | P-003 | non-ASCII convergence | DEEP | phrase-fold done; column-fold + alias-blob exact-rung open (INV-02) | storage/relational/like.py, entity paths, migrations v6, ADR-0029 amendment | like_pattern unchanged signature | none | — | C3 parity (v6 after v5) | W4 | TBD | planned | — | — |
| T-008-P008 | P-008 | cost gate | DEEP/borderline | no cost function; curator has no advisory signal | context/metrics.py or curator.py, config CostGateSettings, ADR-0040 | total_memory_cost(...) pure | PAA_COST_GATE__* | T-007 (scorer lives) | — | W5 | TBD | planned | — | — |
| T-011-P011 | P-011 | coding specialist | DEEP | specialists pkg empty; depth>2 returns failure without rollback event | agents/specialists/*, core/types.py (AgentRole), orchestrator, policy globs, ledger | AgentRole.CODING_SPECIALIST | PAA_SPECIALISTS__CODING__ENABLED | T-007 + ADR-0041 (W1) | C2 types.py | W5 | TBD | planned | — | — |

## Waves
### Wave 0 (investigation)
- WAVE_BASE_SHA: 88e648bf88b187c9cb0007f07e4019a079daf2d5
- Agents: INV-01..INV-05 (all DONE)
- Ownership file: ownership/W0.tsv
- Pre-mortem: plan file:line claims stale → investigators re-verified; plan PREFIX/HEAD drift found (HEAD `c15b665` = docs-only over `88e648b`; `src/` identical) — all claims hold.
- Gate results: orchestrator spot-checks 10 claims (effective_confidence 11/5, workers:alloc 1 producer, STATE_ROLLBACK_TRIGGERED wired, _touch_recalled 4 defs/12 hits, total_memory_cost absent, ADR 0036–0043 free, Dockerfile.embed absent, redact :256/:278, runtime:481 unredacted, replay _NO_PHASE_CHANGE) — all held (see 00-mission.md).
- Status: DONE (reports committed W1 boundary commit)

### Wave 1 (foundation: Settings + ADRs + redaction + derivation)
- WAVE_BASE_SHA: `c15b665` (run HEAD at open)
- Agents: W1-IMPL-01 (T-014), W1-IMPL-02 (T-001), W1-IMPL-03 (T-002) — parallel, disjoint write-sets (ownership/W1.tsv)
- Pre-mortem: (a) P-014 ADR texts copied from plan without resolving INV-05 offline-vs-derived contradiction → gate: reviewer checks ADR-0042 resolves it explicitly; (b) P-001 Fix B temptation (new helper) → forbidden by brief; (c) P-002 payload-enrichment temptation → Fix A enforced, creator.py comment-only; (d) siblings colliding on config.py/runtime.py → disjoint ownership + COMMS CLAIM required.
- Gate results: orchestrator gate ruff 0 / mypy 3 / W1 scoped 47 green; REV-W1 REJECT (BLOCKER docs 3→63, MAJOR unredacted hydration log) + TREV-W1 APPROVE-WITH-CHANGES → fixes 10989e1 + 789f501 → re-review REV-W1b/TREV-W1b both APPROVE-WITH-CHANGES, 0 blocker/major; orchestrator re-gate ruff 0 / mypy 3 / docs-lock 5 passed (63→0) / W1 tests 52 green.
- Status: DONE

### Wave 2 (DONE)
- WAVE_BASE_SHA: `32b3138` (run HEAD at open)
- Agents: W2-IMPL-01 (T-004a touch), W2-IMPL-02 (T-009a vocab), W2-IMPL-03 (T-009b DDL v5), W2-IMPL-04 (T-005 embed image), W2-GEN-01 (minor P-002 tests) — parallel, disjoint write-sets (ownership/W2.tsv)
- Pre-mortem: (a) blind +N docs shifts again this wave → post-wave docs re-point is a standing gate item (T-004a/T-009b/T-005 all insert mid-file); (b) DDL backfill silent-skip (single guard) → separate guards enforced in brief; (c) Docker build claimed locally → brief forces NOT-RUN honesty; (d) tier EventTypes in _PHASE_MAP/TERMINAL → brief forbids, _NO_PHASE_CHANGE only.
- Gate results: orchestrator gate ruff 0 / mypy 3 / W2 scoped green (1 PG skip) / docs-lock 44 broken → REV-W2 REJECT (BLOCKER Dockerfile USER, BLOCKER docs, MAJOR fold validation) + TREV-W2 APPROVE-WITH-CHANGES → fixes f009484 + 8d39e5c → re-review REV-W2b/TREV-W2b both APPROVE-WITH-CHANGES, 0 blocker/major → orchestrator re-gate ruff 0 / mypy 3 / lock 5 passed.
- Status: done

### Wave 3 (DONE)
- WAVE_BASE_SHA: `492d4b2` (run HEAD at open)
- Agents: W3-IMPL-01 (T-009c sweeper), W3-IMPL-02 (T-010 vault), W3-IMPL-03 (T-006 panels), W3-IMPL-04 (T-004b fallback) — parallel, disjoint write-sets (ownership/W3.tsv)
- Pre-mortem: (a) dry_run bypass (sweeper hardcodes flag) → brief forces injection; (b) fence silent-overwrite → conflict error is the contract; (c) fake attention score → null enforced; (d) second touch copy at fallback site → T05 lock must stay green; (e) docs lock re-break (standing gate item).
- Gate results: REV-W3 REJECT → fixes 037a8d6 + f3baf89/efe4207 → REV-W3b/TREV-W3b both APPROVE-WITH-CHANGES, 0 blocker/major → orchestrator re-gate ruff 0 / mypy 3 / lock 5 passed.
- Status: done

### Wave 4 (DONE)
- WAVE_BASE_SHA: `45b542d` (run HEAD at open)
- Agents: W4-IMPL-01 (T-007 allocator+drain), W4-IMPL-02 (T-013 reflection), W4-IMPL-03 (T-003 stored-norm v6) — parallel, disjoint write-sets (ownership/W4.tsv)
- Pre-mortem: (a) queue without ledger = non-replayable → ledger-before-ack enforced; (b) T13 literal-not-ADR → fixed pins; (c) docs lock re-break (standing gate item).
- Gate results: orchestrator gate ruff 0 / mypy 3 / W4 scoped green (1 PG skip) / docs-lock 2 failing → REV-W4 REJECT (BLOCKER docs lock, MAJOR R-W4-002 unscheduled allocator, MAJOR R-W4-003 TUI mirror dead, MAJOR R-W4-004 graph 2nd writer, casualties C1–C5 stale) + TREV-W4 APPROVE-WITH-CHANGES → fixes 8971e83 + 48b9b5a/9e8e688 + 6fec6bb → re-review REV-W4b APPROVE-WITH-CHANGES (MAJOR R-W4b-001 test pollution — fixed by 6fec6bb shape (a)) + TREV-W4b APPROVE-WITH-CHANGES → orchestrator re-gate ruff 0 / mypy 3 / lock 5 passed.
- Status: done. Tracked residuals: R-W4-002 allocator scheduling (W5), R-W4-006 DRAIN_THROTTLED emitter (W5), R-W4b-003 docs sweep ~65 lock-blind (W5), R-W4b-004 v5 STRICT wedge NEED (open, see deferred), drain-window survivor (open, minor).

### Wave 5 (DONE)
- WAVE_BASE_SHA: `af8c12b` (run HEAD at open)
- Agents: W5-IMPL-01 (T-008 cost gate), W5-IMPL-02 (T-011 coding pilot), W5-IMPL-03 (T-007b scheduling + attention_top + DRAIN_THROTTLED), W5-DOCS (docs sweep + ADR-0029 amendment 0044) — parallel, disjoint write-sets (ownership/W5.tsv)
- Pre-mortem: (a) auto-delete temptation on cost gate → advisory only; (b) 21-specialist big-bang → coding pilot only; (c) allocator producer invented → named decision OR operator-fed amendment; (d) docs sweep blind +N → read each citation; (e) ADR-0029 amendment numbered wrong → 0044 verified free.
- Gate results: orchestrator gate ruff 0 / mypy 3 / W5 scoped green / lock green → REV-W5 APPROVE-WITH-CHANGES (3 MAJOR, 4+4 MAJOR test gaps) + TREV-W5 APPROVE-WITH-CHANGES → fix c2e746c + cef92c2 → re-review REV-W5b APPROVE + TREV-W5b APPROVE-WITH-CHANGES, 0 blocker/major → orchestrator re-gate ruff 0 / mypy 3 / lock 5 passed.
- Status: done. Residuals: R-W5-006 settings-threading (deferred, legitimate MINOR — orchestrator.py+daemon.py needed); exact-threshold boundary, cap kept-set order, producer multi-line evasion, 50-task ≥45 soften (all MINOR, recorded).

## Deep dives
| Task | Agent | Design approved by | Report | Reviewers |
|---|---|---|---|---|
| (none yet — ADR-0037 projection-target decision is recorded in ADR-0037 by T-014; tier-dict fold accepted per INV-01 §P-009) | — | — | — | — |

## Conflicts (cross-task, decided)
- C1: ADR README + ADR files — sole owner T-014 (W1). Later waves cite, never create ADRs in parallel.
- C2: `core/types.py` + `ledger/replay.py` EventType surface — T-009a (W2) → T-007 (W4) → T-011 (W5) sequenced; each adds to the previous, never parallel.
- C3: schema parity lock — T-009b owns both SQL files + `migrations.py` v5 (W2); T-003 v6 only after v5 lands (W4); same-commit both-files rule enforced.
- C4: `config.py` — sole owner T-014 (W1) declares all 8 sub-models; later waves consume flags, do not declare new sub-models except via new task brief.
- C5: `context_agents.py` / `api/app.py` / `runtime.py` shared files — sequenced by function (T-002 _semantic W1 → T-004a touch sites W2 → T-004b runtime.embedder W3); CLAIM via COMMS before editing.
- C6: `reflection.py` — T-010 delegates `_write_managed_block` to WorldModel (W3) BEFORE T-013 weekly wiring (W4); no parallel edits.
- C7: `migrations.py` v5 step number — T-009b confirms v5 is next (INV-01: SCHEMA_VERSION=4; INV-02: same) before writing.

## Deferred / open items
- W1 residuals (REV-W1b/TREV-W1b, all minor/non-gating): hostile payload scalar crash in fallback `float()` (pre-existing class, 2 branches — DO LATER hardening); ~200 non-broken docs citations off-by-one (lock-blind, pre-existing); R-W1-005 ANSI residual (DO LATER, needs rendering-policy decision); R-W1-008 `inf` threshold (DO LATER, bundle if config touched again); TREV R-W1b-03 TypeError-path test (NIT, seen-and-skipped); pre-existing unredacted `str(exc)` siblings at `context_agents.py:137/:252/:367` + INV-04's 5 further siblings (FINDINGs — revisit as one harden task before final review).
- RSK3 (vault precedence overwrite) — closed by T-010 fence conflict semantics (ADR-0038); verify at W3 gate.
- RSK4 (first deleting tick before R3 verified) — dry_run=True default enforced by T-014 flag; R3 drill is operator-gated manual (plan §H Manual) — flagged for final report.
- RS2 Docker availability UNKNOWN on host (INV-05: docker absent on Windows host) — P-005 T06 runs in CI/embed-image only; not a W1 blocker.
- Baseline category A–D failures (00-mission.md) — pre-existing; gate = never grow; category B strftime + category C docs-ref + category D stale test are NOT this run's fixes unless directed.
- `datetime` — planned Wave sequencing above; no task may start until its upstream task's review passes (W1 gate before W2 briefs).

## Out-of-plan discoveries
- A-001: mypy 3 pre-existing errors on Windows host at `src/paa/sandbox/subprocess_backend.py:548` (POSIX `os.killpg`/`os.getpgid`/`signal.SIGKILL` in `else` of `IS_WINDOWS`); CI Linux gate unaffected. Pre-existing, not this run's change.
- INV-01: reflection shadow writer + private-method use (in T-010 scope via delegation refactor).
- INV-01: `world_model.py _new_document(..., source)` ignores `source` (include header write in T-010).
- INV-04: 5 further unredacted `str(exc)` siblings of P-001 (out of scope; FINDINGs, not fixes).
- INV-04: `hot_serving_evaluation_results` + `hot_serving_agent_registry` tables never written by any code (P-006 honest-null context).
- INV-05: CI `all` shape timeout — embed job would add second torch download path (scope to all-only/nightly — ADR-0042 should record).

## Temporary mitigations (user-approved)
- A-005: operator authorized autonomous execution (no approval checkpoints mid-run); wave gates substitute for checkpoints. Recorded in 00-mission.md assumptions.

## Assumptions → see 00-mission.md
