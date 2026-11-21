# SPEC: A universal pipeline platform ("add any loop, customise it")

*Written 2026-11-21 against `main` @ `5e0a0d8` · Supersedes the delivery phases (not the
goals) of [EXTENSIBILITY_PIPELINES.md](../EXTENSIBILITY_PIPELINES.md) · Implements
[VISION.md](../VISION.md) Horizon 5 "pipeline SDK", with the extension rules from the
vision as hard constraints · Sequenced after
[2026-11-12-next-spec-2.md](2026-11-12-next-spec-2.md)*

---

## 0. The answer in one page

**Goal.** Let anyone add a new agentic pipeline (a ReAct loop, tree-of-thought, a TDD
loop, a review-then-merge flow, a wrapper around Claude Code or Codex) and customise
it, **without weakening any PAA guarantee**: ledger-before-effect, crash recovery,
replay, policy and sandbox on every mutation, mandate on every task.

**Best way (recommended).** A pipeline is **a "mind" in a separate process that can only
ask the host for "hands"**:

1. **Out-of-process, language-agnostic.** The pipeline is any program speaking a small
   JSON-RPC protocol over stdio. PAA (the *host*) owns the ledger, policy, sandbox,
   budgets, and mandate.
2. **Resumable by a host-side call journal.** Every capability call gets a sequence
   number and a fingerprint. After a crash the host restarts the pipeline and answers
   already-completed calls **from the journal** without re-executing them. If the
   pipeline's calls diverge from the record, the run stops safely.
3. **Capabilities are the only door.** The host mints unforgeable capability tokens
   from `manifest ∩ mandate ∩ policy`. A pipeline **cannot emit lifecycle events**; it can
   only request typed operations (`plan.submit`, `policy.check`, `commit`) that the host
   validates and records.
4. **Customisation by layers**: parameters, then slot overlays, then hooks (tighten-only),
   then composition (pipeline calling pipeline), then forking.
5. **External harnesses are pipelines too**, with an honest evidence tier
   (`paa observe` = T1, ACP/MCP-mediated = T2, contained = T3).
6. **Extract, don't design.** The abstraction is pulled out of **three real pipelines**
   that already exist or are already specified: the built-in orchestrator, the coder tool
   loop, and the observed external run. The wire protocol is frozen only after all three
   run on it.

