# API reference

PAA exposes a single HTTP surface: the **FastAPI ingestion API**, loopback-only
on `127.0.0.1:8787`. It is a type-safe parser and validator edge only — it
holds no application state, makes no model calls, and writes directly to neither
operational tables nor the ledger; a request becomes either a ledger event (via
the orchestrator) or a cold-lake row (via the signal repository) (`api/app.py:8`).

The app is built by `create_app(runtime)` (`src/paa/api/app.py:50`) and imported
lazily (FastAPI is imported inside the function) so the package imports on a
minimal install.

## Base configuration

| Setting | Source | Default |
|---|---|---|
| Bind host | `Settings.api_host` / `--host` flag | `127.0.0.1` (loopback enforced by validator unless `PAA_ALLOW_NON_LOOPBACK=1`) |
| Bind port | `Settings.api_port` / `--port` flag | `8787` |

`api_host` validator: `config.py:409` (`_enforce_loopback`).

## Endpoints

### `GET /health`
**Handler:** `api/app.py:67`
**Response:** `HealthResponse` (`api/schemas.py:96`)
```json
{
  "status": "ok",
  "version": "<__version__>",
  "backends": {
    "vector": "<type name or null>",
    "graph": "<type name or null>",
    "queue": "<type name or null>",
    "model_router": "<type name or null>"
  },
  "open_tasks": <int>
}
```
`open_tasks` = `len(await runtime.ledger.open_correlations(limit=10_000))`.

### `POST /tasks`
**Handler:** `api/app.py:85`
**Request:** `TaskRequestBody` (`api/schemas.py:28`)
```json
{
  "goal": "string (min 1)",
  "modality": "SIMPLE|STANDARD|COMPLEX|MAX | null",
  "required_slots": ["string", ...] | null,
  "workspace_path": "string | null",
  "target_agent": "string | null",
  "run": false
}
```
**Behavior:**
1. Calls `_admit(runtime, essential=False)`; if the queue is deep (SHEDDING),
   returns **HTTP 429** "runtime under backpressure; retry shortly"
   (`api/app.py:87`).
2. `runtime.submit(goal, modality, required_slots, workspace_path,
   target_agent)` → writes `TASK_REQUESTED` + `TASK_QUEUED`, enqueues to the
   queue, returns `correlation_id` (`runtime.py:189`, `orchestrator.py:188`).
3. If `body.run` is true: `runtime.run(cid)` drives to terminal; returns
   `queued=False` and the final phase. Otherwise returns `queued=True`,
   `phase="QUEUED"`.

**Response:** `TaskSubmitResponse` (`api/schemas.py:43`)
```json
{ "correlation_id": "uuid", "phase": "QUEUED|<final phase>", "queued": bool }
```

### `GET /tasks/{correlation_id}`
**Handler:** `api/app.py:104`
**Response:** `TaskStatusResponse` (`api/schemas.py:49`)
```json
{
  "correlation_id": "uuid",
  "phase": "CREATED|QUEUED|...|COMMITTED|...",
  "modality": "STANDARD|...",
  "plan_steps": <int>,
  "completed_steps": [int, ...],
  "current_step_index": <int>,
  "attempts": <int>,
  "tokens_consumed": <int>,
  "awaiting_reason": "string | null",
  "policy_reason": "string | null",
  "errors": [ {...} ],
  "is_terminal": bool
}
```
**Behavior:** `runtime.ledger.head(cid)` → 404 if None; `runtime.project(cid)`
for the projection. Invalid UUID → 400.

### `POST /tasks/{correlation_id}/gate`
**Handler:** `api/app.py:126`
**Request:** `GateRequest` (`api/schemas.py:64`) `{ "approved": bool, "note": "string|null" }`
**Behavior:** `runtime.orchestrator.clear_human_gate(cid, approved, note)`.
409 if the task is not awaiting human attestation (`PaaError`). Returns the
updated task status.

Only an external caller reaches this; nothing in the automated path can clear
its own gate (`orchestrator.py:688`).

### `POST /tasks/{correlation_id}/correction`
**Handler:** `api/app.py:139`
**Body:** `{ "correction": "string" }` (arbitrary dict; reads `.get("correction", "")`)
**Behavior:** `runtime.orchestrator.record_correction(cid, correction)` → writes
`USER_CORRECTION` event. Returns 202 `{"status": "recorded"}`.

