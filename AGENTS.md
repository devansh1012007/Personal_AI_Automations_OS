# AGENTS.md — PAA runtime

Local-first, event-sourced agent substrate (`paa`, Python ≥3.11, src-layout under `src/paa`).
Background: `README.md`. Design reasoning and every RFC deviation: `docs/adr/README.md`.

## Setup and verification

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q          # Linux; README shows the Windows .venv/Scripts path
```

The `dev` extra carries `paa[api]`, so the documented setup installs FastAPI and the API
suite **runs**. It did not used to: `tests/api/conftest.py` skips the whole directory when
the extra is absent, so a developer following the documented setup was never exercising a
single route, and it said so by *skipping* — the quietest way to not test something. The
`dev` extra now resolves `paa[api]`, `paa[s3]`, `paa[attest]` and `paa[tui]` itself (the same
self-referential form `all` already used), so there is no environment in which the gate
below can pass while the API, S3, attest or chat-TUI path is untested.

Verify in this order before calling work done:

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/ -q
```

- Focused runs: `pytest tests/ledger -q`, `pytest tests/ledger/test_replay.py::test_x -q`,
  `pytest -m "not slow"`, `pytest -m chaos`, `pytest -m requires_graph`,
  `pytest -m requires_postgres` (needs `PAA_TEST_POSTGRES_DSN`; see below).
- mypy is `strict = true` with `warn_unreachable` — `disallow_untyped_defs` is relaxed only for `tests.*`.
- ruff `line-length = 100`, and `S` (bandit) is on. The `S608` / `S105` ignores in `pyproject.toml` are
  per-file allow-lists for known-safe patterns; do **not** add new files to them, and do not "fix" a
  triggered line by widening the ignore. A single line that legitimately interpolates SQL takes a
  **per-line** `# noqa: S608` (the convention in `memory/facts.py` and `skills/registry.py`) — that is
  narrower than a per-file allow-list and keeps the rest of the file under the rule.
- **The baseline is zero, and that is a different kind of gate.** `ruff` reports **0** findings
  and `mypy` **0** errors across `src/` and `tests/`. It used to report 69 and 108, and every
  one of those was fixed by changing the code rather than by silencing the checker: no
  `# noqa` was added, no `# type: ignore` was added, no ruff per-file ignore was added, and no
  `pyproject.toml` rule was relaxed. "Do not expand the count" was the old gate; **"do not
  make it non-zero" is the one now**. The ways to break it are the same ways it was broken
  before: an `# noqa` over a real finding, a `cast` where a check could have been written, a
  `type: ignore` for a narrowing mypy could not see, a type widened to `Any` so the check
  stops mattering. A finding that is genuinely *not* a finding — an unused directive, a
  stale `type: ignore` — is deleted, not silenced.
- **The fixes were not all cosmetic, and that is the point of the exercise.** Clearing the
  type errors surfaced real defects that no test had caught: a `false` JSON schema accepted
  every payload it was written to refuse; an `await` inside a generator expression made every
  memory write that reached the embedding hand-off die; a property shadowed `str.title` and
  broke the `str` contract; a `BlobStore` protocol declared a signature **neither**
  implementation had, so `isinstance` passed on a contract that could not be called; a
  Windows-only branch of the sandbox was invisible to the type checker because mypy folds a
  `sys.platform` comparison against the platform it is checking, so the containment code was
  unchecked. A type error is a symptom, and the symptom is sometimes a lie about the code.
  Fix the code, not the symptom.
- **Six of the suite's failures were the host, not the code, and the host is now fixed.** The
  sandbox builds its child environment from scratch with `PATH=/usr/local/bin:/usr/bin:/bin` —
  deliberately not the host's `PATH`, because the host's `PATH` leaks the developer's
  toolchain and can put an attacker-writable directory ahead of the system ones
  (`sandbox/base.py::build_child_env`). On a host whose `python` was user-local
  (`~/.local/bin/python`) and which had no `/usr/bin/python`, a plan step that ran
  `python -c "…"` found no interpreter, the task never reached `COMMITTED`, and the six
  assertions that expect `COMMITTED` failed. The ledger, the CLI and the recovery sweep were
  all behaving correctly. The fix was one line on the host — `sudo apt install
  python-is-python3` — and widening the sandbox's `PATH` would have undone the security
  property it exists for. **A host without `python` in a system directory will fail those
  six tests again**, and the fix is the host's, not the code's.
- The `requires_postgres` marker and the `PAA_TEST_POSTGRES_DSN` convention are described
  under *Testing conventions* below. The mypy count went 116 → 108 (the schema-migration
  change closed eight) → 0 (the section-by-section pass closed the rest).
- There is **no Makefile and no pre-commit hook** in this repo; the gate is enforced by the CI
  workflow (see *The rules this tree holds itself to*), and nothing enforces it locally.

## The one invariant: the ledger is the truth

`src/paa/ledger/` is the spine; Redis, queues, vector/graph indexes, and the filesystem are all derived
views rebuilt by replay (`ledger/replay.py`). Two rules follow and are not negotiable:

1. **Ledger event before side effect, always.** A crash between event and work is recoverable; the
   reverse is not. See the task lifecycle diagram in `README.md`.
2. **Everything else must be replayable.** If you add state that isn't derivable from events, it needs
   a snapshot *and* a recovery rule (`ledger/store.py::save_snapshot`/`load_snapshot` for the snapshot,
   `ledger/recovery.py` for the sweep rule).

Adding an `EventType` means touching, at minimum: `core/types.py` (`EventType`, plus `TERMINAL_EVENTS`
if it ends a lineage), `ledger/replay.py` (`_PHASE_MAP` and the projection `match`, or `_NO_PHASE_CHANGE`
if it is informational), and its emitter — `agents/orchestrator.py` for lifecycle events and
`pipelines/builtin.py` for the extracted stages, `ledger/recovery.py` for recovery and rollback events, `models/router.py` for `MODEL_ESCALATED`.
`ledger/store.py` assigns `state_version`, links `prev_hash`, and updates the head projection in one
transaction — never append outside it.

`EXECUTION_COMPLETED` fires **per step**, not per task (ADR-0010); it is not terminal.

## Storage is a polyglot with a parity lock

`storage/relational/schema_sqlite.sql` and `schema_postgres.sql` must stay table- and column-identical
(types may differ: UUID/JSONB/TIMESTAMPTZ vs TEXT/INTEGER). `tests/storage/test_schema_parity.py`
parses both with a small DDL lexer and fails the build on any divergence. If you add a column, add it
to **both** files.

Every substrate has an embedded default and a server variant, selected by `PAA_*` env vars
(`StorageSettings.backend_*`). The factories degrade, they never raise — `runtime.py::_try_vector /
_try_graph /_try_queue` and `runtime.py::_acquire_sandbox` (5s probe timeout, then subprocess) are the
pattern. Heavy deps (qdrant, kuzu, sentence-transformers, redis, psycopg, boto3) are optional extras;
**never** import one at module scope in a core path, or a minimal install stops importing.

### Every LIKE pattern comes from one helper

A `LIKE` pattern is the one place where a *value* changes the *meaning* of a query. `%` matches any run
of characters, `_` matches any one, and a NUL ends the pattern where SQLite reads it through the C
string API — so `%\x00%` is a bare `%` that matches every row. A keyword that happens to contain one
is that keyword, not a pattern.

**Every `LIKE` pattern that interpolates text comes from `storage/relational/like.py`** —
`like_pattern(term)` for the pattern, `like_predicate(*columns)` for the SQL half, and
`escape_like` underneath them. Not from an f-string at the call site. Three facts are stated once
there and are not to be rediscovered per site:

- SQLite's `LIKE` has **no default escape character**; PostgreSQL's defaults to a backslash. The same
  escaped pattern is wrong on one engine and right on the other unless the SQL declares `ESCAPE '\'`.
  The escape character and the clause are derived from one constant, so they cannot drift.
- A **NUL truncates the pattern**, and the escape clause does not help — a NUL is not a metacharacter,
  it is the end of the string.
- A term with **nothing to search for** is refused, not compiled: the pattern would be `%%`, which
  matches every row, and there is no `LIKE` pattern that matches nothing instead.

The policy on a NUL is the caller's, and both are deliberate: `memory/facts.py` refuses it (the query
is a model-supplied tool argument, so the refusal goes back to the model as an error value), and the
internal recall paths drop it and carry on. ADR-0023 has the full reasoning and the consequence at
each site.

**A `LIMIT` cut needs a total order.** `ORDER BY` on a non-unique column leaves the cut to an order
the SQL does not define, so a `LIMIT` over it is not reproducible by the standard — add a unique
secondary key (`f.id`). SQLite happens to be deterministic for a fixed plan, but that is an
implementation detail, and PostgreSQL promises nothing about ties at all.

## Config

- `PAA_HOME` roots all state; default `~/.paa`, deliberately outside the repo so `git clean` can't eat
  the ledger.
- Settings are pydantic-settings: prefix `PAA_`, nested delimiter `__`
  (e.g. `PAA_STORAGE__POSTGRES_DSN`), `.env` read from the **current working directory**.
- That `.env` is read by *two* layers, and only one of them is pydantic-settings. A provider API key is
  not a `Settings` field — `registry.ProviderSpec.api_key_env` names the env var per row — so
  `registry.resolved_api_key` falls back to `config.dotenv_value(name)`, which parses the same file
  **without** touching `os.environ` (writing there would inject every `PAA_*` line into the process,
  including test runs). Environment still wins; `reset_dotenv_cache()` exists for tests that chdir.
  If you add anything else configured by name rather than by field, use `dotenv_value` too.
- `config.py::_enforce_loopback` refuses a non-loopback `api_host` unless `PAA_ALLOW_NON_LOOPBACK=1`.
- Config stores the *names* of secret env vars (`s3_secret_key_env`, `escalation_api_key_env`), never
  values. Keep it that way.
- **"MinIO" is still in the tree, and every remaining occurrence is deliberate.** The cold lake's
  substrate is S3 — the *protocol* — and the compose stack's server is SeaweedFS; the extra is
  `paa[s3]`, and `paa[minio]` is removed on purpose so the break lands at install time
  (ADR-0026). What survives is a compatibility alias and one filename:
  `StorageSettings` still accepts `PAA_STORAGE__MINIO_*` and `backend_coldlake="minio"` through
  `_accept_legacy_minio_names`, so a deployment written against the old names keeps working; and
  the module is still `storage/coldlake/minio_backend.py` (the file rename is deferred by
  ADR-0026). Do not "tidy" the alias away — an operator's deployment breaks when a key is renamed
  under them — and do not read the module name as a dependency: nothing imports `minio`.
- `get_settings()` is `lru_cache`d. Anything that changes `PAA_HOME` (tests, mostly) **must** call
  `reset_settings_cache()`.

## Testing conventions

- `asyncio_mode = "auto"` — async tests need no decorator. Fixtures are function-scoped and write into
  `tmp_path`, so order-independence is expected; don't add session-scoped state.
- `filterwarnings = ["error::DeprecationWarning:paa.*"]` — a DeprecationWarning raised from `paa`
  fails the test. Fix the code, don't silence it.
- Extras-gated tests use a `pytest.mark.requires_*` marker **and** a `skipif`/`importorskip` guard
  (see `tests/storage/test_vector.py`, `tests/api/conftest.py`). `--strict-markers` is on.
- The **PostgreSQL** tests are the exception that proves the rule: they need a *server*, not just
  the extra, so they are marked `requires_postgres` **and** skipped unless `PAA_TEST_POSTGRES_DSN`
  names one. The DSN names a *server*: `tests/storage/test_postgres_migrations.py` creates a
  database of its own on it and drops it afterwards, so a run can never touch the database a
  deployment is using.
- `paa_home` (tests/conftest.py) isolates state; `snapshotter` is a byte-oriented fake on purpose —
  `write_text`/`read_text` would translate newlines on Windows and report phantom manifest drift.
- Set `PAA_TEST_VERBOSE=1` to un-silence structlog.
- Hypothesis covers the invariants that must hold for all inputs (gatherer token ceiling, replay over
  arbitrary event orderings, confidence in [0,1], LIKE-escaping losslessness). Extend those rather
  than writing example-based tests for them. The files that do: `tests/context/test_budget.py`,
  `test_compaction.py`, `test_gatherer.py`, `test_metrics.py`, `tests/ledger/test_replay.py`,
  `tests/memory/test_decay.py`, `tests/storage/test_like.py`.
- A Hypothesis test that calls a function which can raise must handle the raising input. Draw the
  adversarial value from the strategy, don't hope it turns up: a strategy that *might* produce one
  makes the test a coin flip, and a test that only sometimes exercises the rule is a test that
  sometimes misses it.
- Crash/chaos tests simulate power loss by closing the DB mid-lineage and reopening — committed
  transactions survive, uncommitted vanish.
