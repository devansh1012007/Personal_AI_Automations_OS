# ADR-0046: Coder-session events — vocabulary, schema versioning, and the forward-compatibility policy

## Status

Accepted

*Recorded 2026-11-09 by W2-IMPL-01 (wave 2a) for plan item N2.1 of run
20261109-next-spec. This ADR freezes the eight coder-session / mandate event
types, their v1 payload contracts, the `ToolJournal` protocol signature, and
the policy for reading ledger rows written by a newer binary. It deliberately
does **not** implement the journal — that is N2.2's `coder/journal.py` — and
it does not implement the recovery sweep (N2.3) or the mandate machinery
(N2.4). It freezes what those three build on, before any of them writes a
row.*

## Context

The coder loop records only to a JSONL transcript; nothing it does is in the
ledger. `src/paa/coder/loop.py` contains no ledger write, and the `Tool`
protocol there carries no hook for one. The coder composition root
(`src/paa/coder/agent.py`) already reserves a `journal` parameter slot and
fails fast on any non-`None` value, waiting for exactly this freeze — so the
protocol can be landed before the implementation, and the slot cannot be
silently accepted and dropped.

Every ledger event so far is a **task-lineage** event: it was emitted by the
orchestrator or the recovery engine about a correlation that is a task, and
the replay fold carries task state. Coder activity breaks that assumption in
three ways, each of which is a concrete failure mode rather than a style
preference:

1. **Replay must stay total.** The fold records any type it does not know
   into `TaskProjection.unknown_events` instead of raising, but only for
   types the running binary can parse. A new vocabulary lands in an
   installed base of old binaries the moment a journal is wired in, so the
   reader-side behaviour has to be decided *before* the first producer is
   switched on.
2. **Coder lineages must not be requeued as tasks.** The boot sweep reads
   the head projection for non-terminal correlations and resumes them. A
   coder session that ends without any terminal task event would be found
   by that sweep as resumable work — the exact failure the plan names
   planner-B-5. The events therefore must not move any phase and must not
   be terminal.
3. **A crash between PROPOSED and the outcome must be classifiable.**
   Ledger-event-before-side-effect means the intent is on disk before the
   tool runs; without persisted pre/post hashes, the recovery sweep (N2.3)
   cannot tell an applied edit from one that never ran, and a blind retry
   would duplicate a committed effect. The intent therefore travels in the
   PROPOSED payload itself.

## Decision

**Eight new `EventType` members** (`src/paa/core/types.py`), ordered after
the allocator members. None enters `TERMINAL_EVENTS`; all eight live in
`_NO_PHASE_CHANGE` (`src/paa/ledger/replay.py`) and map to no phase, so a
coder event neither advances a projection, closes a lineage, nor counts as
an error. `apply_event` carries one explicit no-op arm for the eight with a
comment stating that contract, following the tier/allocator precedent of
"the record is the payload; the fold carries nothing into `TaskProjection`".

The eight name the whole coder surface and nothing else:

| Event type | Meaning |
|---|---|
| `CODER_SESSION_STARTED` | A coder session began (composition root entry). |
| `TOOL_CALL_PROPOSED` | A tool call was journalled **before** execution, with persisted intent. |
| `TOOL_CALL_DENIED` | A tool call was refused (permission, ASK decline, hook block) and **never executed**. |
| `TOOL_CALL_COMPLETED` | The tool ran and returned. |
| `TOOL_CALL_FAILED` | The tool raised, or recovery marked an interrupted call. |
| `CODER_SESSION_ENDED` | The session's loop finished (every stop reason). |
| `MANDATE_GRANTED` | A mandate was asserted and accepted for the run. |
| `MANDATE_REFUSED` | A mandate request was refused; no work proceeded under it. |

### Payload contracts (v1, pinned by golden tests)

Every payload carries `v` (integer schema version, currently `1`). Readers
that need a field added in a later version **must** branch on
`payload.get("v", 0)` rather than probing for the field: a payload without a
recognised version is not evidence that the field is absent. The field is
named `v` — one short key, so the dispatch is one lookup.

Tool-call payloads share a call identity — `session_id`, `call_id`, `tool` —
so the four outcomes of one call can be assembled without the event
envelope. `isolation_level` in a payload is the containment the execution
**actually** ran under, stored as the member name string (`"NONE"`,
`"PROCESS"`, `"NAMESPACE"`, `"VM"`), matching the shell tool's existing
convention of reporting the sandbox's `isolation_level.name`. It is never
the requested level: an honest `"PROCESS"` on a host that asked for
`"NAMESPACE"` is the whole point of recording it.

