# PAA extensibility audit

## 1. Executive verdict

**No: PAA cannot today support an independent developer shipping a completely new agentic loop and harness as an installable, selectable, recoverable first-class runtime component without forking or modifying PAA core.**

PAA has a strong substrate for this future: an append-only ledger, deterministic policy, sandbox/validation, recovery, skills, marketplace metadata, and a separately testable coder loop. But the actual loop boundary and package-to-execution path are missing. The current architecture supports **configuration and extension around the built-in flow**, not replacement of that flow.

The decisive distinction is:

- **Working:** built-in orchestrator lifecycle, deterministic policy, worker validation/sandboxing, ledger append/replay, recovery, skill dispatch, and the isolated coder `AgentLoop`.
- **Partial:** runtime composition and coder-agent composition; both construct reusable pieces but do not expose a stable pipeline host contract.
- **Design-only:** `docs/EXTENSIBILITY_PIPELINES.md` and its proposed `Pipeline`/`RuntimeContext` model.
- **Unsupported:** pipeline selection, executable pipeline registration, package installation/loading, versioned SDK compatibility, and ledger-aware recovery for third-party loops.

No tracked files were modified during this audit.

## 2. Capability matrix

| Capability | Status | Evidence and conclusion |
|---|---|---|
| Built-in task lifecycle | **Working** | `src/paa/agents/orchestrator.py:292-324`, `ChiefOrchestrator.run()` directly owns hydrate → plan → policy → execute/critique/retry → commit. |
| Runtime composition | **Partially supported** | `src/paa/runtime.py:232-345` wires the built-in runtime, but `Runtime.build()` accepts no pipeline registry, pipeline implementation, or arbitrary loop host. |
| Planner/worker/policy/critic chain | **Working** | `src/paa/agents/orchestrator.py:542-811`; tests in `tests/agents/test_orchestrator.py:64-210` cover ordering, policy blocking, retry, gates, and resumed steps. |
| Deterministic permissions and risk checks | **Working** | `src/paa/agents/policy.py:122-183`; policy is model-independent and covers permissions, lockdown/network, irreversible operations, protected paths, operator rules, and anti-goals. |
| Worker sandbox and validation | **Working on the built-in path** | `src/paa/runtime.py:479-524` composes `CriticValidatorAdapter`, `DeterministicValidationEngine`, sandbox, and `WorkerCell`. |
| Append-only ledger | **Working** | `src/paa/ledger/store.py:102-166` performs idempotency, state versioning, hash chaining, event insertion, and head projection atomically. |
| Replay and recovery | **Working for ledger-aware orchestrator tasks** | `src/paa/ledger/recovery.py:226-304` and `:354-414`; `tests/ledger/test_recovery.py:71-431` covers recovery, idempotence, gates, drift rollback, and process death. |
| Delegation/recursion protection | **Working** | `src/paa/agents/base.py` and orchestrator delegation mediation around `src/paa/agents/orchestrator.py:640+`. |
| Independent model/tool loop | **Working, but isolated** | `src/paa/coder/loop.py:116-198`; `_handle_call()` at `:200-220` provides tool gating, hooks, denial, and max-step bounds. `tests/coder/test_loop.py:53-215` exercises these. |
| Coder harness composition | **Partial** | `src/paa/coder/agent.py:105-214` composes a runtime, router-backed model, runtime tools, permissions, hooks, and session, but it is a separate front end rather than an interchangeable runtime pipeline. |
| Coder loop ledger integration | **Unsupported** | `src/paa/coder/loop.py:142-198` records to `Session` (`user`, `assistant`, `tool_call`, `tool_result`), not `LedgerEvent`, task projections, workspace checkpoints, or `RecoveryEngine`. |
| Stable pipeline abstraction | **Design-only** | `docs/EXTENSIBILITY_PIPELINES.md` proposes `Pipeline` and `RuntimeContext`, but there is no `src/paa/pipelines/` implementation, SDK version, context facade, or conformance contract. |
| Pipeline selection | **Unsupported** | `src/paa/runtime.py:209-211`, `Runtime.run()`, unconditionally calls `self.orchestrator.run(correlation_id)`. `TaskRequest` has no pipeline field and there is no CLI/API `--pipeline`, session default, or global default. |
| Pipeline registry | **Unsupported** | No `PipelineRegistry`, activation API, registry table, or registration mechanism exists. `src/paa/skills/registry.py` is skill-specific and cannot select execution strategies. |
| Declarative stage composition | **Design-only** | YAML/stage composition is described in `docs/EXTENSIBILITY_PIPELINES.md`; there is no parser, stage registry, or execution host. |
| Marketplace pipeline package type | **Unsupported** | `src/paa/marketplace/package.py:31-33` allows only `skill`, `agent`, `playbook`, `config`, and `bundle`; `pipeline` is rejected by `__post_init__()` at `:58-61`. |
| Marketplace install gates | **Working for metadata packages** | `src/paa/marketplace/installer.py:90-129` runs signature, hash, AST, permissions, and smoke gates; tests are in `tests/marketplace/test_marketplace.py:115-187`. |
| Executable package loading | **Unsupported** | `PackageInstaller._register()` at `src/paa/marketplace/installer.py:200-220` inserts a marketplace row; it does not unpack/import executable code, resolve an entrypoint, register a pipeline, or connect it to `Runtime`. |
| Marketplace smoke/conformance | **Design-only/incomplete** | `_gate_smoke_test()` at `src/paa/marketplace/installer.py:183-200` checks sandbox health but never executes the package entrypoint. |
| Skill dispatch | **Working** | `src/paa/skills/usa.py:106-234` resolves contracts, validates input, authorizes before invocation, applies bounded secret access, validates output, and updates reliability. |
| Skill marketplace path | **Partial** | Skill contracts and USA exist, but the audit found no complete installed-package-files → live adapter/skill path. |
| Versioned runtime SDK | **Unsupported** | No RuntimeContext SDK version, semver range, compatibility gate, or migration policy exists. |
| Extensibility tests | **Unsupported** | Existing tests prove built-in behavior; none register a second pipeline, select it per task, install executable pipeline code, or recover a crashed external loop. |

