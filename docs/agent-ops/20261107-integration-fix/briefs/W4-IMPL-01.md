# Brief — W4-IMPL-01 (IMPL) · wave 4 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-007 (T-007) — ledger-wired `AttentionAllocator`/`allocate_attention()` consuming `workers:alloc`, scoring via ADR-0012 `attention_allocation_score`, top 3–5 → notify, new allocator `EventType`s + replay wiring, drain throttle in `drain_pending_embeddings`. Why: the score exists as pure code with zero producers/consumers; the queue stream is registered with no traffic; the drain loop iterates unthrottled.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (ledger-is-truth + EventType checklist verbatim; ledger-before-side-effect; `class X(str,enum.Enum)`; idempotency full key)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (hotspots: `core/types.py` + `ledger/replay.py` single-writer this wave)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P07 row (claim-ack, top3-5→notify, full ADR-0008 key, StrEnum waiver, starvation fallback, drain throttle owner, Settings `enabled=False/max_results=5`) + §H T08/T15 + §I (stale/empty queue, P/R=0) + §K (`allocator.enabled=False`, `allocator.scores_histogram`, `drain.throttle`)
5. ADR `docs/adr/0036-allocator-and-attention-loop.md` (FULLY — new EventType NAMES, `_NO_PHASE_CHANGE` vs `_PHASE_MAP` decision, K/F/R derivations, starvation fallback; ADR wins over plan where they differ; do NOT edit the ADR)
6. Investigation: `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-03.md` §P-007 (score signature/guards, zero callers, queue `claim/ack/depth` semantics + at-least-once, `compute_idempotency_key`, EventType checklist mapping, orchestrator `_emit` pattern `orchestrator.py:263-288`, drain sites `creator.py:1490` + 3 call sites, Fix A endorsed), `W0-INV-02.md` §P-007 queue half (`workers:alloc` zero traffic, CONTROL_PLANE shed-first, payload validation); `W0-INV-05.md` §P-014 (Settings declared W1 — `AllocatorSettings`, `drain.max_per_second`).
7. W-wave evidence: ADR-0036 §P01 (W1 — drain bound precedent), `reports/w2-implementation/W2-IMPL-02.md` (tier `_NO_PHASE_CHANGE` precedent — same shape for allocator events).
8. Template: implementation report ORCH-DIGEST first.
9. Code at HEAD (verify line numbers yourself): `src/paa/context/metrics.py:315` score, `src/paa/storage/queue/base.py` (claim/ack/depth/dead_letters), `src/paa/core/types.py` EventType (verify allocator members ABSENT — ADR names them, you add them), `src/paa/ledger/replay.py` `_PHASE_MAP`/`_NO_PHASE_CHANGE`/`match`, `src/paa/agents/orchestrator.py` `_emit`, `src/paa/memory/creator.py` `drain_pending_embeddings`, `src/paa/config.py` (`AllocatorSettings`, drain field — CONSUME only), `src/paa/daemon.py` (how ORCHESTRATOR_CORE is claimed — the scheduler pattern to mirror), W1 `tests/context/test_metrics.py` (152-test guard).

