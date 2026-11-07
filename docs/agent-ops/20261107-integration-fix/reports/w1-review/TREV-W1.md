<!-- ORCH-DIGEST:START -->
agent: TREV-W1 | scope: wave 1 / T-014-P014, T-001-P001, T-002-P002 | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 47 (31 config+T13, 2 runtime-redaction, 5 cli-redaction, 9 semantic-derivation)
fail-before/pass-after confirmed: 47 / reasoned: 0 / not-possible: 0
findings: BLOCKER 0 | MAJOR 0 | MINOR 3 | NIT 2
flaky_suspects: none (3 repeat runs green)
missing_edge_cases: P1 1 | P2 3 | P3 2
mutation_survivors: 1 of 14 plausible bugs not caught (partial hydration failure on a subset of hits)
changed_behavior_without_tests: db=None fallback path in _semantic (context_agents.py:272) covered by none of the 9 tests
confidence: high (all 47 tests run green by me; fail-before re-proven against 294fb31 in temp worktrees; regression suites + ruff + mypy re-run)
<!-- ORCH-DIGEST:END -->

# Test quality review — TREV-W1 (independent, read-only; did not write this code or these tests)

## 0. Method (temporary worktree used? repeat counts, coverage tooling)

- Blind pass first: read the full diff (`294fb31..HEAD`, 21 files) and all 4 new test files before opening any implementer report; then read W1-IMPL-01/02/03 reports and audited their claims.
- Ran all 47 new tests myself [RAN `.venv\Scripts\python.exe -m pytest tests/test_config_settings.py tests/runtime/test_redaction_p001.py tests/cli/test_redaction_p001.py tests/agents/test_semantic_derivation_p002.py -p no:cacheprovider -> 47 passed in 1.98s`].
- Fail-before: created a temp git worktree at `294fb31` (never touched the working tree), copied the 4 new test files in, ran with `PYTHONPATH=<worktree>/src` and the main venv. Then a second temp worktree where I added ONLY the two CLI constants (so imports succeed) while keeping the old `[red]…[/red]` print line. Both worktrees removed afterwards.
- Flakiness: 3 additional repeat runs of the 16 P001+P002 tests (all green; no F/E in any run).
- Regression: [RAN `pytest tests/cli/test_memory.py tests/agents/test_context_agents.py tests/memory/test_decay.py tests/context/test_gatherer.py -> 89 passed` (dots, no F/E)] guarding the touched functions.
- Gates on touched files: [RAN `ruff check` on all 4 touched src files + 4 test files -> All checks passed!] [RAN `mypy` on config/runtime/cli/context_agents -> Success, 0 issues].
- No coverage tooling run (no plugin installed); coverage claims below are by inspection of test-vs-diff.

## 1. Per test/suite assessment

### tests/test_config_settings.py (31 tests) — P-014 Settings + T13

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| 8 default tests (allocator/tier/cost_gate/vault_sync/specialists/reflection/drain/submodel-types) | Yes — pinned to plan §K/§E values, not to code internals | Yes (collection ImportError at 294fb31; any default change fails its test) | Yes — `paa_home` fixture + `Settings()` reads env at construct time | No | `test_submodel_types` is shape-locking (isinstance), acceptable as surface pin |
| `test_new_names_in_all` | Yes (export contract) | Yes (ImportError at 294fb31) | Yes | No | Guards the documented `__all__` surface |
| 6 boolean env overrides (parametrized) | Yes — raw value is opposite-of-default, so a pass proves the env var was read | Yes | Yes — monkeypatch.setenv + reset in finally | No | Textbook override proof; a "wire ignored, default wins" bug fails |
| 3 numeric env overrides (max_results/threshold/drain) | Yes | Yes | Yes | No | Same opposite-of-default discipline |
| `test_get_settings_picks_up_env_after_cache_reset` | Yes — behavior of the lru_cache contract w.r.t. reset | Yes | Yes | No | Honestly pins that WITHOUT reset the cache stays stale (line 196) — documents the gotcha instead of hiding it |
| 9 invalid-value raises | Yes — hostile values fail at load (ValidationError), not at use | Yes | Yes | No | Covers 0/negative/non-numeric/bad-bool across three sub-models; boundary 0 vs (0,1) is pinned for threshold (gt=0) |
| T13: 3 tests (set equality, count 12, MUTATES) | Yes for code; **no for the ADR half** — the "ADR table" is a test-local literal (see R-W1-01) | Yes (ImportError at 294fb31 for Settings names; EDGE_TYPES value itself unchanged by this wave — see below) | Yes | No | EDGE_TYPES was NOT changed by this diff; T13 pins pre-existing state against the ADR-0043 claim. Set-equality is byte-exact against the literal |

