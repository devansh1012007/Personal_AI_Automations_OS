# Pluggable Agentic Loops & Pipelines — Design Plan

*How to make the execution strategy itself a swappable, installable component —
so you (or anyone) can drop in a new agentic loop or pipeline, possibly better
than the built-in one, and the runtime uses it with no core changes.*

---

## The goal, stated precisely

Today the task lifecycle is **hardcoded** in `ChiefOrchestrator.run()`:

```
hydrate → plan → policy → execute(steps + critique + retry) → commit
```

That is *one* pipeline. The goal is to make "which pipeline runs this task" a
**choice** — config, per-task, or per-session — and to let a pipeline arrive as
an **installed package** (like a skill), written by someone who never saw the
runtime's internals, and have the system run it safely.

A "pipeline" (or "agentic loop") here means: **the strategy that turns a task
into committed work** — the control flow between model calls, tools, and agents.
Examples the system should be able to host: the current plan-execute-critique
loop; a ReAct think→act→observe loop; a Tree-of-Thought planner; a debate/
ensemble loop; a plan-and-solve loop; a fully custom user-written loop.

## The one hard constraint everything bends around

**The ledger is truth, and recovery replays it.** If a custom loop does its work
without emitting ledger events, then a crash mid-run cannot be recovered, and the
system's single biggest advantage over a stateless agent evaporates. Two other
invariants ride along: **every mutating action passes policy + sandbox +
validation**, and **budgets/recursion guards are enforced**.

So the design is not "run arbitrary code." It is: **the runtime owns the
guarantees (ledger, recovery, safety, budgets); the pipeline owns the strategy
(what to think, when to act, how to decide).** The boundary between them is a
capability surface — an SDK — that makes recording and safety the *path of least
resistance*, so a well-behaved pipeline gets them for free and a misbehaving one
is contained.

## What's already pluggable (and what isn't)

**Already flexible** — reuse these, don't reinvent:
- `Runtime.build()` injects every collaborator; swapping one is a constructor arg.
- The model layer is a registry of providers behind one interface.
- Skills (USA), agents, and marketplace packages already install + register.
- `coder/AgentLoop` is a *second, self-contained loop* with `ToolCallingModel` +
  `Tool` protocols — living proof that "a loop is an object with a `run()`" works.
- Delegation graph, recursion guards, permission modes, sandbox, validation —
  all reusable scaffolding a pipeline can call.

**Not yet flexible** — the work of this plan:
- `ChiefOrchestrator.run()` *is* the pipeline; it can't be replaced or chosen.
- There's no pipeline abstraction, registry, package kind, or selection policy.
- There's no stable SDK a third-party loop could depend on across versions.
- The two loops that exist (`orchestrator.run` and `coder.AgentLoop`) don't
  share an interface, so neither is swappable for the other.

---

## The core abstraction: `Pipeline` + `RuntimeContext`

Two new protocols. This is the whole idea; everything else is plumbing around it.

### 1. `Pipeline` — what a custom loop implements

```python
class Pipeline(Protocol):
    name: str
    version: str                       # semver of the pipeline itself
    requires_runtime: str              # semver range of RuntimeContext it needs
    required_permissions: tuple[Permission, ...]

    async def run(self, task: TaskSpec, ctx: RuntimeContext) -> PipelineResult:
        """Drive one task to a terminal state, using ctx for all capabilities."""

    async def can_handle(self, task: TaskSpec) -> float:
        """0..1 confidence this pipeline suits the task (for auto-selection)."""
```

A pipeline is judged only by two things: it produces a terminal
`PipelineResult`, and it did everything through `ctx`. It is otherwise free —
ReAct, ToT, debate, a hand-rolled state machine, anything.

### 2. `RuntimeContext` — the capability surface (the SDK)

This is the *only* thing a pipeline touches. It is deliberately a **narrow,
versioned facade** over the runtime — not the `Runtime` object itself — so
internals can change without breaking installed pipelines, and so every powerful
action is funnelled through a recording + gating path.

```python
class RuntimeContext(Protocol):
    correlation_id: uuid.UUID
    session_id: uuid.UUID | None
    budget: Budget                      # tokens/time/nodes; enforced

    # --- recording (this is how recovery keeps working) ---
    async def emit(self, event: EventType, payload: dict) -> None
    async def checkpoint(self, label: str, workspace: Path | None = None) -> None

    # --- capabilities, all pre-gated ---
    async def think(self, prompt, *, schema=None, modality=None) -> Any   # model call, budgeted + escalation-aware
    async def call_agent(self, role, message) -> AgentResult              # reuse the 9-agent hierarchy
    async def use_skill(self, name, args) -> SkillResult                  # USA: permission + sandbox + validation
    async def recall(self, query, *, k=10) -> list[Fact]                 # bounded context / memory retrieval
    async def remember(self, fact) -> None                               # write through the memory creator
    async def gate(self, reason, risk) -> bool                           # request human attestation
    def sub_context(self, **overrides) -> RuntimeContext                 # child budget (halved), delegation-guarded
```