- `tests/validation/` now has its own suite (`test_schema_validator.py`, 19 tests); it was an
  empty package until the JSON Schema boolean fix gave it something to pin.

## Model layer

Agents depend on the narrow `agents/reasoning.py::ModelLike` protocol
(`complete_structured(prompt, schema, **kwargs)`), never on a provider. `runtime.py::RouterModelAdapter`
and `coder/model_adapter.py::RouterToolCallingModel` are the only two places that know about
`EscalatingModelRouter`; keep it that way.

A new provider threads the transport-retry budget (`ModelSettings.max_transport_retries`, ADR-0021)
through its constructor, and so do the fast paths in `models/__init__.py` and the registry rows in
`models/registry.py` — a provider that does not retry a transient 429 ends the whole run, and
escalation policy can forbid the fallback that would have absorbed it.

To test anything agent-shaped, inject a scripted model:

```python
rt = await Runtime.build(model_adapter=ScriptedModel(), ...)
```

`EchoProvider` cannot drive a plan (its minimal-schema-instance output isn't a plan). The
`ScriptedModel` in `tests/integration/conftest.py` is the reference implementation — it answers by
schema shape and records every call. That is how the "no model in the security loop" guarantee is
asserted, and it is asserted *structurally*: `test_policy_agent_has_no_model_dependency` proves the policy
agent holds no model dependency at all, so a blocked task reaches its verdict with the model never
consulted. (A different, unrelated `ScriptedModel` lives in `tests/coder/test_agent.py` — it emits
scripted `ModelTurn`s, not schema-shaped dicts.)

## Conventions that differ from defaults

- `class X(str, enum.Enum)`, **not** `StrEnum` (UP042 is deliberately ignored in `pyproject.toml`).
  Pass `.value` explicitly; enums round-trip through pydantic and SQLite TEXT columns.
- `CorrelationId`/`SessionId`/`TaskId` are `NewType`s over UUID, not bare strings — don't collapse
  them, transposed-id bugs are what they exist to catch.
- Ids are UUIDs; never assume monotonicity. Deterministic tie-breaking on "oldest first" needs an
  explicit secondary key or ingestion order stops meaning anything under load.
- Docstrings here are long and load-bearing: they carry the RFC section or `SPEC DEVIATION
  (docs/adr/NNNN)` rationale. A deviation from the RFC without an ADR is a review failure. Comments
  explaining *why* (not *what*) are the house style — see the `#:` attribute comments in `config.py`.
- Payload hashing uses `ledger/events.py::canonical_json` (sorted keys, no whitespace). Any change to
  serialization breaks the hash chain — treat as a data migration.

## Docker / server topology

Separate from the laptop default and documented in `docs/deployment/docker.md`:
`cp .env.example .env && docker compose up -d --build`. Postgres/Redis/Qdrant/S3 (SeaweedFS) publish
**no** host ports; only the ingestion API on `127.0.0.1:8787`. gVisor is opt-in, Linux-only, via
`docker compose -f docker-compose.yml -f docker-compose.gvisor.yml up -d`, and the runtime honestly
reports `NAMESPACE` instead of `VM` when `runsc` isn't registered.

The `paa` container mounts `/var/run/docker.sock` so the daemon can launch the containers the
sandbox backend runs commands in (ADR-0024). Three things about that are footguns, and all three
have bitten:

- **The socket's GID must be a supplementary group of the container user.** The socket is
  `root:<docker-gid>` mode `0660` and the container's `paa` user is uid 10001, so the mount
  alone is not enough. `docker-compose.yml` passes `group_add: ["${DOCKER_GID:-999}"]`; set
  `DOCKER_GID=$(stat -c %g /var/run/docker.sock)` in `.env`. Without it: *"permission denied
  while trying to connect to the docker API"*.
- **Under gVisor the socket also needs `runtimeArgs: ["--host-uds=all"]`** in the Docker
  daemon's runtime registration. gVisor does
  not pass a host Unix socket through merely because it was bind-mounted in. Both socket
  spellings are listed because `/var/run` is a symlink to `/run` on the host but not
  necessarily inside the container. A ready-to-copy file is `deploy/docker/daemon.json`,
  and the reasoning behind it is in `deploy/docker/README.md` — the file itself
  must be plain JSON, because the Docker daemon **refuses to start** on a
  top-level key it does not recognise. The same list appears in
  `docker-compose.gvisor.yml` and `docs/deployment/docker.md` — keep the three
  in step.
- **The socket is host root.** Deliberate, recorded, and reversible — but it means a
  compromise of the daemon is a compromise of the host. The executor-sidecar design (the
  daemon never holds the socket) is the real fix and is deferred.

Two container images, both built from this repo: `paa/base_worker:v4.1` (`deploy/worker/`,
the untrusted plan-step image — deliberately minimal) and `paa/toolbox:v1` (`deploy/toolbox/`,
the MCP-server and plugin image — carries Node 24 because most MCP servers are npm packages).
`paa/base_worker:v4.1` is also the ledger's `allocated_worker_image` default, so the recorded
image and the executed image cannot drift apart.

Two invariants the sandbox backend holds, and that a change to it must not break:

- **The container's uid is never 0, and `--user` is always passed.** Omitting the flag
  means "whatever the image says", and an image that says nothing runs as root. The uid is
  the workspace's owner when it is known and not root, otherwise the image's declared
  user, otherwise `10001:10001`. A root-owned workspace must not select `--user 0:0`.
- **The isolation level and `memory_enforcement` follow the *effective* runtime**, not the
  requested one. A host that asks for `runsc` but has not registered it runs under `runc`,
  with a real cgroup cap, and must record `"cgroup"`.

### Image builds are network-fragile by nature

A container build reaches the network for its dependencies, and the network fails for
reasons nobody in this repository controls. A build that dies on a dropped connection is
indistinguishable from a build that died on a real error — so **every network operation
in every Dockerfile retries**, and only a *persistent* failure is allowed to stop a build.

The rules, applied by convention in `Dockerfile`, `deploy/worker/Dockerfile` and
`deploy/toolbox/Dockerfile`:

- **apt**: `-o Acquire::Retries=10` on both `update` and `install`.
- **pip**: `PIP_RETRIES=10` and `PIP_TIMEOUT=120`, *plus* an outer `retry()` shell loop
  (5 attempts, growing backoff). pip's own retry covers a request that failed mid-flight;
  the loop covers a download pip considered complete and turned out not to be.
- **plain downloads** (the Docker client tarball): the same `retry()` loop, and the
  SHA-256 is verified *after* the download — a corrupt transfer is a failed attempt, not
  a failed build.

The runtime image is built in three stages arranged around that fragility:

- **`wheelhouse`** — downloads every dependency wheel, into a layer that depends only on
  `pyproject.toml`. The requirement list is extracted from the project's own metadata
  with `tomllib`, never maintained by hand: a hand-maintained list is a second place to
  forget to update, and the failure it produces is an image missing a dependency at
  *runtime* rather than at build time. A source change does not invalidate this layer,
  so a rebuild after a code change does no network I/O for dependencies at all.
- **`builder`** — installs from that wheelhouse with `--no-index`, i.e. **offline**. This
  is the property that makes the wheelhouse safe: the install either finds every wheel
  it needs or fails loudly. It can never quietly install less than the project declares.
  `pip check` runs afterwards as the last line of defence.
- **`runtime`** — the venv, the Docker client, and a non-root user. No compiler, no pip
  cache, no source.

Do not "simplify" the builder stage back to a networked `pip install`. The offline
install is what turns a stale or incomplete wheelhouse into a build failure instead of
a broken image.

## Known-stale references (don't chase these)

- `docs/adr/README.md` indexes ADRs 0001–0018 and 0019–0026, 0028–0035 (0027 is reserved
  for the tool-containment/MCP layer), but only `0008`, `0010`, `0015`, `0019`–`0026` and
  `0028`–`0035` exist on disk. The rest are **index-only**: the decision is recorded in
  `README.md` and in the source's own `SPEC DEVIATION (docs/adr/NNNN)` citation, but the body
  is still to be written (the ROADMAP's cross-cutting *Docs* item).
- `README.md`'s Testing section still shows Windows paths, and its per-subsystem test counts in the
  build-status table are approximate — the total line is stale too, so reconcile neither against it.
- `src/paa/agents/specialists/` is an empty package.
- `tests/validation/` has a suite per module, and was an empty package until the JSON Schema
  boolean fix gave it something to pin.

## The coder agent (`paa agent`)

Full write-up: `docs/coder-agent.md`.

`src/paa/coder/` was standalone until `coder/agent.py` wired it up: `CoderAgent.build()`
composes a real `Runtime` + `coder/runtime_tools.py` + `RouterToolCallingModel` + `AgentLoop`.
Read `coder/__init__.py` before adding to that package — the pieces are deliberately decoupled.

**Position (SPEC 2 C8.3, Wave 8):** the coder is the *preliminary* loop — the fallback
coding loop a run reaches for when **no external harness is attached**. It stays
deliberately small, per the owner's direction ("not overkill, just enough to add features
to the OS"); external coding harnesses arrive via `paa record` (the flight recorder
mediating their tool calls) and `paa thread` (the control plane that swaps them
mid-task), never by growing this loop. The position is machine-readable in the pipeline
registry: `CODING_FALLBACK_PIPELINE = "coder.tool-loop"`
(`pipelines/registry.py`), pinned by `tests/pipelines/test_registry.py::TestTheCodingFallback`,
and surfaced by `paa pipeline inspect coder.tool-loop`.

- **The model seam is constrained JSON, not native tool calls.** `coder/model_adapter.py` asks the
  router for a `{text, tool_calls[]}` object conforming to a schema with tool names in an `enum`;
  `ModelProvider.complete_structured` does the validate-and-repair. No provider surface accepts a
  *catalogue* of callable tools — the only `tools` payload in `models/` is Anthropic's single
  synthetic structured-output tool, whose `input_schema` is the JSON schema itself — so adding
  native tool calling is an optimisation *inside that one class*.
- **`ModelTurn.tokens` is always 0** on this path (`complete_structured` returns a dict and drops
  usage numbers). `AgentLoop(max_tokens=...)` therefore never trips — bound runs with `max_steps`.
- **Tool names must match the policy globs.** `submit_task`/`run_task`/`remember_fact` match none
  of `DEFAULT_MUTATING_TOOLS`, which is why `CoderAgent.build` passes
  `mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS)` — otherwise `--mode plan`
  would silently exempt the three tools that actually change state. A new mutating tool needs
  adding to that tuple or it becomes read-only-by-accident.
- **ASK mode standing-allows the read-only half.** `CoderAgent.build` passes
  `allow=[*READ_ONLY_TOOLS, *(allow or [])]`, so only state-changing calls prompt. An explicit deny
  still wins (precedence step 1) and PLAN still refuses the mutating tools.
- **Memory writes go through the cold lake.** `remember_fact` files the assertion as a
  `cold_lake_signals` row and materialises the facts from it, so the fact carries a resolvable
  `source_signal_id` and the daemon cannot write it a second time — the tool and the daemon batch
  claim the same row, and whoever wins writes the facts in the transaction that marks the row
  processed (ADR-0022). A new tool that writes memory does the same; a direct write into
  `hot_serving_active_facts` is not rebuildable by replay and breaks the invariant the whole
  subsystem rests on.
- Provider choice is entirely `PAA_MODELS__*`; the command names no vendor. To use OpenRouter:

  ```bash
  export OPENROUTER_API_KEY=sk-or-...
  export PAA_MODELS__LOCAL_PROVIDER=openrouter
  export PAA_MODELS__LOCAL_MODEL=openai/gpt-4o-mini
  export PAA_MODELS__ESCALATION_PROVIDER=none
  paa agent --list-tools          # catalogue without spending a token
  paa agent "which backends are live, and what is open?"
  ```

  `LOCAL_PROVIDER` holding a hosted model is a router *slot* name, not a locality claim — the
  registry row sets `tier = FRONTIER`, so `leaves_machine` in any escalation record stays truthful.
  Without `--yes`, an ASK-ed (state-changing) call prompts; with no confirm callback the loop
  auto-*denies*.

## The rules this tree holds itself to

Five rules, each with the reason it exists. They are grouped at the end of the file
rather than beside their topics for one reason: **the documentation quotes this file by
line number**, so a rule inserted in the middle silently renumbers a dozen citations
elsewhere, and the reader following `AGENTS.md:33` lands on the wrong line. Add to the
end, and say so in the commit that adds the rule.

### A contract is resolved the same way everywhere it is used

`RelationalBackend` is the relational substrate's contract, and nine modules that one
commit touched used to resolve it two different ways: a runtime import in one, an
`if TYPE_CHECKING:` block in the other eight. The difference is invisible until
something needs the *class* rather than the annotation — a runtime `isinstance`, a
pydantic model resolving its annotations at class-creation time, a `get_type_hints`
call — so it survived a review. **All nine now resolve it the one way, and there is no
exception mechanism**: the deferral map that used to let a module stay behind
`if TYPE_CHECKING:` is gone, because it held a single entry, and a deferral that is
fulfilled is drift nobody is coming back to.

The lock is `tests/conventions/test_import_conventions.py`, and it has six halves. The
**source** half says the contract is not imported behind `if TYPE_CHECKING:` anywhere in
`src/paa`. The **runtime** half says every module that *mentions* it — in any import
form — binds it in its own namespace, to the same object, which is what "resolved at
runtime" means as opposed to "annotated at runtime". The **annotation** half says the
family, and the package that owns the contract, can have their hints read at runtime.
The **sweep** half says every module under `src/paa` imports cleanly in an interpreter
that has imported nothing, and it is a **subprocess** because a cycle breaks only in a
fresh one — inside a test session `sys.modules` is already populated and the cycle's
back edge takes the shortcut that hides it. The **extra** half says each module the
sweep tolerates a failure for really needs the extra it is listed under, so the
tolerance list is a set of facts about the tree and not a set of claims about it. The
**classification** half says every `if TYPE_CHECKING:` block left in the package is
classified.

The classification is the tree's own, and it replaced the prose that used to carry it:
a real cycle (proved by converting the block in a copy of the tree and watching the
import fail), an optional extra, deliberate laziness (the measured price of every
block is recorded, so the judgement can be re-examined rather than taken on trust), a
guard that buys nothing, or a cheap first-party import. The table is
`TYPE_CHECKING_CLASSES` in the lock's own file, and it is checked against the same AST
sweep it was derived from — so a block that appears without a class fails the lock, and
a block that is converted without being removed from the table fails it too. **A new
`if TYPE_CHECKING:` block has to be classified in the same commit that adds it.**

### The live S3 suite names a server, not a bucket

The live S3 tests follow the PostgreSQL convention exactly, because it is the same
convention: the environment names a **server**, and the fixture makes a resource of its
own on it. `PAA_TEST_S3_ENDPOINT` / `PAA_TEST_S3_ACCESS_KEY` / `PAA_TEST_S3_SECRET_KEY`
name one server; a **second** server is the same four names with a label in front of
the last word (`PAA_TEST_S3_SEAWEEDFS_ENDPOINT`), so the unlabelled spelling is the
single-server case of the rule rather than a second thing to remember.
`PAA_TEST_S3_BUCKET` names the **base** of the test's bucket and the fixture appends
the pid and a uuid, so a run can never write to a bucket the operator chose.

The suite is marked `requires_s3` and needs a **server** as well as the driver: a
missing driver is a missing dependency and a missing server is a missing server, and
neither is a failure. ADR-0033 records the rule the suite exists to enforce — a stub
shaped like the driver proves the **mechanism** and nothing about the **table**, and
the codes a server actually answers with are the codes the error vocabulary has to
name.

### The gate is enforced on every push

`.github/workflows/ci.yml` runs the repo's three gate commands on every push and pull
request — `ruff check src tests`, `mypy src`, and the whole `pytest` suite with
`--strict-markers --strict-config` — and it runs them over a **matrix of install
shapes** (a minimal one, the documented `.[dev]` one, and `.[all]`), because "never
import one at module scope in a core path" is a claim a minimal install has to be
tested against rather than asserted. The developer shape installs `.[dev]`, **not**
`.`: the `dev` extra is what carries `paa[api]`, `paa[s3]`, `paa[attest]` and
`paa[tui]`, and without it the API suite, the live S3 suite, the attestation suite
and the chat-TUI suite skip themselves at import — the quietest way to not test
something. The minimal shape's mypy excludes the two trees whose extras it lacks
(`src/paa/api`, `src/paa/tui`); the `dev` and `all` shapes type-check all of `src`.

Two things in it are landmines rather than choices:

- **`python-is-python3` is installed on purpose.** The sandbox builds its child
  environment's `PATH` as `/usr/local/bin:/usr/bin:/bin` — deliberately, because the
  host's `PATH` leaks the developer's toolchain — so `actions/setup-python`'s
  tool-cache interpreter is invisible to it. Without the package, six assertions fail
  on the runner for a reason that has nothing to do with the code, and the fix is
  never to widen the sandbox's `PATH`.
- The install uses the **network-fragility convention** the Dockerfiles already
  encode — `PIP_RETRIES`, `PIP_TIMEOUT`, and a retry loop around the install —
  because a CI install that gives up on one dropped connection is a red build that
  says nothing about the code.

A **PostgreSQL service container** and the **S3 service containers** are declared with
health checks, and the environment variables the suites read are exported with the
names the suites read: `PAA_TEST_POSTGRES_DSN`, and the two servers' *labelled*
`PAA_TEST_S3_*` spellings, because the label is what a failing test's id names. A
green run therefore says something about the server-backed paths, and a run without
the services says only that the suites skipped.

### A `path:line` in the documentation is a promise

`tests/conventions/test_docs_references.py` extracts every `path:line` reference from
the markdown in its scope and asserts three things: the cited path names **exactly
one** file, the cited line **exists**, and the cited line is **not blank**. A blank
cited line is almost always a wrong reference, and a bare name that several files
share — `base.py` is eight of them — names no file at all, so a reference has to be
qualified. Two directories are excluded, each with its reason in the checker itself:
`docs/agent-ops/` is the run trail, where a reference was true when it was written and
is allowed to have rotted since, and `docs/plans/` is a dated proposal that discusses
references as findings rather than citing them.

**The lock cannot see a reference that is wrong without being broken** — the line
exists, it is not blank, and it no longer holds what the sentence claims. That half is
human work, and it is why a corrected reference has to be *read* and not merely
re-numbered.

### Two policy decisions are open, and they belong to the operator

The policy agent's gate has two questions that were deliberately **not** answered in
code, because each one is a judgement about what the policy should *say* rather than a
defect to repair. The declared-permission half of `agents/policy.py::_check_lockdown`
cannot fire — the permission check refuses first — and making it reachable changes
*which* refusal the ledger records, for a plan class that is refused either way. And
four pattern-semantics findings (`git push -f`, the `rm` flag spellings, `DELETE FROM
public.users`, `TRUNCATE` without `TABLE`) each widen a security rule over prose. Do
not "fix" either without the operator's word; both are carried, with their
reproductions, in the run's work ledger
(`docs/agent-ops/20261022-w6-w7-w9/01-work-ledger.md` §"Open items").

## Mandatory Engineering Workflow

### 1. Understand Before Changing (ALWAYS)

Before making **any code change**, spawn subagent(s) to investigate the relevant code and its surrounding system. This step is mandatory, regardless of change size.

- Read relevant documentation, `AGENTS.md` files, ADRs, and existing tests.
- Trace execution flows, dependencies, interfaces, invariants, and downstream effects.
- Identify the root cause, not just the visible symptom.
- Return a concise context report identifying affected files, risks, expected behavior, and required tests.
- Do not modify code until sufficient context has been gathered. Never invent APIs, assume behavior, or guess at undocumented contracts.

### 2. Implement in Atomic Phases

Break work into small, independently verifiable phases. Make minimal, focused changes that preserve existing contracts unless a change is explicitly required.

Follow established conventions, keep abstractions justified, handle errors explicitly, validate inputs, and avoid duplication, dead code, unnecessary dependencies, and unrelated refactoring. Update documentation when behavior or interfaces change.

Use tests and actual source evidence to resolve uncertainty. Never fabricate implementation details, claim unverified behavior works, or weaken tests merely to achieve a passing suite.

### 3. Mandatory Independent Review (EVERY PHASE)

After each atomic phase, spawn a **separate reviewer subagent** to audit the changes before declaring the phase complete.

The reviewer must independently inspect the diff, surrounding code, original requirements, and tests. Check for:

- Bugs, regressions, flawed assumptions, and integration issues.
- Security, error handling, edge cases, and violated invariants.
- Maintainability, unnecessary complexity, and inconsistent conventions.
- Tests that are missing, incorrect, superficial, or pass without testing the intended behavior.
- Whether tests would detect the actual bug or failure they claim to prevent.

Run relevant tests and verify their results. The reviewer must report concrete findings with evidence and severity. Fix valid findings and repeat review and testing until critical issues are resolved or explicitly documented as blockers. Never approve changes merely because tests pass.

### 4. Mandatory Final Review (BEFORE COMPLETION OR COMMIT)

After all phases are implemented, spawn a fresh reviewer subagent for a full-task review. Re-evaluate the complete diff against the original requirements and system architecture, including cross-phase interactions, regressions, test coverage, and whether the tests genuinely prove correctness.

Run the appropriate test suite, inspect the final diff, and report verified results separately from unverified assumptions. Do not declare the task complete, commit, or open a PR until this review passes. If issues are found, fix them and repeat the review.

### 5. Git Discipline

Commits and PRs are permitted. Create focused commits and a PR when appropriate, but only after the mandatory reviews pass. Never commit known unresolved critical issues, claim tests passed without executing them, or bypass review gates to save time.

**Non-negotiable rule:** Context-gathering subagents must run before every change; an independent review subagent must run after every atomic phase and once again at the end. If subagents or required verification are unavailable, stop and report the blocker instead of pretending the workflow was completed.

---

## Live work state — the coordination board

**Who writes here:** the orchestrator agent and every subagent working in this tree.
Update your row in place when you start, and again when you finish; a row that says
"verified" must name its evidence (command + result). Rows for finished work move to
the archive at the bottom. Keep prose out — this is a register, not a report.

### Environment (this development sandbox)

- Repo: `/home/z/my-project/paa`, branch `wave-7f` @ ebbb439 (pushed to
  `origin/main` — the Wave 7 close) plus this campaign's commits. The sandbox
  was recycled before W8 opened: the fresh venv is `.venv/` **inside the repo**
  (Python 3.12.16), installed `pip install -e ".[dev]"` — the exact shape
  CI's `gate (dev)` and `status` jobs pin (dev now carries `paa[tui]`, so the
  shape is core+api+s3+attest+tui+tooling; pytest 9.2.0 / mypy 2.4.1 /
  ruff 0.17.6, the same versions CI resolves). No service containers this
  session: `scripts/test-env.sh` was not brought up, so server-gated suites
  skip by design and CI's gate job (postgres+seaweedfs+rustfs) is the
  authority for those paths.
