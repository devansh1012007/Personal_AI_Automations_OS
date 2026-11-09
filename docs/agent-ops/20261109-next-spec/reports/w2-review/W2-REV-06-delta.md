<!-- ORCH-DIGEST:START -->
agent: W2-REV-06 | scope: Wave 2c improve delta 357134a (W2-IMPR-05) + 837ba17/agent.py-in-357134a (W2-IMPR-06) | verdict: APPROVE
findings: BLOCKER 0 | MAJOR 0 | MINOR 0 | NIT 1
claims_audit: 6 claims checked / 0 unsupported-or-false
band_aids_found: none
top_3_risks: S3-backed journal blob path not exercised live (pre-existing, factory-selected); directory-as-target reads checked-absence (NIT-1, verdict still defensible); white-box prod tests couple to private journal plumbing (disclosed DECISION)
optimizations: DO-NOW 0 | DO-LATER 1 | NOT-WORTH-IT 1
reversal_check: reasoned-only (read-only mandate; each new test is constructed to fail on revert of its hunk)
confidence: high (blind pass on diff, then scoped suites re-run by reviewer, conventions green after ORCH docs sweep)
<!-- ORCH-DIGEST:END -->
# Code review — W2-REV-06 (delta: W2-IMPR-05 + W2-IMPR-06)

## 0. Method
Blind pass on the delta diff (`git show 357134a`, `git show 837ba17`, `git diff b93bae4..HEAD`) read against the
W2-REV-04 MAJOR-1/MINOR-1/MINOR-2 and W2-REV-05 MAJOR-1 expectations before opening the W2-IMPR-05/W2-IMPR-06 reports.
Then verified each claim against the live tree, re-ran the scoped suites, ruff, mypy, and the conventions lock
myself. Source untouched (read-only mandate); reversal is reasoned-only.

## 1. Blind-pass findings
Four things to check, and all four held up on reading: (a) `MODEL_ESCALATED` added to the coder-only set must not
over-exempt task lineages — the exemption is a subset test (`_is_coder_only_lineage`, recovery.py:504-513), and a
task lineage carries `TASK_*`/`EXECUTION_*` events outside the set, so it stays non-exempt; (b) a `descending` flag
on `events_since` must be a fixed-literal ORDER BY branch, not interpolated SQL, and the default must preserve
ascending order for the reflection/CLI counting callers; (c) the absent-file verdict must delegate to
`classify_interrupted` (which already distinguishes creation `pre=None` from edit-vanished `pre!=None`) rather than
hard-coding `NOT_APPLIED`; (d) the prod journal injection must use the same `get_blob_store(settings.storage)`
factory as the signal/intake path, lazily, so caller-supplied journals and the import graph are untouched. All
four are exactly what the diff does.

## 2. Claims audit
- **(1) Escalated coder lineage exempt; task lineage with escalation still resumes** — SUPPORTED. [VERIFIED
  src/paa/ledger/recovery.py:282] `EventType.MODEL_ESCALATED` is in `_CODER_EVENT_TYPES`; [VERIFIED
  src/paa/ledger/recovery.py:504-513] `_is_coder_only_lineage` is `bool(kinds) and kinds <= _CODER_EVENT_TYPES`
  (empty and pure-neutral histories still non-exempt); [VERIFIED src/paa/ledger/replay.py:412]
  `MODEL_ESCALATED` is `_NO_PHASE_CHANGE`. Tests: escalated coder lineage → `SKIPPED_CODER_LINEAGE` (1 skipped, 0
  resumed, no requeue), orphan `MODEL_ESCALATED`-only lineage → skipped, task lineage + escalation → `RESUMED` (1,
  requeued=1) [VERIFIED tests/coder/test_recovery_chaos.py:434-524; RAN -> 12 targeted tests passed].
- **(2) Newest-first scan evicts oldest (limit test)** — SUPPORTED. [VERIFIED src/paa/ledger/store.py:385-388]
  `descending` selects a fixed `ORDER BY sequence_id DESC` literal, default `ASC`; [VERIFIED
  src/paa/ledger/recovery.py:454-456] the resume scan passes `descending=True` and reverses to chronological
  order. `test_bound_evicts_oldest_not_newest` (limit=2 over 4 tool events, target is the newest) fails on an
  ascending scan by construction and passes now; `test_results_keep_chronological_order` pins the reverse
  [VERIFIED tests/coder/test_recovery_chaos.py:334-359]. Other `events_since` callers (cli/main.py:308,
  improvement/reflection.py:282) use the default and are unaffected [VERIFIED via grep].
- **(3) Absent creation file → `not_applied`, not `unknown`** — SUPPORTED. [VERIFIED
  src/paa/coder/agent.py:360-367] `elif resolved is not None:` sets `live_checked=True, live_sha256=None` for a
  jailed target with no file, delegating to `classify_interrupted`, which returns `NOT_APPLIED` iff `pre is None`
  and `UNKNOWN` for a vanished edit [VERIFIED src/paa/ledger/recovery.py:349-356]. Unreadable file (`OSError`)
  still degrades to `UNKNOWN` [VERIFIED agent.py:354-359]; jail failure (`resolved is None`) still degrades
  [VERIFIED agent.py:352-353]. Test asserts the resume note reads `not_applied` + "safe to retry" with no
  "do not re-run" [VERIFIED tests/coder/test_recovery_chaos.py:749-807; RAN green].
