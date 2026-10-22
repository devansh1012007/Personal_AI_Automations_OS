# ADR-0034: A hard stop parks the task until a human rules

## Status

Accepted

*Amended 2026-10-22 by W2-IMPR-01 (the wave-2 improve phase), after the
wave-2 code review found that this record mis-described the case it said it
did not decide. A lineage can ask a human **twice** — the context builder's
question and then the policy agent's — and the ledger could not tell the two
questions apart: the gate event carried no identity, so both hashed to the
same idempotency key and the second was swallowed. The task then committed
with the policy's risk gate unanswered. The amendment gives a question an
identity, makes the release name the question it answers, and makes each
stage consult **its own** question. The decisions below all stand; the
amendment is read with them (see the Amendment at the end of this record).*

## Context

RFC §2.1(1) gives the Chief Orchestrator the job of routing and escalation;
RFC §9 and the context-builder contract give it a *routing directive* it can
receive back from the context builder. One of those directives is
`HARD_STOP_ESCALATE_TO_USER`: the context builder has decided that the
context it was given is too thin to plan from, and that a human has to look
before the task goes any further.

The directive's name says **stop**. The event it produced said the same thing —
`AWAITING_HUMAN_ATTESTATION`, the same event the policy gate emits when it
asks for approval.

The orchestrator did not stop.

`ChiefOrchestrator.run()` checked the task's phase for a park **once**, after
`_check_policy`. An escalation raised earlier in the pipeline — during
hydration, which is the first stage — was therefore never inspected. The
orchestrator wrote the escalation event, and the *next* event it wrote
(`PLAN_COMPILED`) moved the projection's phase off `AWAITING_HUMAN` and on to
`POLICY_REVIEW`. From there the pipeline ran to the end:

```
TASK_REQUESTED → TASK_QUEUED → CONTEXT_HYDRATION_REQUESTED →
AWAITING_HUMAN_ATTESTATION → PLAN_COMPILED → POLICY_CLEARED →
EXECUTION_STARTED → CRITIQUE_CONCLUDED → EXECUTION_COMPLETED →
MUTATION_COMMITTED
```

and the task ended `COMMITTED`.

Measured on this repository at `02f3636` (the pre-change sources, reproduced
by driving `ChiefOrchestrator` with fakes over a real `LedgerStore`): the
escalated task emitted ten events and finished `COMMITTED`. The escalation was
advisory where its name, its event type and the RFC all said it was a stop.

The phase map was not the culprit. `ledger/replay.py::_PHASE_MAP` maps
`AWAITING_HUMAN_ATTESTATION → TaskPhase.AWAITING_HUMAN`, so the projection
lands in the right phase — *provided the escalation is the last event*. The
proof is a task with **no planner and no policy agent**: with nothing left to
run after the escalation, the projection stayed `AWAITING_HUMAN` and the task
parked correctly. The defect was that the orchestrator kept writing events
after the escalation, and never looked at the phase again.

What made it dangerous is not the wasted work. It is that a task which had
been escalated to a human — which is precisely a task whose plan must not be
guessed at — was then planned, executed and **committed** without anyone
being asked. The gate was not a gate.

## Decision

**`HARD_STOP_ESCALATE_TO_USER` parks the task, and the existing human-gate
machinery releases it. No second mechanism is introduced.**

### 1. A park is honoured wherever it is raised

`run()` checks the task's phase after **every** stage that can raise a park,
not only after `_check_policy`. The hydration stage now has its own check:

```python
state = await self._hydrate(state, ctx)
if state.phase is TaskPhase.AWAITING_HUMAN:
    return TaskOutcome(ctx.correlation_id, state.phase, state)
```

The rule is the one the policy gate already followed: a stage that parks the
task ends the run, and the caller gets a `TaskOutcome` whose phase is
`AWAITING_HUMAN`. `TaskOutcome.ok` is `False` for a parked task, because
`ok` means `COMMITTED`.

### 2. The release mechanism is the one that already existed

`clear_human_gate(approved=…)` emits `HUMAN_GATE_CLEARED` and moves the task
out of `AWAITING_HUMAN`. Nothing in the automated path calls it — that is
what makes the event mean something, and it is unchanged. The escalation
therefore reuses the *same* machinery the policy gate uses rather than
inventing a parallel one, and the two kinds of park are indistinguishable to
the machinery that releases them.

A rejected gate (`approved=False`) emits `HUMAN_GATE_REJECTED`, which the
phase map lands in `BLOCKED` — a terminal phase, so the lineage ends.

### 3. The question is asked once, and it is asked only once

