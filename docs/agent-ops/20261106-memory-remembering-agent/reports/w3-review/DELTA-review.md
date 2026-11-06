<!-- ORCH-DIGEST:START -->
agent: W3-DELTA-REV (DELTA) | scope: wave-3 improvement delta 2ab48e3..HEAD (code 7da607f + docs e60b5f2) | verdict: APPROVE
findings: BLOCKER 0 | MAJOR 0 | MINOR 1 | NIT 2
claims_audit: 9 claims checked / 0 unsupported (1 qualified: REG1 discriminator is persisted-signal provenance, not universal real-memory — see R-DELTA-001)
band_aids_found: none
top_3_risks: (1) REG1 mixed provenanced+dust empty-index pass defers dust until index repopulates [accepted, safe direction]; (2) IGNORED signals carry drops only at DEBUG, INFO signal_processed covers PROCESSED only [R-DELTA-002]; (3) ContextVar counters are asyncio-task scoped — no threaded caller exists today [documented]
optimizations: DO-NOW 0 | DO-LATER 1 (INFO on signal_ignored) | NOT-WORTH-IT 1 (structlog augmentation / stacklevel pointing at user code — no single stacklevel reaches user through pydantic)
reversal_check: reasoned-only (implementer supplied both-directions reversals; delta reviewer re-ran post-fix tests, did not re-break code — read-only brief)
confidence: high (148 scoped tests pass, ruff/mypy clean on changed files, probes confirm attribution + first-person parity, docs citations spot-checked against content)
<!-- ORCH-DIGEST:END -->

# Delta review — W3-DELTA-REV (DELTA)

## 0. Method
- Diff range: `git diff 2ab48e3..HEAD -- src/ tests/ docs/` excluding `docs/agent-ops` [RAN git log/diff -> 2 commits 7da607f+e60b5f2, 15 files, 523+/42-].
- Blind pass first over the code diff (curator.py, creator.py, config.py), then implementer report IMPR-01.md.
- COMMS.md read at start; last line read: W3-IMPR-01 CORRECTION (trailers) line.
- Commands run [RAN]: pytest on the 4 changed test files + docs-refs (148 passed); ruff + mypy on the 3 changed src files (clean); python probes for warning attribution and `_first_person_parts`; line-content spot-checks for docs citations.
- Read-only on source; only this report file written.

## 1. Blind-pass findings (before reading implementer report)
- Orphan guard: blanket skip replaced by provenance discriminator — first impression: discriminator must be checked against the real writer, not the test insert.
- `_drops` property backed by `ContextVar` — first impression: need to check `__init__` assignment goes through setter, and that no code path reads another task's counters.
- `stacklevel=1` — first impression: 1 attributes to the `warnings.warn` line itself (paa code), which matches the `paa.*` error filter but is not the user call site; acceptable given pydantic-validator depth variance.
- Floor forwarding: `explicit_prune_floor()` passed positionally as kwarg; need to confirm `None` defers to global inside `DecaySweeper` (verified decay.py:168-177).

## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| REG1: provenance-less dust still sweeps with empty index | Yes [RAN] | `test_orphan_sweep_removes_danglers` (test_curator.py:108) inserts fact without `source_signal_id`, entity_count=0, provenanced=0 -> sweep runs, orphans_removed=1. Passes. |
| REG1: catastrophic case prevented (provenanced facts + empty index -> skip, no wipe) | Yes [RAN] | `test_empty_index_skips_provenanced_facts` (test_curator.py:138) passes; guard at curator.py:355-368 returns before delete. |
| REG1 discriminator: creator writes source_signal_id for every persisted fact | Yes, qualified | creator.py:1431-1438 `add_fact(..., envelope.id if envelope.persisted else None, ...)` — true for persisted signals (default `persisted=True`, creator.py:139; run_batch rows always persisted, :1598). Non-persisted direct callers produce NULL — see R-DELTA-001 (NIT, no production caller found). |
| R-W3-001 stacklevel=1 matches paa.* filter | Yes [RAN probe] | Probe: `MemorySettings(decay_lambda=...)` prints `src/paa/config.py:310: DeprecationWarning`. Test test_decay.py:454 pins filename + error-filter raise. Passes. |
| R-W3-002 floor forwarding reaches sweep; unset = byte-identical | Yes [RAN] | curator.py:132-137 forwards `self._settings.explicit_prune_floor()`; decay.py:168-177: non-None wins, None reads global. `test_injected_settings_floor_reaches_the_sweep` passes. Explicit sweeper still wins via `sweeper or ...` (unchanged precedence). |
| R-W3-003 LLM first-person parity | Yes [RAN + probe] | creator.py:945 `_first_person_parts(" ".join(subject.split()))` + importance; probe: I/We/WE->(user,0.3), Ikea/Weave/User/Acme->(name,0.5). `test_llm_i_subject_lands_on_user_entity_row` passes, incl. same-row rule-path assertion. |
| R-W3-004 ContextVar per-task counters | Yes [RAN] | creator.py:489-506, 862-877 per-instance ContextVar, reset at extract head (518, 894), snapshot is copy (515, 882). Gated concurrent test (test_llm_extractor.py:709, 2-vs-5 nonzero, barrier Event, no timers) passes. |
| R-W3-005 INFO drop counts | Yes for PROCESSED; gap for IGNORED | creator.py:1197-1204 `log.info("memory.signal_processed", dropped=..., drop_counts=as_dict())` — constant 8-key shape (as_dict :228-239). IGNORED path (1171) logs only `log.debug` + `_log_drops` debug (1704). See R-DELTA-002 (MINOR follow-up). |
| BatchReport exact-set unchanged; ProcessingResult exact-set incl. additive keys | Yes [RAN] | test_creator.py:951 BatchReport summary == 7 keys (no dropped); test_creator.py:919 ProcessingResult summary == 8 keys incl. dropped/drop_counts. Pass. |
| Docs: 18 citations re-pointed to matching content | Yes (spot-checked) | [RAN probe] creator.py:455 class RuleBasedExtractor, :1034 VectorEmbeddingSink, :1133 process_signal, :1216 run_batch, :1273 _claim, :1305 _resolve, :1400 _commit, :1198 signal_processed; config.py:42 _default_home, :56 StorageSettings, :59 SPEC DEVIATION, :420 peak-RSS, :630 queue fields, :708 _enforce_loopback — all match citing sentences. `test_docs_references.py` 5 passed in scoped run. Line-neutral stacklevel edit confirmed (2 lines changed, 0 added) so read-only docs citations below config.py:310 unaffected. |

## 3. Long-run test (independent answers)
- Source: Fix is at the source for each finding (guard predicate, constructor forwarding, extractor seam, counter storage) — no call-site patches, no new flags, no swallowed errors.
- Load (100x, concurrency, partial failure): Guard adds one `COUNT(*)` only when entity index is empty (curator.py:356-362) — no hot-path cost. ContextVar is O(1) per drop, one extra object per extract. Concurrent drains: gated test proves separation under forced interleaving; no locks added, LLM throughput unchanged. Partial failure: malformed LLM response path unchanged (malformed_skipped increment, then quarantine).
- Hostile input: LLM subject goes through split-join + first-token check — hostile subjects (empty, NUL) are rejected before this seam (927-935, `_reject_suspicious` 895). `_first_person_parts` on weird tokens returns passthrough (probe). Floor NaN rejected by existing chained comparison (test_decay.py:421).
- Operability: Orphan skip carries `facts=` count at WARNING (364-366) — observable. INFO dropped/drop_counts on every PROCESSED signal; IGNORED drops remain DEBUG-only (R-DELTA-002). Rollback = revert 2 commits; no schema/migration involved. Reversal test: which test fails if reverted? REG1 pair (either direction), attribution test, injected-floor test, LLM parity test, concurrent test — all present and passing post-fix.
- Reversal: reasoned-only (read-only brief; implementer reversals accepted as evidence, post-fix state re-run green).

