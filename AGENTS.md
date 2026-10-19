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
  triggered line by widening the ignore. A single line that legitimately interpolates SQL takes a
  **per-line** `# noqa: S608` (the convention in `memory/facts.py` and `skills/registry.py`) — that is
  narrower than a per-file allow-list and keeps the rest of the file under the rule.
- **The baseline is red.** `ruff` reports 69 findings and `mypy` 116 at HEAD, all pre-existing; so
  does the suite, with 6 environmental failures (the sandbox cannot exec a bare `python` binary).
  "Do not expand the count" is the gate, not "make it zero" — fixing the 185 pre-existing findings is
  a project of its own and would bury any change you are making. Do not silence one either.
- There is no CI config, Makefile, or pre-commit hook in this repo. Nothing enforces the order above
  but you.

## The one invariant: the ledger is the truth

`src/paa/ledger/` is the spine; Redis, queues, vector/graph indexes, and the filesystem are all derived
views rebuilt by replay (`ledger/replay.py`). Two rules follow and are not negotiable:

1. **Ledger event before side effect, always.** A crash between event and work is recoverable; the
   reverse is not. See the task lifecycle diagram in `README.md`.
2. **Everything else must be replayable.** If you add state that isn't derivable from events, it needs
   a snapshot *and* a recovery rule (`ledger/store.py::save_snapshot`/`load_snapshot` for the snapshot,
   `ledger/recovery.py` for the sweep rule).

Adding an `EventType` means touching, at minimum: `core/types.py` (`EventType`, plus `TERMINAL_EVENTS`
if it ends a lineage), `ledger/replay.py` (`_PHASE_MAP` and the projection `match`, or `_NO_PHASE_CHANGE`
if it is informational), and its emitter — `agents/orchestrator.py` for lifecycle events,
`ledger/recovery.py` for recovery and rollback events, `models/router.py` for `MODEL_ESCALATED`.
`ledger/store.py` assigns `state_version`, links `prev_hash`, and updates the head projection in one
transaction — never append outside it.

`EXECUTION_COMPLETED` fires **per step**, not per task (ADR-0010); it is not terminal.

## Storage is a polyglot with a parity lock

`storage/relational/schema_sqlite.sql` and `schema_postgres.sql` must stay table- and column-identical
(types may differ: UUID/JSONB/TIMESTAMPTZ vs TEXT/INTEGER). `tests/storage/test_schema_parity.py`
parses both with a small DDL lexer and fails the build on any divergence. If you add a column, add it
to **both** files.

Every substrate has an embedded default and a server variant, selected by `PAA_*` env vars
(`StorageSettings.backend_*`). The factories degrade, they never raise — `runtime.py::_try_vector /
_try_graph /_try_queue` and `runtime.py::_acquire_sandbox` (5s probe timeout, then subprocess) are the
pattern. Heavy deps (qdrant, kuzu, sentence-transformers, redis, psycopg, boto3) are optional extras;
**never** import one at module scope in a core path, or a minimal install stops importing.

### Every LIKE pattern comes from one helper

A `LIKE` pattern is the one place where a *value* changes the *meaning* of a query. `%` matches any run
of characters, `_` matches any one, and a NUL ends the pattern where SQLite reads it through the C
string API — so `%\x00%` is a bare `%` that matches every row. A keyword that happens to contain one
is that keyword, not a pattern.

**Every `LIKE` pattern that interpolates text comes from `storage/relational/like.py`** —
`like_pattern(term)` for the pattern, `like_predicate(*columns)` for the SQL half, and
`escape_like` underneath them. Not from an f-string at the call site. Three facts are stated once
there and are not to be rediscovered per site:

- SQLite's `LIKE` has **no default escape character**; PostgreSQL's defaults to a backslash. The same
  escaped pattern is wrong on one engine and right on the other unless the SQL declares `ESCAPE '\'`.
  The escape character and the clause are derived from one constant, so they cannot drift.
- A **NUL truncates the pattern**, and the escape clause does not help — a NUL is not a metacharacter,
  it is the end of the string.
- A term with **nothing to search for** is refused, not compiled: the pattern would be `%%`, which
  matches every row, and there is no `LIKE` pattern that matches nothing instead.

The policy on a NUL is the caller's, and both are deliberate: `memory/facts.py` refuses it (the query
is a model-supplied tool argument, so the refusal goes back to the model as an error value), and the
internal recall paths drop it and carry on. ADR-0023 has the full reasoning and the consequence at
each site.

