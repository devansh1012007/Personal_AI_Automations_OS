# Module: The pipeline platform (`src/paa/pipelines/`)

## Module identity

- **Name:** pipelines — the pipeline platform: declarations, host, journal, registry, conformance, distribution (C8.6) and the hosted economy (C9.1)
- **Path:** `src/paa/pipelines/`
- **Main responsibility:** The vocabulary every structured run speaks (SPEC 2's pipeline platform, `docs/plans/2026-11-21-pipeline-platform-spec.md`): a closed declaration contract (ADR-0052), a capability-granting host and facade, a ledger-backed call journal with replay (ADR-0050), a registry of what exists, a six-check conformance standard, the `pipeline` package kind installed from local dirs, pinned git URLs, signed `.paapkg` files and hub listings through the marketplace's five gates (C8.6) — and, since C9.1 lifted the spec's P5 gate by operator order, the hosted half: a publish flow that builds and signs packages into a hub, and hub-source installs that read them back through the same stages.
- **Architectural layer:** Core / orchestration vocabulary
- **Entrypoints:** `PipelineRegistry.default()` and `select()` (`pipelines/registry.py:427`, `pipelines/registry.py:550`); `declaration_from_manifest()` (`pipelines/registry.py:240`); `SandboxedPipelineRunner` (`pipelines/sandboxed.py:136`); `install_pipeline_package()` (`pipelines/packages.py:1383`); `publish_pipeline_package()` (`pipelines/publish.py:309`); the CLI doors `paa pipeline …` (`cli/pipeline.py:68`), `paa pipeline install` (`cli/main.py:914`) and `paa pipeline publish` (`cli/main.py:1013`)
- **Public interfaces:** `PipelineDeclaration`, `ParamSpec`, `SlotDeclaration`, `declaration_for` (`slots.py`); `Capability`, `CapabilitySet`, `compute_grants`, `Pipeline`, `CapabilityFacade`, `PipelineResult`, `RunLimits`, `PipelineTask` (`base.py`); `BuiltinPipelineFacade` (`builtin.py`); `SubRunComposer`, `ManagerPipeline`, `run_composition` (`compose.py`); `CallJournal`, `fingerprint`, `read_call_journal` (`journal.py`); `ReplayView`, `ReplayAction` (`replay.py`); `Overlay`, `load_overlay`, `tightened_limits` (`overlay.py`); `PipelineRegistry`, `RegisteredPipeline`, `LocalManifestError` (`registry.py`); `ConformanceCheck`, `ConformanceReport`, `CHECK_NAMES`, `installed_conformance`, `every_builtin_conformance` (`conformance.py`); `SandboxedPipelineRunner`, `PIPELINE_ENTRYPOINT`, `ENTRYPOINT_METHOD` (`sandboxed.py`); `PipelinePackageInstaller`, `CapabilityReview`, `ReviewApprover`, `render_review`, `terminal_review_approver`, `refuse_review`, `install_pipeline_package`, `compatibility_refusal`, `registry_for_hub` (`packages.py`); `PublishReport`, `PipelinePublishError`, `load_or_generate_publisher_key`, `publish_pipeline_package` (`publish.py`)
- **Other modules that depend on it:** `agents/orchestrator.py`, `chat/` (loop, service, extensions), `coder/pipeline.py`, `improve/pipeline.py`, `observe/pipeline.py`, `mcp/` (protocol, server, host), `tui/chat/`, and the CLI (`cli/pipeline.py`, `cli/main.py`, `cli/improve.py`, `cli/observe.py`)

## Purpose and responsibilities

A pipeline is a *declared, journalled, replayable* way to structure a run. The
declaration is a closed TOML contract (`pipeline.toml`, ADR-0052): identity,
`run` facts (`resume`, the external-harness shape), an `evidence` tier, a
`capabilities` ask, `limits`, typed `params` and slot wiring. The host is the
capability authority: a pipeline runs only with the grants its mandate
allows (`compute_grants`, `pipelines/base.py:259`), and a foreign ask is
refused by name — never scored, never auto-approved.

Three rules the module holds at all costs:

1. **The journal is the pipeline's memory, and it is ledger-before-effect.**
   Every capability call is PROPOSED before the effect and COMPLETED/FAILED
   after (`CallJournal`, `pipelines/journal.py:201`); a crash between them
   leaves the unknown outcome the D-6 rule refuses to auto-repeat
   (`OutcomeUnknownError`, `pipelines/base.py:197`).
2. **Replay never re-executes what it can serve, and never resyncs what
   diverged** (ADR-0050). `ReplayView` (`pipelines/replay.py:120`) reads the
   record; a changed call halts the run with `PIPELINE_RUN_DIVERGED`, and the
   host never resyncs.
3. **No gate is ever model-decided** (SPEC 2 §9). Selection is explicit and
   mandate-scoped (`pipelines/registry.py:550`); overlays tighten only
   (`pipelines/overlay.py:352`); and the distribution flow's capability
   review is a recorded human decision (`pipelines/packages.py:334`), never a
   model's.

## Internal implementation

### `pipelines/slots.py` — the declaration and the builtins' asks
`PipelineDeclaration` (`pipelines/slots.py:86`) is the closed vocabulary;
the seven in-process builtins declare through it — plan-execute-critique
(`:196`), chat (`:233`), coder (`:279`), observe (`:328`), improve (`:371`,
the C8.1 ask: `commit` and nothing else) and manager (`:415`). The seventh
is P6's last adapter: `builtin.mcp-host` (`:453`, W9-C9.4) — the
externally-driven MCP session, whose declaration states the two facts that
separate every external-harness adapter from the in-process six:
`resume = "none"` (spec §6 — the host refuses to mark such a run
recoverable) and `evidence_tier = "T2"`, the mediated-calls tier, with the
capability ask restating the six tools' own `TOOL_MANIFEST` union (derived
in `mcp/host.py`, so it cannot drift); the session's own shape is
documented in [`interfaces/mcp-server.md`](../interfaces/mcp-server.md).
Slot refs declare the interface they satisfy (`CONTEXT_PROVIDER_INTERFACE`,
`:149`) and a bad ref is refused at load, not at call time.

