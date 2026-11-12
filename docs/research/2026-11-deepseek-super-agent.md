# Research note: DeepSeek Super-agent (Oct 31, 2026)

*Written 2026-11-06. Purpose: record what DeepSeek's Super-agent design and prototype
actually say, what they validate or challenge in PAA's direction, and how reliable
each source is. This note is the evidence base for [VISION.md](../VISION.md) and
[ROADMAP.md](../ROADMAP.md).*

## 1. What was published

There are no academic papers. DeepSeek's Harness team published three things:

| Artifact | What it is | Date |
|---|---|---|
| Design essay, "Exploring Super-agent Design: Towards a More Autonomous Agent System" (Chinese original on WeChat/Zhihu; credited to Yujia Qiao, with ~15 reviewers) | A ~30-minute architecture essay with two small experiments. It states that it is not a description of shipped DeepSeek Harness features. | 2026-10-31 |
| `deepseek-ai/dsh-superagent-prototype` (MIT, TypeScript, v0.1.0) | A research prototype for **one GitHub repository**, packaged as a bundle for DeepSeek Harness, with optional Feishu. Docs 000–008. Labelled "research prototype, not an official DeepSeek product"; parts of the essay are not implemented. | 2026-10-31 |
| Community field reports (GitHub Discussions on `deepseek-harness`) | Independent attempts to run the prototype | 2026-11-01 onward |

## 2. What it is

A system design for an always-on agent that a whole team talks to, and that does not
require a person to supervise each session. The stated premise: a person can follow
only about **3–5 active agent sessions**, so for many concurrent tasks the bottleneck
is human attention, not the model. (The essay presents this as an observation from
experience, not a measured result.)

### Five modules (essay §2–§6)

1. **Two layers.** A long-lived **Router** session talks to people and dispatches; it
   does not execute tasks and may have no shell. Isolated **Worker** sessions, one per
   task, use the strongest model and never talk to humans directly; the Router replies
   for them. This differs from an "agent team lead", which decomposes one task.
2. **Event-driven Triggers.** Chat messages, emails, review comments, and CI results
   enter as events. **Fast path / slow path**: deterministic code (source, group,
   allowlist, "respond only to explicit requests / treat as candidate / ignore")
   filters first; only survivors reach an LLM judging against prompt-written policy.
   Events in one **Thread** are routed by **subscription**: after the Router creates a
   Worker, follow-ups go straight to it ("pass-through"). Subscriptions are
   many-to-many so Workers on the same PR see each other's events.
3. **Context as a cache.** *Inline* likely-needed information, *Spill* the rest behind
   a pointer, and give every object an ID that is inlined into related objects, so the
   model can follow references hop by hop (**Chain of Recall**). Also *speculative
   execution*: reproduce and fix in the background; speak only if it worked.
4. **Full-duplex, multi-channel interaction.** Long tool calls become asynchronous
   `start()` / `stop()` with results delivered later as events (implemented in
   DeepSeek Harness, off by default). Output is shaped per channel; IM replies are
   capped at 100 words.
5. **Separated compute, state, and environment, plus a Watchdog.** The agent loop runs
   in stateless cloud containers; session state lives in a database; a small daemon on
   each user machine only runs commands ("bring your own machine"). A separate
   **Watchdog** session on a timer handles infrastructure faults and judges whether
   each Worker is making real progress or has drifted.

### The prototype's domain model (docs/001–003)

This is the most reusable part and is more rigorous than the essay.

- **Source → Trigger → Thread → Eval → Decision → Ticket (with Mandate) →
  Assignment → Executor**, with results returned through a **Channel**.
- **Trigger**: one immutable event; *accepting a Trigger twice creates one Ticket*.
- **Eval** has two layers: a deterministic **gate** (who may originate work, and with
  what strength: *explicit* or *inferred*) and a model **assessment**. It is stateless
  and runs serially per Thread, merging Triggers that arrive meanwhile.
- **Mandate**: immutable record of why work is allowed and how far: the **policy
  version**, scope, strength, matched rules, the sender's identity. Wider scope means
  a new Ticket. A person's stop withdraws it; a policy that no longer holds it revokes
  it.
- **Accept mode** per policy: `off` / `observe` (default: record "would accept", start
  nothing) / `auto`. Explicit requests work in every mode. Work nobody asked for needs
  a team-written rule and starts in observe mode.
- **Stored work**: what happens after a decision "is stored first and done
  afterwards", then retried with backoff (2 s doubling to 1 min; model/network errors
  retried three times; launch failures fail the Ticket).
- **Restart recovery**: interrupted Assignments restart with a note telling the worker
  to check its directory, git history, and GitHub before repeating any outside action.
  Read/unread input accounting survives restarts.
- **Panel** shows only what needs a person. Philosophy: *take as little human
  attention as possible and give as much value as possible*; speak only when a person
  must act or can use a result; "do not use it to produce volume".
- **Executor is a port.** The prototype's Executor is a DeepSeek Harness session plus
  a `git worktree` directory per Ticket, but the domain model puts the Executor
  outside the domain, connected only through Assignments.

### Experiments (essay §7), as reported

Synthetic stream: about 300 messages per run, about 20 of them work requests, five
runs per design. **Coding was replaced by a tool that only consumes time**, and
message and work durations were scaled to 1/4.

| Design | p50 / p95 completion (min) | Cache-read tokens per run |
|---|---|---|
| Super-agent | 12.4 / 15.7 | 23.2M |
| Super-agent without pass-through | 12.6 / 15.5 | 31.8M |
| Single session + subagent tool, neutral prompt | 20.4 / 29.5 | 6.1M |
| Single session + subagent tool, delegate-first prompt | 13.8 / 29.0 | 10.8M |

