# ADR-0051: The pipeline facade is typed calls over minted capabilities — no `emit`, no `checkpoint`

## Status

Accepted

*Recorded 2026-11-21 for plan item P0.2 of
[docs/plans/2026-11-21-pipeline-platform-spec.md](../plans/2026-11-21-pipeline-platform-spec.md).
This ADR fixes the facade surface (the "narrow waist" every pipeline speaks),
the capability-token grant rule, and the two deliberate departures from the
older facade design in [EXTENSIBILITY_PIPELINES.md](../EXTENSIBILITY_PIPELINES.md).
Like ADR-0050 it decides semantics before producers: P1.1 lands the protocols
in `src/paa/pipelines/base.py`, P1.2 runs the built-in on them, and the token
refusal is live from the first in-process facade — but the *security* boundary
(tokens as unforgeable proofs) is only real out-of-process (P3), and this ADR
says so plainly.*

## Context

The older design gave a pipeline a `RuntimeContext` with `emit(event, payload)`
and `checkpoint(label)` — the pipeline could write lifecycle events into the
ledger and take its own checkpoints. Both fail a threat model this runtime
already takes seriously:

- `ctx.emit` lets a pipeline append **any** event it can name. A pipeline that
  emits `POLICY_CLEARED` has just approved its own plan; one that emits
  `MUTATION_COMMITTED` has committed its own work. The ledger's value is that
  lifecycle events are *facts the host vouches for*; a forgeable source is a
  lie with a hash chain on it. The permission machinery
  (`core/types.py:290`'s `Permission` grants, `coder/permissions.py:127`'s
  mutating-tool globs) exists precisely because "the caller says it is fine"
  is not an authorisation.
- `checkpoint` was undefined for an opaque coroutine: a checkpoint of *what*,
  resumed *how*, by a process that no longer exists? The coder journal
  already answered this at the right granularity — every call, journalled by
  the host, `PROPOSED` before effect (ADR-0046) — and ADR-0050 froze that
  answer for the platform. A second, pipeline-owned checkpoint mechanism
  would be a competing source of truth for the same question.