### `pipelines/base.py` — capabilities, grants, the pipeline and facade protocols
The `Capability` enum (`pipelines/base.py:82`) is the closed ask vocabulary;
`CapabilitySet`/`CapabilityToken` (`:228`, `:214`) are what a facade hands a
pipeline; `Pipeline` (`:370`) and `CapabilityFacade` (`:398`) are the two
sides of every execution. `PipelineStatus` (`:284`) is the closed terminal
vocabulary (`completed`/`failed`/`needs_human`).

### `pipelines/builtin.py` — the in-process host
`BuiltinPipelineFacade` (`pipelines/builtin.py:210`) grants, journals and
gates for the in-process builtins; `BuiltinPlanExecuteCritique` (`:565`) is
the reference pipeline the RFC's core loop runs.

### `pipelines/compose.py` — sub-runs and the manager (C8.2)
`SubRunComposer` (`pipelines/compose.py:314`) drives child runs with halved
budgets (`halve_budget`, `:233`) under a composition host whose grants never
exceed the parent's; `ManagerPipeline` (`:565`) is the delegating built-in.

### `pipelines/journal.py` — the call journal
`CallJournal` (`pipelines/journal.py:201`) writes the `PIPELINE_RUN_*` /
`PIPELINE_CALL_*` rows on one run lineage; `fingerprint` (`:149`) is the
SHA-256 over `{method, params}` that makes a call's identity; the fold
(`fold_call_journal`, `:494`; `read_call_journal`, `:586`) is what recovery
and `paa pipeline trace` read. Params ride the PROPOSED row as fingerprint +
shape + digest — never as raw text.

### `pipelines/replay.py` — the decision table
`ReplayView` (`pipelines/replay.py:120`) answers `serve_or_execute`
(`:208`) per call: SERVE the recorded result, DIVERGE (halt, never resync),
OUTCOME-UNKNOWN (refuse to auto-repeat a mutating call), or FAIL (the
recorded failure stands).

### `pipelines/overlay.py` — tighten-only overlays
An `Overlay` (`pipelines/overlay.py:103`) may narrow params, slots,
capabilities and limits; a widening attempt is refused at load
(`WideningOverlayError`, `:91`).

