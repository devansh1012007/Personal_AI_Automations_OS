<!-- ORCH-DIGEST:START -->
agent: W1-REV-01 | scope: Wave 1 T-001 C01+C01b+C02 | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 1 | MINOR 3 | NIT 3
claims_audit: 11 claims checked / 1 unsupported (acceptance-criteria-via-Runtime.build tested)
band_aids_found: none — catch-Exception handlers encode the documented pending-never-failed invariant and DB errors propagate to call-site handlers
top_3_risks: production composition (Runtime.build threading + daemon drain call) has no test — the original failure mode; frozen payload ranking snapshots + superseded vectors until reconcile wave; >100-pending backlog invisible under API/CLI single-shot drains
optimizations: DO-NOW 2 | DO-LATER 3 | NOT-WORTH-IT 2
reversal_check: reasoned-only (implementer hand-reverts recorded; reviewer probed live behaviour with scratch script, no worktree reversal)
confidence: high on code behaviour (code read + 62 scoped tests run); med on production behaviour (fakes only, no live vector stack)
<!-- ORCH-DIGEST:END -->

# Code review — W1-REV-01

## 0. Method
Diff reviewed: `git diff eb6606e24d33f56b1dd7d2d811110b77940b8318..HEAD -- src/paa/runtime.py
src/paa/memory/creator.py src/paa/daemon.py src/paa/api/app.py src/paa/cli/main.py tests/`
(code commit c5766c8; report commits 3568013/008258a read for context only).
Surrounding code read before judging: `runtime.py` build/factories (L280-490),
`creator.py` sink/`_enqueue_embeddings`/drain (L780-1030, L1170-1430), `facts.py`
`pending_embeddings`/`mark_embedded`/`Fact` (L278-342, L1045-1104),
`context_agents.py::_semantic` (L241-262), `daemon.py` loops (L144-190),
`api/app.py` drain endpoint, `cli/main.py` drain, `embeddings.py` both encoders,
`store.py` `ACTIVE_FACTS` spec, daemon/creator/runtime test harnesses.
Order followed: blind pass over diff + surrounding code first; implementer report
(`reports/w1-impl/W1-IMPL-01-T-001.md`) read only afterwards for the claims audit.
Commands run (Windows, repo root):
- [RAN `.venv\Scripts\python.exe -m pytest tests/memory/test_creator.py tests/runtime/test_memory_creator.py -q` -> 30 passed]
- [RAN `.venv\Scripts\python.exe -m pytest tests/api/test_memory.py tests/cli/test_memory.py -q` -> 32 passed]
- [RAN `.venv\Scripts\python.exe -m ruff check <9 changed files>` -> All checks passed!]
- [RAN `.venv\Scripts\python.exe -m mypy src` -> exactly baseline 3 errors `src\paa\sandbox\subprocess_backend.py:548` (win32), 0 new]
- [RAN scratch probe (temp dir, throwaway DB, inline script, file deleted) -> limit=1 leaves 1 pending; subset-return marks only returned ids; failing embedder via real `VectorEmbeddingSink` leaves `pending`; 5 concurrent drains encode the same fact 5x and lose nothing]
COMMS last line read: [2026-11-06T18:30:00Z] W1-IMPL-01 DONE.

