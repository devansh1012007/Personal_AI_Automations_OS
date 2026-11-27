# Module: The pipeline platform (`src/paa/pipelines/`)

## Module identity

- **Name:** pipelines — the pipeline platform: declarations, host, journal, registry, conformance, and (C8.6) distribution
- **Path:** `src/paa/pipelines/`
- **Main responsibility:** The vocabulary every structured run speaks (SPEC 2's pipeline platform, `docs/plans/2026-11-21-pipeline-platform-spec.md`): a closed declaration contract (ADR-0052), a capability-granting host and facade, a ledger-backed call journal with replay (ADR-0050), a registry of what exists, a six-check conformance standard, and — since C8.6, the spec's P5-lite — the `pipeline` package kind installed from local dirs, pinned git URLs and signed `.paapkg` files through the marketplace's five gates.
- **Architectural layer:** Core / orchestration vocabulary
- **Entrypoints:** `PipelineRegistry.default()` and `select()` (`pipelines/registry.py:394`, `pipelines/registry.py:542`); `declaration_from_manifest()` (`pipelines/registry.py:232`); `SandboxedPipelineRunner` (`pipelines/sandboxed.py:136`); `install_pipeline_package()` (`pipelines/packages.py:1120`); the CLI doors `paa pipeline …` (`cli/pipeline.py:68`) and `paa pipeline install` (`cli/main.py:914`)
- **Public interfaces:** `PipelineDeclaration`, `ParamSpec`, `SlotDeclaration`, `declaration_for` (`slots.py`); `Capability`, `CapabilitySet`, `compute_grants`, `Pipeline`, `CapabilityFacade`, `PipelineResult`, `RunLimits`, `PipelineTask` (`base.py`); `BuiltinPipelineFacade` (`builtin.py`); `SubRunComposer`, `ManagerPipeline`, `run_composition` (`compose.py`); `CallJournal`, `fingerprint`, `read_call_journal` (`journal.py`); `ReplayView`, `ReplayAction` (`replay.py`); `Overlay`, `load_overlay`, `tightened_limits` (`overlay.py`); `PipelineRegistry`, `RegisteredPipeline`, `LocalManifestError` (`registry.py`); `ConformanceCheck`, `ConformanceReport`, `CHECK_NAMES`, `installed_conformance`, `every_builtin_conformance` (`conformance.py`); `SandboxedPipelineRunner`, `PIPELINE_ENTRYPOINT`, `ENTRYPOINT_METHOD` (`sandboxed.py`); `PipelinePackageInstaller`, `CapabilityReview`, `ReviewApprover`, `render_review`, `terminal_review_approver`, `refuse_review` (`packages.py`)
- **Other modules that depend on it:** `agents/orchestrator.py`, `chat/` (loop, service, extensions), `coder/pipeline.py`, `improve/pipeline.py`, `observe/pipeline.py`, `mcp/` (protocol, server), `tui/chat/`, and the CLI (`cli/pipeline.py`, `cli/main.py`, `cli/improve.py`, `cli/observe.py`)

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
   after (`CallJournal`, `pipelines/journal.py:187`); a crash between them
   leaves the unknown outcome the D-6 rule refuses to auto-repeat
   (`OutcomeUnknownError`, `pipelines/base.py:197`).
2. **Replay never re-executes what it can serve, and never resyncs what
   diverged** (ADR-0050). `ReplayView` (`pipelines/replay.py:120`) reads the
   record; a changed call halts the run with `PIPELINE_RUN_DIVERGED`, and the
   host never resyncs.
3. **No gate is ever model-decided** (SPEC 2 §9). Selection is explicit and
   mandate-scoped (`pipelines/registry.py:542`); overlays tighten only
   (`pipelines/overlay.py:352`); and the distribution flow's capability
   review is a recorded human decision (`pipelines/packages.py:291`), never a
   model's.

## Internal implementation

### `pipelines/slots.py` — the declaration and the builtins' asks
`PipelineDeclaration` (`pipelines/slots.py:86`) is the closed vocabulary;
the six in-process builtins declare through it — plan-execute-critique
(`:196`), chat (`:233`), coder (`:279`), observe (`:328`), improve (`:371`,
the C8.1 ask: `commit` and nothing else) and manager (`:415`). Slot refs
declare the interface they satisfy (`CONTEXT_PROVIDER_INTERFACE`, `:149`) and
a bad ref is refused at load, not at call time.

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
`CallJournal` (`pipelines/journal.py:187`) writes the `PIPELINE_RUN_*` /
`PIPELINE_CALL_*` rows on one run lineage; `fingerprint` (`:135`) is the
SHA-256 over `{method, params}` that makes a call's identity; the fold
(`fold_call_journal`, `:452`; `read_call_journal`, `:544`) is what recovery
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
`PipelineRegistry` (`pipelines/registry.py:394`) merges three sources:
builtins (trust `builtin`), the operator's flat `PAA_HOME/pipelines/<name>/`
manifests (trust `local`, listed but not runnable — running an
out-of-process declaration is P3's RPC transport), and the installer's
`installed/<name>/<version>/` stagings (trust `installed`, **runnable** —
`_scan_installed`, `pipelines/registry.py:464`). Staging *is* registration.
`declaration_from_manifest` (`:232`) is the one closed parser all three
layouts and the package flow share; off-contract manifests are named in
`local_errors`, never silently dropped.

### `pipelines/conformance.py` — the six-check standard
`CHECK_NAMES` (`pipelines/conformance.py:76`): double-run call-sequence
equality, divergence halt, unknown-outcome never auto-repeats, cancel within
grace, budget exhaustion, no secrets in journal. Each vocabulary instantiates
the checks in its own terms and may mark a check `n/a` with a pointer to
where the property is pinned — a masked green is the thing the standard
exists to stop. `every_builtin_conformance` (`:2205`) is the CI entry.
`installed_conformance` (`:1897`) drives the six checks through the sandboxed
adapter against an installed package's own bytes; its no-secrets row states
the honest split — the goal rides the args file and the PROPOSED fingerprint,
never a host-written payload text, while the child's own answer rides the
recorded result ADR-0050 serves back.

### `pipelines/sandboxed.py` — the loading contract, pinned
**Installed code is never imported into this process.** The gates clear
*bytes*; those bytes execute as a sandboxed subprocess speaking the
`--paa-args` JSON-in / JSON-on-stdout contract (`PIPELINE_ENTRYPOINT`,
`pipelines/sandboxed.py:74`; `ENTRYPOINT_METHOD`, `:80`).
`SandboxedPipelineRunner` (`:136`) drives one run per journal: replay check,
PROPOSED, sandboxed execution (`_execute`, `:255`), classified outcome. The
honest gap — an installed pipeline cannot call `ctx.think` in v1, because a
subprocess holds no facade tokens — is the P3 RPC transport's work, exactly
where the spec's P5 placed executable package loading.

### `pipelines/packages.py` — the install flow (C8.6, the P5-lite)
One install action, one fresh lineage, six stages
(`PipelinePackageInstaller.install`, `pipelines/packages.py:678`):

1. **source** — a local directory, a `git+<scheme>://` URL at a *pinned* ref
   (a commit SHA or a tag; a moving branch is refused after the clone,
   before any byte is read — `_resolve_git`, `:514`), or a signed `.paapkg`
   unpacked through the hardened loader. Host-side git runs with literal
   argv, an allowlisted environment and a wall budget (`_run_git`, `:460`).
2. **declaration** — `pipeline.toml` parsed by the same closed contract
   (`declaration_from_manifest`), plus the `pipeline.py` entrypoint it pins.
3. **the five gates** — the marketplace installer, unmodified in its order
   (signature, content hash, AST scan, permissions, smoke test). An unsigned
   local/git source installs only on the operator's explicit
   `--allow-unverified` opt-in.
4. **conformance** — the six-check standard against the gated bytes; a RED
   report refuses the install *after* the gates, unregistering their
   marketplace row so the zero-trace rule is the whole flow's.
5. **capability review** — the C8.1 human-gate pattern: a deterministic
   `CapabilityReview` artifact (`pipelines/packages.py:203`) listing the
   declaration's ask, the conformance verdict and the content hash; a human
   approves or refuses through the `ReviewApprover` seam. An unwired seam
   refuses (`refuse_review`, `:241`); the rendered review states the v1
   limit honestly — the ask is recorded, not minted (`render_review`,
   `:257`).
6. **stage** — the gated bytes (and the review artifact,
   `REVIEW_ARTIFACT_NAME`, `:123`) land under
   `PAA_HOME/pipelines/installed/<name>/<version>/` (`_stage`, `:1045`),
   which is what makes the registry row appear.

A refusal at any stage leaves zero trace by construction: no marketplace
row, no staged files, no registry row — only the `PIPELINE_PACKAGE_REFUSED`
event that says it was tried. The two events (`PIPELINE_PACKAGE_INSTALLED` /
`PIPELINE_PACKAGE_REFUSED`, `core/types.py:419`) ride their own fresh
lineage, carry `v: 1` payloads, and ship with goldens and replay/recovery
coverage (ADR-0046; `tests/ledger/test_pipeline_package_events.py`).

## The one-call door and the CLI

`install_pipeline_package` (`pipelines/packages.py:1120`) is the single
driver — the CLI's door and the tests' door are the same function, so both
leave identical rows and identical staging. The terminal surface is
`paa pipeline install <dir|git-url> [--ref <sha|tag>] [--allow-unverified]`
(`cli/main.py:914`): the five gates run, conformance runs, and the capability
review is a prompt a human answers (`terminal_review_approver`,
`pipelines/packages.py:291`; a walk-away is a refusal, not an error). Exit
codes follow the skills door's contract: 0 installed, 1 refused (the stage
named), 2 usage.

## The parked boundary (stated, not implied)

The hosted marketplace stays parked, per the spec's own gating sentence
("do not start it without a real outside author" —
`docs/plans/2026-11-21-pipeline-platform-spec.md:426`):
`marketplace/registry_client.py` is untouched, there is no publish verb, and
the only remotes the install flow talks to are git URLs the operator names
at a ref the operator pins. The boundary is pinned by test:
`tests/pipelines/test_packages.py` never imports the registry client.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `PipelineRegistry.default()` | `PAA_HOME` | registry rows (builtin + local + installed) | none (read at construction) |
| `declaration_from_manifest(path)` | a `pipeline.toml` | `PipelineDeclaration` | none |
| `SandboxedPipelineRunner.run(goal)` | staged dir, declaration, journal, sandbox | `PipelineResult` | `PIPELINE_RUN_*`/`CALL_*` rows; one sandboxed child |
| `install_pipeline_package(source, …)` | local dir / pinned git URL / `.paapkg` | `PipelineInstallReport` | gates' marketplace row (unregistered on later refusal), staged bytes, `PIPELINE_PACKAGE_*` event |
| `installed_conformance(…)` | staged bytes, declaration, sandbox | `ConformanceReport` | six real runs on their own lineages |

## Failure behavior

- Off-contract declaration (unknown section, undeclarable capability, bad
  `run.resume`) → `LocalManifestError` / a `declaration`-stage refusal,
  before any gate runs.
- Unpinned or moving git ref, unresolvable ref, non-URL → `source`-stage
  refusal; the clone's words are never logged or raised.
- Unsigned source without the opt-in, invalid signature, tampered payload,
  AST-blocked code, ungranted permission, failed smoke → the refusing gate's
  name, zero trace.
- RED conformance report or a refusing human → refusal *after* the gates,
  with the gates' marketplace row unregistered again.
- Cancelled run → the call row closes `FAILED('cancelled')` before the
  `CancelledError` propagates; exhausted containment clock → a `FAILED` run
  by rule, never a crash.

## Known gaps

- An installed pipeline cannot call facade capabilities (`ctx.think` et al.)
  in v1 — the sandboxed adapter has no token path; that is P3's RPC
  transport, stated in the review artifact so no human approves a silent
  no-op.
- The registry keeps one version per installed name (the last scan wins);
  there is no multi-version pinning surface yet.
- `paa pipeline run` drives the in-process builtins; running an *installed*
  pipeline goes through the sandboxed adapter's callers (the install flow's
  conformance and any host that mints a journal) — a first-class
  `paa pipeline run <installed>` door is P3-shaped work.

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
  refusal paths), `tests/pipelines/test_sandboxed.py`,
  `tests/pipelines/test_registry.py`, `tests/cli/test_pipeline_install_cli.py`
  (the door), `tests/ledger/test_pipeline_package_events.py` (the goldens)