### What a developer can do today

A developer can add tools/skills or build a separate application around the coder loop, and can reuse internal runtime primitives if willing to couple directly to them. They cannot produce a package that PAA discovers, validates, activates, selects for a task, and runs through the same ledger/policy/recovery contract as the built-in orchestrator.

## 3. Architectural blockers

### Critical: the orchestrator is the execution strategy, not a host

`ChiefOrchestrator.run()` directly invokes `_hydrate`, `_plan`, `_check_policy`, `_execute_steps`, `_critique`, and `_commit` (`src/paa/agents/orchestrator.py:292-324`). There is no injected loop interface or dispatch boundary. Replacing the strategy requires changing that source or bypassing `Runtime.run()`.

### Critical: the proposed capability facade does not exist

The design document correctly identifies the need for a `RuntimeContext`, but the repository has no supported facade that exposes ledger emission, checkpoints, model calls, skills, memory, human gates, budgets, child contexts, and recovery as controlled capabilities.

`src/paa/agents/base.py`'s `AgentContext` is an invocation envelope with token/child-context concerns; it is not a pipeline SDK. A third-party loop therefore faces two bad choices: receive broad concrete collaborators and bypass policy/ledger conventions, or receive no supported way to perform durable work.

### Critical: no selection path

`TaskRequest`/`Runtime.submit()` carry task data but no pipeline identifier. `Runtime.run()` always dispatches to the built-in orchestrator. There is no explicit selection, session default, global default, `can_handle()` scoring, confidence threshold, activation state, or compatibility check.

### Critical: marketplace is metadata registration, not executable extension loading

`PackageManifest` rejects `pipeline`. Even supported package kinds are recorded as metadata; installation does not create a live import/entrypoint/registry path. Therefore the documented-looking flow `pack → sign → install → select → run` does not exist for loops or harnesses.

