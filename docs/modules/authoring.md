# Module: The authoring kit (`paa pipeline new`, the scaffold, the worked examples)

## Module identity

- **Name:** authoring — the kit a pipeline author starts from (the spec's P7)
- **Path:** `src/paa/pipelines/scaffold.py` (the renderer), `src/paa/cli/pipeline.py` (the door), `examples/` (the worked references)
- **Main responsibility:** The zero-infrastructure start the pipeline platform's distribution order names first (P7, `docs/plans/2026-11-21-pipeline-platform-spec.md:438`): `paa pipeline new <name>` writes the four files a new pipeline starts from — manifest, entrypoint, README, test skeleton — proven through the closed parser before success is reported, with named refusals for every refusal path; the two worked examples (`examples/tdd-loop/`, `examples/tree-of-thought/`) are complete pipelines in that shape, held to the same six-check standard; and this document carries the protocol compatibility matrix the spec's P7 list asks for.
- **Architectural layer:** Core / orchestration vocabulary (the renderer lives in `pipelines/`); the door is Edge
- **Entrypoints:** `scaffold_pipeline()` (`pipelines/scaffold.py:501`); the CLI door `paa pipeline new` (`cli/pipeline.py:811`); the worked examples' entrypoints (`examples/tdd-loop/pipeline.py:80`, `examples/tree-of-thought/pipeline.py:99`)
- **Public interfaces:** `SCAFFOLD_FILES`, `SCAFFOLD_VERSION`, `ScaffoldError`, `ScaffoldReport`, `scaffold_pipeline` (`pipelines/scaffold.py`)
- **Other modules that depend on it:** the CLI (`cli/pipeline.py`) — nothing else; the scaffold is a leaf by design, it renders files and refuses, it never runs them

## Purpose and responsibilities

The kit's position, stated once: **the files it writes are not documentation
about the contract — they are the contract, in the smallest shape that
already runs.** A scaffold that shipped prose about the manifest would be a
second, driftier copy of `pipelines/registry.py`'s closed parser; a
scaffold that shipped an entrypoint the sandboxed runner could not drive
would be a born-broken start. So everything the kit writes is held to the
same doors everything else is: the manifest must parse
(`declaration_from_manifest`, `pipelines/registry.py:232`), the entrypoint
must answer the result contract (`run_result_shape`,
`pipelines/sandboxed.py:114`), and the install flow's five gates and
six-check conformance standard must pass on the scaffold's own bytes —
pinned by test, not asserted (`tests/pipelines/test_scaffold.py`).

### What `paa pipeline new` generates

Four files, and only ever these four (`SCAFFOLD_FILES`,
`pipelines/scaffold.py:72` — the list is the whole of both write promises,
plain and `--force`):

| file | what it is |
|---|---|
| `pipeline.toml` | a manifest the closed parser accepts as written — identity, `resume`, the (empty) capability ask, all four limits concrete, one typed param, tier `unrated` |
| `pipeline.py` | the entrypoint skeleton for the sandboxed contract (`--paa-args` JSON in, one JSON result on stdout), with the fill-in sections marked |
| `README.md` | the author's own door map: the three doors, the six-check standard, where the limits and the evidence tier live |
| `test_pipeline.py` | the checks the author keeps green while editing, written against the real parser and the real result reader — never a copy of either |

The version is `0.1.0` (`SCAFFOLD_VERSION`, `pipelines/scaffold.py:82`) and
`requires_runtime` is derived from the running facade
(`_runtime_range`, `:125`), so a scaffold is born on-contract the day the
facade moves rather than pinned to yesterday's range.

### The three doors

1. **List and inspect** — the default parent is `PAA_HOME/pipelines`, the
   registry's local dir, so the row appears the moment the scaffold lands:
   `paa pipeline list`, `paa pipeline inspect <name>` (trust `local`,
   listed and inspected, not executed — the local-manifest boundary).
2. **Test while you edit** — from the scaffolded directory:
   `python -m pytest test_pipeline.py`. The checks parse the manifest
   through the closed contract and drive the entrypoint against the result
   shape the host reads.
3. **Install** — the door that makes the pipeline runnable:
   `paa pipeline install <dir> --allow-unverified` (`cli/main.py:914`)
   drives the five gates, the six-check conformance standard and the
   capability review, and stages the bytes under
   `PAA_HOME/pipelines/installed/<name>/<version>/` where the registry row
   is runnable through the sandboxed adapter.

A scaffold placed under `--path` anywhere else keeps both ends: install
accepts it where it stands, list needs the directory moved (or symlinked)
under the local dir.

### The refusals, named

Every refusal is a `ScaffoldError` (`pipelines/scaffold.py:98`) whose
message names the reason before any file is written: an unscaffoldable
name (the closed shape `_NAME_SHAPE` pins, `:89`), the reserved
`installed` (the installer's staging root, `INSTALLED_DIR_NAME`,
`pipelines/registry.py:68`), a taken target (naming what it holds), a
`--force` that would clobber a foreign or unparseable manifest, a parent
that is a file. The parse-back lock (`pipelines/scaffold.py:549`) closes
the loop: the manifest the scaffold just wrote is parsed back through the
real closed parser, and a round-trip mismatch fails the scaffold loudly —
a scaffold can never announce a directory the registry would immediately
name in `local_errors`.

## The protocol compatibility matrix

The spec's §5 defines the wire protocol `paa-pipeline/1`
(`docs/plans/2026-11-21-pipeline-platform-spec.md:281`). What a pipeline
*must answer* depends on which surface of that protocol a host drives —
and this branch drives one of the two:

| protocol | surface | what a pipeline must answer | methods | errors | status vocabulary |
|---|---|---|---|---|---|
| `paa-pipeline/1` — **this branch** (`feat/authoring`) | the sandboxed entrypoint contract (`pipelines/sandboxed.py:74`) | one-shot: `python pipeline.py --paa-args <args.json>`; the args file holds `{"goal", "params"}` (the install-time smoke probe holds `{}`); one JSON result object on stdout | none over a wire — the args file *is* the initialize, the run is the process, cancel is the host's containment clock and process-tree kill | the child never speaks JSON-RPC errors: no JSON, an unknown status or a non-zero exit is FAILED by rule, never a parse error the child chooses | `completed` / `failed` / `needs_human` (`_RESULT_STATUSES`, `pipelines/sandboxed.py:96`) |
| `paa-pipeline/1` — **the RPC transport's row** (W9-C9.3, `feat/rpc`) | NDJSON JSON-RPC 2.0 on the child's stdin/stdout, UTF-8, one message per line (spec §5) | a long-lived child speaking the full method table: `pipeline.initialize` (reply `{ok, name, version}`), `pipeline.run` (return the final result), `pipeline.cancel` (stop within the grace period), `pipeline.shutdown` | the four above (spec `:287`–`:294`) | the `-32001`–`-32008` table below, over the wire | the same three, plus `artifacts` in the final result (spec `:307`) |

**The error codes** (spec `:320`; the classes are this branch's, the codes
are what the RPC transport renders them as — `PipelineError`'s own
docstring says so, `pipelines/base.py:129`):

| code | spec name | this branch's class |
|---|---|---|
| -32001 | `BudgetExceeded` | `BudgetExceededError` (`core/errors.py:125`) |
| -32002 | `PermissionDenied` | `PermissionDeniedError` (`core/errors.py:188`) |
| -32003 | `GateRejected` | `GateRejectedError` (`pipelines/base.py:142`) |
| -32004 | `Diverged` | `PipelineDivergedError` (`:148`); `ToolSequenceDivergedError` (`pipelines/replay.py:224`) on the tool path |
| -32005 | `Cancelled` | `PipelineCancelledError` (`pipelines/base.py:167`) |
| -32006 | `CapabilityNotGranted` | `CapabilityNotGrantedError` (`:173`) |
| -32007 | `InvalidParams` | `InvalidCallParamsError` (`:173`) |
| -32008 | `OutcomeUnknown` | `OutcomeUnknownError` (`:197`) |

Beyond the spec's table, the same server-error band carries this branch's
own surfaces — the overlay (`-32009`/`-32010`), the registry
(`-32012`/`-32013`), the sandboxed adapter (`SandboxedPipelineError`,
`-32014`, `pipelines/sandboxed.py:111`), the install flow (`-32015`) and
the scaffold itself (`ScaffoldError`, `-32016`, `pipelines/scaffold.py:108`)
— additive additions the spec's own versioning rule sanctions.

**The additive-only rule** (`docs/plans/2026-11-21-pipeline-platform-spec.md:329`): `protocol` is
`paa-pipeline/<major>`; changes within a major are additive only, and the
host refuses a pipeline whose declared `requires_runtime` range excludes
the facade version (`FACADE_RUNTIME_VERSION`, `pipelines/base.py:79`). The
closed parser pins the protocol string to `paa-pipeline/1` today — a
manifest declaring anything else refuses at load, by name, before any code
runs. When a `paa-pipeline/2` ever exists, this matrix gains its row and
the old row's surface stays answerable: that is what additive-only means
here.

**Where the transport's own record lives:** ADR-0055 is *not* on this
branch — it lands with `feat/rpc` (W9-C9.3), the same wave. This branch's
latest pipeline ADRs are 0050 (the host, the journal, replay) and 0052
(the manifest contract); the matrix above cites the spec's §5 directly
rather than pre-citing an ADR the tree cannot show a reader.

## The worked examples

Two complete pipelines in the scaffolded shape, held to everything a
scaffolded pipeline is held to (`tests/pipelines/test_examples.py`: the
closed parse, the registry row, the author's own checks passing where they
land, and the install flow green through the five gates with all six
conformance checks passing):

- **`examples/tdd-loop/`** — the ordered-stages reference: red (name the
  failing test first, `_red`, `examples/tdd-loop/pipeline.py:50`), green
  (the smallest change — deliberately duplicating, which is what refactor
  exists to merge), refactor (merge the duplication); `max_cycles` and
  `strict_refactor` are layer-1 params with metrics-visible effects
  (`run`, `:80`).
- **`examples/tree-of-thought/`** — the branching reference: propose
  (branch the current thought), critique (a stable digest score, keep or
  prune), select (the best survivor by a total `(score, candidate)`
  order, `_select`, `examples/tree-of-thought/pipeline.py:87`); `depth`
  and `branching` are the knobs (`run`, `:99`).

Both state the same honest split in their READMEs: the *shape* is the
example; the *work inside each stage* is a deterministic stand-in, because
a sandboxed entrypoint holds no facade tokens in v1 — wiring `ctx.think`
into a stage is the RPC transport's row, not an example's.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `scaffold_pipeline(name, parent, force)` | a name, a parent dir | `ScaffoldReport` (the four files, where) | four files under `parent/name`, nothing else |
| `paa pipeline new <name>` | name, `--path`, `--force` | exit 0 + the receipt, or exit 1 + the named reason, or exit 2 usage | as above |
| the examples' `run(goal, params)` | a goal, the typed params | one result object (`completed`/`failed`/`needs_human`, summary, metrics) | none — pure and deterministic |

## Failure behavior

- Unscaffoldable name, the reserved `installed`, a taken target, a
  `--force` facing a foreign or unparseable manifest, a parent that is a
  file → `ScaffoldError` before any byte is written; the CLI prints the
  reason and exits 1.
- A scaffolded (or example) manifest that fails its own parse-back → the
  scaffold refuses after writing, naming the parser's own message — the
  lock, not a hope.
- A missing argument at the door → Typer's usage error, exit 2.

## Known gaps

- **No TypeScript SDK in v1.** The spec's P7 list carries one; this branch
  does not, and the gap is stated rather than papered over: Python's
  entrypoint contract above is the whole authoring surface today, and the
  first non-Python proof is the RPC transport's row (W9-C9.3, `feat/rpc`),
  which is where a second SDK would bind. This mirrors the spec's own P7
  list rather than deviating from it.
- **No slot wiring and no capability ask in what the scaffold writes** —
  the manifest teaches both vocabularies as comments and asks for
  nothing, because every key asked for is surface a human reviews and an
  installed pipeline holds no facade tokens in v1.
- **`paa pipeline test`, the standalone conformance door, is not on this
  branch** — it arrives with the same transport work; until then the
  install flow runs the same six checks (`CHECK_NAMES`,
  `pipelines/conformance.py:78`), which is what the examples' proofs
  drive.
- The scaffold never runs the code it writes — running an author's bytes
  is the install door's job, behind the gates (`install_pipeline_package`,
  `pipelines/packages.py:1120`).

## Related documents

- [`pipelines.md`](pipelines.md) — the platform the kit scaffolds into:
  the declaration contract, the registry, the conformance standard, the
  install flow
- `docs/plans/2026-11-21-pipeline-platform-spec.md` — §4.1 the manifest
  contract (`docs/plans/2026-11-21-pipeline-platform-spec.md:235`), §5 the
  wire protocol (`docs/plans/2026-11-21-pipeline-platform-spec.md:281`), P7
  the authoring kit (`docs/plans/2026-11-21-pipeline-platform-spec.md:438`)
- `docs/adr/0050-pipeline-host-and-call-journal.md` and
  `docs/adr/0052-manifest-slots-overlays-tighten-only.md` — the host and
  the closed declaration contract
- `examples/tdd-loop/README.md` and `examples/tree-of-thought/README.md` —
  the worked references' own door maps
- The done-when as tests: `tests/pipelines/test_scaffold.py` (the
  renderer and every refusal path), `tests/pipelines/test_examples.py`
  (the worked references through the whole install flow),
  `tests/cli/test_pipeline_new_cli.py` (the door)
