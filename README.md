# PAA — Personal Autonomous Cognitive Operating System Runtime

A local-first, event-sourced substrate for running autonomous agents on personal hardware. I built this off my own v4.0/v4.1 RFC, and adapted it wherever the spec didn't actually run on my machine or didn't work as written.

**Status:** ~28,000 lines, 755 tests passing. The event-sourced core, context engine, storage layer, and sandbox are done and tested. A few subsystems are written but not tested yet — see the build status table below, I'm not going to pretend those are further along than they are.

## What this actually is

Most agent frameworks keep task state in process memory. Kill the process, task's gone. My starting premise (from the RFC) is that the ledger is the only source of truth — everything else, queues, caches, even the filesystem, is a derived view you can always rebuild by replaying events.

That one decision is what buys you:

| Property | How |
|---|---|
| Crash recovery | Boot sweep replays the ledger, checks the workspace against a checksum manifest, rolls back drift, re-queues what's safe |
| Tamper evidence | Every event commits to the previous one's hash, so editing history breaks every digest after it |
| Bounded context | Hard token ceiling enforced by the gatherer — there's a property test asserting it can't be broken |
| Deterministic safety | AST scan, schema validation, and real test runs gate every mutation. No model sits in the security loop |
| Explainability | Causation links let you walk backwards from any action to whatever event caused it |

## Quick start

```
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest tests/ -q      # 1138 passed, 6 skipped
```