### High: the smoke gate does not run the package

`_gate_smoke_test()` health-checks the sandbox, but no package entrypoint is staged and executed. A pipeline can pass installation without its code being imported or checked against a runtime contract.

### High: coder `AgentLoop` is not a drop-in pipeline

Its contract is effectively `run(user_message: str) -> LoopResult`, while a durable runtime pipeline needs something like `run(task: TaskSpec, ctx: RuntimeContext) -> PipelineResult`. The coder loop has its own session transcript, permissions, and max-step policy, separate from task projections, modality budgets, workspace checkpoints, ledger events, and recovery.

### High: recovery depends on events an external loop cannot safely emit

`LedgerStore.append()` and `RecoveryEngine` require a known event vocabulary, idempotency discriminators, versions, projections, checkpoint semantics, and terminal-state rules. A custom loop can call raw internals only by depending on unstable implementation details; there is no safe host API that turns those invariants into mandatory operations.

### High: security is host-side only

The built-in path enforces deterministic policy, sandboxing, validation, and USA skill authorization. An in-process custom Python component is not forced through those paths. AST scanning and package metadata are not runtime containment. Without a capability facade or process boundary, an extension can directly access files, network, secrets, raw storage, or model/provider objects if given them.

### Medium: no isolation between independent harnesses

There is no pipeline identity/namespace in task submission, ledger projections, session defaults, or resource quotas. Two external harnesses would need to coordinate outside PAA and could collide in sessions, workspace state, budgets, or event lineages.

### Medium: no API evolution contract

There is no versioned SDK, capability negotiation, compatibility range, deprecation policy, or upgrade quarantine for external implementations.

## 4. Recommended target architecture

The right boundary is **host-controlled runtime infrastructure + replaceable loop + replaceable harness adapters**, not a general-purpose plugin that receives the whole `Runtime` object.

### Core runtime (must remain under PAA control)

Own:

- ledger append, hash chain, projections, replay, snapshots, and recovery;
- task/session/workspace identity and isolation;
- deterministic policy, permissions, lockdown, validation, and human gates;
- resource budgets, wall-clock/step/recursion ceilings;
- sandbox/process boundaries and secret mediation;
- terminal-state rules and idempotency;
- observability and audit emission.

A loop must never receive raw `LedgerStore`, database connections, secret stores, filesystem-wide capabilities, or unrestricted provider credentials.

### Agentic loop contract

Illustrative Python protocols:

```python
from collections.abc import Awaitable, Mapping, Sequence
from typing import Protocol, runtime_checkable

@runtime_checkable
class AgenticLoop(Protocol):
    api_version: str
    name: str

    async def run(
        self,
        task: "TaskSpec",
        ctx: "RuntimeContext",
    ) -> "LoopResult": ...

    async def cancel(self, ctx: "RuntimeContext") -> None: ...

@dataclass(frozen=True)
class TaskSpec:
    task_id: TaskId
    correlation_id: CorrelationId
    goal: str
    pipeline: str
    workspace: WorkspaceRef
    budget: Budget

@dataclass(frozen=True)
class LoopResult:
    status: Literal["completed", "blocked", "failed", "cancelled"]
    output: Mapping[str, object]
    terminal_reason: str | None
```

The loop controls reasoning, planning, reflection, tool choice, and stop/continue decisions. It does **not** control authorization, ledger writes, checkpoint identity, secret access, or recovery semantics.

### Host-owned `RuntimeContext`

```python
class RuntimeContext(Protocol):
    sdk_version: str
    task: TaskSpec
    signal: Any

    async def emit(self, event: RuntimeEvent) -> EventReceipt: ...
    async def model(self, request: ModelRequest) -> ModelResponse: ...
    async def use_tool(self, name: str, args: Mapping[str, object]) -> ToolResult: ...
    async def use_skill(self, name: str, args: Mapping[str, object]) -> SkillResult: ...
    async def plan_check(self, candidate: Plan) -> PolicyDecision: ...
    async def human_gate(self, request: GateRequest) -> GateResult: ...
    async def checkpoint(self, label: str) -> CheckpointRef: ...
    async def child(self, label: str) -> "RuntimeContext": ...
    async def finish(self, result: LoopResult) -> None: ...
```

