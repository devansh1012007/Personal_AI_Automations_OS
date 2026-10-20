# ADR-0020: Provider API keys resolve from `.env`, not only from the process environment

## Status

Accepted. Correctness class — the configuration workflow this repository
documents did not work.

## Context

`config.py` reads its settings with pydantic-settings, configured with
`env_file=".env"` and documented (in `AGENTS.md` and in the header of
`.env.example`) as reading from the **current working directory**. Following
that instruction is exactly what `.env.example` tells you to do: copy the file,
fill in your credentials, run the tool.

A provider API key does not fit that mechanism, because it is not a `Settings`
field. `ModelSettings` stores `escalation_api_key_env` — the *name* of a
variable — and `registry.ProviderSpec.api_key_env` names one or more variables
per registry row. That indirection is deliberate and worth keeping: the same
reason `StorageSettings` holds `s3_secret_key_env` rather than the key is
that a credential must not be able to end up in config, in the ledger, or in an
image layer. So the value is read out of the environment at build time:

```python
# registry.resolved_api_key, before
for name in self.api_key_env:
    if value := os.environ.get(name):
        return value
return None
```

The two halves never met. pydantic-settings reads `.env` into the settings
object; it does **not** write anything into `os.environ`. Nothing in the codebase
called `load_dotenv()`. A key placed in `.env` was therefore read by config's
layer and invisible to the model layer's, and the failure mode is the worst
kind: not a configuration error but an HTTP 401 from the provider —
`No cookie auth credentials found` — which reads as "your key is wrong" when
the key was never read at all.

The bug is easy to miss because a shell `export` works fine. It only bites the
documented path.

## Decision

`config.dotenv_value(name)` parses the CWD `.env` and returns one variable's
value. `registry.resolved_api_key` checks `os.environ` first, then falls back to
it. `config.reset_dotenv_cache()` drops the parse, for tests that change
directory.

The file is **not** merged into `os.environ`. That was the obvious fix and it is
the wrong one: doing so would inject every `PAA_*` line into the process
environment, where pydantic-settings would then see them as *real* environment
variables rather than file-sourced ones — including in test runs, whose whole
isolation strategy is setting environment variables and calling
`reset_settings_cache()`. A read-only lookup on demand has none of that blast
radius, and it keeps the precedence the config module already documents:
explicit kwargs > environment > `.env` > defaults.

`python-dotenv` was already installed as a transitive dependency of
pydantic-settings. It is now a declared one, because this code imports it
directly and the project rule is that nothing may be relied on that is not
declared.

## Consequences

- `OPENROUTER_API_KEY=...` in `.env` now actually authorises requests. Verified
  end to end: `paa agent --mode plan --yes "which backends are live"` reaches a
  committed answer on `nvidia/nemotron-3-ultra-550b-a55b:free` with no `export`.
- Exported variables still win, so nothing that worked before changes.
- `.env` is read from `os.getcwd()`, matching pydantic-settings' own
  behaviour here. A process that changes directory after first lookup will keep
  the original file's values; `reset_dotenv_cache()` exists for that case.
- Anything else configured by *name* rather than by field should use
  `dotenv_value` too. This is recorded in `AGENTS.md` so the next such case does
  not reintroduce the split.
- Covered by `tests/models/test_registry.py::TestApiKeyResolution`, including
  that environment beats `.env`, that a commented-out key is not a key, and that
  an empty `KEY=` reads as unset rather than as an empty credential.
