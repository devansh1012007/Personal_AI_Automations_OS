<!-- ORCH-DIGEST:START -->
agent: W1-TREV-01 | scope: Wave 1 tests (T-001, C01+C01b+C02, T01-T03) | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 14 | fail-before/pass-after confirmed: 12 / reasoned: 0 / not-possible: 0
findings: BLOCKER 0 | MAJOR 3 | MINOR 4 | NIT 2
flaky_suspects: none
missing_edge_cases: P1 5 | P2 5 | P3 5
mutation_survivors: 7 of 11 plausible bugs not caught
changed_behavior_without_tests: daemon._ingestion_loop drain (src/paa/daemon.py), Runtime.build threading (src/paa/runtime.py:334)
confidence: high (temp-worktree pre-fix run + 3x scoped repeats + coverage; live embedder/Qdrant not run)
<!-- ORCH-DIGEST:END -->

# Test quality review — W1-TREV-01

## 0. Method (temporary worktree used? repeat counts, coverage tooling)

- READ-ONLY on source; only this report file written. No commits made.
- Verified count: `git diff eb6606e..HEAD -- tests/` adds **14** tests
  (7 in `tests/memory/test_creator.py`, 5 in `tests/runtime/test_memory_creator.py`,
  1 in `tests/api/test_memory.py`, 1 in `tests/cli/test_memory.py`) [VERIFIED diff].
  Implementer digest claim of 14 is correct.
- Read in order: brief, STANDING-ORDERS, AGENTS.md, 00-mission.md, plan
  C01/C02/C01b/T01-T03, review-tests template, COMMS.md, then the diff and
  surrounding code (`creator.py` sink/drain, `facts.py` pending/mark,
  `context_agents.py::_semantic`, `vector/embeddings.py` + `vector/store.py`
  protocols, test fixtures/helpers), then implementer report for claims audit.
- [RAN `.venv\Scripts\python.exe -m pytest tests/memory/test_creator.py
  tests/runtime/test_memory_creator.py tests/api/test_memory.py tests/cli/test_memory.py -q`
  -> 62 passed (14 new + 48 pre-existing), repeated **3x** total, identical result, no flake.]
- [RAN same 14 tests `-v` -> 14 passed in 1.41s.]
- **Temp-worktree fail-before (NOT reasoned):** created detached worktree
  `C:\Users\devan\AppData\Local\Temp\opencode\trev-prefix` at pre-fix base
  `eb6606e`, copied the 4 new test files over it, ran with
  `PYTHONPATH=<worktree>/src` so the pre-fix `src/paa` shadows the editable
  install [RAN -> **12 failed, 2 passed**]. Worktree removed afterwards
  (`git worktree remove --force`); shared tree untouched. The 2 that pass
  pre-fix do so by construction (see §1): they pin pre-existing behaviour,
  they do not prove the fix. Implementer hand-revert claims (payload/drain)
  are independently confirmed by this run.
- [RAN scoped coverage `--cov=paa.memory.creator,paa.runtime,paa.daemon,paa.api.app,paa.cli.main`
  over the 4 files -> creator 80%, runtime 76%, api 58%, cli 43%; `paa.daemon`
  **never imported** by these files. New-code misses: `creator.py:1236`
  (`limit<=0`), `creator.py:829` (sink empty-batch), `api/app.py:281-282`
  (drain-failure except), `cli/main.py:646-647` (drain-failure except),
  all of `daemon.py` drain lines.] [ASSUMED] full-suite gate not run
  (scoped-checks rule §16 of STANDING-ORDERS).
- COMMS last line read at start: `[2026-11-06T18:30:00Z] W1-IMPL-01 -> ORCH | DONE | T-001`.
  Re-read before finishing: no new peer lines affecting this review [VERIFIED COMMS.md].

