<!-- ORCH-DIGEST:START -->
agent: W2-IMPR-01 | scope: F-1 (MAJOR) — a second human-gate request in one lineage is swallowed by the ledger's idempotency key, and the task commits with it unanswered | status: DONE
findings: FIXED 1 | REJECTED 0 | DEFERRED 0
blockers_major_open: none
code_commits: c3e14fd | report_commit: (this commit)
tests_added_or_strengthened: 14 (11 fail without the fix; 3 are guards that hold in both trees)
ownership_violations: none
confidence: high (the defect is measured on the pre-fix tree and reproduced through the real runtime; the design is derived from the ledger's own idempotency contract, and seven plausible wrong implementations are each caught by a named test)
<!-- ORCH-DIGEST:END -->

# W2-IMPR-01 — F-1: a human gate needs an identity

## 0. What this was

F-1 of `reports/w2-review/W2-REV-01-code.md`, MAJOR. The orchestrator emitted
`AWAITING_HUMAN_ATTESTATION` at two sites with **no discriminator and no
attempt**, so every gate request in one lineage hashed to the same
`correlation | event_type | attempt | discriminator` key
(`src/paa/ledger/events.py:101`). The second request was therefore
*suppressed* as a duplicate: the projection never saw it, the orchestrator saw
no pending gate, and the task carried on to `MUTATION_COMMITTED` with the
policy's risk gate unanswered.

This is a **re-dispatch**. The interrupted dispatch left an uncommitted
design in `src/paa/agents/orchestrator.py` (+228/−34). Per the brief, that
draft is **untrusted input**, not a starting truth. §2 is the audit: what I
kept, what I changed and why, and what I discarded.

**Status: DONE.** The defect is fixed, the ADR is corrected, and the fix is
pinned by 14 new tests, 11 of which fail against the pre-fix sources.

---

## 1. Finding-by-finding disposition

| Finding | Severity | Disposition | What I did / why | Commit |
|---|---|---|---|---|
| **F-1** — a second human-gate request in one lineage is swallowed; the task commits with the gate unanswered | MAJOR | **FIXED** | The gate event now carries the *question's identity* (a discriminator minted at the emit site); the release names the question it answers; each stage consults **its own** question; a human's release of the policy stage's question is recorded as the policy decision, so the policy agent is not asked a second time. ADR-0034 amended. | `c3e14fd` |
| — the interrupted dispatch's draft | — | **audited, partly kept, partly rewritten** | §2 | `c3e14fd` |
| — ADR-0034's "What this ADR does not decide" | — | **corrected** | It described the case wrongly: the release did not "work only because the policy agent happens to be deterministic" — the second gate was *dropped* and the task **committed**. The paragraph is corrected in place and the mechanism is recorded in an amendment. | `c3e14fd` |
| — `_apply_backpressure` degrades the modality on **every** submit | out of scope | **reported, not fixed** | `orchestrator.py::_apply_backpressure` calls `degrade_modality` unconditionally — the observed queue depth is read and then never used, so a task submitted through a full runtime is degraded one rung whatever the load. Pre-existing, not F-1, and out of the brief's scope; posted as a FINDING. | — |

---

## 2. The audit of the interrupted draft

The draft was **+228/−34** in `src/paa/agents/orchestrator.py`, uncommitted
(a copy was also at `/tmp/paa-w2-improve-backup/orchestrator.py`). It had
**no tests at all**. I read it, measured it, judged it, and rebuilt what
survived.

### 2.1 What the draft got right — and I kept

1. **The core insight: a gate request is a *question*, and a question has an
   identity.** The draft introduced a `_GateQuestion` dataclass, a
   `_gate_discriminator` that mints `gate:<role>:<ordinal>`, and a
   `_gate_history` that reads the lineage rather than the projection. This is
   the right shape and it is the right *reasoning* — the discriminator counts
   the questions a stage has had **answered**, not the ones it has asked,
   which is what makes the identity stable across a redelivery and distinct
   across questions. I kept the idea, the naming and the argument, and
   re-derived the argument in my own words in the docstring (§4.1).
2. **The ordinal's basis.** Counting *answered* questions rather than *asked*
   ones is the subtle part, and the draft had it right. A counter that moved
   on every write would record a redelivery as a second question; a counter
   that moved on every *ask* would do the same. Only the answered-count
   keeps a redelivery the same request.
3. **The release names the question it answers.** `HUMAN_GATE_CLEARED`
   carrying the question's discriminator is right: it makes the ledger say
   which of two questions the human ruled on, and it keeps two releases from
   colliding on the key.
4. **The per-stage gate history.** Replacing "the last gate event in the
   lineage" with "this stage's own question" is right, and it is what the
   brief asks for ("Read the projection and the release path and make the
   two agree"). A lineage with two questions cannot be described by its last
   gate event.

### 2.2 What I changed — and why

| The draft | What I did | Why |
|---|---|---|
| The two emit sites each minted the discriminator inline (`self._gate_discriminator(questions, ROLE)` at both call sites) | Extracted **one** emit site, `ChiefOrchestrator._open_gate(ctx, role, payload)`, which reads the lineage, mints the identity and writes the event | Two copies of "how a question is identified" is two things to forget to update. The draft's own version also computed the ordinal from a list read at the *start* of the stage, so the identity was computed *before* the agent ran; `_open_gate` computes it at the moment of the write, which is the moment it has to be right. |
| The draft left `state_version` on `_GateQuestion` | Removed it | Nothing read it. The list is already in lineage order (`read_correlation` guarantees `state_version` order), so the index *is* the order. |
| The draft's `POLICY_CLEARED`-on-release payload was `{"decision", "released_by", "gate"}` | Added `"reason": "human attestation granted"` | `apply_event` reads `payload["reason"]` into `policy_reason` (`ledger/replay.py:215`), which the API exposes. A release with no reason leaves the projection's reason empty. |
| The draft's comments were written as if the release were a formality | Rewritten to state the *rule* and the hazard | The house style: a comment says why, not what. |
| The draft had **zero** tests | 14 new tests (§5) | The brief's largest remaining item, and the standing orders' "tests are part of the work". |

### 2.3 What I discarded

Nothing in the draft was *wrong*; two things were **incomplete**, and I
completed them rather than keeping them:

* **No tests.** Nine of the eleven new unit tests and both new integration
  tests fail against the pre-fix sources. The draft's design was plausible on
  paper and I have no evidence it held — the same position the W1-IMPL-02
  dispatch was in, and the reason the brief told me to audit rather than
  trust.
* **The `POLICY_CLEARED` emitted on the human's release was unexplained.**
  The draft emitted it; I kept the emission (it is the reviewer's own
  suggestion — "record the policy's verdict so `_check_policy` is not
  re-consulted after a release") but the *reason* is now written down: the
  human's ruling **is** the policy decision, the policy agent's second
  opinion is not what the human approved, and re-asking would ask the human
  the same question a second time — the loop ADR-0034 records.

### 2.4 One thing the draft got *more* right than the review suggested

The reviewer's suggested fix was "a per-lineage gate ordinal, or the gate's
identity — `"context"` vs `"policy"`". The draft used **per stage**, not per
lineage. That is the better choice and I kept it: a per-lineage ordinal
would have to be read from the whole lineage and would repeat under
concurrency (the reviewer's own F-4 against the router's escalation
ordinal), and the stage is the *subject* of the question anyway — "is the
context sufficient?" and "is this plan safe to run?" are different questions
by construction.

---

## 3. The defect, measured

Reproduced on the pre-fix sources (`git show HEAD:src/paa/agents/orchestrator.py`
in a shadow tree, `PYTHONPATH` pointed at it), driving the real
`ChiefOrchestrator` over a real `LedgerStore`:

```
phase after run 1: TaskPhase.AWAITING_HUMAN
[debug] ledger.duplicate_suppressed  event_type=AWAITING_HUMAN_ATTESTATION
events: 1 TASK_REQUESTED  2 TASK_QUEUED  3 CONTEXT_HYDRATION_REQUESTED
         4 AWAITING_HUMAN_ATTESTATION  5 HUMAN_GATE_CLEARED  6 PLAN_COMPILED
         7 EXECUTION_STARTED  8 CRITIQUE_CONCLUDED  9 EXECUTION_COMPLETED
        10 MUTATION_COMMITTED
phase after resume: TaskPhase.COMMITTED
gate count: 1 [None]
```

The context builder's question was asked and answered; the policy agent's
question was **dropped by the ledger**; the task committed. The audit trail
says the policy never asked.

And through the **real runtime** (the real gatherer, the real
`PolicyRiskAgent`), after the fix:

```
  4 AWAITING_HUMAN_ATTESTATION  role=context_builder_planner disc='gate:context_builder_planner:1'
  5 HUMAN_GATE_CLEARED           role=chief_orchestrator    disc='gate:context_builder_planner:1'
  6 PLAN_COMPILED                role=strategic_planner
  7 AWAITING_HUMAN_ATTESTATION   role=policy_risk           disc='gate:policy_risk:1'
phase after resume: TaskPhase.AWAITING_HUMAN
```

Two questions, two records, and the task parks again.

---

## 4. The code changes

### 4.1 `src/paa/agents/orchestrator.py`

**A question has an identity.** The gate event's discriminator is
`gate:<role>:<ordinal>`:

* **the role** — the stage that asked. The role is the *subject* of the
  question, and two stages asking are two different questions by
  construction.
* **the ordinal** — one more than the number of that stage's questions that
  have been **answered**. This is the whole trick, and it is worth stating
  precisely:
  * a *redelivery* of a question nobody has answered is the **same**
    request: nothing has been answered, so the ordinal does not move, the key
    does not change, and the store suppresses the redelivery;
  * a genuinely *new* question can only be asked after the previous one was
    answered (a stage does not ask again while its question is open), so the
    ordinal has moved and the new question is a different request with a
    different key.

  Counting the questions *asked* would break the first rule, and counting
  every write would break it too. §5's mutation battery measures both.

**The release names the question it answers.** `HUMAN_GATE_CLEARED` and
`HUMAN_GATE_REJECTED` carry the discriminator of the question they rule on,
and the payload carries `gate` and `gate_role`. A release that names no
question (a lineage written before the discriminator existed) answers the
question it follows, which is the only one that can be open.

**Each stage consults its own question.** `_gate_history` reads the lineage
and returns the questions in order; `_stage_question` picks the one the
stage asked. The stage then acts:

* the question is **open** → the stage does not act. The context builder is
  not asked again (wave 2's rule (a): a builder that changed its mind would
  otherwise emit a fresh `CONTEXT_HYDRATED` and walk the task past a gate no
  human released), and the policy agent is not asked again (the same hazard,
  one stage later: a second opinion is not an answer, and must never replace
  a question for a human with a clearance no human saw);
* the question is **answered** → the stage does not act again. For the
  context builder that is wave 2's rule (b). For the policy agent it is new,
  and it is what stops the human being asked the same question twice.

**A human's release of the policy stage's question is the policy decision.**
The orchestrator records `POLICY_CLEARED` (`decision: STATUS_APPROVED`,
`released_by: human`, `gate: <discriminator>`) so the projection carries the
decision and the stage is skipped from then on. The gate itself is still
released by the human: `HUMAN_GATE_CLEARED` is written only by
`clear_human_gate`, and nothing in the automated path calls it. The
human-only rule is intact, and §5 pins it.

**The release path and the phase map agree.** The release names the phase
the task was working towards: `PLANNING` for a question asked during
hydration (the task has not planned yet), `EXECUTING` for one asked during
the policy review. Before the amendment the release always asserted
`EXECUTING`, which is a phase a context-gate task had not reached.

**One emit site.** `_open_gate(ctx, role, payload)` is where a gate is
opened. Both stages go through it, so the identity of a question is minted in
one place and the two sites cannot drift apart.

### 4.2 `docs/adr/0034-hard-stop-escalation-parks-the-task.md`

* The amendment at the end of the record: what was wrong, the mechanism, what
  it costs, what it does not change, how to roll it back.
* The "What this ADR does not decide" paragraph is **corrected in place** —
  it said the release "works only because the policy agent happens to be
  deterministic", and the measured behaviour is the opposite and worse: the
  release did not work at all, and the task committed.
* Pointers in §3 and §4 where the mechanism they describe has changed, in the
  house style of ADR-0032's corrections.

The ADR's decisions 1–5 all still stand; the amendment adds to them.

### 4.3 `src/paa/ledger/**` — untouched

The brief makes it read-only, and the fix did not need it: `discriminator`
is already a field of `LedgerEvent`, already an input of the idempotency
key, and already the ledger's own documented answer to "same-typed events
that are legitimately concurrent" (`ledger/events.py:90`). **No NEED posted,
no DEFERRED-DEPENDENCY.**

---

## 5. The test changes

Fourteen new tests: eleven in `tests/agents/test_orchestrator.py` (ten in a
new `TestGateIdentity` class, one in `TestExactEventSequence`) and two in
`tests/integration/test_end_to_end.py` (a new `TestHumanGate` class).

### 5.1 What fails without the fix — measured

`PYTHONPATH` pointed at a shadow tree holding `HEAD:src/paa/agents/orchestrator.py`:

```
FAILED tests/agents/test_orchestrator.py::TestExactEventSequence::test_the_released_gate_path_emits_the_exact_event_sequence
FAILED tests/agents/test_orchestrator.py::TestGateIdentity::test_a_second_gate_in_one_lineage_is_recorded_and_parks_the_task
FAILED tests/agents/test_orchestrator.py::TestGateIdentity::test_each_gate_question_carries_its_own_identity
FAILED tests/agents/test_orchestrator.py::TestGateIdentity::test_a_release_names_the_question_it_answers
FAILED tests/agents/test_orchestrator.py::TestGateIdentity::test_a_task_parked_on_a_second_gate_is_not_walked_past
FAILED tests/agents/test_orchestrator.py::TestGateIdentity::test_an_answered_gate_does_not_block_the_task
FAILED tests/agents/test_orchestrator.py::TestGateIdentity::test_the_gate_request_minted_for_a_question_is_the_same_request
FAILED tests/agents/test_orchestrator.py::TestGateIdentity::test_the_policy_stage_does_not_consult_its_agent_while_its_question_is_open
FAILED tests/agents/test_orchestrator.py::TestGateIdentity::test_the_release_resumes_the_task_where_it_parked
FAILED tests/integration/test_end_to_end.py::TestHumanGate::test_a_second_question_parks_the_task_again
FAILED tests/integration/test_end_to_end.py::TestHumanGate::test_the_two_questions_carry_two_identities
```

**11 of 14 fail without the fix.** The three that hold in both trees are
guards, and I say so rather than pretending otherwise:

* `test_a_redelivery_of_the_same_gate_request_is_suppressed` — the brief
  demands that the *deduplication* property survive the fix. It holds in the
  pre-fix tree too (a key with no discriminator is also stable), so it is a
  guard on the fix's own shape; §5.3 shows what it catches.
* `test_the_open_policy_gate_holds_the_task_against_a_policy_agent_that_changes_its_mind`
  — wave 2's parking property, extended to the policy stage. The pre-fix
  tree also holds it, because `run()`'s phase check stops the run first.
* `test_a_rejected_gate_ends_the_lineage` — a regression guard on the
  release path I changed.

### 5.2 The two white-box tests, and why they are white-box

Two tests reach into the emit site on purpose, and both say so in their
docstrings:

* `test_the_gate_request_minted_for_a_question_is_the_same_request` calls
  `ChiefOrchestrator._open_gate` twice with the same request. The public
  flow *cannot* re-enter the emit site — a stage does not ask again while
  its question is open — so the property the fix rests on ("the same
  question is the same request") is only observable where the identity is
  minted. The repo does this elsewhere (`tests/agents/test_policy.py` calls
  the gate's own `_render`).
* `test_the_policy_stage_does_not_consult_its_agent_while_its_question_is_open`
  calls `_check_policy` directly. `run()`'s phase check fires first, so the
  stage-level guard underneath it is unreachable through the public flow —
  see §6.

### 5.3 The mutation battery — the suite pins the *shape* of the fix

Each mutation was applied to a copy of the tree and the suite re-run. All
seven are caught, and each by a test whose name says what it protects:

| mutation | caught by |
|---|---|
| **M1** the discriminator is a fresh `uuid4` per write (a nonce, the router's shape) | `test_a_redelivery_of_the_same_gate_request_is_suppressed`, `test_the_gate_request_minted_for_a_question_is_the_same_request` |
| **M2** the ordinal counts the questions *asked*, not the ones *answered* | `test_the_gate_request_minted_for_a_question_is_the_same_request` |
| **M3** the discriminator is the stage only (no ordinal) | `test_the_gate_request_minted_for_a_question_is_the_same_request` |
| **M4** the release does not name the question it answers | `test_a_release_names_the_question_it_answers`, `test_the_release_resumes_the_task_where_it_parked` |
| **M5** the policy agent is consulted again after a human ruled | `test_an_answered_gate_does_not_block_the_task`, `test_the_released_gate_path_emits_the_exact_event_sequence`, `test_the_policy_stage_does_not_consult_its_agent_while_its_question_is_open` |
| **M6** the open gate does not hold the stage (the policy stage re-asks) | `test_the_policy_stage_does_not_consult_its_agent_while_its_question_is_open` |
| **M7** the resume phase is always `EXECUTING` (the pre-fix default) | `test_the_release_resumes_the_task_where_it_parked` |

M1–M3 are the three ways to get the *identity* wrong; M4–M7 are the four
ways to get the *gate* wrong.

### 5.4 The event lists

The brief warns: *"a discriminator changes the event's **key**, not its event
type or order — if it changes an event list, the list was wrong and the test
must say so."*

**No existing event list changed.** The five exact-list tests the wave-2
suite added (`happy path`, `failure path`, `block path`, `anti-goal block`,
`gate path`, and the hard-stop escalation path) all pass unchanged.

One list is **new** — `test_the_released_gate_path_emits_the_exact_event_sequence`
— and it has one event the pre-fix list did not: `POLICY_CLEARED`. The test
says why: the pre-fix trace had none, because the policy stage's clearance
was never written down (the second `AWAITING_HUMAN_ATTESTATION` was
swallowed, so the stage "cleared" by way of a question that was never
asked). The stage's decision is now recorded, and its payload says who made
it.

### 5.5 The integration tests drive the real thing

`tests/integration/test_end_to_end.py::TestHumanGate` runs the reviewer's
own measurement through the **real** composition root: the real gatherer
(the task declares slots it cannot fill, so the real density is 0 and the
real context builder escalates), the real `PolicyRiskAgent` (the plan
carries a step over the always-gate risk profile, so the real gate asks),
the real ledger, the real runtime. Both integration tests fail against the
pre-fix tree.

---

## 6. Residual risks, and what the reviewer should check

1. **The stage-level guard in `_check_policy` is unreachable through
   `run()`.** `run()` checks the phase after every stage, and a parked task
   stops there — so the branch that stops the policy stage from consulting
   its agent while its question is open never fires in the current pipeline.
   I kept it because it is the *precise* statement of the rule ("a stage
   whose question is open does not act") under the *coarse* one ("the task
   is parked"), because it is what protects the stage if anything is ever
   written after a gate event, and because `_hydrate` has the same branch
   (where it **is** reachable and load-bearing). It is tested by a direct
   call, and the test says why. **A reviewer who thinks it should be deleted
   should read M6 first: deleting it re-opens the hazard.**
2. **Two `read_correlation` reads per run** (one per stage that runs, plus
   one on the gate path). The orchestrator already re-reads and replays the
   whole lineage several times per run. Recorded in the ADR.
3. **The `POLICY_CLEARED` on the human's release** is written with
   `agent_role=policy_risk` and `released_by: human` in the payload. A reader
   filtering by role sees the *stage's* record; the payload says who
   decided. I judged the alternative (role `chief_orchestrator`) worse: the
   policy stage's series of decisions would then have a hole in it.
4. **A lineage written before the amendment** is read by the same
   projection. A gate event with no discriminator is a question whose
   identity is unknown, and the release path falls back to "the question it
   follows" — the only one that can be open in a lineage that has one.
5. **The two-question case now needs two humans** (or one human twice).
   That is the point, and it is a visible change: a task that used to
   commit with a swallowed question now waits.
6. **Out of scope, reported not fixed:** `_apply_backpressure` degrades the
   modality on **every** submit — the observed queue depth is read and then
   never used (`orchestrator.py::_apply_backpressure`). It is pre-existing
   and not F-1; a FINDING is posted in COMMS.
7. **Not verified here:** the PostgreSQL substrate. The fix writes no SQL and
   changes no schema, and the parity lock (`tests/storage/test_schema_parity.py`)
   is untouched by it.

---

## 7. Commands run (scoped)

```
$ .venv/bin/python -m pytest tests/agents tests/integration -q
165 passed in 5.38s                             # 151 before, +14 new

$ .venv/bin/python -m ruff check src/paa/agents tests/agents tests/integration
All checks passed!

$ .venv/bin/python -m mypy src/paa/agents
Success: no issues found in 8 source files

$ .venv/bin/python -m ruff check src tests
All checks passed!

$ .venv/bin/python -m mypy src
Success: no issues found in 137 source files
```

**Reversal** (the pre-fix orchestrator in a shadow tree, the new tests
against it): **11 failed**, listed in §5.1.

**Mutation battery**: 7 mutations, 7 caught, listed in §5.3.

---

## 8. The long-run answers

* **Source** — the identity is minted in one place (`_open_gate`), from the
  lineage, not from a counter that lives in the process. A restart cannot
  change it, and two processes minting it concurrently mint the *same* one
  (the store's key is what arbitrates).
* **Load / concurrency** — two orchestrators delivering the same task both
  ask the same question and both write the answer; the store suppresses the
  second. Pinned by
  `test_a_redelivery_of_the_same_gate_request_is_suppressed`, whose barrier
  makes the interleaving deterministic (both orchestrators read the lineage
  before either writes, so both write the *same* request).
* **Hostile input** — the discriminator is derived from the role enum and an
  integer; nothing a model or a user supplies reaches it. The release path
  refuses to release a gate the lineage never opened.
* **Operability** — the ledger says which question was asked, which stage
  asked it, and which question the human ruled on; the discriminator is the
  join key between the question and its release. The projection says the
  phase the task resumes at and who decided the policy. Rollback: revert the
  amendment (§4.2).
* **Reversal** — 11 of the 14 new tests fail against the pre-fix sources;
  seven mutations of the fix's shape are each caught by a named test.
