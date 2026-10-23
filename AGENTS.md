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
`dev` extra now resolves `paa[api]` and `paa[s3]` itself (the same self-referential form
`all` already used), so there is no environment in which the gate below can pass while the
API or the S3 path is untested.

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
if it is informational), and its emitter — `agents/orchestrator.py` for lifecycle events,
`ledger/recovery.py` for recovery and rollback events, `models/router.py` for `MODEL_ESCALATED`.
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
`.`: the `dev` extra is what carries `paa[api]` and `paa[s3]`, and without it the API
suite and the live S3 suite skip themselves at import — the quietest way to not test
something.

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