**Why this shape wins:** a pipeline that calls `ctx.use_skill(...)` gets policy,
sandbox, validation, secret-proxy, and reliability tracking *automatically* —
because those already live in the USA. A pipeline that calls `ctx.think(...)`
gets budgeting + escalation logging for free. A pipeline that calls `ctx.emit`/
`ctx.checkpoint` becomes recoverable. **Doing the safe, recordable thing is the
easy thing.** The runtime never has to trust the pipeline's *internals* — only
that it goes through this door.

---

## Three tiers of pluggability (increasing power, increasing containment)

Not every "custom pipeline" is equally trusted, so offer a ladder:

### Tier 1 — Configuration & composition (no code)
Recompose the *existing* stages declaratively: reorder, skip, or repeat phases;
swap the planner for the router; add a second critic; change retry counts. A YAML
pipeline spec assembled from built-in stages. **Covers 60% of "I want it to work
differently" with zero new code and zero new trust.**

```yaml
pipeline: my-fast-coder
stages: [hydrate, plan, policy, execute]     # skip critique for speed
execute: { retries: 1, critic: none }
```

### Tier 2 — In-process registered pipeline (trusted code)
A Python class implementing `Pipeline`, registered like a native skill. For code
*you* wrote or vetted. Full speed, full access to `RuntimeContext`, but it runs
in-process so it's as trusted as the rest of your install. This is how the
built-in loops become just two entries in the registry.

### Tier 3 — Installed / marketplace pipeline (untrusted code)
A pipeline that arrives as a signed `.paapkg` from someone else. Runs behind the
**five-gate installer** (signature → hash → AST scan → permissions → smoke test),
and its `RuntimeContext` is a **restricted facade** honoring the active
permission mode. An untrusted pipeline literally cannot call `ctx.use_skill` for
a skill needing `NET_EGRESS` under LOCKDOWN, because the facade refuses. For the
paranoid case, the pipeline body itself runs in a sandbox and talks to the
`RuntimeContext` over an RPC bridge (same pattern as the MCP client), so even a
malicious loop can only *ask* for capabilities, never seize them.

---

## The pieces to build

| # | Piece | What it is | Reuses |
|---|---|---|---|
| 1 | **`Pipeline` + `RuntimeContext` protocols** | The two interfaces above, in `paa/pipelines/base.py` | agent base, budgets |
| 2 | **`RuntimeContext` implementation** | The recording+gating facade over the real runtime | ledger, USA, model router, memory, delegation guard |
| 3 | **Refactor: orchestrator becomes a *host*** | Extract today's `run()` into `BuiltinPlanExecutePipeline`; `ChiefOrchestrator` selects and hosts a pipeline instead of *being* one | existing `run()` logic verbatim |
| 4 | **`PipelineRegistry`** | Install / list / get / activate / version pipelines | skill registry pattern, a new DB table |
| 5 | **Marketplace package kind `pipeline`** | Ship + sign + install a pipeline as a `.paapkg` | 5-gate installer, signing |
| 6 | **Selection policy** | How a task picks a pipeline: explicit → session default → `can_handle` auto-score → global default | prototypical classifier (task→pipeline) |
| 7 | **Conformance harness** | A test suite that certifies a pipeline emits events, respects budgets, is recoverable, and passes gates — run before install | integration test patterns |
| 8 | **Versioned SDK contract** | Semver on `RuntimeContext`; a pipeline declares the range it needs; installer refuses incompatible ones | (new) |
| 9 | **Docs + a reference pipeline** | A worked ReAct pipeline as the canonical example + authoring guide | coder `AgentLoop` |

### The load-bearing refactor (piece 3), in detail

This is the crux and must be done without breaking the 1138 tests. Move the body
of `ChiefOrchestrator.run()` into a class:

```python
class BuiltinPlanExecutePipeline(Pipeline):
    name = "builtin.plan-execute-critique"
    async def run(self, task, ctx):
        packet = await ctx.call_agent(CONTEXT_BUILDER_PLANNER, ...)
        plan   = await ctx.call_agent(STRATEGIC_PLANNER, ...)
        if not await ctx.call_agent(POLICY_RISK, ...): return blocked
        for step in plan.steps:
            out = await ctx.call_agent(WORKER, step)
            if not await ctx.call_agent(CRITIC, out): retry...
        await ctx.emit(MUTATION_COMMITTED, ...)
```

`ChiefOrchestrator.run(cid)` then becomes: *pick a pipeline, build a
`RuntimeContext`, call `pipeline.run(task, ctx)`.* Because the built-in pipeline
uses the same `ctx.call_agent`/`ctx.emit` calls the orchestrator makes today, the
existing behavior — and every existing test — is preserved. The orchestrator
keeps owning submission, backpressure, recovery, and human gates; it just
delegates the *strategy*.

**Migration test:** the current end-to-end integration suite must pass unchanged
with the built-in pipeline selected. That's the proof the refactor is behavior-
preserving.

---

## A worked example: installing a Tree-of-Thought loop