Router stress test (real model for Router, mock Workers): from ~1 to ~40 triggers per
minute, mean batch size 1.0 → 2.8, per-request time 2.5 s → 3.9 s, amortised time per
decision 6.4 s → 1.6 s.

### Open problems the authors list (essay §8)

Scaling the Router (sharding vs. shared context); long-term memory over months;
presenting work once sessions are hidden; context boundaries and agent-to-agent
communication; and **security**: a compromised cloud loop could control every
connected machine, and credentials must be usable without ever entering model
context.

## 3. How to read the evidence

- The headline timing result measures **scheduling with simulated work**. It shows
  that a single session with a neutral prompt never delegated and was interrupted by
  new messages. It says nothing about code quality, and the design used about
  3.8× the cache-read tokens of the neutral single session.
- n = 5 per design, authors call the results preliminary.
- A single working instance at DeepSeek is reported ("runs continuously"), but there
  are no published outcome metrics (merge rate, defect rate, reviewer time).
- GitHub stars (reported in the thousands within days) measure attention, not use.

## 4. What the field reports found

Independent users ran the prototype within days and filed problems. These are
**correctness-under-failure** issues, not routing issues:

- A provider retry could reach a worker **after its authorization had changed**.
- A stopped Ticket's **prepare command could keep running** and hold capacity after the
  Ticket ended; a failed prepare could leave a partial directory.
- The GitHub Source **reported a successful poll while skipping events** when more
  than 100 items shared a timestamp.
- **Notification delivery was never checked**, so a Source stayed "ready" while
  silently missing requests.
- Test suite failed under a **non-English locale**.
- Several **POSIX-only assumptions** (tmux, `fcntl`, `sh`) blocked a Windows run.
- **Thread IDs omit the repository**, so changing the watched repository collided
  with old threads.
- The authors' own docs state that blocked-command wrappers and per-Ticket directories
  are "defaults, not a security boundary", that executor sessions "run commands
  without approval as the operator's OS user", and that a worker can still run a
  program by absolute path.

## 5. What it means for PAA

### Validates

| Super-agent idea | PAA equivalent today | Reading |
|---|---|---|
| "Stored first, done afterwards" | Ledger before side effect | Independent convergence on PAA's core rule |
| Accepting a Trigger twice creates one Ticket | Idempotency keys with attempt counter (ADR-0008) | Same problem, same remedy |
| Deterministic gate, then model assessment | "AI proposes, code decides"; policy agent | Same pattern |
| Mandate records policy version | `POLICY_CLEARED`, human attestation gates | PAA records the decision but not the *requester* or policy version as first-class |
| Recovery note after restart | Boot sweep, checkpoint rollback | PAA verifies the workspace; Super-agent relies on the agent re-checking |
| Observe mode before autonomy | None | **New idea** worth adopting |
| Context IDs and reference graph | Context engine, causal links | Compatible |

### Gaps in Super-agent that are PAA's existing strengths

Containment (sandbox backends); deterministic validation of patches (AST, rollback);
tamper-evident history and replay; checkpointed workspace recovery; a credential proxy
design in the Skill Adapter. Super-agent has none of these, and its authors list
security as an open problem.

### Gaps in PAA that Super-agent has addressed

Event intake and eligibility (who may ask, with what strength); dispatch across many
concurrent work items; an attention-minimising panel; progress/drift supervision
(Watchdog); asynchronous tool calls with cancellation; channel-shaped output;
per-ticket workspace isolation as a default.

### Net reading

Super-agent is the **front half** of an autonomous-work system: *who may start work,
and how it is routed*. PAA is the **back half**: *contain it, verify it, record it,
recover it*. The two are complementary, not competing, and the missing joint is
**authority plus evidence**: a record that links *who authorised this and under which
rules* to *what actually happened and whether it was verified*.

### Risks it introduces

- A frontier lab has open-sourced the front half under MIT. Rebuilding it from scratch
  is poor use of a solo maintainer.
- The lab will likely add stronger containment itself; the independent evidence
  layer is differentiated only if it stays vendor-neutral and verifiable offline.
- The prototype is built on its own harness, not a neutral protocol. Whether its
  Executor port can be implemented by something other than a DeepSeek Harness session
  has **not been verified** from code.

## 6. Source reliability

| Source | Reliability | Notes |
|---|---|---|
| Repo docs 000, 001, 003 (read in full via GitHub and raw) | **High** (primary) | Authoritative for the prototype's behaviour |
| Repo docs 004, 006, 007 (excerpts only) | High, partial | |
| Repo docs 002, 005, 008 and source code | **Not read** | Authorization details (002) are a notable gap |
| Essay, English machine translation reproduced in a third-party GitHub issue | Medium-high | Machine translation; a Chinese original is included alongside and agrees on every section read |
| Essay, original on WeChat/Zhihu | Not fetched | |
| Third-party summaries (AgentsPulse, CellCog, MindStudio, etc.) | Medium | Consistent with the essay; some vendors have a stake |
| GitHub Discussions field reports | Medium | First-hand but individual, mostly unverified by maintainers |

## 7. Sources

- Essay, English machine translation with Chinese original:
  https://github.com/yu-iskw/agent-works/issues/184
- Prototype: https://github.com/deepseek-ai/dsh-superagent-prototype
  (docs 000 Philosophy, 001 Domain model, 003 Execution; 004, 006, 007 in part)
- Field reports: https://github.com/deepseek-ai/deepseek-harness/discussions/11706,
  /11769, /11774, /11801, /11818
- Reviews: https://agentspulse.github.io/tutorials/deepseek-super-agent-architecture/,
  https://cellcog.ai/blog/deepseek-super-agent/
