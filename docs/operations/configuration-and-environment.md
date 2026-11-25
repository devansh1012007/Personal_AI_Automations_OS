# Configuration and environment

All runtime behavior is controlled through `src/paa/config.py`: a pydantic-
settings `Settings` tree with prefix `PAA_`, nested delimiter `__`, and `.env`
read from the current working directory.

## Configuration precedence

**explicit kwargs > environment (`PAA_*`) > `.env` file > defaults**
(`config.py:7`). `get_settings()` is `lru_cache`d (`config.py:490`); callers
that change `PAA_HOME` must call `reset_settings_cache()` (`config.py:931`).

## Two-layer .env reading

`.env` is read by **two** layers:
1. **pydantic-settings** for `PAA_*` settings fields (`config.py:390`).
2. **`dotenv_value(name)`** (`config.py:896`) for variables that are *not*
   settings fields — chiefly each provider's API key, named per registry row
   (`registry.ProviderSpec.api_key_env`). It parses `.env` **without** writing
   to `os.environ` (which would inject every `PAA_*` line into the process,
   including test runs). Environment still wins; `reset_dotenv_cache()` exists
   for tests that chdir (`config.py:500`).

## Settings tree (verified against config.py)

### `Settings` (top level)

| Field | Default | Notes |
|---|---|---|
| `home` | `~/.paa` (or `PAA_HOME`) | Roots all state; deliberately outside the repo |
| `api_host` | `127.0.0.1` | Loopback enforced unless `PAA_ALLOW_NON_LOOPBACK=1` |
| `api_port` | `8787` | |
| `storage` | `StorageSettings()` | |
| `context` | `ContextSettings()` | |
| `memory` | `MemorySettings()` | |
| `policy` | `PolicySettings()` | |
| `sandbox` | `SandboxSettings()` | |
| `models` | `ModelSettings()` | |
| `queue` | `QueueSettings()` | |
| `observability` | `ObservabilitySettings()` | |
| `reflection` | `ReflectionSettings()` | |
| `chat` | `ChatSettings()` | SPEC 2 W4 chat surface |
| `mcp` | `MCPSettings()` | SPEC 2 W4 MCP server |

`_propagate_home` re-roots storage paths when `home` is overridden directly
(`config.py:425`). `ensure_directories()` creates all state dirs
(`config.py:886`).

### `StorageSettings`

| Field | Default | Consumer |
|---|---|---|
| `backend_relational` | `sqlite` | `relational/__init__.py:get_database` |
| `backend_vector` | `auto` | `vector/__init__.py:get_vector_store` |
| `backend_graph` | `auto` | `graph/__init__.py:get_graph_store` |
| `backend_queue` | `sqlite` | `queue/__init__.py:get_queue` |
| `backend_coldlake` | `filesystem` | `coldlake/__init__.py:get_blob_store` |
| `sqlite_path` | `~/.paa/state/paa.db` | `Database` |
| `sqlite_busy_timeout_ms` | `10000` | `Database` |
| `postgres_dsn` | `None` | Postgres backend |
| `qdrant_path` | `~/.paa/state/qdrant` | Vector store |
| `qdrant_url` | `None` | Qdrant server |
| `kuzu_path` | `~/.paa/state/graph` | Graph store |
| `cold_lake_path` | `~/.paa/cold_lake` | CAS |
| `s3_endpoint` | `None` | S3 cold lake (ADR-0026) |
| `s3_bucket` | `paa-cold-lake` | S3 |
| `s3_secure` | `False` | S3 TLS |
| `s3_access_key_env` | `PAA_S3_ACCESS_KEY` | name only, never value |
| `s3_secret_key_env` | `PAA_S3_SECRET_KEY` | name only, never value |
| `redis_url` | `redis://127.0.0.1:6379/0` | Redis queue |
| `vault_path` | `~/.paa/vault` | World model |
| `workspace_root` | `~/.paa/workspaces` | Sandbox jail root |
| `zstd_level` | `3` (1–22) | CAS compression |
| `zstd_level` validator | ge=1, le=22 | `config.py:133` |
| `all_paths()` | — | Directories that must exist before boot |

The pre-ADR-0026 names — `minio_endpoint`, `minio_bucket`, `minio_secure`,
`minio_access_key_env`, `minio_secret_key_env` — are still accepted and are
normalised onto the `s3_*` fields, so a deployment written against them keeps
working. `backend_coldlake` likewise takes `"s3"` or the alias `"minio"`. The
*extra* is the one thing that is deliberately broken: `pip install
'paa[minio]'` no longer works, and the extra is `paa[s3]`.

