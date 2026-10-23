# Module: Skills (`src/paa/skills/`)

## Module identity

- **Name:** Skills (Unified Skill Adapter)
- **Path:** `src/paa/skills/`
- **Main responsibility:** Discover, authorize, invoke, and validate skills via a single dispatch state machine; secret broker; Claw-Hub / MCP / native adapters.
- **Architectural layer:** Integration
- **Entrypoints:** `UnifiedSkillAdapter.dispatch()`, `SkillRegistry.register()/search()`
- **Public interfaces:** `SkillContract`, `SkillInvocation`, `SkillResult` (`contracts.py`); `SkillRegistry` (`registry.py`); `UnifiedSkillAdapter` (`usa.py`); `SecretBroker`, `SecretValue` (`secrets.py`); `SkillAdapter`, `SecretProvider` (`adapters/base.py`); `ClawHubAdapter`, `MCPClient`/`McpServerAdapter`, `NativeAdapter` (`adapters/`)
- **Other modules that depend on it:** None outside `src/paa/skills/` — skills is self-contained and not yet wired into the orchestrator's main task path (`__init__.py` consumers note)

## Purpose and responsibilities

RFC §8: a skill contract is the only thing trusted about a skill before
execution. The Unified Skill Adapter (`usa.py`) dispatch pipeline:
1. Discover/resolve contract.
2. Compile intent (input_schema validation).
3. Authorize: `required_permissions ⊆` mode grants, **before** sandbox boots.
4. Sandbox-mount + invoke via adapter.
5. Bound secret proxy (skill never receives the secret).
6. Validate output against `output_schema`.
7. Reliability adjustment (EWMA, 2:1 down:up ratio).

## Internal implementation

### `contracts.py` — SkillContract
Frozen pydantic v2 (corrected from RFC's v1 `regex=`/`@validator`, `skills/contracts.py:439`):
`skill_name` (pattern), `provider` (claw_hub/mcp_server/native/marketplace),
`version` (semver), `description`, `input_schema`/`output_schema`,
`risk_profile` [0,1], `required_permissions`, `reliability_weight`, `invocation`,
`source_uri`, `source_checksum`, `signature`. `key = f"{name}@{version}"`
(`skills/contracts.py:510`). `parse()` wraps Pydantic errors into `SkillContractError` without
echoing input data (`skills/contracts.py:565`).

### `skills/registry.py` — SkillRegistry
CRUD over `hot_serving_skill_registry`; idempotent on `(skill_name, version)`
(UNIQUE on `skill_name`; same version = no-op, different = in-place upgrade).
`search()`: semantic if vector store supplied, else trigram/`LIKE` + bounded
Jaccard re-rank; corrupt rows tolerated (logged, returned None) (`skills/registry.py:233`,
`:413`).

### `usa.py` — UnifiedSkillAdapter
`dispatch(skill_name, arguments, mode, sandbox, secret_broker, ...)` →
`SkillResult`. Raises `SkillContractError` (unknown/inactive skill, no adapter)
or `PermissionDeniedError` (missing permissions) before execution (`:142`).
`_BoundSecretProxy` delegates to broker with bound grants — adapter cannot
widen permissions (`:70`). `_finalise()` validates output via `SchemaValidator`
and nudges reliability (`:235`).

### `secrets.py` — SecretBroker
Host-side secret broker. Skills emit `get_secret` system call; host resolves
after `PERM_SECRET_READ` check (`skills/secrets.py:495`). Plaintext only in host process one
call; `SecretValue.__str__`/`__repr__`/`__format__` return `"***"` (`skills/secrets.py:129`).
Encryption: Fernet (AES-128-CBC + HMAC-SHA256, PBKDF2 200k) if `cryptography`
installed, else XOR dev fallback loudly warned (`skills/secrets.py:268`/`skills/secrets.py:305`). `lease()`
invalidates handle on exit (`skills/secrets.py:522`). Audit log of accesses/denials (`skills/secrets.py:580`).

### `skills/adapters/`
- `base.py`: `SkillAdapter` ABC (`discover`/`invoke`), `SecretProvider` protocol,
  adapter `SkillResult`.
- `native.py`: in-process callables (sync offloaded to thread; `get_secret`
  injected if signature declares it).
- `mcp.py`: embedded MCP client, JSON-RPC 2.0 over stdio; autodetects framing;
  `McpServerAdapter` runs start→initialize→work→close per operation.
- `claw_hub.py`: OpenClaw skill directories (`SKILL.md` + Python scripts);
  runs entrypoints under sandbox with skill dir mounted read-only; args via
  JSON file in argv (`--paa-args`), never env (`skills/adapters/claw_hub.py:78`).

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `USA.dispatch(skill, args, mode, ...)` | skill name + args + mode | `SkillResult` | may run sandboxed skill; updates reliability |
| `SkillRegistry.register(contract)` | `SkillContract` | `SkillContract` | upserts `hot_serving_skill_registry` |
| `SkillRegistry.search(intent)` | intent string | list of contracts | read only |
| `SecretBroker.get_secret(name, granted, ...)` | name + grants | `SecretValue` | audit log entry |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `USA` → `SkillRegistry` | `discover()` | Find contract | `usa.py:132` |
| `USA` → adapter | `invoke()` | Run skill | `usa.py:142` |
| `USA` → `SecretBroker` | bound proxy | Skills never get secrets | `usa.py:142` |
| `USA` → `SchemaValidator` | `validate_tool_output()` | Validate skill output | `usa.py:235` |
| `ClawHubAdapter` → `Sandbox` | `run(spec)` | Contained skill entrypoint | `claw_hub.py:435` |
| `MCPClient` → subprocess | stdio JSON-RPC | MCP server session | `mcp.py` |

## Runtime behavior

- `cryptography` imported lazily (never at module scope) — XOR fallback keeps
  minimal install importable (`secrets.py:205`).
- Adapter `invoke` returns adapter `SkillResult`; USA wraps into runtime
  `SkillResult` with `output_valid`/`schema_errors` (`usa.py:235`).

## Failure behavior

- Unknown/inactive skill or no adapter → `SkillContractError` (`usa.py:142`).
- Missing permissions → `PermissionDeniedError` (`usa.py:142`).
- Corrupt registry row → logged, returned as None (`skills/registry.py:413`).
- Adapter invoke exceptions → `ok=False` in `SkillResult` (samples for
  optimiser, `usa.py` docstring).

## Modification impact

- Adding an adapter: implement `SkillAdapter`, register in `USA._adapters` by
  `provider` name.
- Changing `required_permissions` enforcement affects all skill invocations.
- Tests: `tests/skills/` (51 tests per README).

## Known gaps

- Skills is self-contained; no external consumers in `src/` beyond the package
  itself — not wired into the orchestrator's main task path.
- No marketplace publishing UI or user/payment model yet (`README.md:174`).

## Related documents

- [`modules/storage.md`](storage.md) — `hot_serving_skill_registry`.
- [`modules/sandbox.md`](sandbox.md) — Claw-Hub containment.
- [`modules/validation.md`](validation.md) — `SchemaValidator` for output.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — skills not yet in main path.
- [`README.md:174` marketplace gaps](../../README.md)
