<!-- ORCH-DIGEST:START -->
agent: W2-GEN-01 | scope: wave 2 / W2-GEN-01 trivial test additions to tests/agents/test_semantic_derivation_p002.py | verdict: DONE
tests_added: 2 (R-W1b-01 per-hit redaction sibling + R-W1b-02/TREV P2-c duplicate-IDs dedup) | 14 -> 16 passed, 3 repeat runs green, ruff 0 scoped
mutation_kills: per-hit redact() deletion + dict.fromkeys dedup removal (both pinned, fail-if-removed reasoning in report)
src_edits: none (read-only context_agents.py) | skipped: R-W1b-03 TypeError NIT (out of scope per brief)
commits: test 5b7bfd6, report docs-only follows (lists 5b7bfd6)
blockers: none
<!-- ORCH-DIGEST:END -->

# Report — W2-GEN-01 (GEN) · wave 2 · run 20261107-integration-fix

Task: apply the two MINOR test additions from wave-1 re-review TREV-W1b
(R-W1b-01 per-hit redaction pin + R-W1b-02/TREV P2-c duplicate-hit-IDs dedup)
to `tests/agents/test_semantic_derivation_p002.py`. Test additions only — no
src/logic/interface changes. R-W1b-03 (TypeError path NIT) is explicitly out of
scope and recorded as seen-and-skipped.

## 0. Required reading (in order, done)

1. `templates/STANDING-ORDERS.md` (general agent of Step 5 — minor = trivial test additions).
2. `AGENTS.md` (zero-baseline: ruff 0 / mypy 0; no `# noqa`, no weakened tests).
3. `reports/w1-review/TREV-W1b.md` §§5–6 (R-W1b-01 exact sketch incl. secret-shaped
   corrupt value `sk-ABCDEF1234567890` and `***REDACTED***` + token-absent
   assertions; R-W1b-02/TREV P2-c sketch: two `_Hit`s same id → 1 `fetch_all`
   with 1 placeholder or result+count-1).
4. `reports/w1-review/TREV-W1.md` §6 P2-c (unchanged sketch).
5. `tests/agents/test_semantic_derivation_p002.py` in full (659 lines, 14 tests:
   T02, missing-key, degrade, unknown-domain, idempotent, batch, load-100x,
   ranking, vector-failure, partial, db=None, empty, corrupt-row, superseded)
   and `src/paa/agents/context_agents.py` `_semantic` (read-only).

## 1. Source verification (what the tests will pin)

- Per-hit log [VERIFIED `src/paa/agents/context_agents.py:311-319`]: `except
  (ValueError, TypeError) as exc:` → `log.warning("context.hydration_failed",
  fact_id=..., error=redact(str(exc)))`, then payload fallback. The batch log
  at `:285` is a *different* line (already redaction-pinned by the updated
  degrade test, TREV-W1b §1).
- `from_iso` embeds its input in the error [VERIFIED
  `src/paa/storage/relational/database.py:72`: `datetime.fromisoformat(value)` —
  `ValueError: Invalid isoformat string: '<input>'`]. So a secret-shaped
  corrupt `last_queried_at` makes the per-hit `redact()` testable: the raw
  token would appear in the unredacted error text, the marker after redaction.
- The fixture token really matches the pattern [VERIFIED
  `src/paa/models/base.py:244`: `re.compile(r"sk-[A-Za-z0-9_\-]{12,}")` —
  `sk-ABCDEF1234567890` has 16 chars after `sk-`, so the positive-control
  assertion (`***REDACTED***` present) cannot pass vacuously, exactly as
  TREV-W1b verified for the degrade test].
- Dedup [VERIFIED `src/paa/agents/context_agents.py:275-276`]:
  `ids = list(dict.fromkeys(str(hit.id) for hit in hits))`,
  `placeholders = ",".join("?" for _ in ids)`. Removing the dedup makes the
  IN list (and params) contain the id twice. The test pins placeholder count
  (`sql.count("?") == 1`) + one call + params carry the id once — so the
  failing leg is the placeholder/params one, not the call count (a single
  call happens either way).

## 2. Changes (WRITE-SET only: tests/agents/test_semantic_derivation_p002.py)

### (a) NEW `test_corrupt_row_hydration_failed_log_is_redacted` (R-W1b-01)

Design decision: sibling test (not in-place modification of
`test_corrupt_row_falls_back_to_payload_not_crash`) so the file grows by
exactly +2 tests to the briefed 16, and the existing corrupt-row test is
untouched (extend-never-rewrite). It creates one fact, corrupts its
`last_queried_at` via raw SQL `UPDATE` to the secret-shaped value
`sk-ABCDEF1234567890` (the honest route — `FactRepository` would not write a
corrupt date), serves a single `_Hit` with payload 0.42/0.33, and asserts:

- result is exactly 1 element, payload-exact 0.42/0.33 (fallback, no crash);
- the captured `context.hydration_failed` entry carries `fact_id == corrupt_id`;
- its `error` text contains `***REDACTED***` (positive control);
- its `error` text does not contain the raw token substring.

Why it fails if the per-hit `redact()` is deleted: `from_iso`'s ValueError
embeds the secret verbatim ([VERIFIED §1]), so the unredacted `error` would
contain `sk-ABCDEF1234567890` → token-absent leg fails; the
`***REDACTED***`-present leg fails too (no pattern → no marker). The batch
log at `:285` is not involved (the batch `fetch_all` succeeds; only the
per-hit `from_iso` raises), so this test pins the *second* redacted emission
point specifically. Mental revert only — src not modified to prove it, per
brief.

### (b) NEW `test_duplicate_hit_ids_deduped_before_in_clause` (R-W1b-02 / TREV P2-c)

Two `_Hit`s with the same fact id, counting `fetch_all` wrapper that records
`(sql, params)` and delegates to the real fetch (mirrors the existing batch/
load/empty tests' failure-injection pattern). Asserts:

- result length 2 (both hits served, derived from the shared row — no hit
  dropped);
- exactly 1 `fetch_all` call;
- `sql.count("?") == 1` and `list(params) == [fact_id]`.

Why it fails if the `dict.fromkeys` dedup is removed: ids become
`[fid, fid]` → placeholders `"?,?"` → `count("?") == 2` fails, and params
carry the id twice → params leg fails. Both legs are deterministic (injected
`now`, no RNG). `params` is captured as the `ids` list passed by the
production call; `list(params)` normalizes tuple/list.

### Out of scope (seen-and-skipped)

- R-W1b-03 (TypeError corrupt path, e.g. NULL column → `float(None)`): NOT
  implemented — brief marks it non-gating NIT and out of minor scope.
  Recorded as skipped in this report and in the final message.
- Nothing else: no src edits, no other test files touched.

## 3. Determinism / isolation

- Both tests inject `now=utc_now()` (the dup test's derivation assertion is
  length-only, but the call is still clock-free for the placeholder/params
  legs; the redaction test asserts payload-exact + log text, no derivation).
- No RNG, no sleeps, no wall-clock dependence for any assertion; fixed payload
  values; real `Database` fixture + raw SQL corruption, counting wrappers are
  `monkeypatch`-scoped (auto-undone).
- The file is run 3x (brief §3) + `ruff check` scoped to the file (§7).

## 4. Evidence (filled after runs)

- Baseline before my edit: 14 tests. After adding the 2 new tests: 16.
- [RAN `.venv/Scripts/python.exe -m pytest tests/agents/test_semantic_derivation_p002.py -q`
  -> 16 passed (16 dots, no F/E), run 1 of 3].
- [RAN same command -> 16 passed, run 2 of 3].
- [RAN same command -> 16 passed, run 3 of 3]. No flakiness (all deterministic:
  injected `now`, real `Database` fixture, counting wrappers `monkeypatch`-scoped).
- [RAN `.venv/Scripts/python.exe -m ruff check tests/agents/test_semantic_derivation_p002.py`
  -> All checks passed! (0 findings)].
- Non-vacuity proof for the redaction pin (scratch `-c` checks, no files
  written, no src touched):
  - [RAN `from paa.storage.relational.database import from_iso;
    from_iso('sk-ABCDEF1234567890')` -> traceback `ValueError: Invalid
    isoformat string: 'sk-ABCDEF1234567890'`] — the raw exception text carries
    the secret verbatim, so an unredacted per-hit log would leak the token.
    [VERIFIED behavior beyond the source read in §1].
  - [RAN `redact('Invalid isoformat string: sk-ABCDEF1234567890')` ->
    `Invalid isoformat string: ***REDACTED***`] — the marker positive-control
    is a real pattern hit, not vacuous: the token matches
    `models/base.py:244` `sk-[A-Za-z0-9_\-]{12,}`.
  - Deleting the per-hit `redact()` therefore flips both legs of the new
    test (marker absent, token present); the other 15 tests do not inspect
    the per-hit `error`, so they stay green — exactly the SURVIVOR the
    review flagged, now killed. (Mental revert only; src never modified.)
- Non-vacuity for the dedup pin (by source read, §1): with the dedup at
  `context_agents.py:275-276`, `ids=[fid]` → placeholders `"?"` → test legs
  pass. Removing `dict.fromkeys` makes `ids=[fid, fid]` → `"?,?"` →
  `sql.count("?") == 1` fails and `list(params) == [fid, fid]` fails; the
  call-count leg (1 call) and result length (2) hold either way, which is
  why the placeholder/params legs, not the count legs, are the pin.
- Scope check: `git status --porcelain` for the two paths before committing
  showed only `M tests/agents/test_semantic_derivation_p002.py` and untracked
  report; no src or other test file touched.
- Full suite: NOT RUN (scoped-only per standing orders §16 + brief §7).

## 5. Long-run test (per standing orders §8, for the 2 tests)

- *Source:* ranking truth stays `effective_confidence` (dup test: both hits
  served from one hydration row, result length 2); degradation truth is the
  payload snapshot per-hit (redaction test: 0.42/0.33 exact — the per-hit
  fallback branch, distinct from the batch fallback the degrade test pins).
- *Load (100x, concurrency, partial failure):* no new load behavior added
  (test-only change); the dup test additionally proves the IN-list size is
  bounded by distinct ids, not hit count — i.e. the placeholder blow-up
  vector is pinned at 1 for a doubled hit. The existing load-100x test is
  untouched and re-run green as part of the 3x file runs.
- *Hostile input:* secret-shaped DB error text (`sk-` token in a corrupt
  timestamp) is redacted at the per-hit emission point before it reaches the
  log — a hostile stored value abusing the error channel. Garbage timestamp
  degrades per-hit, never crashing (_semantic swallows into payload fallback).
- *Operability (observe, roll back, migrate):* the per-hit entry keeps event
  name `context.hydration_failed` + `fact_id` key + redacted `error` — operators
  can distinguish per-hit corruption from batch DB outage; rollback is a pure
  test revert, no migration.
- *Reversal (which test fails if the fix is reverted?):* deleting `redact()`
  at the per-hit line (`context_agents.py:318`) fails the new redaction test
  (marker absent + raw token present) while all other 15 stay green (the
  corrupt-row test asserts event name only, by design). Deleting the
  `dict.fromkeys` dedup (`:275`) fails the new dup test (placeholder count 2,
  params doubled) while the batch/load tests stay green (they count calls,
  not placeholders).

## 6. What I could not verify / not run

- **Src-revert proof by execution.** The brief forbids src edits and says to
  run a mental revert, so the fail-if-removed claims are reasoned + supported
  by the non-vacuity scratch proofs in §4 (raw error carries the token;
  `redact()` maps it to the marker; dedup removal doubles the placeholder
  string by construction). They were NOT re-proven by reverting src.
- **Full suite / neighbouring suites:** out of scope (standing orders §16 +
  brief §7). Only the one test file (x3) + scoped ruff were run. `mypy` not
  run — the trev was test-only and the brief scopes checks to the test file
  + ruff; no type annotations that mypy strict would newly check beyond the
  file's existing relaxed `tests.*` rules.
- **Postgres placeholder path** for the dup test (same limitation as TREV-W1
  §7): no `PAA_TEST_POSTGRES_DSN`; the SQLite path is exercised for real.

## 7. Out-of-scope findings

None. No scoped failure was caused by a sibling's in-flight source edit
(The brief §5 contingency — wait/retry/FINDING — was not triggered; all 3
runs were green on the first pass after the edit while W2-IMPL-01..04 work in
parallel). Pre-existing items noted in the brief stay out of scope:
hostile payload scalar crash and lock-blind off-by-one citations were not
encountered and not touched; the pre-existing unredacted `str(exc)` lines
(expander, vector path, graph) remain FINDING-only per the wave-1 handoff.
R-W1b-03 (TypeError corrupt path) was seen and deliberately skipped (§2).

## 8. Commits

- Test commit: `5b7bfd6` — adds the 2 tests only (1 file, 93 insertions).
  Trailers `Task: W2-GEN-01`, `Agent: W2-GEN-01`, `Wave: W2`,
  `Report: <this path>`.
- Report commit (this file, docs-only, follows; lists `5b7bfd6` as the code
  commit). If index.lock were hit, retry 2–6 s (max 10), never delete it —
  did not occur.
- Branch verified before committing: `git branch --show-current` →
  `ops/20261107-integration-fix`. Explicit paths only; no forbidden git
  operations used (no `add -A/./-u`, no `-a/--amend`, no stash/reset/checkout/
  clean/rebase/merge/push/`--no-verify`). COMMS appends used PowerShell
  `Add-Content` (the host has no `printf`/`date -u`; same append-only,
  one-line-per-message semantics, real UTC timestamps).

## 9. COMMS read-point

Last COMMS line read at start: `2026-11-07T08:48:33Z ORCH -> ALL | DECISION | W2 | FACT wave 2 start...` (wave-2 START). Updated after each subsequent read point. COMMS lines posted by me: START + CLAIM (2 of ≤6 allowed). Final DONE line appended before finishing (see COMMS.md tail); no NEED/QUERY/FINDING/CONFLICT was needed — both tests passed without any src change, so no DEFERRED-DEPENDENCY.