- Full gate at the C7.3 close (sandbox): `python -m pytest tests/` → 4,769
  collected = 4,525 passed / 243 env-gated skipped / 1 xfailed, 0 failures,
  exit 0 (+50 recorder tests over the C7.2 close); `mypy src/paa` clean
  (197 files); `ruff check src tests` clean. The host's earlier pre-existing
  failures lived only in its then-untracked `tests/test_git_host_exec_gate.py`
  — that file is tracked and green in the merged tree. Note: pytest 9's double
  `-q` (addopts + a manual `-q`) suppresses the summary line — count with a
  single `-q` or read the dots.
- Full gate after the skip sweep (W7-IMPL-04, commit 8710623): 4,934
  collected = **4,929 passed / 4 skipped / 1 xfailed, 0 failures** with
  every environment gate open (`scripts/test-env.sh up` + the extras).
  The 4 remaining skips are either-or gates that *must* skip on this host
  precisely because the thing they test-for is installed: the no-driver
  S3 test (boto3 present), the shell start-failure test (a shell is
  present), and the two hash-embedder fallback tests
  (sentence-transformers is installed — its selection test runs instead).
  `mypy src/paa` 197 files clean (numpy ≥2.3 stubs; the old `type-arg`
  ignores were removed with the pre-defaults stubs); `ruff check src
  tests` clean; conventions incl. docs lock 34 passed.
