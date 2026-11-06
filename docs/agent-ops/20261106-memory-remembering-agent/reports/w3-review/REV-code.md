<!-- ORCH-DIGEST:START -->
agent: W3-REV-01 | scope: Wave 3 (T-005 C05 extractor counters + pronoun mapping; T-006 C06 explicit-floor + decay_lambda deprecation) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 0 | MINOR 4 | NIT 2
claims_audit: 20 claims checked / 3 unsupported-or-partial (1 false: "DeprecationWarning fails loudly in tests" — attributed to pydantic, never matches paa.* filter)
band_aids_found: none (warn-and-ignore is briefed behaviour for a dead field; getattr probe is a tested compat shim, not error swallowing)
top_3_risks: (1) deprecation signal is silent in production — attributed to pydantic/main.py so the write is invisible to operators and the gate; (2) LLM path still creates `I`/`we` entities instead of `user`; (3) injected-curator settings floor bypasses the sweep (global-only read)
optimizations: DO-NOW 2 | DO-LATER 3 | NOT-WORTH-IT 3
reversal_check: reasoned-only (read-only review: reversal runs not re-executed; see §9)
confidence: high (blind probes + scoped tests run; known gate regressions excluded per brief)
<!-- ORCH-DIGEST:END -->

# Code review — W3-REV-01 (wave 3)

## 0. Method
- Diff range: `git diff 38cda61..HEAD` over `creator.py, config.py, decay.py, test_creator.py, test_decay.py, test_llm_extractor.py` (commits 648f979, f4d0ca8) [RAN shell diff].
- Surrounding code read: `creator.py` (extractor classes, `process_signal`, `run_batch`, `_first_person_mapping`, `_is_storable`, `_resolve_entities`), `config.py` (`MemorySettings`, `Settings`, `get_settings`), `decay.py`, `domains.py` (DOMAINS), `curator.py`, `daemon.py`, `api/app.py`, `api/schemas.py`, `cli/main.py`, `facts.py` (`resolve_entity`, `upsert_entity`), `pyproject.toml:103`.
- Order followed: blind pass on diff + probes first, then read implementer reports (§2 claims audit last).
- Commands run [RAN]: scoped pytest `test_creator + test_decay + test_llm_extractor` → 123 passed (44+53+26), zero failures; `ruff check` on the six touched files → All checks passed; fresh-interpreter `import paa.memory.decay` → OK; DOMAINS↔MemoryDomain parity probe → True (18==18); extractor probes (I/we/Ikea/Weave/object-`I`/auxiliary forms); settings probes (env var, `.env` file, kwargs, `model_validate`, explicit `0.15` via env); warning-attribution/filter probes (filename `pydantic/main.py`, error-filter replication); NaN comparison semantics.
- NOT run (scoped-only rule + read-only): full `pytest tests/`, full `mypy src`, reversal runs (no tree mutation), concurrent-drain load test.
- COMMS read at start through line 46 ([2026-11-06T19:24:24Z] CORRECTION); no new COMMS lines needed (no NEED/FINDING outside this report).

