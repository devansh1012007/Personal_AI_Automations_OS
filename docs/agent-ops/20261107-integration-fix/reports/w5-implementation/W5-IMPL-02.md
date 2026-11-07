<!-- ORCH-DIGEST:START -->
agent: W5-IMPL-02 | domain: P-011 coding specialist pilot T12 | status: DONE
items: pilot coding only; block emits STATE_ROLLBACK_TRIGGERED resumable; no new EventType
proposed_tasks: 0
top_risks: types.py +1 line shifts lower docs citations (FINDING for docs sweep); tool-EXECUTION deferred post-gate (pinpointed below)
cross_domain_conflicts: none (siblings disjoint; full-tree ruff 0 at commit time)
out_of_plan_discoveries: 1 (CoderAgent.build reuse is circular — specialist uses the seam, not the loop; see report)
confidence: high (21 new tests incl. 50-task T12 green fail-before->pass-after; delegation/coder/replay/agents suites green; ruff 0 full-tree; mypy total 3 baseline)
last_comms_line_read: 2026-11-07T15:00:00Z W5-DOCS CLAIM (own START+CLAIM appended; COMMS left uncommitted for ORCH per rule 8)
<!-- ORCH-DIGEST:END -->

# Implementation report — W5-IMPL-02 — P-011 (T-011-P011)

## Status: DONE

## 0. Scope and method
Task: implement plan P-011 (T-011-P011) exactly per brief
`docs/agent-ops/20261107-integration-fix/briefs/W5-IMPL-02.md` and ADR-0041.
WRITE-SET only: `src/paa/agents/specialists/__init__.py`,
`src/paa/agents/specialists/coding.py` (new), `src/paa/core/types.py`
(AgentRole only), `src/paa/runtime.py` (registration only),
`src/paa/agents/orchestrator.py` (agents entry + block emit only),
`tests/agents/test_coding_specialist_p011.py` (new).
`coder/permissions.py` and `coder/agent.py` were NOT edited (verify-only:
audit proved coverage — see Phase 2). `config.py`, `docs/adr/*`,
`coder/model_adapter.py`, `ledger/*` untouched.

