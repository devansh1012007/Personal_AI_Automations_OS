<!-- ORCH-DIGEST:START -->
agent: W2-REV-04 | scope: Wave 2c T-N2-3 recovery (aac0a48) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 1 | MINOR 3 | NIT 2
claims_audit: 14 claims checked / 2 unsupported-or-false
band_aids_found: none
top_3_risks: MODEL_ESCALATED defeats coder exemption (phantom task requeue); resume scan limit evicts newest sessions; absent-file resume verdict always unknown
optimizations: DO-NOW 1 | DO-LATER 1 | NOT-WORTH-IT 2
reversal_check: reasoned-only (read-only mandate; mutation probe not re-run)
confidence: high (probes executed against real store; scoped suites green)
<!-- ORCH-DIGEST:END -->
# Code review — W2-REV-04 (T-N2-3 recovery, commit aac0a48)

## 0. Method
Blind pass on the 4-file diff (`recovery.py`, `session.py`, `agent.py`, `test_recovery_chaos.py`) read against ADR-0046 semantics (`_NO_PHASE_CHANGE`, MODEL_ESCALATED writers) before opening the W2-IMPL-05 report. Then: ran the scoped suites myself, wrote two throwaway red-team probe scripts (deleted after; source untouched), then audited the implementer's claims against the code.

## 1. Blind-pass findings
The core design is sound: PROPOSED-minus-outcome detection keyed on `(session_id, call_id)`, golden v1 FAILED closure with a deduping discriminator, classification as a pure function of persisted intent, exemption as a sweep branch rather than replay surgery, and the resume-time verdict refinement that joins ledger hashes to the transcript path through the workspace jail. Three things smelled wrong blind: (a) `MODEL_ESCALATED` is phase-neutral but absent from `_CODER_EVENT_TYPES`, so any coder lineage that escalated — or an orphan escalation lineage — reads as task work; (b) `find_interrupted_calls_for_session` fetches `events_since(... limit=10_000)` ordered ASC and filters by session *after* the cut, so the bound evicts new work, contradicting its own docstring; (c) `_resume_entry` only sets `live_checked=True` when the target file exists, so an absent creation file can never be `not_applied` at resume even though `classify_interrupted` supports it. All three confirmed by probe (§5).

## 2. Claims audit
- **PROPOSED-without-outcome closed as FAILED(reason=interrupted), golden v1 shape, idempotent** — SUPPORTED [VERIFIED src/paa/ledger/recovery.py:449-485; test_recovery_chaos.py:406-448; RAN scoped suite green].
- **applied/not_applied/unknown classification from persisted intent** — SUPPORTED with disclosed deviation: precise verdicts exist only at resume time (sweep is `UNKNOWN` by construction, `live_checked=False`); deviation §12.3 of the impl report is honest and the ADR-0046 v1 freeze (§12.1) justifies the missing `state` field. [VERIFIED recovery.py:298-352, agent.py:312-358].
- **D-6 never auto-repeats unknown** — SUPPORTED: nothing in the diff re-executes a call; unknown paths degrade to note text only [VERIFIED agent.py:352-358, session.py:173-207].
- **Coder-only lineage exemption, never requeued** — **FALSE in the general case** (see MAJOR-1): holds only for lineages whose event set is exactly the 8 ADR-0046 types; a `MODEL_ESCALATED` row collapses it. The impl report never considers this shape.
- **Kill -9 simulation loses/repeats nothing** — SUPPORTED [VERIFIED test_recovery_chaos.py:485-524; RAN green; second sweep appends nothing, chain verifies].
- **DENIED-alone is proof of non-execution, never interrupted** — SUPPORTED, probe P2 [RAN -> interrupted=0, SKIPPED_CODER_LINEAGE].
- **`--resume` injects classification note** — SUPPORTED [VERIFIED agent.py:264-309; RAN tests incl. applied/not_applied/unknown parametrization].
- **Unknown reported not retried** — SUPPORTED (note text; no re-execution path exists).
- **"bounded, newest-first not required" (§10) / §14 oldest-first story** — **FALSE as documented**: the bound is oldest-first, so it strands *new* sessions, not old ones (probe P5). The code docstring's "newest-first protection is the caller's" (recovery.py:427) is wrong.
- **mypy 3 pre-existing / ruff 0** — SUPPORTED for the 3 files [RAN ruff All checks passed; RAN mypy Success on the 3 source files]. Full-suite numbers [ASSUMED from report].
- **Docs-references failure** — was true at report time; since swept by ORCH (fa43a3e): [RAN tests/conventions -> 34 passed].
- **Mutation probe / reversal** — [ASSUMED]: not re-run (read-only mandate); exact-count assertions make the claim plausible.