The second constraint is the vision's: "no silent capability without a gate"
and extension rule 6, authority travels with the work. A pipeline's reach must
be the intersection of what it *asks for* (manifest), what the task's
authority record *allows* (mandate scope, `ledger/mandate.py:145`), and what
policy *permits* (the active permission mode's grants) — nothing else
narrows it, and nothing widens it.

## Decision

### The facade: typed operations, host-validated and host-emitted

A pipeline may **only** request typed operations. The host validates each one,
emits the resulting lifecycle events itself, and returns the outcome. The
surface (spec §3.2, the P1.1 freeze in `src/paa/pipelines/base.py`):

| Capability | Purpose | Journalled as | Notes |
|---|---|---|---|
| `ctx.think` | Model call with role, schema, budget | `PIPELINE_CALL_*` | Honours escalation and LOCKDOWN |
| `ctx.call_agent` | Reuse a built-in agent role | `PIPELINE_CALL_*` | Delegation graph and recursion guard apply |
| `ctx.use_skill` | Skill or tool (coder tools, MCP tools) | existing `TOOL_CALL_*` | Policy, sandbox, validation, secret proxy; reuses the coder journal (ADR-0046) |
| `ctx.recall` / `ctx.remember` | Bounded memory read and write | `PIPELINE_CALL_*` | Write goes through the memory creator |
| `ctx.gate` | Ask a human | existing gate events | Parks the run; resume after clearance |
| `ctx.plan.submit` | Offer a plan | host emits `PLAN_COMPILED` | Host validates schema and size |
| `ctx.policy.check` | Request clearance for a plan | host emits `POLICY_CLEARED` / `POLICY_BLOCKED` | Runs the policy agent |
| `ctx.commit` | Commit validated changes | host emits `MUTATION_COMMITTED` | Requires a valid clearance and a passing validation |
| `ctx.sub_run` | Run another pipeline | `PIPELINE_RUN_*` | Child budget is halved; recursion guarded |
| `ctx.now` / `ctx.random` | Recorded time and seeded randomness | journalled | Makes ADR-0050's determinism contract satisfiable |
| `ctx.progress` | Heartbeat and status text | not journalled | Feeds the watchdog and attention ledger |

**Naming.** This table's dotted names (`ctx.plan.submit`, `ctx.policy.check`) are the
wire vocabulary; the frozen protocol spells them `submit_plan` / `check_policy`
(and the capability enum `PLAN_SUBMIT` / `POLICY_CHECK`), matching the
underscored `ctx.use_skill` convention the protocol uses throughout. The two
spellings name one thing; a third is not to be invented.

**`ctx.use_skill` reuses `TOOL_CALL_*`, not a parallel vocabulary.** The coder
journal's rows, protocol, and recovery classification (ADR-0046) already carry
persisted intent, redaction, and isolation attestation for exactly these
calls; a `PIPELINE_CALL_*` duplicate of that would double the rows and split
the sweep's view. The open decision (spec §12.4) is closed here: reuse, and
the `PIPELINE_CALL_*` vocabulary covers everything else.

**Two departures from the older design, both permanent:**

1. **No `ctx.emit`.** Only the host emits lifecycle events, and only as the
   result of a typed call it validated. The built-in orchestrator pipeline is
   first-party trusted code and P1.2 keeps its stage-transition events
   (`CONTEXT_HYDRATED`, `EXECUTION_STARTED`, …) host-emitted for exactly this
   reason: the event set a pipeline can cause is the event set the host
   chose to cause for it.
2. **No `ctx.checkpoint`.** The call journal is the checkpoint (ADR-0050).
   There is no third answer to "where was it" and no second one worth
   building.

### Capability tokens: the only door

At run start the host computes
`granted = manifest.capabilities ∩ mandate.scope ∩ policy(mode)` and mints one
opaque, run-scoped token per granted capability. A call without a valid token
for that capability is refused — in-process that refusal is a
`CapabilityNotGrantedError` (a `PaaError`, so the existing failure path turns
it into a recorded `EXECUTION_FAILED`); over the wire (P3) it is
`-32006 CapabilityNotGranted`. Tokens expire at run end, are never written to
the ledger or transcript, and are delivered out of band (an environment
variable on the child process) so they never appear in argv, logs, or
journalled arguments.

**Honesty about in-process tokens.** In-process, a token is *correlation, not
containment*: same-address-space code can reach anything the process can
reach, and no token changes that. That is why in-process pipelines are
permitted **only** for built-ins and code the operator wrote
(spec §2.1, option A) — the trust is the install's own. The unforgeability
claim starts being true when the pipeline runs out-of-process behind the RPC
transport (P3) or in a container launcher, which is the same line the
[security model](../security/security-model-and-trust-boundaries.md) already
draws for skills and MCP servers. The facade *shape* is identical on both
sides of that line, which is what makes P1's in-process extraction a real
rehearsal of P3 rather than a throwaway.

### `commit` is clearance-gated

`ctx.commit` additionally requires the `clearance_id` of a `POLICY_CLEARED`
event covering the plan hash. **A pipeline cannot skip policy, because the
mutating capability does not exist without the clearance.** In P1 the
built-in's commit stays the orchestrator's own unconditional emission — it is
first-party code whose policy stage already ran; the clearance check lands
with the journal and the RPC transport (P2/P3) where it is enforceable against
code the host did not write.

### The five gates a facade call passes

Every capability call, regardless of transport, passes in order: capability
token check → budget/mode check → journal `PROPOSED` (ADR-0050) → execution →
outcome row. The coder loop's ordering
(`coder/loop.py:218`: PRE hook → `PROPOSED` → permission → prompt → execute →
outcome) is the `use_skill` instance of this ladder; the MCP client's
framing (`skills/adapters/mcp.py:336`'s read loop) and request/response
correlation (`skills/adapters/mcp.py:449`) is the transport P3
reuses for every other capability over stdio.

## Consequences

**What it costs.** A pipeline cannot record a lifecycle fact the host did not
validate, which is the point; the cost is that anything genuinely new a
pipeline wants to report must become a typed operation first — the surface
grows by decision, not by escalation. The facade is a frozen contract
(`paa-pipeline/1` stays draft until three pipelines run on it), so early
mistakes in it are expensive; that is why P1 freezes only the subset the
built-in, the coder loop, and `observe` actually exercise.

**What it does not change.** The agents' own seams: the orchestrator still
never touches a model, the policy agent still runs model-free, and the
facade's `ctx.think` routes through the same escalation router the reasoning
agents use — the facade is a door, not a bypass. The coder journal's frozen
protocol and rows.

**What remains open, owned elsewhere.** `ctx.sub_run`'s budget-halving and
recursion guard land with P2/P3's `PIPELINE_RUN_*` events; hooks (tighten-only,
layer 3 of the customisation model, ADR-0052) fire inside the facade's ladder
and never widen a grant.

**What would change our mind.** A pipeline that needs to report a class of
fact the typed set cannot express, three times from three authors — at which
point the answer is a new typed operation with its own event and its own
validation, still not `emit`.

**Alternatives rejected.** `emit` with an event allowlist per manifest, because
an allowlist of lifecycle events is still a forgeable authority chain — the
pipeline picks what it asks for at install time and the host cannot tell an
honest request from a self-clearing one. `checkpoint` via pickle/ORM snapshots,
because it re-fights ADR-0050's R4 with a weaker boundary. Making
`use_skill` emit `PIPELINE_CALL_*` rows instead, because it splits the
interrupted-call sweep into two vocabularies for one kind of effect.