## 1. Blind-pass findings (before reading implementer reports)
Formed from diff + surrounding code, in finding order:
1. `Runtime.build` threading (runtime.py:334-336) and the daemon drain call site
(daemon.py:158-161) are the two production paths; scanning the test diff, tests hit
`_build_memory_creator` directly and the API/CLI endpoints — I found no test that builds
a `Runtime` with backends or drives the daemon loop with a recording sink. Deleting the
two kwargs or the daemon drain block would keep the new suite green.
2. `drain_pending_embeddings` is a single pass of ≤100; all three callers invoke it once
and discard the return value; the `pending=N` log is the batch size, not the backlog.
An operator running `paa memory drain` against a 5,000-row backlog gets 100 indexed
and no signal that 4,900 remain (the daemon does progress, but also silently).
3. The `memory.embedding_sink status=degraded` warning fires on every degraded boot —
including intentional minimal installs (`enable_optional_backends=False`) — and duplicates
`runtime.vector_unavailable` from `_try_vector`.
4. Payload now freezes `confidence` (decayed-at-embed-time) and `importance` into the
vector payload; nothing refreshes them on decay/supersede, and superseded facts' points
stay in the index. No reconciler runs yet (T03/C04 by plan), so `_semantic` can return
superseded facts ranked by stale snapshots — acceptable only as a tracked deferral.
5. `zip(facts, vectors, strict=False)` in the sink still silently truncates if an
embedder returns fewer vectors than texts (pre-existing); now that the sink is live the
shortfall is silent (facts stay `pending`, so safe, but invisible).
6. Test hygiene nits: hand-rolled monkeypatch restore in
`test_sink_construction_failure_degrades`; unredacted `str(exc)` in sink error logs
(matches the existing creator convention).
Everything else probed held up: `limit<=0` guard (negative `LIMIT` in SQLite means
unlimited — the guard is load-bearing), subset returns, failing-embedder path, empty
backlog, concurrent drains, collection-name agreement (`ACTIVE_FACTS.name == "active_facts"`
at store.py:122 vs the reader literal at context_agents.py:246), 3-positional-arg
backward compat, NUL/oversized statements (extractor-capped at 8192, JSON payload
escaping), no new LIKE/SQL surface (bound params only), no schema change.

## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| 14 new tests, 62 in 4 files pass, exit 0 | ✅ Yes | [RAN pytest 4 files -> 30+32=62 passed] Matches counts (18 creator+12 runtime+16 api+16 cli) |
| ruff clean; mypy only baseline 3 | ✅ Yes | [RAN ruff (changed files) -> clean; mypy src -> same 3 errors `subprocess_backend.py:548`] Full-tree ruff not re-run by reviewer |
| Sink payload alignment (C01b) fixes empty `_semantic` content; old points read with defaults | ⚠️ Mostly | Code + round-trip test confirm new points; but "old points" cannot exist (sink had 0 instantiations pre-change) and reader default for missing `object_value` is still `""` — comment is harmless but overstates compat. NIT only |
| Failing embedder leaves `pending`; next drain flips to `indexed` | ✅ Substance yes | Tested at sink level (`BrokenSink`); reviewer probe confirmed the same at embedder level via real `VectorEmbeddingSink` + raising embedder → drain 0, status `pending` |
| Drain idempotent, never loses a fact, never writes `failed` | ✅ Yes | `drain_pending_embeddings` only calls `mark_embedded` with default status; probe: concurrent drains lose nothing |
| Root cause = unwired composition, not missing capability | ✅ Yes | Pre-change code read confirms: `_try_vector` pair never reached creator; 0 `VectorEmbeddingSink(` instantiations pre-change |
| "All acceptance criteria exercised with passing tests" | ❌ Unsupported (1 claim) | Acceptance line 1 ("`Runtime.build(...).memory_creator` has non-None sink") has no test — see R-W1-001. Daemon call-site acceptance likewise untested. Code is correct; proof is missing |
| Reversal: payload-revert → 2 failures; drain-revert → 4 AttributeErrors | ✅ Plausible | Reasoned: payload test asserts `payload["object_value"]` (KeyError on revert); round-trip asserts non-empty content (reader default `""`); missing method → AttributeError. Not re-run by reviewer (reasoned-only) |
| No band-aids; DB failures propagate to call-site handlers | ✅ Yes | Drain catches only `sink.enqueue` exceptions; `mark_embedded` DB errors propagate and all 3 call sites isolate them. Invariant-encoding, not symptom-masking |
| Keyword-only threading avoids double-constructing store/embedder | ✅ Yes | `Runtime.build` already holds the `_try_vector` pair; rebuilding would repeat `get_vector_store`+`get_embedder`. Signature keeps 3-positional calls valid (test at test_memory_creator with 3 args confirms) |
| ST embedder batching safe for 100-fact single `embed` call | ✅ Yes | `embeddings.py:223` chunks with `batch_size=min(32, len)`, lock-serialised; hash encoder vectorised. No batching change needed |
| Observability: pending-count gauge (plan §K) | ⚠️ Partial | Per-drain `pending`/`indexed` logged, but `pending` = batch size, not backlog total; no gauge. Deferred to C07 by plan; tracked as R-W1-002 |