### `ContextSettings`

| Field | Default | Notes |
|---|---|---|
| `token_ceiling` | `1500` (ge=128) | Planner context hard ceiling |
| `worker_token_ceiling` | `1000` (ge=64) | Worker context ceiling |
| `relevance_floor` | `0.75` | Semantic matches below discarded |
| `confidence_floor` | `0.70` | Relational facts below discarded |
| `invariant_importance` | `0.85` | Survive pruning |
| `density_proceed` | `0.85` | Planner may run unassisted |
| `density_hydrate` | `0.40` | Background hydration threshold |
| `_check_density_ordering` | — | `density_hydrate < density_proceed` |
| `chars_per_token` | `4.0` | Cheap estimator |

### `MemorySettings`

| Field | Default | Notes |
|---|---|---|
| `prune_confidence_floor` | `0.15` | Explicit-set-only eviction override: unset ⇒ per-domain `prune_floor` from `DOMAINS` (byte-identical); set explicitly (`PAA_MEMORY__PRUNE_CONFIDENCE_FLOOR`) ⇒ replaces every mutable domain's floor in `DecaySweeper` |
| `contradiction_threshold` | `0.75` | Quarantine both records |
| `contradiction_degraded_confidence` | `0.20` | Quarantined confidence |
| `decay_lambda` | {long_term_distilled:0.001, semantic:0.002, reflection:0.002, relationship:0.004, temporal:0.01, tool:0.05, operational:1.0} | **Deprecated, ignored.** Kept only so existing entries keep parsing; sole source is `domains.domain_policy`. Setting it emits `DeprecationWarning`; unknown keys warn-and-ignore |
| `relationship_prune_floor` | `0.10` | Graph edges severed |
| `use_count_reinforcement` | `0.01` | δ in importance index |
| `decay_sweep_interval_hours` | `6` | Daemon maintenance interval |
| `curation_window_start_hour` | `2` | Nightly curation |
| `curation_max_runtime_hours` | `2.0` | Hard cap |

*Note: `domains.py` is the source of truth for per-domain decay policy. `MemorySettings.decay_lambda` no longer mirrors it — it is deprecated/ignored with load-time warnings, and decay reads only `DOMAINS`.*

### `PolicySettings`

| Field | Default | Notes |
|---|---|---|
| `mode` | `ASK` | `PermissionMode.ASK` |
| `anti_goal_threshold` | `0.82` | Cosine similarity kill threshold |
| `auto_confidence_floor` | `0.80` | AUTO mode gate skip |
| `always_gate_risk_profile` | `0.90` | Belt-and-braces human gate |
| `latency_budget_ms` | `50.0` | Logged, not fatal |

### `SandboxSettings`

| Field | Default | Notes |
|---|---|---|
| `backend` | `auto` | `auto`\|`subprocess`\|`wsl`\|`docker`\|`dryrun` |
| `container_runtime` | `runc` | `runsc` selects gVisor (only if registered) |
| `default_timeout_seconds` | `30.0` | |
| `container_start_grace_seconds` | `5.0` (ge=0) | The budget on the **container's start**, on the container backend only. Separate from the work's `timeout_seconds` and never added to it (ADR-0031) — the two are enforced independently, and a start that overruns is a `start_timeout` (a host problem), not a work timeout. The subprocess and WSL backends have no start phase, ignore it entirely, and say so in their own report — the subprocess backend in both its `preflight` and its `describe()`, the WSL backend in its `describe()`. `0` leaves the start no budget at all, which no real daemon can meet. |
| `enforce_peak_rss` | `True` | Peak RSS (ADR-0009), not ∫Memory·dt |
| `watchdog_interval_seconds` | `0.25` | |
| `heartbeat_interval_seconds` | `5.0` | |
| `heartbeat_miss_tolerance` | `3` | |
| `default_deny_network` | `True` | |
| `max_capture_bytes` | `8388608` (8 MiB) | |
| `absolute_recursion_ceiling` | `4` (ge=0) | |

### `ModelSettings`

