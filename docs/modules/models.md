# Module: Models (`src/paa/models/`)

## Module identity

- **Name:** Models
- **Path:** `src/paa/models/`
- **Main responsibility:** Provider-agnostic inference contract, provider registry, and local-first escalation router. Agents depend on the `ModelLike` protocol, never on a provider (`AGENTS.md:175`).
- **Architectural layer:** Integration
- **Entrypoints:** `build_provider()`, `build_local_inference()`, `EscalatingModelRouter.complete/complete_structured`, `ModelProvider.complete/healthcheck/complete_structured`
- **Public interfaces:** `ModelProvider`, `CompletionRequest`, `CompletionResponse`, `Message`, `ModelTier`, `validate_against_schema`, `redact`, `extract_json_object`, `minimal_instance_for_schema` (`base.py`); `build_provider`, `resolve_spec`, `KNOWN_PROVIDERS`, `provider_names` (`registry.py`); `EscalatingModelRouter`, `EscalationDecision`, `resolve_model_settings` (`router.py`); `get_embedder` (`embeddings.py`, a re-export of `paa.storage.vector.embeddings.get_embedder`)
- **Other modules that depend on it:** `runtime.py` (RouterModelAdapter + router), `agents/reasoning.py` (via ModelLike), `context/` (embedder), `coder/` (RouterToolCallingModel)

## Purpose and responsibilities

Provider-agnostic and local-first with explicit, ledger-logged escalation
(ADR-0007/0015). Swapping the model is a config change, not a rewrite
(`base.py:13`). The model seam is `ModelLike.complete_structured(prompt,
schema)`; `RouterModelAdapter` (`runtime.py:108`) and `RouterToolCallingModel`
(`coder/model_adapter.py:123`) are the only two places that know about
`EscalatingModelRouter`.

No heavy optional dependency is imported at module scope in a core path —
the provider classes are, and they need only httpx (`AGENTS.md:85`).

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
`KNOWN_PROVIDERS`: 29 `ProviderSpec` rows (OpenAI family + ollama/anthropic/
gemini/echo). Each: `name`, `kind`, `default_model`, `default_base_url`,
`api_key_env` (tuple), `default_tier` (LOCAL/FRONTIER), `is_local`.
`build_provider()` constructs with lazy imports; `build_local_inference()` for
localhost backends. `resolved_api_key()`: env first, then `.env` via
`dotenv_value` — never `os.environ` (`registry.py:104`).

### `router.py` — EscalatingModelRouter
Policy, in order (`router.py:13`):
1. Escalation forbidden by mode? (`LOCKDOWN` → raise, never escalate.)
2. Modality ≥ `escalate_at_or_above`?
3. Try local; on `ModelUnavailableError`/`StructuredOutputError` → escalate.
Semaphore caps `max_concurrent_streams=2` (memory bound, `router.py:229`). Every
escalation writes `MODEL_ESCALATED` ledger event **before** the frontier call
(`router.py:452`), with `leaves_machine = frontier.tier >= FRONTIER` and the
per-router sequence number as the event's `discriminator`, so two escalations
in one lineage are two events rather than one suppressed duplicate. Escalation
without a ledger store logs a warning (unaudited, `router.py:628`).

**Configuration resolution** (`router.py:90`, `resolve_model_settings`): the
router and `get_model_router` accept either a full `Settings` or its `models`
half, told apart by `isinstance` rather than by a `getattr(settings, "models",
settings)` fallback — the fallback cannot be type-checked, because the `models`
attribute exists on one member of the union and not the other, and it cannot
distinguish "no configuration" from "a value that happens to be falsy".
Anything that is neither is **refused** with a `TypeError` rather than
silently replaced by the defaults. The defaults are not a neutral answer for a
router: what this configuration carries is the *escalation policy*, so a
deployment that configured "never escalate" and handed over a falsy value
would have run on the default policy instead, and escalated.

### Providers
`openai_compatible_provider.py`, `ollama_provider.py`, `anthropic_provider.py`,
`gemini_provider.py`, `echo_provider.py`, `embeddings.py`, `_http.py`.