Fail-before honesty note: T13's 3 tests would have PASSED on `294fb31` had the import of Settings names not failed first (EDGE_TYPES is identical before/after). They are regression pins for a previously phantom-counted claim, not bug-fix tests — that is legitimate, but the report should not present them as fix-verified. The 28 Settings tests genuinely fail at 294fb31 (collection ImportError).

### tests/runtime/test_redaction_p001.py (2 tests) — runtime.py:481

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_secret_shaped_exception_text_is_redacted` | Yes — drives `_build_memory_creator`'s except branch via a raising `_BrokenSink`, asserts captured `memory.embedding_sink` kwargs: `***REDACTED***` present (positive control), no `sk-`/Bearer/`api_key` fragment, event/kwargs contract (`status="degraded"`, sink None) | **Confirmed for the right reason**: at 294fb31 the captured error is the verbatim fixture text (`AssertionError: no redaction marker: 'constructor failed: sk-or-v1-… Bearer tok-… api_key: K42_SECRET_VALUE_9'`) | Yes — tmp_path + monkeypatch; structlog captured by replacing `runtime.log.warning` | No — mocks the failure-injection point (`VectorEmbeddingSink`), not the unit under test; `_FakeDB` minimal | Multi-pattern fixtures (sk-/Bearer/api_key) each matching a `models/base.py:244-246` pattern — wrong-pattern bugs caught |
| `test_benign_exception_text_passes_through` | Yes — guards over-redaction | Passes both ways **by design** (pinning test — same role as CLI 200-boundary) | Yes | No | Correctly documented as pinning, not fix-verified |

### tests/cli/test_redaction_p001.py (5 tests) — cli/main.py drain print

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_secret_shaped_exception_text_is_redacted` | Yes — real `paa memory drain` via CliRunner + real `Runtime.build`, injected raising `drain_pending_embeddings`; asserts marker + no fixture + batch table still printed | **Behaviorally confirmed**: in temp worktree with constants added but old print line, 4F/1P; secret test failed (no REDACTED, raw fixtures) | Yes — own `paa_home` fixture with teardown `reset_settings_cache()` (R-W1-09, leak-proof) | No — real Runtime, only the raise is injected | Whitespace-strip normalization for Rich folding is sound for the single-word bodies chosen |
| `test_boundary_200_chars_no_truncation` | Yes — exact-boundary: 200 chars rendered in full, no marker | Passes both ways by design (pinning) | Yes | No | The 200-side of the boundary pair |
| `test_boundary_201_chars_truncates` | Yes — off-by-one: 201 chars → exactly 200−marker x's + marker, 201st char absent | **Confirmed** (failed on old line: 201 raw chars, no marker) | Yes | No | Pins the marker-inside-200 reading; inversion (`>=` vs `>`) caught either way |
| `test_huge_exception_bounded_and_redacted_before_slice` | Yes — 10 KB + token straddling the slice point; asserts `***REDACTED***` + marker + no token fragment | **Confirmed** (failed on old line: raw `sk-` token in tail) | Yes | No | Also kills the slice-then-redact order mutation (see §3); the `!` separator comment documents the `redact()` greedy-run trap honestly |
| `test_hostile_text_does_not_break_command` | Yes — é, NUL, ANSI, `[/red]` markup attack, secret, 300-char run → exit 0, bounded, marked, table printed | **Confirmed with strongest evidence**: old line exits 1 with `MarkupError: closing tag '[/red]'…` — a crash fix, not just cosmetic | Yes | No | Honestly scopes raw-escape passthrough as out-of-scope (cites FINDING §4 of W1-IMPL-02) rather than asserting sanitization that doesn't exist |

