<!-- ORCH-DIGEST:START -->
agent: W3-IMPR-01 | scope: T-005/T-006 wave-3 findings (REG1, DOCS, R-W3-001..007, TREV M1-M4/N1/P1) | status: DONE
findings: FIXED 18 | REJECTED 0 (with evidence) | DEFERRED 10 (P2/P3 per brief §3)
blockers_major_open: none
code_commits: 7da607f608c050703b01cc0fda7a39e50d0b779e | report_commit: pending
tests_added_or_strengthened: 12 added, 3 strengthened (M3 exact-set, M4 isolation, NaN case)
ownership_violations: none
confidence: high (5 behavioral fixes proven by run-reversals in both directions; independent reviewer dispatch advised - nested subagents depth-blocked, see §6)
<!-- ORCH-DIGEST:END -->

# Improvement report — W3-IMPR-01

PLAN:memory-remembering-agent:C05,C06

## 1. Finding-by-finding disposition
| Finding | Severity | Disposition | What I did / why rejected or deferred (with evidence) | Commit |
|---|---|---|---|---|
| R-W3-REG1 orphan guard | BLOCKER-class | FIXED | `src/paa/memory/curator.py::_phase_orphans`: guard now skips only when the entity index is empty AND `COUNT(source_signal_id IS NOT NULL) > 0` (real provenance-carrying memory fronts the empty index → index is the anomaly → skip + warn `facts=` count). Provenance-less dangling facts (FK-off cross-backend import dust; the failing test's insert has no `source_signal_id`) are swept as the phase intends. Discriminator verified: `creator._commit` writes `envelope.id` as `source_signal_id` for every persisted ingestion [VERIFIED creator.py:1376 pre-edit], so real memory always carries it. Accepted tradeoff documented in code: `source_signal_id` NULLs after `ON DELETE SET NULL` (signal pruned) — needs an index wipe *and* signal prune together to misclassify, and the next pass after the index repopulates still sweeps it. Two-direction reversal proven (§3). | code |
| R-W3-DOCS citations | MAJOR | FIXED | 18 citations re-pointed (not merely re-numbered — each target read and matched to sentence content): 7 config.py (overview:195→`:59` SPEC-DEVIATION backends; overview:202→`:420` peak-RSS comment; persistence:19→`:56` StorageSettings; workflow:23→`:42` _default_home; integrations:126→`:630` queue fields; cli:50→`:708` _enforce_loopback; bg-jobs:102→`:630`) + 11 creator.py lines my own edits shifted (Plan1:66→`1133-1212` process_signal; 95→`:455` RuleBasedExtractor; 96→`1305-1451` resolve→commit; 117→`:321` FactExtractor protocol decorator; 198→`1216-1271` run_batch; 201→`:1034` VectorEmbeddingSink; 203→`1273-1300` _claim; 214→`1400-1451` _commit; 230→`:1198,1181,1652` signal logs; persistence:102→`:1453` _enqueue_embeddings). Pre-existing content drift found and corrected by reading: Plan1:645 was the `_LLM_CONFIDENCE` comment, never `VectorEmbeddingSink`; persistence `creator.py:655` pointed inside `_from_text`, not embedding. Conventions green (§5). | docs |
| R-W3-001 stacklevel | MINOR | FIXED | `src/paa/config.py:316,330`: `stacklevel=2` → `1` at both `warnings.warn` sites (validator + unknown-keys). Root cause [RAN probe]: level 2 inside a pydantic validator attributes to `pydantic/main.py:280`, so the gate's `error::DeprecationWarning:paa.*` never matches and Python defaults ignore it — silent everywhere. Level 1 attributes to `src/paa/config.py:310` [RAN probe post-fix]. Line-neutral change (2 lines touched, 0 lines added) so 11 `config.py` citations in read-only docs (`configuration-and-environment.md`, `memory.md`, overview) do not shift. New test pins filename + error-filter raise (reversal §3). | code |
| R-W3-002 injected floor | MINOR | FIXED | `src/paa/memory/curator.py::__init__`: default sweeper now built with `prune_confidence_floor=self._settings.explicit_prune_floor()`. Constructor arg wins over the global read by decay design; an unset (None) floor still defers to global, so default deployments are byte-identical. Test proves injected 0.5 evicts a 0.4 semantic fact with global env unset (reversal §3). | code |
| R-W3-003 LLM first-person | MINOR | FIXED | `src/paa/memory/creator.py` LLM candidate site: `entity_name, importance = _first_person_parts(" ".join(subject.split()))` + `importance=importance`. Non-first-person names pass through at 0.5 (identical to the default they replace); `I`/`we` → `user` @0.3, same row as the rule path (DB-pinned). Reversal §3. | code |
| R-W3-004 concurrent counters | MINOR | FIXED | Both extractors: `self._drops` (previously one shared mutable object per instance) is now a property backed by a per-instance `ContextVar[ExtractionDropCounts \| None]`. Each asyncio task's reset-at-extract-head is invisible to other tasks, so a model await can no longer interleave reset/increments/snapshot across concurrent drains. Single-flight INVARIANT docs replaced with the per-task invariant (class comments + `process_signal`). Third-party extractors without `drop_counts` untouched (probe fallback unchanged). Deterministic gate test (2-vs-5 nonzero totals, order-independent) + reversal (§3). | code |
| R-W3-005 INFO drop counts | MINOR | FIXED | `memory.signal_processed` now carries `dropped=drop_counts.total` + `drop_counts=drop_counts.as_dict()` (constant-size, unconditional — consistent key shape beats reviewer-suggested conditional; verified in captured run log during reversal: `memory.signal_processed dropped=7 drop_counts={...}` at INFO). `BatchReport.summary()` untouched. | code |
| R-W3-006 NaN | NIT | FIXED | `float("nan")` case added to `test_explicit_floor_out_of_range_rejected` (chained comparison is False for NaN → `ValueError`). | code (test) |
| R-W3-007 user merge | NIT | FIXED | `_USER_ENTITY_NAME` comment now documents the deliberate merge + max-importance consequence + do-not-rename warning; pin test pre-creates `user` @0.8, writes a first-person fact, asserts one row @0.8 with fact importance 0.3. | code (doc) + code (test) |
| M1 / P1-1 | MINOR | FIXED | Valid-LLM-triple → `PROCESSED` + `facts_written==1` + `drop_counts.total==0` in `test_llm_extractor.py` (kills TREV mutation (c) in the new-suite contract instead of relying on pre-existing tests). | code (test) |
| M2 | MINOR | FIXED | First-person mapping tests for the two untested rule paths: key-value (`{"subject":"I","text":"status: active"}` → user) and `_walk` (`{"subject":"I","status":"active"}` → user), extractor-level like their neighbours. | code (test) |
| M3 | MINOR | FIXED | `test_result_summary_surfaces_drop_counts`: `set(summary) >= {...}` → `==` exact set of 8 keys (incl. additive `dropped`/`drop_counts`). `BatchReport.summary()` was already exact-`==` (test_creator.py:868) — verified, no change needed there; the digest claim now holds for both. | code (test) |
| M4 | MINOR | FIXED | `test_unset_env_floor_keeps_per_domain_behavior` now takes `monkeypatch`, calls `monkeypatch.delenv(..., raising=False)` + `reset_settings_cache()` (isolated against a CI env that sets the var; cache reset is extra safety since `delenv` alone leaves a loaded singleton). | code (test) |
| N1 | NIT | FIXED | Contraction pin: `"I'm based in Berlin"` → entity `"I'm"` @0.5, total 0 [RAN probe] — documented as current behavior, deliberately not changed (mapping contractions is a behavior change outside this brief). | code (test) |
| P1-2 | MINOR | FIXED | `"User owns Acme"` → entity `"User"` @0.5, zero drops (first-word map fires only on `i`/`we` tokens). | code (test) |
| P1-3 | MINOR | FIXED | `""` / `"I"` / `"we"` → all IGNORED, totals `[0,1,1]` (bare pronouns = `no_match`×1; empty = nothing), zero entity/fact rows. | code (test) |
| P2-1..5, P3-1..5 | TEST | DEFERRED | Per brief §3 ("defer P2/P3 with reasons"): no risk is left silent — P2-1 pronunciation variants are subsumed by the N1 contraction pin; P2-2 is FIXED as M2; P2-3 (LLM question/hedge counting — LLM extractor counts only malformed skips, classification of question text on the LLM path is genuinely unpinned and worth deciding); P2-4 (.env explicitness — reviewer-probed OK, only a committed test is missing; low value now); P2-5 (multiple λ domains — covered mechanically by the unknown-keys pair); P3-1 (DOMAINS spot values — `test_every_domain_has_a_policy` already guards existence; value edits would be caught by behaviour tests); P3-2 (registry dormancy pin — schema-hotspot-adjacent; behaviour risk is nil); P3-3 = FIXED as M3; P3-4 (NaN-via-env — same validation path as R-W3-006); P3-5 (10k counter saturation — bounds are O(len(payload)), one log line per signal by construction). None blocks the gate; listed for the delta reviewer to triage to a follow-up. | — (report) |

## 2. Code changes (per file, with rationale; root-cause level, no band-aids)
| File | Change | Why |
|---|---|---|
| `src/paa/memory/curator.py` — `__init__` + `_phase_orphans` | Forward `explicit_prune_floor()` into default sweeper; provenance-refined empty-index guard | The guard compared only "index empty?" (one bit) and threw away the bit that distinguishes dust from real memory — provenance. The settings floor had two sources (injected object vs global singleton) with no precedence rule at the construction site. |
| `src/paa/memory/creator.py` — `RuleBasedExtractor` + `LLMFactExtractor` | `_drops` becomes a `ContextVar`-backed property (per-instance var, lazy-materialise getter, setting head at every `extract()`) | Shared mutable counters across tasks were the race; moving them to per-task locals fixes attribution without a protocol change (returns stay bare lists) and without serializing LLM calls (lock alternative rejected — it would cap drain throughput and still needs care at every entry). |
| `src/paa/memory/creator.py` — LLM candidate site | `_first_person_parts` on the subject | The seam was the only place first-person normalization ran; normalizing there too removes the fork at the source instead of special-casing in `process_signal`. |
| `src/paa/memory/creator.py` — `memory.signal_processed` | + `dropped`/`drop_counts` kwargs | C05's purpose is making silent forgetting observable; DEBUG-only per-signal lines are invisible at the default level. |
| `src/paa/memory/creator.py` — `_USER_ENTITY_NAME` comment | Documented merge semantics | The merge was a reviewer-found hidden contract; it is now a stated intent with a do-not-rename guard and a pinned test. |
| `src/paa/config.py` | `stacklevel=2` → `1` ×2 (line-neutral) | See R-W3-001. Structlog augmentation (reviewer suggestion) declined with reason: it needs new lines in `config.py` (e.g. an import or call), which shifts every lower citation in read-only docs I cannot edit; `stacklevel=1` alone satisfies the brief's verify criterion (matches `paa.*` error filter, operator-visible under `-W always`). |

Invariants preserved: `FactExtractor.extract` signature unchanged; `BatchReport.summary()` key set unchanged (exact-pinned by both tests now); counters stay extractor-only + creator `non_storable` (disjoint by construction, still); `DOMAINS` frozen; decay sweep comparison still inlined; no schema/SQL/format change; line-neutral config edit (read-only docs citations below the validator unaffected).

## 3. Test changes (12 added, 3 strengthened; reversal evidence)
Scoping: 4 files — `test_creator.py` 44→50 (+M2×2, N1, P1-2, P1-3, R-W3-007), `test_llm_extractor.py` 26→29 (+M1/P1-1, R-W3-003, R-W3-004), `test_curator.py` 8→10 (+REG1 provenance, +R-W3-002), `test_decay.py` 53→54 (+R-W3-001; NaN/M4 are strengths to existing tests). Full `tests/memory/` = 204 tests.

Run-proven reversals (each reverted by hand, re-run, restored):
- REG1 both directions: guard→no-skip (`if provenanced:`→`if False:`) → `test_empty_index_skips_provenanced_facts` FAILS (dust path passes); guard→blanket (`if True:`) → `test_orphan_sweep_removes_danglers` FAILS. Refined → both pass.
- R-W3-001: both `stacklevel`→2 → attribution test FAILS (filename `pydantic/main.py`); restored → passes.
- R-W3-002: forwarding→`None` → `test_injected_settings_floor_reaches_the_sweep` FAILS (0 evicted); restored → passes.
- R-W3-003: LLM mapping→`(" ".join(subject.split()), 0.5)` → `test_llm_i_subject_lands_on_user_entity_row` FAILS; restored → passes.
- R-W3-004: LLM `_drops`→shared instance attribute → concurrent test FAILS with cumulative totals [5,7] (first run: under a 0-vs-2 assertion it passed [0,2] because the gate-winner increments 0 — caught in self-review, test redesigned to 2-vs-5 nonzero so *either* interleaving order fails; re-run FAILS deterministically, then restored). Note the first attempt's lesson: a nonzero-pair assertion is what makes this test order-independent.
- R-W3-005: verified live in captured output (INFO line carries `dropped`/`drop_counts`); no test asserts the log shape (no precedent for log assertions in this suite) — accepted gap, flagged §6.

Structural / pin tests (pass pre/post by design, documented as pins, not fail-before): N1 contraction, P1-2 `User`, P1-3 IGNORED, R-W3-006 NaN, R-W3-007 max-merge, M3 exact-set (fails on any future key addition), M4 (fails only if the CI env pollutes — that is its purpose), M1/M2 (assert the new paths' behavior; removing the mapping or emptying the LLM extractor fails them by construction). P2/P3 deferrals are listed in §1.

Flake check: `tests/memory/` re-run multiple times (before + after edits + during reversals); the gated concurrent test uses synchronisation (barrier event both callers must reach), never timers; repeated runs green.

## 4. Optimizations applied and evidence (and ones declined, with reason)
- Applied: all of §2 (each review/code finding maps to a root-cause change, not a symptom patch). Counter change is O(1) per drop as before; `ContextVar` adds one object per extract — no throughput cost.
- Declined: reviewer R-W3-001 structlog augmentation — would need added lines in `config.py` and shift read-only docs citations (§2 table); `stacklevel=1` is sufficient per the brief. Note for the delta reviewer: if operator visibility needs more than the warning default filters give, a `paa doctor` surfacing step is the right follow-up (also flagged by W3-IMPL-06 §13).
- Declined: per-call extractor cloning / locking for R-W3-004 (implementation note in §2); counter-in-return protocol change (breaks doubles/third-party extractors — matches reviewer optimization #3 NOT WORTH IT).
- Declined: `.env`-explicitness committed test (P2-4) — reviewer probe already confirmed the path; marginal value.

## 5. Commands run and results (scoped)
- `.venv\Scripts\python.exe -m pytest tests/memory/ -q` → `204 passed in 21.53s` [RAN]
- `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` → `5 passed`, EXIT:0 [RAN]
- `.venv\Scripts\python.exe -m ruff check src tests` → `All checks passed!` [RAN]
- `.venv\Scripts\python.exe -m mypy src` → `3 errors in 1 file` — all pre-existing baseline `src/paa/sandbox/subprocess_backend.py:548` (win32 `killpg/getpgid/SIGKILL`), zero new [RAN]
- Import-order smoke both ways (`import paa.memory.creator, paa.config, paa.memory.curator` / reverse) → OK [RAN]
- Probe rerun (warning attribution + edge corpus) → `WARN DeprecationWarning src/paa/config.py 310`; `"I'm based in Berlin"` → entity `I'm` @0.5 total 0; `"User owns Acme"` → `User` @0.5; `""`/drops 0, `"I"`/`"we"` → no_match=1 [RAN]

## 6. Residual risks and anything the delta reviewer should check
1. **Independent review was NOT run.** The mandatory workflow requires context-gathering + independent review subagents; this session is already depth-1 (`[RAN subagent -> "Subagent depth limit reached (1)"]`), so nested subagents are impossible. I compensated with probe-reproduced root causes, both-directions reversals, and full-diff self-review — but an independent reviewer should re-audit: (a) the provenance discriminator's `ON DELETE SET NULL` caveat; (b) `ContextVar` task-context semantics for any thread-based (non-asyncio) caller of the extractors; (c) the two restructured test helpers' shapes.
2. `ContextVar` scope is asyncio tasks. A hypothetical threaded drain would need `copy_context()` handling — no such caller exists (drains are async; API/CLI/daemon all drive `process_signal` — reviewer confirmed sequential sequential drains + API concurrency, both asyncio).
3. R-W3-005 adds two kwargs unconditionally at INFO — check downstream log parsers don't do strict schema validation on `memory.signal_processed` (grep found no structured consumers; structlog events are schemaless in-tree).
4. The refined guard's extra `COUNT(*)` query runs only when the entity index is empty — no hot-path cost.
5. `test_event_log_shape` does not exist: the INFO `signal_processed` change is asserted only via the summary/result objects; a future strict-log-schema consumer would need this test. Flagging honestly.
6. Out-of-scope discoveries (not touched, per standing orders): same class of shared-mutable diagnostic (`self._drops`) exists in `ScriptedLLMExtractor`-style test doubles? No — test doubles have no counters. `hot_serving_memory_registry.decay_lambda` dormancy remains (W3-IMPL-06 §13, schema hotspot).

## 7. Commits
code: 7da607f608c050703b01cc0fda7a39e50d0b779e | docs+report: pending (this file + 7 docs files, same commit)