- `TOOL_CALL_PROPOSED`: `v`, `session_id`, `call_id`, `tool`,
  `args_sha256`, `args_preview_redacted`, `isolation_level`, and the
  **persisted intent** — `pre_sha256`, `post_sha256`, `patch_sha256`, each
  `null` when the call touches no file. `args_sha256` is the SHA-256 of
  the canonical JSON of the **post-hook** arguments (after the PRE hook has
  had its chance to redact), so the ledger attests the redacted form and
  never the pre-hook original. `args_preview_redacted` is a short,
  human-readable preview with secret-shaped values already masked; it is
  kept short on purpose so the ledger stays readable, and it never
  contains a raw secret (N2.5's canary test owns that).
- `TOOL_CALL_DENIED`: `v`, `session_id`, `call_id`, `tool`, `reason`
  (the frozen denial string, e.g. a protected-path reason or a permission
  reason). The tool did not run: a DENIED row is proof of non-execution.
- `TOOL_CALL_COMPLETED`: `v`, `session_id`, `call_id`, `tool`,
  `result_sha256` (SHA-256 of the tool's result), `args_sha256`,
  `isolation_level`.
- `TOOL_CALL_FAILED`: `v`, `session_id`, `call_id`, `tool`, `reason`,
  `result_sha256` (`null` — a failed call attests no partial result
  hash), `args_sha256`, `isolation_level`. Recovery writes this type with
  `reason` naming the interruption when a call has a PROPOSED row and no
  outcome at next boot (N2.3).
- `CODER_SESSION_STARTED`: `v`, `session_id`, `workspace_path`
  (`null` when the session has no jailed tree), `permission_mode` (the
  coder policy posture name, as configured).
- `CODER_SESSION_ENDED`: `v`, `session_id`, `stopped_reason` (the loop
  result's reason verbatim: model finished, token budget, max steps),
  `steps`, `tool_calls`, `denied_calls`, `tokens`.
- `MANDATE_GRANTED`: `v`, `session_id` (`null` for a task-level mandate),
  `requester_asserted` (`{kind, id}` — *asserted*, never authority; the
  field name is frozen with the `-asserted` suffix so a reader cannot
  quote it as provenance), `identity_assurance` (`"asserted"`),
  `basis` (`explicit` / `inferred` / `implicit`), `policy_version`
  (SHA-256 of the canonical effective policy), `policy_label`, `scope`.
  The mandate machinery itself is N2.4; this payload is the contract it
  writes.
- `MANDATE_REFUSED`: `v`, `session_id` (`null` allowed), `requester_asserted`,
  `reason`, `policy_version`.

### Idempotency

The store's ADR-0008 key is `(correlation_id, event_type, attempt,
discriminator)` and the payload is excluded from it. The journal **must**
construct every tool-call event with `attempt = 0` and

```
discriminator = f"{session_id}:{call_id}:{kind}"
```

where `kind` is one of `PROPOSED`, `DENIED`, `COMPLETED`, `FAILED`. A
redelivered append of the same logical outcome therefore dedupes, while
the PROPOSED and COMPLETED rows of one call — same session, same call,
different `kind` — are two distinct events, as they must be. This mapping
is pinned by a golden test over `compute_idempotency_key`, so N2.2 cannot
change it silently.

### Frozen `ToolJournal` protocol

`src/paa/coder/loop.py` carries the protocol frozen by this ADR — a
`Protocol` of three async methods beside a frozen, slots `ToolCtx`
dataclass, matching the module's existing `Tool`/`ToolCallingModel`
idiom. The methods are async because the journal performs I/O; a sync
signature would push the ledger boundary into threads or an event-loop
bridge inside `_handle_call`. There is no flag to signal "do not execute":
an implementation that must abort raises, because between PROPOSED and
DENIED there is no third state.

```python
@dataclass(frozen=True, slots=True)
class ToolCtx:            # identity + attested args + persisted intent
    session_id: str
    call_id: str
    tool: str
    args_sha256: str
    args_preview_redacted: str
    isolation_level: IsolationLevel   # actual, never requested
    pre_sha256: str | None = None     # persisted intent (N2.3)
    post_sha256: str | None = None    # persisted intent (N2.3)
    patch_sha256: str | None = None   # persisted intent (N2.3)

class ToolJournal(Protocol):          # N2.2 implements, in coder/journal.py
    async def before(
        self, ctx: ToolCtx
    ) -> None: ...                    # -> TOOL_CALL_PROPOSED
    async def denied(
        self, ctx: ToolCtx, reason: str
    ) -> None: ...                    # -> TOOL_CALL_DENIED
    async def after(
        self, ctx: ToolCtx, ok: bool, result_sha: str | None
    ) -> None: ...                    # -> COMPLETED | FAILED
```

`ToolCtx` is frozen because the same value must describe the call at
PROPOSED time and at outcome time; a redaction is a new value, never an
in-place edit after the fact. `IsolationLevel` is imported under
`TYPE_CHECKING` in `loop.py` — the value is bound by the journal module
that constructs it, which is the only reader the annotation needs; a
runtime import would pull the sandbox tree into every importer of the
loop for one annotation. The eight payload fields above are exactly the
persisted-intent fields N2.3 classifies an interrupted call from:
compare the live file against `pre_sha256` / `post_sha256` (applied),
against `pre_sha256` (not applied), and — for shell/git calls, where no
hash of intent exists — report `unknown` and never auto-repeat.

### Forward-compatibility policy (the "v field rule")

1. **Writers** always write the current `v`. Bumping `v` is a change of
   record, reviewed like a schema change: the golden tests fail on the
   old payloads, which is the intended tripwire.
2. **Readers** branch on `v`. A reader that understands `v = 1` and meets
   `v = 2` treats the unknown payload as the documented unknown event —
   recorded in `unknown_events`, never a raise — rather than guessing the
   fields.
3. **`canonical_json` is frozen.** Sorted keys, no whitespace, non-ASCII
   preserved. Any change to serialization invalidates every stored digest
   in existence, because the hash chain commits to the payload bytes. That
   is a data migration, not an edit, and is out of scope for this ADR.
4. **No new fields inside an existing `v`** without bumping it in the same
   commit. The newcomer field's golden row is the evidence.

### The one-way door: `_row_to_event` and mixed-version fleets

`_row_to_event` (`src/paa/ledger/store.py`) constructs `EventType(...)`
from the stored string and raises `ValueError` on a string the running
binary's enum does not name. That behaviour is **deliberate and stays**:
silently mapping an unknown string to a known member would corrupt the
fold it exists to feed. The consequence is the one-way door the plan
names:

- A ledger row carrying any of these eight types is **unreadable by a
  binary that predates N2.1** (a binary at the N1.5 tree or earlier).
  The failure is a raised `ValueError` on first read of that
  lineage, not a silent skip — a stale binary cannot pretend to have
  read a history it cannot name.
- **Mixed-version fleets:** during rollout, do not point an old binary at
  a `PAA_HOME` a new binary has written coder events into. The point of
  no return is the **first N2.2 journal write to a shared home**, not this
  freeze: until N2.2, no producer emits any of the eight types, so a
  ledger written today contains none of them. Rollback of this ADR (and
  of N2.1) before that point is a plain revert; after it, a revert must
  also refuse to open the forward-written home, loudly.
- **New binaries reading old rows** are unaffected: the eight members are
  additive, and replay remains total over any ordering including them (the
  property test extends the existing sampled enum with all eight).

## Consequences

**What it costs.** Eight enum members, eight `_NO_PHASE_CHANGE` entries,
one no-op arm, one protocol and one dataclass, an ADR, and golden tests.
The Windows-cost half of the decision is the `IsolationLevel` import
class in `loop.py`: a `TYPE_CHECKING` block whose classification entry in
the import-conventions lock is a one-line follow-up outside this task's
write-set (filed as a NEED; until it lands, that one conventions test
reads the tree against a stale table).

**What it does not change.** No existing event's payload, digest, or
meaning; `TERMINAL_EVENTS` is untouched (a coder event is never terminal —
even `CODER_SESSION_ENDED` is not, because the session is not the lineage:
an operator may resume the same correlation after it). No column, no
migration: `event_type` is `TEXT` and the mandate travels as payload, so
the schema-parity lock is not engaged. The coder `journal` slot in
`CoderAgent.build` still fails fast — this freeze is what the slot was
named for, but the wire is N2.2.

**What remains open, owned elsewhere.**

- **The sweep exemption is not delivered by this ADR.** `_NO_PHASE_CHANGE`
  makes coder events phase-inert and non-terminal, which is necessary —
  but a coder-only history still projects to the initial phase and its
  head is `is_terminal = 0`, so `boot_sweep` will find that lineage as
  resumable work until N2.3 adds the explicit coder-lineage exemption
  the plan names (coder-prefixed correlation, or a dedicated sweep
  filter). Until then, do not run a boot sweep against a home that
  contains coder lineages. This is an open item with an owner (N2.3), not
  an assumption this ADR makes.
- **The emitter assignment** (composition root vs. journal for
  `CODER_SESSION_STARTED` / `CODER_SESSION_ENDED`) belongs to N2.2: the
  events are vocabulary + payloads here, and the choice is recorded when
  the producer is written.

**What would change our mind.** A third coder-adjacent consumer that
needs a real per-call projection (a live "session board") would move the
eight out of "no-op fold" and into typed projections — with a new ADR,
because `_NO_PHASE_CHANGE` purity is what keeps replay total today. A
second principal (real authentication) would revisit `identity_assurance`
and the `-asserted` naming.

**Alternatives rejected.**

- **Phase-map the session events.** Rejected: a coder session has no task
  phase; mapping `TOOL_CALL_FAILED` to `FAILED` would close the
  projection for a lineage that may be mid-task, and recovery would skip
  the rest of the plan.
- **Prefix exemption in the sweep instead of `_NO_PHASE_CHANGE`.**
  Rejected: it teaches the sweep a second rule for what the phase map
  already expresses, and the sweep would have to learn it before any
  coder row exists. The exemption is still needed (see open item) but as
  a sweep-side filter, not as the primary classification.
- **Strongly-typed pydantic payloads.** Rejected: no other ledger event
  is modelled, and a second validation layer `LedgerEvent` does not apply
  would be structure without a consumer. Golden dicts pin the contract
  with equal force at a fraction of the machinery.
- **A sync journal protocol.** Rejected: the only implementer performs
  ledger I/O; sync would force a thread or loop bridge inside `_handle_call`.
- **A `schema_version` field name instead of `v`.** Rejected: `v` is the
  brief's name and the plan's (`v1` payloads); a longer name buys nothing
  and diverges from the plan's terminology for no benefit.