The escalation is a *question*. Two rules follow, and both are needed for the
gate to actually be a gate:

**(a) While the question is open, the builder is not asked again.** The
context builder's answer can change between two runs — the memory store may
have filled a slot since, so the density rises and the directive becomes
`PROCEED_TO_PLANNER`. If the builder were asked again while the escalation
was unanswered, it would emit a fresh `CONTEXT_HYDRATED`, the projection
would leave `AWAITING_HUMAN`, and the task would plan and commit **without
anyone having been asked**. The gate has to hold until a human answers it,
whatever the builder says in the meantime.

**(b) After the human has ruled, the builder is not asked again either.** The
answer to "is the context sufficient?" is a judgement the human has already
made; asking the question again would park the task a second time on the
answer it just gave.

Both are decided from the lineage's **gate history** — the last event among
`AWAITING_HUMAN_ATTESTATION`, `HUMAN_GATE_CLEARED` and
`HUMAN_GATE_REJECTED`:

```python
gate = await self._last_gate_event(ctx.correlation_id)
if gate is EventType.HUMAN_GATE_CLEARED:
    return state   # the human answered; carry on
if gate is EventType.AWAITING_HUMAN_ATTESTATION:
    return state   # the question is still open; stay parked
```

> **Amended.** The history is now read **per stage** rather than as "the last
> gate event in the lineage", and a question carries an identity. A lineage
> that asked two questions has one open at a time, and each stage has to be
> able to tell which of them is its own — "the last gate event" cannot. See
> the Amendment at the end of this record.

The check reads the **lineage**, not the projection, because the projection
cannot express it: `apply_event` clears `awaiting_reason` when the gate is
cleared and the phase is overwritten by whatever event comes next. The ledger
is the truth; a projection is one view of it, and this question is about the
past rather than about the present state.

The consequence of (b) is that the resumed task plans from an *empty* context
packet — the context was never hydrated, and there is no API for supplying
it. That is the honest outcome: the human said "proceed", not "here is the
context you were missing".

### 4. The ledger says which question was asked

The escalation payload carries the directive verbatim:

```json
{"reason": "insufficient context density",
 "directive": "HARD_STOP_ESCALATE_TO_USER",
 "vacant_slots": ["..."]}
```

The policy gate and the context escalation are the *same event type*; only
the payload distinguishes them, so an operator reading the raw ledger can
tell a policy approval request from a context-density escalation without
having to know the emit site.

> **Amended.** The payload still distinguishes them, and the **discriminator**
> now does as well: it names the stage that asked, so the ledger says which
> question was asked even when a lineage has asked two. See the Amendment at
> the end of this record.

### 5. The phase map needed no change

`ledger/replay.py::_PHASE_MAP` is unchanged, and this is deliberate. The
projection lands in `AWAITING_HUMAN` because the escalation is the **last
event** of a parked lineage — the phase map already says the right thing, and
the defect was that the orchestrator kept writing events after it. Changing
the map would have been treating the symptom: it would have made the
projection say `AWAITING_HUMAN` for a lineage that had in fact gone on to
commit, which is a *less* honest projection, not a more honest one.

The one projection-level consequence worth recording: `TaskPhase.AWAITING_HUMAN`
is excluded from the resumable phases, so a task parked by an escalation
**stays parked across a restart**. A crash is never read as consent.

## Consequences

**What it costs.**

* A task whose context builder escalates now needs a human to finish. Before
  this change it finished by itself — wrongly, but it finished. An operator
  who never wired a human into the loop will see tasks stop at
  `AWAITING_HUMAN` instead of committing. That is the point, but it is a
  visible change: the escalation path is now a path that needs a person.
  **The blast radius is bounded and worth stating precisely.** The real
  gatherer computes the density as *filled required slots / declared required
  slots* (`context/metrics.py::context_density`) and escalates below
  `ContextSettings.density_hydrate` (0.40). A task that declares **no**
  required slots has density 1.0 — the convention is deliberate — so it
  never escalates. Only a task that *declared* slots and could not fill 40% of
  them is affected, which is exactly the set of tasks whose plan was being
  guessed at.
* The release path already exists and is reachable: the HTTP API
  (`api/app.py`), the TUI runner (`tui/runner.py`) and the CLI
  (`cli/main.py`) all call `clear_human_gate`. A parked task is not a dead
  task.
* The resumed task plans with an empty context packet (§3). A plan produced
  that way is no worse than the plan the escalation was refusing to make
  without a human — but it is a plan made without the context.
* `_hydrate` reads the lineage once more per run (one extra
  `read_correlation`). The orchestrator already re-reads and replays the whole
  lineage several times per run, so the marginal cost is a fraction of what
  is already being paid.