| Field | Default | Notes |
|---|---|---|
| `local_provider` | `ollama` | Any registry name |
| `local_model` | `qwen2.5:3b-instruct` | |
| `local_base_url` | `http://127.0.0.1:11434` | |
| `local_api_base` | `None` | Override |
| `escalation_provider` | `anthropic` | `"none"` disables escalation |
| `escalation_model` | `claude-sonnet-5` | |
| `escalation_api_key_env` | `ANTHROPIC_API_KEY` | name only |
| `escalation_base_url` | `None` | Override |
| `escalate_at_or_above` | `COMPLEX` | |
| `escalation_forbidden_modes` | `(LOCKDOWN,)` | |
| `max_concurrent_streams` | `2` | Router semaphore |
| `request_timeout_seconds` | `120.0` | |
| `max_retries` | `2` | Structured-output repair |
| `embedding_model` | `all-MiniLM-L6-v2` | 384-dim |
| `embedding_dimensions` | `384` | |
| `allow_hash_embedder_fallback` | `True` | Warns loudly |

### `QueueSettings`

| Field | Default | Validator |
|---|---|---|
| `backpressure_depth` | `10` | |
| `shed_load_depth` | `50` | `> backpressure_depth` |
| `max_delivery_attempts` | `3` | |
| `visibility_timeout_seconds` | `300.0` | |
| `entity_lock_ttl_seconds` | `60.0` | |

### `ObservabilitySettings`

| Field | Default | Notes |
|---|---|---|
| `enabled` | `True` | |
| `service_name` | `paa-runtime` | |
| `otlp_endpoint` | `None` | Local ledger only when None |
| `log_level` | `INFO` | |
| `log_json` | `False` | |
| `trace_retention_days` | `30` | |

### `MCPSettings` (`PAA_MCP__*`)

The MCP server surface (`paa mcp serve`, SPEC 2 §3.1 C4.5): stdio-only, spawned
by the MCP client, so there is no socket, no bind address, and nothing for the
loopback rule to widen.

| Field | Default | Notes |
|---|---|---|
| `token_env` | `PAA_MCP_TOKEN` | The **name** of the env var holding the token — never the value (the same indirection `s3_secret_key_env` uses). When the named variable holds a non-empty value, every client must present it in `initialize` (`params.token`) before any tool executes; unset, the spawn pipe is the trust boundary. `paa doctor`'s `mcp token env` row reports exactly this variable, set/unset, name only |
| `default_session` | `None` | Session id that session-less tool calls fall back to. `None` means session-less calls behave session-less: `memory_remember` refuses until the caller names a session |

### `ChatSettings` (`PAA_CHAT__*`)

The chat surface (SPEC 2 §3.1 C4.1–C4.3), offline-first by default.

| Field | Default | Notes |
|---|---|---|
| `model` | `echo` | Names a **registry row** (`models/registry.py:resolve_spec`), not a vendor. `echo` resolves to the deterministic offline provider, so chat demos with no network and no key; any other row needs whatever credential its `api_key_env` names. `paa chat --model` overrides per invocation; `paa doctor`'s `chat model` row resolves this through the registry (no network call) and reports the key env **name** set/unset |
| `token_budget` | `None` (ge=1) | Hard token ceiling for the chat context bundle; `None` inherits `ContextSettings.token_ceiling` at use time so chat carries one budget line, not a second one that drifts |
| `cross_session_episodic` | `False` | Cross-session episodic reads. `False`: a session's raw turns are visible only inside that session (semantic facts are global either way, with originating session in provenance). `True` lifts the fence deliberately — and the refusal either way is a ledger event |
| `streaming` | `True` | Stream reply deltas to the caller's callback; the ledger records the final reply either way |
| `last_k_turns` | `6` (ge=0) | How many of this session's recent turns the bundle carries |
| `recall_k` | `5` (ge=0) | How many semantic facts a turn's recall may consult |
| `persona` | `"You are PAA, a local-first assistant with persistent memory."` | First thing packed into every bundle and first thing a small ceiling evicts |

## Environment variable reference

