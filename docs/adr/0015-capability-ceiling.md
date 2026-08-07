# ADR-0015: On matching Claude Code — what architecture can and cannot buy

## Status

Accepted. This ADR exists to prevent a predictable disappointment.

## Context

The project brief includes the requirement:

> It should perform equal to or better than Claude Code

alongside a specification pinning the reasoning layer to a local
`Llama-3-8B-Instruct-Q8_0.gguf` served by vLLM, with a `Mistral-7B` critic.

These two requirements are in direct conflict, and no amount of architectural
work resolves it. This needs saying plainly rather than being discovered after
the system is built.

## The gap is in the model, not the scaffold

Agentic coding performance is dominated by the reasoning model's capability.
On the benchmarks that track this kind of work (SWE-bench Verified, Terminal-Bench
and similar), the spread between an 8B open-weights model and a frontier model
is not a few percent — it is most of the task distribution. An 8B model
struggles to hold a multi-file refactor in working memory, reliably emit valid
structured output under a schema, or recover from its own mistakes. Those are
the three things an agentic coding loop does constantly.

Retrieval quality, context discipline, sandboxing and validation — everything
this runtime is good at — raise the *floor*. They stop the system doing damage,
they keep it recoverable, they cut wasted tokens. They do not raise the
*ceiling* on reasoning. A perfectly engineered harness around an 8B model
produces a well-behaved 8B agent.

Additionally, on the target hardware the RFC's inference stack cannot run at
all (see ADR-0007): vLLM ships Linux-only wheels and requires CUDA/ROCm, while
the machine is Windows with a 2 GB AMD 660M iGPU and ~3.5 GB free RAM. A Q8
8B model needs roughly 8.5 GB of weights.

## Decision

Three things, in order of importance.

**1. Make the model layer swappable, and make capability a configuration
choice rather than an architectural commitment.** `paa.models` exposes a
provider interface; the runtime does not care what is behind it. Matching
Claude Code is then a question of what you point it at, not of rewriting the
system.

**2. Default to local-first with explicit, ledger-logged escalation.** Cheap
local models handle routing, classification, extraction and summarisation —
work where an 8B model is genuinely adequate and where the volume makes API
calls wasteful. Tasks classified `COMPLEX` or `MAX` may escalate to a frontier
model. Every escalation writes a `MODEL_ESCALATED` event, so the privacy
boundary is auditable rather than implicit. `LOCKDOWN` mode forbids escalation
outright, keeping the air-gap promise available when it is wanted.

**3. Compete where architecture actually wins.** There are real advantages
this design holds over a stateless coding agent, and they are worth building
deliberately:

- **Crash recoverability.** A killed session resumes at the exact step, with
  filesystem drift detected and rolled back. Most agent runtimes lose the task.
- **Durable cross-session memory.** Facts, provenance and relationships persist
  and decay on a schedule rather than living in a context window.
- **Deterministic validation.** AST scanning, schema enforcement and real test
  runs gate every mutation, with no LLM in the security loop.
- **Auditability.** A tamper-evident hash chain answers "why did it do that?"
  by walking causation links, not by guessing.

## Consequences

- The honest framing is: **equal to or better than Claude Code *on
  recoverability, memory persistence, and safety guarantees*; dependent on the
  configured model for raw reasoning.** That is achievable and worth building.
  "Better at agentic coding using a local 8B" is not, and should not be
  promised.
- If air-gapped operation is the priority, accept the capability cost
  explicitly and set `PermissionMode.LOCKDOWN`.
- If coding capability is the priority, configure the escalation provider and
  accept that reasoning calls leave the machine. Memory, ledger, workspaces and
  telemetry still never do.
- Revisit when local models close the gap. The provider abstraction means that
  is a config change, which is the entire point of decision 1.