## 4. Findings
### R-DELTA-001 [NIT] REG1 discriminator covers persisted-signal facts, not non-persisted ones
- Location: `src/paa/memory/curator.py:355-368` + `src/paa/memory/creator.py:1438`
- Evidence: `envelope.id if envelope.persisted else None` — a fact written via a non-persisted envelope carries NULL provenance and reads as import dust. Combined with an empty entity index it would be swept. In practice the same transaction creates the entity row, so an empty index still requires a separate wipe; no production caller sets `persisted=False` (run_batch rows are always persisted, :1598; default True, :139). The implementer's ON DELETE SET NULL tradeoff is documented in code; this smaller case is not.
- Why it matters: Low — misclassification needs index wipe + non-persisted provenance together; next pass after repopulation is the recovery either way.
- Recommended fix (follow-up, not blocking): one comment line noting the persisted-only scope of the discriminator.

### R-DELTA-002 [MINOR] INFO drop counts cover PROCESSED signals only; IGNORED drops stay DEBUG-only
- Location: `src/paa/memory/creator.py:1169-1174` vs `:1197-1204`
- Evidence: A signal whose candidates are all dropped returns IGNORED with only `log.debug("memory.signal_ignored")` and `_log_drops` at DEBUG (:1704). The drop-heaviest outcome (everything discarded) is thus invisible at the default INFO level, while PROCESSED signals now carry `dropped`/`drop_counts` at INFO.
- Why it matters: C05's purpose is making silent forgetting observable; the all-dropped case is the purest form of it. Not a new defect (pre-existing structure), and `ProcessingResult.drop_counts` still carries the data programmatically.
- Recommended fix (follow-up): add `dropped`/`drop_counts` to the `signal_ignored` debug line or promote a counters-only INFO line for IGNORED-with-drops. DO-LATER.

### R-DELTA-003 [NIT] stacklevel=1 attributes the warning to paa code, not the user call site
- Location: `src/paa/config.py:316,330`
- Evidence: Probe shows attribution to `config.py:310`. That is exactly what the `error::DeprecationWarning:paa.*` gate needs, and no single stacklevel reliably reaches user code through pydantic's validator machinery (depth varies by construction path). Tradeoff is sound and documented in code; noting only that operators see paa-internal attribution, and default filters ignore DeprecationWarning outside `__main__` regardless — visibility still depends on `-W always` or the error filter.
- Recommended fix: none required. If operator visibility matters later, surface via `paa doctor` (as IMPR-01 §4 already flags).

