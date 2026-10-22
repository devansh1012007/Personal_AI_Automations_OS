# ADR-0034: A hard stop parks the task until a human rules

## Status

Accepted

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

**How to roll it back.**

Revert the three changes in `src/paa/agents/orchestrator.py`: the phase check
after `_hydrate`, the two gate-history branches in `_hydrate`, and the
`_last_gate_event` helper (with its `_GATE_EVENTS` set). The `directive`
payload key is additive and can stay. Nothing else in the tree depends on the
new behaviour, and no stored data depends on it — an escalated task that is
already parked stays parked, and re-running it after a revert takes the
pre-change path.

**What this ADR does not decide.** The policy gate's *resume* path has a
related, pre-existing fragility: after a human clears a policy gate, the
policy agent is consulted a second time, and the second
`AWAITING_HUMAN_ATTESTATION` is *suppressed* by the ledger's idempotency key
rather than honoured — so the release works only because the policy agent
happens to be deterministic. It is out of scope here (this ADR is about the
context escalation), it is recorded as a finding in the T-006 report, and
the same `_human_ruled` mechanism would close it.
