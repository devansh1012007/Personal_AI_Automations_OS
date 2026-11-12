<!-- ORCH-DIGEST:START -->
agent: W2-IMPR-06 | task: T-N2-6-IMPR (W2-REV-05 MAJOR-1) | status: DONE
code_commits: agent.py hunks in 357134a (W2-IMPR-05 whole-file add, shared tree); tests/coder/test_blobbing.py in 837ba17 | report_commit: follows (docs-only)
files_changed: src/paa/coder/agent.py (2 hunks), tests/coder/test_blobbing.py (imports + TestProdComposition)
contracts_changed: none (default journal gains a blob store; journal/event shapes unchanged)
tests: added 3, changed 0; scoped run: pass (235: blobbing 32 + journal + ledger; build-path 34)
ownership_violations: none (recovery.py untouched)
deferred_dependencies: none for this fix; conventions 33/34 is sibling recovery.py edits (FINDING filed); ORCH sweep should re-point docs/agent-ops cites of agent.py:219+ (+15 shift)
out_of_scope_findings: 1 FINDING (conventions 33/34 sibling recovery.py); independent reviewer unavailable (depth limit)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: independent review unavailable; S3 path not run live
confidence: high (mutation-pinned composition tests; scoped gates green)
last_comms_line_read: W2-IMPR-06 FINDING 16:05:48Z
<!-- ORCH-DIGEST:END -->
# Implementation report — W2-IMPR-06 — W2-REV-05 MAJOR-1

## 1. Task and acceptance criteria (restated)

Fix W2-REV-05 MAJOR-1: `CoderAgent.build` constructs
`LedgerToolJournal(ledger, correlation_id)` with no `blob_store`
(`src/paa/coder/agent.py:219`), so an oversized journalled row raises
`ValueError` and aborts the tool call (fail-closed) instead of blobbing.
Inject the real blob store in the production composition —
`Runtime.build` / `CoderAgent.build` — using the same PAA_HOME blob dir,
sha-pinned. Prove it with a test in `tests/coder/test_blobbing.py` that
goes through the production path (write + read). Do NOT touch
`recovery.py`.

## 2. Investigation (context report)

- [VERIFIED src/paa/coder/agent.py:216-223] The only production
  construction of `LedgerToolJournal` is here; `grep` shows the other
  constructions are tests (which pass stores explicitly or rely on the
  fail-closed raise, both preserved).
- [VERIFIED src/paa/storage/coldlake/__init__.py:68-99] `get_blob_store(settings)`
  is the one factory deciding the substrate (`filesystem` → CAS rooted at
  `settings.storage.cold_lake_path`, `s3` → S3BlobStore); both satisfy the
  `BlobStoreLike` contract the journal needs.
- [VERIFIED src/paa/coder/runtime_tools.py:573-585] Precedent: the signal
  repo is built via `get_blob_store(self._rt.settings.storage)` inside a
  lazy import — the same call the composition root makes for the journal.
- [VERIFIED src/paa/config.py:101,868-889] `cold_lake_path` defaults under
  `home` (`~/.paa/cold_lake`) and `_propagate_home` re-roots it when
  `Settings(home=...)` is passed, so tests get an isolated blob dir for
  free.
- [VERIFIED W2-IMPL-06 NEED-2] The deferred-dependency note named this
  exact one-line fix in the composition root.
- `Runtime.build` wires no journal (the journal is `CoderAgent.build`'s
  default), so the injection point is `CoderAgent.build` only.
- Affected: `src/paa/coder/agent.py` (one hunk), `tests/coder/test_blobbing.py`
  (new composition test class). Invariants preserved: small payloads stay
  inline; caller-supplied journals unchanged; events/journal untouched;
  recovery.py untouched.
- Docs-cite risk: the lazy import goes *inside* `build` (after line ~215),
  so live-doc cites at `agent.py:84/115/118` keep their positions; only
  `docs/agent-ops` reports cite lines after the hunk, and those are
  excluded from the docs-references lock.

## 3. Changes

**Phase 1 — inject the configured store in the composition root
(`src/paa/coder/agent.py`, +15/−8):**
- The default-journal branch of `CoderAgent.build` now passes
  `blob_store=get_blob_store(runtime.settings.storage)` to
  `LedgerToolJournal`. Same factory the signal/intake path uses
  (`runtime_tools.py:573-585`, `api/app.py:387-390`), so the journal blobs
  into the deployment's own cold lake — `cold_lake_path` under `PAA_HOME`
  (`config.py:101,884`) — sha-addressed by the CAS (or the configured S3
  substrate), and `store_blob_bytes` verifies the returned ref's sha
  against the recomputed digest (`events.py:395-402`). Sha-pinning needs
  no new code; it is the existing contract.
