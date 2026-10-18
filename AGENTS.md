# AGENTS.md — PAA runtime

Local-first, event-sourced agent substrate (`paa`, Python ≥3.11, src-layout under `src/paa`).
Background: `README.md`. Design reasoning and every RFC deviation: `docs/adr/README.md`.

## Setup and verification

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q          # Linux; README shows the Windows .venv/Scripts path
```

Verify in this order before calling work done:

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/ -q
```

- Focused runs: `pytest tests/ledger -q`, `pytest tests/ledger/test_replay.py::test_x -q`,
  `pytest -m "not slow"`, `pytest -m chaos`, `pytest -m requires_graph`.
- mypy is `strict = true` with `warn_unreachable` — `disallow_untyped_defs` is relaxed only for `tests.*`.
- ruff `line-length = 100`, and `S` (bandit) is on. The `S608` / `S105` ignores in `pyproject.toml` are
  per-file allow-lists for known-safe patterns; do **not** add new files to them, and do not "fix" a
  triggered line by widening the ignore.
- There is no CI config, Makefile, or pre-commit hook in this repo. Nothing enforces the order above
  but you.

## The one invariant: the ledger is the truth

`src/paa/ledger/` is the spine; Redis, queues, vector/graph indexes, and the filesystem are all derived
views rebuilt by replay (`ledger/replay.py`). Two rules follow and are not negotiable:

1. **Ledger event before side effect, always.** A crash between event and work is recoverable; the
   reverse is not. See the task lifecycle diagram in `README.md`.
2. **Everything else must be replayable.** If you add state that isn't derivable from events, it needs
   a snapshot *and* a recovery rule (`ledger/recovery.py`, `save_snapshot`/`load_snapshot`).

Adding an `EventType` means touching, at minimum: `core/types.py` (`EventType`, plus `TERMINAL_EVENTS`
if it ends a lineage), `ledger/replay.py` (`_PHASE_MAP` and the projection `match`, or `_NON_PHASE_EVENTS`
if it is informational), and the emitter in `agents/orchestrator.py`. `ledger/store.py` assigns
`state_version`, links `prev_hash`, and updates the head projection in one transaction — never append
outside it.

`EXECUTION_COMPLETED` fires **per step**, not per task (ADR-0010); it is not terminal.

## Storage is a polyglot with a parity lock

`storage/relational/schema_sqlite.sql` and `schema_postgres.sql` must stay table- and column-identical
(types may differ: UUID/JSONB/TIMESTAMPTZ vs TEXT/INTEGER). `tests/storage/test_schema_parity.py`
parses both with a small DDL lexer and fails the build on any divergence. If you add a column, add it
to **both** files.

Every substrate has an embedded default and a server variant, selected by `PAA_*` env vars
(`StorageSettings.backend_*`). The factories degrade, they never raise — `runtime.py::_try_vector /
_try_graph /_try_queue` and `sandbox/_acquire_sandbox` (5s probe timeout, then subprocess) are the
pattern. Heavy deps (qdrant, kuzu, sentence-transformers, redis, psycopg, boto3) are optional extras;
**never** import one at module scope in a core path, or a minimal install stops importing.

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
- Config stores the *names* of secret env vars (`minio_secret_key_env`, `escalation_api_key_env`), never
  values. Keep it that way.
- `get_settings()` is `lru_cache`d. Anything that changes `PAA_HOME` (tests, mostly) **must** call
  `reset_settings_cache()`.

## Testing conventions

- `asyncio_mode = "auto"` — async tests need no decorator. Fixtures are function-scoped and write into
  `tmp_path`, so order-independence is expected; don't add session-scoped state.
- `filterwarnings = ["error::DeprecationWarning:paa.*"]` — a DeprecationWarning raised from `paa`
  fails the test. Fix the code, don't silence it.
- Extras-gated tests use a `pytest.mark.requires_*` marker **and** a `skipif`/`importorskip` guard
  (see `tests/storage/test_vector.py`, `tests/api/conftest.py`). `--strict-markers` is on.
