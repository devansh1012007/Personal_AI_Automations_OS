# PAA Vision

**Authority and evidence for autonomous agents.**

*Last reviewed: 2026-11-06 · Applies to PAA 4.1 · Status: working draft for validation*

This document says what PAA is for, what it deliberately is not, how it is meant to
grow, and what would make us stop. It supersedes the earlier ten-phase vision
([where each old phase went](#appendix-mapping-from-the-earlier-ten-phase-plan)) and
was revised after studying DeepSeek's Super-agent design
([research note](research/2026-11-deepseek-super-agent.md)). For what exists today,
read the [README](../README.md). For delivery detail, read the [ROADMAP](ROADMAP.md).

---

## Contents

1. [Thesis](#thesis)
2. [The problem](#the-problem)
3. [The landscape: front half and back half](#the-landscape-front-half-and-back-half)
4. [Beliefs we are betting on](#beliefs-we-are-betting-on)
5. [Who it is for](#who-it-is-for)
6. [Product shape](#product-shape)
7. [Extension model](#extension-model)
8. [Horizons](#horizons)
9. [Validation and kill criteria](#validation-and-kill-criteria)
10. [Non-goals](#non-goals)
11. [Principles](#principles)
12. [Risks and open questions](#risks-and-open-questions)
13. [Appendix](#appendix-mapping-from-the-earlier-ten-phase-plan)
14. [Temporal and spatial composability for PAA](#appendix-temporal-and-spatial-composability-for-paa)

---

## Thesis

Autonomous agents are becoming a normal way to change software and systems, and the
tools that run them are many, fast-moving, and mostly stateless. Two questions decide
whether an organisation can trust that work:

- **Authority:** *Who or what was allowed to start this, under which rules, and how
  far could it go?*
- **Evidence:** *What actually ran, what did it touch, and did verification pass?*

PAA is the layer that answers both, independently of any one agent. It is a
local-first, event-sourced runtime that **records** every action, **gates** every
mutation with deterministic checks, lets you **prove** what happened and replay it,
and **connects** to the agents, editors, event sources, and tools people already use
through narrow, open interfaces.

PAA is infrastructure beneath agents. It does not compete with them on reasoning.

## The problem

Agents rarely fail by crashing. They fail by *succeeding wrongly*: the task reports
done, the diff looks plausible, and a test was skipped, a command ran against the
wrong target, or a request was honoured that nobody should have been able to make.
Four things make this hard to catch today:

- **Self-reported success.** The summary of what happened is written by the system
  being audited.
- **Fragmented state.** Each agent keeps its own history in its own format.
- **Weak provenance.** After the fact it is hard to answer: *what changed, who or
  what authorised it, what did the agent know, and did verification actually run?*
- **Unbounded attention cost.** As agents become proactive and numerous, the
  scarce resource is the human who must supervise them. Every unnecessary
  interruption, and every unverifiable claim a person has to take on trust, spends it.

A durable, tamper-evident, agent-independent record, with deterministic checks that
no model can override and a record of the authority behind each piece of work,
addresses all four.

## The landscape: front half and back half

On 2026-10-31 DeepSeek's Harness team published a design for a **Super-agent** and an
MIT-licensed prototype ([research note](research/2026-11-deepseek-super-agent.md)).
It is the clearest public statement so far of where autonomous coding agents are
heading, and it sharpens PAA's position.

**What it is.** One always-on agent that a team talks to. A thin *Router* session takes
requests from chat and GitHub, a deterministic gate plus a model decide whether a
request is real work, and isolated *Worker* sessions, one per task, do the work and
report back through the Router. A *Watchdog* checks that workers are actually
progressing. The stated goal is to *take as little human attention as possible and
give as much value as possible*.

**What it proves.** Several PAA principles were reached independently: record
intent before acting ("stored first, done afterwards"), make intake idempotent, put
deterministic code in front of the model, and keep an immutable record of why work
was permitted (the prototype's *Mandate*, which captures the policy version, scope,
and sender).

**What it leaves open.** By its authors' own account, workers run shell commands
"without approval as the operator's OS user", the per-task directory and blocked-
command list are "defaults, not a security boundary", and security, long-term memory,
and cross-boundary collaboration are listed as unsolved. There is no tamper-evident
history, no deterministic patch validation, and no replay. Independent users who
tried the prototype within days reported lifecycle-correctness bugs (authorisation
changing mid-retry, work outliving its cancellation, a poll that reported success
while skipping events).

**The reading.** Super-agent is the **front half** of autonomous work: *who may start
it and how it is routed.* PAA is the **back half**: *contain it, verify it, record it,
recover it.* They are complementary. The missing joint, and PAA's opportunity, is
**authority plus evidence**: a verifiable record that links *who authorised this
under which rules* to *what actually happened*.

**The consequence for strategy.** A frontier lab has open-sourced the front half.
Rebuilding routers, sources, and panels from scratch would spend a solo maintainer's
time on commodity. PAA should (a) adopt the ideas that strengthen its own guarantees,
(b) integrate with front-half systems rather than imitate them, and (c) stay
vendor-neutral and offline-verifiable, which a harness vendor cannot credibly offer.

## Beliefs we are betting on

These are hypotheses. Each is falsifiable, and
[validation](#validation-and-kill-criteria) is designed to test them.

| # | Belief | What would disprove it |
|---|---|---|
| B1 | People and teams will keep using **more than one** agent system, and no vendor will be a neutral recorder of the others. | Consolidation onto one system whose built-in audit is considered sufficient. |
| B2 | **Verification evidence** (commands run, tests executed, approvals) is more valuable than agent-reported success. | Reviewers do not read or trust provenance, even when it is offered. |
| B3 | Governance works best at **boundaries** (shell, filesystem, MCP, network, git), not inside prompts. | Agents routinely act outside any boundary PAA can observe. |
| B4 | **Local-first** matters for code and personal data. | Users readily route everything through hosted services. |
| B5 | **Deterministic gates** beat model-in-the-loop safety for anything that matters. | Model-based review proves as reliable and far cheaper to maintain. |
| B6 | **Human attention is the scarce resource** for multi-agent work, and a record of *authority* (who asked, under which policy) reduces the attention spent verifying it. | Teams review agent output at the same cost with or without authority and evidence attached. *(The 3–5 session limit cited by DeepSeek is an observation from experience, not a measured result.)* |
| B7 | **Proactive agents need staged autonomy**: observe before acting, act only under a written rule. | Teams grant broad autonomy immediately and see no harm. |

## Who it is for

Hypotheses about users we have not yet interviewed:

1. **Individual developers** who run coding agents on their own repositories and want
   recovery, a trustworthy record, and a way to compare agents on their own code.
2. **Small engineering teams** that let agents open pull requests, and especially
   teams starting to let agents act on chat or issue-tracker events. They want each
   change to carry verifiable *authority and evidence* rather than a summary.
3. **Regulated or security-sensitive organisations** (later, and only if 1 and 2
   hold). They need identity, retention, and export. PAA is single-user today and does
   not serve this group.

The tension to manage: PAA's local-first, single-user design fits (1) well and (3)
poorly. The sequence in [Horizons](#horizons) reflects that.

## Product shape

Five pillars:

- **Record.** An append-only, hash-chained ledger is the single source of truth. All
  other state is derived and rebuildable.
- **Authorise.** Every unit of work carries a *mandate*: who or what requested it, how
  strong the basis was (explicit request or inferred), under which policy version, and
  within what scope. Proactive work is staged: observe, then act under a written rule.
- **Gate.** Policy, sandbox, and validation decide whether a mutation proceeds. Humans
  attest where policy says so. Models propose; they do not decide.
- **Prove.** Replay, causal walk-back, and exportable evidence. Failed runs become
  regression tests. Agents can be compared on your own tasks with deterministic
  verification.
- **Attend.** Spend as little human attention as possible: speak only when a person
  must act or can use a result, and measure the attention each change costs.

```
   Event sources · Editors · UIs · chat · CI       (ACP · HTTP · CLI · webhooks)
                      │   northbound seams
        ┌─────────────▼────────────────────────────────────────┐
        │  PAA core                                            │
        │  ledger · mandates · policy · sandbox · validation   │
        │  watchdog · memory · context · recovery              │
        └─────────────┬────────────────────────────────────────┘
                      │   southbound seams
   Executors / agent harnesses (ACP, Executor port) · Tools (MCP)
   Models · Hooks · Event export
```

The design aim is a **narrow waist**: a small, stable core surface that many things
attach to. A wide, attach-anywhere plugin API would weaken the guarantees that justify
PAA's existence.

## Extension model

PAA should be extensible, but extensibility is not the product. The product is the
guarantees. Extension is permitted only where it preserves them.

**The rule.** An extension may attach wherever its inputs and outputs cross a boundary
that is *recorded in the ledger* and *gated by policy*.

| Seam | Direction | Interface | State |
|---|---|---|---|
| Tools and skills | Runtime → tools | MCP, Claw Hub (Unified Skill Adapter) | Built |
| Models | Runtime → providers | Provider registry and escalation router | Built |
| Editors and UIs | Client → PAA | ACP server | Experimental |
| Lifecycle hooks | Around tool calls, commits, gates | Out-of-process commands, JSON in/out | In-process built; out-of-process planned |
| Agent harnesses | PAA → agents | ACP client under the sandbox | Planned |
| Front-half systems (event intake, dispatch) | Their executor → PAA | **PAA as an Executor behind a port**: they hand over a ticket and mandate; PAA contains, verifies, and records | Planned; feasibility unverified |
| Event sources | Source → PAA | Triggers with idempotent identity | Planned, minimal |
| Event export | Ledger → observers | JSONL, OpenTelemetry | Planned |
| Custom agentic loops | Strategy replacement | `Pipeline` + `RuntimeContext` | Design only |

**Constraints on every extension:**

1. **Out of process.** Third-party code does not run inside the trusted core. Static
   analysis is a screen, not a sandbox.
2. **Tighten only.** An extension may add restrictions to policy; it may never relax
   one.
3. **Recorded results.** Anything non-deterministic (a model call, a network fetch) has
   its result written as an event. Replay reads the record and never re-executes.
4. **Versioned envelope.** Events carry a version and a stable envelope. The ledger is
   hash-chained, so history cannot be rewritten to migrate it. The schema must be
   designed for forward compatibility *before* third parties depend on it.
5. **Rule of three.** Do not extract a general adapter interface until three concrete
   integrations exist.
6. **Authority travels with the work.** An extension that originates work must supply
   a mandate; work without one is not accepted.

**What this means for external agents.** Hosting a third-party agent under PAA gives
*workspace-level* guarantees (checkpoint, rollback, a complete record of what crossed
the boundary), not resumption of an agent mid-thought. How much of an agent's
activity crosses an observable boundary is open and is the first thing to measure.

## Horizons

Ordered by dependency and by how soon each can falsify a belief. Item-level detail,
exit tests, and provenance of each idea are in the [ROADMAP](ROADMAP.md).

### Horizon 0: Foundation (built)

Event-sourced ledger, replay, and crash recovery; bounded context engine; polyglot
storage with pluggable backends; sandbox backends; deterministic validation; policy;
memory subsystem; model providers with an escalation router; Unified Skill Adapter;
signed-package marketplace (metadata path); API, daemon, and CLI; a router-backed
coder loop; an experimental ACP server.

### Horizon 1: Hands, authority, and honesty

*Make PAA capable of real work, record who authorised it, and make its documentation
trustworthy.*

- File, shell, and git tools as permissioned skills run through the sandbox.
- Wire the coder loop into the ledger so every tool call is an event and a crash is
  recoverable.
- **Mandate events**: `TRIGGER_RECEIVED`, `ELIGIBILITY_DECIDED` (with strength and
  matched rules), and `MANDATE_GRANTED` (with policy version), so every task records
  *why it was allowed*.
- Asynchronous tool calls with cancellation, so a long command never blocks the loop.
- Drive a task with a real frontier model end to end.
- Keep documentation measured, not asserted.

**Exit:** `paa agent "fix the failing test"` edits, tests, and commits; every step and
its mandate are in the ledger and survive `kill -9`.

### Horizon 2: Seams

*Make PAA connectable without widening the core.*

- Harden the ACP server (streaming, cancellation, editor context).
- Event envelope v1 with a compatibility policy.
- Out-of-process hooks; event export as JSONL and OpenTelemetry.
- **First external integration, run inside the sandbox**, recording every file
  operation, command, and permission request. Measure what the agent does *outside*
  any observable boundary before designing anything general.
- **Executor-port spike**: determine whether a front-half system such as DeepSeek's
  prototype can hand a ticket to PAA and receive its result and evidence.
- A credential broker so shell, git, and CLI work can use secrets the model never sees.

**Exit:** one external agent runs under PAA with its boundary activity recorded, and a
crash restores the workspace to the last checkpoint.

### Horizon 3: Evidence and attention

*Deliver the value the thesis claims, and test it with people who are not us.*

- **Attested change.** A pull request or patch accompanied by an offline-verifiable
  bundle: **the mandate** (who asked, under which policy), the diff, commands run, test
  results, approvals, and the hash chain. `paa verify` checks it without trusting the
  producer.
- **Observe mode** for proactive work: record "would accept" and start nothing, so a
  team can read what an agent would have done before granting it permission.
- **Watchdog**: deterministic stall and loop detection from the ledger first (no
  progress events, repeated identical calls, spend without change), model-based drift
  judgement later.
- **Attention ledger**: record every human interruption as an event and report
  attention per accepted change, making B6 measurable.
- Replay to regression test; an evaluation harness that compares agents on your own
  repository with deterministic verification.

**Exit:** the [validation test](#validation-and-kill-criteria) has been run.

### Horizon 4: Reach (conditional on Horizon 3)

- Minimal event intake (one GitHub source) *only if* integration with a front-half
  system proves insufficient.
- Web dashboard that lists what needs a person; ledger "time machine".
- Durable long-horizon tasks with milestone checkpoints and progressive summarisation;
  context objects with stable IDs for multi-hop recall.
- Speculative execution inside sandboxes, with recorded, discardable branches.
- Authentication, per-user isolation, and team mode.

### Horizon 5: Platform (conditional on Horizon 4)

- Out-of-process pipeline SDK for custom loops.
- Executable package loading and a marketplace publishing path.
- Strong isolation as the default where it can run, and a third-party security review.

### Parked

Ideas that do not serve the thesis until the horizons above succeed. Parking is a
sequencing decision, not a rejection.

- A full router/worker/source/panel stack of our own (see the landscape section).
- Ambient bridges beyond a minimal source; multimodal input; local fine-tuning.
- Cloud-hosted loop with remote daemons ("bring your own machine"). It changes PAA's
  threat model: DeepSeek's own authors note that a compromised cloud loop could control
  every connected machine. PAA's local loop avoids that, at the cost of not being
  always-on.
- Skill economy: payments, reputation, bounties, agent-to-agent hiring.

## Validation and kill criteria

Building before testing demand is the largest risk. Horizons 1 and 2 are the cheapest
route to a testable artifact, and Horizon 3 ends with a test.

**The test.** Give the attested-change workflow to about ten engineers who run agents
on shared repositories, including at least a few who use or are evaluating a
proactive, event-driven agent. Observe what they do, not what they say. Measure
review time, whether the mandate and evidence are opened, and attention per change.

| Outcome | Decision |
|---|---|
| At least three adopt it in a real workflow and read the evidence | Proceed to Horizon 4 |
| Interest is polite but usage is low | Narrow to the one piece that *was* used; stop platform work |
| Fewer than three care about authority or provenance | Keep PAA as a personal runtime and reference implementation; do not build Horizons 4–5 |

Also stop and reassess if:

- most agent activity happens outside any boundary PAA can observe (B3 fails);
- front-half systems ship containment and verifiable audit of their own that teams
  consider sufficient (B1 fails).

## Non-goals

- **Out-reasoning frontier models.** Architecture does not close a capability gap.
- **Being another general-purpose agent framework, or reproducing a router/worker
  orchestrator.** PAA sits beneath and beside them.
- **Safety through prompting or model review.** Safety is deterministic or it is not
  claimed.
- **Resuming a third-party agent mid-thought.** PAA can checkpoint and restore
  workspaces and record history; it cannot restore another agent's internal state.
- **Unrestricted "plug anything anywhere" extensibility.**
- **Serving regulated enterprises today.**

## Principles

1. **The ledger is truth; everything else is a cache.**
2. **Deterministic where it matters.** Safety, validation, and anything involving
   money never depend on a model.
3. **Local-first; escalate on purpose.**
4. **No silent capability without a gate.** Every new power arrives with a permission
   and an audit trail.
5. **Honesty over hype.** If something is untested, unbuilt, or weaker than the
   alternative, the documentation says so.
6. **Narrow waist.** Prefer a few stable interfaces over many flexible ones.
7. **Record the boundary.** If an interaction cannot be observed and recorded, PAA does
   not claim to govern it.
8. **Authority travels with the work.** Work without a recorded mandate is not
   accepted, and proactive work is staged: observe, then act under a written rule.
9. **Spend attention deliberately.** Speak only when a person must act or can use a
   result.

## Risks and open questions

| Risk or question | Why it matters | How we will find out |
|---|---|---|
| A frontier lab owns the front half and may add its own containment and audit | PAA's differentiation shrinks if it is not neutral and offline-verifiable | Stay vendor-neutral; make verification independent of the producer |
| The harness-orchestration space is crowded and mostly free | A thin multi-agent layer is not a business | Differentiate on authority and evidence, not on running many agents |
| Can another system's executor really be replaced? | The Executor-port integration depends on a prototype whose code we have not read | Horizon 2 spike before committing to the seam |
| Agents may act outside observable boundaries | Without enforcement, PAA records less than it appears to | Horizon 2 integration; run the whole agent inside the sandbox where needed |
| Local-first single-user vs. what organisations pay for | The users we can serve and the users who pay may differ | Horizon 3 trial; do not assume |
| Hash-chained history is hard to migrate | Third-party dependence on schemas makes mistakes permanent | Envelope v1 and a compatibility policy first |
| Replay determinism | Unrecorded effects break replay silently | Record all non-deterministic results; fault-injection replay tests |
| Lifecycle-correctness bugs in new intake code | The prototype's field reports show authorisation drift, orphaned work, and silent event loss are easy to introduce | Property and chaos tests for every new lifecycle path *before* it ships |
| Proactive agents interrupt people | Attention is the scarce resource; noise destroys trust | Observe mode, attention ledger, hard output limits |
| Model ceiling | A weak local model limits what PAA-hosted agents can do | Support frontier providers by configuration; state the limit plainly |
| Solo-maintainer scope | A large surface area outpaces one maintainer | Parking list, horizon gating, rule of three |
| Documentation drift | Inconsistent figures undermine a project whose pitch is trust | One measured status block with the command that reproduces it |
| Windows sandbox strength | The primary development platform has the weakest backend | State the limit; recommend WSL2 or Docker for untrusted code |

## Appendix: mapping from the earlier ten-phase plan

| Earlier phase | Where it went |
|---|---|
| 1. Real capabilities (tool library) | **Horizon 1**, as the first priority |
| 2. Codebase intelligence (index, LSP, git) | Git in **Horizon 1**; index and LSP in **Horizon 3** only as far as evidence needs them |
| 3. Interfaces (TUI, web, search) | ACP and CLI in **Horizon 2**; web dashboard in **Horizon 4** |
| 4. Ambient presence | Authority and observe mode in **Horizons 1 and 3**; minimal source in **Horizon 4**; the rest **parked** (a front-half system exists) |
| 5. Long-horizon autonomy | Watchdog in **Horizon 3**; durable task graphs in **Horizon 4**, conditional |
| 6. Multimodal senses | **Parked** |
| 7. Learning system | Evaluation harness in **Horizon 3**; fine-tuning **parked** |
| 8. Platform and economy | **Horizon 5**, conditional; payments and reputation **parked** |
| 9. Advanced reasoning | Speculative execution in **Horizon 4**; the rest **parked** |
| 10. Trust, scale, moat | Credential broker in **Horizon 2**; isolation and review in **Horizon 5**; ongoing throughout |
## Appendix: Temporal and spatial composability for PAA

> **Status: proposed architectural clarification; not an implementation claim.** This
> addendum extends the existing thesis, product shape, extension constraints, and
> horizons. It does not replace or remove any existing task. A proposed interface or
> event name below is a design candidate until it is implemented and tested.

### Why this belongs in PAA

The paper [*A Programming Paradigm for Spatiotemporal Composability*](https://arxiv.org/abs/2608.25512)
(Yifan Shi, Wei Zhang, and Tianyi Cui, 26 August 2026) distinguishes two properties
of dynamically composed systems:

- **Temporal composability:** a component can be removed and the effects it introduced
  into its managed environment can be accounted for and, where reversible, reverted.
- **Spatial composability:** a component declares what it requires, and the runtime
  reacts correctly when those requirements become satisfied, unsatisfied, or changed.

The paper develops these ideas as *revertible effects* and *reactive coeffects*,
mediated through a shared context. Cordis is its reference implementation. PAA should
adopt the underlying discipline, not copy a plugin-oriented API or assume that every
real-world action is reversible.

PAA's environment is more demanding than an in-process component container. A PAA
component may own a process, workspace, capability lease, event subscription, model
session, database transaction, or external action. It may run for minutes or days,
crash between an external action and its acknowledgement, and operate under a mandate
whose permissions can be tightened while it runs. Therefore:

**PAA temporal composability means durable, scope-owned lifecycle accounting and safe
unwinding of managed effects. PAA spatial composability means declarative, reactive,
policy-aware dependency resolution for each task.**

Neither property means that PAA can reverse an email already delivered, restore a
third-party model's hidden reasoning state, or safely hot-swap an arbitrary process in
the middle of an uncheckpointed operation.

### The three graphs must remain distinct

PAA should not collapse every relationship into one generic graph. Three related
structures answer different questions:

| Structure | Question it answers | Durable representation |
|---|---|---|
| **Component dependency graph** | What must be available before a harness, pipeline, provider, or service can activate? | Versioned declarations and recorded resolution decisions |
| **Task execution DAG** | What work must happen, in what order, and with which branches, joins, retries, and approvals? | Task/step records, checkpoints, and causal event links |
| **Effect ownership graph** | Which scope introduced each resource or side effect, and what is its disposal or compensation path? | Effect records linked to task, component, resource, and ledger events |

The dependency graph is the spatial-composition mechanism. The effect ownership graph
is the temporal-composition mechanism. The execution DAG describes task work. They
must link through stable IDs, but they must not be treated as interchangeable.

### A task-scoped composition model

The target is not one universal agent loop with every capability loaded globally. PAA
should assemble a bounded runtime for each task from a shared trusted kernel and a
task-specific set of compatible components.

Proposed conceptual objects:

1. **TaskSpec** — the user's goal, inputs, constraints, expected outputs, deadline or
   budget, and verification requirements.
2. **Mandate** — who or what requested the task, the request's authority strength, the
   applicable policy version, and the maximum permitted scope. This remains the source
   of authority; a harness declaration cannot grant itself permission.
3. **HarnessSpec** — the selected execution arrangement: harness adapter, planning
   strategy, memory strategy, action strategy, and required contracts. It is
   declarative configuration, not arbitrary code generated and trusted at runtime.
4. **Component declarations** — the requirements and services offered by harnesses,
   pipelines, capability providers, context adapters, watchdogs, and other runtime
   participants.
5. **Resolved task context** — the bounded, versioned view of memory, task state,
   budget, policy decisions, workspace handles, and capability leases exposed to that
   task.
6. **Task execution DAG** — the actual steps and dependencies for carrying out the
   TaskSpec.
7. **Effect scopes** — the ownership boundaries for runtime registrations, processes,
   leases, transactions, workspaces, and other effects created while executing it.

A task therefore gets its own composition and context. It does not receive unrestricted
access to a mutable global service container. The shared PAA kernel continues to own
the ledger, mandate checks, policy, sandbox boundary, validation, recovery, and
resource accounting.

A harness can be native to PAA or external (for example, an ACP-connected agent). Both
must use the same narrow, versioned runtime contract for any operation for which PAA
claims authority, containment, or evidence. An external harness that bypasses PAA's
instrumented boundary does not inherit PAA's guarantees.

### Temporal composability: effects, scopes, and lifecycle

#### 1. Give every effect an owner and a scope

Every managed effect should be attributed to a stable scope, with hierarchical
ownership such as:

```text
PAA runtime
└── Task scope
    ├── Harness-instance scope
    │   ├── process/session
    │   └── event subscriptions
    ├── Pipeline-instance scope
    │   └── strategy-local resources
    ├── Workspace scope
    │   └── snapshot / branch / temporary files
    └── Capability-lease scopes
        ├── filesystem lease
        ├── shell lease
        └── test-runner lease
```

A scope has a stable ID, owner, parent, generation, creation event, lifecycle state,
and the IDs of effects it owns. A resource shared by multiple tasks must be represented
as an explicitly shared resource with leases or reference ownership; it must not be
silently owned by whichever task happened to use it first.

#### 2. Represent each effect as a managed lifecycle record

A proposed effect record should capture at least:

```yaml
 effect_id: effect-uuid
 owner_scope_id: task-123/harness-generation-2
 resource_kind: event_subscription
 operation: register
 provider_id: paa-event-bus
 operation_key: stable-idempotency-key
 inverse_or_recovery:
   operation: unregister
   resource_handle: subscription-789
 reversibility: exact
 state: applied
 created_by_event: ledger-event-id
 mandate_id: mandate-id
 policy_version: policy-version
```

The exact schema is open. The requirements are not: ownership, operation identity,
recovery information, authority provenance, and lifecycle state must be explicit.
Sensitive credentials should be referenced through a credential broker, never copied
into the ledger or exposed to a model.

Use one mediated effect-registration path for all runtime-managed mutations. A
component must not mutate shared runtime context through an untracked side channel and
then claim it can be unloaded safely.

#### 3. Classify effects by what can truthfully be undone

PAA must distinguish at least these classes:

| Class | Examples | Removal behavior |
|---|---|---|
| Runtime-local registration | Listener, callback, registry entry | Invoke its exact inverse; record disposal |
| Acquired resource | Process, socket, temporary directory, model session | Cancel/close/release; verify termination or record failure |
| Transactional local mutation | Database row or isolated workspace change | Roll back inside the transaction, or apply a version-checked undo/snapshot restore |
| Persistent local artifact | Committed file or Git commit | Preserve by default; revert only under an explicit, separately authorised rollback policy |
| External side effect | Email, webhook, issue comment, publication | Record as irreversible unless the provider proves cancellation; compensation is a new action |
| Financial or safety-critical action | Payment, purchase, destructive infrastructure change | Require the relevant deterministic gate and human authority; never imply generic undo |

A compensation is not erasure. If an email was sent and a correction is later sent,
both events remain in the ledger. The correction requires its own mandate and policy
decision. A task may be *stopped with residual effects*; PAA must not label that state
as a clean rollback.

#### 4. Use prepare, apply, reconcile, and dispose semantics

For an effectful operation, PAA should durably record an intent and its recovery
strategy before crossing the effect boundary whenever possible. It then executes with
a stable operation/idempotency key and records the provider result and resource handle.
If the process crashes after the provider acts but before PAA records the response, the
effect becomes **outcome unknown** and must be reconciled against provider state or an
idempotency lookup. PAA must not blindly repeat the action or blindly run a compensator.

Candidate ledger events (names are illustrative):

- `EFFECT_PREPARED`
- `EFFECT_APPLIED`
- `EFFECT_OUTCOME_UNKNOWN`
- `EFFECT_RECONCILIATION_RESULT`
- `EFFECT_DISPOSAL_REQUESTED`
- `EFFECT_DISPOSED`
- `EFFECT_COMPENSATION_REQUIRED`
- `EFFECT_COMPENSATED`
- `EFFECT_DISPOSAL_FAILED`

The registry is a derived index over the append-only ledger, not a second source of
truth. Replay reconstructs effect state from recorded facts; it never reissues the
original external operation.

#### 5. Define component lifecycle and safe replacement

A useful proposed lifecycle is:

```text
DECLARED → PENDING → RESOLVING → ACTIVE
                              ↘ DEGRADED
ACTIVE → QUIESCING → DISPOSING → DISPOSED
   │          │          ↘ DISPOSAL_FAILED
   └──────────┴→ SUSPENDED / NEEDS_RECONCILIATION
```

The exact state machine can be smaller in the first implementation. Its invariants
matter more than its names:

- No new operation is admitted after quiescing begins.
- In-flight operations are drained or cancelled according to their declared contract.
- Child scopes and dependent components are quiesced before their providers are
  disposed; independent effects are disposed in reverse registration order (LIFO)
  within their owning scope.
- Shared resources are released only when their ownership/lease rules permit it.
- Disposal failure is durable and visible; it is never silently treated as success.
- A scope cannot become `DISPOSED` while it has unresolved effects.

For replacement, do not mutate a running component's identity in place. Create a new
generation, validate and prepare its dependencies, transfer only explicitly
serializable checkpoint state, then cut over at a safe checkpoint. Quiesce and dispose
the old generation after the new one is healthy. If preparation fails, retain the old
generation where safe. This is blue/green replacement for managed components, not a
promise to resume an external agent mid-thought.

#### 6. Preserve the distinctions between stop, unload, rollback, and replay

- **Cancel** stops further task work; it does not itself undo prior actions.
- **Quiesce** stops new admissions and drains or cancels in-flight work.
- **Unload/dispose** releases the component's owned runtime effects.
- **Rollback** reverses eligible mutations within an explicit transaction boundary.
- **Compensate** performs a new, separately authorised action to mitigate an earlier
  irreversible effect.
- **Replay** reconstructs state from recorded events and never repeats external work.
- **Recover** restores the task and workspace to a known checkpoint, then resumes only
  under valid authority and dependencies.

This vocabulary should be used consistently in APIs, event names, UI, and docs.

### Spatial composability: requirements and reactive resolution

#### 1. Declare requirements independently of providers

A harness or pipeline should say what it needs, not which concrete integration must
satisfy it. For example, a coding harness may require `filesystem.read`,
`filesystem.write`, `shell.execute`, and `tests.run`; a research harness may require
`web.search`, `document.read`, and `evidence.store`.

A component declaration should be able to express:

- required and optional capabilities/services;
- contract and version range;
- trust and isolation requirements;
- data-location or network constraints where applicable;
- resource needs and limits (time, tokens, concurrency, storage);
- required context fields or schemas;
- events it consumes or publishes;
- conflicts and ordering constraints;
- lifecycle hooks for quiesce and disposal.

A requirement is not permission. Resolving `filesystem.write` only identifies a
candidate provider. The mandate and current deterministic policy must separately
authorize the requested paths and operations before PAA issues a lease.

#### 2. Resolve requirements to providers and issue bounded leases

The dependency resolver should choose an eligible provider using declared contract
compatibility, version, trust, sandbox support, availability, cost/budget, locality,
and task constraints. It should produce a recorded resolution decision containing
both the requested contract and the selected provider/version. The task receives a
narrow lease or client for the resolved operation, not a global provider object or
ambient credentials.

Pin the resolution for the relevant run or checkpoint interval. Do not silently move a
running task to a provider with different semantics, data handling, trust, cost, or
permissions. A provider swap is a dependency change: suspend or quiesce the affected
component, compare the new provider against the original constraints, re-authorize if
needed, record the decision, and then resume at a safe boundary.

#### 3. Treat changing dependencies as lifecycle events

The key mechanism in reactive coeffects is to compare each relevant environment change
against a component's declared requirements and classify it as:

- **Activating:** a previously missing or unsatisfied requirement is now satisfied.
- **Deactivating:** a required dependency is withdrawn, incompatible, unhealthy, or no
  longer authorised.
- **Neutral:** the change does not affect this component's requirements.

PAA should apply this to changes in provider availability, contract version, sandbox
health, credential lease, policy, mandate scope, budget, workspace identity, context
schema, and subscribed event source. A reverse index from dependency keys to affected
components avoids rescanning every component after every event.

A missing *hard* dependency prevents activation or suspends execution. A missing
*optional* dependency may leave a component active in a declared degraded mode. The
fallback must be explicit in the HarnessSpec and policy; it must not silently weaken a
security requirement. Dependency cycles should be rejected by default. If a valid
cycle is needed, it must be an explicit activation group with well-defined startup and
shutdown semantics.

#### 4. Make context access task-scoped and mediated

The Cordis context paradigm suggests one mediated context through which component
effects and dependency requirements are observed. PAA should adapt this as a
`RuntimeContext` boundary, not as a shared mutable bag of services.

A task's context should contain:

- an immutable snapshot/reference to its TaskSpec, mandate, policy decision, and
  resolved dependency set;
- scoped reads of approved memory, artifacts, and prior events;
- capability clients backed by explicit leases;
- budget and deadline views;
- methods to request an effect, checkpoint, approval, or cancellation;
- stable IDs for task, run, component generation, workspace, and checkpoint.

All writes and external calls go through mediated runtime methods that enforce policy
and append evidence. Context changes are published as typed events; affected
components re-evaluate their declared requirements. Components cannot acquire new
ambient authority merely because a new service appeared in context.

#### 5. Keep a stable resolution during a run, but react safely to change

Reactive does not mean that every task should rebind immediately whenever a provider
appears. The default should be predictable execution: record and pin a compatible
resolution, then react to meaningful invalidation. If a required provider disappears,
policy tightens, a credential is revoked, or a budget is exhausted, the affected work
is suspended or denied. Re-resolution is allowed only if the new binding still meets
the original constraints and current authority. Any change that expands scope requires
a new mandate or explicit approval.

### How the two forms work together in a task-specific PAA run

Consider two different tasks arriving at the same PAA runtime:

```text
Task A: Refactor a repository
  Harness: coding
  Requirements: repo.read, repo.write, shell.execute, tests.run
  Context: repository snapshot, coding conventions, relevant memory
  Execution DAG: inspect → plan → edit → test → review → commit

Task B: Research a regulation
  Harness: research
  Requirements: web.search, document.read, evidence.store
  Context: question, source policy, prior research, evidence collection
  Execution DAG: discover → retrieve → compare → verify → synthesise
```

Both tasks use the same PAA ledger, mandate service, policy engine, sandbox boundary,
recovery machinery, and evidence model. They resolve different dependency graphs and
receive different bounded RuntimeContexts. They do not load every capability into a
global agent environment.

For Task A, suppose `tests.run` becomes unavailable. Spatial resolution marks the
coding harness as blocked or degraded according to its declared contract. PAA pauses
steps that require test evidence; it must not let the harness claim verified success.
If a compatible test provider becomes available, PAA records a new resolution and
resumes only after the required authorization checks.

Suppose the coding harness itself must be replaced. Temporal composition quiesces its
old generation, preserves the task ledger and durable workspace checkpoint, disposes
its listeners/process/leases, and starts a new harness generation against the same
approved task context. The new harness sees recorded evidence and checkpoint state; it
does not inherit hidden internal reasoning from the old process.

If Task A is then cancelled, PAA disposes only Task A's effect scopes. Task B's research
context, provider leases, and event subscriptions remain intact. Any committed or
external effects from Task A remain in the record and are not falsely described as
undone.

### Proposed component declaration and task configuration

The following is illustrative YAML, not a committed schema. It shows the separation
between a task's requested outcome, the strategy selected for it, and the contracts
that the runtime resolves:

```yaml
 task:
   kind: repository_change
   goal: "Refactor authentication and prove the tests pass"
   constraints:
     workspace_scope: current_repository
     network: denied_by_default
     require_test_evidence: true

 harness:
   id: coding
   version: "1.x"
   strategy:
     planning: dependency_aware
     action: iterative
     memory: repository_context
   requires:
     - capability: filesystem.read
       contract: "^1"
       required: true
     - capability: filesystem.write
       contract: "^1"
       required: true
     - capability: shell.execute
       contract: "^1"
       required: true
     - capability: tests.run
       contract: "^1"
       required: true
   lifecycle:
     checkpoint_before_replace: true
     quiesce_inflight: true
     dispose_owned_effects: true
```

The runtime validates this declaration, resolves eligible providers, checks the
mandate and policy, creates task-scoped leases, and records the resulting resolution.
The model may propose a HarnessSpec; deterministic validation decides whether it is
well-formed and permitted. A generated specification must never grant itself a
capability, widen its mandate, or bypass sandbox constraints.

### The immutable-ledger boundary

Temporal disposal must never rewrite, delete, or reverse the authoritative event
ledger. The ledger records that an effect was requested, authorised, attempted,
applied, disposed, compensated, or left unresolved. Disposal changes the current
managed environment where possible; it does not erase the historical fact that the
effect happened. Derived registries, leases, caches, and temporary resources may be
rebuilt or unwound, but the evidence history remains append-only. This is the boundary
that lets PAA combine reversible runtime composition with trustworthy provenance.

### Relationship to the existing PAA architecture

| Existing PAA element | Composability role | Clarification to preserve |
|---|---|---|
| Append-only ledger and replay | Durable record of effect, dependency, and lifecycle transitions | The ledger remains the source of truth; registries and graphs are derived indexes |
| Mandates and policy | Authority for activation and every effectful operation | Dependency availability never implies permission |
| Sandbox and capability boundary | Enforces where providers and harnesses execute | Third-party code remains out of process; a declaration is not a sandbox |
| Memory and bounded context | Supplies task-specific, permission-filtered information | A task receives a scoped context view, not global mutable state |
| Recovery and checkpoints | Restores known task/workspace state | Does not restore a third-party agent's hidden reasoning state |
| `Pipeline` + `RuntimeContext` | Candidate seam for task-specific strategies | Keep strategy replaceable; the runtime owns guarantees |
| ACP / Executor ports | Candidate external harness boundary | Measure observable coverage before claiming containment |
| Unified Skill Adapter / MCP | Existing capability integration path | Treat as a provider/transport behind capability contracts, not as the core abstraction |

This design reinforces the existing narrow-waist rule. It does not justify a wide API
where arbitrary components can mutate each other's state. The trusted kernel should
mediate context access, effect registration, dependency resolution, authority checks,
and event recording. Components may be numerous; the privileged runtime surface
should remain small.

### Required invariants

These should become executable tests before PAA claims temporal or spatial
composability:

**Temporal invariants**

1. Every managed effect has exactly one owning scope or an explicit shared-ownership
   rule.
2. Disposing one task cannot dispose another task's effects.
3. An effect is not reported disposed until its inverse, release, or reconciliation
   has been confirmed; failures remain visible.
4. Disposal follows reverse dependency order and LIFO order within a scope where the
   operations are ordered and independent.
5. A crash at every point between intent, provider execution, acknowledgement, and
   result recording yields a reconcilable state; no blind duplicate external action.
6. Replay never invokes the forward operation or compensation again.
7. Irreversible effects remain in evidence; compensation creates a new event and
   requires its own authority.
8. A component replacement does not erase task history or claim mid-thought recovery.

**Spatial invariants**

1. A component cannot activate while a hard requirement is unsatisfied.
2. A provider is eligible only when its contract, version, trust, isolation, and task
   constraints match.
3. Dependency resolution never grants authority; every effect is still policy-gated.
4. Withdrawal or invalidation of a hard dependency blocks affected work before its
   next dependent effect.
5. Optional dependency loss produces only the declared degraded behavior.
6. Provider changes are recorded and pinned/re-authorised; no silent semantic or
   security downgrade.
7. A policy or mandate tightening can revoke a lease even if the provider remains
   available; prior ledger facts are not rewritten.
8. Cycles and ambiguous provider selection fail closed unless explicitly modelled.

### Validation scenarios

The smallest credible proof should use two concurrent tasks and a small set of fake
providers before integrating more external harnesses.

- Run a coding task and a research task simultaneously; cancel one and prove the
  other's scopes and leases remain active.
- Remove a required provider; prove only dependent components suspend and no dependent
  operation is admitted afterward.
- Add the provider back; prove reactivation occurs only after compatibility and policy
  checks and that the resolution is recorded.
- Tighten a policy mid-task; prove affected leases are revoked without rewriting the
  original mandate or prior events.
- Crash before an effect, during provider execution, after provider success, and before
  result persistence; prove recovery reconciles each state without duplicate side
  effects.
- Replace a harness at a checkpoint; prove the old generation is quiesced and
  disposed, the new generation receives only serialized checkpoint state, and the
  task ledger remains continuous.
- Force disposal to fail; prove PAA reports residual effects and refuses to claim a
  cleanly disposed scope.
- Replay the full event sequence; prove no shell command, network call, email, or other
  external effect is executed again.

### Adoption sequence without removing existing roadmap items

This is an additive interpretation of the current horizons, not a replacement or
reordering of them:

- **Horizon 1 — Hands, authority, and honesty:** while wiring the coder loop and
  permissioned tools, introduce task/run/scope IDs, effect ownership, and the minimum
  prepare/apply/result lifecycle for managed operations. Keep the first implementation
  narrow; do not build a general hot-reload framework yet.
- **Horizon 2 — Seams:** when adding the external harness and out-of-process hooks,
  define versioned capability requirements, provider resolution records, bounded
  leases, quiesce/cancel semantics, and dependency-loss behavior. Measure which
  external operations cross PAA's boundary.
- **Horizon 3 — Evidence and attention:** add crash-point fault injection, concurrent
  task isolation tests, disposal/reconciliation evidence, policy-change tests, and
  independent verification of the resulting event history.
- **Horizon 4 — Reach:** apply the same scope and dependency rules to durable,
  long-horizon task graphs, milestone checkpoints, and speculative branches. A branch
  must own its effects and be discardable without damaging another branch.
- **Horizon 5 — Platform:** only after the earlier invariants hold, expose an
  out-of-process component/pipeline SDK and package/marketplace path. Third-party
  components provide declarations and execute behind the same mediated boundaries.

This does not move an existing parked idea into active work, mark a planned item done,
or remove any validation or kill criterion. Each capability should be promoted only
when its corresponding exit tests pass.

### Open design decisions to resolve before freezing a public contract

- Which effects can be exactly inverted, which require compensation, and which must be
  retained as irreversible evidence?
- What is the minimum stable event envelope for effects, dependency resolutions,
  leases, and component generations?
- Which resource ownership model is appropriate for shared providers: reference-counted
  leases, explicit owner sets, or both for different resource classes?
- What is the policy for a provider disappearing mid-operation, especially when the
  outcome is unknown?
- Which checkpoint data can safely cross a harness-generation boundary, and how is it
  schema-versioned?
- How are dependency cycles represented if a future capability genuinely requires a
  mutually activating group?
- Which requirements must be pinned for an entire task, and which may be re-resolved
  at a checkpoint?

Do not freeze these as universal abstractions before real integrations expose the
necessary cases. Apply the existing rule of three: validate the narrow contract against
at least three concrete component/provider cases before generalising it.

### Positioning consequence

PAA should not promise that every agent component can be removed with a magical global
undo, or that every service can be swapped at any instant. Its stronger and more
credible promise is:

**Every task runs with an explicit, recorded dependency set and bounded authority;
every managed effect has an owner and a truthful recovery/disposal path; and changes
to either dependencies or lifecycle are mediated, recorded, and testable.**

That is how temporal and spatial composability become extensions of PAA's authority
and evidence thesis rather than a second, plugin-centric product direction.