`emit()` should accept only typed event commands, not arbitrary event dictionaries. `use_tool()` and `use_skill()` route through existing policy/USA/sandbox/validation. `checkpoint()` is host-owned and ledger-first. `finish()` validates that required lifecycle events, budgets, and terminal status are present before committing.

### Harness contract

A harness owns model/context construction, session UX, resource discovery, and loop instantiation, but receives a capability-scoped host:

```python
class Harness(Protocol):
    api_version: str

    async def build(
        self,
        ctx: RuntimeContext,
        config: Mapping[str, object],
    ) -> AgenticLoop: ...
```

The harness may provide a ReAct loop, plan-and-execute loop, reflection loop, multi-agent coordinator, or coder-style session. It must not replace the host's policy/recovery boundary.

### Extension kinds

Keep distinct registries and contracts for:

- **Tools:** typed input/output, policy annotations, mutation classification, timeout, idempotency.
- **Hooks:** named lifecycle event, ordering/priority, transformation vs notification vs gate semantics, timeout, failure policy.
- **Memory providers:** query/write scopes, retention, consistency, redaction, replay behavior.
- **Planners/routers:** pure or capability-limited proposal interfaces; host policy remains mandatory.
- **Loops/harnesses:** versioned entrypoints with conformance tests and explicit permissions.

Use a small stable core. Do not expose every internal class as an extension point.

### Lifecycle events

At minimum, the host should own typed events such as:

`TASK_ACCEPTED`, `PIPELINE_SELECTED`, `LOOP_STARTED`, `MODEL_REQUESTED`, `MODEL_RESPONDED`, `TOOL_REQUESTED`, `TOOL_AUTHORIZED`, `TOOL_COMPLETED`, `CHECKPOINT_CREATED`, `HUMAN_GATE_REQUESTED`, `HUMAN_GATE_RESOLVED`, `LOOP_SUSPENDED`, `LOOP_RESUMED`, `LOOP_FAILED`, `TASK_COMPLETED`, `TASK_BLOCKED`, `TASK_CANCELLED`.

The loop may request operations; the host emits the authoritative event before the side effect. Recovery must be able to replay any externally visible side effect from these events.

### Selection and dependency injection

Add precedence:

1. explicit task pipeline;
2. session default;
3. `can_handle()` scoring above a confidence floor;
4. global default.

`Runtime.build()` should construct a host registry and register the built-in plan/execute pipeline. Third-party implementations should be discovered through a package entrypoint only after signature/hash/compatibility/conformance checks. Runtime dependencies should be passed as narrow protocols, not concrete classes.

### Package/marketplace model

A package manifest needs:

```toml
kind = "pipeline"
entrypoint = "acme_react:factory"
sdk = ">=1.0,<2.0"
requires = ["...user-approved dependencies..."]
permissions = ["workspace.read", "tool:git", "model.call"]
```

Installation must be transactional: verify source/signature/hash → resolve dependencies in an isolated environment → static checks → execute a real smoke/conformance test in a disposable sandbox → register only after success. Removal must deactivate first, drain active tasks, then remove files/registry rows. Runtime upgrades must refuse incompatible packages rather than import them.

For untrusted third-party loops, prefer an RPC/process boundary with a capability broker. Same-process loading should be an explicit trusted mode, not the default.

## 5. Comparison with Pi's extensibility model

Current upstream Pi is documented as a minimal extensible harness. The repository currently redirects from the historical `badlogic/pi-mono` URL to `earendil-works/pi`; the relevant package is `packages/coding-agent`.

References:

- Repository/package overview: https://github.com/earendil-works/pi/tree/main/packages/coding-agent
- Extension API and lifecycle: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md (raw current content redirects/aliases with the repository move)
- Package distribution: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/packages.md
- Embedding SDK: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/sdk.md
- CLI modes and package commands: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/cli.md
- Exact exported types: `packages/coding-agent/src/core/extensions/types.ts`

What Pi does well and PAA should adopt:

1. **Small explicit API:** extension factories receive `ExtensionAPI`; extensions register tools, commands, flags, providers, MCP servers, and lifecycle handlers rather than reaching into internal objects.
2. **Lifecycle contract:** documented events include input preparation, agent/model turns, provider calls, message lifecycle, tool execution, settlement, session startup/shutdown, reload, and session replacement. The docs explicitly distinguish `agent_end` from final settlement.
3. **Injectable host services:** the SDK lets callers replace `modelRuntime`, `settingsManager`, `sessionManager`, `resourceLoader`, and tools. `createAgentSession()` is a clear composition boundary.
4. **Real package distribution:** npm, git, URL, and local sources; settings-based personal/project scope; version/ref pinning; explicit resource manifests; peer dependencies for host-provided packages; package removal/update commands.
5. **Testable examples and declarations:** SDK examples and exported TypeScript declarations make the extension contract concrete.
6. **Resource scoping and trust:** project packages load only after project trust, and package code is visibly treated as executable code.

What PAA should not copy blindly:

- Pi extensions run in-process with the same OS permissions and can inspect prompts, files, credentials, and session history. Pi's own docs tell users to install only trusted packages. That is acceptable for a user-owned coding harness but insufficient for PAA's ledger/security guarantees.
- Pi's session JSONL is durable transcript state, not an append-only task ledger with deterministic policy and replayable side effects. PAA must retain its stronger host boundary.
- Pi hooks can mutate or gate request/tool flow, but PAA must make authorization and event ordering non-bypassable rather than merely convention-based.
- Pi's extension API is highly capable but does not itself solve multi-tenant resource quotas, capability isolation, or crash recovery of arbitrary side effects.

Pi demonstrates that explicit APIs, package loading, injected resources, lifecycle events, and a real SDK are practical. PAA needs those ideas **plus** a host-controlled capability broker and ledger-aware conformance contract.

## 6. Prioritized file-level roadmap

### P0 — Foundational host boundary (high priority, ~2–4 weeks)

**Files/modules:**

- Add `src/paa/pipelines/base.py`: `AgenticLoop`, `Harness`, `TaskSpec`, `LoopResult`, `RuntimeContext`, SDK/API version types.
- Add `src/paa/pipelines/context.py`: host implementation over ledger, policy, USA, model router, sandbox, workspace checkpoints, budgets, gates, and recovery commands.
- Add `src/paa/pipelines/builtin.py`: extract current orchestrator flow into a built-in `PlanExecutePipeline`.
- Modify `src/paa/agents/orchestrator.py`: retain task lifecycle/projection/recovery ownership and dispatch a selected pipeline instead of embedding the strategy.
- Modify `src/paa/runtime.py`: inject a host/registry and register the built-in pipeline.

**Limitation addressed:** hardcoded `ChiefOrchestrator.run()` and missing capability facade.

**Compatibility:** preserve existing `Runtime.run()` behavior by selecting the built-in pipeline by default; keep compatibility wrappers around current private methods during migration.

**Tests:** existing orchestrator tests must remain green; add event-sequence equivalence tests, context capability tests, denied mutation tests, budget tests, and host-only ledger write tests.

**Acceptance:** the built-in pipeline produces the current event/projection behavior, while a second in-process test pipeline can complete a task only through `RuntimeContext`.

**Dependencies:** none. This is the prerequisite for all later work.

### P1 — Registry, selection, and isolation (~1–2 weeks)

**Files/modules:**