## 3. Long-run test (independent answers)
- **Source:** Embedding is post-commit best-effort (`process_signal` → `_commit_with_retry`
→ `_enqueue_embeddings`). Crash between commit and upsert, or a `mark_embedded` failure
after upsert, leaves `pending`/orphan points — both recoverable by drain (upsert is
idempotent on fact-id). Drain introduces no new fact-write path; facts are already
committed, drain only writes the derived `embedding_status` + points. The ledger-is-truth
invariant is untouched (no direct hot-table writes outside `FactRepository`).
- **Load (100x, concurrency, partial failure):** Each drain is bounded (`LIMIT 100`,
oldest-first, no OFFSET). Daemon steady-state: 25 signals + ≤100 drained per loop (~1s
idle cadence) — drain rate exceeds fact-production rate, so a sink-absent-era backlog
drains progressively. Partial failure (sink returns subset / upsert fails mid-batch)
leaves the rest `pending` for the next drain — verified by probe. Concurrent drains are
safe but wasteful: probe showed 5 concurrent drains each embedding+upserting the same
single fact (sum=5) — idempotent overwrite, no loss, 5x embed cost. No claim/lease
exists; acceptable at current scale, revisit if double-drain is observed.
- **Hostile input:** Drain input is already-committed facts only. Statement text is
extractor-bounded (`MAX_VALUE_CHARS=8192`); NUL bytes flow into the hashing encoder and
JSON-escaped payload — no LIKE, no string-interpolated SQL added (all `?`-bound).
`limit<=0` guarded (defeats SQLite `LIMIT -1` = unlimited). Oversized backlog cannot OOM:
one 100-row fetch + one embed call per drain.
- **Operability (observe, roll back, migrate):** Wired/degraded + per-drain counts are
logged; rollback is a pure code revert (facts stay `pending`, re-drainable, no migration,
derived data only). Gaps: no total-backlog gauge (R-W1-002); degraded warning on every
intentional minimal boot (R-W1-003); failing-embedder daemon retries every ~1s loop with
no backoff (pre-existing retry philosophy; log/CPU cost only while the substrate is down).
- **Reversal (which test fails if the fix is reverted?):** Remove payload keys →
`test_payload_carries_reader_keys` (KeyError) + round-trip (empty content) fail. Remove
`drain_pending_embeddings` → 4 AttributeError failures. Remove the 3 API/CLI drain calls →
API/CLI tests fail (fact stays `pending`). Remove the `Runtime.build` kwargs or the daemon
drain call → **no test fails** (R-W1-001). Partial-drain semantics (limit, subset) revert →
no test fails (no coverage; probe-only).

## 4. Findings
### R-W1-001 [MAJOR] Production composition paths have no test (the original failure mode)
- Location: `src/paa/runtime.py:334-336`; `src/paa/daemon.py:158-161`; tests at
`tests/runtime/test_memory_creator.py`, `tests/api/test_daemon.py`
- Evidence: new runtime tests call `_build_memory_creator(settings, db, model_adapter,
vector_store=…, embedder=…)` directly. No test builds a `Runtime` with fake backends
and asserts `runtime.memory_creator._sink` is a `VectorEmbeddingSink`, and no test drives
`Daemon._ingestion_loop` (harness exists at `tests/api/test_daemon.py:43`) with a
recording sink and a seeded pending fact. The C01 root cause was literally "sink built
in isolation, never composed in production wiring" — the tests now pin the isolated unit
but not the composition, so the same class of regression (a refactor dropping the kwargs)
passes the suite.
- Why it matters: the two most important call sites (every production `Runtime`, the
primary daemon loop) are the only ones without behavioural proof; the implementer's
acceptance line 1 ("`Runtime.build(...).memory_creator` has non-None sink") is asserted
by code inspection only.
- Recommended fix (root-cause level, test-only): (a) monkeypatch `_try_vector` (and
`_acquire_sandbox`) as the existing runtime tests already do, call `Runtime.build`,
assert the creator holds a `VectorEmbeddingSink` bound to the fake store/embedder;
(b) extend the daemon ingestion test: seed a `pending` fact, attach a recording sink to
`runtime.memory_creator`, start the daemon, poll until the fact is `indexed`. Small,
deterministic, uses existing fixtures.