Every provider's constructor takes `ModelSettings.max_transport_retries`
(ADR-0021), and so do the fast paths in `__init__.py` and the registry rows in
`registry.py`. A provider that does not retry a transient 429 ends the whole
run, and escalation policy can forbid the fallback that would have absorbed
it. `embeddings.py` is a re-export only: it forwards to
`paa.storage.vector.embeddings.get_embedder` unchanged, so the two entry
points share one contract — a `Settings` or a `ModelSettings`, and nothing
else. The public signature was narrowed from `Any` to that union when the
implementation switched from the `getattr` fallback to an `isinstance`
resolution; the undocumented tolerance that fallback carried (a
`Settings`-shaped proxy exposing only `.models`) went with it.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `Router.complete_structured(request, schema, modality, ...)` | request + schema + routing ctx | dict | `MODEL_ESCALATED` ledger event if escalating |
| `ModelProvider.complete(request)` | `CompletionRequest` | `CompletionResponse` | network call (unless local) |
| `ModelProvider.healthcheck()` | — | bool | never raises |
| `build_provider(name, ...)` | provider name | `ModelProvider` | none (no I/O at construction) |
| `get_model_router(settings, ledger_store)` | `Settings` or `ModelSettings` | `EscalatingModelRouter` | none (providers do no I/O at construction); `TypeError` if `settings` is neither |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `RouterModelAdapter` → `EscalatingModelRouter` | `complete_structured()` | Bridge agents' ModelLike to router | `runtime.py:131` |
| `Reasoning agents` → `ModelLike` | `complete_structured()` | Model seam | `reasoning.py:26` |
| `Router` → local/frontier `ModelProvider` | `complete/complete_structured` | Inference + escalation | `router.py:341`, `:376` |
| `Router` → `LedgerStore` | `append(MODEL_ESCALATED)` | Audit escalation | `router.py:654` |
| `Context builders` → embedder | `embed()` | Vector search | `context_agents.py:247` |
| `Registry` → `dotenv_value` | read provider API key | Credentials without os.environ | `registry.py:104` |

## Runtime behavior

- `temperature=0.0` default — sampling variance would break replay
  determinism (`base.py:202`).
- `max_concurrent_streams=2` — prevents swap on 3.5 GB machines
  (`router.py:29`).
- Escalation only when policy permits; local failure under LOCKDOWN raises
  (air-gap promise kept) (`router.py:541`).

## Failure behavior

- `ModelUnavailableError` — provider could not serve (routable; triggers
  escalation consideration) (`base.py:69`).
- `StructuredOutputError` — answered but not schema-conformant (carries
  `schema_errors`, redacted excerpt) (`base.py:86`).
- Healthcheck failures return `False`, never raise (`base.py:600`).
- Provider absent → `UnknownProviderError` lists valid names
  (`registry.py:59`).
- A `settings` argument that is neither a `Settings` nor a `ModelSettings` →
  `TypeError` at the boundary (`router.py:90`). Deliberate: the alternative
  was the defaults, and the defaults carry the *escalation policy*.

## Modification impact

- Adding a provider = add a `ProviderSpec` row to `registry.py:_SPECS` (+ a
  provider class if new `kind`) — data, not code (`registry.py:148`).
- Changing `escalate_at_or_above` changes when escalation occurs
  (`config.py:491`).
- Changing `canonical_json`/schema validation affects model output parsing
  (`base.py`).
- Changing how the model configuration is *resolved* (which object counts as
  the configuration) touches `router.py:90` and every call site that passes
  one — the resolution is deliberately a refusal, not a fallback.
- Tests: `tests/models/` (135 tests).

## Known gaps

- `ModelTurn.tokens` is always 0 on `complete_structured` path — `max_tokens`
  budget never trips; `max_steps` bounds runs instead (`AGENTS.md:322`).
- EchoProvider cannot drive a plan (minimal-schema output isn't a plan)
  (`AGENTS.md:191`).
- A `Settings` whose `models` field was *reassigned* to a non-`ModelSettings`
  after construction is not caught by the resolution: pydantic does not
  validate on assignment, and the type checker cannot see a runtime
  reassignment, so the misconfiguration surfaces as an `AttributeError` on a
  policy field rather than as the `TypeError` the boundary raises. Catching it
  would need a `type: ignore` or an `Any` cast, and neither is worth it for a
  case that requires deliberately breaking a validated field.

## Related documents

- [`modules/coder.md`](coder.md) — `RouterToolCallingModel` bridges loop to router.
- [`interfaces/integrations-and-communication.md`](../interfaces/integrations-and-communication.md) — provider list and keys.
- [`operations/configuration-and-environment.md`](../operations/configuration-and-environment.md) — `PAA_MODELS__*` settings.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — escalation diagram.
- [`docs/adr/0007` capability ceiling](../adr/0008-idempotency-key-with-attempt.md) (ADR list: 0007 is local-first escalation; only 0008/0010/0015/0019/0020 exist on disk)
- [`docs/adr/0015-capability-ceiling.md`](../adr/0015-capability-ceiling.md)
