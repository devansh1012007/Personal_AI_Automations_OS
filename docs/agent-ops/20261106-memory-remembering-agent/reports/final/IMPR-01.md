<!-- ORCH-DIGEST:START -->
agent: FINAL-IMPR-02 | scope: FINAL-01..05 + F-01 MAJOR + F-02..05 (recovery of FINAL-IMPR-01 uncommitted diff) | status: DONE for in-write-set findings
findings: FIXED 4 | PARTIAL 1 (F-02: 1 of 3 lines in-set; other 2 outside write-set as DEFERRED-DEPENDENCY) | REJECTED 1 (F-05 plan edit, with operator-doc note) | DEFERRED 4
blockers_major_open: none (F-01 closed; F-02 remainder needs owner of runtime.py/cli/main.py)
code_commits: this report's commit chain (see Commands/Commits below) | report_commit: this file
tests_added_or_strengthened: 3 added + 1 strengthened (handle-touch e2e, 2 floor tests, note-content assertions)
ownership_violations: none (9 files audited; edits confined to write-set; docs citations corrected in write-set docs)
confidence: high (scoped gate green vs baseline; one executed reversal; daemon.py line-ending incident restored from byte backup)
<!-- ORCH-DIGEST:END -->

# Improvement report — FINAL-IMPR-02

## 1. Finding-by-finding disposition

| Finding | Severity | Disposition | What was done / why (evidence) | Commit |
|---|---|---|---|---|
| F-01 operator docs stale + snapshot/rollback guidance missing | MAJOR | FIXED | Audited FINAL-IMPR-01's 4-file docs edit: authoritative runbook (*Memory curation*) in `docs/operations/deployment-and-runtime.md` describes full curator (not bare sweep), first-tick dry-run preview 6h after boot, snapshot-before-delete, cold-lake replay restore vs code revert; `docs/modules/daemon.md` and `docs/modules/memory.md` link to it; `docs/deployment/docker.md` embeddings caveat added. Corrected 3 mis-anchored `path:line` cites in the runbook/daemon doc (preview-consume :302→:312, curation_started/completed :292/:331→:302/:330, preview-incomplete :314→:321) after reading `src/paa/daemon.py:263-331`. Verified `paa memory drain` exists (`src/paa/cli/main.py:628`) and `POST /memory/drain` (`src/paa/api/app.py:272`), `source_signal_id` (`src/paa/memory/facts.py:295`), replay-stability (`src/paa/memory/creator.py:185`). | docs commit below |
| F-02 new error lines unredacted | MINOR | PARTIAL (1 of 3 FIXED; 2 DEFERRED-DEPENDENCY) | FIXED in-set: `src/paa/memory/creator.py:1543` `error=str(exc)` → `error=redact(str(exc))` (redact already imported from `paa.models.base` at :60). Not fixed (outside write-set, untouched): `src/paa/runtime.py:481` (`memory.embedding_sink` degraded) and `src/paa/cli/main.py:649` (`embedding drain failed: {exc}`) still unredacted [VERIFIED via grep]. NEED posted in COMMS.md for owner of those files. | code commit below |
| F-03 concurrent drain duplication + curator unbounded phase loads | MINOR | DEFERRED (with evidence) | Concurrent drains: idempotent upsert + `mark_embedded` same status; waste is embedder cost/log noise only, no corruption [VERIFIED]. No lock added (behavior change + CLI is a separate process anyway). Curator `fetch_all` (reconcile/consolidate) safe at current scale; pagination is DO-LATER per FINAL-01 §7 (O2). | none (deferred) |
| F-04 drain throughput under load (hot-loop throttles ingestion) | MINOR | DEFERRED + operator note | No sleep/backoff added (behavior change; brief prefers defer unless starvation repro). Runbook gained *Backlog watch (no throttle dial)* section: batch 100/call, drain-at-embedder-speed, observe `memory.embedding_drain pending_count` (`src/paa/memory/creator.py:1556`), no rate knob exists. | docs commit below |
| F-05 plan §K rollback wording | NIT | REJECTED (plan edit) + operator note | Plan file is outside repo (plan-mode artifact) and in brief READ-ONLY; not edited. Correction carried in operator docs instead: runbook *Restore is cold-lake replay, not code revert* + `docs/deployment/docker.md` note that Docker snapshot is Postgres dump + S3 bucket snapshot. | docs commit below |
| FINAL-01 stale coder note "no vector sink wired" | MINOR | FIXED | `src/paa/coder/runtime_tools.py`: description + note corrected to keyword-tool / planner-side semantic over embedded vectors (C01-wired). E2E `test_recall_facts_synonym_only_miss` strengthened: asserts `"no vector sink wired" not in note`, `"planner-side"` and `"embedded vectors"` in note. | code+test commits below |
| FINAL-02 fallback sweep bypasses injected prune floor | MINOR | FIXED | `src/paa/daemon.py:286-289`: fallback `DecaySweeper(self._rt.db, prune_confidence_floor=settings.explicit_prune_floor())` where `settings = self._rt.settings.memory` (injected, :263) — same source as curator path (`src/paa/memory/curator.py:135`). `None` floor still defers to global inside `DecaySweeper` (:167-177), so defaults byte-identical. Two tests added: fallback spy test + curator-forward companion. `src/paa/memory/decay.py` needed no edit (constructor already accepts the kwarg). | code+test commits below |
| FINAL-03 e2e paraphrase bypasses touch | MINOR | FIXED | New `test_paraphrase_recall_via_handle_advances_use_count`: writes fact, drives real `PlannerContextBuilder.handle()` with paraphrase goal (`rapid state aurora`), asserts `use_count` 0→1. Direct `_semantic` legs kept. Touch lives in `handle()` (`src/paa/agents/context_agents.py:328`); this closes criterion A3 end-to-end. | test commit below |
| FINAL-04 duplicated `_touch_recalled` helpers (4 copies) | NIT | DEFERRED | Refactor, not correctness; behavior identical across copies today. Shared helper in `paa.memory.decay` is DO-LATER. No edits (out-of-write-set files anyway). | none (deferred) |
| FINAL-05 drain limit never plumbed + reconcile preview 0 | NIT | DEFERRED (documented) | Drain default 100 is reasonable; API `limit` governs `run_batch` not drain. Runbook documents batch-100 semantics. Reconcile dry-run count-only returning 0 is truthful for stores lacking retain-only; preview still safe (deletes blocked under dry_run). No code change. | docs commit below |