## 3. Long-run test (independent answers)
- **Source:** ledger idempotency is delegated to the store's `(correlation_id, discriminator)` dedupe — the same contract golden FAILED rows already rely on. New rows are phase-neutral audit events, so replay state cannot move. [VERIFIED recovery.py:466-484].
- **Load (100x/concurrency/partial failure):** per open lineage at boot: one extra `read_correlation` + one append per interrupted call. Crash mid-sweep re-closes idempotently. The resume scan is the only unbounded-ish path: `events_since` with a 10k ASC cut, then a Python filter — at 100x tool-call volume the newest sessions silently miss their note (MINOR-1). No locks, no shared mutable state beyond the session's own turn list.
- **Hostile input:** malformed payloads are skipped by type-checked reads, never raised [VERIFIED recovery.py:355-413]. The resume join is jailed before any read (`assert_inside_workspace`, absolute/`..` paths reject to `UNKNOWN`) [VERIFIED agent.py:328-343]. One whole-file hash read at resume is a disclosed pre-existing exposure shape; nothing new introduced.
- **Operability:** observers get `CorrelationRecovery.interrupted` / `RecoveryReport.interrupted_count` and `FAILED(reason=interrupted)` rows. Rollback = revert code commit (one-way door for ADR-0046 rows thereafter, as documented). No flag, no migration.
- **Reversal:** reasoned only — the closure/exemption tests assert exact counts, row counts, and exact-dict payload equality against a real store, so reverting `close_interrupted_calls` or the coder-only branch must fail 4+ tests for the right reason.

## 4. Findings

