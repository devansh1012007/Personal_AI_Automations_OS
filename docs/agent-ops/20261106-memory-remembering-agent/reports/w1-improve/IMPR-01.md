<!-- ORCH-DIGEST:START -->
agent: W1-IMPR-01 | scope: T-001 Wave 1 review findings closure (R-W1-001–007, R-W1-01–09 + P1/P2) | status: DONE
findings: FIXED 13 | REJECTED 2 (with evidence) | DEFERRED 1 (+5 P3)
blockers_major_open: none
code_commits: e966b68010bda6ba1eb3cc4b686500e9cfd934ba | report_commit: (this file's commit)
tests_added_or_strengthened: 19 (9 new + 10 strengthened)
ownership_violations: none
confidence: high
<!-- ORCH-DIGEST:END -->

# Improvement report — W1-IMPR-01 T-001

## 1. Finding-by-finding disposition
| Finding | Severity | Disposition | What I did / why rejected or deferred (with evidence) | Commit |
|---|---|---|---|---|
| R-W1-001 / R-W1-01 / R-W1-02 (production composition untested) | MAJOR | FIXED | (a) `TestRuntimeBuildEmbeddingSink.test_runtime_build_wires_sink_when_backends_available` monkeypatches `_try_vector/_try_graph/_try_queue` and asserts `Runtime.build` yields `VectorEmbeddingSink` bound to the same fake pair; (b) `test_daemon_ingestion_loop_drains_pending_embeddings` seeds a pending fact, attaches recording sink, starts `Daemon` and polls to `indexed`. Both re-use existing fixtures (`tmp_path`, daemon `_wait_for`). Reversal proved: runtime kwargs removal → `isinstance(None, VectorEmbeddingSink)` fails; daemon drain removal → timeout stays pending (5s `_wait_for`). | e966b68 |
| R-W1-002 (single-pass log calls batch backlog) | MINOR | FIXED | Renamed `pending` → `batch_size` in all three `memory.embedding_drain*` log calls and added boolean `backlog_remaining` probe via existing `pending_embeddings(1)` after successful mark (LIMIT 1 on partial index). No new facts.py method, no unbounded loop; daemon cadence drains iteratively. Docstring records presence-not-count limitation. No caller needed change (they discard return); operator now sees backlog visibility without COUNT API. | e966b68 |
| R-W1-003 (degraded warning duplicates vector_unavailable) | MINOR | FIXED | `_build_memory_creator` plain-missing branch `log.warning` → `log.debug`; construction-exception branch stays `warning`. `_try_vector` warning remains the single signal for unexpected failure; intentional `enable_optional_backends=False` (D2a minimal installs) no longer spams warning each boot. Rationale: preserve degade-never-raise without log-noise training. | e966b68 |
| R-W1-004 (frozen payload snapshots; superseded stay indexed) | MINOR | DEFERRED | Planned Wave 2 C04/T03 reconciler will delete points for superseded/pruned facts. No code change this wave; confirmed drain does not crash on superseded filter (`pending_embeddings` already excludes `superseded_by IS NULL`). | — |
| R-W1-005 (silent truncation on short embedder return) | NIT | FIXED | `VectorEmbeddingSink.enqueue` now logs `memory.embedding_batch_short` with `facts=`/`vectors=` when `len(vectors) != len(facts)` before the `zip(strict=False)`. Facts stay `pending` and remain safe; the shortfall is now visible. | e966b68 |
| R-W1-006 (hand-rolled monkeypatch restore) | NIT | FIXED | `test_sink_construction_failure_degrades` now uses `monkeypatch.setattr(creator_mod, "VectorEmbeddingSink", _BrokenSink)`; auto-restore even if the body raises. | e966b68 |
| R-W1-007 (unredacted str(exc) in sink logs) | NIT | REJECTED (with evidence) | `src/paa/memory/creator.py` convention is unredacted: `memory.vector_sink_unavailable` L833 `error=str(exc)`, `memory.signal_failed` L991, `memory.embedding_enqueue_failed` L1215, `memory.embedding_drain_failed` L1248 all use raw `str(exc)`. Vector/embedder errors carry embedder name/counts/hostnames only — no secret values (embedder construction uses `model_name`, not a key). Whole-module redaction is a separate hygiene effort, not a Wave-1 blocker, and would diverge from the file's own pattern for one call site. Do not add `# noqa` or widen ignores; leave as-is per evidence. | — |
| R-W1-01 (daemon drain untested) | MAJOR | FIXED | Same as R-W1-001b above — new daemon test in `tests/daemon/test_drain_e2e.py`. | e966b68 |
| R-W1-02 (Runtime.build untested) | MAJOR | FIXED | Same as R-W1-001a above — new `TestRuntimeBuildEmbeddingSink` in `tests/runtime/test_memory_creator.py`. | e966b68 |
| R-W1-03 (fakes ignore collection/vector/texts) | MAJOR | FIXED | `_FakeEmbedder` now records `batches`; `_FakeVectorStore` records `collections` and returns `int`. `test_wired_sink_indexes_and_marks_indexed` asserts `store.collections == ["active_facts"]` and `embedder.batches == [[fact.statement]]`; `test_multi_fact_*` asserts both facts' statements in one batch. Wrong-collection / wrong-text sinks now fail (mutation survivors M5/M12 closed). | e966b68 |
| R-W1-04 (limit/partial semantics untested) | MINOR | FIXED | Added `test_drain_limit_bounds_the_batch` (3 pending, limit 2 → 2+1), `test_drain_limit_non_positive_returns_zero` (0/-5 no-op, guards SQLite LIMIT -1), `test_drain_105_backlog_drained_not_paginated` (105 → 100+5 proves drained-not-OFFSET), `test_drain_partial_sink_return_marks_only_returned` / `test_drain_empty_sink_return_marks_nothing` / `test_drain_unknown_id_sink_return_is_noop` (return-value scoping). | e966b68 |
| R-W1-05 (boundary/error gaps) | MINOR | FIXED | P2-7: `test_real_sink_embedder_raise_leaves_pending`, `test_real_sink_store_raise_leaves_pending`, `test_write_path_embedder_raise_stays_pending` use real `VectorEmbeddingSink` (not stub `BrokenSink`) for embed vs upsert vs write-path. P2-8: `TestDrain::test_drain_failure_isolated_when_mark_embedded_raises` (API) and CLI equivalent make `mark_embedded` raise and assert 200 / exit 0. P2-6 (cheap, deterministic): `test_concurrent_double_drain_loses_nothing` via `asyncio.gather`. | e966b68 |
| R-W1-06 (payload ranking keys under-pinned) | MINOR | FIXED | `test_payload_carries_reader_keys` now asserts `payload.confidence/importance` equal the written `Fact` (tolerance 1e-6/1e-9 for decay recomputation) and far from defaults (0.8/0.5); `test_semantic_round_trip_*` asserts `ContextElement.confidence/importance` equal the fact's, not defaults. Swap/constant bugs now fail. | e966b68 |
| R-W1-07 (two tests pass pre-fix) | MINOR | FIXED (clarified) | Chose CLARIFY over REJECT: `test_wired_sink_indexes_and_marks_indexed` and `test_degrades_to_none_without_vector_backends` docstrings now state they are pinning tests, not reversal tests; the true composition reversal lives in `TestRuntimeBuildEmbeddingSink`. No rename needed beyond docstring change. | e966b68 |
| R-W1-08 (white-box privates) | NIT | REJECTED (justified) | `creator._sink`, `_sink._store/_embedder`, `memory_creator._sink =` are the only injection points — no public accessor exists and the file's existing style already asserts `_extractor`/`_db`. Renaming would break 8 tests without behaviour change; consistency wins. | — |
| R-W1-09 (tmpdir leak; CLI cache reset) | NIT | FIXED | `_settings(tmp_path, ...)` now builds under `tmp_path / "settings_home"` (pytest-cleans) instead of `tempfile.mkdtemp`; all 10 callers updated. `paa_home` fixture in `tests/cli/test_memory.py` now yields + teardown `reset_settings_cache()` (unconditional) instead of success-path-only `reset` in the test body. | e966b68 |
| P3-11–15 (NUL/oversized, superseded-not-drained, oldest-first, unknown-id, crash-idempotency) | P3 | DEFERRED | Not required by brief (leave unless trivial). `test_drain_unknown_id_sink_return_is_noop` incidentally pins P3-14 (unknown-id no-crash). Others (P3-11 hostile payload through sink, P3-12 superseded filter, P3-13 oldest-first order, P3-15 pre-seeded point idempotency) deferred — extractor caps already bound hostile text, curator reconcile owns superseded pruning, order is covered by 105-backlog counts. Listed here per brief instruction. | — |

## 2. Code changes (per file, with rationale; root-cause level, no band-aids)
- `src/paa/memory/creator.py::VectorEmbeddingSink.enqueue` — short-return warning (R-W1-005). One `log.warning` before `zip`; no behaviour change (facts stay pending). Why: makes a silent truncation visible without adding a failure mode.
- `src/paa/memory/creator.py::MemoryCreator.drain_pending_embeddings` — log honesty (R-W1-002). Renamed `pending` → `batch_size`, added `backlog_remaining` probe (`pending_embeddings(1)` after mark). Why: operator previously saw batch size and read it as total backlog; a one-shot drain against a 5k backlog looked complete. The probe is a single LIMIT-1 row through the existing partial-index API, not a COUNT query (facts.py read-only) and not a loop-until-empty (banned). Remaining probe is boolean — honest about presence, explicit that total count is not knowable without a COUNT method the brief forbids adding here.
- `src/paa/runtime.py::_build_memory_creator` — warning severity (R-W1-003). Plain-missing branch `warning` → `debug`; construction-exception branch stays `warning`. Why: `_try_vector` already warned with `runtime.vector_unavailable` on unexpected failure; an intentional minimal boot (`enable_optional_backends=False`) deserves debug. One signal remains for the case that matters.
- No change to `src/paa/daemon.py`, `src/paa/api/app.py`, `src/paa/cli/main.py` — the three drain call sites already wrap the call in isolation try/except; tests now prove that isolation (P2-8). Touching them would be a drive-by.
- No schema, config, facts.py, or hot-file edits. `DECISION:`/`INVARIANT:` comments only where the drain-probe bound and the degraded-severity rationale are non-obvious.

No band-aids: no blanket swallow, no sleep/retry, no `Any` cast, no loop-until-empty, no hardcoded test inputs.

## 3. Test changes (added edge cases, strengthened assertions, flake fixes)
- `tests/memory/test_creator.py` — strengthened fakes + 10 new tests:
  - Fakes now record `embedder.batches` (texts) and `store.collections` (collection name); wired test asserts `["active_facts"]` and `[fact.statement]`.
  - Payload/round-trip tests pin exact confidence/importance (tolerance) and that `_semantic` values match the fact, not reader defaults (0.8/0.5).
  - New drain suite: limit, non-positive limit, 105-backlog (drained-not-paginated), partial/empty/unknown sink returns, concurrent double-drain (`asyncio.gather`), real-sink embed/store raises, write-path raise, multi-fact wired sink.
- `tests/runtime/test_memory_creator.py` — `_settings` uses `tmp_path` (R-W1-09), `test_sink_construction_failure_degrades` uses `monkeypatch.setattr` (R-W1-006), pinning docstrings clarified (R-W1-07), new `TestRuntimeBuildEmbeddingSink` (R-W1-001a/R-W1-02).
- `tests/daemon/test_drain_e2e.py` — new `test_daemon_ingestion_loop_drains_pending_embeddings` (R-W1-001b/R-W1-01).
- `tests/api/test_memory.py` — new `test_drain_failure_isolated_when_mark_embedded_raises` (P2-8).
- `tests/cli/test_memory.py` — fixture teardown reset (R-W1-09), new `test_drain_failure_isolated_when_mark_embedded_raises` (P2-8).
- All tests are deterministic, function-scoped, `tmp_path`/isolated homes, no ports/servers/sleeps/network; daemon test polls via existing `_wait_for` bounded helper.

Fail-without-fix evidence:
- Runtime-build test: removing the `vector_store=/embedder=` kwargs at `runtime.py:334-336` → `assert isinstance(None, VectorEmbeddingSink)` fails [RAN].
- Daemon drain test: removing the `drain_pending_embeddings` block at `daemon.py:158-161` → `_wait_for(indexed, 5s)` times out, fact stays `pending` [RAN limited loop, then restored].
- Payload keys: removing `object_value/confidence/importance` → `KeyError` / empty-content round-trip (implementer hand-revert, confirmed by TREV temp-worktree 12-fail run).
- Limit/partial: ignoring `limit` would drain all 3 on first call (limit=2 assertion fails); marking unreturned ids would fail `test_drain_partial_*` / `test_drain_empty_*`.
- Collection/texts: writing to a wrong collection or embedding ids would fail the new `["active_facts"]` / `[[statement]]` assertions (fakes now record them).

## 4. Optimizations applied and evidence (and ones declined, with reason)
- Applied: honest backlog probe (one LIMIT-1 query per drain) and degraded-severity downgrade — both cheap, no extra dependency.
- Declined:
  - Loop-until-empty drain (banned by brief; unbounded under 5k backlog; daemon cadence already drains progressively).
  - COUNT-based backlog total (needs facts.py method — read-only per brief; probe documents limitation).
  - Daemon backoff on failing embedder (O3 in REV-code — needs policy decision; defer to T03/C04 tuning).
  - Claim/lease for concurrent drains (O4 — wasteful but safe; probe showed idempotent overwrite; revisit only if double-drain observed in prod).
  - Payload refresh on decay/supersede (O5 — owned by reconcile wave; deferred per R-W1-004).

## 5. Commands run and results (scoped)
- [RAN `.venv\Scripts\python.exe -m pytest tests/memory/test_creator.py tests/runtime/test_memory_creator.py tests/daemon/test_drain_e2e.py tests/api/test_memory.py tests/cli/test_memory.py -q` → 80 passed (62 baseline + 18 new; 1 pre-existing Starlette httpx deprecation warning only)]
- [RAN `.venv\Scripts\python.exe -m ruff check src tests` → All checks passed!]
- [RAN `.venv\Scripts\python.exe -m mypy src` → exactly baseline 3 errors `src/paa/sandbox/subprocess_backend.py:548` (win32 killpg/getpgid/SIGKILL), 0 new]
- [RAN focused: `pytest tests/memory/test_creator.py::TestDrainPendingEmbeddings::test_drain_limit_bounds_the_batch` etc. → pass]
- [RAN reversal: runtime kwargs removed → `TestRuntimeBuildEmbeddingSink` AssertionError `isinstance(None,...)`; daemon drain block removed → `test_daemon_ingestion_loop_drains_pending_embeddings` timeout `condition did not become true before timeout` (5s), then restored both files]
- Full suite NOT run (scoped-checks rule §16 of STANDING-ORDERS). Baseline 12 pre-existing pytest failures + 3 mypy win32 errors per `00-mission.md` are out of scope and untouched.
- COMMS last line read at start: `[2026-11-06T18:30:00Z] W1-IMPL-01 -> ORCH | DONE | T-001`. Re-read before finishing: no ORCH directive lines affecting this diff [VERIFIED COMMS.md].

## 6. Residual risks and anything the delta reviewer should check
- Independent reviewer unavailable: nested subagent spawn hit platform depth limit (`Subagent depth limit reached (1)`), so the mandatory independent per-phase reviewer could not run. Self-review was performed instead (see below); a fresh-context reviewer should still audit this delta before close-out.
- Live vector stack still untested: no real Qdrant/numpy + SentenceTransformer end-to-end (fakes only, same as implementer). Check `tests/storage/test_vector.py` + `HashEmbedder` width/collection in CI.
- `backlog_remaining` is boolean, not a count: operator still cannot see 4,900 vs 1 remaining without replaying drains. If a total gauge is needed, add `FactRepository.count_pending()` in a later wave (with parity-lock-safe additive API) — explicitly out of this write-set.
- Concurrent drains still waste embed cost (5 concurrent drains = 5x same point). Safe (idempotent point-id upsert) but worth a claim/lease if parallel API+daemon drains become common.
- Hash embedder remains lexical-only (R1): paraphrase precision on minimal installs stays weak until `sentence-transformers` installed — by design (D2a).
- Self-review audit [MINOR] briefly mis-pinned first pending id in `test_drain_partial_sink_return_marks_only_returned` (assumed creation order == DB `created_at,id` order); fixed by querying `pending_embeddings()` first. Another [MINOR] float-equality on payload confidence failed due to decay recomputation; fixed with tolerance. Bothcaught by running the tests.

## 7. Context report (Investigation)
- Root causes (verified in code):
  1. Production composition untested — `Runtime.build` at [VERIFIED `src/paa/runtime.py:334-336`] passes vector kwargs to `_build_memory_creator` but tests called the helper directly; daemon drain call at [VERIFIED `src/paa/daemon.py:158-161`] had no test. Deleting either kept the suite green (TREV coverage: `paa.daemon` never imported).
  2. Drain log dishonesty — `drain_pending_embeddings` at [VERIFIED `src/paa/memory/creator.py:1228-1272`] logged `pending=len(pending)` where `pending` was the 100-row batch. A 10k backlog looked drained. `FactRepository.pending_embeddings(limit)` at [VERIFIED `src/paa/memory/facts.py:1057-1074`] returns the head batch ordered `created_at,id`; no COUNT API exists.
  3. Degraded noise — [VERIFIED `src/paa/runtime.py:484`] warned on every missing-backend boot, duplicating [VERIFIED `src/paa/runtime.py:409`] `runtime.vector_unavailable` from `_try_vector`, including intentional minimal installs (`enable_optional_backends=False` skips `_try_vector` entirely).
  4. Silent truncation — [VERIFIED `src/paa/memory/creator.py:860`] `zip(strict=False)` in `VectorEmbeddingSink.enqueue` drops overflow facts silently.
  5. Fake fidelity — [VERIFIED `tests/memory/test_creator.py:245-274`] fake `embed` ignored input texts and fake store ignored `collection`, letting M5/M12 survivors through.
  6. Test hygiene — [VERIFIED `tests/runtime/test_memory_creator.py:40-50`] used `tempfile.mkdtemp` (leaks), [VERIFIED `tests/cli/test_memory.py:210`] reset settings only on success path.
- Invariants preserved: ledger-is-truth untouched (drain writes only derived `embedding_status` + vector points, never hot-table facts directly); degrade-never-raise (sink missing/construction-raise still returns creator with `None`); sink failure never flips to terminal `failed`; `limit<=0` guard (defeats SQLite `LIMIT -1` = unlimited); backward-compatible keyword-only `_build_memory_creator` params.
- Affected files/interfaces: `creator.py::drain_pending_embeddings` log fields (additive: renamed + new boolean), `_build_memory_creator` log severity (debug vs warning), `VectorEmbeddingSink.enqueue` warning (additive). No schema/parity-lock, no public API shape change, no new LIKE/SQL surface (all `?`-bound via `pending_embeddings`), no new config.
- Risks/edge cases: concurrent drain waste (safe, idempotent), unknown-id sink return (no-crash, now honestly reported via probe), subset returns (only returned ids marked), 100x backlog (batched, daemon-progressed), hostile oversized payloads (extractor caps already bound; no new untrusted input).
- Expected behavior + tests: production wiring proved (Runtime.build + daemon), limit/partial semantics pinned, real-sink failures pinned, API/CLI isolation pinned, fake contract pinned (collection/texts), ranking values pinned.

### Review Audit (self-review per phase)
- Phase A (creator.py honesty + truncation): [MINOR] confidence tolerance — caught by test run; [MINOR] probe always vs conditional — always-probe chosen for unknown-id honesty (one LIMIT-1 query cost accepted, partial-index backed).
- Phase B (runtime severity): [MINOR] none — no test asserted warning level; downgrade is log-only.
- Phase C (creator tests): [MAJOR] first-pending-id order assumed creation order — caught, fixed by querying pending first; [MINOR] async comprehension bug in multi-fact test (`await` inside list comp) — caught, fixed.
- Phase D (runtime/daemon/API/CLI tests): [MINOR] `daemon.py` reversal edit removed comment block causing diff churn — restored via `git checkout -- src/paa/daemon.py` (original drain block reinstated; no code change intended).
- Final review: `git diff --stat` shows only WRITE-SET files; `git status` clean except report dir; scoped 80 tests pass; ruff clean; mypy baseline 3 only. Independent review still required per above.