**What it does not change.**

* The policy gate's behaviour, including its resume path, is untouched.
* The ledger's event vocabulary, the phase map and the projection's shape are
  untouched. No migration, no replay of existing lineages, no change to the
  hash chain's inputs beyond one payload key.
* The `AWAITING_HUMAN_ATTESTATION` event type is unchanged, so a lineage
  written before this change and one written after are read by the same
  projection.

> **Amended.** The first bullet was true when this ADR was written and is not
> true now: the policy gate's resume path *is* touched, because the two
> questions in one lineage could not be told apart. See the Amendment at the
> end of this record.

**How to roll it back.**

Revert the three changes in `src/paa/agents/orchestrator.py`: the phase check
after `_hydrate`, the two gate-history branches in `_hydrate`, and the
`_last_gate_event` helper (with its `_GATE_EVENTS` set). The `directive`
payload key is additive and can stay. Nothing else in the tree depends on the
new behaviour, and no stored data depends on it — an escalated task that is
already parked stays parked, and re-running it after a revert takes the
pre-change path.

> **Amended.** The rollback is the amendment's own: revert the amendment's
> section below. The three changes above are still there; the amendment adds
> to them rather than replacing them.

**What this ADR does not decide.** The policy gate's *resume* path, in the
shape this ADR described it, is decided by the Amendment below: a lineage
that asks a human twice is a lineage with two questions, and the ledger has
to be able to tell them apart.

*(Corrected 2026-10-22, by W2-IMPR-01: this section used to say that "after
a human clears a policy gate, the policy agent is consulted a second time,
and the second `AWAITING_HUMAN_ATTESTATION` is *suppressed* by the ledger's
idempotency key rather than honoured — so the release works only because the
policy agent happens to be deterministic". That described the case **wrongly,
and in the direction that matters**. The measured behaviour is not that the
release works: the second gate request is *dropped* by the ledger, the
orchestrator therefore sees no pending gate, and the task carries on to
`MUTATION_COMMITTED` **with the second question unanswered**. The release did
not work; the gate was not a gate. And the two-question case is not exotic —
it is what this ADR's own change made reachable, because a task that parks on
the context builder's question resumes at the policy review, where the
policy agent may ask a question of its own. See the Amendment at the end of
this record.)*


---

## Amendment — a question has an identity, and the ledger must be able to tell two of them apart

*Amended 2026-10-22, by W2-IMPR-01 (the wave-2 improve phase), after the
wave-2 code review (F-1). Everything above still stands: a hard stop parks
the task, the park is honoured wherever it is raised, the release mechanism
is the one that already existed, the question is asked once, the ledger says
which question was asked, and the phase map needed no change. This amendment
changes **what a gate request is** — it is a *question*, and a question has
an identity — and it corrects this record's own account of the case it said
it did not decide. It adds no event type, no projection field and no phase.*

### What was wrong

`AWAITING_HUMAN_ATTESTATION` carried **no discriminator and no attempt**. The
ledger's idempotency key is

```
correlation_id | event_type | attempt | discriminator
```

(`ledger/events.py::compute_idempotency_key`), so every gate request in one
lineage hashed to the **same** key. The store's rule is that a duplicate key
is suppressed — that is how at-least-once delivery is made safe — and so the
**second** question in a lineage was dropped:

```
AWAITING_HUMAN_ATTESTATION (context builder)   -> recorded
HUMAN_GATE_CLEARED                              -> recorded
AWAITING_HUMAN_ATTESTATION (policy agent)      -> SUPPRESSED
```

The projection never saw the second question, so the orchestrator saw no
pending gate, and the task carried on to `MUTATION_COMMITTED` **with the
policy's risk gate unanswered**. Measured on this repository against the
pre-amendment sources, driving `ChiefOrchestrator` with the real
`PolicyRiskAgent` over a real `LedgerStore`:

```
phase after first run: TaskPhase.AWAITING_HUMAN
[debug] ledger.duplicate_suppressed  event_type=AWAITING_HUMAN_ATTESTATION
events: [TASK_REQUESTED, TASK_QUEUED, CONTEXT_HYDRATION_REQUESTED,
         AWAITING_HUMAN_ATTESTATION, HUMAN_GATE_CLEARED, PLAN_COMPILED,
         EXECUTION_STARTED, CRITIQUE_CONCLUDED, EXECUTION_COMPLETED,
         MUTATION_COMMITTED]
phase after resume: TaskPhase.COMMITTED
```

The audit trail says the policy agent never asked. It did.