### `pipelines/registry.py` — what exists, and at what trust
`PipelineRegistry` (`pipelines/registry.py:402`) merges three sources:
builtins (trust `builtin`), the operator's flat `PAA_HOME/pipelines/<name>/`
manifests (trust `local`, listed but not runnable — running an
out-of-process declaration is the RPC transport's, `pipelines/rpc_host.py`), and the installer's
`installed/<name>/<version>/` stagings (trust `installed`, **runnable** —
`_scan_installed`, `pipelines/registry.py:472`). Staging *is* registration.
`declaration_from_manifest` (`:240`) is the one closed parser all three
layouts and the package flow share; off-contract manifests are named in
`local_errors`, never silently dropped.

### `pipelines/conformance.py` — the six-check standard
`CHECK_NAMES` (`pipelines/conformance.py:77`): double-run call-sequence
equality, divergence halt, unknown-outcome never auto-repeats, cancel within
grace, budget exhaustion, no secrets in journal. Each vocabulary instantiates
the checks in its own terms and may mark a check `n/a` with a pointer to
where the property is pinned — a masked green is the thing the standard
exists to stop. `every_builtin_conformance` (`:2557`) is the CI entry.
`installed_conformance` (`:2249`) drives the six checks through the sandboxed
adapter against an installed package's own bytes; its no-secrets row states
the honest split — the goal rides the args file and the PROPOSED fingerprint,
never a host-written payload text, while the child's own answer rides the
recorded result ADR-0050 serves back. The external-harness adapters
instantiate the same six in their own vocabulary: `observe_conformance`
drives the bundle writer, and `mcp_host_conformance` (`:1824`, W9-C9.4)
drives the UNCHANGED MCP server in-process over its own framing — four
checks real (the deterministic `mcp.session` call, the classified transport
death, the between-frames cancel, the no-secrets scan with the server's own
`args_sha256` as the positive control), two honest `n/a` (no replay exists
to diverge — `resume="none"` by declaration — and no budget exists at the
session layer to exhaust; the binding disciplines are the server's per-call
policy gate and caps).

### `pipelines/sandboxed.py` — the loading contract, pinned
**Installed code is never imported into this process.** The gates clear
*bytes*; those bytes execute as a sandboxed subprocess speaking the
`--paa-args` JSON-in / JSON-on-stdout contract (`PIPELINE_ENTRYPOINT`,
`pipelines/sandboxed.py:74`; `ENTRYPOINT_METHOD`, `:80`).
`SandboxedPipelineRunner` (`:136`) drives one run per journal: replay check,
PROPOSED, sandboxed execution (`_execute`, `:255`), classified outcome.
The honest gap — an installed pipeline cannot call `ctx.think` in v1, because a
subprocess holds no facade tokens — is the RPC transport's territory
(`pipelines/rpc_host.py`, which carries tokens out-of-band), exactly
where the spec's P5 placed executable package loading.

### `pipelines/rpc_host.py` — the streaming step protocol (C9.3, P3)
`RpcPipelineHost` (`pipelines/rpc_host.py:394`) is the third transport: a
child process speaking **`paa-pipeline/1`** — NDJSON JSON-RPC 2.0 over the
child's stdin/stdout on the shared framing kit (`paa/mcp/protocol.py`, the
same kit ACP rides), the capability **token** delivered out-of-band via an
environment variable, stderr drained continuously into the run transcript.
The host side owns the rules the protocol demands: the declared
version-range check at `pipeline.initialize` (a range excluding the facade
version is refused, named), budget enforcement (`-32001` — counters move
*after* the token check, so an ungranted call never consumes budget), the
token check (`-32006` — the host never issues tokens for ungranted
capabilities), divergence on resume (`-32004` — a call sequence that stops
matching the journal halts the run, mirroring the in-process facade), and
cancel's grace-then-process-tree-kill. The protocol version itself is
`PIPELINE_PROTOCOL` (`pipelines/base.py:97`), **frozen at `paa-pipeline/1`**
by ADR-0055 under the spec's own three-users rule — the in-process facade,
the C8.6 sandboxed runner, and this RPC host all speak it; additive-only
within the major from here.

The child side ships as a **second package**: `paa_pipeline`
(`src/paa_pipeline/`, ~480 lines) — an async client, a `ctx` object
exposing the facade method names as journaled calls carrying `{seq,
token}`, and determinism helpers. It imports **nothing** from `paa`: the
child side of a wire protocol must stay dependency-light, and the SDK pins
the same `paa-pipeline/1` string independently (held equal by test — it
cannot import the constant, so the test is the weld).

