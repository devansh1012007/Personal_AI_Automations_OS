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