### R-W1-002 [MINOR] Drain is single-pass; backlog beyond 100 is invisible at API/CLI
- Location: `src/paa/memory/creator.py:1220-1262`; callers `src/paa/daemon.py:159`,
`src/paa/api/app.py:280`, `src/paa/cli/main.py:645`
- Evidence: `drain_pending_embeddings(limit=100)` drains one batch and returns; callers
discard the return value and never loop. Probe: 2 pending + `limit=1` → 1 indexed,
1 remains, log says `pending=1` (batch size). A one-shot `paa memory drain` against a
10,000-row backlog indexes 100 and reports nothing about the remaining 9,900. The
daemon is fine (progresses 100/loop), but the plan's §K "pending-count gauge"
observability is not delivered (it is C07 scope — acceptable, but it should be an
explicit tracked item, not silent).
- Recommended fix: after the drain batch, log the *remaining* backlog honestly — e.g.
one extra `pending_embeddings(1)` probe logged as `backlog_remaining=<bool|count>` — or,
if cheap, a `COUNT(*)` on the partial index in `FactRepository`. Optionally loop the
drain with an overall per-call cap (e.g. 10 batches) so one API/CLI invocation makes
real progress on a large backlog. Do not raise the single-call batch size instead —
that just moves the cliff.

### R-W1-003 [MINOR] Degraded warning fires on every intentional minimal boot
- Location: `src/paa/runtime.py:476-490`
- Evidence: whenever either backend is `None` — including a deliberate
`enable_optional_backends=False` build (test/CLI paths) or a healthy minimal install —
every `Runtime.build` emits `WARNING memory.embedding_sink status=degraded`, doubling
the `runtime.vector_unavailable` warning `_try_vector` already emits on failure. The
implementer flagged this themselves (report §14). Per-invocation CLI runs each print
one warning; volume trains operators to ignore the one signal that matters when a
real embedder silently drops out.
- Recommended fix: log the sink-absent case at `info`/`debug` when backends were
deliberately disabled, reserving `warning` for the unexpected-failure path (the
construction-exception branch already warns distinctly). No behaviour change.

### R-W1-004 [MINOR] Payload ranks by frozen snapshots; superseded facts stay indexed (deferred-by-plan)
- Location: `src/paa/memory/creator.py:845-861`; reader `src/paa/agents/context_agents.py:251-262`
- Evidence: `confidence` is the decayed value at embed time, frozen into the point
payload forever; decay/supersede never refresh it, and supersede never deletes the
point. `_semantic` therefore ranks by stale confidence and can surface superseded
facts until the curator reconcile phase (T03/C04) runs. This is pre-existing sink
behaviour (nothing ever updated points), made visible by wiring the sink live — the
plan sequences reconcile in a later wave, so this is a tracked deferral, not a Wave-1
blocker, but it caps recall correctness now.
- Recommended fix (later wave, not W1): curator `_phase_reconcile_vectors` (T03) must
delete points for superseded/pruned facts (and the C03 touch wave should consider
whether `confidence`/`importance` payload refresh is needed, or the reader should
re-read those columns by fact id instead of trusting the payload snapshot).

### R-W1-005 [NIT] Silent truncation on embedder short-return
- Location: `src/paa/memory/creator.py:860` (`zip(facts, vectors, strict=False)`)
- Evidence: an embedder returning fewer vectors than texts silently leaves the
overflow facts `pending` (safe — next drain retries — but invisible; nothing logs the
mismatch). Pre-existing line, newly load-bearing now that the sink is production-wired.
- Recommended fix: log `len(vectors) != len(facts)` (or assert in dev) before zipping;
no behaviour change needed.

### R-W1-006 [NIT] Test hand-rolls monkeypatch restore
- Location: `tests/runtime/test_memory_creator.py` (`test_sink_construction_failure_degrades`)
- Evidence: manual `creator_mod.VectorEmbeddingSink = ...` save/restore in try/finally
instead of the `monkeypatch` fixture already used elsewhere in the same file. Works,
but a mid-test raise outside the try would leak the patch.
- Recommended fix: use `monkeypatch.setattr(...)`.