- **(4) Prod journal carries the configured blob store rooted at `cold_lake_path`; oversized round-trips
  sha-pinned** — SUPPORTED. [VERIFIED src/paa/coder/agent.py:228-237] the default-journal branch passes
  `blob_store=get_blob_store(runtime.settings.storage)`, lazily imported, caller-supplied journal untouched;
  [VERIFIED src/paa/storage/coldlake/__init__.py:95-99] the factory returns the CAS rooted at
  `settings.cold_lake_path` for the default `filesystem` backend. `TestProdComposition` (3 tests) drives the real
  built agent's own journal: store is a `ContentAddressedStore` with `root == settings.storage.cold_lake_path`,
  an oversized PROPOSED row is a blobbed stub ≤ 8 KB, the blob file exists under the cold-lake root, the resolved
  payload is byte-identical, `sha256(canonical_json(back)) == descriptor sha256`, and `verify_chain` passes
  [VERIFIED tests/coder/test_blobbing.py:450-544; RAN green]. The `journal=` seam is pinned by
  `test_caller_supplied_journal_still_wins` (explicit journal used verbatim).
- **(5) No regressions: recovery_chaos + blobbing + journal + ledger suites, ruff 0, mypy 3, conventions 34** —
  SUPPORTED [RAN `.venv/Scripts/python -m pytest tests/coder/test_recovery_chaos.py
  tests/coder/test_blobbing.py tests/coder/test_journal.py tests/ledger -q -p no:warnings` -> exit 0, 265 dots
  (72+72+72+49)] [RAN `... tests/coder/test_recovery_chaos.py::TestFindInterruptedCallsForSession
  ...::TestCoderExemption ...::test_resume_absent_creation_is_not_applied
  tests/coder/test_blobbing.py::TestProdComposition -v` -> 12 passed] [RAN `ruff check src tests` -> All checks
  passed!] [RAN `mypy src` -> Found 3 errors in 1 file, all pre-existing
  `src/paa/sandbox/subprocess_backend.py:548` (win32 POSIX attrs), 0 in changed files] [RAN
  `tests/conventions -q` -> 34 dots, exit 0; first full attempt timed out at 120s (known aggregate-import child
  flake, cf. W2-REV-05 NIT-2), `test_docs_references` (5) and `test_import_conventions` (21) passed separately,
  full suite then passed at a longer timeout — the 33/34 both implementers reported was the pre-sweep state, closed
  by ORCH's 5fcf2a8 docs re-point].
- **(6) Shared commit 357134a contains both agents' hunks coherently (no lost hunks)** — SUPPORTED [RAN
  `git show 357134a:src/paa/coder/agent.py | Select-String "blob_store=get_blob_store|elif resolved"` -> both
  present] [RAN `git show 357134a --stat` -> 4 files: agent.py, recovery.py, store.py, test_recovery_chaos.py;
  `git show 837ba17 --stat` -> test_blobbing.py only] [RAN `git diff b93bae4..HEAD --stat` -> the same 4 source/test
  files plus the two reports and the ORCH docs sweep, nothing dropped]. The attribution overlap (W2-IMPR-06's
  agent.py hunk landed inside W2-IMPR-05's whole-file add, per standing-order 12) is honestly recorded in both
  reports; the working tree at HEAD contains both hunks.

## 3. Long-run test (independent answers)
- **Source:** ledger idempotency and exemption remain a pure subset test over persisted event types; blobs are
  sha-addressed by the existing CAS. No new state, no replay change. [VERIFIED recovery.py:504-513, replay.py:412]
- **Load (100x/concurrency/partial failure):** per-lineage sweep cost unchanged (one read + existing closes);
  the resume scan is still O(limit) rows, now newest-first. Crash mid-sweep re-closes idempotently; escalated
  lineages can no longer mint one phantom task per boot. The prod journal adds one deduping CAS `put` per
  oversized append — the cold lake's existing growth model.
- **Hostile input:** `descending` is a bool selecting between two fixed ORDER BY literals — no interpolation
  surface [VERIFIED store.py:385-388]. The absent-file verdict still requires `assert_inside_workspace` jail
  resolution first [VERIFIED agent.py:348-353]; unreadable/unjailed targets stay `UNKNOWN`, never a guess.
- **Operability:** same outcomes vocabulary; `SKIPPED_CODER_LINEAGE` counts are now truthful for escalated/orphan
  lineages. Journal output is observable via `is_blobbed_payload`/`payload_size_bytes`; rollback is a revert
  before any blobbed row exists.
- **Reversal (reasoned-only):** reverting the `_CODER_EVENT_TYPES` line fails the two exemption tests (RESUMED,
  requeued=1 — exactly W2-REV-04's P1/P3 probes); reverting `descending=True` fails
  `test_bound_evicts_oldest_not_newest`; reverting the `elif` fails `test_resume_absent_creation_is_not_applied`;
  removing `blob_store=` fails both prod-composition tests (`_blob_store is None` / `ValueError` on the oversized
  row). Each assertion is exact-count/exact-value against a real store or the real built agent.

## 4. Findings
**NIT-1 — Directory/broken-symlink target reads as checked file absence**
`src/paa/coder/agent.py:360-367`: the `elif` fires whenever the jailed target is not a regular file, including
when the path exists as a directory (or a broken symlink). The comment says "no file there", which is literally
true, and for a creation the `NOT_APPLIED` verdict is still defensible (no file edit landed), but the note will
say "safe to retry" for a target that is a directory the retry cannot write as a file. D-6-safe (the model
attempts and the tool refuses), observation-only. No action required for this delta.

No BLOCKER, MAJOR, or MINOR findings.

## 5. Attempts to break it
- Over-exemption: task lineage + escalation + interrupted call → still RESUMED and re-queued (pinned by test) —
  the subset guard holds.
- Scan bound: 4 tool events, limit=2, target newest → target found; order back to chronological (pinned) — the
  descending cut is the one that keeps the newest.
- Absent-file verdict: creation with no file → `not_applied`; edit whose file vanished → `UNKNOWN` (the
  `classify_interrupted` branch), unreadable file → `UNKNOWN`, jail escape → `UNKNOWN` — each by construction
  of the unchanged branches.
- Prod path: oversized row through the built agent's own journal (not a rebuilt double) → blobbed stub ≤ 8 KB,
  blob under `cold_lake_path`, sha re-verified on read-back, chain verifies; explicit `journal=` still wins —
  all pinned.
- Malformed/orphan shapes: `MODEL_ESCALATED`-only lineage → skipped, not a phantom task (pinned).
- Read-only limits: no source mutation was performed, so the red-side of each reversal is reasoned from the test
  bodies, not re-run. The tests themselves assert the failure modes, not just the happy path.

## 6. Dimension checklist
- **Correctness:** all five brief checks verified against the live tree and by re-run; no over-exemption, no
  ordering regression, no caller of `events_since` changed behavior.
- **Security:** no new trust surface; fixed-literal SQL branch; jail-before-hash on resume preserved; blob
  substrate is the deployment's own cold lake under `PAA_HOME`.
- **Performance:** boot/resume-time only; one extra CAS `put` per oversized append; no hot-path cost.
- **Maintainability:** docstrings now state the newest-first behavior and the checked-absence semantics (the
  previously false "newest-first protection" claim is gone); commit attribution overlap is documented in both
  implementer reports.
- **Tests:** 6 new recovery-chaos tests + 3 prod-composition tests, all exact assertions on real stores / the real
  built agent; mutation property holds by construction for each hunk.

## 7. Optimization and simplification opportunities
- DO-NOW (0): nothing.
- DO-LATER (1): a session-scoped tool-call query (indexed) instead of scan+filter for resume — only pays off
  past 10k tool events (already recorded as DO-LATER in W2-REV-04; the descending cut makes the bound honest in
  the meantime).
- NOT-WORTH-IT (1): streaming blob reads / store-level size guard (W2-REV-05 MINOR-1/MINOR-4) — unchanged by
  this delta; in-memory is honest at KB scale.

## 8. Cross-agent interactions
- W2-REV-04's MAJOR-1/MINOR-1/MINOR-2/MINOR-3 and W2-REV-05's MAJOR-1 are all closed by this delta; the two
  implementer reports' per-phase accounting matches the diff.
- The conventions 33/34 that both implementers reported (blank docs cites after the recovery.py/store.py shifts)
  is closed at HEAD by ORCH's 5fcf2a8 docs re-point: [RAN `tests/conventions` -> 34 passed].
- Shared-tree commit attribution: W2-IMPR-06's agent.py hunks are inside 357134a and its tests in 837ba17; the
  combined tree is coherent and complete.

## 9. What I could not verify
- Mutation/reversal by actually reverting hunks (read-only mandate) — reasoned from test construction only.
- Full test suite beyond the scoped suites [RAN: recovery_chaos + blobbing + journal + ledger (265), targeted 12,
  conventions (34)] — [ASSUMED] from the scoped runs and the implementers' reports.
- S3-backed prod journal (`backend_coldlake=s3`) end-to-end — needs a live server; the factory selection is the
  same one the signal path uses [VERIFIED coldlake/__init__.py:95-99], and the journal's `BlobStoreLike`
  contract is backend-agnostic [ASSUMED]. A cold lake that cannot be constructed now fails `CoderAgent.build` at
  composition time (fail-fast at the honest boundary, disclosed in W2-IMPR-06 §5) rather than failing mid-run.
- PostgreSQL-backed `events_since` ordering — SQLite-only here; the ORDER BY literals are standard SQL
  [ASSUMED].