- `paa_home` (tests/conftest.py) isolates state; `snapshotter` is a byte-oriented fake on purpose —
  `write_text`/`read_text` would translate newlines on Windows and report phantom manifest drift.
- Set `PAA_TEST_VERBOSE=1` to un-silence structlog.
- Hypothesis covers the invariants that must hold for all inputs (gatherer token ceiling, replay over
  arbitrary event orderings, confidence in [0,1]). Extend those rather than writing example-based
  tests for them.
- Crash/chaos tests simulate power loss by closing the DB mid-lineage and reopening — committed
  transactions survive, uncommitted vanish.
- `tests/validation/` is an empty package and there is no `tests/observability/` — those subsystems are
  genuinely uncovered. `src/paa/observability/` in particular is exercised nowhere.

## Model layer

Agents depend on the narrow `agents/reasoning.py::ModelLike` protocol
(`complete_structured(prompt, schema, **kwargs)`), never on a provider. `runtime.py::RouterModelAdapter`
is the only place that knows about `EscalatingModelRouter`; keep it that way.

To test anything agent-shaped, inject a scripted model:

```python
rt = await Runtime.build(model_adapter=ScriptedModel(), ...)
```

`EchoProvider` cannot drive a plan (its minimal-schema-instance output isn't a plan). The
`ScriptedModel` in `tests/integration/conftest.py` is the reference implementation — it answers by
schema shape and records every call, which is how the "no model in the security loop" guarantee is
asserted (a blocked task must reach its verdict with `call_count` unchanged).

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
`cp .env.example .env && docker compose up -d --build`. Postgres/Redis/Qdrant/MinIO publish **no**
host ports; only the ingestion API on `127.0.0.1:8787`. gVisor is opt-in, Linux-only, via
`docker compose -f docker-compose.yml -f docker-compose.gvisor.yml up -d`, and the runtime honestly
reports `NAMESPACE` instead of `VM` when `runsc` isn't registered.

## Known-stale references (don't chase these)

- `docs/adr/README.md` indexes ADRs 0001–0020, but only `0008`, `0010`, `0015`, `0019`, `0020` exist
  on disk. Source comments cite `docs/adr/0001`–`0007` etc. as rationale; the intent is documented
  even though the file is absent.
- `docs/architecture.md` is referenced from docstrings but does not exist.
- `README.md`'s Testing section still shows Windows paths, and its per-subsystem test counts in the
  build-status table are approximate — only the total line was reconciled against a real run.
- `src/paa/agents/specialists/` is an empty package.

## The coder agent (`paa agent`)

Full write-up: `docs/coder-agent.md`.

`src/paa/coder/` was standalone until `coder/agent.py` wired it up: `CoderAgent.build()`
composes a real `Runtime` + `coder/runtime_tools.py` + `RouterToolCallingModel` + `AgentLoop`.
Read `coder/__init__.py` before adding to that package — the pieces are deliberately decoupled.

- **The model seam is constrained JSON, not native tool calls.** `coder/model_adapter.py` asks the
  router for a `{text, tool_calls[]}` object conforming to a schema with tool names in an `enum`;
  `ModelProvider.complete_structured` does the validate-and-repair. Nothing in `models/` supports a
  `tools` wire field, so adding native tool calling is an optimisation *inside that one class*.
- **`ModelTurn.tokens` is always 0** on this path (`complete_structured` returns a dict and drops
  usage numbers). `AgentLoop(max_tokens=...)` therefore never trips — bound runs with `max_steps`.
- **Tool names must match the policy globs.** `submit_task`/`run_task` match none of
  `DEFAULT_MUTATING_TOOLS`, which is why `CoderAgent.build` passes
  `mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS)` — otherwise `--mode plan`
  would silently exempt the two tools that actually change state. A new mutating tool needs adding
  to that tuple or it becomes read-only-by-accident.
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
  Without `--yes`, an ASK-ed call prompts; with no confirm callback the loop auto-*denies*.