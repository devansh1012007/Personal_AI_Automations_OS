<!-- ORCH-DIGEST:START -->
agent: TREV-W1b | scope: wave 1 fix re-review / T-002-P002 (diff 228fcf1..HEAD, semantic tests 9->14) | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 14 (5 new/updated: degrade-redaction, P1-a partial, P2-a db=None, P2-b empty, corrupt-row, superseded — 6 with degrade counted)
fail-before/pass-after confirmed: 0 / reasoned: 6 / not-possible: 0 (read-only: fail-before reasoned from test bodies + fix-report reversal data, not re-run)
findings: BLOCKER 0 | MAJOR 0 | MINOR 2 | NIT 1
flaky_suspects: none (3 repeat runs green)
missing_edge_cases: P1 0 | P2 1 | P3 2
mutation_survivors: 1 of 15 plausible bugs not caught (duplicate hit IDs, TREV P2-c still open); +2 narrow sub-paths (per-hit redact, TypeError corrupt path)
changed_behavior_without_tests: none
confidence: high (all 14 tests run green by me x3; blind-pass on the diff before reading the fix report; fix-report 3F/11P arithmetic audited coherent against the bodies)
<!-- ORCH-DIGEST:END -->

# Test quality review — TREV-W1b (fresh independent, read-only; did not write the code or tests)

## 0. Method (temporary worktree used? repeat counts, coverage tooling)