**A `LIMIT` cut needs a total order.** `ORDER BY` on a non-unique column leaves the cut to an order
the SQL does not define, so a `LIMIT` over it is not reproducible by the standard — add a unique
secondary key (`f.id`). SQLite happens to be deterministic for a fixed plan, but that is an
implementation detail, and PostgreSQL promises nothing about ties at all.

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
  arbitrary event orderings, confidence in [0,1], LIKE-escaping losslessness). Extend those rather
  than writing example-based tests for them. The files that do: `tests/context/test_budget.py`,
  `test_compaction.py`, `test_gatherer.py`, `test_metrics.py`, `tests/ledger/test_replay.py`,
  `tests/memory/test_decay.py`, `tests/storage/test_like.py`.
- A Hypothesis test that calls a function which can raise must handle the raising input. Draw the
  adversarial value from the strategy, don't hope it turns up: a strategy that *might* produce one
  makes the test a coin flip, and a test that only sometimes exercises the rule is a test that
  sometimes misses it.
- Crash/chaos tests simulate power loss by closing the DB mid-lineage and reopening — committed
  transactions survive, uncommitted vanish.
- `tests/validation/` is an empty package and there is no `tests/observability/` — those subsystems are
  genuinely uncovered. `src/paa/observability/` in particular is exercised nowhere.

## Model layer

Agents depend on the narrow `agents/reasoning.py::ModelLike` protocol
(`complete_structured(prompt, schema, **kwargs)`), never on a provider. `runtime.py::RouterModelAdapter`
and `coder/model_adapter.py::RouterToolCallingModel` are the only two places that know about
`EscalatingModelRouter`; keep it that way.

A new provider threads the transport-retry budget (`ModelSettings.max_transport_retries`, ADR-0021)
through its constructor, and so do the fast paths in `models/__init__.py` and the registry rows in
`models/registry.py` — a provider that does not retry a transient 429 ends the whole run, and
escalation policy can forbid the fallback that would have absorbed it.

To test anything agent-shaped, inject a scripted model:

```python
rt = await Runtime.build(model_adapter=ScriptedModel(), ...)
```

