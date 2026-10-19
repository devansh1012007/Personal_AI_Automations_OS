# Configuration and environment

All runtime behavior is controlled through `src/paa/config.py`: a pydantic-
settings `Settings` tree with prefix `PAA_`, nested delimiter `__`, and `.env`
read from the current working directory.

## Configuration precedence

**explicit kwargs > environment (`PAA_*`) > `.env` file > defaults**
(`config.py:7`). `get_settings()` is `lru_cache`d (`config.py:489`); callers
that change `PAA_HOME` must call `reset_settings_cache()` (`config.py:495`).

## Two-layer .env reading

`.env` is read by **two** layers:
1. **pydantic-settings** for `PAA_*` settings fields (`config.py:384`).
2. **`dotenv_value(name)`** (`config.py:460`) for variables that are *not*
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

`_propagate_home` re-roots storage paths when `home` is overridden directly
(`config.py:425`). `ensure_directories()` creates all state dirs
(`config.py:450`).

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
| `minio_endpoint` | `None` | MinIO cold lake |
| `minio_bucket` | `paa-cold-lake` | MinIO |
| `minio_secure` | `False` | MinIO TLS |
| `minio_access_key_env` | `PAA_MINIO_ACCESS_KEY` | name only, never value |
| `minio_secret_key_env` | `PAA_MINIO_SECRET_KEY` | name only, never value |
| `redis_url` | `redis://127.0.0.1:6379/0` | Redis queue |
| `vault_path` | `~/.paa/vault` | World model |
| `workspace_root` | `~/.paa/workspaces` | Sandbox jail root |
| `zstd_level` | `3` (1–22) | CAS compression |
| `zstd_level` validator | ge=1, le=22 | `config.py:111` |
| `all_paths()` | — | Directories that must exist before boot |

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
| `prune_confidence_floor` | `0.15` | Eviction threshold (note: appears unused by decay.py/curator; domain `prune_floor` is used instead — see gaps) |
| `contradiction_threshold` | `0.75` | Quarantine both records |
| `contradiction_degraded_confidence` | `0.20` | Quarantined confidence |
| `decay_lambda` | {long_term_distilled:0.001, semantic:0.002, reflection:0.002, relationship:0.004, temporal:0.01, tool:0.05, operational:1.0} | Per-domain λ in C(t)=C₀·e⁻ˡᵃᵐᵇᵈᵃ·t |
| `relationship_prune_floor` | `0.10` | Graph edges severed |
| `use_count_reinforcement` | `0.01` | δ in importance index |
| `decay_sweep_interval_hours` | `6` | Daemon maintenance interval |
| `curation_window_start_hour` | `2` | Nightly curation |
| `curation_max_runtime_hours` | `2.0` | Hard cap |

*Note: `domains.py` is the source of truth for per-domain decay policy; `MemorySettings.decay_lambda` duplicates the values but grep shows neither `decay.py` nor `domains.py` reads it directly (potential config/drift).*

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
| `PAA_API_HOST` / `PAA_API_PORT` | pydantic-settings | API bind |
| `ANTHROPIC_API_KEY` | `dotenv_value` via registry | Escalation provider key |
| `OPENAI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, … | `dotenv_value` via registry | Provider keys (named per `api_key_env`) |
| `PAA_MINIO_ACCESS_KEY`, `PAA_MINIO_SECRET_KEY` | `dotenv_value` via registry | MinIO credentials (names in config) |

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
| Cold lake | MinIO | Filesystem CAS | ADR-0004 |
| Sandbox | gVisor (runsc) | subprocess / WSL / Docker | ADR-0006 |
| Model | vLLM + Llama-3-8B-Q8 | Ollama / local + escalation | ADR-0007, ADR-0015 |
| Memory rule | ∫Memory·dt > Ceiling | Peak RSS | ADR-0009 |
| Idempotency | SHA-256(cid+type+version) | +attempt+discriminator | ADR-0008 |
| Recovery index | partial index (excludes terminal) | head projection | ADR-0010 |

## Local development setup

1. `uv venv --python 3.12 && uv pip install -e ".[dev]"` (`AGENTS.md:8`).
2. Optionally `cp .env.example .env` and set provider keys.
3. `.venv/bin/python -m pytest tests/ -q` (`AGENTS.md:11`).
4. `paa doctor` to confirm which backends are actually live (`cli/main.py:208`).

## Deployment vs development

- Docker deployment (`docs/deployment/docker.md`): `cp .env.example .env &&
  docker compose up -d --build`. Postgres/Redis/Qdrant/MinIO publish no host
  ports; only ingestion API on `127.0.0.1:8787`.
- gVisor is opt-in, Linux-only (`docker compose -f docker-compose.yml -f
  docker-compose.gvisor.yml up -d`).
- The laptop default is SQLite WAL + embedded fallbacks (idles at ~0MB,
  `README.md:59`).

## Validation

- `SettingsConfigDict(validate_default=True)` (`config.py:390`) — defaults are
  validated too.
- `extra="ignore"` — unknown `PAA_*` fields are ignored, not errors
  (`config.py:389`).
- A variable defined in `.env.example` but never consumed by the application
  is **not** an active runtime setting.

## Gaps

- `MemorySettings.prune_confidence_floor` appears unused in `src/` (decay uses
  domain `prune_floor`; curator's usage unverified). It may be reserved for
  callers or dead config.
- `tests/validation/` is empty; there is no `tests/observability/`
  (`AGENTS.md:97`).

## Related documents

- [`operations/deployment-and-runtime.md`](deployment-and-runtime.md) — Docker/deployment topology.
- [`architecture/overview.md`](../architecture/overview.md) — configuration philosophy.
- [`modules/storage.md`](../modules/storage.md) — `PAA_STORAGE__*` consumers.
- [`modules/models.md`](../modules/models.md) — `PAA_MODELS__*` consumers.
