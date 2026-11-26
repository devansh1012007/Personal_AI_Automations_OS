# ADR-0052: Pipeline manifests declare, slots compose, and overlays only ever tighten

## Status

Accepted

*Recorded 2026-11-21 for plan item P0.3 of
[docs/plans/2026-11-21-pipeline-platform-spec.md](../plans/2026-11-21-pipeline-platform-spec.md).
This ADR fixes the manifest's parse contract, the five customisation layers,
and the tighten-only rule for overlays and hooks. The manifest in the
Decision below is the exact one `scripts/pipeline_manifest_check.py` parses
and overlay-checks today; the registry, CLI, and `paa pipeline fork` that make
the manifest operational are P4 and are not decided here beyond their shape.*

## Context

"Add any loop, customise it" has to be answerable without trusting the thing
being customised. The older design
([EXTENSIBILITY_PIPELINES.md](../EXTENSIBILITY_PIPELINES.md)) reached for three
tiers — YAML composition, in-process registration, sandboxed marketplace code
— and left the customisation question mostly to code. Three requirements rule
the design here:

1. **No execution needed to know what a pipeline is.** A manifest must be
   analysable before any code runs — the marketplace's five gates
   (`marketplace/installer.py:54`) already work this way for skills: signature,
   content hash, AST scan, permission check, smoke test, with
   `marketplace/package.py:34`'s `_VALID_KINDS` as the closed kind set a
   `pipeline` kind slots into.
2. **Tighten only.** [VISION.md](../VISION.md) constraint 2: an extension may
   add restrictions to policy; it may never relax one. An overlay or hook that
   could *widen* a grant, budget, or capability is the vision's constraint
   violated by configuration instead of by code — the same violation with a
   friendlier syntax.
3. **Customise the built-in without forking it.** The most common customisation
   is "the built-in loop, but with my critic / my validator / a smaller
   budget". If that requires copying 1,200 lines of orchestrator, every user
   of it drifts from every fix to it; the layers below make it a config edit
   (P1.5 makes the built-in's stages slot-addressable, which is the
   precondition).

## Decision

### The manifest (`pipeline.toml`)

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
resume = "journal"                       # "journal" | "none"; "none" is the honest declaration an external
                                          # harness (spec §6) must make — the host refuses to mark such a
                                          # run recoverable (ADR-0050)

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

Parse contract: `tomllib` (stdlib, 3.11+) reads it; the loader validates the
closed key set, the capability names against the manifest's declarable subset
of ADR-0051's facade vocabulary — the seven keys `[capabilities]` may carry
(`think`, `use_skill`, `recall`, `remember`, `call_agent`, `sub_run`,
`commit`); `gate`, `plan.submit`, `policy.check` and `progress` are host-side
operations of the facade itself and are not manifest-requestable in v1 — the
`requires_runtime` range (parseable, checked against the facade version at
`run` time), the `run.resume` value in its closed set, and that `[slots]`
interface and default shapes resolve. An unparseable or unknown-keyed
manifest fails at load, before any code is launched.
`scripts/pipeline_manifest_check.py` is the throwaway proof of that contract
and of the overlay rule below; it is not the P4 loader.

### The five customisation layers

| Layer | What the user changes | Mechanism | Trust needed |
|---|---|---|---|
| 1. **Parameters** | retries, budgets, model role mapping, tool allowlist | Typed `[params]` schema; `--set key=value`; a mandate scope can only tighten | None |
| 2. **Slot overlays** | Swap a named part: context provider, planner, critic, validator, memory policy, delivery | Pipeline declares slots with typed interfaces and defaults; an overlay fills them with a skill, an agent role, or an out-of-process step | Same as the replaced component |
| 3. **Hooks** | Observe or veto at call boundaries | Out-of-process commands, JSON in and out, **tighten-only** (can block, never unblock) | Low |
| 4. **Composition** | A pipeline calling others | `ctx.sub_run(name, input)`, recursion and budget guards | As the callee |
| 5. **Fork** | Copy a built-in and edit it | `paa pipeline fork builtin.plan-execute-critique my-fast` → a manifest plus an overlay; code only if the layers above are not enough | As your code |

Customising a built-in is a layer-2 overlay, **not** a code change; this is
what P1.5's slot-addressable stages buy.

### Tighten-only, enforced at load

An overlay (`my-overlay.toml`) may set `[params]`, fill `[slots]`, narrow
`[capabilities]`, and tighten `[limits]`. Any attempt to widen — a capability
not in the manifest's ask, a limit above the manifest's, a param outside its
declared range, a slot whose interface does not match — is **rejected at load
time** with a message naming the widened key. The same rule bounds hooks: a
hook may block a call or shrink a budget; there is no syntax for unblocking,
and the facade's grant check (ADR-0051) never consults overlays for
*permission*, only for restriction. Widening requires editing the manifest
itself — which, for an installed package, means re-signing and re-gating it
through the installer's five gates.

### Distribution and the evidence tier

Distribution order: local directory (`./my-pipeline/pipeline.toml`) first,
then a git URL pinned to a commit, then a signed `.paapkg` with a `pipeline`
kind through the existing five gates plus capability review and conformance,
then an MCP/ACP adapter (spec §6). The `[evidence] declares_tier` field is a
**claim the host recomputes** — the host's own observation of isolation
determines the tier that lands in the report; the manifest's claim can only
ever lower what the host would say, never raise it, which is tighten-only
applied to honesty.

## Consequences

**What it costs.** TOML with a closed key set is a schema to maintain, and a
slot's typed interface is a small contract per seam — but both are cheap next
to the alternative (parsing code to learn what it does, or diffing forks to
learn what changed). A pipeline that wants a capability it did not declare
fails at run start, not at first use, which is the whole value of declaring:
the failure is cheap and the audit trail exists before any effect.

**What it does not change.** The built-in's behavior: the built-in gains a
manifest-shaped identity (`builtin.plan-execute-critique`, P1.2) without
becoming configurable-by-overlay until P1.5 lands slot addressing; until
then the honest statement is that its only customisation layers are 4 and 5.

**What remains open, owned elsewhere.** The registry layout, trust levels
(`builtin` / `local` / `installed`), `paa pipeline list|inspect|test|fork` are
P4; signed distribution is P5 and is gated on a first outside author asking;
the hook execution shape is P4's overlay loader. Auto-scoring selection
(`can_handle`) is explicitly **not** built — authority limits pipelines
(mandate scope), and there is no data to score with; the design doc's
auto-score is superseded by mandate-scoped explicit selection.

**What would change our mind.** Three real overlays from three users that
each needed to widen something legitimate would mean the manifests are
declaring too tightly — the fix would be the manifests, not the rule, since
a widenable overlay is a privilege-escalation primitive in a config file.

**Alternatives rejected.** YAML composition as the primary description
(the older Tier 1): it grows into a bad programming language, so it is built
instead *as a pipeline* — an interpreter running on the same facade — with
code as the escape hatch. A visual graph format: another product; the
declarative form can be its storage later. Widening with a warning: a warning
is an audit trail nobody reads; the rejection is the guarantee.