### `GET /ledger/{correlation_id}`
**Handler:** `api/app.py:146`
**Response:** list of `LedgerEventView` (`api/schemas.py:85`)
```json
[
  { "sequence_id": int|null, "state_version": int|null, "event_type": "string",
    "agent_role": "string|null", "recorded_at": "iso", "payload": {...} }
]
```
**Behavior:** `runtime.ledger.read_correlation(cid)`; 404 if empty. This is the
explainability endpoint — walk the causal chain backwards via `causation_id`.

### `POST /signals`
**Handler:** `api/app.py:165`
**Request:** `SignalRequest` (`api/schemas.py:71`)
```json
{ "channel": "string (min 1)", "payload": {...}, "external_id": "string|null" }
```
**Response:** `SignalResponse` (`api/schemas.py:79`)
```json
{ "signal_id": "uuid", "channel": "string", "duplicate": bool }
```
**Behavior:**
1. `_admit(runtime, essential=False)` → 429 under backpressure.
2. Builds `SignalRepository` over `runtime.db` + cold-lake CAS
   (`_signal_repo`, `api/app.py:219`); 503 if cold lake not configured.
3. If `external_id` set, checks `get_by_external_id` for a pre-existing row
   (`duplicate` flag).
4. `repo.record(channel, payload, external_id)` → idempotent insert.

## Request/response schemas (pydantic)

Defined in `src/paa/api/schemas.py` (plain pydantic, importable without
FastAPI):

| Schema | Fields | File |
|---|---|---|
| `TaskRequestBody` | goal, modality?, required_slots?, workspace_path?, target_agent?, run (bool, default false) | `schemas.py:28` |
| `TaskSubmitResponse` | correlation_id, phase, queued | `schemas.py:43` |
| `TaskStatusResponse` | correlation_id, phase, modality, plan_steps, completed_steps, current_step_index, attempts, tokens_consumed, awaiting_reason?, policy_reason?, errors[], is_terminal | `schemas.py:49` |
| `GateRequest` | approved, note? | `schemas.py:64` |
| `SignalRequest` | channel, payload, external_id? | `schemas.py:71` |
| `SignalResponse` | signal_id, channel, duplicate | `schemas.py:79` |
| `LedgerEventView` | sequence_id?, state_version?, event_type, agent_role?, recorded_at, payload | `schemas.py:85` |
| `HealthResponse` | status, version, backends{}, open_tasks | `schemas.py:96` |

## Error responses

| Status | Condition | Source |
|---|---|---|
| 400 | Invalid correlation_id UUID | `_parse_uuid` (`api/app.py:232`) |
| 404 | No such task (head is None / no events) | `task_status` (`api/app.py:109`), `ledger` (`api/app.py:150`) |
| 409 | Task not awaiting human gate | `clear_gate` (`api/app.py:135`) |
| 429 | Queue under backpressure (SHEDDING) | `_admit` (`api/app.py:87`, `:168`) |
| 503 | Cold lake not configured | `ingest_signal` (`api/app.py:172`) |

Backpressure: essential control-plane traffic is always admitted; only
discretionary ingestion (`/tasks` without `run`? actually all task submission and signals)
is shed when depth ≥ `shed_load_depth` (`api/app.py:198`, `backpressure.py:115`).

## Authentication / authorization

None. The API is single-user, loopback-only. Access control is enforced by
`Settings.api_host` loopback validation (`config.py:409`) and the network
topology (Docker publishes no host ports; only the ingestion API on
`127.0.0.1:8787`, `AGENTS.md:132`).

## Verification status

- All seven endpoints are implemented with handlers in `api/app.py`.
- `create_app` is tested via `tests/api/` (27 tests per README).
- The API does not implement pagination, rate limiting beyond backpressure, or
  API keys — these are absent by design (single-user, loopback).

## Related documents

- [`interfaces/integrations-and-communication.md`](integrations-and-communication.md) — external integrations and model providers.
- [`backend/request-lifecycle.md`](../backend/request-lifecycle.md) — what happens after `POST /tasks`.
- [`operations/configuration-and-environment.md`](../operations/configuration-and-environment.md) — `PAA_API_*` settings.
- [`modules/cli.md`](../modules/cli.md) — the CLI surface (the other entry point).