1. **Author** writes `tot_pipeline.py` implementing `Pipeline`, using
   `ctx.think(...)` to expand branches and `ctx.sub_context()` (budget halves per
   depth, delegation-guarded) for parallel exploration. Declares
   `requires_runtime = ">=1.0,<2.0"` and `required_permissions`.
2. **Package**: `paa pipeline pack ./tot` → a signed `tot-0.1.0.paapkg`.
3. **Install**: `paa pipeline install tot-0.1.0.paapkg` → five gates run; the
   conformance harness runs it against a canned task and checks it emitted the
   lifecycle events, stayed in budget, and recovered from an injected crash.
   Refused if any gate or conformance check fails — zero trace left (already how
   the installer works).
4. **Use**: `paa submit "..." --pipeline tot`, or set it as the session/global
   default, or let auto-selection pick it when `can_handle` scores highest.
5. **It just works** because every capability it used went through
   `RuntimeContext`, so it's recorded, gated, budgeted, and recoverable — the
   author never had to think about any of that.

---

## Selection: how the system chooses a pipeline

Precedence, most specific wins:
1. **Explicit** — `--pipeline X` on the task.
2. **Session default** — set for a working session.
3. **Auto-score** — each registered pipeline's `can_handle(task)`; highest wins
   above a confidence floor. Feed this with the **prototypical classifier**
   already built: learn task→pipeline from what worked (a coding task → the coder
   loop, a research task → a ReAct loop).
4. **Global default** — the built-in plan-execute-critique.

Keep it optional and overridable, exactly like the router (ADR-0011): auto-
selection is a convenience, never a cage.

---

## How the invariants survive an untrusted pipeline

| Threat | Defense (all already exist, reused via the facade) |
|---|---|
| Loop does work without recording it | Capabilities only exist on `RuntimeContext`, which records; a loop that bypasses it can't touch the ledger, memory, or skills at all |
| Loop runs a dangerous command | `ctx.use_skill` routes through policy + sandbox + validation |
| Loop ignores budgets / recurses forever | `ctx.think`/`ctx.sub_context` enforce token + recursion budgets; the host caps wall-clock |
| Loop escalates to a frontier model under LOCKDOWN | the facade's `think` obeys the escalation-forbidden modes |
| Malicious installed loop | five-gate installer + conformance harness before install; Tier-3 sandboxed RPC bridge at runtime |
| Loop crashes mid-task | it emitted checkpoints via `ctx`, so recovery replays and re-queues like any built-in task |
| Installed loop targets an incompatible runtime | semver contract check at install time |

The elegant part: **you don't have to trust the pipeline's code to trust the
system's guarantees**, because the guarantees live on the runtime side of the
facade, not the pipeline side.

---

## Phased delivery

- **P1 — Extract the abstraction (no new features).** Define `Pipeline` +
  `RuntimeContext`, implement the facade, refactor today's lifecycle into
  `BuiltinPlanExecutePipeline`, make the orchestrator a host. **Exit: all 1138
  tests pass unchanged.** This is pure enabling work and unlocks everything else.
- **P2 — Registry + Tier-2 pipelines.** `PipelineRegistry`, `--pipeline` flag,
  session/global defaults. Register the coder `AgentLoop` as a second pipeline to
  prove two dissimilar loops coexist.
- **P3 — Tier-1 declarative pipelines.** YAML stage composition over built-in
  stages; the 60% case with zero code.
- **P4 — Marketplace kind + conformance harness.** `pipeline` package kind,
  packing/signing, the certify-before-install harness. Tier-3 install path.
- **P5 — Auto-selection + Tier-3 sandbox bridge.** `can_handle` scoring wired to
  the prototypical classifier; the sandboxed-RPC `RuntimeContext` for fully
  untrusted loops.
- **P6 — Reference pipeline + authoring kit.** A ToT/ReAct reference, `paa
  pipeline new`, docs, and a compatibility test matrix.

## Risks & how the design handles them

- **Refactor regressions** → the migration test (existing E2E suite unchanged) is
  the gate; do P1 behind the existing green suite.
- **SDK churn breaking installed loops** → semver the `RuntimeContext`; additive-
  only within a major; installer enforces the declared range.
- **A "better" loop that wants to bypass safety for speed** → it can skip the
  *critic* (that's a strategy choice) but not policy/sandbox/validation on
  mutations (those are on the capability, not the stage). Speed is allowed;
  unsafety is not.
- **Two loops, divergent event vocabularies** → the lifecycle `EventType` set is
  fixed and owned by the runtime; a pipeline maps its internal steps onto
  `ctx.emit`, so recovery and the dashboard stay uniform across any loop.

## The payoff

After P1–P2 you can already `--pipeline coder` vs the default and see two
completely different strategies run the same task, both recoverable and audited.
After P4–P6, someone can publish a better loop, you install it behind five gates,
and the system uses it as a first-class citizen — **the execution strategy
becomes a market, not a fixed asset.** That is arguably the deepest form of
"self-improving, modular architecture" the project set out to be: not just
swappable skills and models, but a swappable *mind*.