### tests/agents/test_semantic_derivation_p002.py (9 tests) — context_agents.py `_semantic`

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_t02_derived_confidence_and_provenance_and_floor` | Yes — end-to-end (real `NumpyVectorStore` + `VectorEmbeddingSink` + synonym embedder): payload 1.0 provenance asserted before AND after aging, derived value `≈ effective_confidence(...)`, floor drop via real gatherer `_candidate_pool` | **Confirmed** (fails at 294fb31; the 8 semantic tests fail with TypeError on the new `now` kwarg at originals, and with payload-trust values under the implementer's signature-preserving revert) | Yes — injected `now`, function-scoped db/tmp_path, store closed in `finally` | No — real store + real sink; fakes only where a real store is impossible | Oracle shares `effective_confidence` (see §3) — acceptable since decay.py is independently pinned (54 tests, all passing in my regression run) |
| `test_missing_confidence_key_hydrated_value_wins` | Yes — no 0.8/0.5/semantic leak when payload omits keys | Confirmed (0.8 vs 0.223 under revert) | Yes | No | Domain `tool` exercises a different λ — not a one-domain accident |
| `test_hydration_db_failure_degrades_to_payload_snapshot` | Yes — partial failure: DB explodes → payload 0.9 + `context.vector_search_failed` warning + non-empty tier | Confirmed (missing warning under revert) | Yes | No — `fetch_all` replaced only to explode | Would catch a "hydration failure empties tier" regression |
| `test_unknown_domain_passthrough_not_crash` | Yes — hostile domain → initial-value passthrough + warning, no crash, mirrors `_to_fact` | Confirmed (payload 1.0 vs initial 0.9 under revert) | Yes | No | Asserts the *right* passthrough value (initial 0.9), not merely "no crash" |
| `test_idempotent_same_now_same_derived` | Yes — determinism of derivation | Confirmed | Yes | No | Same-`now` twice → identical; catches any caching/memoization of derived values |
| `test_batch_hydrate_one_query_per_search` | Yes — 30 hits → exactly 1 `fetch_all` with `WHERE id IN` | Confirmed (0 vs 1 query under revert) | Yes | Counting wrapper around the real `fetch_all` — not a mock | Directly kills an N+1 "fix" |
| `test_load_100x_hydration_query_count` | Yes — 100 searches × 5 hits → exactly 100 queries, never 500 | Confirmed | Yes | As above | The brief's 100x load case, as a count assertion (deterministic, no timing flake) |
| `test_ranking_uses_derived_not_payload` | Yes — two identical-payload hits order by derived value through real `_rank_key` | Confirmed (1.0 == 1.0 tie under revert) | Yes | No | Proves ranking *truth* moved, not just the emitted number |
| `test_vector_failure_still_returns_empty` | Yes — existing `[]` path unchanged | Passes both ways by design (pinning) | Yes | No | Regression pin, correctly identified |

## 2. Coverage of changed lines/branches, error paths, boundaries

- **runtime.py:481** — both branches of the one changed line covered: secret-shaped exception (redaction) and benign (passthrough). The only other observable (event name/status/degrade-to-None) is asserted. No truncation at this site by design (structlog kwarg, mirrors :474 sibling) — no test asks for a bound here, which matches the spec.
- **cli/main.py drain except** — all branches: ≤200 (no truncation), >200 (truncation + marker), redact-before-slice order, hostile markup/long-text. `len(safe) == 200` boundary pair is explicit (both sides). `console.print` markup=False proven by the MarkupError reversal.
- **context_agents.py `_semantic`** — covered: 0 hits (implicit, via `test_vector_failure...`? no — see gap), hydrated path, payload fallback (total hydration failure), unknown domain, N+1 guard, vector-failure `[]`. **Not covered:** (a) `self._db is None` branch (line 272) — db-less builders exist in production shape (`_hot_facts` guards the same way); (b) partial hydration — some hits hydrated, some rows missing (deleted fact) — per-hit fallback branch (line ~301 `row is None` → else clause) is exercised ONLY by the total-failure test where hydrated={}; mixed hit/return is untested; (c) the `if not hits: return []` empty-hits early return (no-query guard) — untested, trivial but it is the guard that prevents a `WHERE id IN ()` syntax error; (d) `now=None` default (only the degrade test implicitly exercises wall-clock).
- **config.py** — all 8 new fields × (default + env override), full invalid-value matrix for the 3 constrained fields, cache-reset contract. `cost_gate.threshold` invalid matrix includes exactly 0 (pins gt=0). No test pins `PAA_ALLOCATOR__ENABLED` with case variants (`True`/`TRUE`/`1`) — pydantic-settings handles, low risk.
- **creator.py** — comment-only change; nothing to cover. Stated correctly in the report.
- Untested-by-design (not a gap): EDGE_TYPES itself was not modified by this wave; T13 is a state pin.

## 3. Mutation thinking

| Plausible bug | Caught? | By which test |
|---|---|---|
| Remove `redact()` at runtime.py:481 | **Yes — proven** | runtime secret test: verbatim fixtures in captured kwargs at 294fb31 |
| Remove `redact()` at cli drain | **Yes — proven** | cli secret test failed on old print line in temp worktree |
| Remove truncation (`if len(safe) > 200`) | **Yes — proven** | 201-char + huge tests failed on old line (201 raw chars, no marker) |
| Slice-then-redact order swap | **Yes (by inspection + regex verification)** | The straddle test: slice-first would keep the full token (ends at 180 < 200), redact it to 154 chars, **skip truncation, omit the marker** → the `_MARKER_NORMALIZED in tail` assertion fails. Verified `sk-` pattern requires ≥12 chars (`models/base.py:244`) so the partial-token edge is real and the marker assertion is the killing blow |
| Off-by-one on the 200 boundary (`>=` instead of `>`) | **Yes** | 200-exact test (no marker allowed) vs 201 test (marker required) — either inversion fails one side |
| Remove `markup=False` | **Yes — proven** | hostile test: exit 1 MarkupError on old line |
| Wrong defaults in config (e.g. `dry_run=False`, `max_results=3`) | **Yes** | per-flag default tests |
| Env override not read (hardcoded default) | **Yes** | opposite-of-default raw values make a dead wire fail |
| Relax constraint (`ge=1` → `ge=0` etc.) | **Yes** | invalid matrix includes 0 and negatives for all 3 constrained fields |
| EDGE_TYPES +1/−1 member | **Yes** | set-equality + count tests |
| Restore payload trust in `_semantic` (remove hydration) | **Yes — proven** | 8/9 semantic tests fail; implementer's signature-preserving revert gave 7F/2P with payload-trust values (1.0 vs 0.549, 0.8 vs 0.223) |
| Per-hit hydration (N+1) | **Yes** | batch (30 hits → 1 query) + load-100x (→ 100 queries) |
| Hydration failure empties the tier | **Yes** | degrade test asserts non-empty + payload value + warning |
| Wrong resting value on unknown domain (e.g. 0.8 default or crash) | **Yes** | unknown-domain test asserts initial 0.9 exactly |
| Partial hydration failure (1 of N rows missing) silently uses payload defaults for that hit | **NOT CAUGHT** | no test with mixed hydrated/missing hits (see R-W1-02) — code falls to payload branch (line ~301) which is correct, but the branch is only exercised as all-or-nothing |
| `now` ignored (uses wall clock always) | **Yes** | T02/missing-key/idempotency compare against `effective_confidence(..., now=now)` with an aged fact — wall clock would drift from the injected `now` by test runtime, well beyond 1e-6 |
| Missing-row (deleted fact) hit dropped instead of payload fallback | **NOT CAUGHT** | same gap as above (R-W1-02): no hit whose id has no live row |
| db=None builder skips hydration silently (regression: AttributeError) | **NOT CAUGHT** — but this is missing *branch* coverage, not a surviving behavioral mutation of the current code (current code handles it) | see R-W1-02 |
| Docs table and test literal both changed without ADR update | **NOT CAUGHT** | T13 compares code to a test-local literal; nothing parses ADR-0043's table (R-W1-01) |

Survivors: 1 mutation class of 14 (partial/missing-row hydration semantics) + 1 doc-sync class (ADR↔literal drift).

## 4. Flakiness evidence (repeated runs) and test-suite health

- [RAN the 16 P001+P002 tests 3× additional times -> all green each run (16 dots, no F/E)] and the full 47 once (47 passed). No flaky suspects.
- Determinism mechanisms are real, not assumed: injected `now` everywhere derivation is asserted; `_SynonymEmbedder` is hash-based (blake2b, no RNG); 100x load is a query-count assertion, not a timing assertion (no sleep, no threshold).
- Isolation: all new tests are function-scoped; `paa_home` fixtures isolate `PAA_HOME`; the CLI fixture resets the settings cache in teardown; the semantic tests close their `NumpyVectorStore` in `finally`. I re-ran the files in one process (all 47) and in file subsets — no order effects observed.
- Suite health: regression files I ran (cli/test_memory, agents/test_context_agents, memory/test_decay, context/test_gatherer — 89 tests) all green, so the touched modules' neighbors are healthy.
- Docs lock: [RAN `pytest tests/conventions/test_docs_references.py -> test_every_referenced_line_is_not_blank FAILED`] — this is the failure W1-IMPL-01 reported as a FINDING (pre-existing category-C + line-shift breakage from this wave's own config.py/context_agents.py/cli/main.py insertions). It is NOT caused by the new tests (it fails on the documentation corpus), and it is not a test-quality defect of the 47 tests under review. I am not re-litigating its decomposition (that belongs to the wave gate review); I note that none of the 47 new tests adds doc citations, so the new tests cannot grow it.

## 5. Findings (R-W<N>-nnn, severity, location, evidence, recommended fix)

### R-W1-01 — MINOR — T13 pins code↔test-literal, not code↔ADR-0043 — `tests/test_config_settings.py:237-262`

The "ADR table" (`EXPECTED_EDGE_TYPES`) is a literal inside the test file. If someone adds an edge to `EDGE_TYPES` and to the test literal but not to `docs/adr/0043-graph-edge-count-reconciliation.md`, all 3 T13 tests pass while the ADR drifts — exactly the failure mode the test's docstring claims to prevent ("adding or removing a graph edge without updating the ADR fails the build"). Verified: [VERIFIED EDGE_TYPES live value == 12 members == the 12 lines in ADR-0043's table today] — the *current* state is correct; the *lock* is one step weaker than advertised. Recommended fix (cheap, keeps the byte-exact pin): parse the backtick-quoted edge names out of ADR-0043's table at test time and assert equality with `EDGE_TYPES`, keeping the literal as a cross-check; or document in the test docstring that literal↔ADR correspondence is human-verified at review time. Not a blocker: the failure mode requires a coordinated 2-file edit that code review would see.

### R-W1-02 — MINOR — Per-hit fallback (missing live row / deleted fact) and `db=None` branches untested — `tests/agents/test_semantic_derivation_p002.py` (absence), branch at `src/paa/agents/context_agents.py:272,301-311`

The code's most subtle fallback — *some* hits hydrated, *one* hit's row missing (deleted fact) — is exercised only as all-or-nothing (total `fetch_all` failure). A fetcher returning a subset (e.g. an `INNER JOIN` regression, or `fetch_all` truncating) would silently degrade individual hits to payload defaults with no test noticing. Same for the `self._db is None` guard (db-less builders exist in the codebase shape; `_hot_facts` at :171 has the identical guard). Recommended tests (see §6 P1/P2): (1) hits = [live fact, deleted fact id] → live hit derived, deleted hit payload-fallback, both in output; (2) `PlannerContextBuilder(db=None, ...)` → payload fallback, no crash. Both are ~15 lines following the existing `_FakeVectors` pattern.

### R-W1-03 — MINOR — Implementer fail-before claims for P002 rest partly on a TypeError, not on behavioral mismatch — verification note, not a code defect

Against `294fb31` originals, 8 of 9 semantic tests fail with `TypeError: _semantic() got an unexpected keyword argument 'now'` — they fail, but not by exercising the payload-trust bug. The *behavioral* proof (7F/2P) comes from W1-IMPL-03's signature-preserving file-copy revert, which I did not repeat (their reported values — 1.0 vs 0.549, 0.8 vs 0.223 — are the right shape and consistent with my originals run; I re-proven the runtime and CLI behavioral reverts myself). This is honest in their report ("7 failed, 2 passed" after revert) but the task brief's "tests must fail without the fix" is only fully satisfied because the fix includes the signature. Anyone re-verifying naively against the base SHA will see TypeErrors and should not mistake them for behavioral pins. No fix needed; record for the gate.

### R-W1-04 — NIT — `test_get_settings_picks_up_env_after_cache_reset` asserts stale-then-fresh in one test — `tests/test_config_settings.py:190-201`

The mid-test assertion (line 196, cache stays stale without reset) is a correct and useful pin, but if `reset_settings_cache()` ever runs between lines 194-196 due to a future fixture change, the test fails for a confusing reason. Fine as-is; consider a comment (already present) — no action required.

### R-W1-05 — NIT — CLI whitespace-strip normalization silently depends on single-word bodies — `tests/cli/test_redaction_p001.py:11-13,57-64`

The strip-reconstruction is exact *because* every fixture body is one word, and the docstring says so. But a future multiline exception body (real exceptions have newlines) would fold differently and could produce a false failure. The docstring already discloses this; a one-line guard (`assert "\n" not in exc_text` in `_invoke_drain_with_raising_drain`) would make the assumption fail loudly at the fixture instead of opaquely in an assertion. Optional.

## 6. Missing edge cases and tests (prioritized)

### P1 (must add)

**P1-a. Partial hydration: one hit's live row missing (deleted fact) mid-batch.**
- Scenario: vector store returns 2 hits — hit A has a live `hot_serving_active_facts` row, hit B's id has no row (fact deleted after enqueue).
- Expected behavior: A derives via `effective_confidence` with live `last_queried_at`; B falls back to payload snapshot (confidence/importance/domain) — both elements present, B's confidence is exactly the payload value (not 0.8/0.5 unless the payload itself says so).
- Belongs in: `tests/agents/test_semantic_derivation_p002.py`.
- Sketch: create facts for A only; payload for B with confidence 0.42/importance 0.33; `_FakeVectors([A_hit, B_hit])`; assert `by_id[A].confidence == approx(effective_confidence(...))`, `by_id[B].confidence == 0.42`, `by_id[B].importance == 0.33`, `len(elements) == 2`.

### P2 (should add)

**P2-a. `db=None` builder serves payload fallback without crashing** (`context_agents.py:272` guard).
- Sketch: `PlannerContextBuilder(db=None, vector_store=_FakeVectors([hit]), embedder=_FakeEmbedder())` → `_semantic` returns payload values, no warning required.

**P2-b. Empty hits → early `[]` without a hydration query** (`if not hits: return []`).
- Sketch: `_FakeVectors([])` + counting `fetch_all` wrapper → `_semantic` returns `[]`, `calls == []`. Guards the `WHERE id IN ()` syntax-error regression.

**P2-c. Duplicate hit ids deduped before the IN clause.**
- Sketch: two `_Hit`s with the same id → exactly 1 `fetch_all` call whose SQL contains one `?` placeholder (or at least: result has both elements, query count 1). The dedup is in the production code (`dict.fromkeys`); a refactor dropping it inflates the placeholder list (correctness-neutral under SQLite's 999-var limit at limit=30, but the IN-size contract is worth pinning since the code's comment claims it).

### P3 (nice to have)

**P3-a. Config env: case variants and `.env`-file loading** — `PAA_TIER__DRY_RUN=True` vs `true`; and a `.env` in CWD (pydantic-settings reads CWD `.env` per AGENTS.md) not polluting `Settings()` in tests. Low risk; pydantic owns this behavior.

**P3-b. CLI: newline-containing exception body under the strip-normalization.** Either add a `"\n" not in exc_text` fixture guard (R-W1-05) or a normalized assertion that newline bodies still truncate within 200 rendered chars. Current fixtures deliberately avoid it; a guard is enough.

## 7. What I could not verify

- **Postgres placeholder path** for the new `WHERE id IN (?,…)` (P002): cannot run without `PAA_TEST_POSTGRES_DSN`; the `?`→`%s` translation is assumed from codebase convention (same as W1-IMPL-03's stated assumption). The SQLite-path tests are real, on the `Database` fixture.
- **Live Qdrant/Postgres production path** for `_semantic`: not run (no containers per standing orders); the fake-vector tests exercise the identical `fetch_all`/`effective_confidence` code.
- **Full test suite**: out of scope (standing orders §16); only scoped regression files were run. The 14 baseline failures from 00-mission.md were not re-run; nothing in the new tests touches their files except `tests/cli/test_memory.py` (89-test regression batch above included it and it was green).
- **Coverage tooling**: none installed; branch coverage in §2 is by inspection.
- **W1-IMPL-01's docs-lock decomposition** (9 blank-line citations from config.py shift vs pre-existing): I confirmed the lock test fails but did not independently re-derive the 9-vs-rest split; that is beyond test-quality scope and belongs to the wave gate.
- **W1-IMPL-03's behavioral revert** (signature-preserving): accepted on their reported values + my originals run (8 TypeError failures + 2 by-design passes); I re-proven the P001 runtime and CLI behavioral reverts myself, including the MarkupError crash, but did not repeat the P002 copy-revert. The two by-design passes on originals (runtime benign, semantic vector-failure) match their reported 2 passes exactly.

## Verdict rationale

APPROVE-WITH-CHANGES, not REJECT: all 47 tests are behavior-focused, deterministic, isolated, and fail for the right reason (independently re-proven against `294fb31` in temp worktrees, including the strongest evidence — the hostile-text test crashing with MarkupError on the old line). No BLOCKER or MAJOR findings. The changes requested are additive tests (P1-a through P2-c) and one lock-strengthening note (R-W1-01) — none of them casts doubt on the tested behavior. The mutation-survivor gap (partial hydration) is a branch the current code handles correctly but no test pins; it is the only item I would gate a P-002 sign-off on beyond a reviewer noting it as accepted risk.

**Long-run test (standing orders §8):**
- *Source:* ranking truth is `effective_confidence` (T02 approx at 1e-6, T02/ranking tests); log/console text is the house `redact()` at both P-001 sites (marker positive controls at both).
- *Load:* 100x searches over 5 hits = exactly 100 hydration queries, never 500 (count-asserted); 10 KB exception renders ≤200 chars (size-asserted, not timed).
- *Hostile input:* sk-/Bearer/api_key fixtures, unknown domain, missing payload keys, NUL/ANSI/`[/red]` markup attack, 0/negative/non-numeric env values — all pinned.
- *Operability:* event names preserved (`memory.embedding_sink`, `embedding drain failed:` prefix), token-straddle ordering, marker carries the best-effort note; rollback is a revert with no migration.
- *Reversal:* every fix-verifying test fails without its fix (proven by me against 294fb31 for runtime + CLI, and by the implementer's behavioral revert + my originals run for P002); the 4 pinning tests (benign passthrough ×2, 200-boundary, vector-failure `[]`) pass both ways *by design* and are labeled as such in the test files themselves.

**Commits:** none (read-only review; I edited only this report). Last COMMS line read: 2026-11-07T13:46:20Z W1-IMPL-02 -> ALL | DONE | T-001-P001. Working tree source/tests untouched by me; two temp worktrees created for fail-before runs and removed.