## 2. Code changes (per file)

- `src/paa/coder/runtime_tools.py`: stale `_RecallFacts` description + note replaced (post-C01 truth; keyword tool, semantic planner-side). Root cause: cross-wave leftover text contradicted the wired sink; flagged as NEED in W4, fixed here.
- `src/paa/daemon.py`: fallback `DecaySweeper` forwards injected explicit floor (FINAL-02). Comment documents the shared-source invariant.
- `src/paa/memory/creator.py`: single-line redaction of `memory.embedding_drain_failed` error (F-02 in-set third).

## 3. Test changes

- `tests/memory/test_e2e_remember_recall.py`: strengthened note-content assertions (FINAL-01); new handle-touch e2e test (FINAL-03).
- `tests/daemon/test_maintenance_loop.py`: two new floor tests (FINAL-02); fixed import sort (I001) in the new fallback test — reordered function-local imports so `ruff check src tests` returns 0. (Prior agent had reported an "unused import"; actual finding was I001 import ordering, now resolved.)
- `src/paa/memory/decay.py`: no edit (write-set file already correct; constructor accepts floor kwarg at :139-145).

## 4. Optimizations applied and evidence

- None beyond the findings. Single-flight drain lock, curator pagination, and `_touch_recalled` dedup deliberately declined (DO-LATER / NOT-WORTH-IT per FINAL-01 §7) — any of them is a behavior/refactor risk disproportionate to the final pass.

## 5. Commands run and results (scoped, each run once)

