# Integrations and communication

PAA communicates over three boundaries: (1) the ingestion API (HTTP, loopback),
(2) the model provider layer (local + optional frontier), and (3) optional
external substrates (Qdrant, Kuzu, Redis, the S3 object store). There is no service mesh, no
message broker beyond the durable SQLite/Redis queue, no webhooks, and no
ingestion bridges (Telegram/email/Slack) yet (`README.md:169`).

## 1. Model providers (the primary external integration)

### Provider contract
`src/paa/models/base.py` — `ModelProvider` ABC: `complete(request) ->
CompletionResponse`, `healthcheck() -> bool` (must never raise), and
`complete_structured(request, schema)` with validate-and-repair. Providers are
concurrent-safe and hold no per-request state (`base.py:564`).

### Provider registry
`src/paa/models/registry.py` — `KNOWN_PROVIDERS` holds ~20 `ProviderSpec` rows:
- **OpenAI family** (`/v1/chat/completions`): openai, groq, openrouter,
  together, deepseek, xai, mistral, fireworks, perplexity, azure; plus local
  lmstudio, llamacpp, vllm, tgwebui, openai_compatible, ollama_openai.
- **Bespoke**: ollama, anthropic, gemini.
- **Echo**: deterministic offline provider (no network).

Each row: `name`, `kind`, `default_model`, `default_base_url`, `api_key_env`
(tuple, tried in order), `default_tier` (LOCAL/FRONTIER), `is_local`, plus
OpenAI-family quirks (chat_path, auth_header, api_version, ...).

### API key sourcing
`ProviderSpec.resolved_api_key()` (`registry.py:104`): environment first, then
`.env` via `config.dotenv_value(name)` — **without** writing to `os.environ`.
Provider keys are **not** `Settings` fields; they are named per registry row
(`AGENTS.md:68`). Secret *names* (never values) are stored in config, e.g.
`escalation_api_key_env: "ANTHROPIC_API_KEY"` (`config.py:310`).

### Escalation router
`src/paa/models/router.py` — `EscalatingModelRouter`:
1. Is escalation forbidden by permission mode? (`LOCKDOWN` → raise, never
   escalate.) — checked first.
2. Is modality ≥ `escalate_at_or_above`? (default `COMPLEX`.)
3. Try local; on `ModelUnavailableError` or `StructuredOutputError`, escalate to
   frontier.
- Semaphore caps `max_concurrent_streams` (default 2) — a memory bound, not a
  politeness setting (`router.py:30`).
- Every escalation writes a `MODEL_ESCALATED` ledger event **before** the
  frontier call (`router.py:417`), with `leaves_machine = frontier.tier >=
  FRONTIER`.
- A concurrency semaphore sized from `max_concurrent_streams`.

Default routing: `ModelSettings.local_provider="ollama"`, `local_model=
"qwen2.5:3b-instruct"`, `local_base_url="http://127.0.0.1:11434"`;
`escalation_provider="anthropic"`, `escalation_model="claude-sonnet-5"`,
`escalation_api_key_env="ANTHROPIC_API_KEY"` (`config.py:298`–`:311`).

### Structured output
Every structured call is validated via `validate_against_schema` (pure-Python
JSON Schema subset) before the caller sees it; up to `max_retries` repair
attempts, then `StructuredOutputError` (`base.py:609`). `ModelTurn.tokens` is
always 0 on the `complete_structured` path (`AGENTS.md:160`).

### Providers not used at module scope
No provider is imported at module scope in a core path; `build_provider` does
lazy imports (`registry.py:334`). A minimal install boots without any provider
package.

## 2. Optional external substrates

| Substrate | Client | Default | Connection | Configured by |
|---|---|---|---|---|
| Qdrant (vector) | `qdrant-client` | embedded at `qdrant_path` | local path or `qdrant_url` | `StorageSettings.backend_vector`, `qdrant_path/url` |
| Kuzu (graph) | `kuzu` | embedded at `kuzu_path` | local path | `StorageSettings.backend_graph`, `kuzu_path` |
| Redis (queue/lock) | `redis` | — | `redis_url` | `StorageSettings.backend_queue`, `redis_url` |
| S3 (cold lake) | `boto3` | — | `s3_endpoint` | `StorageSettings.backend_coldlake`, `s3_*` |

