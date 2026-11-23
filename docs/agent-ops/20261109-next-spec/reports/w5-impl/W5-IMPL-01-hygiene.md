<!-- ORCH-DIGEST:START -->
agent: W5-IMPL-01 | task: T-C5-4 (C5.4) | status: DONE
code_commits: none (orchestrator commits after review) | report_commit: pending
files_changed: 5 (src/paa/validation/engine.py, src/paa/validation/ast_scanner.py,
  tests/sandbox/test_docker_backend.py, tests/validation/test_engine.py,
  tests/validation/test_ast_scanner.py) + this report
contracts_changed: none frozen (new ScannerPolicy.max_ast_depth field is additive;
  empty-artifact verdict change is the filed TASK-3 acceptance, not a silent one)
tests: added 5, changed 3, removed 0 strict-xfails; scoped runs all green
  (docker 107, engine 20, ast_scanner 185, patch 52, policy 61+1 xfail by design)
ownership_violations: none (write-set per brief; docs other than this file untouched)
deferred_dependencies: TASK-5 (operator decision, re-filed with owner); H1.5 async
  delivery half (re-filed with owner); docs cite re-number for my 2 source files
  (pre-declared central task, not done here)
out_of_scope_findings: 3 (conventions blank-cite failures split between my line
  shifts and the sibling C5.2 write-set; sibling in-flight ruff ASYNC109 x3; a
  transient shared-tree red window on test_docker_backend.py mid-C5.2, resolved
  by the sibling before my final run)
temporary_mitigation: no
deep_issue_flag: no (each fix verified by reversal; TASK-4 backstop verified manually)
least_confident_about: the H1.6 mapping (PAA covers the prototype's five fault
  classes at architectural equivalents, not at the prototype's literal surfaces)
confidence: high (every disposition backed by a green scoped run executed myself)
last_comms_line_read: 2026-11-22T00:00:00Z (WAVE-START)
<!-- ORCH-DIGEST:END -->
# Hygiene sweep disposition — W5-IMPL-01 — T-C5-4 (C5.4), wave 5, 2026-11-22

Task: spec `docs/plans/2026-11-21-next-spec-2.md` §4 C5.4 — close or explicitly
re-schedule the ledger's own deferred list (TASK-1..5 from the N0-5 triage,
`reports/w0-investigation/N0-5-triage.md` §7), and verify-or-file H1.5 (async
tool-call delivery) and H1.6 (lifecycle fault tests). Acceptance: **each item
DONE or re-filed with an owner** — this document is that deliverable, one
section per item. No commit is made here; the orchestrator commits after review.

## 1. Method and what was actually verified myself

- Read the filing triage (N0-5 §4.5, §6.1-6.4, §7) and the W1 follow-ups first:
  W1-IMPL-04 already fixed TASK-2 (its report §5/§7 records the one-expression
  fix and the un-xfailed verifier); W1-IMPL-05 §13 records TASK-1 as
  deliberately left. Every claim below was re-verified in this working tree,
  not inherited: each fix was run against its own tests, and each re-filed item
  was re-run to confirm the pinning tests and strict xfails still stand.
- Baseline before my edits: `pytest` over the five affected files
  (`tests/sandbox/test_docker_backend.py`, `tests/validation/test_engine.py`,
  `tests/validation/test_ast_scanner.py`, `tests/validation/test_patch.py`,
  `tests/agents/test_policy.py`) = **417 passed, 3 xfailed** — the three
  strict xfails being exactly TASK-3, TASK-4, TASK-5.
- This host: Linux, uid 1001 (non-root), `os.getuid` available — the TASK-1
  test passes here today; it was still made robust per the filed intent.

## 2. Per-item disposition

### TASK-1 — getuid test robustness — **DONE**

- Filed: N0-5 §4.5 — `test_user_is_the_workspace_owner_not_root` assumes a
  non-root POSIX host; fails on Windows (no `os.getuid`) and under root (the
  workspace is root-owned and the never-root rule must refuse the owner).