- The import is lazy (inside the `else` branch), mirroring
  `runtime_tools.py`; a caller-supplied journal pays nothing and the
  module import graph is unchanged (the import-conventions lock stays
  green). The ternary became an `if/else` so the lazy import reads
  naturally; the `journal=` seam semantics are identical.
- The `journal` parameter docstring now states the default journal blobs
  oversized rows (N2.6) rather than failing the call closed.
- Explicitly *not* built on `Runtime`: the journal is `CoderAgent.build`'s
  default, not `Runtime.build`'s; `Runtime` holds no journal and gaining a
  `blob_store` field would shift `runtime.py` cites for no caller. The
  NEED-2 one-liner named the composition root; that is `CoderAgent.build`.

**Phase 2 — prod-path test (`tests/coder/test_blobbing.py`, +114):**
- New `TestProdComposition` (3 tests) builds the real `CoderAgent`
  (Runtime + loop, scripted model — the documented agent test seam,
  `AGENTS.md` model layer) and drives the journal the loop actually
  holds (`agent.loop._journal` — white-box by necessity: the loop keeps
  the journal private, and a rebuilt journal would prove only that a
  second journal could blob):
  - `test_built_agent_journal_carries_configured_store` — the built
    journal's store is a `ContentAddressedStore` whose `root` is
    `settings.storage.cold_lake_path` (the PAA_HOME blob dir).
  - `test_oversized_row_blobs_and_round_trips_through_prod_journal` —
    an oversized PROPOSED row through that journal is a blobbed stub
    ≤ 8 KB inline, the blob file exists under the cold-lake root, the
    descriptor sha equals the sha256 of the resolved canonical bytes,
    the resolved payload is byte-identical to the original, and
    `verify_chain` passes.
  - `test_caller_supplied_journal_still_wins` — the `journal=` seam is
    untouched: an explicitly passed journal is used verbatim.
- Sibling note (per standing-order 12): `agent.py` also contains an
  uncommitted sibling hunk in `_resume_entry` (W2-REV-04 MINOR-2
  checked-absence verdict, landed mid-wave). Both `agent.py` hunks of
  mine and that sibling hunk ended up in the sibling's commit `357134a`
  (whole-file add); my commit `837ba17` holds only the test file. The
  hunks are untouched and unclaimed — see §8 for the verified accounting.

## 4. Tests and verification