All are optional extras; factories degrade, never raise, in `auto` mode
(`AGENTS.md:59`). Docker compose publishes **no host ports** for these; only
the ingestion API on `127.0.0.1:8787` (`AGENTS.md:132`).

S3 credentials: `s3_access_key_env` / `s3_secret_key_env` store env-var
**names** (default `PAA_MINIO_ACCESS_KEY` / `PAA_MINIO_SECRET_KEY`), never
values (`config.py:97`–`:99`).

## 3. Internal (in-process) communication

### Agent message protocol
`src/paa/agents/base.py` — `AgentMessage` (typed envelope: sender, recipient,
`intent`/`MessageType`, payload, `correlation_id`, `session_id`, `task_id`,
`recursion_depth`, `required_permissions`, `confidence`, `risk_level`).
`AgentResult` (ok, value, error, tokens_consumed, latency_ms, model_used,
escalated, confidence). Agents communicate via typed messages, not prose
(`base.py` module docstring, RFC §13).

### Delegation
`Agent.delegate()` → `ChiefOrchestrator.mediate_delegation()` → target
`Agent.run()`. The orchestrator owns the `DelegationGraph` per correlation and
enforces depth/cycle/node bounds before the callee runs (`delegation.py:87`).
Non-blocking at the topology level: the wait graph is a DAG
(`base.py:20`).

### Orchestrator → agents
The orchestrator sends `CONTEXT_REQUEST`, `PLAN_PROPOSAL`, `POLICY_CHECK`,
`EXECUTION_REQUEST`, `REVIEW_RESULT` messages and reads `AgentResult`
(`orchestrator.py:353`–`:607`).

## 4. Synchronous vs asynchronous communication

| Boundary | Mode | Durability |
|---|---|---|
| CLI/API → Runtime | async (awaited) | Ledger event before response |
| Orchestrator → agents | async `await agent.run()` | Events per phase |
| Agent → model router | async `complete_structured` | `MODEL_ESCALATED` logged before frontier call |
| Daemon → queue | claim/ack/nack | At-least-once; exactly-once via ledger idempotency |
| Runtime → storage | async DB calls | SQLite WAL transaction |
| Runtime → sandbox | async `sandbox.run()` | `SandboxResult` (total, incl. kills) |

There are no long-polling, streaming, or webhook endpoints. `paa serve` runs
uvicorn over the FastAPI app plus the daemon loops over one shared runtime
(`cli/main.py:266`).

## 5. Timeouts, retries, idempotency

- Model: `request_timeout_seconds=120`, `max_retries=2` (`config.py:326`).
- Structured output: `max_retries` repair attempts per provider (`base.py:567`).
- Task step: `max_step_retries=2` per step (`orchestrator.py:108`); `EXECUTION_STARTED`
  uses `attempt` + `discriminator="step-N"` for idempotency.
- Queue: `max_delivery_attempts=3`, `visibility_timeout_seconds=300` (`config.py:349`).
- Ledger: idempotency key suppresses duplicate redelivery; `attempt` permits
  genuine retries (`events.py:68`).

## 6. What is NOT integrated (honest gaps)

- No ingestion bridges (Telegram/email/Slack) — CLI/HTTP only (`README.md:169`).
- No webhooks.
- No service-to-service RPC beyond in-process agent messages.
- No API keys, OAuth, or mTLS on the ingestion API (single-user loopback).
- No pagination on `/ledger/{id}` (returns all events; capped at 10_000 for
  open-correlations count only).
- Marketplace/skills/coder layers import cleanly but are not wired into the
  orchestrator's main task path; no built-in file-edit/bash/web/git skills
  (`README.md:160`).

## Related documents

- [`interfaces/api-reference.md`](api-reference.md) — the FastAPI endpoints.
- [`modules/models.md`](../modules/models.md) — model layer deep dive.
- [`modules/storage.md`](../modules/storage.md) — optional substrates.
- [`modules/coder.md`](../modules/coder.md) — `paa agent` tool loop.
- [`operations/configuration-and-environment.md`](../operations/configuration-and-environment.md) — `PAA_MODELS__*` and `PAA_STORAGE__*` settings.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — external service diagram.
