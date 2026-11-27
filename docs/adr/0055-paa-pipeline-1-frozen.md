# ADR-0055: `paa-pipeline/1` frozen — one wire, three users, additive-only within the major

## Status

Accepted

*Recorded 2026-11-27 by Wave 9 task C9.3 (the pipeline spec's P3, the step
protocol) over the W9 tree. It stands on ADR-0050 (the call journal whose
replay discipline the wire carries), ADR-0051 (the facade whose typed calls
the wire serialises), and ADR-0052 (the declaration contract whose manifest
must name the protocol string this record freezes). It is the freeze those
records deferred: ADR-0051 fixed the semantics, and this record fixes the
string they travel under.*

## Context

The pipeline spec gated its own wire protocol on population.
[docs/plans/2026-11-21-pipeline-platform-spec.md](../plans/2026-11-21-pipeline-platform-spec.md)
titles §5 "`paa-pipeline/1`, draft until three pipelines use it", its P1
exit line says "three pipelines run on one facade. Only now is the interface
worth freezing", and its risk table names the failure mode this record
exists to close: "The protocol freezes too early". The spec's versioning
rule was written but never switched on: "`protocol` is
`paa-pipeline/<major>`. Additive changes only within a major."

The population now exists, and it is not three copies of one transport —
it is three *users* of one vocabulary, which is what the gate was actually
for:

1. **The in-process facade** (`src/paa/pipelines/base.py`) — the typed
   calls (`think`, `recall`, `commit`, …) whose parameter and result shapes
   the wire serialises, load-bearing since P1 in every built-in the
   orchestrator, chat, coder, observe, manager and improve loops drive.
2. **The sandboxed runner** (C8.6, `src/paa/pipelines/sandboxed.py`) — the
   installed-package adapter that speaks the protocol's *result* vocabulary
   (`status`/`summary`/`artifacts`/`metrics`, folded under the same
   bounded rules the RPC host folds a wire result) without the duplex
   conversation: its entrypoint is a one-shot JSON contract, and the
   conformance standard drives it six ways over real staged bytes.
3. **The RPC host** (P3, `src/paa/pipelines/rpc_host.py`) — the wire
   itself: newline-delimited JSON-RPC 2.0 on a sandboxed child's stdio,
   `pipeline.initialize` / `run` / `cancel` / `shutdown`, capability tokens
   delivered out of band in the child's environment, the
   `-32001..-32008` error table, grace-then-tree-kill cancellation — and,
   on the other side of the pipe, the `paa_pipeline` child SDK
   (`src/paa_pipeline/`), which speaks it without importing a line of
   `paa`.

A protocol with three users and no freeze is a protocol any of the three
can break: a rename that is invisible to the in-process facade (the
compiler follows it) breaks the wire child, and the wire child is the one
user the host cannot recompile. That asymmetry — the out-of-process user
cannot be fixed by a same-commit edit — is the reason a freeze decision
exists at all.

## Decision

1. **The protocol is frozen at `paa-pipeline/1`.** The constant is
   `PIPELINE_PROTOCOL` in `src/paa/pipelines/base.py:97`, and the freeze
   comment above it (`src/paa/pipelines/base.py:82`) is this record's
   one-line form. The constant is the single source on the host side: the
   manifest parser refuses any `pipeline.toml` whose `[pipeline].protocol`
   is not this string (`src/paa/pipelines/registry.py:281`), and the RPC
   host sends it in `pipeline.initialize` — a child that answers a
   different protocol refuses the handshake itself, so the two ends cannot
   silently disagree.

2. **The rule within the major is additive-only.** New methods may be
   added, new *optional* params on existing methods may be added, and new
   result fields may be added. Nothing that exists at 1 may be renamed,
   removed, or made mandatory. Concretely: a host at 1.x must serve every
   method the v1 table names and must tolerate a child that ignores fields
   it does not know (both sides of this tree's implementation already do —
   the host journals a call's params as given and the SDK reads only the
   keys it knows); a child at 1.x must tolerate unknown params in
   `pipeline.initialize` and unknown result fields on its calls. The three
   facade operations the RPC host cannot yet serve (`ctx.call_agent`,
   `ctx.gate`, `ctx.policy.check`, `src/paa/pipelines/rpc_host.py:284`)
   are absent, not removed — making one servable is an addition and is
   permitted by this freeze.

3. **A v2 needs its own ADR and a real negotiation.** The spec's "the host
   refuses a pipeline whose declared range excludes the facade version"
   covers the facade axis; the protocol axis has no negotiation today —
   the host states `paa-pipeline/1`, and a child built for another major
   refuses the handshake. A v2 therefore requires: an ADR that says what
   changed and why the addition could not be additive; and a negotiation
   at `pipeline.initialize` where the two ends agree a major both speak —
   a v2 host that cannot serve 1, or a v2 child that cannot speak 1, is a
   v2 that has not earned its number. Until that ADR exists, the string
   does not change.

4. **The child SDK pins the same string independently.**
   `src/paa_pipeline/client.py:34` re-states `"paa-pipeline/1"` because
   importing `paa` is the one thing a child SDK must not do — the coupling
   the wire exists to remove would come back as an import. The two copies
   are held equal by test
   (`tests/pipelines/test_rpc_host.py:275`), which is the strongest
   mechanism available offline. The freeze makes that pin cheap: the
   string cannot drift within the major, so the test is a tripwire against
   accident, not a synchronisation burden. A third implementation (a
   TypeScript SDK, the shell proof in `examples/shell-pipeline/`) re-states
   the same string for the same reason.

5. **What the freeze covers, stated narrowly.** The frozen surface is: the
   method set of both directions, the param and result shapes the spec's
   §5 tables name, the final-result vocabulary
   (`completed`/`failed`/`needs_human`, the bounded summary, the
   `[{kind, sha256}]` artifact shape), and the meaning of the
   `-32001..-32008` error run. The platform codes past that run
   (`-32010` the replayed failure stands, `-32015` the RPC transport's own
   fault) follow the in-tree convention of extending past the spec table
   and are host-side vocabulary: a child need not understand them, only
   fail. `FACADE_RUNTIME_VERSION` (`"4.1"`, checked by every declaration's
   `requires_runtime` range) is a *separate axis* and is not frozen here —
   the facade version and the protocol version move independently, and
   `pipeline.initialize` carries both so they can never be confused.

## Consequences

Costs: a mistake at 1 is a mistake for the life of the major — the
wire-shaped subset's boundaries (the `use_skill` journaling split, the
three not-yet-reshaped operations answering `-32601`) are now contracts,
not choices that can be quietly revised; every new implementation must
copy the protocol string rather than import it, and the pin test is the
only thing watching; and a v2 pays twice — its own ADR, plus the
negotiation machinery at initialize that v1 never needed.

The honesty line, stated rather than implied. The three users are all in
this tree: there is no third-party implementation yet, and the shell proof
— written from the protocol doc alone, no SDK — is still this repository's
own artifact, which is evidence the doc is *complete enough to implement
from*, not evidence that anyone else has. The freeze is therefore exactly
as strong as the three users' tests: the loopback pair (a real SDK child
over real pipes through a real sandbox) pins the wire both directions, and
that remains self-referential evidence, the strongest kind available
offline. And the freeze governs the *wire*, not the facade: in-process
signatures may still be reshaped (base.py's own protocol docstring says
which), provided the wire shapes they serialise to do not move.

To move back: there is no path within the major — renaming or removing
anything at 1 is not available at any price short of a v2, which is the
point of this record. The additive rule is the only door, and it only
opens one way.