- Add `src/paa/pipelines/registry.py`.
- Add relational schema/migration for pipeline metadata and task/session defaults; preserve SQLite/Postgres parity in `storage/relational/schema_sqlite.sql` and `schema_postgres.sql`.
- Add `pipeline` to `TaskRequest` and task/session metadata.
- Modify `Runtime.submit()`/`Runtime.run()` and API/CLI surfaces.
- Add explicit → session → scored → global-default selection.

**Tests:** explicit selection, inactive/missing selection refusal, default precedence, confidence floor, duplicate name/version rejection, runtime SDK incompatibility.

**Acceptance:** `paa run --pipeline acme.react ...` selects only that registered pipeline and records the identity/version in the ledger.

### P2 — Adapt the existing coder loop (~1–2 weeks)

**Files/modules:**

- Add `src/paa/pipelines/coder.py`.
- Adapt `CoderAgent`/`AgentLoop` to `TaskSpec`/`RuntimeContext` rather than registering `AgentLoop` directly.
- Route tools, permission prompts, checkpoints, and model calls through the host.

**Tests:** coder pipeline event emission, policy denial, checkpointing, max-step/budget enforcement, injected crash and resume, idempotent recovery.

**Acceptance:** a coder-style task can be selected as a pipeline and recovered by `RecoveryEngine` after process death.

### P3 — Executable marketplace packages (~2–4 weeks)

**Files/modules:**

- Modify `src/paa/marketplace/package.py`: add `pipeline`, entrypoint, permissions, dependency, and SDK range fields.
- Modify `src/paa/marketplace/installer.py`: staged loading, dependency resolution, compatibility checks, actual smoke execution, conformance certification, atomic registration/rollback.
- Add `src/paa/pipelines/loader.py`.
- Add package registry/activation/removal operations.

**Compatibility:** existing skill/agent/playbook/config/bundle package behavior remains; pipeline packages use a stricter contract.

**Tests:** valid/invalid entrypoints, signature/hash/AST/permission refusal, real entrypoint execution, incompatible SDK refusal, failed conformance leaves zero registry trace, deactivate-before-remove, dependency isolation.

**Acceptance:** `paa pipeline install ./acme-react`, `paa pipeline list`, `paa pipeline activate acme.react`, and task selection work without editing PAA source.

### P4 — Conformance and containment (~3–6 weeks)

**Files/modules:**

- Add `src/paa/pipelines/conformance.py`.
- Add host wall-clock, step, recursion, output, and child-task ceilings.
- Add sandbox/RPC capability broker for untrusted pipeline bodies.
- Integrate runtime failure/timeout/cancellation event handling with recovery.

**Tests:** bypass attempts, raw-runtime access denial, lockdown network denial, unauthorized commit denial, crash after checkpoint, replay/idempotency, corrupted result handling, concurrent harness isolation.

**Acceptance:** a malicious or buggy pipeline cannot write the ledger directly, access secrets outside granted capabilities, or commit without required events; a crash is requeued/recovered deterministically.

### P5 — SDK/docs/authoring kit (~1–2 weeks)

**Files/modules:**

- Add pipeline authoring docs and reference ReAct implementation.
- Add `paa pipeline new/pack/install/list/activate/remove`.
- Update `README.md`, architecture docs, ADR index, API/CLI docs, and marketplace docs.
- Publish an SDK compatibility matrix and migration policy.

## 7. End-to-end acceptance tests

The minimum contract suite should include:

1. **Independent package:** build a separate `acme-paa-react` package with an entrypoint implementing `AgenticLoop` and a package manifest declaring `sdk >=1,<2`.
2. **Install:** install into a temporary PAA home; verify signature/hash/dependency/compatibility checks and registry activation; verify failed install leaves no active row or files.
3. **Select:** submit a task with `pipeline="acme.react"`; ledger records `PIPELINE_SELECTED` with name/version/digest.
4. **Execute:** the loop performs model → tool → result → stop through `RuntimeContext`; no raw runtime objects are available.
5. **Security:** a tool requiring a protected path or network capability is denied by host policy even when the loop requests it; lockdown remains effective.
6. **Ledger ordering:** inspect events and prove each side effect has its event before execution, with idempotency and hash-chain continuity.
7. **Checkpoint/recovery:** kill the worker after a checkpoint and before a tool side effect; restart and prove recovery requeues/resumes without duplicate mutation.
8. **Failure contract:** return malformed output, exceed step/time budget, and raise an exception; verify typed failure/terminal event and no success-shaped fallback.
9. **Isolation:** run two tasks with different pipeline versions concurrently and prove separate task/session/workspace lineage and quotas.
10. **Upgrade:** install an incompatible SDK range and verify refusal; install a compatible version and verify deterministic activation/update behavior.

