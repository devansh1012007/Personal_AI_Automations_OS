# Module: Observability (`src/paa/observability/`)

## Module identity

- **Name:** Observability
- **Path:** `src/paa/observability/`
- **Main responsibility:** Logging (structlog), metrics, tracing, and execution runs. **Built but untested** — exercised nowhere (`AGENTS.md:97`).
- **Architectural layer:** Cross-cutting / observability
- **Entrypoints:** structlog configuration; metrics registry; trace recording; run recording
- **Public interfaces:** `logging.py`, `metrics.py`, `tracing.py`, `runs.py` (specific public symbols to be confirmed by inspection)
- **Other modules that depend on it:** `runtime.py` (structlog loggers throughout); essentially every module uses `structlog.get_logger(__name__)`

## Purpose and responsibilities

RFC §10 tracing and metrics. `ObservabilitySettings.enabled` (default `True`),
`service_name` ("paa-runtime"), `otlp_endpoint` (when None, traces recorded to
local ledger only — no egress, `config.py:581`), `log_level`, `log_json`,
`trace_retention_days` (30).

## Internal implementation

- `logging.py`: structlog configuration.
- `metrics.py`: metrics registry (provider usage consumed from `router.token_usage()`).
- `tracing.py`: tracing.
- `runs.py`: execution runs over `Database` (`hot_serving_execution_runs`).

Every module logs via `structlog.get_logger(__name__)` (e.g. `runtime.py:53`,
`orchestrator.py:50`, `ledger/store.py:37`, `ledger/recovery.py:48`).

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| All modules → structlog | `get_logger(__name__)` | Structured logging | pervasive |
| `Router` → metrics | `token_usage()` | Per-provider counters | `router.py:175` |
| `Observability runs` → `Database` | run rows | Execution traces | `runs.py:32` |

## Runtime behavior

- Logging is the primary observability surface; no metrics export confirmed.
- `otlp_endpoint=None` means no egress — traces stay in the local ledger
  (`config.py:581`).

## Failure behavior

- Untested; no test coverage (`tests/observability/` absent, `AGENTS.md:97`).

## Modification impact

- `ObservabilitySettings` fields consumed by logging/tracing setup; specifics
  not fully traced in source inspection.

## Known gaps

- **Zero test coverage** — `src/paa/observability/` is exercised nowhere
  (`AGENTS.md:97`).
- Behavior beyond `structlog` logging and `ObservabilitySettings` defaults was
  not fully verifiable from the inspected files; treat as *partially verified*.

## Related documents

- [`operations/configuration-and-environment.md`](../operations/configuration-and-environment.md) — `PAA_OBSERVABILITY__*` settings.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — observability position.