Run it from the CLI (local model via Ollama, or `echo` if you're offline):

```
paa doctor                       # what backends/sandbox are actually live
paa submit "summarise my notes"  # run a task to completion
paa ledger <correlation-id>      # walk the event chain, see why it did what it did
paa recover                      # post-crash boot sweep
paa serve                        # FastAPI ingestion + background daemon (loopback only)
```

Optional extras, each with a fallback if you skip it:

```
uv pip install -e ".[vector]"       # Qdrant embedded, else numpy brute force
uv pip install -e ".[graph]"        # KuzuDB, else SQLite recursive CTEs
uv pip install -e ".[embeddings]"   # real embeddings, else a hash embedder (~2GB torch if you want real ones)
uv pip install -e ".[api]"          # FastAPI ingestion
```

No Docker, no server, no GPU required.

## This machine vs. the RFC

The RFC assumes a CUDA workstation. My laptop is not that. I've been running this on a Ryzen 5 7535HS, 13.3 GB RAM (about 3.5 GB free most of the time), a Radeon 660M iGPU with 2GB VRAM, Windows 11, no Docker. Those are two different machines and I had to actually resolve the gap instead of hand-waving it.

| RFC wants | What I've got | What I did instead |
|---|---|---|
| vLLM + Llama-3-8B-Q8 at 85% VRAM | vLLM only ships Linux wheels, needs CUDA/ROCm, and I have 2GB VRAM against an ~8.5GB requirement | Provider-agnostic layer, local-first with logged escalation (ADR-0007, ADR-0015) |
| Concurrent Mistral-7B critic + 8B curator | Would need ~20GB more RAM than I have | One configurable provider — the critic's authority is deterministic, not generative, so it doesn't need its own model |
| gVisor `runsc` sandbox | It's a Linux userspace kernel, doesn't exist on Windows | Pluggable Sandbox interface; Windows backend uses Job Object caps + workspace jail + AST pre-scan. It's materially weaker than gVisor and I'm not going to pretend otherwise (ADR-0006) |
| PostgreSQL + Qdrant + Redis + MinIO | No Docker on this box, and that stack idles at 4-6GB RAM | SQLite (WAL) + Qdrant embedded + SQLite queue + content-addressed blob store. Idles at basically 0MB (ADR-0001–0005) |
| KuzuDB embedded | win_amd64 wheel exists, so this one's fine | used as specified |
| 384-dim embeddings | MiniLM-L6-v2 is 384-dim and runs on CPU, also fine | used as specified |

Every substitution keeps the same logical contract as the RFC. Swapping back to Postgres or gVisor later is a backend change, not a rewrite — that's the whole point of writing the ADRs down.

## Bugs I found in my own spec

Implementing the RFC literally would have produced something that deadlocks, leaks memory, or silently drops work. Fixed all of these, reasoning's in `docs/adr/`.

**1. The idempotency scheme makes retries impossible.** (ADR-0008) `SHA256(correlation_id + event_type + state_version)` under a UNIQUE constraint means `EXECUTION_STARTED` can only ever be written once per task. So a task that fails on its first attempt can never get a second one — recovery deadlocks on exactly the tasks it's supposed to be rescuing. Fixed with an attempt counter plus a discriminator.

**2. The recovery index never shrinks.** (ADR-0010) A partial index that excludes terminal events never sheds rows on an append-only table, so every event from every completed task stays indexed forever. Boot recovery gets slower for the entire life of the system. Replaced it with a bounded head projection — one row per lineage.

**3. `EXECUTION_COMPLETED` can't actually be terminal.** (ADR-0010) It fires once per step, not once per task. Treat it as closing the whole task and recovery marks a half-finished plan as done and silently skips the rest of the steps. Caught this with a test, not by reading the code.

**4. The sandbox kill rule has the wrong units.** (ADR-0009) `∫MemoryUsage(t)dt > Ceiling` gives you MB·seconds. That never fires on an actual spike and always fires on a long, benign run. Swapped it for peak RSS, which is what cgroups and Job Objects actually measure.

**5. `A_score` divides by things that can be zero.** (ADR-0012) Dividing by `pollution · hallucination_risk` (both in [0,1]) sends the score to infinity as either approaches zero. Rewrote it with `(1 + x)` denominators — same monotonic behavior, now bounded.

**6. Temporal decay was hitting the wrong term.** (ADR-0013) Operator precedence in `R_hybrid` only decayed the graph term, so stale semantic matches never got damped. Applied it to the whole score instead.

**7. Friction score divides by zero for exactly the domains that need it most.** (ADR-0016) `F_ops` divides by "Total Successful Commits" — which is zero for a domain that's only ever failed. That's precisely the domain that needs reflection.

**8. Pydantic v1 syntax.** My own `SkillContractModel` used `regex=` and `@validator`, neither of which run on Pydantic v2.

Found four more of these during the build, through tests rather than by reading the code:

- Keyset vs OFFSET pagination — deleting rows while paginating with OFFSET silently skips some. Those never get evicted and nothing tells you.
- Recovery-sweep idempotency — keying the audit event on a version that the event itself bumps grows the ledger by one row per boot, forever.
- zstd size sentinel — `stream_writer(size=None)` raises. The library wants `-1`. A declared-size mismatch fails inside the write, so the explicit check I'd written below it was dead code.
- Non-deterministic claim order — tiebreaking "oldest first" on a random UUID shuffles same-instant arrivals, so ingestion order quietly stops meaning anything under load, right when it matters most.

## Architecture

```
                        ┌──────────────────────────┐
   ingestion ──────────►│  cold_lake (immutable)   │  raw signals, CAS blobs
                        └────────────┬─────────────┘
                                     │
                        ┌────────────▼─────────────┐
                        │  system_state.ledger     │  ◄── SOURCE OF TRUTH
                        │  append-only, hash-chain │      everything else derives
                        └────────────┬─────────────┘
                                     │ replay (left fold)
                        ┌────────────▼─────────────┐
                        │   TaskProjection         │
                        └────────────┬─────────────┘
                                     │
   ┌─────────────┬───────────────────┼───────────────┬──────────────┐
   ▼             ▼                   ▼               ▼              ▼
hot_serving   vector index      graph edges      queue          workspaces
(truth)       (recall)          (provenance)     (volatile)     (host disk)
```

Task lifecycle — every arrow writes a ledger event first:

```
TASK_REQUESTED → TASK_QUEUED → CONTEXT_HYDRATED → PLAN_COMPILED
      → POLICY_CLEARED → EXECUTION_STARTED → CRITIQUE_CONCLUDED
      → EXECUTION_COMPLETED → MUTATION_COMMITTED
                    │
                    ├── POLICY_BLOCKED / SECURITY_VIOLATION  (terminal)
                    ├── AWAITING_HUMAN_ATTESTATION           (parks, never auto-clears)
                    └── STATE_ROLLBACK_TRIGGERED             (drift → restore)
```

Ledger before side effect, always. A crash between writing the event and doing the work is recoverable — replay knows the work was intended. A crash between doing the work and writing the event isn't recoverable. Ordering it this way means the only failure mode you can hit is the survivable one.

## A few design calls worth explaining

**The router is optional.** Mostly it doesn't need to run — `should_route()` returns `False` if you name a target agent, or if fewer than 3 agents are even eligible. When it does run, it uses an LLM rather than a small classifier, because a compact intent model sees the request string but not the project state. It can't tell "fix the login bug" (one file) apart from the same words meaning a full auth rewrite.

**Agents can call each other without deadlocking.** The RFC forbids peer calls; I wanted them anyway. Turns out both are satisfiable, because the RFC's actual concern is blocking call graphs, not interaction in general. Delegation goes through the orchestrator, which owns the delegation graph and refuses any edge that would close a cycle. Real multi-agent interaction, safety property preserved by construction instead of by a blanket rule. (ADR-0018)

**The context builder only uses AI for half the job.** AI proposes what to look for — query expansion, slot inference. Deterministic code decides what actually gets in — ranking, the ceiling, routing. If a model picked packet contents directly, text pulled from an untrusted source could talk its way into the planner's context, and packets would stop being reproducible, which breaks replay.

**18 memory domains, 6 real mechanisms.** The RFC lists 18 rows but most of them differ only by label — "Skill Memory" and "Procedural Memory" are the same table with the same policy under two names. Kept all 18 as vocabulary; each one maps onto one of six actual mechanisms (volatile, relational, immutable log, vector, graph, document). Adding a domain is a data change now, not a new subsystem. (ADR-0014)

**On meta-learning — what's real here and what isn't.** I looked at MAML, prototypical networks, memory-augmented nets. Gradient-based MAML needs a differentiable model, a labelled task distribution with support/query splits, and training infra I don't have. A file called `maml.py` that doesn't actually do MAML would be worse than just not having it. Prototypical few-shot classification is the one that actually fits what I need — prototypes as mean embeddings of past task exemplars, classify by nearest prototype with a confidence margin, return "unknown" instead of guessing when the margin's thin. No gradients, no training loop, and it gets me the "adapt from a few examples" property I actually wanted. (ADR-0017 — designed, not built yet.)

**On matching something like Claude Code.** Worth being straight about this: architecture doesn't close a model-capability gap. Agentic coding performance is mostly the reasoning model, not the harness. A well-built harness around an 8B model gets you a well-behaved 8B agent, nothing more. What this design does beat a stateless coding agent at: crash recoverability, durable memory across sessions, deterministic validation, auditability. So: equal or better on recoverability/memory/safety, dependent on whatever model you plug in for raw reasoning — which is exactly why the model layer is swappable. (ADR-0015)

## Why this over what already exists

I built this because I wanted something that doesn't exist as a single package yet. Let me be honest about what I'm comparing to and where PAA actually wins:

**vs Claude Code** (frontier reasoning agent): Claude Code will always out-think anything I can fit on this hardware. That's not what I'm competing on. What I get instead is: if a task crashes mid-execution, I get it back with full recovery. If I'm offline for a week and come back, I have complete history and can resume where I left off. I can point it at a local model, or a cheap API, without giving my code to a third party. Most importantly, I have an audit trail of every decision — I can walk backwards through the ledger and understand *why* it did something.

**vs OpenClaw** (skill/agent ecosystem): OpenClaw is an excellent open-source framework for connecting agents to tools. But it doesn't have event-sourced recovery, deterministic validation, a sandbox that's actually restrictive, or signed packages with dependency isolation. PAA is built around the assumption that you want those guarantees before you install someone else's code.

**vs Hermes/Jarvis** (ambient assistants): These are great at always-on operation and multi-channel presence. I'm not there yet — I haven't built the ingestion bridges, the proactive loops, or the mobile layer. But once I do, mine will have something those don't: if you ask "what did I decide about auth in March?", the system can actually walk the causal chain and show you the reasoning, not just return a fuzzy search result.

**The real differentiation** is the combo: event-sourced recovery + crash-safe operation + deterministic safety + persistent memory + an open skill economy, all on hardware you own. None of those individually are novel; the fact that they coexist in one running system is the point.

**What this *isn't***: it's not trying to out-reason a frontier model. It's not a consumer product yet (no UI, no installer wizard). It's not multimodal. And I haven't implemented the three products on top yet (the coder, the skill platform, the ambient assistant). The foundation is solid; the floors aren't built.

## Known limitations and what I'm actively working on

**Load-bearing gaps** (these block actually using the system day-to-day):

- **No concrete tools wired up.** The orchestrator can dispatch skills, but there are almost no built-in skills for real work — no file editing, no bash, no web search, no git. The coder loop isn't connected to the orchestrator yet. **In progress:** Phase 1 of the vision adds a real tool suite.
- **No UI beyond CLI.** A TUI exists on paper; there's no web dashboard, no streaming output to the terminal, no way to watch a task run. **Planned:** Phase 3 adds this.
- **Observability is untested.** Metrics and tracing code is there but never validated. You can't actually see what's slow or verify the token ceiling in action. **In progress:** Phase 0 (the build-status roadmap) covers this.

**Capability gaps**:

- No multimodal (vision, voice, document reading). If you want to work with images or audio, you'd have to add that yourself.
- No codebase indexing or LSP integration. The context builder doesn't know anything about your repo's structure; it's generic semantic retrieval.
- No git integration. Changes don't automatically become commits; there's no branch-per-task or conflict awareness.
- No ingestion bridges. You can't talk to PAA via Telegram, email, or Slack yet — it's CLI/HTTP only.
- No learning harness. The self-improvement loops exist on paper but aren't validated. There's no A/B testing, no eval framework, no way to measure whether custom skills are actually helping.

**Product gaps**:

- No marketplace yet. The 5-gate installer and signed package system are built, but there's nowhere to publish a skill and no user/payment model.
- No auth or multi-user. Right now this is single-user, single-machine.
- No mobile or remote access. You run it on one box; if you're away from that box, you can't talk to it.
- No cost/budget dashboard. You can't see how much you're spending on API calls or which models are used most.

**Honest weaknesses on the Windows backend** (the machine I built and test on):

- The sandbox is weaker than gVisor on Linux. It uses Job Objects + workspace jails + AST pre-scan, which is multiple layers of defense but not formally proven equivalent to a userspace kernel. If you're running untrusted code, you should use the WSL2 or Docker backend.
- Performance tuning isn't done. Everything works, but nothing is optimized for latency yet.

## The vision: what this becomes

I've written out a 10-phase roadmap for what PAA grows into. The honest summary is:

1. **Phases 1–3**: Add real tools (read/write files, bash, web, browser), add codebase intelligence (LSP, git, symbol awareness), add UIs (TUI, web dashboard, the "like xAI" search over your entire history).
2. **Phases 4–6**: Ambient presence (ingestion bridges from Telegram/email/Slack, proactive suggestions, multimodal).
3. **Phases 7–9**: Learning loops (self-improving on your own tasks, pluggable agentic loop strategies, multi-agent reasoning patterns like debate and ensemble).
4. **Phase 10**: Security hardening, scaling to multiple machines, marketplace maturity, team mode.

The end state is three products on the same foundation:
- **A Claude-Code-like coding agent** that remembers your codebase, survives crashes, and you can run offline.
- **A skill marketplace** (like OpenClaw) that's signed, sandboxed, has revenue splits, and lets you sell your own agents/skills.
- **An ambient assistant** (like Hermes) that's always-on, reachable from anywhere, remembers everything, and proactively offers help.

All three run on the same ledger-backed, crash-recoverable, deterministically-safe foundation. The reason this matters is that each layer trusts the layer below it — the marketplace trusts the sandbox, the ambient layer trusts the memory system, the coding agent trusts the crash recovery. That chain of trust is the moat.

**Guiding principles I'm not dropping:**

1. The ledger is truth; everything else is a cache you rebuild on boot.
2. Deterministic where it matters — safety, validation, and money never depend on a model's mood.
3. Local-first, escalate on purpose — privacy is the default; the network is a deliberate, logged choice.
4. No silent capability without a gate — every new power comes with a permission and an audit trail.
5. Honesty over hype — if a weaker model can't do something, say so and route around it.

## Build status

"Tested" means tests exist and pass. "Untested" means it imports fine but I haven't verified it — treat it as unfinished, because it is.

| Subsystem | State | Tests |
|---|---|---|
| Core types, config, permissions | tested | via ledger suite |
| Event ledger + replay + recovery | tested | 71 |
| Bounded context engine (metrics, budget, gatherer, compaction) | tested | 353 |
| Storage (vector, graph, cold lake, queue, relational) | tested | 255 (+5 skipped, Redis) |
| Sandbox (subprocess/WSL/docker/dryrun + recursion guard) | tested | 39 |
| Memory decay + domains + contradiction | tested | 37 |
| Agents (orchestrator, delegation, policy, context, planner, critic, router, worker) | tested | 70 |
| Composition root + end-to-end integration | tested | 16 |
| Deterministic validation (AST, patch, workspace, tests) | exercised via agents, no dedicated suite | — |
| Model providers + escalation router + platform registry | tested (OpenAI/Groq/Gemini/etc + localhost) | 71 |
| Memory creator/curator + world model | tested | 76 |
| Unified Skill Adapter (Claw Hub + MCP) | tested | 51 |
| Marketplace (signed packages, 5-gate installer) | tested | 18 |
| Self-improvement + meta-learning | tested | 22 |
| Claude-Code features (permissions, hooks, loop, sessions) | tested | 39 |
| FastAPI ingestion + daemon + CLI | tested | 27 |
| Docker deployment + Postgres/MinIO backends | built, live-server run still needs a pass | parity + backend |
| Observability (metrics, tracing, runs, logging) | built, untested | — |

Total: 1138 passing, 6 skipped.

You can drive the runtime from the CLI (`paa submit/status/ledger/recover/gate/doctor/serve`) or over HTTP (loopback FastAPI), and there's a background daemon that runs boot recovery, drains the task queue, ingests signals, and runs the decay sweep.

`Runtime.build()` in `src/paa/runtime.py` wires the whole thing together, and `tests/integration/` proves all five DoD items in a single run: full lifecycle to `MUTATION_COMMITTED`, crash-and-recover, a human gate parking across a restart, deterministic security blocks, and the token ceiling actually holding.

Validation, model providers, and observability still only get exercised indirectly through the agent and integration suites — they need dedicated coverage of their own. Observability's the only one with zero exercise right now. See `docs/ROADMAP.md` for what's next, and read that alongside this — it's a more honest picture of what's actually wired up end to end versus what's just tested in isolation.

## Layout

```
src/paa/
├── core/          types, errors — imported by everything, depends on nothing
├── config.py      every RFC constant, with its section cited
├── ledger/        events, store, replay, recovery      ← the spine
├── storage/
│   ├── relational/  SQLite WAL + schema (Postgres-portable)
│   ├── vector/      Qdrant embedded / numpy fallback
│   ├── graph/       Kuzu / SQLite recursive CTE
│   ├── coldlake/    content-addressed blobs + zstd
│   └── queue/       durable SQLite queue / Redis
├── context/       metrics, budget, gatherer, compaction
├── memory/        domains, decay, contradiction
├── agents/        base, delegation, orchestrator, policy, context, reasoning
├── sandbox/       pluggable containment backends
├── validation/    AST scan, patch apply/rollback, workspace manifests
├── models/        provider abstraction + escalation router
├── skills/        Unified Skill Adapter
└── observability/ metrics, tracing, execution runs
```

## Testing

```
.venv/Scripts/python.exe -m pytest tests/ -q                 # everything
.venv/Scripts/python.exe -m pytest tests/ledger -q           # the spine
.venv/Scripts/python.exe -m pytest -m chaos -q               # crash injection
```

Uses `hypothesis` for the properties that need to hold universally — the gatherer never exceeding its ceiling, replay never raising on any event ordering, confidence never leaving [0,1].

Crash tests simulate power loss by closing the database mid-lineage and reopening it: committed transactions survive, uncommitted ones vanish, exactly how SQLite behaves under WAL.

## What's next (the roadmap)

**Immediate (Phase 0 of the build roadmap):** Test the untested subsystems — especially agents and validation. Right now, security properties are asserted only in docstrings, which isn't strong enough. Once the suite is green, I can ship this with actual confidence.

**Short-term (Phase 1 — Real capabilities):** Wire up concrete tools. The orchestrator is architecturally sound but has no hands. File read/write, bash, web search, git — none of these are hooked in yet. This phase is the delta between "impressive substrate" and "something you use every day."

**Medium-term (Phase 2–3):** Codebase intelligence (LSP, symbol indexing) and UIs (TUI, web dashboard, history search). Right now you can only drive this from the command line. Add the visualizations and the system stops being opaque.

**The modular architecture piece:** I'm also building "pluggable agentic loops" as a separate system. Right now the execution strategy is hardcoded: hydrate → plan → policy → execute → commit. I want that to be swappable — so you can install a Tree-of-Thought pipeline, or a ReAct loop, or your own strategy as a signed package, and the system runs it with the same recovery/safety guarantees. This is in `docs/EXTENSIBILITY_PIPELINES.md` if you want the full design.

**My actual v-next focus:** long-horizon tasks. The ledger and snapshot machinery is already the right foundation — a task spanning weeks is just a lineage that survives many restarts. Once tools and the coder loop are wired, this is where the effort goes.

See `docs/ROADMAP.md` and `docs/VISION.md` for the detailed, phased breakdown of the next 10 phases.