## 5. Attempts to break it (concrete inputs/interleavings)
- REG1 mixed case (provenanced + dust, empty index): guard returns early, dust deferred until index repopulates. Safe direction (prevents wipe), dust is unreachable noise — accepted, not immortal. [reasoned]
- REG1 with `dry_run=True` and empty index: same early return, `orphans_removed=0` — consistent with catastrophic-prevention (dry-run preview also refuses to preview a wipe). [reasoned]
- REG1 TOCTOU: entity_count read, then provenanced count, then delete — a concurrent index rebuild between reads could still delete inside a repopulating index. Pre-existing pattern (guard existed before), not introduced by this delta; curator passes are single-writer in practice. [reasoned]
- First-person: `"I am"` multi-word LLM subject -> first token `i` maps to user (parity with rule path's documented first-word check, creator.py:1730-1746). `"we"` maps (probe). Contraction `"I'm"` unmapped, pinned by test — deliberate non-change.
- ContextVar fork: task created after parent materialised counters copies the object reference, but every `extract()` head assigns a fresh object in the child's context — parent/sibling invisible. `drop_counts` read from a never-extracted task materialises its own zeros (creator.py:497-502) rather than leaking another task's counts. [reasoned + gated test]
- Floor: injected `MemorySettings(prune_confidence_floor=0.0)` -> explicit 0.0 forwarded (not None), sweeper keeps everything. Covered by existing explicit-zero test at sweeper level; forwarding is a straight pass-through of `explicit_prune_floor()`. [reasoned]

## 6. Dimension checklist
- Correctness: pass — all 9 verified claims hold; discriminator, forwarding, parity, counters, INFO counts, exact-sets all match code+tests.
- Root cause vs symptom: pass — predicate refined at guard, floor forwarded at construction, mapping applied at extractor seam, counters moved to per-task storage.
- Regression/compat: pass — `FactExtractor.extract` signature unchanged; BatchReport summary key set unchanged (exact-pinned); `BatchReportView` unpacking unaffected; default deployments byte-identical (None floor defers to global).
- Concurrency/idempotency: pass for asyncio (gated test); threaded callers absent (grep: drains are async) — documented scope, no new hazard.
- Error handling: pass — no new swallow; malformed/negated paths unchanged.
- Security/abuse: pass — no new LIKE/SQL/format; floor NaN/out-of-range still rejected; no secrets touched.
- Performance/scalability: pass — one extra COUNT only on empty index; O(1) counter ops.
- Migration/rollback: pass — no schema change; revert = 2 commits.
- Observability: pass with R-DELTA-002 noted (IGNORED INFO gap, follow-up).
- Maintainability/conventions: pass — long why-comments match house style; line-neutral config edit protects read-only docs citations; ruff/mypy clean on changed files.
- Docs impact: pass — citations content-verified (spot-check + docs-refs suite green).

## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | creator.py:1171 signal_ignored | Drops invisible at INFO on all-dropped signals | Operator visibility of silent forgetting | Small | Low | DO-LATER (R-DELTA-002) |
| 2 | config.py stacklevel pointing at user code via structlog augmentation | Needs added lines in config.py, shifts read-only docs citations; depth varies by path | Marginal visibility gain | Medium | Citation churn | NOT-WORTH-IT (agree with IMPR-01 §4) |

## 8. Cross-agent / cross-task interactions observed
- The line-neutral constraint on config.py (protecting read-only docs citations) correctly shaped the R-W3-001 fix; docs commit e60b5f2 re-points 18 citations created by W3-IMPL-05/06 shifts. No cross-agent conflicts in the delta diff.
- COMMS trailing line (W3-IMPR-01 CORRECTION) notes both delta commits omit brief-required trailers — process note only, SHAs recorded in IMPR-01 digest.

## 9. What I could not verify
- Full `tests/memory/` suite (204 tests per IMPR-01): NOT-RUN in full — standing orders limit to scoped files. Delta-relevant 4 files + docs-refs = 148 passed [RAN]. Full-suite ruff/mypy gates: NOT-RUN (scoped ruff/mypy on changed files clean instead). Baseline (mypy 3, pytest 12 pre-existing per 00-mission.md) untouched by this delta — no files overlapping those areas.
- Reversal runs (both directions) relied on implementer's reported evidence — read-only brief, reasoned-only.
- No live log-capture assertion for INFO `signal_processed` (implementer §6.5 honest gap) — verified by reading the log call site, not by captured output.

## 10. Commands run and results (scoped)
- `.venv\Scripts\python.exe -m pytest tests/memory/test_curator.py tests/memory/test_llm_extractor.py tests/memory/test_decay.py tests/memory/test_creator.py tests/conventions/test_docs_references.py` -> `148 passed in 18.02s`, EXIT 0 [RAN]
- `.venv\Scripts\python.exe -m ruff check src/paa/memory/curator.py src/paa/memory/creator.py src/paa/config.py tests/memory/test_curator.py tests/memory/test_llm_extractor.py tests/memory/test_decay.py tests/memory/test_creator.py` -> `All checks passed!` [RAN]
- `.venv\Scripts\python.exe -m mypy src/paa/memory/curator.py src/paa/memory/creator.py src/paa/config.py` -> `Success: no issues found in 3 source files` [RAN]
- Probes [RAN]: `_first_person_parts` mapping table (I/We/WE->user@0.3; others passthrough@0.5); warning attribution to `config.py:310`; citation line-content reads for 10 creator.py + 6 config.py lines — all match citing sentences.