## 1. Blind-pass findings (before reading implementer reports)
T-005: counters are pre-candidate only + creator-side `non_storable` (disjoint by construction); `BatchReport.summary()` frozen (safe for `BatchReportView(**summary)` + CLI key readers). First-word mapping works on the natural corpus (`I am …`) and does NOT fire on `Ikea`/`Weave`/`Acme owns I` [RAN]. Gaps found without reports: (a) LLM path never maps first-person and never gets the counters' other classes; (b) per-signal drop line is `log.debug` only, and `signal_processed` carries no drop counts; (c) single-flight counter invariant races under concurrent `/memory/drain` on a shared creator (counters only, facts safe).
T-006: explicitness via `model_fields_set` is sound on env var, `.env`, kwargs and `model_validate` [RAN — the implementer's least-confident angle confirms clean]. DOMAINS values untouched [RAN parity True]. Gaps found: (a) `stacklevel=2` attributes the `DeprecationWarning` to `pydantic/main.py:280`, so the gate's `error::DeprecationWarning:paa.*` never matches AND Python's default filters ignore it — silent in tests and production; (b) the floor read is global-only: `MemoryCurator`'s injected `settings` never reaches its default sweeper; (c) the constructor-settings read now also fires on `touch_many`-only constructions (4 recall call sites).

## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| T-005: 18 tests added; scoped 70 passed | YES | [RAN] 44 creator + 26 LLM = 70 (123 with decay), matches test counts in diff |
| T-005: Protocol unchanged; result/batch fields additive | YES | [VERIFIED] `FactExtractor.extract` signature identical; `BatchReport.summary()` key set frozen + test pins it |
| T-005: exact-match `I/we` maps ~none of the natural corpus (`"I am"` subject) | YES | [VERIFIED] regex absorbs auxiliaries; [RAN] `"I am based in Berlin"` → subject `I am` → `user` |
| T-005: `"I"` inside `object_value` cannot false-fire | YES | [RAN] `"Acme owns I"` → `no_match` (object min 2 chars), no `user` |
| T-005: `Ikea`/`Weave` do not map | YES | [RAN] both stay full-word entities @0.5 |
| T-005: `non_storable` disjoint from extractor counters (no double-count) | YES | [VERIFIED] extractor never emits noise-object candidates on triple path; creator adds the count; test pins disjointness |
| T-005: LLM counters only for malformed skips; protocol-compat zero fallback | YES | [VERIFIED] code + `test_extractor_without_drop_counts_yields_zero` |
| T-005: "no concurrent `process_signal` caller exists / drains are sequential" | PARTIAL | `run_batch`, CLI, daemon loops are sequential, but `POST /memory/drain` can be invoked concurrently against the shared `runtime.memory_creator` (api/app.py:273-285). Rule path is await-free so safe; LLM path can interleave the shared `self._drops`. Counters-only impact, but the claim as stated is overstated. See R-W3-004 |
| T-005: reversal 17/18 fail without fix | NOT RE-RUN | Read-only review; not re-executed. Code+tests consistent (failures described match entity/counter names). Reasoned only (§9) |
| T-005: ruff clean; mypy baseline 3 | PARTIAL | Scoped ruff [RAN] clean; full mypy NOT run by reviewer (scoped rule), implementer-reported baseline |
| T-006: 16 tests; 53/53 decay pass | YES | [RAN] 53 passed; 4+7+5 test classes match |
| T-006: `model_fields_set` sound for env var AND kwargs AND `.env` | YES (reviewer-closed) | [RAN] `.env` file probe: `model_fields_set == {'decay_lambda','prune_confidence_floor'}`, floor 0.33 honored, unknown key dropped with both warnings; env `0.15` and `model_validate` also mark explicit. Implementer's §15 least-confident is CONFIRMED OK |
| T-006: "explicit use fails loudly in tests" via `filterwarnings error::DeprecationWarning:paa.*` | FALSE | [RAN] warning is attributed to `pydantic/main.py:280` (`stacklevel=2` from inside a pydantic validator); replicated filter `error::DeprecationWarning:paa\..*` does NOT raise, and default filters ignore it (count 0). So the deprecation is silent in BOTH test suite and production, contrary to §4's claim. Root cause + fix in R-W3-001 |
| T-006: "operator-visible via warnings/`paa doctor`" | PARTIAL | Same mechanism: only visible with `-W always/error`. Implementer caveat §13 acknowledges hiding; root cause (attribution) not identified — covered by R-W3-001 |
| T-006: NaN/out-of-range constructor rejected | CODE YES / TEST NO | `0.0 <= nan <= 1.0` is False → `ValueError` [RAN semantics], but no NaN test exists (grep for nan: none). NIT R-W3-006 |
| T-006: sweep signature unchanged; DOMAINS values frozen | YES | [VERIFIED] diff shows no `domains.py` change; [RAN] DOMAINS↔enum parity `True` (18/18) |
| T-006: import-cycle smoke OK | YES | [RAN] fresh `import paa.memory.decay` OK; lazy import sits behind construction, not module load |
| T-006: daemon per-tick sweeper → snapshot floor safe | YES | [VERIFIED] daemon constructs curator per tick (`_run_curation_tick`); fallback sweeper also per-boot |
| T-006: curator injected-settings forwarding deferred (out of write-set) | CONFIRMED GAP | [VERIFIED] `curator.py:126` builds sweeper without floor; sweeper reads global `get_settings()`. Prod-equivalent only because runtime settings default to the global singleton. R-W3-002 |
| T-006: reversal 13F/3P pre-change | NOT RE-RUN | Read-only; described failure modes match code (missing attr/kwarg, no warning, undropped key). Reasoned only |
| Both: docs-refs/rouff gate failures are sibling/own NEEDs | CONFIRMED | Out of this review's scope per brief (R-W3-REG1/R-W3-DOCS triaged to improver); not re-reported |

Claims checked: 20 (10 T-005 + 10 T-006) / unsupported-or-partial: 3 (1 FALSE, 2 PARTIAL). Tally includes the rows above as counted claims.

## 3. Long-run test (independent answers)
- **Source:** Counters are a pure function of payload at 8 increments/drop, reset at `extract()` head, snapshotted synchronously before any `await` in the sequential path [VERIFIED creator.py:483,1104-1106]. First-person mapping is first-token + case-fold at all 4 rule candidate sites (sentence/triples/structured/KV) [VERIFIED]. Floor is snapshotted at construction (O(1)/row) [VERIFIED decay.py:167-177,242-244].
- **Load (100x):** 10k hedged lines → `hedges=10000`, one DEBUG line per signal, no per-line spam, constant-size copies [re-verified by inspection; bounded line count]. Sweep stays keyset-paginated; floor adds one attribute read. Concurrent `/memory/drain` x2 races only counters on the LLM path (see R-W3-004).
- **Hostile input:** containment (`_reject_suspicious`, `_unparseable_guard`, caps) runs BEFORE counting; NUL/control/oversized payloads still raise and never reach the mapping [VERIFIED]. `I` in object position never maps [RAN]. Log line carries ids + counters only (no payload content) [VERIFIED]. Malformed env floor fails fast at settings validation (by design, DA), NaN rejected by chained comparison [RAN semantics].
- **Operability:** `result.drop_counts` + `summary()["dropped"]` + `BatchReport.{dropped,drop_counts}` + `memory.batch_completed` INFO line are the visible surface. Weak spots: per-signal line is DEBUG-only and `signal_processed` lacks counts (R-W3-005); deprecation warning never surfaces (R-W3-001); injected-curator floor not honored (R-W3-002). Rollback = revert commit (no migration); facts written as `user` remain valid; counters unpersisted.
- **Reversal:** reasoned-only. Removing `_first_person_mapping` breaks `test_i_am_based_writes_user_fact` (entity `I am`), `test_we_subject_maps_to_user`, uppercase and triple variants; removing counters zeros all `drop_counts` assertions; removing the decay threshold line breaks the explicit-floor tests while settings/deprecation tests still pass. Tests read only public fields (never `_drops`) [VERIFIED in tests].

## 4. Findings
### R-W3-001 [MINOR] Deprecation warning is silent in production AND below the error filter (stacklevel attribution)
- Location: `src/paa/config.py:310-317`. Evidence [RAN]: `warnings.warn(..., stacklevel=2)` inside `@model_validator(mode="after")` fires from pydantic's validation machinery; recorded warning filename is `pydantic\main.py, 280`, not `paa.*`. Replicating `error::DeprecationWarning:paa\..*` → NO error; Python default filters → warning ignored (count 0).
- Why: T07's warn-on-use was meant to be visible. Operators with stale `PAA_MEMORY__DECAY_LAMBDA` in `.env` never learn the value is ignored; the coverage the gate gets is `pytest.warns` only, and explicit use without asserting the warning would NOT fail the `error::paa.*` gate. The implementer's §4 claim ("fails loudly in tests") is false; §8/§13 partially concede visibility but misattribute the cause to default filters only.
- Recommended fix (root cause): make the warning actually visible — replace/augment the bare `warnings.warn` with a structlog `log.warning` at validation (surfaced at INFO in production, greppable) and either raise `stacklevel` so the warning lands on user code (verify per ingress: kwargs, `Settings()` from env) or emit under a category `paa` owns so the gate matches. Add a regression test asserting `warning.filename` is inside `src/paa/` (or that the recorded warning survives `simplefilter("error", DeprecationWarning)`).

### R-W3-002 [MINOR] `MemoryCurator`'s injected `settings` never reach the sweep floor (global-only read)
- Location: `src/paa/memory/curator.py:122-128`, `src/paa/memory/decay.py:175-177`. Evidence [VERIFIED]: curator does `sweeper or DecaySweeper(db, archive_writer, batch_size)` — its own `self._settings` is unused for the sweep, and the sweeper lazily reads the **global** `get_settings()` singleton. The daemon passes `settings=self._rt.settings.memory` (daemon.py:242-250); that coincides with the global only because `runtime.py:284` defaults to `get_settings()`. Any caller injecting non-global `MemorySettings` (tests, coder agent, future second settings) gets a floor silently from the wrong source.
- Why: same class of wiring gap C06 set out to close — settings object present but unwired at one construction site.
- Recommended fix: in `MemoryCurator.__init__`, forward `self._settings.explicit_prune_floor()` as the constructor arg when building the default sweeper (constructor arg wins over global read by design). Test: curator with `MemorySettings(prune_confidence_floor=0.5)` evicts a 0.4 semantic fact while global env is unset.

### R-W3-003 [MINOR] LLM extractor still creates `I`/`we` entities instead of `user` (cross-extractor inconsistency)
- Location: `src/paa/memory/creator.py:887-900` vs `:1668-1694`. Evidence [RAN]: LLM triple `{"subject":"I",...}` → `entity_name="I" @0.5` with zero counter impact. The same signal through the rule path creates `user` @0.3. At entity resolution, `I` fuzzy lookups will not reliably land on `user`, so enabling `extraction_model` forks first-person memory into a second entity and splits contradiction/dedup behavior.
- Why: the plan scoped C05 to the rule extractor and the implementer disclosed this as out-of-scope #2 — acceptable in-wave, but it is a behavior fork any operator enabling LLM extraction will hit immediately.
- Recommended fix (root cause): normalize at the `process_signal` seam — apply `_first_person_parts(candidate.entity_name)` to every candidate the creator accepts regardless of extractor (or inside `LLMFactExtractor.extract`). Test: LLM `I` triple → `user` @0.3 and the fact lands on the same entity row as a rule-path first-person fact.

### R-W3-004 [MINOR] Concurrent `/memory/drain` can interleave the shared extractor's drop counters (facts unaffected)
- Location: `src/paa/memory/creator.py:467-483,823-841,1100-1104`, `src/paa/api/app.py:273-285`. Evidence [VERIFIED]: the single-flight docstring claims no concurrent caller; the rule extractor has no `await` inside `extract()` (safe), but the LLM extractor awaits the model mid-`extract()` while holding the shared `self._drops`. Two concurrent `run_batch` calls on the shared `runtime.memory_creator` reset/snapshot each other's counters. Impact is miscounted drops only — candidates and facts are per-call locals, so nothing is written twice or lost.
- Why: the in-code invariant is documented but the claim "no such caller exists" is false for the API path; operators diagnosing drops under concurrent drains would get wrong numbers.
- Recommended fix: make the invariant true, not the claim — either serialize drains (existing backpressure gate already exists at app.py:275; extend it or lock `run_batch` per creator), or have `MemoryCreator` instantiate/use a per-call extractor copy. Do not "fix" by adding locking inside counter mutation alone (the reset/snapshot pair must become atomic across the await). At minimum, correct the docstring to the counters-only impact.

### R-W3-005 [MINOR] Drop counts are DEBUG-only per signal; `signal_processed` carries no drop fields
- Location: `src/paa/memory/creator.py:1639-1648,1137-1150`. Evidence [VERIFIED]: `_log_drops` emits `log.debug("memory.extraction_dropped", ...)` (invisible at default INFO), and the PROCESSED info line only logs `facts=`/`entities=`. A signal that stores 3 facts and drops 5 lines has no INFO-level trace of the drops — undercutting C05's purpose (making silent forgetting visible) for the common case. The batch-completion INFO line does include aggregated drops, but only per batch, at INFO, and batched away from the signal.
- Recommended fix: add `dropped` + `drop_counts` kwargs to the existing `memory.signal_processed` info line (cost: constant-size dict, only when nonzero), or promote the per-signal line to INFO when total > 0. No new log line needed.

### R-W3-006 [NIT] NaN floor rejection is unpinned by a test
- Location: `src/paa/memory/decay.py:168-173`. `0.0 <= nan <= 1.0` is False → `ValueError` [RAN semantics], and the sim report's hostile-input section claims NaN coverage, but `tests/memory/test_decay.py` has no NaN case (grep: no match). One-case addition to `test_explicit_floor_out_of_range_rejected`.

### R-W3-007 [NIT] `user` entity merges with any literal `user` entity; importance uses max() semantics
- Location: `src/paa/memory/creator.py:1668-1694`, `facts.py:358,387`. Evidence [VERIFIED]: the name is plan-mandated, and `upsert_entity` is idempotent on `canonical_name` with importance taking the maximum, so a literal entity named `user` (system account, test fixture) shares the first-person row — and any other writer storing importance > 0.3 for an entity named `user` raises the effective importance of all first-person facts. Acceptable for the single-user local-first design (the operator *is* the user), but the merge is undocumented and untested.
- Recommended fix: document the reserved `user` entity in `_USER_ENTITY_NAME`'s comment and add a pin test: pre-create an entity named `user`, write a first-person fact, assert one row, importance 0.3. Do NOT rename to `@self` without an operator decision (would fork existing `user` rows).

## 5. Attempts to break it (concrete inputs/interleavings)
1. `"I am based in Berlin"` → `[('user','based_in','Berlin') @0.3]`, drops 0 [RAN via probe + committed test]. Exact case and lowercase `i am …` both map.
2. `"Ikea owns Acme"` / `"Weave owns Acme"` → full-word entities @0.5, `user` NOT created [RAN]. First-word token check, not substring.
3. `"Acme owns I"` → no candidate, `no_match=1`, no mapping (object position never inspects pronouns) [RAN]. Hostile false-fire ruled out.
4. `"You currently own Acme"` (other-pronoun leading word) → parses as entity `You currently` (pre-existing exact-match gap, implementer-disclosed, out of scope) [VERIFIED by code path + report; reasoned, probe was covered by gap class]. Not a C05 regression.
5. `{"facts": [{"subject":"I",...}]}` → `user` (triple path) [committed test; verified via code at _from_triples].
6. LLM `{"subject":"I",...}` → `I` entity @0.5 [RAN] → R-W3-003.
7. Mixed signal `Maybe … Is …green? You owns Acme.` → hedges/questions/pronouns 1/1/1, total 3, single-field attribution [committed test; question-before-hedge branch order verified].
8. `PAA_MEMORY__PRUNE_CONFIDENCE_FLOOR=0.15` via env (equal to default) → explicit override honored (semantic 0.2 fact survives at 0.15 where default 0.30 would evict) [RAN probe on `model_fields_set`; committed test covers 0.15 kwargs; env-0.15 path verified by reviewer probe].
9. Malformed env floor (string/garbage) → settings `ValidationError` at sweeper construction, never swallowed [VERIFIED: decorator raises inside `DecaySweeper.__init__`]; daemon maintenance already wraps in try/except (`daemon.maintenance_failed`).
10. Interleaving: two concurrent `run_batch(limit=N)` over shared creator with LLM extractor → counters can be attributed to the wrong signal (facts correct) [reasoned from await structure, NOT load-tested] → R-W3-004.
11. `.env` containing `DECAY_LAMBDA={"bogus": 2.0}` → load succeeds, bogus dropped, both warnings recorded [RAN dotenv probe]. Warn-and-ignore confirmed honest (value is ignored anyway — the drop is cosmetically redundant but harmless).

## 6. Dimension checklist
- **Correctness:** PASS — counters exact-once per drop (verified disjointness), mapping scope exact (first-token, case-folded, subject-only; probes 1-3), floor semantics exact (`<` threshold, per-domain or explicit replacement, immutable skip, unknown-domain error preserved).
- **Root cause vs symptom:** PASS — the two real causes (no reader for the floor; extractor contract couldn't report drops) are both addressed structurally, not by flag or special-case.
- **Regression/compat:** PASS — `BatchReport.summary()` frozen with test; `FactExtractor` protocol identical; `ProcessingResult` additive; `decay_lambda` parses-and-ignores; unset deployments byte-identical eviction (default `self._prune_floor is None` → per-domain path).
- **Concurrency/idempotency:** MINOR ISSUE (R-W3-004) — sweep is idempotent (derived confidence) and floor snapshot is per-instance; counters race only under concurrent drains on the LLM path with counters-only impact.
- **Error handling:** PASS — fail-fast on malformed floor/NaN/out-of-range; unknown domain rows error-count, never deleted; malformed LLM entries skipped+counted, never fatal.
- **Security/abuse:** PASS — containment before extraction/mapping; no payload in logs; `user` is a constant, not attacker text; no new SQL/LIKE surfaces.
- **Performance/scalability:** PASS — O(1) increments per drop, one log line per signal, constant-size counter copies; sweep floor is O(1)/row inside the existing keyset loop.
- **Migration/rollback:** PASS — no schema change; revert = two code commits; facts/counters are not persisted state.
- **Observability:** MINOR ISSUE (R-W3-001, R-W3-005) — batch INFO aggregates and result objects are good; per-signal DEBUG-only + silent deprecation are the gaps.
- **Maintainability/conventions:** PASS — long why-comments, `class X(str, enum.Enum)` untouched, no new `noqa`/`type: ignore`, lazy-import pattern matches curator/decay precedent, no new `if TYPE_CHECKING:` block (formatting lock untouched).
- **Docs impact:** Gate failures R-W3-DOCS already triaged to improver per brief; no independent docs findings. `explicit_prune_floor`/deprecation field comments are thorough [VERIFIED config.py:221-248].

## 7. Optimizations
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | `config.py` validator warn | R-W3-001: warning attributed to pydantic/main.py, invisible in prod + gate | Operators can migrate stale `.env` | S | S | **DO NOW** |
| 2 | `decay.py` floor snapshot (per-sweep reread of settings) | Snapshot-at-construction is deliberate, invariant-documented; daemon/curator construct per tick | None within current callers | — | M (changes determinism) | NOT WORTH IT |
| 3 | `creator.py` counters into return value (protocol change) | Would atomize counters across concurrent drains | Correct counts under concurrency | L | H (breaks doubles/third-party extractors) | NOT WORTH IT |
| 4 | `creator.py` `signal_processed` info line gains counts | R-W3-005 | Drops visible at default log level | S | S | **DO NOW** |
| 5 | `curator.py` forward `self._settings.explicit_prune_floor()` | R-W3-002 | Injected-curator correctness | S | S (out of this task's write-set; follow-up) | DO LATER |
| 6 | `process_signal` seam first-person normalization for all extractors | R-W3-003 | LLM/rule parity | S | S | DO LATER |
| 7 | `.env`-file explicitness test (chdir + `reset_dotenv_cache`) | Reviewer verified by probe; committed suite only has env-var test | Guards the documented-least-confident path | S | S | DO LATER |
| 8 | Per-line drop logging removal | Already one line per signal; spam shape handled | — | — | — | NOT WORTH IT |

## 8. Cross-agent / cross-task interactions observed
- `daemon.py` fallback (`DecaySweeper(db)` on curator-construction failure) and 4 recall `touch_many` sites now resolve the floor from global settings; with default prod wiring (global == runtime settings), so no behavioral divergence today [VERIFIED runtime.py:284, daemon.py:242-258]. Noted for the curator-forwarding follow-up, not a blocker.
- `memory/__init__.py` was not extended with `ExtractionDropCounts` (outside implementer's write-set) — importable from `paa.memory.creator` only; flagging in case a re-export is desired (NIT, no action now).
- No other `ProcessingResult.summary()` strict consumers found (API/CLI use `BatchReport.summary()`, frozen) [VERIFIED grep].

## 9. What I could not verify
- Reversal runs (17/18, 13F/3P) — read-only: no tree mutation allowed; reasoned from test/implementation consistency only.
- Full `pytest tests/` / `mypy src` gates — scoped-only rule; reviewed the counts my scoped runs produced (123 pass; scoped ruff clean).
- Concurrent-drain counter race — reasoned from the async structure (await between reset and snapshot on the LLM path); not load-tested.
- Orphan-guard regression (R-W3-REG1 / gate-w3-1) — excluded per brief, not re-checked.
- `paa doctor` visibility of the deprecation warning under `-W always/error` — reasoned from warning attribution probe (would show the warning under `-W always`, but only with the pydantic-internal location); not run.
- `.env` + cwd through `paa doctor`/CLI entry — probe covered `Settings()` from a chdir'd dir only.

## 10. Out-of-scope findings (not mine; from briefed angles, no new action)
1. `hot_serving_memory_registry.decay_lambda` column is dormant/seedless (implementer-disclosed, governs a schema hotspot — left untouched, correctly so).
2. First-word pronoun treatment remains first-person-only (`You currently own Acme` pre-existing exact-match gap, implementer-disclosed `W3-IMPL-05 §13.1`).
3. `DOMAINS` and `MemoryDomain` are 1:1 (18/18) [RAN] — no drift risk for the known-key set today; `test_every_domain_has_a_policy` guards it.