### Concrete vertical slice

A developer writes, outside the PAA repository:

```python
class ReactLoop:
    api_version = "1.0"
    name = "acme.react"

    async def run(self, task, ctx):
        await ctx.emit(LoopStarted(task.task_id, self.name))
        response = await ctx.model(ModelRequest(prompt=task.goal))
        if response.tool_call:
            result = await ctx.use_tool(response.tool_call.name, response.tool_call.args)
            await ctx.emit(ToolObserved(result=result))
        await ctx.finish(LoopResult("completed", {"text": response.text}, None))
        return LoopResult("completed", {"text": response.text}, None)

def factory(context: RuntimeContext) -> AgenticLoop:
    return ReactLoop()
```

The package is installed and selected through CLI/configuration. The loop owns the reasoning policy, but the host owns event ordering, authorization, tool execution, checkpointing, terminal state, and recovery. This is the smallest proof that PAA supports an external loop without sacrificing guarantees.

## 8. Smallest viable implementation

Do not begin with a marketplace or a generalized graph of plugins. Prove one vertical slice first:

1. Extract the current orchestrator strategy behind `AgenticLoop` and `RuntimeContext`.
2. Implement a built-in pipeline adapter plus one test-only external loop loaded from a Python entrypoint in a temporary package.
3. Add task-level pipeline selection and host-owned lifecycle events.
4. Require all loop side effects to use the context facade.
5. Add crash/recovery and policy-denial contract tests.
6. Only after that, extend `PackageManifest`/`PackageInstaller` to distribute the same entrypoint.

If this slice cannot preserve the existing event sequence and recovery tests, a larger plugin marketplace will only hide the architectural gap.

## 9. Long-term risks and decisions to defer

- **Privilege escalation:** same-process plugins can bypass every convention. Default to RPC/process isolation for untrusted packages; reserve in-process mode for explicitly trusted packages.
- **Malicious packages:** signatures/authors are not sufficient; use capability declarations, staged execution, dependency isolation, and user-visible permissions. Treat package code as executable code, as Pi's own docs do.
- **Dependency conflicts:** use isolated environments or a stable host SDK; never let packages replace core dependencies in-process.
- **Unbounded execution:** enforce host wall-clock, step, token, recursion, child-task, output-size, and concurrency budgets.
- **Corrupted state:** typed events, atomic append, checkpoints, idempotency, recovery sweeps, and conformance crash tests must remain host responsibilities.
- **API evolution:** version the SDK and capabilities, reject incompatible ranges, and keep migration shims only for a bounded period.
- **Extension assumptions:** make required events/capabilities explicit and validate them at install time; never rely only on abstract classes or documentation.
- **Flexibility versus stability:** planners, routers, memory providers, tools, and loops can be replaceable; ledger storage, authorization, recovery, secret mediation, and terminal-state validation should remain core-controlled. Configure policy thresholds and provider choices; do not make deterministic security itself an arbitrary plugin.
- **Scope discipline:** implement one real ReAct/plan-execute package end to end before adding marketplace search, remote registries, multi-tenant scheduling, or arbitrary graph composition.

**Final test:** An independent developer can build a new loop today only by coupling to internal code or bypassing PAA's durable host guarantees. They cannot package, install, select, and run it as a supported PAA pipeline. The target architecture above would make that possible, but it is not yet implemented.