- Full gate at the Wave 8 triple merge (wave-7f, C8.1+C8.2+C8.4 integrated):
  5,343 collected = **5,098 passed / 245 env-gated skipped / 1 xfailed, 0
  failures** (no service containers this session — by-design skips; CI's gate
  job is the authority there). `mypy src` 0 issues in **231** source files
  (improve/compose/chat-extensions add 10 over the W8-CI2 close); `ruff check
  src tests` clean; conventions incl. the docs lock green — after re-anchoring
  **59** `cli/main.py` citations the C8.1/C8.4 import+registration inserts had
  drifted (the mechanical insert-shift mapping; `docs/agent-ops/` left to rot
  by the lock's own scope rule). The three features were built in parallel
  worktrees (`paa-c81/improve`, `paa-c82/hierarchy`, `paa-c84/chatext`) and
  union-merged: the six-pipeline registry pins, both conformance runners, the
  TYPE_CHECKING classifications, and the two CLI run doors were the conflict
  surface, all resolved as unions.
- Full gate at the Wave 7 close (wave-7f): 5,242 collected = **5,237 passed /
  4 skipped / 1 xfailed, 0 failures, exit 0** with every environment gate open
  (`scripts/test-env.sh up` + the extras; 2026-11-26). The 4 skips are the
  same either-or gates as the skip-sweep run. `mypy src/paa` 0 issues in 221
  source files (evals/thread/acp add 24 over the C7.3 close); `ruff check src
  tests` clean; conventions incl. the docs lock 34 passed — after re-anchoring
  42 `cli/main.py` citations the Wave 7 union had drifted onto blank lines
  (d366655; the union of three registrations shifted the file; re-anchored
  against the integration base 1602467 with the feature tips as fallback
  parents).
- venv: repo-local `.venv/` (this session; see the repo bullet above — same
  layout and shape as CI). Earlier sessions used `/home/z/.venv` outside the
  project dir; after a sandbox recycle reinstall with `pip install -e ".[dev]"`
  from the repo root (dev carries `tui` since W8-CI). Host repo: `.venv/`
  inside the repo (Python 3.12.3), gate commands
  `PYTHONPATH=src .venv/bin/python -m …`. 2 CPU cores; run at most ~3 parallel
  jobs.
- The skip sweep (W7-IMPL-04): `scripts/test-env.sh up` brings up Redis
  (redislite), PostgreSQL 16 + pg_trgm + pgvector (pgserver + compiled
  contrib), and two distinct S3 implementations (SeaweedFS + Garage;
  MinIO's OSS server is archived, 410 Gone). With
  `PAA_TEST_POSTGRES_DSN`/`PAA_TEST_S3_*` set from its output plus the
  `qdrant-client`/`kuzu`/`psycopg[_pool]`/`boto3`/`sentence-transformers`
  extras, every server-gated suite runs; the remaining skips are by
  design (either-or driver gates, platform gates, live-model key gates).
- Tooling present: pytest 9.x (9.0.2 sandbox / 9.2.0 host), ruff 0.17.x (above
  the `>=0.8` floor — see the ruff note in the verified-facts register), mypy
  (strict, clean).

### Archived campaign: W6-LOOPS — SPEC 2 Wave 6: loops everywhere (2026-11-24, closed)

Owner ask: work everything the satisfaction report left "not started / partial" in the
P and C sections, in wave order. Scope: Wave 6 (C6.1-C6.6 = P1.3, P1.5, P2, P4-lite,
conformance v0), then Wave 7 as capacity allows. C5.1/G1 stays operator-blocked; P5
stays gated on a first outside author (the spec's own rule).

**WAVE 6 CLOSED (2026-11-24):** all six rows done, G-LOOPS gate met — three
pipelines on one facade, conformance + chaos + overlay green, protocol still draft
(freezing waits for P3's RPC run, per the spec's own rule). The W6-CLOSE row below
stays this tree's evidence; the successor campaign is W7-EVIDENCE.

| Task ID | Agent | Assignment | Status | Evidence / notes |
|---|---|---|---|---|
| W6-C6.2 | orchestrator | CallJournal + PIPELINE_RUN_*/CALL_* events per ADR-0046/0050 | **done** | commit 1e7906d; W6-IMPL-01 report; 23 tests, replay property green |
| W6-C6.1 | orchestrator | coder.tool-loop on the facade; use_skill -> existing ToolJournal | **done** | this commit; W6-IMPL-02 report; tests/coder 484 passed unchanged, S1 dry-run all checks held (after uninstalling sandbox-injected ddtrace — see report §13) |
| W6-C6.3 | orchestrator | replay/divergence logic + kill -9 chaos for chat and coder | **done** | commit f535071; ReplayView+ServedToolResults on the facade; coder chaos matrix (5 boundaries: sweep twice → resume, zero duplicate effects) + chat redelivery chaos (3 tests); 22 tests |
| W6-C6.4 | orchestrator | slots + overlay loader, tighten-only at load | **done** | commit b87234e; slots.py declarations + overlay.py loader (every widening rejected at load by name); chat context-provider slot; the behaviour proof as a test; 31 tests |
| W6-C6.5 | orchestrator | registry v0 + paa pipeline list/inspect/run/trace | **done** | commit b5390ea; builtin+local sources, mandate pipeline scope (Scope.pipelines, golden payload preserved), selection explicit→scope→default; CLI list/inspect/run/trace driven live in tests; 29 tests |
| W6-C6.6 | orchestrator | conformance v0 for all registered pipelines | **done** | commit 215cb25 (landed ccf0793), then remediated here; six-check standard in each pipeline's own vocabulary; all three GREEN. Applied here from patch `0006` and then remediated: the builtin no-secrets pass and the coder unknown-outcome fake pass became honest, pinned n/a rows; no-secrets now proves hash attestation; cancel, budget, double-run and the RED-row path are genuinely asserted. Focused gate: `tests/pipelines/test_conformance.py` 5 passed; `tests/pipelines tests/cli tests/chat tests/coder` 869 passed / 1 skipped; a new runnable builtin without a runner now renders RED by name instead of being silently skipped |
| W6-CLOSE | orchestrator | G-LOOPS gate evidence + truth pass + deliverables | **done** | applied here from patch `0007` (landed ccf0793); docs truth pass repointed this tree's longer `ledger/replay.py` and the local CLI anchors to their real lines, docs-reference lock 5 passed + conventions 34 passed; final full suite 4,439 passed / 240 skipped / 1 xfailed / 3 pre-existing failures outside W6; protocol still draft (freezing waits for P3's RPC run — the spec's own rule) |

### Archived campaign: W7-EVIDENCE — SPEC 2 Wave 7: the moat goes external (2026-11-24, closed 2026-11-25)

Owner ask: continue down the satisfaction report's "not started / partial" list in
wave order; Wave 7 rows as capacity allows, C7.1 first (it is also the pipeline
spec's P1.4: the fourth pipeline the rule of three was waiting on).

**WAVE 7 CLOSED (2026-11-25):** all six rows done — observe (C7.1), attest
(C7.2), record (C7.3) landed through the hardened main line; evals (C7.4),
thread (C7.5) and ACP v1 (C7.6) landed on feature branches and integrated on
`wave-7f` (three merges, registration unions in `cli/main.py`, 42 drifted
citations re-anchored). Full gate at the close: 5,242 collected = 5,237
passed / 4 by-design skips / 1 xfail / 0 failures, every environment gate
open; mypy 221 files clean; ruff clean. The successor movable item is P3
(the RPC host/protocol run) — everything else left is operator-gated or
author-gated by the spec's own rules.

| Task ID | Agent | Assignment | Status | Evidence / notes |
|---|---|---|---|---|
| W7-C7.1 | orchestrator | `paa observe` — H3.2 observer mode, T1 evidence tier, pipeline #4 per P1.4: run any CLI in a workspace, record manifest diff + git changes + transcript, classify state after a crash; `paa-observe-bundle/1` (two-phase atomic write, `resume="none"`, `evidence_tier="T1"`), `builtin.observe` on the facade with an empty capability ask, one shared driver for `paa observe` and `paa pipeline run builtin.observe`, `[evidence] declares_tier` on manifests | **done** | landed ccf0793 (patch `0020`) with local hardening: reserved `--env` keys refused, argv secrets kept out of failed-journal reasons, missing git recorded not fatal, canonical-hash/atomic-write claims stated honestly; 63 observe tests; `PYTHONPATH=src .venv/bin/python -m pytest tests/ -q -o addopts=` → 4,506 passed / 240 skipped / 1 xfailed / **3 pre-existing failures** in the untracked `tests/test_git_host_exec_gate.py`; `mypy src` 0 issues / 186 files; `ruff check src tests` only those same 3 pre-existing findings; docs lock 5 passed; patch-author SHAs, counts and `/home/z/...` paths are not adopted |
| W7-C7.2 | orchestrator | attested change bundle + `paa verify` (H3.1) | **done** | landed f5555b6 (patch `0021`) with local hardening: algorithm-aware signature dispatch + signed blob index, hostile-manifest verdicts, bounded decompression; git option injection closed via `rev-parse --end-of-options`, false-clean git refused, key mode checked on load, T1/`resume="none"` enforced, portable secrets refused, PR body verified before rendering with inert code-span escaping and terminal-safe refusals; `paa[attest]`/`requires_attest` added. 92 attest tests; focused `tests/attest` + conventions 126 passed; final full suite 4,599 passed / 239 skipped / 1 xfailed / **3 pre-existing failures** in untracked `tests/test_git_host_exec_gate.py`; `mypy src` 0 issues / 191 files; `ruff check src tests` only those same 3 findings; status lock matches; author-host claims and untrusted `.bin` evidence not adopted/committed |
| W7-C7.3 | orchestrator | `paa record` — the harness-neutral flight recorder | **done** | landed by this merge (63c5a0e, built on the pre-hardening attest); W7-IMPL-03 report; `paa-record/1`: append-only, crash-safe hash-chained NDJSON (digest covers schema/seq/at/kind/data/prev, genesis seed; two-phase summary, torn-tail reported not dropped) + the sequential-writer handoff (core ↔ proxy append under flock, tail digest verified, one chain no splice); `src/paa/recorder/{record,tools,proxy,core}.py` + `paa record run\|proxy\|verify`; the MCP policy proxy over the shared framing kit (tool_call → shape → run's recorded policy → execute sandbox/jailed → tool_result/policy_denied; unattended ask = denied_unattended with the raw decision kept; deny wins; no token, reasoning stated); the harness under a real SandboxSession with live stdout relay; mediated vs observed never collapsed (the honesty block states it where the code enforces it); attest builder +2 additive validated params (extra_evidence, observed_command_rows backed by bundle blobs) — closes the C7.2 command-row-extractor finding; done-when met as a real chain: fake harness (real child) → real proxy subprocess over real stdio → real chain → `verify_bundle` green; chaos: self-SIGKILL → signalled, overrun → timed_out, both finalised+verifying; non-cooperating harness → T1 floor, zero proxy events; 50 recorder tests, full gate 4,769 collected = 4,525/243/1x/0 exit 0 (+50 exactly; one textual-gated TUI skip un-skipped), mypy 197 files, ruff clean; docs/interfaces/record-format.md + attest-spec additive section + 95 citation files re-anchored |
| W7-C7.4 | orchestrator | cross-harness eval harness | **done** | landed a230b30 (merged a0d666f onto wave-7f); W7-IMPL-05 report; `src/paa/evals/`: `tasks.py` (EvalTask + TASK_SCHEMA; five repository tasks under `evals/tasks/`, incl. two designed discriminators — palindrome-guard trips a naive direct-write harness on its forbidden path, fizzbuzz-off-by-one separates harnesses that run the tests they were given from ones that ship a plausible wrong fix), `harnesses.py` (adapter registry; two scripted fakes — recorded-fake mediated through the `paa record proxy` MCP loop, scripted-direct unmediated), `runner.py` (matrix inside the evidence boundary, journalled, canonical REPORT_SCHEMA), `scoring.py` (four deterministic checks: tests-pass, bundle-verifies — honestly N/A for plain adapters, never silently passed, diff-matches-intent, no-forbidden-effects); `paa eval run\|tasks` CLI; no new EventType — the matrix journals PIPELINE_RUN_COMPLETED on its own lineage (the ADR-0046 pricing rule); 62 tests incl. the 2×5 matrix end-to-end with a real per-harness distinction in the report; **demo artifact delivered** (`paa-eval-demo/`: the live 2×5 matrix report — recorded-fake 5/5 mediated with verifying bundles, scripted-direct 2/5 tripping all three designed discriminators) |
| W7-C7.5 | orchestrator | `paa thread` | **done** | landed eea1b4f (merged 2e8fa7e onto wave-7f); W7-IMPL-06 report; `src/paa/thread/`: `record.py` (ThreadRecord/Segment, durable JSON under PAA_HOME/threads, append-only invariants enforced by the methods), `journal.py` (four new event types — THREAD_CREATED, THREAD_SEGMENT_STARTED carrying packet_sha256, THREAD_SEGMENT_ENDED, THREAD_HARNESS_SWAPPED; replay folds without requeue, the boot sweep taught), `handoff.py` (content-addressed HandoffPacket: mandate + gathered memory + ledger tail + git summary — "context intact" is a checkable fact), `harness.py` (registry + fake-alpha/fake-beta), `engine.py`; `paa thread new\|run\|swap\|show\|list`; the swap acceptance: start on fake-alpha, swap to fake-beta mid-task — same thread id and mandate, the second packet carries the first segment's effects, the packet-hash chain intact, the ledger ordered and unbroken; 99 tests + ledger 161 green with the new vocabulary; **demo artifact delivered** (`paa-thread-demo/`: the live swap — new/run/swap/show/ledger through the real CLI, both handoff packets, the ledger chain ordered with `chain integrity: ok`) |
| W7-C7.6 | orchestrator | ACP v1 | **done** | landed e4290c4 (merged fa626aa onto wave-7f); W7-IMPL-07 report + ADR-0053; `src/paa/acp/` on the shared framing kit (one framing codebase, three fronts — MCP server, MCP client, ACP): `protocol.py` (initialize with pinned version, session/new, session/prompt, streaming session/update, session/cancel, fs/read_text_file, JSON-RPC error envelopes), `server.py` (T1 stdio front: the editor spawns PAA; prompts drive the chat service; cancel wired to the loop's real cancellation — a stop, not an ACK), `client.py` (T2 front: external ACP agent behind a SandboxSession, its permission asks mediated through the deterministic tool policy — the recorder-proxy pattern applied to ACP; unattended ask = recorded refusal), `fs.py` (scoped by the coder protected-path rules); `paa acp serve` / `paa acp client --`; 90 tests incl. the streaming acceptance over real subprocess pipes and the cancel acceptance (ACKed, chunks actually stop, session stays usable, children reaped); **demo artifact delivered** (`paa-acp-demo/`: the wire transcript of a live initialize → session/new → streaming turn → mid-flight cancel → usable-after-cancel → EOF session, 13 frames) |
| W7-CLOSE | orchestrator | integrate C7.4/C7.5/C7.6, full gate, deliverables | **done** | wave-7f integration: evals/thread/acp each on its own branch over the H-1 fix (0662027), merged in sequence (a0d666f, 2e8fa7e, fa626aa — the cli/main.py registration union resolved by hand, the recovery table row kept on thread's shifted lines, repo-map re-anchored); 42 drifted `cli/main.py` citations re-anchored onto the union (d366655, docs lock green); full gate 5,242 collected = **5,237 passed / 4 skipped / 1 xfailed / 0 failures** with every environment gate open; mypy 221 files / 0 issues; ruff clean; conventions 34; demo artifacts regenerated for all six rows (`paa-observe-demo-bundle.json`, `paa-attest-demo-bundle/`, `paa-record-demo/`, `paa-eval-demo/`, `paa-thread-demo/`, `paa-acp-demo/`); deliverables regenerated as `paa-wave7-complete.*` + the 6th-edition report |

### Archived campaign: W8-DEEPEN — SPEC 2 Wave 8: the looping OS (2026-11-26, closed 2026-11-27)

Owner ask: "start wave 7 and setup CI". Wave 7 is closed and pushed (ebbb439),
so the next movable wave per the SPEC 2 wave map (`docs/plans/2026-11-21-next-spec-2.md` §2)
is **Wave 8 — deepen: the looping OS**; that is what opened. The CI half was
literal and urgent: the gate had been red on **every push since C4.4's TUI
landed** (all 24 recorded runs failed) — `textual` sat in no CI install shape.

| Task ID | Agent | Assignment | Status | Evidence / notes |
|---|---|---|---|---|
| W8-CI | orchestrator | repair the CI gate — red on every push since the chat TUI (C4.4) landed | **done** | root cause, one dependency in the wrong place: `textual` (the `[tui]` extra) was in NO install shape. `gate (dev)`/`gate (all)` ran `mypy src` without it → 19 `Any`-subclass/no-any-return artefacts in 3 files (`tui/chat/widgets.py`, `screens.py`, `app.py`; measured identical in the dev and minimal CI jobs and reproduced locally: `Found 19 errors in 3 files (checked 221 source files)`); `status (--check)` died at `pytest --collect-only` importing `tests/tui/test_chat_app.py` (a collection ERROR, worse than a skip — it aborts the suite). Fix: (1) `paa[tui]` added to `dev` and `tui` to `all` (`pyproject.toml`, the same documented argument as api/s3/attest — the documented dev install must be able to run what the CLI advertises); (2) `tests/tui/conftest.py` — `pytest.importorskip("textual", reason="needs the tui extra")`, the `tests/api` pattern, so the minimal shape SKIPS the directory instead of erroring; (3) the minimal shape's mypy exclude widened to `src/paa/(api|tui)` with both measurements written into the step (`mypy src --exclude src/paa/(api|tui)` → Success, 202 files — measured, on a textual-less install); (4) README status block regenerated for the shape the status job pins: 221 files / 91,230 lines / **5,203 collected** (`scripts/status.py --write`, `--check` green); (5) the stale extras lists updated in `ci.yml`'s header/status comments and `status.py`'s docstring. Local verification: ruff 0; `mypy src` 221 files 0 issues; full suite `pytest tests/ -o addopts="--strict-markers --strict-config" -q` → **4,958 passed / 245 env-gated skipped / 1 xfailed / 0 failures** (no service containers this session — by-design skips; CI's gate job runs postgres+seaweedfs+rustfs and is the authority there); minimal simulation (textual uninstalled): collection exits 0 with tests/tui skipped, mypy-with-exclude Success 202 files, mypy-without-exclude reproduces the 19/3 measurement |
| W8-CI2 | orchestrator | the first green runner run: fix the latent failures the gate had never reached (pytest had never completed on a runner — every one of the 24 recorded runs failed before it) | **done** | four failures, each reproduced locally before the fix. (1) `tests/thread/test_cli.py` imported `click.testing` — click is not core (typer 0.29 vendors its own); the minimal shape died at collection. Fixed: import `Result` from `typer.testing`, which re-exports it. (2) `tests/deployment/test_fetch_images.py` (≈21 tests + collateral, day-one: the Nov 21 run failed the same set) — root cause MEASURED on the runner via the fix's own diagnostic: `/usr/bin/timeout` on ubuntu-26.04 is **uutils coreutils 0.10.0** (the Rust rewrite Ubuntu now ships), so the script's `grep -q "GNU coreutils"` gate refused a compatible implementation and no GNU timeout exists anywhere on that image. Fixed both sides: the script's gate (`require_coreutils_timeout`) now accepts a banner naming GNU *or* uutils coreutils — both speak `timeout <seconds> <command>`, the only form the script uses; busybox names neither banner and stays refused — and the test provisions a compatible timeout by banner (`_coreutils_timeout()`: walk PATH then /usr/bin, lazily, failure names every candidate's banner), with the not-found and wrong-implementation messages and the two docs (ADR-0030, deployment/docker.md) updated to the measured truth. (3) recorder/evals (4 tests: record front-door artifact set, both done-when bundles, the 2×5 acceptance matrix) need real Ed25519 — reproduced by uninstalling cryptography: exactly those 4 fail; fixed with `pytest.importorskip("cryptography", ...)` at the four sites, the tests/attest line — minimal skips labelled, dev/all run them. (4) conventions: `OPTIONAL_EXTRA_MODULES` gained the four `paa.tui.chat` modules (checkable — the blocker test proves they need textual), and the A-classified cycle proof defers extra-blocked controls by name (`paa.tui.chat.screens` on minimal; `paa.storage.coldlake.cas` still proven); plus `tests/coder/test_agent.py`'s two `--help` assertions read the help through `_plain()` (ANSI stripped, line breaks removed) — measured on the runner: `--max-steps` is split by Rich's styled rendering there and intact on every terminal-less host, and the assertions are about content. Local: minimal shape — coder/recorder/evals/deployment/conventions 284 passed + 4 attest skips, 0 failures; dev shape — ruff 0, mypy 221 files 0 issues, the affected suites 821 passed / 1 skip, the complement 4,137 passed / 244 env-gated skips / 1 xfail, 0 failures; README status lock green (5,203 collected, unchanged). **CLOSED GREEN: run 45786883743 on 83ed958 — the repository's first fully green CI run** (all 24 prior runs failed): gate (minimal) 4,746 passed / 257 labelled skips / 1 xfail; gate (dev) 5,001 passed / 244 env-gated skips / 1 xfail; gate (all) **5,237 passed / 8 skips / 1 xfail** with postgres+seaweedfs+rustfs live and every extra installed — the same 5,237 as the Wave 7 close gate; status (--check) and embed image separation green |
| W8-C8.1 | orchestrator | self-improvement loop, guarded: reflection → patch proposal → deterministic validation → human gate → commit, as a pipeline; every improvement an attested change (C7.2) | **done** | `improve.reflection-loop` registered (slots `_improve_declaration`; capability ask `commit` only — the determinism rule made machine-readable), the five stages in `src/paa/improve/` (pipeline/proposal/validation/gate/gitcommit, +2,036 lines): reflection over the real ledger (the Weekly Reflection Engine's own `analyze_window`), a deterministic proposal (full-content artifact, `prior_sha256` jail, managed fence — never auto-applied), validation as real gate-command subprocesses on a scratch copy (exit codes only, argv never journalled — fingerprinted, digests-only), a human gate with refusal-by-default, and a commit that lands only as a C7.2 attested bundle behind the host-git integrity gate. Both refusal paths first-class: red gate → `stopped: validation_failed`, tree untouched; refusing human → proposal recorded, nothing applied (`tests/improve/test_cycle.py::TestTheRefusalPaths`); happy path → `verify_bundle` ok, approvals==1 (`TestTheHappyPath`); crash discipline: re-driven completed cycle lands nothing twice (`TestTheReplay`). Both doors CLI-tested (12 tests: `paa improve` exit 0/1/2 paths + `--json`; `paa pipeline run improve.reflection-loop` + `--gate` refused for foreign pipelines); door parity decided and pinned: the shared runner never grows `--insecure-fallback-signing` (test-pinned + `docs/modules/improve.md`). Six-check `improve_conformance` (the first mutating-call pipeline: real D-6), registry pins `TestTheImprovementLoop`, 5→6 declaration pin. Honesty line in module docstring + docs: prototypical few-shot classification ≠ gradient meta-learning (ADR-0017). No new EventType (rides `PIPELINE_CALL_*`/`PIPELINE_RUN_*`). Gates on the branch: full chunked suite 5,039 passed / 245 env-gated skips / 1 xfail / 0 failures; ruff 0; mypy 228 files 0 issues |
| W8-C8.2 | orchestrator | hierarchy and delegation: `ctx.sub_run` composition with budget halving and recursion guard — manager loops delegating to worker loops | **done** | layer 4 implemented: `src/paa/pipelines/compose.py` (835 lines — `halve_budget`, `SubRunComposer`, `ManagerPipeline`, `CompositionHost`, `run_composition`); `BuiltinPipelineFacade.sub_run` now grant-checks then delegates (the ungranted refusal stays the default for workers); `builtin.manager` registered (slots; sub_run ask names the four workers). Halving semantics pinned per key: tokens/wall_s/calls `max(1, remaining//2)` with wallet remainder; `max_sub_runs` `remaining//2` NO floor (0 = may-not-compose); None stays None; `MAX_COMPOSITION_DEPTH=8`; cycle = name-stack membership (A→B→A refuses). Child grants from the child's own declaration only (anti-laundering pinned). Child isolation: failed/crashed child → failed child result, parent continues; control errors propagate; cancel journalled then re-raised. Manager demo over two different workers (`TestTheManagerDemo`) + end-to-end CLI (`TestManagerRun`: two delegations, correlation traces PIPELINE_RUN_STARTED→sub_run rows→COMPLETED). Replay: recorded child results served with zero re-execution, no new rows; changed call → `PipelineDivergedError` + `PIPELINE_RUN_DIVERGED`. `manager_conformance` six checks (the first REAL D-6: sub_run is a mutating journaled call; unknown-outcome refusal before any worker executes), GREEN 5→6. No new EventType (sub_run rides `PIPELINE_CALL_*`, method="sub_run"). 24-test `tests/pipelines/test_compose.py` + 4 CLI tests. Gates on the branch: chunked suite 4,986 passed / 245 env-gated skips / 1 xfail / 0 failures; ruff 0; mypy 222 files 0 issues |
| W8-C8.3 | orchestrator | coder re-scoped as the preliminary loop — the fallback coding loop when no external harness is attached; kept deliberately small; external harnesses arrive via `paa record`/`paa thread` instead | **done** | the re-scope is machine-readable where selection vocabulary lives: `coder.tool-loop`'s declaration re-scoped (`pipelines/slots.py::_coder_declaration` — description + docstring carry the position, surfaced by `paa pipeline inspect coder.tool-loop`); `CODING_FALLBACK_PIPELINE = "coder.tool-loop"` exported beside `DEFAULT_PIPELINE` in `pipelines/registry.py` (the registry is the one place that answers "which pipelines exist"; C8.4's chat extension surface and C8.5's Loop Console are the consuming rows); 4 pins in `tests/pipelines/test_registry.py::TestTheCodingFallback` — the fallback is registered, explicitly selectable, NOT the task default (two defaults, two meanings), and the declaration carries the position. Docs: `AGENTS.md` coder section, `docs/modules/coder.md` (Position paragraph), `docs/coder-agent.md` (Position in the OS section). Gates: focused `tests/pipelines` + `tests/conventions` 208 passed (docs lock green — no re-anchoring needed: the in-scope `AGENTS.md` citations stop at line 217, the coder-section insert starts at 342); ruff 0; mypy 221 files 0 issues; full suite as in W8-CI (the 4 new tests are in the 5,203 collected / 4,958 passed) |
| W8-C8.4 | orchestrator | extension surface for chat: Claw-Hub/OpenClaw discovery → install → activate through the 5-gate installer, personal skills dir, MCP tools as chat capabilities, ACP as a chat transport | **done** | the real flow at last: `src/paa/chat/extensions.py` (986 lines — `ChatExtensionSurface`): discover (ClawHubAdapter over a hub dir or the personal skills dir `PAA_HOME/skills/personal`) → install through ALL five gates (the smoke gate now really stages+runs the entrypoint under sandbox, JSON on stdout required; refusal = zero trace by construction) → staged bytes at `skills/installed/<name>/<ver>` → activation rebuilds the contract FROM THE STAGED BYTES (what the smoke gate ran is what a chat turn executes) → surfaced as chat capabilities via the `@use-skill` directive in `chat/loop.py` (≤3 rounds/turn, `[capabilities]`/`[skill results]` prompt sections, default prompts byte-identical). E2E pinned: `tests/chat/test_extensions.py::test_the_flow` (all 5 gates asserted in order, registry row, catalogue, scripted @use-skill turn, TOOL_CALL_PROPOSED→COMPLETED); zero-trace refusal pinned (AST-blocked: no marketplace row, no staged files, no registry row, exactly one `EXTENSION_REFUSED`); MCP tools as capabilities (`install_mcp_server` → catalogue → real stdio server child dispatched from a turn); ACP as a chat transport (production `serve()` loop: session/prompt streams the skill result, prompts carry the sections — `tests/acp/test_extensions_transport.py`). `paa skills install/list/activate/mcp` CLI (10 tests; the tests caught and fixed a real defect: activate-by-name after `--no-activate` now reads the staged bytes). `CODING_FALLBACK_PIPELINE` consumed at `chat/extensions.py::resolve_coding_pipeline` (activation of a `kind: coding` skill with no harness attached selects it by name; recorded in the `EXTENSION_ACTIVATED` payload — golden-pinned). Three new events (`EXTENSION_INSTALLED/REFUSED/ACTIVATED`) with v=1 goldens, ADR-0008 discriminators, replay-total property (Hypothesis, 60 examples), `_NO_PHASE_CHANGE`, `SKIPPED_EXTENSION_LINEAGE` recovery exemption — shape-checked, not event-sniffing (converse pinned). Delimitations stated in-test: stdio process boundary (`serve_child.py`) and streaming chunk shape not covered. Gates on the branch: chunked suite 4,989 passed / 245 env-gated skips / 1 xfail / 0 failures; ruff 0; mypy 223 files 0 issues |
| W8-CI3 | orchestrator | the first runner round over the W8 triple merge: fix the three failures the dev branch gates had never seen | **done** | run 45840762470 on bf2318d, all in the C8.1 surface, each reproduced locally before the fix. (1) the two `--help` assertions in `tests/cli/test_improve_cli.py` read `--gate` from raw stdout — the W8-CI2 Rich measurement applies to `--gate` exactly as it did to `--max-steps` (the runner's styled help wraps the token mid-token; intact on every terminal-less host). Fixed with the same `_plain()` helper (ANSI stripped, line breaks removed, the `tests/coder/test_agent.py` precedent) — assertions are about content. (2) `tests/improve/test_cycle.py::TestTheHappyPath` asserted `verify_bundle(...).ok` — but a bundle signed with `--insecure-fallback-signing` is REFUSED by verify on every host by design (`format.py::verify_attestation_signature`: the symmetric fallback is never trusted by default). On the minimal shape the cycle commits via the fallback, so the test now asserts the honest per-shape verdict: full green on a host with real Ed25519; on a fallback-only host the signature check fails naming `hmac-dev-fallback` while every other check passes — reproduced locally by uninstalling cryptography: exactly that test failed, fixed, both shapes green (minimal 19 passed + 1 labelled skip; dev 76 passed). **CLOSED GREEN: run 45842646553 on 3edd179** — all five jobs: gate (minimal) / gate (dev) / gate (all) / status (--check) / embed image separation; gate (all) with postgres+seaweedfs+rustfs live and every extra installed: **5,377 passed / 8 skips / 1 xfail** (+140 over the Wave 7 close), mypy 231 files 0 issues |
| W8-C8.5 | orchestrator | Loop Console: TUI extension of `paa chat` for loop management — registry panel (trust, tier, version), install/activate/fork, conformance run, evidence-tier badges | **done** | `src/paa/tui/chat/console.py` (633 lines — the console service) on the C4.4 kit (screens.py +598: the console screen; app.py: the ctrl+b door and `start_on_console`; wiring). The registry panel reads `PipelineRegistry.list()` row-for-row — trust/version cells plus bracketed evidence-tier badges (`[T1]` on builtin.observe, `[in-process]` on builtin.chat; `TestTheRegistryPanel`). Install/activate drive the C8.4 `ChatExtensionSurface` WHOLE — the five gates named in the status line, staged bytes, `EXTENSION_INSTALLED/ACTIVATED` ledger rows (`TestTheInstallThroughTheGates`), and the AST-gate refusal renders with zero trace pinned (`✗ refused greeter at the ast_scan gate … nothing landed`); activate flips a staged row live (`TestTheActivate`). Fork pinned to the two mechanisms the kit already offers: a skill fork copies the gated staged bytes into `PAA_HOME/skills/personal/<name>` (registers nothing; re-installing the copy goes back through all five gates), a pipeline fork seeds a personal declaration overlay via `render_fork_overlay` that the tighten-only loader (ADR-0052) loads unchanged — any widening is refused by the loader, not the console (`TestTheFork`, both modal outcomes, taken-name refused inside the still-open modal); a local-manifest fork was rejected deliberately (the manifest contract requires all four `[limits]` as concrete ints and five of six builtins are unbounded). The conformance run drives the six-check runner on a throwaway ledger and renders the standard's own `render_reports` — honest per-check verdicts, `ok` and `n/a` marks distinct, the operator's ledger proven unwritten (`TestTheConformanceRun`). The console is a view + thin dispatcher: no new authority, no new gate, no new event type. `paa chat --console` door CLI-tested (help names it; `--console --plain` exits 2 naming the CLI verbs the plain operator has). The pilot-enabling bug fixes the relaunch found and kept: `action_` bindings (C4.4's bare `show_*` names were dead keys), `RowKey.value` (textual 8.x `str()` is the object repr), first-candidate highlight in the install modal. `paa.tui.chat.console` TYPE_CHECKING classified C with the measured price (+223.2 ms — the extension surface + conformance stack + sandbox, imported inside action bodies). Module doc `docs/modules/console.md`, citations verified against the merged tree. Gates on the branch: tests/tui 87, tests/pipelines 203, tests/chat 123, tests/marketplace 56, tests/cli 120, tests/conventions 34 — 0 failures; ruff 0; mypy 232 files 0 issues |
| W8-C8.6 | orchestrator | `pipeline` package kind + local/git distribution (P5-lite): enable the kind in `package.py`; local dirs + pinned git URLs through the gates; signed `.paapkg` behind the same gates | **done** | the `pipeline` kind in `marketplace/package.py` (`_VALID_KINDS` + `PIPELINE_KIND`) with the v7 `ck_market_kind` migration over both schemas and hostile-manifest verdicts pinned (`tests/storage/test_marketplace_kind_migration.py`). Two events (`PIPELINE_PACKAGE_INSTALLED`/`REFUSED`, `core/types.py:419`) with v=1 goldens, replay/recovery coverage, and `SKIPPED_PIPELINE_PACKAGE_LINEAGE` — the N2.3 exemption's fifth mirror, so a recorded install or refusal never re-queues as resumable task work (`tests/ledger/test_pipeline_package_events.py`). The loading contract, pinned: **installed code is never imported into this process** — the gates clear bytes, and the bytes execute as a sandboxed subprocess speaking the `--paa-args` JSON-in / JSON-on-stdout contract (`SandboxedPipelineRunner`, `pipelines/sandboxed.py`; the honest gap — an installed pipeline cannot call `ctx.think` in v1 because a subprocess holds no facade tokens — is stated in the review artifact and the docs as exactly the spec's P5/P3 boundary). The install flow (`pipelines/packages.py`, 1,206 lines): one install action, one fresh lineage, six stages — source (local dir / `git+<scheme>://` at a pinned SHA-or-tag ref; a moving branch is refused after the clone, before any byte is read; host-side git with literal argv, an allowlisted environment and a wall budget) → declaration (the same closed `declaration_from_manifest` contract + the `pipeline.py` entrypoint it pins) → the five gates unmodified in their order (an unsigned local/git source installs only on the operator's explicit `--allow-unverified`) → conformance (the six checks against the gated bytes; a RED report refuses the install after the gates and unregisters their marketplace row — zero trace is the whole flow's) → capability review (the C8.1 human-gate pattern: a deterministic `CapabilityReview` artifact naming the declaration's ask, the conformance verdict and the content hash; an unwired seam refuses; a terminal walk-away refuses) → stage (`PAA_HOME/pipelines/installed/<name>/<version>/` — staging IS registration; the registry's `_scan_installed` makes the row appear, trust `installed`, runnable). A refusal at any stage leaves zero trace by construction. `install_pipeline_package` is the one-call door shared by the CLI and the tests; `paa pipeline install <dir|git-url> [--ref <sha|tag>] [--allow-unverified]` CLI-tested (exit 0 installed / 1 refused with the stage named / 2 usage — the skills door's contract). The hosted marketplace stays parked, per the spec's own gating sentence: `registry_client.py` untouched, no publish verb, pinned by test (`tests/pipelines/test_packages.py` never imports the registry client). The 85-citation insert-shift re-anchor over the C8.6 source inserts (`recovery.py`/`types.py`/`registry.py`/`conformance.py`/`cli/main.py`); `paa.pipelines.packages` TYPE_CHECKING classified E — the same `sandbox.base` + `ast_scanner` pair as `paa.chat.extensions`, measured -1.3 ms. Module doc `docs/modules/pipelines.md`, citations verified against the merged tree. Gates on the branch: tests/pipelines (test_packages 725 lines, test_sandboxed 477), tests/marketplace, tests/storage (the migration), tests/ledger (the goldens), tests/cli (the door) — 0 failures; ruff 0; mypy 233 files 0 issues |
| W8-CI4 | orchestrator | the runner round over the W8 double merge: fix the two failure classes the local gates had never seen | **done** | run 45881332685 on 313e85a, both classes root-caused from the runner's own output before the fix. (1) gate (all) only, 13 failures, one root cause: the v7 guard wrote `constraint_schema = '$schema$'` — but `_render_comparison` writes the schema in as a quoted *literal*, so the rendered guard read `constraint_schema = ''public''` (empty string, bare word, empty string) → `syntax error at or near "public"`, breaking the whole postgres migration chain (every test that migrates forward from an old shape; the runner's error dump names the doubled quotes itself). Fixed: the placeholder written unquoted, exactly as every other schema-scoped guard in the module writes it (`AND table_schema = $schema$` — the renderer supplies the quotes); the step's docstring corrected — it described the placeholder's value as `'$schema$'`, the very misreading that produced the bug; the fix mechanically re-verified: `_render_comparison` over the fixed guard yields `constraint_schema = 'public'`, no doubling. (2) all three shapes, 1 failure: `tests/cli/test_chat_console_cli.py::test_help_names_the_console_flag` read `--console`/`Loop Console`/`conformance` from raw stdout — the runner's Rich-styled help wraps tokens mid-token, the third instance of the class (`--max-steps` in W8-CI2, `--gate` in W8-CI3). Fixed with the `_plain()` helper (ANSI stripped, line breaks removed, the `tests/coder/test_agent.py` precedent) — the assertions are about content. Local: CLI + storage suites green, ruff 0, mypy 234 files 0 issues. Round 2 (run 45883044080 on 69e476d): all three gates green, status red — the fix round's own docstring lines had moved the src line count under the pinned block; the block regenerated (234 files / 101,049 lines / 5,461 collected). **CLOSED GREEN: run 45884698511 on 55077ba** — all five jobs; gate (all) with postgres+seaweedfs+rustfs live and every extra installed: **5,495 passed / 8 skips / 1 xfail** (+118 over the W8-CI3 green), mypy 234 files 0 issues |

**Wave 8 is closed — all eight rows landed** (W8-CI/CI2/CI3/CI4 + C8.1–C8.6; C8.5 and C8.6 delivered via the checkpoint-restart subagent pattern — the agents' runs were cut off twice by tool deadlines, and the work survived because every green unit was committed to its feature branch the moment it was green).

### Archived campaign: W9-MARKETPLACE — the deferred work (2026-11-27, closed 2026-11-27)

Owner ask: "make market place and everything that was differed [deferred]". That is an
explicit operator order, and it lifts the pipeline spec's P5 gating sentence ("do not
start it without a real author asking") — the operator is the author asking. Scope, per
the spec's own deferred list (`docs/plans/2026-11-21-next-spec-2.md` §8, the pipeline
spec's P3/P5/P6/P7, and the Known-left register above): the hosted marketplace
(P5 full — publish, signed distribution, compatibility checks, the loop economy), the
Wave 9 cooldown rows that are code (container-default verification + the effect
registry with ledger-driven rollback), P3 (the RPC step protocol, then freezing
`paa-pipeline/1`), P6's remaining adapter (`builtin.mcp-host`), P7 (the authoring kit),
and the multi-user/polish design docs — design-level only, because the spec's own rule
(no team features before single-user daily use is proven, G1's spirit) is an operator
gate this sandbox cannot lift. C5.1/G1 real-model S1 stays operator-blocked (no model
key). The parked-forever-unless-ordered items stay parked; only what the spec defers
*and* the order names moves.

Delivery pattern this wave: parallel feature worktrees, subagents, and — new this wave
— every feature branch is PUSHED to the remote the moment it lands green, after the
first attempt's branches were lost to a full sandbox recycle (the checkpoint-restart
lesson extended: green units must survive not only agent deadlines but host loss).

| Task ID | Agent | Assignment | Status | Evidence / notes |
|---|---|---|---|---|
| W9-C9.1 | subagent (feat/marketplace) | the hosted marketplace: `paa pipeline publish` (build + sign a `pipeline`-kind `.paapkg`, write it to a hub in the `LocalDirectoryRegistry` format, publisher identity pinned), hub-source install through the same six stages, manifest compatibility checks (`requires_paa`, protocol version) at publish and install, the loop-economy round trip (install → fork → publish → install) pinned end-to-end | **done** | five green commits on feat/marketplace (3ac512b publish core: manifest gains requires_paa + protocol as signed fields, the hub index round-trip through LocalDirectoryRegistry, publish_pipeline_package with the operator keypair under PAA_HOME/keys; 2e773bd the hub source hub:<name>[@<version>] --hub <dir|url> through the registry clients, the same seven stages; 56fb1f0 both doors `paa pipeline publish` + `install --hub`, exit 0/1/2; 3ba519d the loop economy pinned end-to-end — install → fork → publish → clean-home install → run, all gates green at every hop; 7f71752 docs + the C8.6 park marker retired for the three-truth replacement: a hub is untrusted, hub installs ride the same stages, an install never publishes back — pinned by a hub byte-snapshot). Branch gates: ruff 0; mypy 235 files 0 issues; pipelines+marketplace+cli+conventions 516 passed; ledger 199 passed |
| W9-C9.2 | subagent (feat/effects) | the effect registry + `paa rollback` (Wave 9's core): scope-owned effects with compensation paths declared as data, compensation executed as NEW separately authorized journaled actions (README §Temporal composability's own rule), irreversible effects reported not "fixed", crash-safe resume with no double compensation; plus the container-default line (auto already prefers docker — pin it with a test, document the measured truth) | **done** | nine green commits on feat/effects (2278b55 the effect registry v1 — EffectSpec riding journaled-call payloads, the ledger-derived index, no second store; 08619be the rollback walk pinned — newest-first, per-effect partial failure with re-run completing the remainder, double-rollback honest no-op, irreversible effects reported with their owner call; b826296 the kill-9-mid-rollback chaos drill — four walk boundaries, sweep twice, each effect settled exactly once; 0aad497 the `paa rollback` door exit 0/1/2 + --json; 2d00d34 ADR-0054; a56545a/b36bb93 the container-default pins + sandbox.md's measured truth; f5dac1a/ad80a5c/d8389b0 the module doc, the README implementation-status truth, the citation re-anchor). No new EventType — the effect rides the existing journal payloads. Branch gates: ruff 0; mypy 239 files 0 issues |
| W9-C9.3 | subagent (feat/rpc) | P3 the step protocol: `pipelines/rpc_host.py` (NDJSON JSON-RPC over the shared framing kit, child launch, token out-of-band, cancel with process-tree kill, stderr capture), the `paa_pipeline` child SDK (~200 lines), a shell language proof with no SDK, `paa pipeline test` (the conformance door), then freeze `paa-pipeline/1` to v1 (the three-users rule: in-process facade + sandboxed runner + RPC host) | **done** | six green commits on feat/rpc (c308557 the RPC host v1 — paa-pipeline/1 over the shared framing kit, token out-of-band via env, initialize/run/cancel/shutdown, grace-then-tree-kill, budget -32001 counted only after the token check, ungranted -32006, divergence -32004 on resume, stderr captured; the paa_pipeline child SDK as a SECOND package importing nothing from paa, its independent protocol pin held equal by test; 1f7ce3d ADR-0055 — the freeze under the three-users rule; fee6c87 the shell language proof, 115 lines of POSIX with no SDK driven through the host; 83f46e9 the budget-accounting fix; ea670bb `paa pipeline test` — the six-check door with the runner map moved to conformance.py as the ONE contract (NAMED_CONFORMANCE_RUNNERS), the console delegating lazily; b9f30d8 the docs). Branch gates: ruff 0; mypy 237 files 0 issues |
| W9-C9.4 | subagent (feat/mcphost) | P6's last adapter: `builtin.mcp-host` — an externally-driven MCP session landed on the pipeline platform (`resume="none"`, a declared tier, conformance); the six-tool server and its policy/journaling already exist (`src/paa/mcp/server.py`), the row is the pipeline-shaped registration | **done** | three green commits on feat/mcphost (1840393 the session wrapper; 478529b the registration — declaration, both doors, the six-check vocabulary; f8aebfc the position docs). The six-tool server's own policy/journaling UNTOUCHED; resume=none; mcp_host_conformance drives the UNCHANGED server in-process over its own framing. The integration added the map entry the auto-merge could not know about (builtin.mcp-host in NAMED_CONFORMANCE_RUNNERS). Branch gates: ruff 0; mypy 235 files 0 issues |
| W9-C9.5 | subagent (feat/authoring) | P7 the authoring kit: `paa pipeline new` scaffolds (manifest + entrypoint + test skeleton + conformance config), the worked tree-of-thought and TDD examples, the protocol compatibility matrix | **done** | five green commits on feat/authoring (fa56e47 the scaffold core — four rendered files with the parse-back lock (the written manifest must pass declaration_from_manifest before success is reported), named refusals, the proofs: the scaffold installs green through the five gates and all six checks; f0ab7e3 the `paa pipeline new` door; 97d80c8 the two worked examples — tdd-loop and tree-of-thought as complete pipelines; 0b4b265 docs/modules/authoring.md with the protocol compatibility matrix and the stated gaps; dfa286e the C9.6 design pair rides this branch). Branch gates: ruff 0; mypy 235 files 0 issues |
| W9-C9.6 | orchestrator | the multi-user + polish design docs (per-user memory namespaces + policy separation; themes/init/docs-site roadmap) — design-level only, the spec's own rule | **done** | the design pair, design-level only per the spec's own rule: docs/plans/2026-11-27-multi-user-design.md (a user IS a home — the namespace inventory, the global-floor-vs-per-user-ceiling policy argument, the G1-extended gate that must lift first) and docs/plans/2026-11-27-polish-roadmap.md (paa init ending on the scaffold door, themes as data riding the overlay loader's shape, the docs site as a rendered view never a copy, the two validation rows: observability end-to-end + the Docker live-server pass) |
| W9-INT | orchestrator | integration on wave-9f: merges, the full gate, board evidence, push, CI green | **done** | five merges in overlap order (effects → marketplace → rpc → mcphost → authoring), every conflict resolved with the union that keeps both sides' truth; the merge's own fixes: PIPELINE_PROTOCOL single-sourced (registry re-exports base.py's frozen constant), the canonical runner map gains builtin.mcp-host, the console.md markers resolved, the insert-shift re-anchor across four docs; the README status block regenerated (245 files / 106,781 lines / 5,664 collected); local full gate: ruff 0, mypy 245 files 0 issues, full suite exit 0 (5,419 passed dots, no service containers — CI's gate (all) is the authority there). The runner round: run 46073274685 on 272398f failed with three classes, each root-caused from the runner's own output — (1) all shapes, 2 failures: the authoring door's usage/help assertions read raw stderr/stdout where the runner's Rich inserts ANSI codes mid-token (the FOURTH instance of the class; fixed with `_plain()`); (2) minimal only, 2 failures: publish-from-installed tests lacked the signing_is_secure guard their siblings carry (the dev fallback cannot authenticate a staged-version publish; guard added, the tests/attest pattern); (3) minimal only, 1 failure: the unsigned-hub-entry refusal asserts the per-shape honest verdict (a real backend names the missing signature, the fallback names why it cannot authenticate one — the W8-CI3 improve precedent). **CLOSED GREEN: run 46074820598 on 704acf7** — all five jobs: gate (minimal) 5,163 passed / 274 labelled skips / 1 xfail; gate (dev) 5,462 passed / 244 env-gated skips / 1 xfail; gate (all) with postgres+seaweedfs+rustfs live and every extra installed: **5,698 passed / 8 skips / 1 xfail** (+203 over the Wave 8 close), mypy 245 files 0 issues; status (--check) and embed image separation green |

**Wave 9 is closed — all seven rows landed** (C9.1–C9.6 + INT; the deferred work the operator ordered: the hosted marketplace, the effect registry with `paa rollback`, the RPC step protocol with `paa-pipeline/1` frozen, the mcp-host adapter, the authoring kit, and the two design docs). Delivery notes: five parallel feature worktrees, subagent runs cut off repeatedly by tool deadlines and recovered by the checkpoint-restart pattern, every branch pushed to the remote the moment it was green — the rule adopted after the first attempt's branches were lost entirely to a sandbox recycle.

### Campaign: W9A-REVIEW — the external review round (2026-11-28)

The operator returned a ranked review of the closed Wave 9 tree: six findings, from a
High (the installer fails open) to hygiene. Every finding was verified against the code
before a line was changed, and fixed in rank order on wave-9a — one finding delegated to
a subagent branch (feat/registration) because its files were disjoint from the rest.

| Task ID | Agent | Assignment | Status | Evidence / notes |
|---|---|---|---|---|
| W9A-1 | orchestrator | (High) the installer fails open: the smoke gate passed with no sandbox configured and on a failed sandbox health check, the AST gate passed with no scanner, and `install()` appended SMOKE_TEST to `passed_gates` unconditionally — a package whose gate never ran was reported as having passed it, against the ledger-is-truth rule | **done** | 2424566: every gate fails closed — the AST gate refuses when no scanner is wired, the smoke gate refuses when no sandbox is wired or the sandbox fails its own health check (the log-and-skip is gone), and an entrypointless package records the smoke gate as not-applicable in the new `na_gates` field, never in `passed_gates`; the honest n/a renders at both install doors (cli/skills, the console screen). Four new tests pin each refusal shape; the marketplace tests now wire the production shape (real scanner, real sandbox, a JSON-emitting entrypoint) |
| W9A-2 | orchestrator | (Medium) two execution paths: `Runtime.run()` called `orchestrator.run` directly, so a task submitted through the core runtime could not choose a pipeline while the chat and CLI surfaces could | **done** | e58c88e: one selection door — `Runtime.submit(pipeline=...)` validates the explicit, mandate-scoped selection through `registry.select` BEFORE any event (unknown or out-of-scope raises `PipelineSelectionError` with zero trace), `TaskRequest.pipeline` rides the `TASK_REQUESTED` payload only-when-set (old events replay identically), and `Runtime.run` reads the choice back from the lineage to dispatch: default task pipeline through the orchestrator unchanged, `builtin.manager` as a composition run on the same lineage through the new shared `default_host_runners` (one runner table for this door and `paa pipeline run`, which now consumes it too), any other pipeline refused loudly with its one-shot door named. Six tests in tests/runtime/test_pipeline_selection.py |
| W9A-3 | orchestrator | (Medium) the extensibility audit was stale (it said pipeline registry/packages/SDK were unsupported — Waves 6-9 shipped them; its `runtime.py:209` cite had drifted twice) and ~350 docs carry line-coupled citations that rot on every change | **done** | aea7b49: a dated re-audit section flips the nine stale rows with current evidence and names what honestly remains open (third-party in-process loops stay behind the RPC boundary — deliberate); the stale matrix rows and three closed blockers carry closure notes; the orphaned staler root copy of the audit and the broken one-shot `_remap_citations.py` are gone; 135 drifted citations re-pointed by a content-anchored remap plus the hand fixes the tool flagged (the component-interactions dispatch chain, boot_recovery, build, the optional-backend ranges, transaction/_reader/trust_key); and the conventions lock gains `PINNED_ANCHORS` — load-bearing citations pinned to the text that must sit on the cited line, closing the lock's own documented blind spot ("wrong without being broken") |
| W9A-4 | subagent (feat/registration) | (Medium) extension registration is closed: three adapters imported by name with no entry-point or plugin discovery, and `assert len(reports) == 7` meant adding a pipeline broke tests by design | **done** | five commits on feat/registration (22958a2 the registration surface — `register_adapter`/`adapter_for` with loud duplicate refusal, the three builtins seeded through the same door, `paa.skill_adapters` entry-point discovery where one broken plugin is warned and skipped; 91dccd2 its suite; 6d985f8 the kind-driven choices in chat/extensions consume the registry, with per-modality constructor args staying at the call sites; bdaddeb+b0cd5ef the exact-count pins relaxed to derived counts — `== 7` becomes `len(BUILTIN_DECLARATIONS)` through the registry binding, with a proof test that an eighth monkeypatched declaration grows the reports by exactly one RED gap row and the derived count follows), merged as 263cea3 |
| W9A-5 | orchestrator | (unverified, possible resource leak) tests/storage/test_errors.py produced an aiosqlite worker thread failing with "Event loop is closed"; production connection handling needed checking | **done** | db2fe17: the audit found the write/rollback/replace paths already close what they abandon, and one real leak — `connect()`'s reader-pool opening loop sat outside the failure boundary, so a reader that could not open stranded the write connection and the readers before it (each a worker thread; a stranded one is exactly the teardown noise). The loop is inside the boundary now: connect either returns a fully connected database or closes everything it opened, pinned by a regression test. The exact teardown message did not reproduce in this Linux sandbox (the missing pytest summary line is environment-wide here); the source leak is fixed regardless |
| W9A-6 | orchestrator | (Low) hygiene: ~6.4 MB of git bundles committed (one a "(1).bundle" duplicate with spaces), a 140 KB PDF report, a malformed `# noqa` at migrations.py:559, and a Windows-only venv path in the README while CI runs Linux | **done** | daef1ac: the four delivery artefacts leave the tree (they were workspace hand-offs, not sources; history still carries them — a filter-repo pass is available if the operator wants the bytes gone from history too); the migrations comment no longer spells a literal directive shape ruff parses as malformed; the README's quick start and testing blocks use the POSIX venv path CI actually runs with the Windows path noted inline |
| W9A-INT | orchestrator | integration on wave-9a: merge, full gate, board, push, CI | **done** | the registration branch merged clean (263cea3); local gate: ruff (src tests) 0, mypy 246 files 0 issues, full suite exit 0. Pre-existing debt observed and NOT touched this round: `ruff check .` (wider than CI's scoped gate) flags 18 errors in `scripts/pipeline_journal_spike.py` — a P0-era spike outside the enforced gate. CI evidence: see the CI close commit that follows this board update |

**The review round is closed — all six findings fixed** (the High first: the installer now fails closed; the two execution paths are one selection door recorded in the ledger; the audit tells the current truth and the citation lock now sees semantic drift; registration is open; the one real connection leak is fixed; the artefacts are gone from the tree).

### Archived campaign: T-VERIFY (2026-11-24, complete)

See git history 5e0a0d8..8d4eecf and the delivered satisfaction report
(paa-pipeline-platform-spec-satisfaction-report.pdf). T-VERIFY-1..3d all done;
deliverables: paa-pipeline-platform-work.patch, paa-format-patch-series/ (16),
the report PDF, this board.

### (archived) T-VERIFY — task rows (2026-11-24)

Owner ask was: check whether `docs/plans/2026-11-21-pipeline-platform-spec.md` (the
uploaded copy is byte-identical) is satisfied on `main`, and deliver the `.patch`
files plus a done/left report. Audit range: `5e0a0d8..8d4eecf` (16 commits:
P0 → P1.1 → P1.2 → SPEC-2 W4 C4.1–C4.7 → W5 C5.2–C5.5 close).

| Task ID | Agent | Assignment | Status | Evidence / notes |
|---|---|---|---|---|
| T-VERIFY-1 | orchestrator | Audit the spec phase-by-phase; produce the report | **done** | report built, QA'd (pdf_qa WARN-only, stat-box false positives), visually verified by subagents |
| T-VERIFY-2a | subagent | Full-suite chunk A (storage, coder, cli, tui, runtime, core, daemon, integration — 1,561 tests) | **verified** | `1318 passed, 244 skipped in 46.71s`, exit 0. Note: the 2 cold-start budget tests in `tests/tui/test_chat_startup.py` flake under CPU contention (wall-clock import budget 2.0 s) and pass in isolation/clean runs — load-induced, not regressions. |
| T-VERIFY-2b | subagent | Full-suite chunk B (validation, context, memory, sandbox — 1,435 tests) | **verified** | `1435 passed in 68.13s`, exit 0; sandbox kill -9 drills all green |
| T-VERIFY-2c | subagent | Full-suite chunk C (models, agents, observability, ledger, deployment, skills, chat, api, mcp_server, conventions, pipelines, marketplace, improvement, root — 1,485 tests) | **verified** | `1484 passed, 1 xfailed in 140.40s`, exit 0; the xfail is the known LOCKDOWN air-gap ordering decision (`tests/agents/test_policy.py::TestLockdown`) |
| T-VERIFY-3b | subagent | Visual QA of the 12-page report (all pages read as images) | **verified** | PASS: no duplication/blank/placeholder/overlap; TOC 16/16 page numbers match footers; 3 cosmetic findings |
| T-VERIFY-3c | subagent | Re-check after cosmetic fixes (caption orphan, status-column breaks, filename wrap) | **verified** | caption + status fixed; filename fixed after column widening (text-layer verified one line) |
| T-VERIFY-3d | subagent | Content-accuracy review of the report against the tree | **verified** | APPROVED after fixes: caught 5 stale/misread numbers (base.py 508→518, ADR-0051 166→172, orchestrator churn −575 → net −355, test_session 201 tests is the directory not the file, mcp.py +630 → +546/−84); all corrected and regenerated |

### Verified-facts register (do not re-verify; cite instead)

- P0.4 spike: `scripts/pipeline_journal_spike.py` → 11 PASS, exit 0 (replay serves
  recorded results with zero re-execution; a changed call is flagged DIVERGED).
- P0.3 manifest check: `scripts/pipeline_manifest_check.py` → all tighten-only
  widening rejections PASS, exit 0.
- P0.5: `docs/EXTENSIBILITY_PIPELINES.md` carries the superseded banner and the
  corrected test count.
- P1.1/P1.2 artifacts exist: `src/paa/pipelines/base.py` (518 lines: `Pipeline`,
  `CapabilityFacade`, `CapabilityToken`/`CapabilitySet`, `compute_grants`,
  divergence/unknown-outcome errors), `src/paa/pipelines/builtin.py` (808 lines:
  `PipelineHost`, `BuiltinPipelineFacade`, `BuiltinPlanExecuteCritique`;
  `src/paa/agents/orchestrator.py` net -355 lines, 1,212 to 857).
- SPEC-2 W4 live: `paa chat --plain` echo-model two-turn recall works (fact stored
  turn 1, recalled turn 2); `scripts/mcp_stdio_smoke.py` → SMOKE OK (6 tools,
  `chat_send`, remember→search hit, spawn path per `docs/interfaces/mcp-server.md`).
- Gates: mypy `src/paa` → 0 issues in 173 files; ruff `src tests` → clean.
  ruff 0.17.6 flags 13 findings under `scripts/` (F401, RUF002 en-dash) — version
  drift + scope: the documented gate is `ruff check src tests`, which passes.
- Collection: 4,481 tests; **full-suite result (chunked, 2026-11-24): 4,237 passed /
  243 env-gated skipped / 1 xfailed / 0 failed** (chunk A 1,318+244, chunk B
  1,435, chunk C 1,484+1x) — an exact match with the W5 close claim in
  `docs/plans/2026-11-21-next-spec-2.md`.
- ADR-recorded decisions for the spec's §12 open questions: R3 default + R2 kept
  for the built-in (ADR-0050); `ctx.use_skill` reuses `TOOL_CALL_*` (ADR-0051);
  tighten-only overlays (ADR-0052). Still open: NDJSON-vs-LSP framing and first
  non-Python language (both P3-time), first outside author (P5 gate), sequencing
  deltas documented in the SPEC-2 plan §2.

### Known left (vs the spec's phases) — do not re-derive

- ~~P1.3, P1.5, P2, P4-lite, conformance v0~~ → **done in Wave 6** (C6.1-C6.6;
  the rule of three was met: builtin + chat + coder.tool-loop on the facade).
- ~~P1.4 (observe as pipeline #4)~~ → **done in Wave 7** (C7.1, landed: `builtin.observe`
  on the facade, `resume="none"`, `evidence_tier="T1"`, conformance ×4;
  gate evidence is in the W7 row above).
- ~~H3.1 (attested change bundle + `paa verify`)~~ → **done in Wave 7** (C7.2,
  landed: `paa-attest-bundle/1`, `paa attest build|pr`, `paa verify` offline,
  hardened here — algorithm-aware signature dispatch, hostile-manifest
  verdicts, git option injection closed; gate evidence is in the W7 row above).
- Wave 7 rest: ~~C7.3 `paa record`~~ → **done in Wave 7** (C7.3, landed by this
  merge: `paa-record/1` chain, the MCP policy proxy, the SandboxSession wrap,
  the attested bundle at close — `paa record run|proxy|verify`). ~~C7.4 evals~~,
  ~~C7.5 `paa thread`~~, ~~C7.6 ACP v1~~ → **done in Wave 7** on the wave-7f
  integration branch: C7.4 the cross-harness eval harness (`paa eval`, five
  tasks × two adapters, deterministic four-check scoring), C7.5 the local-first
  control plane (`paa thread`, content-addressed handoff packets, four ledger
  event types, mid-thread harness swap with the chain unbroken), C7.6 ACP v1
  (`paa acp serve` + the mediated client on the shared framing kit, ADR-0053).
  **Wave 7 is closed — all six rows landed.**
- P3 (RPC host/protocol), the P6 adapters beyond observe,
  record and acp-client (mcp-host in its full form), P7 (authoring kit) → not
  started; `paa-pipeline/1`
  stays draft — freezing waits for P3's RPC run (the spec's own rule).
- ~~P5-lite (the `pipeline` package kind, local-dir + pinned-git distribution,
  signed `.paapkg` through the five gates)~~ → **done in Wave 8** (C8.6). The
  hosted marketplace — publishing, the loop economy — stays parked on the
  spec's own rule: a real outside author (P5's gating sentence).
- C5.1/G1 real-model S1: PENDING, operator-blocking (no model key in this sandbox);
  run book in `docs/operations/2026-11-09-next-spec/g1-decision.md`.

### Deliverables of this campaign

- `/home/z/my-project/download/paa-wave7-complete.patch` + `.bundle` — combined
  diff and git bundle of all unpushed commits through the Wave 7 close (every
  environment gate open at verification; regenerated at the wave-7f close;
  supersedes `paa-w7-final.*`, the w4-w7 pair, the host's working-tree series
  and every earlier range).
- `/home/z/my-project/download/paa-format-patch-series-w7c/` — per-commit
  patches from `origin/main` (git am ready; the w7 and w6 series are
  superseded).
- `/home/z/my-project/download/paa-observe-demo-bundle.json` — a complete T1
  bundle from the scripted fake-harness run (the C7.1 done-when, as an artifact).
- `/home/z/my-project/download/paa-attest-demo-bundle/` — a complete, verifying
  attested bundle from the scripted fake-harness flow (the C7.2 done-when, as
  an artifact: observe → attest → verify, plus its PR body).
- `/home/z/my-project/download/paa-record-demo/` — the C7.3 done-when, as an
  artifact: a scripted cooperating harness recorded by the real CLI (flight
  record verifying green via `paa record verify`, its observe bundle, its
  attested bundle and the flipped-byte refusal beside it).
- `/home/z/my-project/download/paa-eval-demo/` — the C7.4 done-when, as an
  artifact: the live 2×5 matrix through `paa eval run` (recorded-fake 5/5
  mediated with verifying bundles; scripted-direct 2/5 — all three designed
  discriminators tripped, with reasons in the report JSON).
- `/home/z/my-project/download/paa-thread-demo/` — the C7.5 done-when, as an
  artifact: the live harness swap through the real CLI (new → run fake-alpha
  → swap fake-beta → show → ledger, both handoff packets, chain integrity ok).
- `/home/z/my-project/download/paa-acp-demo/` — the C7.6 acceptance, as an
  artifact: the raw wire of a live session over real subprocess pipes —
  streaming chunks, a mid-flight cancel that stops the stream in 2 ms and
  keeps the session usable, clean EOF.
- `/home/z/my-project/download/paa-pipeline-platform-spec-satisfaction-report.pdf`
  — the audit report (6th edition: Wave 6 DONE + all six Wave 7 rows landed).
- Earlier ranges (`paa-w7-final.*`, `paa-spec2-w4-w7.*`, `paa-pipeline-platform-work.patch`,
  `paa-spec2-w4-w5.*`, `paa-spec2-w4-w6.*`, `pipeline-platform-p0-p1.*`, the w6/w7
  series dirs) are superseded; `paa-report-cover.html` remains the report cover source.
- This board (committed with each wave close — tracking state, not product;
  the host's uncommitted-by-design copy is superseded by this landed history).

### Archive (finished rows)