**MAJOR-1 — `MODEL_ESCALATED` defeats the coder-lineage exemption; escalated coder lineages are requeued as tasks.** `src/paa/ledger/recovery.py:264-275` (`_CODER_EVENT_TYPES` excludes `EventType.MODEL_ESCALATED`), `:488-497` (subset test), `:662-679` (branch). `CoderAgent.build` passes `correlation_id` to the model adapter (`agent.py:211`), and the router writes `MODEL_ESCALATED` onto that lineage whenever reasoning escalates (`models/router.py:854-863`). Probe P1 [RAN: coder lineage + MODEL_ESCALATED -> RESUMED, requeued=1]; probe P3 [RAN: orphan MODEL_ESCALATED (the CLI's correlation_id=None path, router.py:831-841) -> RESUMED, requeued=1]. Consequences: every boot requeues a phantom task for any lineage that ever escalated, and one phantom per orphaned escalation — the exact failure N2.3 exists to prevent, confined to the branch, since closure still works everywhere. Suggested minimal fix: add `MODEL_ESCALATED` to `_CODER_EVENT_TYPES` (task lineages still carry `TASK_*` events, so they stay non-exempt), plus a regression test with an escalation-polluted lineage and an orphan-escalation lineage. Read-only mandate: not applied.

**MINOR-1 — Resume scan limit evicts the newest sessions; docstring claims the opposite.** `src/paa/ledger/recovery.py:430-446`: `events_since` is `ORDER BY sequence_id ASC LIMIT ?` (`store.py:376`), so at >10k tool events the *newest* interrupted calls are cut before the session filter. Probe P5 [RAN: limit=2 over 4 events -> target's interrupted call found=0]. The sweep still closes the call lineageside, so impact is a missing resume note (the model doesn't know), not a lost closure. Fix: order DESC / filter in SQL / page, or at minimum correct the docstring.

**MINOR-2 — Absent creation file at resume reads `unknown`, never `not_applied`.** `src/paa/coder/agent.py:328-343`: `live_checked` is set only when the target exists. Probe P4 [RAN: creation with no file -> "unknown — do not re-run"]. `classify_interrupted` already handles `(pre=None, live=None) -> NOT_APPLIED` (recovery.py:340-347); the resume path just never exposes it. Safe direction under D-6, but tells the model not to retry a call that provably never landed.

**MINOR-3 — No regression coverage for the escalation shape.** `tests/coder/test_recovery_chaos.py` builds coder lineages without `MODEL_ESCALATED`, which is why MAJOR-1 shipped green. Add: coder lineage + `MODEL_ESCALATED` -> SKIPPED; orphan `MODEL_ESCALATED` alone -> SKIPPED; absent-file resume classification.

**NIT-1 — Resume without `--workspace` silently downgrades file verdicts.** `agent.py:328` (`workspace is not None` gate): correct but invisible; the note could name *why* no verdict was possible. D-6-safe, operability-only.

**NIT-2 — `InterruptedCall.state` on sweep returns stays `UNKNOWN` forever.** The refined verdict lives only in the note's text (`agent.py:358`); report consumers see `UNKNOWN`. Declared in the dataclass docstring, so documented, but a `state` refresh at resume would make the report the single source of truth.

## 5. Attempts to break it
- **P1 (escalated coder lineage):** TOOL_CALL_PROPOSED + MODEL_ESCALATED -> RESUMED + requeued. Confirms MAJOR-1.
- **P2 (DENIED-alone):** -> interrupted=0, SKIPPED_CODER_LINEAGE. Hook-block proof of non-execution holds.
- **P3 (orphan MODEL_ESCALATED):** -> RESUMED + requeued. Same root cause as P1, via the CLI path.
- **P4/P4b/P4c (resume classification):** applied ✓ (P4b), absent creation -> unknown (MINOR-2), no workspace -> unknown (NIT-1).
- **P5 (scan limit):** target missed under ASC limit (MINOR-1).
- Pushed on malformed-payload, stale-sweep-row, double-sweep, mixed-lineage, and torn-hash paths only by reading tests — all pinned by existing tests [RAN: 59 passed in tests/coder/test_recovery_chaos.py + tests/ledger/test_recovery.py]. Red-team probes were throwaway scripts, deleted; no source files touched.

## 6. Dimension checklist
- **Correctness:** closure + classification + mixed-lineage path correct; exemption incomplete (MAJOR-1); scan bound wrong-headed (MINOR-1).
- **Security:** jail-before-hash on resume; no content/path bytes in new ledger rows; malformed rows can't halt the sweep; no new trust surface. The `(tool, args_sha256)` join never guesses — redacted calls degrade to `UNKNOWN`, the safe direction.
- **Performance:** boot/resume-time only; one extra read per lineage; one append per interrupted call. No hot-path cost.
- **Maintainability:** the `live_checked` parameter honestly encodes the ledger-vs-resume information boundary; docstrings carry the rationale. `InterruptedCall.state` defaults make the UNKNOWN-by-default visible.
- **Tests:** real-store, exact-count/payload assertions, chaos kill drill, parametrized verdicts. Missing: escalation shape, limit behavior, absent-file resume (MINOR-3).

## 7. Optimization and simplification opportunities
- **DO-NOW (1):** fold `MODEL_ESCALATED` into the coder set (MAJOR-1 fix) — five lines.
- **DO-LATER (1):** give the store a session-scoped tool-call query (indexed) instead of scan+filter for resume; only pays off past 10k events.
- **NOT-WORTH-IT (2):** manifest-scan classification at sweep time (ambiguous by design, correctly rejected); per-call replay indexing (nothing hot to fix).

## 8. Cross-agent interactions
- ADR-0046 v1 freeze respected (no `state` field) — deviation §12.1 documented and correct.
- N2.2's shape-summary preview is *why* sweep-time verdicts are `UNKNOWN` — the boundary is now honestly documented in `classify_interrupted`.
- `cli/coder.py` fresh-correlation-on-resume (impl report §13) is out of write-set; the session-id scan is the right workaround, but it is exactly what MINOR-1's bound threatens.
- Orchestrator docs sweep already landed (fa43a3e): conventions 34 green.

## 9. What I could not verify
- Full-suite / cross-task results beyond the scoped runs [RAN: tests/coder/test_recovery_chaos.py + tests/ledger/test_recovery.py -> 59 passed; tests/conventions -> 34 passed; ruff 0; mypy 3-file clean]. Ledger-wide, mandate, agent/loop/journal suites [ASSUMED from impl report].
- The impl report's mutation probe (reversal) — not re-run under the read-only mandate; reversal is reasoned-only.
- PostgreSQL/S3-backed paths — CI-only by design (A-001), not run.

Verdict: **APPROVE-WITH-CHANGES**. Core mechanism is correct and well-tested; MAJOR-1 (escalation defeats the exemption) should be fixed before this is called done, since the exemption is a headline acceptance criterion and the escalated-lineage shape is the routine one.