`EchoProvider` cannot drive a plan (its minimal-schema-instance output isn't a plan). The
`ScriptedModel` in `tests/integration/conftest.py` is the reference implementation — it answers by
schema shape and records every call. That is how the "no model in the security loop" guarantee is
asserted, and it is asserted *structurally*: `test_policy_agent_holds_no_model` proves the policy
agent holds no model dependency at all, so a blocked task reaches its verdict with the model never
consulted. (A different, unrelated `ScriptedModel` lives in `tests/coder/test_agent.py` — it emits
scripted `ModelTurn`s, not schema-shaped dicts.)

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

- `docs/adr/README.md` indexes ADRs 0001–0015 and 0019–0023 (0016–0018 are unindexed), but only
  `0008`, `0010`, `0015`, `0019`, `0020`, `0021`, `0022`, `0023` exist on disk. Source comments cite
  `docs/adr/0001`–`0014`, `0016` and `0018` as rationale; the intent is documented even though the
  file is absent.
- `docs/architecture.md` is referenced from docstrings (`config.py:4`, `paa/__init__.py:4`) but does
  not exist — the architecture docs live in `docs/architecture/` (`overview.md`,
  `component-interactions.md`, `repository-map.md`). Fix the docstring references to point there.
- `README.md`'s Testing section still shows Windows paths, and its per-subsystem test counts in the
  build-status table are approximate — the total line is stale too, so reconcile neither against it.
- `src/paa/agents/specialists/` is an empty package.

## The coder agent (`paa agent`)

Full write-up: `docs/coder-agent.md`.

`src/paa/coder/` was standalone until `coder/agent.py` wired it up: `CoderAgent.build()`
composes a real `Runtime` + `coder/runtime_tools.py` + `RouterToolCallingModel` + `AgentLoop`.
Read `coder/__init__.py` before adding to that package — the pieces are deliberately decoupled.

- **The model seam is constrained JSON, not native tool calls.** `coder/model_adapter.py` asks the
  router for a `{text, tool_calls[]}` object conforming to a schema with tool names in an `enum`;
  `ModelProvider.complete_structured` does the validate-and-repair. No provider surface accepts a
  *catalogue* of callable tools — the only `tools` payload in `models/` is Anthropic's single
  synthetic structured-output tool, whose `input_schema` is the JSON schema itself — so adding
  native tool calling is an optimisation *inside that one class*.
- **`ModelTurn.tokens` is always 0** on this path (`complete_structured` returns a dict and drops
  usage numbers). `AgentLoop(max_tokens=...)` therefore never trips — bound runs with `max_steps`.
- **Tool names must match the policy globs.** `submit_task`/`run_task`/`remember_fact` match none
  of `DEFAULT_MUTATING_TOOLS`, which is why `CoderAgent.build` passes
  `mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS)` — otherwise `--mode plan`
  would silently exempt the three tools that actually change state. A new mutating tool needs
  adding to that tuple or it becomes read-only-by-accident.
- **ASK mode standing-allows the read-only half.** `CoderAgent.build` passes
  `allow=[*READ_ONLY_TOOLS, *(allow or [])]`, so only state-changing calls prompt. An explicit deny
  still wins (precedence step 1) and PLAN still refuses the mutating tools.
- **Memory writes go through the cold lake.** `remember_fact` files the assertion as a
  `cold_lake_signals` row and materialises the facts from it, so the fact carries a resolvable
  `source_signal_id` and the daemon cannot write it a second time — the tool and the daemon batch
  claim the same row, and whoever wins writes the facts in the transaction that marks the row
  processed (ADR-0022). A new tool that writes memory does the same; a direct write into
  `hot_serving_active_facts` is not rebuildable by replay and breaks the invariant the whole
  subsystem rests on.
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
  Without `--yes`, an ASK-ed (state-changing) call prompts; with no confirm callback the loop
  auto-*denies*.

## Mandatory Engineering Workflow

### 1. Understand Before Changing (ALWAYS)

Before making **any code change**, spawn subagent(s) to investigate the relevant code and its surrounding system. This step is mandatory, regardless of change size.

- Read relevant documentation, `AGENTS.md` files, ADRs, and existing tests.
- Trace execution flows, dependencies, interfaces, invariants, and downstream effects.
- Identify the root cause, not just the visible symptom.
- Return a concise context report identifying affected files, risks, expected behavior, and required tests.
- Do not modify code until sufficient context has been gathered. Never invent APIs, assume behavior, or guess at undocumented contracts.

### 2. Implement in Atomic Phases

Break work into small, independently verifiable phases. Make minimal, focused changes that preserve existing contracts unless a change is explicitly required.

Follow established conventions, keep abstractions justified, handle errors explicitly, validate inputs, and avoid duplication, dead code, unnecessary dependencies, and unrelated refactoring. Update documentation when behavior or interfaces change.

Use tests and actual source evidence to resolve uncertainty. Never fabricate implementation details, claim unverified behavior works, or weaken tests merely to achieve a passing suite.

### 3. Mandatory Independent Review (EVERY PHASE)

After each atomic phase, spawn a **separate reviewer subagent** to audit the changes before declaring the phase complete.

The reviewer must independently inspect the diff, surrounding code, original requirements, and tests. Check for:

- Bugs, regressions, flawed assumptions, and integration issues.
- Security, error handling, edge cases, and violated invariants.
- Maintainability, unnecessary complexity, and inconsistent conventions.
- Tests that are missing, incorrect, superficial, or pass without testing the intended behavior.
- Whether tests would detect the actual bug or failure they claim to prevent.

Run relevant tests and verify their results. The reviewer must report concrete findings with evidence and severity. Fix valid findings and repeat review and testing until critical issues are resolved or explicitly documented as blockers. Never approve changes merely because tests pass.

### 4. Mandatory Final Review (BEFORE COMPLETION OR COMMIT)

After all phases are implemented, spawn a fresh reviewer subagent for a full-task review. Re-evaluate the complete diff against the original requirements and system architecture, including cross-phase interactions, regressions, test coverage, and whether the tests genuinely prove correctness.

Run the appropriate test suite, inspect the final diff, and report verified results separately from unverified assumptions. Do not declare the task complete, commit, or open a PR until this review passes. If issues are found, fix them and repeat the review.

### 5. Git Discipline

Commits and PRs are permitted. Create focused commits and a PR when appropriate, but only after the mandatory reviews pass. Never commit known unresolved critical issues, claim tests passed without executing them, or bypass review gates to save time.

**Non-negotiable rule:** Context-gathering subagents must run before every change; an independent review subagent must run after every atomic phase and once again at the end. If subagents or required verification are unavailable, stop and report the blocker instead of pretending the workflow was completed.