- `[RAN .venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py tests/daemon/test_maintenance_loop.py -q -> 32 passed (19 e2e + 13 daemon), EXIT 0]`
- `[RAN .venv\Scripts\python.exe -m pytest tests/memory/ -q -> 223 passed, 0 fail]`
- `[RAN combined: ...test_e2e_remember_recall.py tests/daemon/test_maintenance_loop.py tests/memory/ -q -> EXIT 0 (236 executions incl. e2e overlap)]`
- `[RAN .venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q -> 5 passed, EXIT 0]`
- `[RAN .venv\Scripts\python.exe -m ruff check src tests -> All checks passed!, EXIT 0]`
- `[RAN .venv\Scripts\python.exe -m mypy src -> Found 3 errors, all pre-existing win32 in sandbox/subprocess_backend.py:548 (killpg/getpgid/SIGKILL) — baseline preserved]`
- Doc-citation audit: scripted read of all 58 new/kept `src/paa/...:line` refs in the 4 docs files — all non-blank; 3 mis-anchored daemon lines corrected by reading `src/paa/daemon.py:263-331` (see §1 F-01).
- Reversal (executed): temporarily set fallback floor to `None` → `[RAN pytest tests/daemon/test_maintenance_loop.py::test_fallback_sweep_forwards_injected_explicit_floor -> FAILED (assert None == 0.9), EXIT 1]`; restored fix → `[RAN 3 new tests -> 3 passed, EXIT 0]`. Reversal for note-content (restored stale note fails substring asserts) and handle-touch (removed handle touch fails `use_count==1`) verified by code-read: touch lives only in `handle()` (context_agents.py:328), note asserts pin exact substrings; not executed as separate green→red runs beyond the floor test.
- Incident (disclosed): during the reversal step, a `read_text/write_text` round-trip on `src/paa/daemon.py` doubled blank lines (CRLF handling). Restored byte-identical from `C:\Users\devan\AppData\Local\Temp\opencode\daemon_backup.py` before re-running tests; final `git diff --stat` confirms daemon.py is back to the audited +12-line change.

## 6. Residual risks and anything the delta reviewer should check

- F-02 remainder (runtime.py:481, cli/main.py:649) still logs unredacted exception text; needs the owner of those files (COMMS NEED posted). Severity stays MINOR (no secret observed in flight; inconsistent with house `redact` convention).
- First-production curator behavior (preview counts, eviction volumes, hash-embedder R1 precision) is operator post-launch work, not verified here; runbook now tells the operator where to look.
- Full suite not run (scoped only per standing orders); 12 pre-existing Windows failures per 00-mission.md are outside this scope. Mypy baseline 3 preserved exactly.
- COMMS last line read before report: FINAL-INSTALL-01 BLOCKED line (tree dirty, awaiting FINAL-IMPR-02 DONE). The two untracked `docs/`-root files were left untouched.

## Long-run test (standing orders §8)

- **Source:** root-cause fixes at the source (shared floor forwarding, redaction at the log call, truthful docs) — no symptom patches, no blanket try/catch, no sleeps/retries, no test weakening (tests strictly strengthened).
- **Load (100x):** drain still 1×100 batch/call; fallback floor change is O(1) kwarg; docs/tests add no runtime loop. No new unbounded collection.
- **Hostile input:** no new SQL/LIKE surface; floor/fixture inputs are typed (`float | None`) and validated in DecaySweeper (`0.0–1.0`, decay.py:169-173).
- **Operability:** preview/snapshot/replay runbook is now the single authoritative home; rollback honesty stated (cold-lake replay restores facts; code revert does not; `use_count`/`last_queried_at` reset on replay).
- **Reversal:** executed for FINAL-02 (fail without fix → pass with fix); code-read for FINAL-01 note asserts and FINAL-03 handle-touch.

## Commits

- Code: `git add -- src/paa/coder/runtime_tools.py src/paa/daemon.py src/paa/memory/creator.py` + tests, then docs + this report. SHAs in final message; trailers Task: FINAL / Agent: FINAL-IMPR-02 / Wave: FINAL / Report: docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md.