| Variable | Where read | Purpose |
|---|---|---|
| `PAA_HOME` | `config._default_home()` | State root |
| `PAA_ALLOW_NON_LOOPBACK` | `config._enforce_loopback` | Override loopback bind |
| `PAA_STORAGE__*` | pydantic-settings | Storage backend selection & paths |
| `PAA_CONTEXT__*` | pydantic-settings | Context budgets |
| `PAA_MEMORY__*` | pydantic-settings | Memory decay/pruning |
| `PAA_POLICY__*` | pydantic-settings | Policy gating |
| `PAA_SANDBOX__*` | pydantic-settings | Sandbox containment |
| `PAA_MODELS__*` | pydantic-settings | Model providers |
| `PAA_QUEUE__*` | pydantic-settings | Queue/backpressure |
| `PAA_OBSERVABILITY__*` | pydantic-settings | Logging/tracing |
| `PAA_CHAT__*` | pydantic-settings | Chat surface (model, budget, session fence) |
| `PAA_MCP__*` | pydantic-settings | MCP server (token env name, default session) |
| `PAA_API_HOST` / `PAA_API_PORT` | pydantic-settings | API bind |
| `PAA_MCP_TOKEN` | `dotenv_value` via `cli/mcp.py` | The MCP token **value** (the variable *named* by `PAA_MCP__TOKEN_ENV`, default `PAA_MCP_TOKEN`) |
| `ANTHROPIC_API_KEY` | `dotenv_value` via registry | Escalation provider key |
| `OPENAI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, … | `dotenv_value` via registry | Provider keys (named per `api_key_env`) |
| `PAA_S3_ACCESS_KEY`, `PAA_S3_SECRET_KEY` | `dotenv_value` via registry | S3 credentials (names in config) |

Provider `api_key_env` tuples are defined per row in `registry.py:_SPECS`
(e.g. `anthropic: ("ANTHROPIC_API_KEY",)`, `gemini: ("GEMINI_API_KEY",
"GOOGLE_API_KEY")`).

## Defaults that differ from the RFC (documented deviations)

| Setting | RFC | PAA default | Deviation doc |
|---|---|---|---|
| Relational | PostgreSQL | SQLite | ADR-0001–0005 |
| Vector | Qdrant server | Qdrant embedded / numpy | ADR-0001–0005 |
| Graph | Kuzu | Kuzu / SQLite CTE | ADR-0001–0005 |
| Queue | Redis | SQLite | ADR-0001–0005 |
| Cold lake | S3 | Filesystem CAS | ADR-0004, ADR-0026 |
| Sandbox | gVisor (runsc) | subprocess / WSL / Docker | ADR-0006 |
| Model | vLLM + Llama-3-8B-Q8 | Ollama / local + escalation | ADR-0007, ADR-0015 |
| Memory rule | ∫Memory·dt > Ceiling | Peak RSS | ADR-0009 |
| Idempotency | SHA-256(cid+type+version) | +attempt+discriminator | ADR-0008 |
| Recovery index | partial index (excludes terminal) | head projection | ADR-0010 |

## Local development setup

1. `uv venv --python 3.12 && uv pip install -e ".[dev]"` (`AGENTS.md:8`).
2. Optionally `cp .env.example .env` and set provider keys.
3. `.venv/bin/python -m pytest tests/ -q` (`AGENTS.md:11`).
4. `paa doctor` to confirm which backends are actually live, plus the chat
   model resolution, the MCP token env and the TUI extra (`cli/main.py:305`).

## Deployment vs development

- Docker deployment (`docs/deployment/docker.md`): `cp .env.example .env &&
  docker compose up -d --build`. Postgres/Redis/Qdrant/the object store
  publish no host ports; only ingestion API on `127.0.0.1:8787`.
- gVisor is opt-in, Linux-only (`docker compose -f docker-compose.yml -f
  docker-compose.gvisor.yml up -d`).
- The laptop default is SQLite WAL + embedded fallbacks (idles at ~0MB,
  `README.md:81`).

## Validation

- `SettingsConfigDict(validate_default=True)` (`config.py:586`) — defaults are
  validated too.
- `extra="ignore"` — unknown `PAA_*` fields are ignored, not errors
  (`config.py:889`).
- A variable defined in `.env.example` but never consumed by the application
  is **not** an active runtime setting.

## Gaps

- `MemorySettings.prune_confidence_floor` is an explicit-set-only override
  in `DecaySweeper`; unset it is inert and the per-domain `prune_floor`
  applies. `MemorySettings.decay_lambda` is deprecated and ignored
  (`domains.domain_policy` is the sole λ source); unknown keys in it are
  warn-and-ignore, not a load failure.
- `tests/validation/` has a suite now. `tests/observability/` exists and
  covers the run repository's identifier coercions (`AGENTS.md:97`).

## Related documents

- [`operations/deployment-and-runtime.md`](deployment-and-runtime.md) — Docker/deployment topology.
- [`architecture/overview.md`](../architecture/overview.md) — configuration philosophy.
- [`modules/storage.md`](../modules/storage.md) — `PAA_STORAGE__*` consumers.
- [`modules/models.md`](../modules/models.md) — `PAA_MODELS__*` consumers.