- Read STANDING-ORDERS, review-tests template, COMMS (last line read: `2026-11-07T14:20:00Z W1-FIX-DOCS -> ORCH | DONE`), TREV-W1 §§6-7, W1-FIX-CODE entire.
- **Blind pass first** [VERIFIED `git diff 228fcf1..HEAD -- src/paa/agents/context_agents.py` (46 lines: module-level `redact` import, batch log -> `context.hydration_failed` + `redact(str(exc))`, `superseded_by IS NULL` filter, per-hit `try/except (ValueError, TypeError)` wrap) and `git diff 228fcf1..HEAD -- tests/agents/test_semantic_derivation_p002.py` (215 lines: degrade test updated, 5 new tests)] **before** opening W1-FIX-CODE's claims, then audited the report against the source bodies.
- Ran the semantic file myself 3x [RAN `.venv\Scripts\python.exe -m pytest tests/agents/test_semantic_derivation_p002.py -p no:cacheprovider -q` -> 14 passed on all 3 runs (14 dots each, no F/E/s)]. No temp worktree (read-only brief: fail-before reasoned from the test bodies against the pre-fix code shape visible in the diff, plus the fix report's recorded reversal output).
- No coverage tooling (no plugin); branch coverage by inspection of test-vs-code. No git writes; only this report file edited.

## 1. Per test/suite assessment

### tests/agents/test_semantic_derivation_p002.py — 14 tests (9 pre-existing + 5 new + 1 updated)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_t02_derived_confidence_and_provenance_and_floor` (unchanged) | Yes | Yes (payload-trust revert) | Yes — injected `now`, real store, closed in `finally` | No | Pre-existing; re-green in my runs |
| `test_missing_confidence_key_hydrated_value_wins` (unchanged) | Yes | Yes | Yes | No | |
| `test_unknown_domain_passthrough_not_crash` (unchanged) | Yes | Yes | Yes | No | |
| `test_idempotent_same_now_same_derived` (unchanged) | Yes | Yes | Yes | No | |
| `test_batch_hydrate_one_query_per_search` (unchanged) | Yes | Yes (N+1) | Yes — counting wrapper delegates to real `fetch_all` | No | |
| `test_load_100x_hydration_query_count` (unchanged) | Yes | Yes (N+1) | Yes — count, not timing | No | |
| `test_ranking_uses_derived_not_payload` (unchanged) | Yes | Yes | Yes | No | |
| `test_vector_failure_still_returns_empty` (unchanged) | Yes — pins `context.vector_search_failed` still the vector-path event | Passes both ways **by design** (pinning: vector path untouched by the fix) | Yes — `_ExplodingVectors` only | No | Correctly scoped: this pre-existing test proves the rename was surgical, see degrade test below |
| `test_hydration_db_failure_degrades_to_payload_snapshot` (**updated**) | Yes — payload 0.9 exact, **new** event name, **absence** of old name, redaction (marker positive control + raw token absent, on **every** `hydration_failed` entry) | **Reasoned yes** (see §3): pre-fix code logs `context.vector_search_failed` with verbatim `str(exc)` -> both the event-name and redaction assertions fail | Yes — `tmp_path`/`db` fixture, `monkeypatch.setattr(db,"fetch_all",_boom)` is failure injection, not a SUT mock; structlog `capture_logs` | No | Strongest test in the batch. Token `sk-ABCDEF1234567890` (16 chars) actually matches `models/base.py:244` `sk-[A-Za-z0-9_\-]{12,}` [VERIFIED], so the marker assertion cannot pass vacuously. The `"context.vector_search_failed" not in events` leg also pins that the batch path no longer squats on the vector event (operators can distinguish DB vs vector outage) |
| `test_partial_hydration_missing_live_row_falls_back` (**new, TREV P1-a**) | Yes — 2 elements (nothing dropped), live hit derives to `effective_confidence(...)` oracle at 1e-6 (aged 300d, so derived ≈0.55 vs payload 1.0), deleted hit is payload-exact 0.42/0.33, and deleted ≠ 0.8 default | **No — by design** (pinning): the else-branch existed pre-fix; passes on pre-fix code. This is the correct reading of TREV P1-a (branch coverage of already-correct code), not a bug-fix test | Yes — injected `now`; fixed UUID literals; fake vectors/embedder only | No | Closes the wave-1 mutation survivor. The `!= approx(0.8)` leg kills a "default instead of snapshot" mutation; the oracle leg kills "payload for live hit" |
| `test_db_none_serves_payload_fallback` (**new, TREV P2-a**) | Yes — confidence 0.9, importance 0.6, domain `semantic`, no crash, through the `self._db is None` guard | No — by design (pinning; guard existed pre-fix) | Yes — no db fixture needed at all; fully isolated | No | pings the line `context_agents.py:273` exactly. Deliberately no `now` passed (derivation not asserted — wall clock irrelevant), correct |
| `test_empty_hits_returns_empty_without_hydration_query` (**new, TREV P2-b**) | Yes — `[]` **and** `calls == []` (zero queries) | No — by design (pinning). Honest about its value: without the early return the batch query would raise `IN ()` syntax error, be swallowed by the batch except, and still return `[]` — so the `calls == []` leg, not the `[]` leg, is what pins the guard. It does, so the test is well-formed | Yes — counting wrapper delegates to real `fetch_all`; monkeypatch-scoped | No | Good failure-injection-free design: does not mock the query, counts the real one |
| `test_corrupt_row_falls_back_to_payload_not_crash` (**new, R-W1-006**) | Yes — corrupt hit payload-exact 0.42/0.33 (no derivation), **healthy sibling still derives** (per-hit isolation in one batch), `context.hydration_failed` emitted, tier non-empty | **Reasoned yes**: pre-fix, `from_iso('garbage-not-a-date')` raises `ValueError` outside any `try` (diff shows the old code had only `except KeyError` around `effective_confidence`, and `from_iso` was called inside it? No — pre-fix `from_iso` call sat inside the `try:` whose `except` was `KeyError`, so `ValueError` propagated) -> `_semantic` raises, test errors. Fix report's recorded reversal says exactly this | Yes — injected `now`; corrupt value written via raw SQL `UPDATE` (the honest way: `FactRepository` would not write a corrupt date) | No | Gap: asserts event name only, **not** redaction of the per-hit log line (`context_agents.py:318`) — see R-W1b-01. `ValueError` path only (`TypeError`/`float(None)` path unpinned — NIT) |
| `test_superseded_fact_hit_does_not_hydrate` (**new, R-W1-007**) | Yes — payload-exact 0.42/0.33, and `confidence != approx(derived)` (discriminator non-degenerate: aged 300d so derived ≈0.5488 vs payload 0.42) | **Reasoned yes**: pre-fix SELECT has no `superseded_by IS NULL` (diff confirms the filter is added by this fix) -> row hydrates -> derived 0.5488 vs asserted 0.42 -> fails for the right reason | Yes — injected `now`; `superseded_by` written via raw SQL UPDATE | No | A superseded hit proving non-hydration is behaviour, not SQL-string-matching — better than asserting `"superseded_by IS NULL" in sql`. The suite pins the filter narrowly in the other direction (T02 etc. fail if the filter excluded live rows) |

## 2. Coverage of changed lines/branches, error paths, boundaries

- **Batch log rename + redact (`context_agents.py:285`)** — covered both legs (name, absence of old name, redaction on every entry) by the updated degrade test. `structlog` `error=` kwarg name also pinned.
- **`superseded_by IS NULL` (`:280`)** — covered behaviourally by the superseded test; discriminative (derived vs payload differ by construction).
- **Per-hit `except (ValueError, TypeError)` (`:311`)** — `ValueError` leg covered by corrupt test (`from_iso` garbage). `TypeError` leg (`float(None)` on a NULL column, `from_iso(None)`) not directly covered — same exception tuple, same handler body, so residual risk is small; noted as NIT R-W1b-03.
- **Per-hit log `redact(str(exc))` (`:318`)** — event name covered by corrupt test; **redaction NOT covered** (corrupt exception text contains no secret pattern, and the test does not check `error`). See R-W1b-01.
- **TREV P1-a/P2-a/P2-b branches** — partial (else branch), `db is None` (`:273`), empty (`:255`) all now covered. **TREV P2-c (duplicate hit IDs -> deduped IN list) remains uncovered** — see R-W1b-02. `_touch_recalled`'s removed local import: no behaviour change (module-level `redact` resolves identically), correctly no test needed.
- **Boundaries**: none applicable beyond the pre-existing 200-char/200 tests for other P-001 sites (unchanged here). Empty-hits is the fix's own boundary and is covered.

## 3. Mutation thinking

Fix-report fail-before claim audit (3F/11P — read-only audit, no revert run by me): I verified the arithmetic and representativeness from the bodies. Total 14 = 3F + 11P. The 3F (degrade: old event name + verbatim token; corrupt: `ValueError` propagates; superseded: 0.5488 vs 0.42) are exactly the 3 tests that exercise the 3 changed code paths, and each fails for the recorded reason — the diff confirms the pre-fix code lacks the new event name, the per-hit wrap, and the filter, so those failures follow from the code, not from test fragility. The 11P = 8 unchanged old tests + 3 new pinning tests (partial/db=None/empty pin already-correct behaviour, and by the diff inspection none of the other 8 tests is affected by the change: their facts are not superseded, their rows are not corrupt, and they do not assert the renamed event — except `test_vector_failure_still_returns_empty`, which pins the untouched vector path). **The 3F/11P claim is coherent and, on the bodies, correct.** I did not re-run the file-copy revert (read-only brief); the claim is *reasoned*, not *confirmed by me*.

| Plausible bug | Caught? | By which test |
|---|---|---|
| Batch log keeps old event name | Yes (reasoned) | degrade: `hydration_failed in events` + `vector_search_failed not in events` |
| Batch log loses `redact()` | Yes (reasoned) | degrade: token matches `sk-` pattern [VERIFIED base.py:244], marker + absence assertions |
| Both event names logged (partial rename) | Yes | degrade absence leg |
| Per-hit wrap removed | Yes (reasoned) | corrupt: `ValueError` escapes -> test errors |
| Corrupt hit dropped instead of fallback | Yes | corrupt: `len == 2` |
| Corrupt hit crashes whole batch | Yes | corrupt: healthy-sibling derives |
| Superseded filter removed | Yes (reasoned) | superseded: 0.5488 ≠ 0.42 discriminator |
| Missing-row (deleted fact) hit dropped / defaulted to 0.8 | Yes | partial: `len == 2`, 0.42/0.33 exact, `!= 0.8` |
| Live hit uses payload instead of derivation | Yes | partial oracle + pre-existing T02 |
| `db=None` crashes (`AttributeError`) | Yes | db=None test |
| Empty hits issues `IN ()` query | Yes | empty: `calls == []` (the `[]` leg alone would pass even with the query — collected wrongly, this test is right) |
| Per-hit log loses `redact()` | **NOT CAUGHT** | corrupt asserts event name only; no secret in the corrupt exception text (R-W1b-01) |
| `TypeError` corrupt path (`float(None)`) raises | **NOT CAUGHT** | no `float(None)`/NULL-column test (R-W1b-03, NIT) |
| Duplicate hit IDs inflate the IN list | **NOT CAUGHT** | no duplicate-hit test (R-W1b-02, TREV P2-c still open) |
| Per-hit N+1 refactor | Yes | pre-existing batch/load tests (unchanged code) |

Survivors: 1 full (duplicate IDs) + 2 narrow sub-paths. The wave-1 survivor (partial hydration) is killed.

## 4. Flakiness evidence (repeated runs) and test-suite health

- [RAN `pytest tests/agents/test_semantic_derivation_p002.py -p no:cacheprovider -q` -> 14 passed, run 1; 14 passed, run 2; 14 passed, run 3]. Determinism mechanisms verified by reading: injected `now` in every test that asserts derivation (partial, corrupt, superseded, T02, missing-key, idempotent, ranking); no sleeps/timeouts anywhere in the file [VERIFIED grep of the file]; fixed-UUID hit ids; hash-based synonym embedder (no RNG); load test is a query-count assertion, not a timing one.
- Isolation: all new tests are function-scoped against the `db` fixture; the counting wrappers are `monkeypatch`-scoped (auto-undone); no shared state between the new tests (each `add_fact` is independent).
- Suite health: this file alone was run per standing orders §16 (scoped). I did not re-run the P001 runtime/CLI suites (unchanged by this diff).
- One caution for future editors: the degrade test's `"context.vector_search_failed" not in events` is suite-global — if a future refactor makes `_semantic` also log the vector event on the hydration-failure path, this test fails even though the rename itself is intact. That is the intended pin (operator distinguishability), but the failure message should be read as "event-name contract moved", not "hydration broke".

## 5. Findings (R-W<N>-nnn, severity, location, evidence, recommended fix)

### R-W1b-01 — MINOR — Per-hit hydration log redaction is asserted nowhere — `tests/agents/test_semantic_derivation_p002.py:556-616` vs `src/paa/agents/context_agents.py:318`

The fix adds **two** redacted emission points (batch `:285` and per-hit `:318`), but only the batch one is redaction-pinned. The corrupt test's exception text is `Invalid isoformat string: 'garbage-not-a-date'` — no secret pattern — and it asserts only that `context.hydration_failed` is in `events`. Deleting `redact()` at :318 leaves all 14 tests green while leaking whatever secret-shaped text a corrupt error happens to carry. This matters because R-W1-002 was the MAJOR redaction finding and the per-hit line is new code written by this fix. Feasibility verified [VERIFIED `database.py:72`: `from_iso` delegates to `datetime.fromisoformat`, whose `ValueError` message embeds the input] — so a secret-shaped garbage value (e.g. `sk-ABCDEF1234567890` as the corrupt `last_queried_at`) raises a `ValueError` *containing the secret*, and the per-hit `redact()` becomes testable. Recommended fix: in `test_corrupt_row_falls_back_to_payload_not_crash`, set the corrupt `last_queried_at` to a secret-shaped string and mirror the degrade test's redaction assertions against the per-hit `hydration_failed` entry (with `fact_id` key). ~4 lines.

### R-W1b-02 — MINOR — TREV P2-c (duplicate hit IDs deduped before the IN clause) still untested — absence, `tests/agents/test_semantic_derivation_p002.py`

Was in TREV-W1 §6 as P2 (should add); the fix brief scoped to P1-a/P2-a/P2-b, so this is **not a fix-scope miss** — but it remains the one full mutation survivor in the file (the `dict.fromkeys` dedup at `context_agents.py:275`). Correctness-neutral under SQLite's variable limit at `limit=30` but the IN-size contract is claimed in code comments. Recommended fix (per TREV-W1 §6 sketch, unchanged): two `_Hit`s with the same id -> exactly 1 `fetch_all` whose placeholder count is 1, or at minimum result-length + query-count-1. Non-gating.

### R-W1b-03 — NIT — `TypeError` corrupt path unpinned (only `ValueError` covered) — `tests/agents/test_semantic_derivation_p002.py:556`

A NULL `importance`/`initial_confidence` (`float(None)`) or `from_iso(None)` raises `TypeError`, caught by the same `except (ValueError, TypeError)`. One extra `add_fact` + raw SQL `SET importance = NULL` sibling (or parametrize the corrupt test over the corrupt column) would pin it. Same handler body, so risk is minimal — recorded for completeness, not as a gate.

## 6. Missing edge cases and tests (prioritized)

### P1 (must add)

None. All three TREV-gated items (P1-a partial hydration, P2-a `db=None`, P2-b empty hits) are now present, specific, and discriminating; the wave-1 mutation survivor is killed.

### P2 (should add)

**P2-a. Duplicate hit IDs deduped before the IN clause** (TREV P2-c; R-W1b-02 above). Sketch unchanged from TREV-W1 §6: `_FakeVectors([_Hit(x), _Hit(x)])` + counting wrapper -> 1 `fetch_all` call with one `?` placeholder (or result has both elements + query count 1). Belongs in `tests/agents/test_semantic_derivation_p002.py`.

**P2-b. Per-hit redaction pin** (R-W1b-01 above). Secret-shaped corrupt `last_queried_at`; mirror the degrade test's `***REDACTED***` + token-absent assertions for the per-hit entry. Belongs in the corrupt-row test.

### P3 (nice to have)

**P3-a. `TypeError` corrupt path** (R-W1b-03). NULL-column sibling of the corrupt test.
**P3-b. Payload-missing-keys *fallback* path** (else/corrupt branches use `payload.get("confidence", 0.8)` defaults; every current fallback test supplies full payload keys, so the defaults themselves are unpinned in the fallback branches — the pre-existing missing-key test covers the *hydrated* win only).

## 7. What I could not verify

- **Fail-before by execution.** Read-only brief (no temp worktree, no revert run by me). The 3F/11P claim was audited by reasoning over the diff + bodies (§3) and found coherent; it is **reasoned, not confirmed**. The 3 passing-by-design pinning tests (partial, db=None, empty) pass on pre-fix code by construction — they were never expected to fail, and the fix report says so honestly.
- **Postgres placeholder path** for the extended SQL (`?` -> `%s`, same as TREV-W1 §7): no DSN; assumed from the unchanged pattern.
- **Full suite / neighbouring suites** (context_agents, decay, gatherer): out of scope per standing orders §16; only the semantic file was run (3x, 14 green each).
- **Docs-citation lock & the W1-FIX-DOCS half of the diff**: out of scope for this re-review (docs-only changes, no test impact).
- **Pre-existing unredacted `str(exc)` lines** (expander :137, vector path :252, graph :367 — flagged in W1-FIX-CODE Phase C): not re-adjudicated; the semantic tests neither pin nor regress them, and the vector-path one is correctly left as a FINDING per the brief.

## Verdict rationale

APPROVE-WITH-CHANGES, not REJECT: every item TREV-W1 gated on is now closed with strong, discriminating, deterministic tests — the partial-hydration branch (the wave-1 mutation survivor) is pinned with an oracle leg, a payload-exact leg, and an anti-default leg; `db=None` and empty-hits are pinned including the honest `calls == []` leg; the degrade test now pins the new event name, the old name's absence, and redaction on every entry with a token that genuinely matches the `redact()` pattern; the corrupt-row and superseded tests each discriminate (health/sibling isolation; 0.42 vs 0.5488). No BLOCKER/MAJOR. The one change requested before calling the P-002 test story complete is cheap and targeted at the fix's own headline requirement: pin the *second* redacted emission point (R-W1b-01). R-W1b-02 is a pre-existing P2 carried over, explicitly non-gating.

**Long-run test (standing orders §8):**
- *Source:* ranking truth still `effective_confidence` (oracle legs); degradation truth is the payload snapshot, now per-hit — the partial + corrupt tests assert it hit by hit.
- *Load:* unchanged, count-asserted (100 searches = 100 queries), green in my 3 runs.
- *Hostile input:* secret-shaped DB error redacted (batch); garbage timestamp degrades per-hit not tier-wide; superseded truth excluded from hydration. Per-hit secret-in-error is the one hostile case whose redaction is code-only, not test-pinned (R-W1b-01).
- *Operability:* `context.hydration_failed` (DB) vs `context.vector_search_failed` (vector) now distinguishable *and that distinction is itself asserted* — a future rename drift fails loudly.
- *Reversal:* the 3 fix-verifying new/updated tests fail on pre-fix code for the recorded reasons (reasoned from the diff, §3); the 3 new pinning tests pass both ways by design and say so in their docstrings/body.

**Commits:** none (read-only review; I edited only this report). Last COMMS line read: `2026-11-07T14:20:00Z W1-FIX-DOCS -> ORCH | DONE | W1-FIX-DOCS`. Working-tree source/tests untouched by me; no worktree created.