The same defect class the models layer fixed in the same wave for
`MODEL_ESCALATED` (the per-router-instance escalation counter) and the same
shape the agents suite already pinned for `CRITIQUE_CONCLUDED` (the critic
does not pass an attempt, so a retried step's second critique is suppressed
by the key). The wave gave one event type a discriminator and left the
**security** one alone.

### The mechanism

**A gate request is a question, and the discriminator is that question's
identity.** The name is the stage that asked and the question's *ordinal*
within that stage:

```
gate:<role>:<ordinal>          e.g.  gate:policy_risk:1
```

Two properties, and both are required:

* **distinct across different requests.** Two stages asking are two
  different questions by construction — "is the context sufficient?" belongs
  to the context builder, "is this plan safe to run?" to the policy agent —
  so the stage is in the name. A stage that asks a second question gets a
  second ordinal, because the ordinal counts that stage's questions that
  have been **answered**, and a stage does not ask again while its question
  is open.
* **stable across a redelivery of the same request.** The ordinal counts the
  questions that have been *answered*, not the questions that have been
  *asked*. A redelivery of a question nobody has answered yet therefore
  recomputes the same ordinal, the key does not move, and the store
  suppresses the redelivery — which is the whole point of the key. Counting
  the questions *asked* would give the redelivery a new ordinal, and the
  audit trail would show one human being asked the same thing twice.

**The release names the question it answers.** `HUMAN_GATE_CLEARED` and
`HUMAN_GATE_REJECTED` carry the discriminator of the question they rule on,
so a lineage that asked two questions says which one the human ruled on, and
two releases cannot collide on the key either.

**Each stage consults its own question.** The gate history is read from the
lineage, as before, but the question a stage consults is *its own*: the
context builder's question releases the context builder, and the policy
agent's question releases the policy agent. "The last gate event in the
lineage" cannot answer that once a lineage has asked twice.

**The human's release of the policy stage's question is the policy
decision.** After a human has ruled on the policy gate, the policy agent is
**not** consulted again. Its second opinion is not what the human approved:
an agent that answered differently would either block a task a human had
approved, or — worse — wave through a plan the human never saw. It would
also ask the human the same question a second time, which is a loop. The
orchestrator therefore records the stage's clearance (`POLICY_CLEARED`,
`released_by: human`) so the projection carries the decision and the stage
is skipped from then on. The gate itself is still released by the human:
`HUMAN_GATE_CLEARED` is written only by `clear_human_gate`, and nothing in
the automated path calls it.

### What it costs

* Two more `read_correlation` reads per run, one per stage that runs
  (`_hydrate` and `_check_policy`). The orchestrator already re-reads and
  replays the whole lineage several times per run, so the marginal cost is a
  fraction of what is already being paid.
* A task that parks on the context builder's question and then on the policy
  agent's now needs **two** humans, or one human twice. That is the point:
  two questions are two questions.
* The release names the phase the task resumes at, and the phase is now the
  one the task was working towards: `PLANNING` for a question asked during
  hydration, `EXECUTING` for one asked during the policy review. The
  release path and the phase map in `ledger/replay.py` now agree, instead of
  the release asserting a phase the task had not reached.

### What it does not change

* The ledger's event vocabulary, the phase map and the projection's shape.
  The discriminator is an existing field of the existing event; the phase map
  is untouched, and the phase a parked task lands in is unchanged.
* The hash chain. The event digest covers the payload, the event type, the
  chain links and the timestamp — **not** the discriminator and not the
  attempt (`ledger/events.py::compute_event_digest`), so writing a
  discriminator does not change how an event is sealed. The release's payload
  gains two keys, and a payload *is* an input of the digest, so the digests
  of new events differ — as they do for any change of payload. No stored
  event is rewritten, so there is no migration and no re-hash.
* The human-only rule. `HUMAN_GATE_CLEARED` is still written only by a real
  caller, and nothing in the automated path can clear its own gate.
* A lineage written before this amendment is read by the same projection. A
  gate event with no discriminator is a question whose identity is unknown,
  and the release path falls back to "the question it follows" — which is
  the only one that can be open in a lineage that has only one.

### How to roll it back

Revert the amendment's changes in `src/paa/agents/orchestrator.py`: the
`_open_gate` helper and the discriminator it writes, the per-stage gate
history, and the release's discriminator and resume phase. The `directive`
payload key and the three changes the body of this ADR describes stay.
Nothing else in the tree depends on the new behaviour, and no stored data
depends on it — a parked task stays parked, and a lineage whose questions
were recorded with a discriminator is read by the same projection either
way.