## 3. Task and acceptance criteria
Task T-007-P007. Specifically (ADR-0036-first; where ADR-0036 names a member/shape, follow it):
- `src/paa/context/allocator.py` (new): `AttentionAllocator` + `allocate_attention()` — claims `workers:alloc` (`consumer="allocator"`), validates payloads before scoring (`score` via `attention_allocation_score` with K/F/R from ADR-0036 derivations — read them; if ADR-0036 assigns thresholds, implement them; never evaluate raw `P·R`), top ≤ `max_results` (Settings) sorted deterministically (gatherer `_rank_key` tie-break `id` precedent — total order), ledger event FIRST (`_emit`-pattern via the store, full ADR-0008 key), ack only after the event persists, nack/dead-letter on poison payload (never loop). `allocator.enabled=False` gates the consumer (disabled → queue untouched, gatherer unaffected). Empty queue → no-op + `allocator.underfed` log (never block the pure gatherer — it is sync by design). `import paa.context.allocator` must succeed in a minimal install (queue/ledger imports: verify no heavy module-scope import in the context layer — context/__init__ layering + import-conventions lock; allocator sits ABOVE metrics, never inside it).
- `core/types.py`: new EventType member(s) EXACTLY as ADR-0036 names them (additive, ordered along lifecycle; `TERMINAL_EVENTS` review recorded — allocator events are non-terminal unless ADR-0036 says otherwise). `ledger/replay.py`: `_NO_PHASE_CHANGE` or `_PHASE_MAP` exactly per ADR-0036 (not both) + `match` arm if the ADR demands it; replay totality holds (never raises; unknown-event appends never used for these).
- Drain throttle: `drain_pending_embeddings(limit, …)` gains the `drain.max_per_second` Setting (declared W1 in `MemorySettings` — CONSUME, do not edit config): windowed rate check before the pending fetch; on breach → backpressure log + defer (NEVER silent drop); backward-compatible signature (3 call sites `cli/main.py`, `daemon.py`, `api/app.py` — verify-only, they gain no args).
- Producer side: the controller notify is the only `enqueue` this task needs (top-N notify path). Do NOT invent a second producer stream. If ADR-0036 names a producer payload schema, follow it (validate opaque dicts before scoring).
- `orchestrator.py`: emitter callsite only (the `_emit` pattern; one callsite for allocator events). `queue/base.py`: NO contract change expected (verify-only; the StreamName StrEnum waiver is an ADR section, not a fix).
- Tests (must FAIL before, pass after): T08 — 6 fake queue items → top ≤5 notified, sorted by score; empty queue → no-op + underfed + gatherer still serves (pure-compile a packet without allocator); T15 — emit > `max_per_second` → backpressure log + rate cap, no drop without log; replay — allocator events fold per ADR (`_NO_PHASE_CHANGE` → phase unchanged; `_PHASE_MAP` → mapped phase, whichever the ADR chose) + totality; idempotency — re-claim after visibility lapse → single notify (dedupe via full key); poison payload → dead-letter path, no loop; `P/R=0` scored via guards (sourced from metrics tests — assert no ZeroDivision path exists in your code). New `tests/context/test_allocator.py`.
- Trace tag `PLAN:integration-fix:P007` in the allocator module docstring + emit site comment.
- Acceptance: T08+T15 green with fail-before shown; `grep allocate_attention src/` live hits (def + caller); `grep workers:alloc|WORKERS_ALLOC src/` shows producer + consumer; `tests/context/test_metrics.py` 152 green unchanged; `tests/ledger/test_replay.py` green unmodified; ruff 0, mypy total 3; no `config.py`/schema edits.

## 4. Ownership
WRITE-SET (only these): `src/paa/context/allocator.py` (new), `src/paa/core/types.py`, `src/paa/ledger/replay.py`, `src/paa/memory/creator.py` (throttle only), `src/paa/agents/orchestrator.py` (emitter callsite only), `tests/context/test_allocator.py` (new). READ-ONLY: everything else. FORBIDDEN: `src/paa/config.py`, `src/paa/storage/queue/base.py`, `src/paa/storage/relational/*`, `src/paa/context/metrics.py`, `docs/adr/*`.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: COMMS CLAIM before editing `core/types.py`+`replay.py` (hotspots; sole owner this wave).

## 5. Parallel siblings this wave
W4-IMPL-02 (reflection wiring — `daemon.py`/`cli/main.py`/`reflection.py`), W4-IMPL-03 (non-ASCII v6 — `like.py`/`dialect.py`/`facts.py`/SQL/migrations). Disjoint; you own types/replay exclusively. `tests/context/test_metrics.py` shared guard.

## 6. Skip list and known traps
- Do not inline the allocator into gatherer (rejected Fix B — non-replayable). Do not claim without ledger-before-ack.
- Queue payloads are opaque dicts — validate before scoring; `attempts/max_attempts` dead-letter must terminate poison loops.
- `workers:alloc` is NOT control-plane (shed-first) — starvation fallback is ADR-0036's design (read it), not yours to improvise.
- Drain default-arg growth must be backward-compatible (3 call sites unchanged).
- Baseline failures are not yours.

## 7. Environment and tests
Scoped only: your new test file + `tests/context/test_metrics.py` + `tests/ledger/test_replay.py` + ruff/mypy scoped. Tmp fixtures only. No full suite, no containers.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: T-007-P007`, `Agent: W4-IMPL-01`, `Wave: W4`, `Report: docs/agent-ops/20261107-integration-fix/reports/w4-implementation/W4-IMPL-01.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w4-implementation/W4-IMPL-01.md` (create at start, ORCH-DIGEST first, append as you work; long-run answers incl. replay rebuilds allocator decision + load/visibility-timeout). COMMS ≤6 appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