- Change (`tests/sandbox/test_docker_backend.py` only; no product change, as
  the triage specified):
  1. The real-host test keeps its exact assertion
     (`--user == f"{os.getuid()}:{os.getgid()}"`) but is now guarded by a
     `skipif` whose reason *names the assumption* ("the workspace created here
     is owned by the test process: under a root run it is root-owned and the
     never-root fallback answers instead of the owner, and on Windows there is
     no os.getuid to name"). Nothing it asserts was weakened.
  2. A new host-independent sibling
     `test_user_is_the_workspace_owner_whatever_the_host` monkeypatches
     `_workspace_owner` to a fixed non-zero pair (the sibling pattern the
     triage named) and asserts `--user == "1234:2345"` — the owner-to-flag
     mapping is now tested on Windows and root too, not skipped away. This is
     the triage's preferred option (b) on top of its option (a) guard.
- Verified: `pytest tests/sandbox/test_docker_backend.py` = **107 passed**
  (106 before + 1 new test; 0 skipped on this host, so both the guarded
  real-identity test and the new mapping test ran). Reversal: deleting the
  guard reproduces the filed failures only on Windows/root hosts; deleting the
  monkeypatched test removes the only all-hosts coverage of the mapping.

### TASK-2 — patch phantom newline — **DONE (verified, fixed in W1)**

- Fixed by W1-IMPL-04 (commit b97ff73): `UnifiedDiffValidator._plan_file` at
  `src/paa/validation/patch.py:573` — `original_lines = normalised.split("\n")
  if normalised else []` — an empty prior file is zero lines, not one phantom
  line. Re-verified present in this tree by reading the line.
- Verifier run now: `pytest tests/validation/test_patch.py` = **52 passed**,
  including `test_a_created_file_contains_exactly_what_the_diff_describes`
  with no xfail marker (the strict xfail was removed in the same commit as the
  fix, exactly as the task required; a strict xfail would have failed the build
  otherwise). `compute_patch_sha256` records the corrected bytes — pinned by
  the W1 report's own test evidence and unchanged here.
- No change made by me; the item closes on the W1 evidence plus today's
  re-run.

### TASK-3 — empty-artifact PASS — **DONE (fixed here)**

- Filed: an artifact for which no check ran reported PASS, contradicting the
  `ValidationArtifact` docstring ("an empty artifact cannot quietly PASS by
  having nothing to check"); the real caller path is `CriticValidatorAdapter`
  (`src/paa/runtime.py:122` forwards `passed=bool(report.passed)`), which maps
  a worker's output dict onto an artifact — an output with no `source_files`,
  `patch` or `payload_schema` came back approved.
- Fix (`src/paa/validation/engine.py`): in
  `DeterministicValidationEngine.validate`, after the checks and the tests
  block, `if not report.checks_run: report.verdict = ValidationVerdict.FAIL`.
  The refusal is the absence of evidence, not a fabricated failing row:
  `failed_checks` stays empty (no check ran and failed) and every check's skip
  reason survives — the accounting tells an operator *why* nothing ran. The
  module and artifact docstrings now state the rule instead of implying only
  its visibility.
- Tests (`tests/validation/test_engine.py`): the strict xfail above
  `test_an_artifact_with_nothing_to_check_does_not_pass` is removed — it now
  passes and additionally asserts `failed_checks == []` (the refusal is the
  emptiness itself). New `test_an_all_skipped_report_still_records_its_skip_
  reasons` pins that the FAIL verdict does not rewrite the accounting. The
  filed re-check holds: `test_a_skipped_check_is_recorded_with_its_reason`
  (all-skipped-but-counted) is green, as is every check-enabling test
  (`run_tests=True` artifacts still PASS on a passing suite).
- Verified: `pytest tests/validation/test_engine.py` = **20 passed**
  (19 passed + 1 strict xfail before). Reversal: reverting the two-line verdict
  block makes the un-xfailed test fail again.

### TASK-4 — AST walk RecursionError — **DONE (fixed here)**

- Filed: `AstSecurityScanner.scan` guarded `ast.parse` against
  `RecursionError` but not the `NodeVisitor.walk` after it — a 2000-deep
  attribute chain parses and then exhausts the stack inside
  `generic_visit`, so the scanner *raised* on exactly the hostile input it
  exists for: no verdict at all. The triage also flagged the deeper point:
  relying on the interpreter's recursion limit is relying on a host property.
- Fix (`src/paa/validation/ast_scanner.py`), the recursion-limit-independent
  formulation the triage asked for:
  1. `ScannerPolicy.max_ast_depth` (default 256, `ge=1`) — the bound is now a
     policy number an operator can reason about. 256 sits far above
     human-written nesting and below the ~500 visit-stack levels the default
     recursion limit affords, so the guard fires by policy before the stack is
     at risk.
  2. `_SecurityVisitor.generic_visit` counts the descent and raises the
     module-private `_AstDepthExceeded` (carrying node + depth) past the limit.
  3. `scan` catches it and answers with a CRITICAL `ast_depth_exceeded`
     finding naming the file, the depth reached and the configured limit —
     findings, not exceptions, preserving the module's own contract (the
     enforced, raising form is `scan_or_raise`, which now raises
     `SecurityScanError` with this payload). The findings the walk had already
     collected ride along: they are still true statements about the part the
     walk did see.
  4. Backstop: `visitor.visit(tree)` is additionally wrapped in the same
     `(ValueError, RecursionError, MemoryError)` guard as `ast.parse`, for the
     host whose interpreter limit sits below the policy's, or a policy limit
     raised past it — same rule, second site. Verified manually (not as a
     committed test, because it depends on the host's recursion limit):
     with `max_ast_depth=10**9` the 2000-chain raises inside the walk and is
     answered with the `unparseable_source` CRITICAL finding, exactly the old
     crash mode now caught.
- Tests (`tests/validation/test_ast_scanner.py`): the strict xfail above
  `test_deeply_nested_source_does_not_escape_as_a_recursion_error` removed —
  it passes. Three new tests: `test_the_depth_finding_names_the_file_and_the_
  depth` (the refusal carries file, depth and limit — the numbers an operator
  acts on), `test_the_depth_limit_is_configurable` (a lowered limit refuses
  shallower nesting and reports *that* limit), and
  `test_nesting_below_the_limit_is_not_flagged` (200-deep ordinary expression
  syntax still yields no findings — the guard is not a false-positive machine).
  The rule inventory (`EXPECTED_RULES`, `RULE_SOURCES`) gained
  `ast_depth_exceeded`, so the inventory lock and the reachability sweep cover
  the new rule like every other. `test_the_scanner_never_raises_on_hostile_
  source` stays green over the whole hostile corpus.
- Verified: `pytest tests/validation/test_ast_scanner.py` = **185 passed**
  (181 passed + 1 strict xfail before; +1 un-xfailed, +3 new). Reversal:
  reverting the guard returns the pinned test to crashing with the raw
  `RecursionError` (the strict xfail's own reason documents that crash).

### TASK-5 — LOCKDOWN refusal order — **RE-FILED (owner: the operator)**

- Reason: this is the one item the ledger itself forbids a wave agent from
  closing. AGENTS.md ("Two policy decisions are open... Do not 'fix' either
  without the operator's word") and the N0-5 filing ("**Do not fix without
  the operator's word**") both record it as an open *policy* decision, not a
  defect: a LOCKDOWN plan that both declares an ungranted permission and
  trips the air-gap check is refused either way — only which refusal's
  *reason* lands in the `POLICY_BLOCKED` ledger payload differs. Deciding
  "which refusal should win" in code, inside a hygiene sweep, would be
  exactly the silent decision the repo's honesty rules exist to prevent.
- Evidence that the deferral cannot be forgotten: it is double-pinned in
  `tests/agents/test_policy.py` — `test_a_declared_egress_permission_is_
  refused_by_the_permission_check` asserts the current reason
  ("permissions not granted in LOCKDOWN: ['PERM_NET_EGRESS']"), and the
  strict xfail `test_the_lockdown_air_gap_refusal_names_the_egress` pins the
  proposed end state ("egress/external-write requested under LOCKDOWN") and
  fails loudly the day the reorder lands without the decision being made.
- Verified now: `pytest tests/agents/test_policy.py` = **61 passed,
  1 xfailed** (the xfail is the filed tripwire, present on purpose).
- Next action (owner: the solo maintainer/operator): pick (i) move
  `_check_lockdown` ahead of `_check_permissions` in the checks tuple at
  `src/paa/agents/policy.py:164` so the air-gap reason answers, or (ii) delete
  the unreachable declared-permission half of `_check_lockdown`; land the
  decision, the xfail removal, and the companion-reason update in one commit,
  so the ledger-payload reason never changes unexplained. No code was touched
  here.

### H1.5 — async tool calls with cancellation — **SPLIT: cancellation VERIFIED,
async delivery RE-FILED (owner: the solo maintainer)**

ROADMAP H1.5 has three halves. What exists and is verified by tests I ran:

- *Cancel kills the process tree and writes a ledger event* — VERIFIED:
  `tests/sandbox/test_cancellation.py` (**4 passed**):
  `test_cancel_kills_the_tree_leaving_no_orphan`,
  `test_cancel_kills_the_tree_without_a_timeout` (a cancelled run stops
  parent and grandchild, no orphan survives to hold capacity),
  `test_cancel_reraises_promptly_without_a_result`,
  `test_cancel_reaps_the_process_without_resource_warnings`.
  `tests/coder/test_cancellation.py` (**5 passed**):
  `test_cancel_during_tool_run_journals_failed_and_reraises`,
  `test_cancel_before_any_call_writes_no_outcome`,
  `test_normal_completion_is_unchanged`,
  `test_cancel_writes_a_failed_row_to_the_real_ledger` (the cancel is an
  outcome row, so the recovery sweep cannot misclassify it as a crash),
  `test_bash_reraises_cancel_promptly` (the 600-second stand-in is never
  waited out; the cancel lands far inside the 5-second budget).
  The chat turn's cancel is covered too: `tests/chat/test_loop.py`
  (**18 passed**) `TestCancelMidStream::test_cancel_records_cancelled_with_
  partial_reply` and `test_recorded_cancel_resolves_the_proposal_for_the_
  sweep` (`CHAT_TURN_CANCELLED` with the partial reply attested — never
  FAILED, never a hang).
- *Long commands return immediately; the loop accepts new input mid-run;
  ACP cancel works* — RE-FILED. Not implemented, and a hygiene sweep is not
  the place to invent it: the coder loop awaits each tool call inline
  (`src/paa/coder/loop.py:195`, `executed = await self._handle_call(call)`),
  there is no background tool-call delivery, no mid-run input queue, and ACP
  v1 is W7/C7.6 (the W4 docs-truth pass already removed the false
  "experimental ACP server" claims — gap 5). Reason for re-filing rather than
  writing it: it is a loop-architecture change (effort **L** in the ROADMAP),
  not a missing test, and small focused tests cannot verify a feature that
  does not exist. Next action (owner: solo maintainer): schedule the
  async-delivery half when the coder loop is next opened (the ROADMAP routes
  H1.5 into H2.1 ACP hardening; SPEC 2's W7 carries ACP v1), or re-scope the
  ROADMAP row to the cancellation-only state and file the delivery half
  explicitly. The cancellation evidence above stays as the verified half.

### H1.6 — lifecycle fault tests — **VERIFIED (at PAA's equivalents)**

The five failure classes named by the ROADMAP row come from the DeepSeek
prototype's surfaces (docs/research/2026-11-deepseek-super-agent.md §4):
PAA's architecture differs, so each class is covered at its PAA equivalent —
all suites below were run green by me today:

1. *Retry after authorisation changed* → the authorisation basis is versioned
   and re-checked: `tests/ledger/test_mandate.py` (**28 passed**) —
   `TestSubmitBehaviour::test_stale_policy_version_refuses` (a live task whose
   policy version went stale is refused, not silently continued),
   `test_strict_mode_refuses_implicit_in_lockdown`; supporting:
   `tests/models/test_router_lockdown.py` (in the 26-passed run with the
   maintenance loop) — `test_a_failing_local_under_lockdown_does_not_reach_
   the_frontier` (a retry under a changed, stricter policy cannot escalate).
2. *Stop while preparing a workspace (stopped work keeps running and holds
   capacity)* → `tests/sandbox/test_cancellation.py` (**4 passed**) — the
   `TestCancelKillsTheTree` pair: a cancelled run leaves no orphaned
   grandchild and returns promptly.
3. *Partial workspace after a failed prepare* → `tests/ledger/test_recovery.py`
   (**27 passed**) — `test_drift_triggers_rollback`,
   `test_rollback_is_recorded_in_the_ledger`, `TestCheckpointing`; and
   `tests/coder/test_recovery_chaos.py` (**38 passed**) —
   `TestClassifyInterrupted` (a partial effect is classified, never
   auto-repeated — D-6: never guess), `TestKillSimulation::
   test_state_survives_close_and_reopen`,
   `TestResumeRecovery::test_resume_reports_bash_as_unknown_and_never_retries`.
4. *Event poll that reports success while skipping items* →
   `tests/storage/test_coldlake.py` (**53 passed**) —
   `test_claim_unprocessed_is_exclusive` (two polls never return the same
   item; a poll cannot claim items it did not take),
   `test_claim_takes_the_oldest_first` (ordering is by id, not by a timestamp
   that >100 items could share — the prototype's exact failure),
   `test_mark_processed`, `test_release_returns_a_claim_to_the_pool`.
5. *Duplicate trigger* → `tests/ledger/test_store.py` (**39 passed**) —
   `TestIdempotency::test_exact_redelivery_is_suppressed`,
   `test_redelivery_returns_original_not_error`,
   `test_genuine_retry_is_recorded`; `tests/ledger/test_coder_events.py`
   (**35 passed**) — `TestIdempotencyDiscriminator::
   test_duplicate_proposed_append_returns_existing`; supporting:
   `tests/daemon/test_maintenance_loop.py` (in the 26-passed run) — duplicate
   memory merges are counted (`duplicates_merged == 1`), not reported as
   successes over skipped work.

Honest residual, stated rather than buried: there is no *single* property
harness that injects each fault into one surface and asserts it fails, because
the prototype's surfaces (Ticket prepare, GitHub Source poll, provider retry)
do not exist here — the classes are covered at the PAA shapes above. If the
gate ever demands the literal reading, that harness is the next action
(owner: solo maintainer); as it stands, each class has a green test that fails
on the injected fault's PAA equivalent.

## 3. What changed (per file)

- `src/paa/validation/engine.py` (+31 lines): the no-checks-run verdict rule
  in `validate`, with docstrings (module, artifact, method) stating it.
- `src/paa/validation/ast_scanner.py` (+103 lines): `ScannerPolicy.
  max_ast_depth`; `_AstDepthExceeded`; the depth-counting `generic_visit`;
  the guarded `visit` in `scan` (depth finding + `unparseable_source`
  backstop); docstring updates.
- `tests/sandbox/test_docker_backend.py` (+51 lines): the TASK-1 skip guard
  with named reason, plus the host-independent owner-mapping test.
- `tests/validation/test_engine.py` (+47/-20): TASK-3 xfail removed, one
  assertion added, one new accounting test, header docstring updated.
- `tests/validation/test_ast_scanner.py` (+75 lines): TASK-4 xfail removed,
  inventory + reachability entries, three new depth-guard tests, header
  docstring updated.
- This report (new).

## 4. Commands run and results (verbatim summaries)

| Command | Result |
|---|---|
| `python -m pytest tests/sandbox/test_docker_backend.py` | 107 passed |
| `python -m pytest tests/validation/test_engine.py` | 20 passed |
| `python -m pytest tests/validation/test_ast_scanner.py` | 185 passed |
| `python -m pytest tests/validation/test_patch.py` | 52 passed |
| `python -m pytest tests/agents/test_policy.py` | 61 passed, 1 xfailed (the TASK-5 tripwire) |
| `python -m pytest tests/validation` | 443 passed |
| `python -m pytest tests/agents` | 208 passed, 1 xfailed |
| `pytest tests/coder/test_cancellation.py tests/sandbox/test_cancellation.py` | 9 passed (5 + 4) |
| `pytest tests/chat/test_loop.py tests/coder/test_recovery_chaos.py` | 56 passed (18 + 38) |
| `pytest tests/ledger/test_mandate.py tests/ledger/test_store.py` | 67 passed (28 + 39) |
| `pytest tests/ledger/test_coder_events.py tests/ledger/test_recovery.py` | 62 passed (35 + 27) |
| `pytest tests/storage/test_coldlake.py` | 53 passed |
| `pytest tests/models/test_router_lockdown.py tests/daemon/test_maintenance_loop.py` | 26 passed |
| `python -m ruff check` (my five files) | All checks passed |
| `python -m mypy src/paa` | Success: no issues found in 173 source files |

## 5. Out-of-scope findings (not fixed here, per brief)

1. `tests/conventions/test_docs_references.py::test_every_referenced_line_is_
   not_blank` is red on 8 citations. Four are line shifts from *my* source
   edits (`engine.py:226` cited at docs/data/data-lifecycle.md:186 and
   docs/security/security-model-and-trust-boundaries.md:25; `ast_scanner.py:
   342` cited twice at docs/modules/validation.md:39,93); four are shifts from
   the sibling C5.2 write-set's uncommitted `src/paa/sandbox/base.py`
   (`docs/modules/sandbox.md`). The brief pre-declares the central re-number;
   I did not edit any doc outside this file, so the cites remain for that pass.
2. `python -m ruff check src tests` currently reports 3 `ASYNC109` errors,
   all in the sibling's in-flight write-set (`src/paa/sandbox/base.py:544`,
   `src/paa/sandbox/subprocess_backend.py:405`,
   `tests/coder/test_cancellation.py:129` — files I did not touch). My five
   files are clean in the same run.
3. Shared-tree transient, recorded for the timeline: mid-task, the sibling's
   in-flight abstract `SandboxSession.open` briefly made every
   `DockerSandbox(...)` instantiation (and 79 tests in
   `tests/sandbox/test_docker_backend.py`) fail with `TypeError`; the sibling
   landed the backends' `open` implementations before my final run, and the
   file is 107 passed as reported above. Not my regression — my only edit to
   that file is the two containment tests.

## 6. Deviations

- None on scope or write-set. TASK-5 was dispositioned RE-FILED rather than
  fixed — that is the acceptance's explicit second branch, and AGENTS.md
  forbids the alternative. H1.5's async-delivery half likewise.
- TASK-4: the orchestrator brief sketched "raise a typed validation error
  naming the file and depth"; the module's frozen contract is findings-not-
  exceptions from `scan` (and the pinned acceptance test asserts findings),
  so the typed, file-and-depth-naming refusal is a CRITICAL finding whose
  enforced form is `SecurityScanError` from `scan_or_raise` — the raw
  `RecursionError` escapes nowhere either way. The decision and its reason
  are recorded here rather than made silently.

## 7. Checklist

- [x] TASK-1 DONE (guard + all-hosts sibling test; 107 passed).
- [x] TASK-2 DONE (W1 fix verified present + verifier green; 52 passed).
- [x] TASK-3 DONE (empty artifact FAILs; xfail removed; 20 passed).
- [x] TASK-4 DONE (depth guard + backstop; xfail removed; 185 passed).
- [x] TASK-5 RE-FILED (owner: operator; reason: recorded open policy
      decision, AGENTS.md forbids fixing without the operator's word; next
      action: decide (i) reorder or (ii) delete, land with the xfail removal
      in one commit).
- [x] H1.5 cancellation VERIFIED (exact tests + counts above); async
      delivery RE-FILED (owner: solo maintainer; next action: schedule with
      the coder loop's next opening / W7 ACP v1, or re-scope the ROADMAP row).
- [x] H1.6 VERIFIED at PPA's equivalents (five fault classes, exact tests +
      counts above; the single-harness residual stated with an owner).
- [x] ruff clean on my write-set; `mypy src/paa` 0 issues.
- [x] No `git add`/`git commit` performed; no doc outside this file edited.