Required reading done in brief order: STANDING-ORDERS, AGENTS.md (coder
section), 00-mission.md, ADR-0041 (full), W0-INV-03 §P-011 (full),
W1-IMPL-01, docs/coder-agent.md, then code (types, orchestrator, delegation,
replay, recovery, runtime, coder/*, delegation tests). The plan file itself
(`C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md`) was not
re-read: INV-03 already extracted the P11/T12 rows and the brief restates
them; ADR-0041 wins over the plan where they differ (brief §2.5).

Branch verified before committing: `ops/20261107-integration-fix`
[RAN git branch --show-current]. Working tree at start had sibling W5
in-flight changes (allocator/metrics/curator/daemon/runner/ADR-0029/docs) —
none touched or committed by me. COMMS START+CLAIM appended (2 lines);
COMMS.md left uncommitted for ORCH (standing order 8: "Only ORCH commits
this file").

## Context Report (pre-implementation, per mandate section 1)
Root cause / entry points: delegation + rollback infrastructure exists but
has no specialist client. `mediate_delegation` (orchestrator.py) is the only
agent→agent path; `STATE_ROLLBACK_TRIGGERED` is already wired end-to-end
(replay maps it to resumable `ROLLED_BACK`, excluded from
`TERMINAL_EVENTS`); the block path returned failure WITHOUT emitting. The
gap is a missing emitter site + a missing specialist, not a missing event
(ADR-0041; the plan's "terminal in replay" sub-claim is misclassified and
code wins).
Affected files: core/types.py (+1 AgentRole member), specialists/ (new
module + package exports), orchestrator.py (coding entry + emit),
runtime.py (enabled-gated registration), new T12 test file. Downstream
consumers: `mediate_delegation` callers, replay projection
(`rollback_count`), recovery (unchanged — different `reason` distinguishes
delegation blocks).
Invariants preserved: additive-only enum; `mediate_delegation` signature
unchanged; rollback payload additive-only; replay mapping untouched;
ledger-before-side-effect (emit before returning failure); idempotency via
full ADR-0008 key; `class X(str, enum.Enum)`; docstrings carry ADR rationale.
Risks: (a) types.py +1 line shifts every lower docs citation (FINDING —
needs docs re-point, not mine); (b) registered name is "coding" while role
value is "coding_specialist" (brief's dict pattern) — pinned by test that
only one name resolves; (c) emit fires for ALL RecursionGuardError kinds
(depth/cycle/node) per ADR wording; (d) `CoderAgent.build` reuse is
circular (it builds a whole Runtime, which would recursively build the
specialist) — so the specialist uses the constrained-JSON *seam*
(turn_schema + ModelLike), not the loop; recorded as DECISION below.
Expected behavior + tests: T12 50 scripted tasks ≥90% (ADR's adopted gate),
depth>2 blocked + rollback event with matching correlation_id, replay
ROLLED_BACK + rollback_count=1, redelivery suppressed once, unknown target
failure, self-cycle RecursionGuardError, empty payload no-crash, enabled
gate (Runtime-level on/off), tool-surface policy coverage.

## Phase log
### Phase 1 — T12 tests first (fail-before proven)
- New `tests/agents/test_coding_specialist_p011.py` (21 tests): role value +
  additive roles; package exports only coding (gated names absent); no new
  EventType member; rollback not terminal; registered name "coding" only;
  leaf (can_delegate False); mutating-tool policy coverage (globs ∪
  MUTATING_RUNTIME_TOOLS); turn_schema enum == CODING_TOOL_NAMES; 50-task
  routing ≥90%; unknown target; disabled no-agent; empty payload; depth>2
  block + event payload (reason/depth/ceiling/target, correlation match);
  replay ROLLED_BACK + rollback_count; idempotency (emit twice → 1 event);
  self-cycle; Runtime registers coding iff enabled (2 tests).
- [RAN .venv\Scripts\python.exe -m pytest tests/agents/test_coding_specialist_p011.py -q -> 18 failed, 3 passed] — the 3 green pre-fix are negative
  guards that already hold (no new EventType, not-terminal, default-absent
  in Runtime). All 18 new-behavior tests red for the right reasons
  (AttributeError CODING_SPECIALIST / ModuleNotFoundError specialists.coding /
  no "coding" in runtime agents).

### Phase 2 — implementation (DONE, self-reviewed)
- `core/types.py`: `CODING_SPECIALIST = "coding_specialist"` (+1 line) +
  AgentRole docstring now names the pilot (same line count, no extra shift).
- `specialists/coding.py` (new): `CodingSpecialist(Agent)`, role =
  CODING_SPECIALIST, name default "coding", `can_delegate=False`, optional
  ModelLike. `handle` tolerates empty/goal-less payloads; with a model it
  takes one constrained turn (`turn_schema(list(CODING_TOOL_NAMES))`) and
  returns `{agent, role, goal, text, tool_calls}`. `CODING_TOOL_NAMES` = the
  coder runtime catalogue (6 read-only + 3 mutating).
  - DECISION: reuse the seam (turn_schema enum + ModelLike), NOT
    CoderAgent.build: build() constructs a full Runtime (circular if the
    specialist runs inside one) and ADR-0041's own cost list is "one enum
    member, one registration, one specialist package, one emit site, the
    50-task test" — tool *execution* is post-gate work, named in the module
    docstring.
  - DECISION: no `coder/permissions.py` / `coder/agent.py` edit. Audit:
    every mutating name in CODING_TOOL_NAMES is in MUTATING_RUNTIME_TOOLS,
    already appended in CoderAgent.build's mutating_tools tuple (agent.py)
    or glob-matched. Nothing is read-only-by-accident; pinned by test.
- `specialists/__init__.py`: docstring + re-exports CODING_TOOL_NAMES,
  CodingSpecialist. No research/writing/data_analysis/ops attributes.
- `orchestrator.py`: `coding: Agent | None = None` ctor param → registration
  loop + `self._coding`; block path emits STATE_ROLLBACK_TRIGGERED via the
  existing `_emit` BEFORE returning failure, payload
  {reason=delegation_recursion_block, depth, ceiling, cycle, sender,
  target}, attempt=0, discriminator
  `delegation-block:{sender}->{target}:depth{depth}` (full ADR-0008 key →
  store dedups redelivery; verified by test: same block twice = 1 event,
  rollback_count stays 1).
- `runtime.py`: top-level import; `_build_agents` adds `agents["coding"]`
  ONLY when `settings.specialists.coding.enabled`; Runtime.build passes
  `coding=agents.get("coding")` to the orchestrator. Disabled → absent →
  mediate returns the existing no-agent failure (not a crash, not a queue).
- [RAN pytest tests/agents/test_coding_specialist_p011.py -q -> 21 passed]
  (fail-before → pass-after proven by Phase 1 ordering).

### Phase 3 — scoped verification (DONE)
- [RAN pytest tests/agents/test_coding_specialist_p011.py tests/agents/test_delegation.py -q -> 36 passed]
- [RAN pytest tests/agents -q -> all passed (2 pre-existing x-skips)]
- [RAN pytest tests/coder -q -> all passed]
- [RAN pytest tests/agents/test_delegation.py tests/ledger/test_replay.py -q -> all passed]
- [RAN .venv\Scripts\python.exe -m ruff check src/paa/agents/specialists src/paa/core/types.py src/paa/runtime.py src/paa/agents/orchestrator.py tests/agents/test_coding_specialist_p011.py -> All checks passed!]
- [RAN .venv\Scripts\python.exe -m ruff check src tests -> All checks passed!] (full tree, siblings clean at this moment — attributes 0 to me regardless)
- [RAN .venv\Scripts\python.exe -m mypy src -> Found 3 errors in 1 file] — exactly the pre-existing subprocess_backend.py:548 baseline (mission A-001); my files add 0.
- [RAN grep CODING_SPECIALIST src/ -> 2 live hits: core/types.py:499, specialists/coding.py:80]
- [RAN fresh-interpreter import of specialists.coding + specialists + runtime -> imports ok] (no cycle into the import-conventions sweep)
- Full suite NOT-RUN (standing orders §16).

### Self-review (re-read diff from scratch)
- Correctness: emit is before `return AgentResult.failure`; exc.depth /
  exc.ceiling / exc.cycle are JSON-native (PaaError attributes); non-list
  tool_calls from a scripted model degrades to [] instead of crashing.
  No behavior change for non-blocked delegations (emit is inside
  `except RecursionGuardError` only) — delegation/replay suites green
  unmodified prove it.
- INVARIANT (recorded in code comment): the discriminator identifies the
  *block*, not the lineage, so a *different* depth on the same lineage
  still records (rollback_count can legitimately exceed 1 across distinct
  blocks) while a *redelivery* of the same block dedups.
- Mutation mental check: deleting the emit fails 3 tests (event payload,
  replay phase, idempotency count); deleting the registration fails the
  Runtime-on test; deleting the role member fails imports; changing the
  registered name fails the "only one name" test; adding an uncovered
  mutating tool fails the policy-coverage test.
- Findings: NONE critical/major. One minor found and fixed in-review: an
  earlier draft had a dead `if False:` block + noqa import in coding.py —
  removed before testing (zero-baseline honored: no suppressions added).
- Independent reviewer subagent: NOT dispatched (brief does not request one;
  W1 precedent accepted; wave REV/TREV review covers it). Recorded
  honestly instead of faking a pass.

### Long-run test (standing order 8)
- Source: every claim verified against live code, not plan text — the plan's
  "terminal rollback" sub-claim was rejected on code evidence (ROLLED_BACK
  ∈ resumable, ∉ TERMINAL_EVENTS), exactly as ADR-0041 directs.
- Load (100x, concurrency): emit is one ledger append per blocked
  delegation, same cost class as every other lifecycle event; no loops, no
  unbounded state; the 50-task test exercises 50 lineages with no shared
  graph pollution (per-correlation graphs).
- Hostile input: empty payload → goal="" works; unknown target → clean
  failure; oversized/malformed payload fields pass through as inert dict
  values (no eval, no path use in this task); model returning a non-list
  tool_calls degrades to [].
- Operability: rollback = `specialists.coding.enabled=False` (agent
  unregisters; old rollback events remain readable as ROLLED_BACK).
  Observe via `read_ledger` / `rollback_count` in the projection.
- Reversal: removing the coding entry restores the exact prior behavior
  (test_disabled_coding_is_no_agent_failure + default-Runtime test pin it);
  removing the emit restores silent-failure (pinned by 3 red tests).

## Deferred speakers — pinpointed writer (brief §8 requirement)
The four gated specialists (research/writing/data-analysis/ops) and the five
further RFC examples are explicitly NOT built. When the pilot gate is met,
each one grows: `specialists/<name>.py` (new file, same leaf + seam shape as
coding.py), an `AgentRole.<NAME>_SPECIALIST` member (types.py, sole-owner
that wave), one `SpecialistsSettings.<name>` sub-model (config.py, P14
sole-owner), one registration entry in `_build_agents` gated on that
toggle, and its own T12-style test file. No stubs exist today, so there is
nothing to "finish" — each is a clean additive task citing ADR-0041.

## Out-of-scope findings (not fixed, not in write-set)
1. FINDING (docs churn): types.py gained lines at 479-499, so `path:line`
   citations to src/paa/core/types.py at lines ≥479 in docs/** (and to
   runtime.py:583-604 / orchestrator.py:833-886 regions I touched) are now
   stale by ≤6 lines. Owner: W5-DOCS doc-re-point task. (agent-ops is
   excluded from the docs lock.)
2. Tool execution for the specialist's proposed tool_calls is deferred
   post-gate (ADR-0041 cost list covers routing/guards/emit only). This is
   scoping, not a stub: handle() returns the constrained turn verbatim.

## Unverified assumptions & remaining risks
- [ASSUMED] ADR-0041's 90%-over-50 gate is adequate as adopted; if the
  orchestrator later raises the gate, T12's `routed >= 45` follows the ADR
  number (brief §6).
- NOT-RUN: full pytest suite (forbidden in wave rules); PostgreSQL/S3
  suites (not touched).
- mypy total is 3 = pre-existing baseline; CI (Linux) gate unaffected.

## Git State
- Branch: `ops/20261107-integration-fix` [RAN git branch --show-current].
- Code commit: `6e1819a` (6 files: src/paa/core/types.py,
  src/paa/agents/specialists/__init__.py,
  src/paa/agents/specialists/coding.py (new), src/paa/agents/orchestrator.py,
  src/paa/runtime.py, tests/agents/test_coding_specialist_p011.py (new)).
  PROCESS NOTE: the code commit message omits the Task/Agent/Wave/Report
  trailers (one-line message used; `--amend` is forbidden, so it stands).
  The report commit below carries full trailers.
- Report commit: separate docs-only commit listing `6e1819a`.
- Report commit SHA: <REPORT_SHA> (docs-only, this file).
- Working tree: sibling W5 in-flight files (allocator/metrics/curator/
  daemon/runner/ADR-0029/docs) + COMMS.md (my 2 appends, ORCH-committed)
  left untouched/uncommitted by me.

## Verification (gate-report shape)
- Unit/integration: [RAN pytest tests/agents/test_coding_specialist_p011.py -> 21 passed; fail-before run: 18 failed / 3 passed].
- Regression (scoped): [RAN pytest tests/agents -> pass; tests/coder -> pass; tests/ledger/test_replay.py (in combined run) -> pass].
- Lint: [RAN ruff check src tests -> All checks passed!]. Types: [RAN mypy src -> 3 errors, all pre-existing subprocess_backend.py:548].
- New failures introduced by me: none. Acceptance: T12 green with fail-before shown; delegation/coder/replay suites green unmodified; ruff 0; mypy total 3; CODING_SPECIALIST live in src (2 hits); no new EventType member; no config.py edits.