**Honest sequencing.** The vision gates this behind the validation trial (Horizon 5). I
recommend: **do the design decisions (ADRs) and the in-process extraction soon**, because
they are cheap and make Spec 2's observer mode a clean third pipeline. **Do not build the
out-of-process protocol, registry, marketplace kind, or SDKs until the trial says anyone
wants them.** Details in [section 9](#9-sequencing-against-the-vision-and-spec-2).

## 1. What exists today (verified on `main`)

| Fact | Where | Why it matters |
|---|---|---|
| The built-in pipeline **is** `ChiefOrchestrator.run()`: `_hydrate → _plan → _check_policy → _execute_steps → _commit`, each stage taking and returning a ledger `TaskProjection` | `agents/orchestrator.py:429` (1,212 lines) | State lives in the ledger, so resume = re-project. This is an explicit state machine, one valid resumption model. Stages appear to consult the projection; verify in P1. |
| A **second, unrelated loop**: the coder tool loop with `ToolCallingModel`/`Tool` protocols, a ledger `ToolJournal`, crash classification, cancellation | `coder/loop.py`, `coder/journal.py`, `ledger/recovery.py` | A working prototype of "journal every call, never auto-repeat unknown outcomes". The two loops share no interface. |
| An **RPC client over stdio** for MCP servers with a read loop and request/response matching | `skills/adapters/mcp.py` (`MCPClient`) | Framing and correlation code to reuse for the pipeline transport. PAA is the *server* side here. |
| Package manifests with `kind: str`, `entrypoints`, `required_permissions`, and a 5-gate installer | `marketplace/package.py`, `installer.py` | A `pipeline` kind slots in; today only the metadata path is built, and executable loading is not. |
| Permission modes and a policy engine | `core/types.py`, `coder/permissions.py`, policy agent | The capability filter reuses these. |
| **Absent:** `Pipeline`, `RuntimeContext`, registry, selection | none in `src/` | The design doc is accurate that this is still design-only. |

The design doc's numbers (1,138 tests) are stale: there are now 4,173.

## 2. The possibility space

Five independent choices. Any pipeline platform is one cell in each row.

### 2.1 Where pipeline code runs

| Option | Containment | Languages | Crash isolation | Cost to build | Verdict |
|---|---|---|---|---|---|
| **A. In-process Python class** | None; same trust as the install | Python | None | Low | Keep **only** for built-ins and code you wrote |
| **B. Subprocess + stdio RPC** | Process-level; can be wrapped in a sandbox backend | Any | Yes | Medium | **Primary** |
| C. Container per pipeline | Strong | Any | Yes | High; needs Docker | Same protocol as B with a stronger launcher; **use it as the launcher for untrusted pipelines** |
| D. WASM component | Strong, deterministic | Rust, Go, JS compilers | Yes | High; immature for async, network, LLM work | Revisit in a year; do not build now |
| E. Remote service (HTTP) | Network boundary | Any | Yes | Medium; adds auth, latency | Possible later transport for the same protocol; not needed locally |

### 2.2 How a pipeline is described

| Option | Strength | Weakness | Verdict |
|---|---|---|---|
| **A. Code** (`async def run`) | Expressive; any control flow | Opaque; must be resumed somehow | Needed: the "any loop" requirement |
| **B. Declarative spec** (TOML/YAML stages) | Analysable, no trust needed, shareable, editable by non-programmers | Grows into a bad programming language ("inner-platform effect") | Build **as a pipeline** (an interpreter), with code as the escape hatch |
| C. Visual graph | Friendly | Another product to build | Out of scope; B can be its storage format later |
| D. Generator/coroutine DSL | Natural for authors, replayable | Python-centric | Offer as **SDK sugar** over the wire protocol, not as the protocol |

### 2.3 How a pipeline resumes after a crash (the decisive choice)

| Strategy | How | Pro | Con | Verdict |
|---|---|---|---|---|
| R1. Restart from scratch plus a recovery note | Re-run; tell the model what was interrupted | Trivial | Repeats work; unsafe for effects | Only for read-only pipelines |
| R2. **Host-owned state machine** ("decider": `decide(state) → action`, host records result, `evolve`) | State is the projection of ledger events | Deterministic, trivially resumable; what the built-in already does | Awkward for authors to write loops as step functions | **Supported**, and the natural form of declarative pipelines |
| R3. **Durable replay with a host-side call journal** | Re-run the pipeline; the host answers completed calls from the record, executes only new ones | Authors write ordinary code; works for any language; matches the vision's "recorded results" rule | Requires determinism outside host calls; needs divergence detection | **Default for code pipelines** |
| R4. Snapshot the process | CRIU/VM snapshot | Exact | Not portable, not Windows | No |

R3 is how durable-execution systems work, and the coder journal is already a
tool-level version of it.

### 2.4 How a pipeline is attached to a task

| Option | Verdict |
|---|---|
| **Explicit** (`--pipeline X`) | Always supported |
| **Policy/mandate-scoped** (the mandate's scope lists allowed pipelines and a default) | **Add**: authority limits which pipelines a requester may invoke |
| Session or global default | Add |
| Auto-score (`can_handle`) | **Defer.** There is no data to score with, and the design doc itself says it must never be a cage |

### 2.5 How a pipeline is distributed

| Option | Verdict |
|---|---|
| **Local directory** (`./my-pipeline/pipeline.toml`) | First, for development |
| **Git URL pinned to a commit** | Second |
| Signed `.paapkg` through the existing installer | Third; needs executable loading, which does not exist yet |
| An **MCP server or ACP agent** adapted as a pipeline | Fourth; see [section 6](#6-external-harnesses-as-pipelines) |
| Hosted marketplace | Parked |

### 2.6 What the choice rules out

Writing `Pipeline` as an in-process Python Protocol and shipping it (the design doc's
Tier 2 as the main path) is the cheapest start and the hardest to undo: it hard-codes
Python, trusted execution, and a facade that third parties then depend on. Doing the
protocol first is the opposite mistake: freezing a wire contract before three
implementations exist. **The recommended path avoids both by extracting in-process, then
exposing the same facade over RPC.**

## 3. Architecture

### 3.1 Mind and hands

```
          ┌────────────────────────── host (PAA) ──────────────────────────┐
          │ mandate · policy · sandbox · budgets · ledger · call journal   │
          │                                                                 │
 task ───►│ PipelineHost ── mints CapabilitySet ──► CapabilityFacade        │
          │      │                                       ▲                  │
          └──────┼───────────────────────────────────────┼──────────────────┘
                 │ launch (sandboxed for untrusted)      │ JSON-RPC over stdio
                 ▼                                       │ (the only door)
          ┌────────────────────────────────────────────────────────────────┐
          │ pipeline process: the "mind"                                    │
          │ any language · no ambient fs/network when sandboxed             │
          └────────────────────────────────────────────────────────────────┘
```

The pipeline decides *what to do next*. Everything that touches the world (models,
tools, memory, agents, commits) is a capability call the host gates, journals, and
executes.

### 3.2 The facade (the narrow waist)

The existing design lists nine methods. The recommended set is smaller and differs in
two important ways.

| Method | Purpose | Journalled as | Notes |
|---|---|---|---|
| `ctx.think` | Model call with role, schema, budget | `PIPELINE_CALL_*` | Honours escalation and LOCKDOWN |
| `ctx.call_agent` | Reuse a built-in agent role | `PIPELINE_CALL_*` | Delegation graph and recursion guard apply |
| `ctx.use_skill` | Skill or tool (the coder tools, MCP tools) | existing `TOOL_CALL_*` | Policy, sandbox, validation, secret proxy; reuses the coder journal |
| `ctx.recall` / `ctx.remember` | Bounded memory read and write | `PIPELINE_CALL_*` | Write goes through the memory creator |
| `ctx.gate` | Ask a human | existing gate events | Parks the run; resume after clearance |
| `ctx.plan.submit` | Offer a plan | host emits `PLAN_COMPILED` | Host validates schema and size |
| `ctx.policy.check` | Request clearance for a plan | host emits `POLICY_CLEARED` / `POLICY_BLOCKED` | Runs the policy agent |
| `ctx.commit` | Commit validated changes | host emits `MUTATION_COMMITTED` | Requires a valid clearance and a passing validation |
| `ctx.sub_run` | Run another pipeline | `PIPELINE_RUN_*` | Child budget is halved; recursion guarded |
| `ctx.now` / `ctx.random` | Recorded time and seeded randomness | journalled | Makes determinism possible |
| `ctx.progress` | Heartbeat and status text | not journalled | Feeds the watchdog and attention ledger |

**Two deliberate departures from the design doc:**

1. **No `ctx.emit(event, payload)`.** Letting a pipeline emit arbitrary ledger events
   lets it forge `POLICY_CLEARED` or `MUTATION_COMMITTED`. Only the host emits lifecycle
   events, and only as the result of a typed call it validated.
2. **No `ctx.checkpoint`.** The call journal is the checkpoint.

**Capability tokens.** At run start the host computes
`granted = manifest.capabilities ∩ mandate.scope ∩ policy(mode)` and mints an opaque,
run-scoped token for each. A call without a valid token for that capability is refused
(`-32006`). `ctx.commit` additionally requires the `clearance_id` of a
`POLICY_CLEARED` event covering the plan hash. **A pipeline cannot skip policy because
the mutating capabilities do not exist without the clearance.**

### 3.3 Durable replay (the call journal)

Every capability call carries `seq` (monotonic within the run) and
`fingerprint = sha256(canonical_json({method, params}))`.

```text
on call(seq, method, params):
    fp = fingerprint(method, params)
    rec = journal.get(run_id, seq)
    if rec exists:
        if rec.fingerprint != fp:                       -> emit PIPELINE_DIVERGED; fail run
        if rec.outcome in (COMPLETED, FAILED):          -> return recorded result (NO effect)
        if rec.outcome == PROPOSED (no outcome):        -> classify (applied / not_applied / unknown)
                                                           unknown + mutating -> return error
                                                           `-32008 OutcomeUnknown` (never repeat)
    else:
        append PROPOSED(seq, fp, redacted args)         # ledger BEFORE effect
        check capability token, budget, mode
        execute
        append COMPLETED or FAILED(seq, result_sha, blob?)
        return result
```

Rules this implies, all testable:

- **Determinism contract.** Outside host calls the pipeline must be deterministic. It may
  not read the clock, randomness, environment, or network except through `ctx.now`,
  `ctx.random`, and capabilities. The conformance harness enforces this by running every
  pipeline twice and diffing the call sequence.
- **Same rule as D-6 in the coder:** a mutating call with an unknown outcome is never
  auto-repeated; the pipeline is told and decides.
- **Cost.** Replay of N completed calls is N cheap RPC round trips, with no model or tool
  re-execution.

### 3.4 Evidence tier per pipeline

The pipeline process itself is not an effect. What it *does through capabilities* is T2
(mediated and journalled). If the pipeline is launched in a sandbox with no network and no
filesystem beyond a scratch directory, any effect it tries outside the RPC fails, so the
run can be labelled T3. A pipeline that runs unsandboxed can still write files or call
the network directly: those effects are invisible to the journal and the report must say
so (this is the same honesty rule as `paa observe`).

## 4. Customisation: five layers

| Layer | What the user changes | Mechanism | Trust needed |
|---|---|---|---|
| 1. **Parameters** | retries, budgets, temperature, model role mapping, tool allowlist | Typed schema in the manifest; `--set key=value`; mandate scope can only tighten | None |
| 2. **Slot overlays** | Swap a named part of a pipeline: context provider, planner, critic, validator, memory policy, delivery | The pipeline declares slots with a typed interface and a default; an overlay file fills them with a skill, an agent role, or an out-of-process step | Same as the replaced component |
| 3. **Hooks** | Observe or veto at call boundaries | Out-of-process commands, JSON in and out, **tighten-only** (can block, never unblock) | Low |
| 4. **Composition** | A pipeline calling others | `ctx.sub_run(name, input)` with recursion and budget guards | As the callee |
| 5. **Fork** | Copy a built-in and edit it | `paa pipeline fork builtin.plan-execute-critique my-fast`, producing a manifest plus an overlay; code only if the layers above are not enough | As your code |

Customising a built-in is therefore a layer-2 overlay, **not a code change**, which
requires the built-in orchestrator stages to become slot-addressable (phase P1).

### 4.1 Manifest (`pipeline.toml`)

```toml
[pipeline]
name        = "tdd-loop"
version     = "0.1.0"
description = "Write a failing test, make it pass, refactor."
requires_runtime = ">=4.2,<5"        # semver range of the facade (not the product)
protocol    = "paa-pipeline/1"

[run]
command = ["python", "-m", "tdd_loop"]   # any executable
cwd     = "."
transport = "stdio"
isolation = "sandboxed"                  # "trusted" | "sandboxed"; "trusted" needs a local install

[capabilities]                           # what it asks for; the mandate decides what it gets
think        = { roles = ["planner", "worker"] }
use_skill    = ["read_file", "edit_file", "bash", "git_commit"]
recall       = true
remember     = false
call_agent   = ["critic"]
sub_run      = []
commit       = true

[limits]
max_tokens   = 200000
max_wall_s   = 1800
max_calls    = 400
max_sub_runs = 2

[params]                                 # layer 1
max_cycles   = { type = "int",  default = 5, min = 1, max = 20 }
run_linter   = { type = "bool", default = true }

[slots]                                  # layer 2
critic    = { interface = "critic/1",    default = "agent:critic" }
validator = { interface = "validator/1", default = "builtin:tests" }

[evidence]
declares_tier = "T2"                     # the host recomputes; this is a claim, not a grant
```

An **overlay** (`my-overlay.toml`) may set `[params]`, fill `[slots]`, narrow
`[capabilities]`, and tighten `[limits]`. Any attempt to widen is rejected at load time.

## 5. The wire protocol (`paa-pipeline/1`, draft until three pipelines use it)

**Transport.** Newline-delimited JSON-RPC 2.0 on the child's stdin/stdout, UTF-8, one
message per line. stderr is free-form logging captured to the run transcript. Verify
whether `MCPClient`'s read loop can be reused for the framing.

**Host → pipeline**

| Method | Params | Notes |
|---|---|---|
| `pipeline.initialize` | `protocol`, `runtime_version`, `run_id`, `task`, `params`, `granted` (capability names, not tokens), `limits`, `resume: {recorded_calls: int}` | Pipeline replies `{ok: true, name, version}` |
| `pipeline.run` | none | Pipeline runs and finally returns the result below |
| `pipeline.cancel` | `reason` | Pipeline must stop within a grace period, then it is killed (process tree) |
| `pipeline.shutdown` | none | |

**Pipeline → host.** Each call carries `{seq, token}`; `token` is the capability token
delivered in `initialize` out of band (an environment variable on the child). Method names
are those in section 3.2. Representative shapes:

```json
{"jsonrpc":"2.0","id":7,"method":"ctx.use_skill",
 "params":{"seq":7,"token":"…","skill":"edit_file","args":{"path":"calc.py","old":"…","new":"…"}}}

{"jsonrpc":"2.0","id":7,"result":{"ok":true,"effect":{"kind":"file_edit","patch_sha256":"…"},"output":"…"}}
```

**Final result** of `pipeline.run`:

```json
{"status":"completed|failed|needs_human",
 "summary":"≤500 chars",
 "artifacts":[{"kind":"patch","sha256":"…"}],
 "metrics":{"cycles":3}}
```

**Errors.**

| Code | Name | Meaning |
|---|---|---|
| -32001 | `BudgetExceeded` | Tokens, wall time, calls, or sub-runs |
| -32002 | `PermissionDenied` | Mode or policy refused |
| -32003 | `GateRejected` | A human said no |
| -32004 | `Diverged` | Call sequence differs from the journal; the run stops |
| -32005 | `Cancelled` | |
| -32006 | `CapabilityNotGranted` | No token for that capability |
| -32007 | `InvalidParams` | Schema failure |
| -32008 | `OutcomeUnknown` | Previous attempt's mutating effect cannot be classified; decide |

**Versioning.** `protocol` is `paa-pipeline/<major>`. Additive changes only within a
major. The host refuses a pipeline whose declared range excludes the facade version.

## 6. External harnesses as pipelines

A third-party agent is a pipeline whose "mind" the host does not control. Three adapters,
in order of honesty, each declaring its evidence tier:

| Adapter | How | Tier | Resumability | Spec 2 link |
|---|---|---|---|---|
| **`observe`** | Run any CLI in a workspace; record manifest diff, git changes, transcript | **T1** | None; classify state after a crash | N6.2 |
| **`acp-client`** | PAA is the ACP client; the agent's file and terminal requests and permission prompts pass through PAA | **T2** for mediated actions; anything the agent does natively is unobserved | None | H2.6 measurement |
| **`mcp-host`** | PAA exposes capabilities as an MCP server; the harness calls them | **T2** for those calls only | None | New |

These are first-class pipelines in the registry, so `paa submit --pipeline observe:codex`
looks the same as any other, and the bundle states the tier. **They cannot honour the
replay contract** (PAA does not control the agent's loop) and the manifest must say
`resume = "none"`; the host refuses to mark such a run recoverable.

## 7. Security model

| Threat | Defence |
|---|---|
| Pipeline forges lifecycle events | No `emit`; the host emits from validated typed calls |
| Pipeline commits without clearance | `ctx.commit` requires a clearance id; capability does not exist otherwise |
| Pipeline exfiltrates or damages directly | Sandboxed launcher: no network, scratch-only filesystem; effects only through RPC. On the Windows subprocess backend this is **not enforced**, so the run is labelled T2 at best and the install prompt says so |
| Widening via overlay or hook | Load-time check that every overlay value is ≤ the manifest and the mandate |
| Infinite loop or recursion | `max_calls`, wall-clock cap, `sub_run` depth guard, watchdog on missing `progress` |
| Non-determinism causing replay damage | Conformance double-run; `PIPELINE_DIVERGED` stops the run |
| Capability token theft | Tokens are per-run, per-capability, expire at run end, and are never written to the ledger or transcript |
| Malicious package | Five gates plus **capability review** (the user sees what it asks for) plus conformance; unsigned packages run only with `isolation = "sandboxed"` |
| Pipeline leaks secrets | Existing redaction; secrets reach tools only through the secret proxy, never the pipeline |
| Prompt injection through tool output | Unchanged from the coder: ASK/PLAN/LOCKDOWN gating still applies at the capability |

## 8. Phases

Effort scale as before: **S** hours · **M** 1–2 days · **L** 3–5 days · **XL** 1–2+
weeks. All are solo estimates and ordering aids.

### P0: Decide (3 days, no production code)

| ID | Task | Done when |
|---|---|---|
| P0.1 | ADR-0050 **Pipeline host and call-journal semantics** (R3, divergence, unknown outcomes) | Reviewed; includes the pseudocode in section 3.3 |
| P0.2 | ADR-0051 **Facade and capability tokens** (no `emit`, clearance-gated `commit`) | Lists every capability and its journal event |
| P0.3 | ADR-0052 **Manifest, slots, overlays, tighten-only rule** | The manifest in section 4.1 parses in a throwaway script |
| P0.4 | **Spike:** a 150-line script that spawns a child, speaks NDJSON JSON-RPC, journals three calls to a temp ledger, kills the child, and resumes with replay | Replay returns recorded results with zero re-execution; a deliberately changed call is flagged `Diverged` |
| P0.5 | Update `EXTENSIBILITY_PIPELINES.md`: mark P1–P6 as superseded by this spec and fix the stale numbers | Link test green |

### P1: Extract in-process (about 1 week; behaviour-preserving)

*Rule: no new features. The suite must pass unchanged.*

| ID | Task | Done when |
|---|---|---|
| P1.1 | `paa/pipelines/base.py`: `Pipeline` protocol and `CapabilityFacade` (in-process transport) | Type-checks under `mypy --strict` |
| P1.2 | Move `ChiefOrchestrator.run()` stages into `BuiltinPlanExecuteCritique` using the facade; the orchestrator becomes a *host* that owns submit, backpressure, recovery, gates | **All 4,173 tests pass with zero edits** (migration test) |
| P1.3 | Re-express the coder tool loop as `coder.tool-loop` on the same facade; `use_skill` maps to the existing `ToolJournal` | `tests/coder/` pass unchanged; S1 dry-run still passes |
| P1.4 | After Spec 2's N6.2 ships, express `observe` as a third pipeline | Three pipelines, one interface |
| P1.5 | Make the built-in's stages slot-addressable (critic, validator, context provider) | An overlay swapping the critic changes behaviour in a test |

**Exit:** three pipelines run on one facade. Only now is the interface worth freezing.

### P2: Journal and replay (about 1 week)

| ID | Task | Done when |
|---|---|---|
| P2.1 | Generalise the coder journal into a `CallJournal` (seq, fingerprint, outcome); add `PIPELINE_RUN_STARTED/COMPLETED/FAILED/DIVERGED` and `PIPELINE_CALL_PROPOSED/COMPLETED/FAILED` per ADR-0046 | Replay property test over all orderings including the new types |
| P2.2 | Replay and divergence logic from section 3.3 | `kill -9` at every call boundary of each in-process pipeline resumes with zero duplicate effects |
| P2.3 | Chaos suite: crash between `PROPOSED` and outcome for every capability class | `unknown` is classified and never auto-repeated |

### P3: The step protocol (about 1.5 weeks; start only after the trial)

| ID | Task | Done when |
|---|---|---|
| P3.1 | `paa/pipelines/rpc_host.py`: NDJSON JSON-RPC server, child launch, cancellation with process-tree kill, stderr capture | A scripted fake pipeline completes a run |
| P3.2 | Capability tokens and the grant computation | A call without a token returns `-32006`; widening a grant fails |
| P3.3 | Python SDK `paa_pipeline` (about 200 lines): async client, `ctx.*`, determinism helpers | The three reference pipelines also run **out of process** with identical ledgers |
| P3.4 | **Language proof:** a 100-line TypeScript or shell pipeline with no PAA SDK, only the protocol doc | Passes the conformance suite (skipped where the runtime is absent) |
| P3.5 | Conformance harness `paa pipeline test` | See below |

**Conformance harness checks** (run before install and in CI): handshake and version;
double-run call-sequence equality; kill at random call then resume equals uninterrupted
result; budget exhaustion handled; every ungranted capability refused; commit without
clearance refused; unknown-outcome path; cancel within the grace period; no secrets in the
journal; output size limits.

### P4: Registry, selection, CLI, overlays (about 1 week)

| ID | Task | Done when |
|---|---|---|
| P4.1 | Registry: `PAA_HOME/pipelines/<name>/<version>/`, an index table, trust level (`builtin` / `local` / `installed`) | `paa pipeline list` shows trust and tier |
| P4.2 | CLI: `pipeline new`, `list`, `inspect`, `run`, `test`, `fork`, `diff`, `trace` | Each has a test; `trace` prints the call journal for a run |
| P4.3 | Selection: explicit, then mandate scope, then session default, then config default. **No auto-score** | A mandate that does not list a pipeline refuses it |
| P4.4 | Overlay loader with the tighten-only check | Widening attempts fail with a clear message |
| P4.5 | `paa submit --pipeline` and `paa agent --pipeline` | Same task, two pipelines, two recoverable runs |

### P5: Distribution (about 1 week; needs the first outside pipeline author)

Local directory first, then pinned git URL, then a `pipeline` kind in `.paapkg` through
the existing gates with a **capability review prompt** and the conformance harness. This
phase needs executable package loading, which does not exist. Do not start it without a
real author asking.

### P6: External harness adapters

`observe` (with Spec 2), then `acp-client`, then `mcp-host`, each with `resume = "none"`
and a declared tier. Depends on the H2.6 boundary measurement.

### P7: Authoring kit

`paa pipeline new` scaffolds; a TypeScript SDK; a worked tree-of-thought and a TDD
example; a compatibility matrix across protocol versions.

## 9. Sequencing against the vision and Spec 2

| Order | Work | Why |
|---|---|---|
| 1 | **Spec 2 Wave 4** (git gate, flags, probes, S1 re-run, G1) | The containment gap and the missing real-model proof matter more than any platform |
| 2 | **P0** decisions and spike | Cheap; stops later churn |
| 3 | **Spec 2 N6.1–N6.2** (bundle, observer mode) | Delivers pipeline #3 and the thing the trial needs |
| 4 | **P1 + P2** (in-process extraction, journal) | Behaviour-preserving; makes every later seam cheaper |
| 5 | **Trial (G2)** | Decides whether to continue |
| 6 | **P3–P7** | Only if the trial shows pull |

**Conflict with the vision, stated plainly.** The vision puts the pipeline SDK in
Horizon 5, conditional on Horizon 4, which is conditional on the trial. Building P3–P7
now would repeat the pattern of platform work ahead of demand. P0–P2 are exempt because
they restructure code you already own and the observer mode needs them.

## 10. What changes relative to `EXTENSIBILITY_PIPELINES.md`

| Design doc | This spec | Why |
|---|---|---|
| `ctx.emit(event, payload)` | Removed; typed validated calls only | Prevents forged lifecycle events |
| Recovery by "emitted checkpoints via ctx" | Host-side call journal with divergence detection | The checkpoint claim was undefined for an opaque coroutine |
| Tier 2 in-process as the main path | Out-of-process as the main path; in-process only for built-ins and trusted code | Containment, languages, crash isolation |
| Tier 3 safety = AST scan plus a bridge | Sandboxed launcher plus capability tokens plus conformance | An AST scan is a screen, not a boundary (as the README states) |
| `can_handle` auto-selection | Mandate-scoped explicit selection; auto-score deferred | Authority should limit pipelines; no data to score yet |
| Phases P1–P6 | P0–P7 above, extraction first | Rule of three; extract, do not design |
| No evidence model | Tier declared per adapter and recomputed by the host | The bundle must not overstate |
| 1,138 tests | 4,173 | Stale |

## 11. Risks

| Risk | Mitigation |
|---|---|
| Behaviour-preserving refactor of a 1,212-line orchestrator breaks something | P1.2's gate is the full suite unchanged; do it on a branch; no feature work in the same PR |
| Authors cannot meet the determinism contract | Provide `ctx.now/random`; the double-run harness finds violations early; offer R2 (declarative/step) as the easy mode |
| Replay of long histories is slow | Cache per-run journal in memory; measure before optimising |
| The protocol freezes too early | `paa-pipeline/1` stays "draft" until P1.4 shows three pipelines on it |
| Nobody writes pipelines | That is the trial's answer; P0–P2 still paid for themselves by cleaning the core |
| Windows cannot sandbox pipelines | Label the tier honestly; recommend WSL2 or Docker for untrusted pipelines |
| Scope creep into a marketplace | P5 is gated on a real outside author |

## 12. Open decisions

1. **Accept the sequencing in section 9**, or build earlier and accept the risk.
2. **Resumption default**: R3 (journal replay) for code pipelines, R2 for declarative.
   Confirm.
3. **Framing**: NDJSON (recommended) or LSP-style `Content-Length`.
4. **Whether `ctx.use_skill` reuses `TOOL_CALL_*`** (recommended) or all calls move to
   `PIPELINE_CALL_*` (cleaner vocabulary, a schema bump).
5. **First non-Python language** for P3.4: TypeScript or shell.
6. **Who the first outside pipeline author is.** If none exists, stop after P2.
