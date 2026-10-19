# Module: Models (`src/paa/models/`)

## Module identity

- **Name:** Models
- **Path:** `src/paa/models/`
- **Main responsibility:** Provider-agnostic inference contract, provider registry, and local-first escalation router. Agents depend on the `ModelLike` protocol, never on a provider (`AGENTS.md:101`).
- **Architectural layer:** Integration
- **Entrypoints:** `build_provider()`, `build_local_inference()`, `EscalatingModelRouter.complete/complete_structured`, `ModelProvider.complete/healthcheck/complete_structured`
- **Public interfaces:** `ModelProvider`, `CompletionRequest`, `CompletionResponse`, `Message`, `ModelTier`, `validate_against_schema`, `redact`, `extract_json_object`, `minimal_instance_for_schema` (`base.py`); `build_provider`, `resolve_spec`, `KNOWN_PROVIDERS`, `provider_names` (`registry.py`); `EscalatingModelRouter`, `EscalationDecision` (`router.py`)
- **Other modules that depend on it:** `runtime.py` (RouterModelAdapter + router), `agents/reasoning.py` (via ModelLike), `context/` (embedder), `coder/` (RouterToolCallingModel)

## Purpose and responsibilities

Provider-agnostic and local-first with explicit, ledger-logged escalation
(ADR-0007/0015). Swapping the model is a config change, not a rewrite
(`base.py:13`). The model seam is `ModelLike.complete_structured(prompt,
schema)`; `RouterModelAdapter` is the only place that knows about
`EscalatingModelRouter` (`runtime.py:98`).

No provider is imported at module scope in a core path — heavy deps are
optional extras (`AGENTS.md:60`).

## Internal implementation

### `base.py` — contract
- `CompletionRequest`/`CompletionResponse`/`Message` wire types; temperature
  defaults 0.0 for replay determinism (`base.py:202`).
- `ModelProvider` ABC: `complete()`, `healthcheck()` (must never raise,
  `:600`), `complete_structured()` with validate-and-repair up to `max_retries`
  then `StructuredOutputError` (`:609`).
- `validate_against_schema()` — pure-Python JSON Schema subset; unsupported
  keywords ignored, never guessed (`:320`).
- `redact()` — scrubs `sk-`, Bearer, `api_key=` patterns from errors/logs
  (`:139`).
- `extract_json_object()` — pulls JSON from fenced/markdown-wrapped responses
  (`:514`).
- `minimal_instance_for_schema()` — EchoProvider's offline schema honouring
  (`:445`).

### `registry.py` — provider registry
`KNOWN_PROVIDERS`: ~20 `ProviderSpec` rows (OpenAI family + ollama/anthropic/
gemini/echo). Each: `name`, `kind`, `default_model`, `default_base_url`,
`api_key_env` (tuple), `default_tier` (LOCAL/FRONTIER), `is_local`.
`build_provider()` constructs with lazy imports; `build_local_inference()` for
localhost backends. `resolved_api_key()`: env first, then `.env` via
`dotenv_value` — never `os.environ` (`registry.py:104`).

### `router.py` — EscalatingModelRouter
Policy, in order (`router.py:203`):
1. Escalation forbidden by mode? (`LOCKDOWN` → raise, never escalate.)
2. Modality ≥ `escalate_at_or_above`?
3. Try local; on `ModelUnavailableError`/`StructuredOutputError` → escalate.
Semaphore caps `max_concurrent_streams=2` (memory bound, `:150`). Every
escalation writes `MODEL_ESCALATED` ledger event **before** the frontier call
(`:417`), with `leaves_machine = frontier.tier >= FRONTIER`. Escalation without
a ledger store logs a warning (unaudited, `:460`).

### Providers
`openai_compatible_provider.py`, `ollama_provider.py`, `anthropic_provider.py`,
`gemini_provider.py`, `echo_provider.py`, `embeddings.py`, `_http.py`.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `Router.complete_structured(request, schema, modality, ...)` | request + schema + routing ctx | dict | `MODEL_ESCALATED` ledger event if escalating |
| `ModelProvider.complete(request)` | `CompletionRequest` | `CompletionResponse` | network call (unless local) |
| `ModelProvider.healthcheck()` | — | bool | never raises |
| `build_provider(name, ...)` | provider name | `ModelProvider` | none (no I/O at construction) |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `RouterModelAdapter` → `EscalatingModelRouter` | `complete_structured()` | Bridge agents' ModelLike to router | `runtime.py:130` |
| `Reasoning agents` → `ModelLike` | `complete_structured()` | Model seam | `reasoning.py:95` |
| `Router` → local/frontier `ModelProvider` | `complete/complete_structured` | Inference + escalation | `router.py:332`, `:354` |
| `Router` → `LedgerStore` | `append(MODEL_ESCALATED)` | Audit escalation | `router.py:486` |
| `Context builders` → embedder | `embed()` | Vector search | `context_agents.py:186` |
| `Registry` → `dotenv_value` | read provider API key | Credentials without os.environ | `registry.py:116` |

## Runtime behavior

- `temperature=0.0` default — sampling variance would break replay
  determinism (`base.py:202`).
- `max_concurrent_streams=2` — prevents swap on 3.5 GB machines
  (`router.py:33`).
- Escalation only when policy permits; local failure under LOCKDOWN raises
  (air-gap promise kept) (`router.py:356`).

## Failure behavior

- `ModelUnavailableError` — provider could not serve (routable; triggers
  escalation consideration) (`base.py:69`).
- `StructuredOutputError` — answered but not schema-conformant (carries
  `schema_errors`, redacted excerpt) (`base.py:86`).
- Healthcheck failures return `False`, never raise (`base.py:600`).
- Provider absent → `UnknownProviderError` lists valid names
  (`registry.py:67`).

## Modification impact

- Adding a provider = add a `ProviderSpec` row to `registry.py:_SPECS` (+ a
  provider class if new `kind`) — data, not code (`registry.py:29`).
- Changing `escalate_at_or_above` changes when escalation occurs
  (`config.py:317`).
- Changing `canonical_json`/schema validation affects model output parsing
  (`base.py`).
- Tests: `tests/models/` (71 tests per README).

## Known gaps

- `ModelTurn.tokens` is always 0 on `complete_structured` path — `max_tokens`
  budget never trips; `max_steps` bounds runs instead (`AGENTS.md:160`).
- EchoProvider cannot drive a plan (minimal-schema output isn't a plan)
  (`AGENTS.md:111`).

## Related documents

- [`modules/coder.md`](coder.md) — `RouterToolCallingModel` bridges loop to router.
- [`interfaces/integrations-and-communication.md`](../interfaces/integrations-and-communication.md) — provider list and keys.
- [`operations/configuration-and-environment.md`](../operations/configuration-and-environment.md) — `PAA_MODELS__*` settings.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — escalation diagram.
- [`docs/adr/0007` capability ceiling](../adr/0008-idempotency-key-with-attempt.md) (ADR list: 0007 is local-first escalation; only 0008/0010/0015/0019/0020 exist on disk)
- [`docs/adr/0015-capability-ceiling.md`](../adr/0015-capability-ceiling.md)