- [RAN `.venv/Scripts/python -m pytest tests/coder/test_blobbing.py tests/coder/test_journal.py tests/ledger -q -p no:warnings` -> **235 passed**, EXIT=0] (blobbing alone: **32 passed**, up from 29)
- [RAN `tests/coder/test_agent.py tests/coder/test_new_integration.py` (every build-path suite for `CoderAgent.build`) -> **34 passed**, EXIT=0]
- [RAN `.venv/Scripts/python -m ruff check src tests` -> `All checks passed!`]
- [RAN `.venv/Scripts/python -m mypy src` -> `Found 3 errors in 1 file`, all pre-existing `sandbox/subprocess_backend.py:548` Windows baseline, **0 new**]
- [RAN `.venv/Scripts/python -m pytest tests/conventions -q` -> **33/34**: the one failure is `test_docs_references::test_every_referenced_line_is_not_blank` with 10 blank cites, **all in `src/paa/ledger/recovery.py:285/306/366`** — the uncommitted sibling MODEL_ESCALATED hunk shifted those lines. Retried once, still red (standing-order 18); FINDING posted to COMMS; `recovery.py` untouched per brief. **0 new blanks from my files** (all live-doc cites of `agent.py:84/115/118` sit before my hunk and keep their positions).
- Mutation mental-check: with the `blob_store=` argument removed, both new
  composition tests fail (`_blob_store is None` / `ValueError: ... no blob
  store is configured` on the oversized `before`), and no other test
  changes behaviour.

## 5. Review audit

Self-review of the diff (fresh read of `git diff`), per phase:
- **Correctness:** the store comes from the same factory the intake path
  uses, so journal blobs and signal blobs are one substrate under one
  `PAA_HOME`; the sha-pin is the CAS `put` + `events.py` ref check, not new
  logic. Small-payload path is untouched (`_blobbed` returns early).
- **Failure modes:** a cold lake that cannot be constructed (unwritable
  path, missing S3 extra) now fails `CoderAgent.build` at composition
  time instead of failing a tool call mid-run — fail-fast at the honest
  boundary, and loud rather than degraded-to-raise. No `try/except` added,
  no silent default.
- **Seam preserved:** `journal=` callers (tests, CLI) get exactly their
  journal; verified by `test_caller_supplied_journal_still_wins`.
- **Docs-cite drift:** lazy import lives inside `build` (after the cited
  lines), so the only semantically shifted cites are inside
  `docs/agent-ops/` reports, which the docs-references lock excludes.
  The 33/34 conventions failure is sibling `recovery.py` work, verified by
  `git diff --stat` and the failure list naming only `recovery.py`.
- Independent reviewer: attempted to spawn a reviewer subagent; the
  runtime refused (`Subagent depth limit reached (1)` — I am already a
  subagent). Review rests on the fresh-diff self-review above plus the
  new tests' mutation property. Do NOT treat this task as
  independently reviewed.

## 6. Long-run test

- **Source:** the cold lake is the source (CAS/S3 addressed by content
  hash); the ledger row pins the address. Same source the signal archive
  already uses — no second blob directory.
- **Load:** one `put` per oversized append (deduping CAS); an unbounded
  number of oversized rows writes unbounded blobs under the cold lake —
  but that is the cold lake's existing growth model, bounded by the same
  64 KB tool-output caps that produce the rows.
- **Hostile input:** oversized preview/reason blobs carry no new attack
  surface — the preview is the existing canonical prefix, values already
  shape summaries (W2-REV-05 MINOR-2, unchanged here); `get_blob_store`
  reads only `settings.storage`, no user input reaches the path.
- **Operability:** observable via `is_blobbed_payload`/`payload_size_bytes`
  on ledger rows; rollback is a revert before any blobbed row exists
  (none can exist pre-fix, by the fail-closed raise).
- **Reversal:** removing the `blob_store=` argument fails
  `test_built_agent_journal_carries_configured_store` (store is None) and
  the round-trip test (`ValueError` on the oversized row) — for the right
  reason.

## 7. Unverified assumptions and remaining risks

- [UNVERIFIED] Independent review unavailable: reviewer-subagent spawn was
  refused (depth limit); self-review is the only review. Do not treat this
  task as independently reviewed.
- [UNVERIFIED] The S3-backed path (`backend_coldlake=s3`) was not
  exercised end-to-end (needs a live server, same as the N2.6 S3 gap);
  `get_blob_store` only selects it when configured, and the journal's
  `BlobStoreLike` contract is backend-agnostic.
- [UNVERIFIED] `tests/conventions` 34/34 not achieved: 33/34 with the
  failure attributed to sibling uncommitted `recovery.py` edits (FINDING
  filed). Once that task commits, the docs sweep is ORCH-owned.
- Note for the ORCH sweep: my `agent.py` hunk shifts later `agent.py`
  lines +15, so any `docs/agent-ops` cite to `agent.py:219+` (e.g.
  W2-REV-05's own MAJOR-1 cite) now points 15 lines early. Those are
  excluded from the lock, but the sweep should re-point them if it wants
  the trail exact.

## 8. Git state

Branch `ops/20261109-next-spec` [VERIFIED `git branch --show-current`].
**Shared-tree commit attribution (verified, not assumed):** my
`agent.py` hunks were committed *inside* sibling W2-IMPR-05's `357134a`
("Improve: MODEL_ESCALATED coder-lineage exemption...") — that agent's
whole-file `git add src/paa/coder/agent.py` staged the working-tree
state, which already contained my journal hunk. My own commit `837ba17`
contains only `tests/coder/test_blobbing.py` (the `agent.py` pathspec was
already clean at add time). Verified: `git show HEAD:src/paa/coder/agent.py`
contains `blob_store=get_blob_store(runtime.settings.storage)`, and
`git diff b93bae4..HEAD` shows both my hunks byte-identical and all
`test_blobbing.py` hunks mine. Per standing-order 12 the code is committed
as-is; history rewriting is forbidden, so the attribution stands and is
recorded here. Re-ran `test_blobbing.py` at HEAD: 32 passed.
Report commit (docs-only) follows. Not committed by me:
`src/paa/ledger/recovery.py`, `src/paa/ledger/store.py`,
`tests/coder/test_recovery_chaos.py`, `01-work-ledger.md`, `COMMS.md` —
sibling/ORCH-owned.