### R-W1-007 [NIT] Unredacted exception text in sink logs
- Location: `src/paa/memory/creator.py:1215` (`_enqueue_embeddings`);
`src/paa/memory/creator.py:1250` (drain); `src/paa/runtime.py:481` (construction)
- Evidence: `log.error(..., error=str(exc))` whereas call sites redact. This matches
the pre-existing creator-module convention (all errors there are unredacted), and
vector/embedder errors are unlikely to carry secrets (embedder name, counts and
hostnames only). Flagged for hygiene only; not a regression.
- Recommended fix (optional, whole-module): redact consistently in `creator.py`.

## 5. Attempts to break it (concrete inputs/interleavings)
Scratch probe against a throwaway DB (script in `%TEMP%`, deleted after; repo untouched):
1. **Failing embedder (real `VectorEmbeddingSink` + raising embedder):** drain → 0,
fact stays `pending`. No loss; next drain flips it (suite covers via sink level).
2. **Store unavailable at `upsert`:** propagates out of `enqueue`, drain catches,
pins nothing, returns 0; re-drain retries.
3. **Huge backlog (2 pending, `limit=1`):** drains 1, 1 remains. Confirms R-W1-002
(single-pass); no hang/OOM. Safe, but incomplete per call.
4. **Double-drain / concurrent drain (5 concurrent drains, 1 pending fact):** all 5
embed + upsert the same point id, all 5 `mark_embedded`; sum=5, pending=0.
Idempotent; wasteful (see optimisations).
5. **Partial sink return (returns 1 of 2 ids):** only the returned id marked; other
stays `pending` for the next drain. Correct subset handling.
6. **Hostile payload (NUL / 8192-char statement):** no new surface — extractor caps and
quarantine run before any fact exists; statement text reaches only the hashing encoder
and JSON payload (NUL escaped). No LIKE/SQL interpolation introduced.
7. **Empty facts (empty backlog):** drain returns 0, logs, no embed call
(`enqueue` early-returns on empty). Covered by suite + probe path.
8. **Exception in enqueue after partial upsert:** real sink raises post-`upsert`
(transport partial apply) → facts stay `pending`, re-drain overwrites the same point
ids. Idempotent; no duplicate accumulation.
9. **`mark_embedded` DB failure after successful enqueue:** exception propagates from
drain (by design — DB errors are not swallowed), caught at all 3 call sites; vectors
already indexed, retry re-enqueues idempotently.
10. **`_build_memory_creator` backward compat:** 3-positional-arg calls unchanged
(keyword-only, defaults `None`); degrade matrix (neither/only-store/only-embedder/
construction-raises) covered by suite.
11. **Negative `limit`:** `drain_pending_embeddings(limit=-5)` → 0, no query (defeats
SQLite `LIMIT -1` = unlimited). Guard is load-bearing.
Not probed live: real Qdrant/SentenceTransformer stack (fakes only, per scope) — see §9.

## 6. Dimension checklist
| Dimension | Verdict | Notes |
|---|---|---|
| Correctness vs acceptance | Pass with gap | All C01/C01b/C02 behaviours verified in code+probes; proof missing for Runtime.build/daemon composition (R-W1-001) |
| Root cause vs symptom | Pass | Root cause (unwired composition + key mismatch + no backlog loop) fixed at source; no symptom patches |
| Regression/compat | Pass | 62 tests pass; keyword-only signature back-compat pinned; reader untouched; no schema/config/API change; `BatchReport` shape unchanged |
| Concurrency/idempotency | Pass | Drain idempotent (probe: concurrent drains lose nothing); upsert overwrite on fact-id; claim-free but safe. Waste noted, not unsafe |
| Error handling | Pass | Sink errors → stay pending + logged (invariant); DB errors propagate to isolated call-site handlers; import failure inside sink → `[]` (pre-existing) |
| Security/abuse | Pass | No new untrusted-input surface, no SQL/LIKE interpolation, secret names not values, log hygiene NIT only (R-W1-007) |
| Performance/scalability | Pass | Bounded `LIMIT 100` batches; ST embedder chunks internally (batch_size=32) and serialises; duplicate concurrent work is the only waste |
| Migration/rollback | Pass | No migration; revert = code revert, facts re-drainable, derived data only |
| Observability | Minor gap | Wired/degraded + per-drain logs present (R-W1-003 noise); no total-backlog gauge (R-W1-002) |
| Maintainability/conventions | Pass | AGENTS.md degrade-never-raise honoured; no new noqa/ignores/StrEnum; docstrings explain why; test style consistent (R-W1-006 nit) |
| Docs impact | Pass with note | Docstrings updated at all 3 change sites; C01b comment overstates old-point compat (old points cannot exist) — harmless, no operator docs touched (C06/C07 own docs) |

## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| O1 | tests (R-W1-001) | Composition paths untested | Prevents the exact regression class that caused C01 | S | None (test-only) | **DO NOW** |
| O2 | creator/daemon (R-W1-002) | Single-pass drain + batch-size-only log | Operator visibility + one-shot backlog progress | S | Low | **DO NOW** |
| O3 | daemon ingestion loop | Failing embedder retries every ~1s, no backoff | CPU/log load while substrate down; pre-existing philosophy (sink retry), C02 adds a second retry trigger | M (needs policy decision) | Medium | **DO LATER** (with T03/C04 tuning) |
| O4 | drain concurrency | Probe: 5 concurrent drains = 5x embed of same fact | Wasteful under parallel API+daemon drains only | M (claim/lease semantics) | Medium | **DO LATER**; not worth it at current scale |
| O5 | payload refresh on decay/supersede (R-W1-004) | Frozen confidence snapshot | Ranking staleness until reconcile wave | S–M | Low at reconcile time | **DO LATER** (reconcile wave) |
| O6 | embed batch chunking in sink | Current: one `embed(100)` call | None — ST chunks internally, hash is vectorised | — | — | **NOT WORTH IT** |
| O7 | rebuild sink inside `_build_memory_creator` | Tempting dedup | Double-constructs store, reloads embedder, risks two stores disagreeing | — | — | **NOT WORTH IT** (correctly rejected) |

## 8. Cross-agent / cross-task interactions observed
- **C03 (touch-on-recall):** `context_agents.py` was READ-ONLY in this wave; C01b's
producer-side alignment preserved the reader for later waves (correct call — no drift).
If C03 changes reader expectations (e.g. re-reading confidence by fact id), the frozen
payload snapshot (R-W1-004) should be revisited then.
- **C04 (curator in daemon):** touches `_maintenance_loop` (different loop from the
drain call in `_ingestion_loop`); textual overlap is one function boundary. No
double-run risk for `MemoryCurator` (Wave-0 verified no cron).
- **C07 (e2e/observability):** should close R-W1-002's pending-count gauge and
re-verify R-W1-004 with real vectors after T03 lands.
- **T08 (e2e remember→recall):** depends on this wave's wiring; the fake-embedder-only
coverage here is the honest limit stated by the implementer and accepted by this review.
- Fallback sink-less creators (`daemon.py:189` `_memory_creator` fallback, `api/app.py:275`
fallback) drain to 0 — harmless since `Runtime.build` always wires a creator; the
implementer flagged this already (their §13.2). Agree, no action.

## 9. What I could not verify
- **Live vector stack:** no Qdrant/numpy store + SentenceTransformerEmbedder end-to-end
run (fakes only, same as implementer; no servers available in this scoped review).
Collection/dimension agreement verified statically (`ACTIVE_FACTS` 384d, reader literal
matches `name`), not by execution.
- **Full pytest suite:** forbidden by standing orders; not run. The 12 pre-existing
failures in `00-mission.md` were not re-checked and are not attributed here.
- **Worktree reversal:** reasoned-only (implementer hand-reverts recorded in their §7;
reviewer probes covered the untested facets live instead). The one untested facet that
matters enough to matter is R-W1-001, stated as a finding rather than a claim.
- **Full-tree ruff:** implementer ran `ruff check src tests`; reviewer ran ruff on the
9 changed files only (all clean) plus full `mypy src` (baseline 3, exact match).