## 1. Per test/suite assessment

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_creator.py::TestVectorSinkSemanticRoundTrip::test_wired_sink_indexes_and_marks_indexed` | Yes — point in store + `indexed` + 1 fact | **No (passes pre-fix)** — `VectorEmbeddingSink` already indexed on write path; this pins it | Yes (`db` tmp fixture) | Fake store ignores `collection` (M5) | Pinning, not reversal. Name overclaims T01 wiring (sink attached by hand, not via `Runtime`). |
| `::test_payload_carries_reader_keys` | Yes — exact `object_value`, `>0` conf/imp, `statement != ""` | Yes (KeyError pre-fix) | Yes | Same fake; exact conf/imp values not pinned (swap survives) | Strongest C01b test. `>0` is weak for ranking keys. |
| `::test_semantic_round_trip_returns_non_empty_content` | Yes — real `PlannerContextBuilder._semantic`, `content=="active"` | Yes (`'' != ''` pre-fix) | Yes | Fake `search` returns 0.99 regardless of vector — proves key plumbing, **not** paraphrase/relevance | Good reader-contract test; does not prove semantic retrieval. |
| `TestDrainPendingEmbeddings::test_drain_indexes_pending_and_is_idempotent` | Yes — pending→indexed, 2nd drain 0 | Yes (AttributeError) | Yes | `_RecordingSink` (pre-existing) | Core T02. Single-fact only (no limit/backlog). |
| `::test_drain_with_failing_sink_leaves_pending_then_recovers` | Yes — sink raise → 0 + `pending`, recovery → 1 + `indexed` | Yes (AttributeError) | Yes | `BrokenSink` at drain level, **not** real `VectorEmbeddingSink` with failing embedder/store | Catches M3/M4. See P2-7. |
| `::test_drain_without_sink_returns_zero_and_leaves_pending` | Yes | Yes (AttributeError) | Yes | None | Degraded-path pin. Good. |
| `::test_drain_empty_backlog_returns_zero` | Yes | Yes (AttributeError) | Yes | None | Trivial but correct. |
| `test_memory_creator.py::TestBuildMemoryCreatorEmbeddingSink::test_wires_sink_when_vector_store_and_embedder_available` | White-box: `isinstance(_sink, VectorEmbeddingSink)` + `_store/_embedder is` | Yes (TypeError pre-fix: no kwargs) | Yes (but `_settings()` leaks `mkdtemp`, see NIT) | **Thinnest fakes**: store is bare object, embedder 1-dim, no `embed_one` — fine for construction-only, would fail real width checks if exercised | Asserts privates; consistent with file's existing `_extractor` style. Does **not** test `Runtime.build` threading. |
| `::test_degrades_to_none_without_vector_backends` | Yes (`_sink is None`) | **No (passes pre-fix)** — no-backend case was already None | Yes | Same | Pinning only. |
| `::test_degrades_when_only_store_present` | Yes | Yes (TypeError) | Yes | Same | Matrix is complete for the helper. |
| `::test_degrades_when_only_embedder_present` | Yes | Yes (TypeError) | Yes | Same | As above. |
| `::test_sink_construction_failure_degrades` | Yes | Yes (TypeError) | Yes; restores module attr in `finally` (not `monkeypatch`, but correct) | Broken-sink via module patch, mirrors pre-existing extractor test | Good degrade-never-raise pin. No log assertion (extractor test checks redaction; this one does not). |
| `api/test_memory.py::TestDrain::test_drain_also_indexes_pending_embeddings` | Yes — real route, seeded `pending` → `indexed`, `pending_embeddings()` excludes it | Yes (stays pending pre-fix) | Yes (`runtime`/`client` fixtures, tmp) | Local `_RecordingSink` returns all ids; mutates `runtime.memory_creator._sink` (only injection point) | Behavioural, not white-box on enqueue args. Drain-failure isolation untested (except block uncovered). |
| `cli/test_memory.py::TestDrain::test_drain_also_indexes_pending_embeddings` | Yes — real CLI via `CliRunner`, global `pending == []` | Yes (1 row left pre-fix) | Yes (`paa_home` tmp; note `reset_settings_cache()` only on success path) | Local `_RecordingSink` + `monkeypatch _build_runtime` to attach it; asserts *global* emptiness, not the seeded fact's status | Behavioural. Heavier (full `Runtime.build`); no failure-mode coverage. |

**Fake-contract audit (brief requirement):**
- `Embedder` protocol [VERIFIED `src/paa/storage/vector/embeddings.py:77-96`]:
  `dimensions`/`model_name` *properties*, `embed(texts)->FloatArray`
  (`(n,dimensions)` float32, L2-normed), `embed_one`. Creator fake returns a
  **list** (not ndarray), `dimensions` as plain attr, no `embed_one` — duck-typing
  suffices for `VectorEmbeddingSink`/`_semantic`, and nothing under test calls
  `embed_one`, so this is a [MINOR] fidelity gap, not a false pass by itself.
- `VectorStore` ABC [VERIFIED `src/paa/storage/vector/store.py:424,490,494,513`]:
  `ensure_collection/collection_exists/upsert->int/search/delete/count` +
  context-manager/close. Creator fake implements only `upsert/search/count`,
  `upsert` returns `None` (real returns `int`), `search`/`count` ignore
  `collection`, `search` ignores `query_vector`/`score_threshold`/`filters`.
  Runtime fake implements *none* of these. Consequence: any bug in the collection
  name, the embedded vector, or search relevance is invisible (M5/M12).
  Fakes are adequate for "sink was called with this payload" and inadequate
  for "the right collection/vector/score" — the tests do not claim the latter,
  but the plan's T01 ("fact semantically retrievable", "one point in
  `active_facts`") is only half-proven: no test asserts the collection is
  `active_facts`.

## 2. Coverage of changed lines/branches, error paths, boundaries

Covered by the 14: payload keys (`creator.py:855-857`), `_semantic` round-trip,
drain happy path + idempotence + sink-raise + sink-None + empty backlog,
runtime construct/degrade matrix (4 combos + construction-raise), API/CLI
happy-path drain call sites.

Not covered (coverage evidence in §0):
- **Daemon drain call site — 0%** (`daemon.py` never imported by these files;
  no test in `tests/daemon/` references `drain_pending_embeddings`) [VERIFIED grep].
- **`Runtime.build` threading** (`runtime.py:334` pass-through) — only the
  helper `_build_memory_creator` is called directly. Coverage shows the
  helper lines hit, but no test would fail if `Runtime.build` stopped
  passing the kwargs.
- **Drain `limit` branch** (`creator.py:1235-1236`): `limit<=0`, `limit=1`
  with N>1 pending, default-100 with >100 pending — all untested.
- **Partial sink behaviour**: sink returns subset / empty / unknown ids —
  `mark_embedded` scoping untested (code is correct by inspection:
  `creator.py:1254-1256`, but no test pins it).
- **Real-sink failure modes**: `VectorEmbeddingSink` with embedder raising,
  store.upsert raising, or `paa.storage.vector` import failing (`creator.py:829-834`,
  line 829 also uncovered) — only a stub `BrokenSink` is used.
- **Call-site failure isolation**: API `api/app.py:281-282` and CLI
  `cli/main.py:646-647` except blocks uncovered — no test makes
  `drain_pending_embeddings` raise from `mark_embedded` (DB error) and asserts
  the drain *report* still returns.
- **Boundaries**: multi-fact signal through wired sink; concurrent drains;
  superseded-fact exclusion; oldest-first order with multiple pending.

## 3. Mutation thinking

| Plausible bug | Caught? | By which test / why it survives |
|---|---|---|
| Payload key `object_value` missing/renamed | **Caught** | `test_payload_carries_reader_keys` (KeyError); round-trip (`'' != ''`) |
| Drain method deleted / never called at API or CLI | **Caught** | 4× `AttributeError` (creator), API/CLI pending assertions |
| Drain not called in **daemon** loop | **SURVIVES** | No daemon test at all (MAJOR-1) |
| `Runtime.build` forgets to pass `vector_store/embedder` (helper still works) | **SURVIVES** | Tests call helper directly (MAJOR-2) |
| Wrong collection (`absolute_facts_index` or typo) in sink | **SURVIVES** | Fake store discards `collection` (MAJOR-3) |
| Sink embeds wrong text (e.g. `""` or ids, not `f.statement`) | **SURVIVES** | Fake embedder ignores input texts; fake search ignores vector (M12) |
| `confidence`/`importance` swapped, or set to constants | **SURVIVES** | Tests assert only `>0`; round-trip ignores ranking values (MINOR) |
| Drain marks `failed` (terminal) or `indexed` on sink failure | **Caught** | `test_drain_with_failing_sink...` asserts `pending` + return 0 |
| Drain returns `len(pending)` instead of `len(indexed)`; marks unreturned ids | **SURVIVES** | No partial/empty-return sink test (P1-4) |
| Drain ignores `limit` (drains all / `LIMIT 0`) | **SURVIVES** | No multi-fact or limit test; `limit<=0` branch uncovered (P1-3) |
| Double/concurrent drain double-indexes or loses facts | **SURVIVES (sequential only)** | Idempotence is sequential; no `asyncio.gather` drain test (P2-6) |

**7 of 11 survivors.** The two mission-critical bugs from the plan
(empty semantic content, stuck pending) and drain loss are all caught
(pre-fix run proves it). The survivors cluster exactly where the fakes
ignore arguments (collection, vector, texts) and where no test exists
(daemon, `Runtime.build`, limit/partial).

## 4. Flakiness evidence (repeated runs) and test-suite health

- 4 test files: 62 passed (14 new + 48 pre-existing), **3x** identical
  [RAN §0]. New-14 subset: 14 passed. No order dependence observed
  (function-scoped `db`, tmp homes, no ports/servers/sleeps/network).
- Only warning is pre-existing Starlette `httpx` deprecation in api
  testclient (also in implementer run). No flaky suspects.
- [NOT-RUN] Full suite and ruff/mypy (scoped-checks rule; implementer
  recorded ruff clean + mypy baseline-3, not re-claimed here).

## 5. Findings

- **R-W1-01 [MAJOR] Daemon drain call site has no test.**
  `src/paa/daemon.py` adds `drain_pending_embeddings()` after `run_batch`
  in `_ingestion_loop`, but no test exercises the daemon path
  (`grep drain_pending tests/daemon` = 0; coverage: module never imported).
  A removal/regression there passes the whole suite. Fix: daemon-level test
  (drive `_ingestion_loop` once or extract the post-batch step) asserting a
  seeded `pending` fact becomes `indexed`. (Behavior change without test —
  automatic MAJOR per brief.)
- **R-W1-02 [MAJOR] `Runtime.build` threading untested.**
  All 5 runtime tests call `_build_memory_creator(...)` directly. Nothing
  asserts `Runtime.build(...)` with backends enabled yields a creator with
  non-None `_sink`, or that the sink wraps the *same* store/embedder pair
  `_try_vector` built. A dropped kwarg at `runtime.py:334` survives.
  Fix: one `Runtime.build` integration test (fake/monkeypatched `_try_vector`
  or optional backends enabled) asserting `memory_creator._sink is not None`
  and isinstance `VectorEmbeddingSink`; existing degrade tests cover the
  disabled case only at helper level.
- **R-W1-03 [MAJOR] Fakes ignore `collection`/vector/texts — wrong-collection
  and wrong-vector bugs survive.** `_FakeVectorStore.upsert/search/count`
  in `tests/memory/test_creator.py:261-274` and `_FakeEmbedder.embed`
  discard their arguments. `test_wired_sink_indexes_and_marks_indexed`
  claims "one point in `active_facts`" but never checks the collection.
  Fix: record `collection` in the fake and assert `"active_facts"`; record
  embedder input texts and assert they equal the facts' `statement`s.
  Cheap, high-value — closes M5 + M12.
- **R-W1-04 [MINOR] `limit` and partial-return drain semantics untested.**
  `drain_pending_embeddings(limit=100)` with >1 pending, `limit<=0`
  (uncovered `creator.py:1236`), sink returning a subset/empty list.
  The "drained, not paginated" claim in the docstring is unproven.
- **R-W1-05 [MINOR] Boundary/error-path gaps:** real `VectorEmbeddingSink`
  failing at embed vs upsert vs import; API/CLI drain-failure isolation
  (except blocks uncovered); concurrent double drain; multi-fact signal
  through wired sink. Code inspection says each behaves correctly; no test
  pins any of them. See P2 sketches.
- **R-W1-06 [MINOR] Payload ranking keys under-pinned.**
  `test_payload_carries_reader_keys` asserts `>0` only — a swap or constant
  passes. Pin exact `conf`/`imp` from the written fact (or at least
  distinct-value inequality) and assert `_semantic` element's
  `confidence`/`importance` equal the payload's, not the reader defaults
  (0.8/0.5).
- **R-W1-07 [MINOR] Two of 14 pass pre-fix (pinning, not reversal).**
  `test_wired_sink_indexes_and_marks_indexed` and
  `test_degrades_to_none_without_vector_backends` pass on `eb6606e`
  [RAN §0: 12 failed, 2 passed]. Not defects — but their docstrings claim
  reversal ("T01…", degrade matrix) they cannot deliver. Either rename to
  make the pinning intent explicit or fold the wiring claim into a
  `Runtime.build` test (R-W1-02) that *does* fail pre-fix.
- **R-W1-08 [NIT] Implementation-detail assertions.** Tests poke
  `creator._sink`, `_sink._store`, `memory_creator._sink = ...`. Consistent
  with the file's existing style (`_extractor`, `_db`) and there is no public
  accessor, so acceptable — noted only because a rename breaks 8 tests
  without a behaviour change.
- **R-W1-09 [NIT] `_settings()` leaks temp dirs; CLI cache reset on
  success path only.** `tests/runtime/test_memory_creator.py:44-50` uses
  bare `tempfile.mkdtemp()` (pre-existing helper, new tests reuse it);
  `tests/cli/test_memory.py:208` calls `reset_settings_cache()` after the
  assertion instead of in a fixture teardown. Neither flakes today;
  both are traps for the next author.

## 6. Missing edge cases and tests (prioritized) P1/P2/P3 with concrete scenarios

### P1 (add before calling T-001 done)
1. **Daemon drain** (R-W1-01): seed 1 `pending` fact + sink on daemon creator,
   run one ingestion tick, assert `indexed`. Sketch: reuse
   `tests/daemon/test_drain_e2e.py` runtime fixture; set
   `daemon._memory_creator()._sink = _RecordingSink()`; call
   `_ingestion_loop` body once; assert `pending_embeddings() == []`.
2. **`Runtime.build` wiring** (R-W1-02): `Runtime.build(settings,
   enable_optional_backends=True)` with `_try_vector` monkeypatched to
   return (fake store, fake embedder); assert
   `isinstance(rt.memory_creator._sink, VectorEmbeddingSink)`. Fails
   pre-fix (TypeError/None) — a true reversal test for C01.
3. **Backlog + limit** (R-W1-04): write 3 facts sink-less; `drain(limit=2)`
   → returns 2, 1 still pending; `drain(limit=0)`/`-1` → 0, 1 still
   pending; `drain()` → 1. Also 105-fact backlog with default limit →
   100 then 5 (proves drained-not-paginated).
4. **Partial sink return**: sink returns only first id of 2 → returns 1,
   only that id `indexed`, other stays `pending`; sink returns `[]` →
   returns 0, nothing marked. Catches return-value/mark scoping bugs.
5. **Collection assertion** (R-W1-03): fake records collection; after
   `test_wired_sink_indexes...` assert `store.collections == ["active_facts"]`
   (and embedder saw `[fact.statement]`).

### P2 (should add)
6. **Concurrent double drain**: `asyncio.gather(creator.drain_...(),
   creator.drain_...())` over 2 pending → total indexed 2, both `indexed`,
   store point count 2 (no loss). If the substrate races, this is the test
   that finds it.
7. **Real-sink failures**: `VectorEmbeddingSink(store, RaisingEmbedder())`
   and `(RaisingStore, fake)` via `drain_pending_embeddings` → 0, stays
   `pending` (embedder-raise and upsert-raise separately); plus
   `process_signal` write-path with raising embedder → `PROCESSED` +
   `pending` (today only stub `BrokenSink` is used).
8. **Call-site isolation**: make `mark_embedded` raise (monkeypatch
   `FactRepository.mark_embedded`) during API `POST /drain` → still 200
   with batch report; same for CLI → still exit 0 + table printed. Covers
   the uncovered except blocks.
9. **Multi-fact signal through wired sink**: one signal, 2 triples →
   2 points, 2 `indexed`, payload keys on both.
10. **Exact payload values** (R-W1-06): assert
    `payload["confidence"] == fact.confidence` etc. from the DB row, and
    `_semantic` element `confidence/importance` match (not 0.8/0.5 defaults).

### P3 (nice to have)
11. **Unicode/NUL/oversized statement through sink**: facts with emoji,
    CJK, embedded `\x00`, and an 8192-char `object_value` → indexed
    (payload round-trips) — pins no-crash on hostile text. (Extractor caps
    already bound length; this pins the sink/payload leg, which is new.)
12. **Superseded fact not drained**: seed `pending` + `superseded_by` set
    → `drain` leaves it alone (documents the `pending_embeddings` filter
    the drain relies on).
13. **Oldest-first order**: 3 pending facts with distinct `created_at` →
    sink receives them in order (assert `sink.batches[0]` id order).
14. **Unknown-id sink return**: sink returns `["nope"]` → drain returns 1,
    no exception (UPDATE matches 0 rows) — pin the current no-crash
    behaviour explicitly.
15. **Crash-between-upsert-and-mark idempotency**: pre-seed a point for the
    fact in the fake store, leave fact `pending`, drain → still 1 point,
    fact `indexed` (re-upsert by id is safe).

## 7. What I could not verify

- Live embedder/Qdrant/numpy end-to-end (real 384-d vectors, paraphrase
  relevance, `_semantic` ranking over a real store) — fakes only.
  [UNKNOWN] whether `tests/storage/test_vector.py` suite + `HashEmbedder`
  would catch a width/collection regression that these fakes miss.
- Full suite, ruff, mypy (scoped-checks rule). Baseline per 00-mission.md
  (ruff clean, mypy 3 win32, 12 pytest failures) taken as given, not re-run.
- 100x load / real SentenceTransformer latency of drain — no load test run.
- Long-run test (STANDING-ORDERS §8): *Source* — drain writes only derived
  `embedding_status` + points, facts already committed [VERIFIED
  `creator.py:1220-1262`]; *Load* — bounded `LIMIT 100` per call, but
  concurrency never tested (P2-6); *Hostile* — drain input is committed
  rows only, NUL/oversized surface unchanged (P3-11 pins it);
  *Operability* — drain logs pending/indexed counts [VERIFIED
  `creator.py:1239-1261`]; *Reversal* — 12/14 fail on `eb6606e`
  [RAN §0], 2 pinning tests pass by design (R-W1-07).

## Verdict rationale

Core work product is strong: the three real bugs (empty semantic content,
stuck `pending`, drain loss — including API/CLI paths) are caught by
behavioural tests, fail-before was **confirmed in a temp worktree**
(12 failed pre-fix), and the degrade matrix is complete at helper level.
**APPROVE-WITH-CHANGES** because two behaviour changes ship untested
(daemon drain, `Runtime.build` threading — automatic MAJOR) and the fakes
let wrong-collection/wrong-vector bugs through. Add the 5 P1 tests
(~40 lines, sketches above) and the suite proves what the diff claims.
No BLOCKERs; nothing here suggests the implementation itself is wrong —
only that the tests do not yet prove all of it.