**The language proof (P3.4):** `examples/shell-pipeline/pipeline.sh`
(115 lines of POSIX shell, no SDK) speaks the protocol from the doc alone —
fixed-shape JSON via printf/awk (the header comment states the honest
boundary: the protocol's fixed shapes, not general JSON), initialize →
run → one ctx call → final result. A test drives it through the RPC host
(skip-if-no-bash) — proof the protocol is a protocol, not a Python API.

### `pipelines/conformance.py` — the `paa pipeline test` door
The named-conformance map `NAMED_CONFORMANCE_RUNNERS`
(`pipelines/conformance.py:2284`) is THE contract every caller resolves a
pipeline name through — the Loop Console and the CLI door both; a pipeline
registered without a runner earns `no_runner_report`
(`:2296`) — RED, never missing. `run_named_conformance` (`:2315`) drives
one name on a caller-provided throwaway ledger; `paa pipeline test
<name|all>` renders the standard's own `render_reports` and exits 0/1.
The door drives the in-process runners; the sandboxed and RPC transports
are exercised by their own suites — stated in the help, not implied.

### `pipelines/packages.py` — the install flow (C8.6, the P5-lite)
One install action, one fresh lineage, six stages
(`PipelinePackageInstaller.install`, `pipelines/packages.py:678`):

### `pipelines/packages.py` — the install flow (C8.6's P5-lite, C9.1's hub)
One install action, one fresh lineage, seven stages
(`PipelinePackageInstaller.install`, `pipelines/packages.py:942`):

1. **source** — a local directory, a `git+<scheme>://` URL at a *pinned* ref
   (a commit SHA or a tag; a moving branch is refused after the clone,
   before any byte is read — `_resolve_git`, `:557`), a signed `.paapkg`
   unpacked through the hardened loader, or — since C9.1 — a **hub**
   listing (`hub:<name>[@<version>]` with `--hub <dir-or-url>`;
   `_resolve_hub_source`, `:706`). Host-side git runs with literal
   argv, an allowlisted environment and a wall budget (`_run_git`,
   `:503`). The hub resolves the version FIRST — newest satisfiable,
   the index entry's own `requires_paa` restatement the first filter,
   refusing on ambiguity by naming the candidates — then fetches
   through the registry client (`registry_for_hub`, `:659`: a
   `LocalDirectoryRegistry` directory or an `HttpRegistry` URL, both
   untrusted) and checks the entry's vouch before any gate is asked
   to trust a byte: `verify()` against the entry's `(publisher_key,
   signature)` first, then the manifest's own restatement — a key
   cannot sign the claim about itself, so a package may restate one
   publisher while another key's signature authenticates its bytes,
   and the install refuses rather than pick which to believe. Every
   vouch branch is the operator's `--allow-unverified` to override.
2. **declaration** — `pipeline.toml` parsed by the same closed contract
   (`declaration_from_manifest`), plus the `pipeline.py` entrypoint it pins.
3. **compatibility** — the manifest's `requires_paa` range against the
   RUNNING version (`compatibility_refusal`, `:1474`; one comparison,
   shared with the publish flow): a package that excludes this runtime
   refuses pre-gates, named. The signed manifest is the copy the
   install refuses against — a hub's index may claim anything.
4. **the five gates** — the marketplace installer, unmodified in its order
   (signature, content hash, AST scan, permissions, smoke test). An unsigned
   local/git source installs only on the operator's explicit
   `--allow-unverified` opt-in.
5. **conformance** — the six-check standard against the gated bytes; a RED
   report refuses the install *after* the gates, unregistering their
   marketplace row so the zero-trace rule is the whole flow's.
6. **capability review** — the C8.1 human-gate pattern: a deterministic
   `CapabilityReview` artifact (`pipelines/packages.py:246`) listing the
   declaration's ask, the conformance verdict and the content hash; a human
   approves or refuses through the `ReviewApprover` seam. An unwired seam
   refuses (`refuse_review`, `:284`); the rendered review states the v1
   limit honestly — the ask is recorded, not minted (`render_review`,
   `:300`).
7. **stage** — the gated bytes (and the review artifact,
   `REVIEW_ARTIFACT_NAME`, `:153`) land under
   `PAA_HOME/pipelines/installed/<name>/<version>/` (`_stage`, `:1308`),
   which is what makes the registry row appear.

A refusal at any stage leaves zero trace by construction: no marketplace
row, no staged files, no registry row — only the `PIPELINE_PACKAGE_REFUSED`
event that says it was tried. The two events (`PIPELINE_PACKAGE_INSTALLED` /
`PIPELINE_PACKAGE_REFUSED`, `core/types.py:419`) ride their own fresh
lineage, carry `v: 1` payloads, and ship with goldens and replay/recovery
coverage (ADR-0046; `tests/ledger/test_pipeline_package_events.py`).

## The one-call doors and the CLI

`install_pipeline_package` (`pipelines/packages.py:1383`) is the single
install driver — the CLI's door and the tests' door are the same function,
so both leave identical rows and identical staging. `publish_pipeline_package`
(`pipelines/publish.py:309`) is the single publish driver, and it touches no
ledger and no database at all: publishing is a host-side operator action
(the C7.2 host-git precedent), so a refusal leaves only the report it came
back on.

The terminal surfaces are `paa pipeline install <dir|git-url|.paapkg|hub:name>
[--ref] [--hub <dir-or-url>] [--allow-unverified]` (`cli/main.py:914`) and
`paa pipeline publish <dir-or-installed-name> [--version] [--hub <dir>]
[--key <path>] [--allow-unverified]` (`cli/main.py:1013`). The install door
runs the five gates, the conformance standard and the capability-review
prompt (`terminal_review_approver`, `pipelines/packages.py:334`; a walk-away
is a refusal, not an error). The publish door builds and signs the package,
writes it through `LocalDirectoryRegistry.publish`, surfaces the first-use
publisher-key notice, and names the install command for the next hop. Exit
codes follow the skills door's contract on both verbs: 0 installed/published,
1 refused (the reason named), 2 usage.

## The hosted economy (C9.1): the loop, end to end

The spec parked the hosted marketplace behind "Do not start it without a
real author asking" (`docs/plans/2026-11-21-pipeline-platform-spec.md:430`);
the W9 operator order ("make market place and everything that was
deferred") lifted the gate — the operator is the author asking. What the
order bought:

- **Publish** (`pipelines/publish.py`): a directory or an *installed*
  pipeline's staged bytes (minus the local review artifact) become a
  `pipeline`-kind `.paapkg` — the declaration parsed by the same closed
  contract, the manifest built by the same `_build_manifest`
  (`pipelines/packages.py:1550`), carrying `requires_paa` and `protocol`
  inside `signing_bytes` so the signed manifest is the copy an install
  refuses against. Publisher keys live under `PAA_HOME/keys`
  (`load_or_generate_publisher_key`, `pipelines/publish.py:190`; mode 0600,
  generated on first use, private half never travels). The compatibility
  floor is checked against the running version before anything is written;
  an unsigned publish refuses unless `--allow-unverified` (the install
  escape hatch's flag and meaning, mirrored); a remote hub URL refuses
  (there is no upload protocol — its own row).
- **Hub install**: `hub:<name>[@<version>] --hub <dir-or-url>` rides the
  seven stages above unmodified; the registry client is the only new
  dependency, and it is read as untrusted input (version resolution, the
  vouch check, the fetch, and every refusal are the flow's own words —
  bounded and quoted where the hub's data is named). The flow never
  writes a hub back: only the publish door does, so an install cannot
  turn a read into a redistribution.
- **The loop economy**, pinned end-to-end by test
  (`TestTheLoopEconomy`, `tests/pipelines/test_packages.py`): install
  (local dir) → fork (the C8.5 pattern — staged bytes copied to a personal
  location and changed) → publish the fork signed → install it on a CLEAN
  `PAA_HOME` from that hub with no opt-in (the vouch verifies pre-gates,
  all five gates green, `signature_state` `valid` under the trusted
  publisher key) → run. What comes back answers in the fork's own voice.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `PipelineRegistry.default()` | `PAA_HOME` | registry rows (builtin + local + installed) | none (read at construction) |
| `declaration_from_manifest(path)` | a `pipeline.toml` | `PipelineDeclaration` | none |
| `SandboxedPipelineRunner.run(goal)` | staged dir, declaration, journal, sandbox | `PipelineResult` | `PIPELINE_RUN_*`/`CALL_*` rows; one sandboxed child |
| `install_pipeline_package(source, …)` | local dir / pinned git URL / `.paapkg` / `hub:<name>` + `--hub` | `PipelineInstallReport` | gates' marketplace row (unregistered on later refusal), staged bytes, `PIPELINE_PACKAGE_*` event |
| `publish_pipeline_package(source, hub, …)` | a directory or an installed name | `PublishReport` | hub `.paapkg` + index entry (only when every check passed; no ledger events) |
| `installed_conformance(…)` | staged bytes, declaration, sandbox | `ConformanceReport` | six real runs on their own lineages |

## Failure behavior

- Off-contract declaration (unknown section, undeclarable capability, bad
  `run.resume`) → `LocalManifestError` / a `declaration`-stage refusal,
  before any gate runs.
- A `requires_paa` range that excludes the running version (or does not
  parse) → a `compatibility`-stage refusal pre-gates — on both ends of the
  economy: the publish flow refuses to write the package, the install flow
  refuses to stage it.
- Unpinned or moving git ref, unresolvable ref, non-URL → `source`-stage
  refusal; the clone's words are never logged or raised.
- A hub that cannot be opened or searched, holds no such package, is
  ambiguous about `(name, version)`, serves a package whose signature does
  not verify under the entry's publisher key, or serves bytes whose
  restatement disagrees with the entry → `source`-stage refusal, the
  project's own words, bounded where the hub's data is quoted; zero trace.
- Unsigned source without the opt-in, invalid signature, tampered payload,
  AST-blocked code, ungranted permission, failed smoke → the refusing gate's
  name, zero trace.
- RED conformance report or a refusing human → refusal *after* the gates,
  with the gates' marketplace row unregistered again.
- A publish refusal (unreadable key, insecure fallback without the opt-in,
  off-contract source, unknown installed name, remote hub URL) → the reason
  on the report, the hub left exactly as it was.
- Cancelled run → the call row closes `FAILED('cancelled')` before the
  `CancelledError` propagates; exhausted containment clock → a `FAILED` run
  by rule, never a crash.

## Known gaps

- An installed pipeline cannot call facade capabilities (`ctx.think` et
  al.) through the sandboxed adapter in v1 — it has no token path. The RPC
  host (`pipelines/rpc_host.py`) is the token-carrying transport; wiring
  the installed-package runner onto it is the follow-on, stated in the
  review artifact so no human approves a silent no-op.
- The registry keeps one version per installed name (the last scan wins);
  there is no multi-version pinning surface yet.
- `paa pipeline run` drives the in-process builtins; running an *installed*
  pipeline goes through the sandboxed adapter's callers (the install flow's
  conformance and any host that mints a journal) — a first-class
  `paa pipeline run <installed>` door is P3-shaped work.
- Publishing to a remote (`http(s)://`) hub is refused: `HttpRegistry`
  has a `publish` verb on the wire but no server to talk to, and an upload
  protocol with authentication is its own row. Key rotation (re-keying a
  publisher identity and re-signing a catalogue) is likewise deferred;
  `--key` is the manual seam, not a workflow.

## Related documents

- `docs/adr/0050-pipeline-host-and-call-journal.md` — the host, the journal,
  replay and D-6
- `docs/adr/0052-manifest-slots-overlays-tighten-only.md` — the declaration
  contract this module enforces
- `docs/adr/0046-coder-event-forward-compatibility.md` — the v-golden event
  discipline the package events follow
- `docs/plans/2026-11-21-pipeline-platform-spec.md` — the phased plan (P4
  registry, P5 distribution, the gated hosted economy)
- [`marketplace.md`](marketplace.md) — the five-gate installer and the signed
  package form the flow rides
- [`ledger.md`](ledger.md) — the store under the call journal
- [`sandbox.md`](sandbox.md) — the containment the smoke gate, the runner and
  the conformance standard execute under
- The done-when as tests: `tests/pipelines/test_packages.py` (the flow, all
  refusal paths, the hub sources and the loop economy),
  `tests/pipelines/test_publish.py` (the publish flow),
  `tests/pipelines/test_sandboxed.py`,
  `tests/pipelines/test_registry.py`, `tests/cli/test_pipeline_install_cli.py`
  and `tests/cli/test_pipeline_publish_cli.py` (the doors),
  `tests/ledger/test_pipeline_package_events.py` (the goldens)
