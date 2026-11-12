# Plan — AGENTS.md refresh (compact, high-signal only)

**Status:** ready for dispatch (investigations done; plan grouped from two investigator reports).
**Branch:** `docs/agents-refresh-20261108` from `main` @ `b8570a6`.
**Date:** 2026-11-08.

## 1. Problem statement and goal

`AGENTS.md` (550 lines) is the primary ramp-up file, but:
- It is long; several sections repeat prose that lives elsewhere or states folklore.
- Four claims are now stale vs code (FACT, investigators):
  - `specialists/` empty-package (AGENTS.md:330) — false; `specialists/coding.py` exists, wired at `runtime.py:39,617-618`, gated `config.py:747-760`, ADR-0041.
  - Sandbox probe "5s" (AGENTS.md:106) — code is 30s (`runtime.py:64`).
  - Daemon loop count "four plus boot" in `daemon.py:1-27` vs 5 loops — AGENTS.md doesn't state count, but dispatcher lists 4 + boot; keep accurate.
  - "26 tables" folklore — schema has 25 `CREATE TABLE`s + 1 FTS virtual table (`schema_sqlite.sql:244`); parity lock is about table+column identity, not count.
  - mypy "0 errors" vs Windows host 3 errors at `sandbox/subprocess_backend.py:548` (POSIX symbols, non-Windows branch) — AGENTS.md says zero-baseline (correct for Linux CI); file must note the host-specific Windows exception so agents don't chase it.
  - CLI catalogue missing `memory facts|search|signals|drain|world-model` + `gate`/`tui` (`cli/main.py:532-733`).
- Missing high-signal pieces a future agent will guess wrong: exact `.[dev]` vs `.` install trap, minimal-shape `mypy --exclude src/paa/api`, `8` pytest markers, no Makefile/pre-commit/uv.lock, retry/# noqa policy details already present — keep, tighten prose.

**Goal:** Rewrite AGENTS.md in place to a compact, verified file where every line answers "would an agent miss this?" Preserve all verified hard invariants (ledger-is-truth, LIKE helper, parity lock, safety invariants, docker footguns, image network-fragility, import-conventions lock, S3 names-a-server rule, gate-on-every-push). Delete fluff / history / dated counts that an agent cannot verify. Add at most a few new verified bullets; keep total length ≤ current (prefer shorter).

**Planner-reviewer resolutions (ses_e43f0de4...) — must-fix before/at dispatch:**
- R1 (BLOCKER-1): Compaction will semantically rot ~40 inbound `AGENTS.md:NNN` cites across ~15 docs (lock cannot catch "wrong without being broken"). T-01 must end with a human read-pass over every in-scope inbound cite and either re-point it to a section name or to the new actual line; the coder must list each cite checked. If a cite cannot be re-pointed without editing the citing doc, record it.
- R2 (MAJOR-1/2): Explicit procedure — compaction-first, cite-fix-second in one commit. Include a protect-list table (anecdote → mandatory one-sentence failure signature): DOCKER_GID permission-denied, daemon.json unknown-key refusal, offline wheelhouse builder (stale dep = runtime break), StrEnum/pydantic+SQLite round-trip, like.py NUL caller-policy, zero-baseline fix-code-not-symptom, sandbox PATH security property. Step 4 reviewer checks each survives.
- R3 (MINOR-1): Drop the daemon-loop-count bullet; the `daemon.py` docstring "four" vs 5-loops divergence is out of scope (no code changes).
- R4 (MINOR-2): Verification = baseline-identity (record ruff/mypy/pytest counts pre-edit, require identity post-edit), line-count via `len(Path("AGENTS.md").read_text().splitlines())` (splitlines, not wc), semantic read-pass list. Prefer section-name references over new `path:line` cites.
- R5 (MINOR-3): Itemize each new bullet with exact file:line; fix gate/tui cites to their real definitions (`cli/main.py` gate/tui blocks, memory subgroup `:532-733` is memory only).

**Non-goals:** No code changes, no README.md rewrites, no ADR edits, no CI changes, no new tooling, no `opencode.json` creation (absent in tree — do not invent). No attempt to close index-only ADR bodies (ROADMAP cross-cutting, out of scope).

## 2. Facts from Step 1 vs assumptions

**Facts (setup investigator, ses_e43f57f6...):**
- Install: `uv venv --python 3.12` + `uv pip install -e ".[dev]"` (README.md:42-44, AGENTS.md:8-12). `dev` carries `paa[api]`+`paa[s3]` self-referentially (pyproject.toml:54-76); without it `tests/api/conftest.py:22` and `tests/storage/test_s3_live.py:118` skip at import — a bare `.` install passes ruff/mypy/pytest by skipping.
- Gate order fixed: `ruff check src tests` → `mypy src` → `pytest tests/ -q` (AGENTS.md:22-28, ci.yml:399,427,433). Zero baseline (AGENTS.md:39-48). Minimal shape needs `mypy --exclude src/paa/api` (ci.yml:426-429, matrix ci.yml:102-104; 8 artefactual errors without fastapi).
- pytest addopts `-q --strict-markers --strict-config` (pyproject.toml:88-103), `asyncio_mode=auto`, 8 markers. `filterwarnings error::DeprecationWarning:paa.*`. Focused: `tests/<pkg>`, `::test`, `-m "not slow"`, `chaos`, `requires_graph`, `requires_postgres` (Table § exact commands, pyproject markers).
- Postgres skips unless `PAA_TEST_POSTGRES_DSN` (test_postgres_migrations.py:74-87); DSN names a server, fixture makes/drops own DB (:43-46). S3 skips unless `PAA_TEST_S3_ENDPOINT` (+ ACCESS/SECRET) unlabelled or `PAA_TEST_S3_<LABEL>_ENDPOINT` (test_s3_live.py:120-128, AGENTS.md:426-435). CI runs both via service containers (ci.yml:117-234); local green without env proves nothing about live paths.
- `python_is_python3` installed in CI on purpose (sandbox PATH=/usr/local/bin:/usr/bin:/bin, AGENTS.md:458-463).
- Env: `env_prefix=PAA_`, `env_nested_delimiter=__`, `.env` from CWD (config.py:796-803); `dotenv_value()` parses without touching `os.environ` (config.py:896-922); `get_settings()` lru_cache → `reset_settings_cache()`+`reset_dotenv_cache()` after changing PAA_HOME/cwd (config.py:925-939, conftest.py:28,30). `PAA_HOME` default `~/.paa` (config.py:52-55).
- Absent (glob-verified): Makefile, uv.lock, .pre-commit-config.yaml, opencode.json, CONTRIBUTING, CLAUDE.md, .cursorrules — CI is the only enforcer (AGENTS.md:74-75).

**Facts (architecture investigator, ses_e43f444...):**
- Entrypoints: `paa = paa.cli.main:app` (pyproject.toml:81-82); CLI = one-shot except `serve` (cli/main.py:1-6,306-350). Commands: version, submit, run, status, ledger (verify_chain :172), gate, recover, doctor, serve, mode, tui, memory subgroup (:532-733), agent (:482 via cli/coder.py). `Runtime.build()` is composition root (runtime.py:271-410). API `create_app(runtime)` loopback-only (api/app.py:54, config.py:845-859 + cli/main.py:437-476). Daemon 5 loops (:95-101). Coder `CoderAgent.build()` (coder/agent.py:104-215).
- **No file/shell/git tools exist**: catalogue is 9 runtime tools (runtime_tools.py:90-112); `run_task`/`submit_task` drive orchestrator; `specialists/coding.py:27-30` returns a turn, does not execute it yet.
- Ledger: `LedgerStore.append` one transaction (store.py:116-200); `canonical_json` sorted-keys/no-whitespace (events.py:40-53) → any serialization change breaks hash chain; `compute_idempotency_key(correlation_id, event_type, attempt, discriminator)` (events.py:68-102); `_PHASE_MAP` (replay.py:150-168) vs `_NO_PHASE_CHANGE` (:387-399), `TERMINAL_EVENTS` excludes `EXECUTION_COMPLETED` (types.py:242-251, ADR-0010). Add EventType in 4 sites min.
- Lifecycle: `ChiefOrchestrator.run` hydrate→plan→policy→execute→commit (orchestrator.py:400-443), `EXECUTION_STARTED/COMPLETED` per step with discriminator step-{i} (:730-759).
- Storage: parity lock (schema_sqlite.sql:15-17); LIKE helper (like.py:83,93,111,129,152) with NUL/%%/ESCAPE facts; total-order LIMIT (cli/main.py:609-615); factories degrade-never-raise via Runtime try/except (`runtime.py:419-449,517-557` — 30s probe, not 5s). Heavy deps never imported at module scope in core paths.
- Sandbox/docker: `--user` always, never 0 (docker_backend.py:854,895-941), isolation follows effective runtime (:382-390,491-492), DOCKER_GID group_add (docker-compose.yml:230-231), gVisor `--host-uds=all` (docker-compose.gvisor.yml:18-21), images base_worker:v4.1 + toolbox:v1, Dockerfile retry/network-fragility + wheelhouse/builder/runtime offline stages (Dockerfile:33-146).
- Model: ModelLike narrow seam (reasoning.py:26-35); only RouterModelAdapter + RouterToolCallingModel know EscalatingModelRouter; `ModelTurn.tokens` always 0 (model_adapter.py:32-38) → use max_steps not max_tokens; new provider threads max_transport_retries; ScriptedModel reference impl (tests/integration/conftest.py), EchoProvider cannot drive plan.
- Config/safety: `_enforce_loopback`, secrets are env-var names not values, MinIO alias is deliberate do-not-tidy (config.py:149-181), class X(str,enum.Enum) not StrEnum (UP042 ignored pyproject.toml:115-119), NewType IDs (types.py:45-47), SPEC DEVIATION without ADR fails review, byte-oriented FakeSnapshotter for Windows (conftest.py:88-92).
- CODER: model seam constrained JSON not native tool calls; mutating tools must match policy globs (mutating_tools tuple), ASK standing-allows read-only; memory writes via cold lake signal (ADR-0022) never direct to hot_serving_active_facts; provider via PAA_MODELS__* (OpenRouter example).

**Assumptions (explicit):**
- A1: AGENTS.md line-number citation rule ("add to end, say so in commit") remains — new rules go at end or in a new trailing section, existing numbered headings must not shift meaningfully. Keep the § "The rules this tree holds itself to" at the end.
- A2: Zero-baseline stays Linux/CI mypy 0; Windows 3 is a host exception to note in one sentence, not a new baseline.
- A3: "Compact" means ≤550 lines, target ~450-500; cut history/long anecdotes and restatements, keep commands, invariants, footguns.
- A4: PG/S3/Docker live halves stay NOT-RUN locally; file must say CI-only, not claim verified.

## 3. Candidate approaches

- **A. Light touch (fix 4 stale claims only, keep length).** Pros: low risk, no renumbering. Cons: fails user goal "compact, every line high-signal" — file stays bloated with anecdotes agents will skim past.
- **B. In-place compaction (CHOSEN):** Keep § order and hard invariants, rewrite each § to bullets, cut anecdotes to one sentence where the lesson matters ("why the rule exists" in ≤1 clause), fix stale facts, add ≤8 missing verified bullets (dev-vs-bare trap, minimal mypy exclude, [[dev]] command table, memory CLI subgroup, no file/shell/git tools, 30s probe, table-count/FTS nuance, markers). Cons: diff is large, docs lock must stay green (path:line refs in docs point at AGENTS.md — must re-verify every block-level cite still lands on non-blank lines).
- **C. Full rewrite as minimal file.** Rejected: destroys line-number stability for the 5 documented "rules" section and risks losing footguns (DOCKER_GID, daemon.json top-level key refusal, offline builder). Violates "improve in place".

## 4. Atomic tasks

Single atomic task (docs only, no code):
- **T-01 — Rewrite AGENTS.md** (owner: coder, branch `docs/agents-refresh-20261108`). Keep section order, keep § "The rules this tree holds itself to" at end (A1). Edit only `AGENTS.md`. Verify `pytest tests/conventions -q` (docs path:line lock), `ruff`/`mypy` unaffected (docs only — ruff/mypy scan src/tests, but file edits must not perturb them), line count ≤550.

Dependency graph: T-01 alone. Then Step 4 review → fix → Step 8 final review → commit/PR per git discipline. No parallel coders needed (single file).

## 5. Blast radius / risks / rollback

- **Blast:** `AGENTS.md` only; no src/tests/CI/config change. Rollback: `git revert` the commit; line-number cites in docs restore.
- **Risk — docs lock:** `tests/conventions/test_docs_references.py` asserts every `path:line` in scope (including AGENTS.md) names exactly one file, line exists, not blank. Any renumbering that leaves a cite on a blank/wrong line fails. Coder must re-verify every cite after edit.
- **Risk — stale claims reintroduced:** Verify each new claim against code with file:line before writing (30s probe, specialists wiring, 25+1 tables, 9 tools, tokens=0). Do not state counts without a grep/parse.
- **Risk — scope creep:** Do not fix README counts, do not touch ADR bodies, do not "tidy" MinIO alias. Record leftovers in report.

## 6. Verification plan

- `pytest tests/conventions -q` green (docs+import locks) after T-01.
- `ruff check src tests` 0; `mypy src` exactly 3 (Windows host) — confirms no code touched.
- `wc -l AGENTS.md` ≤550 (target 450-500). `git diff --stat` shows only AGENTS.md.
- `git diff` inspected for: no new false counts, no path:line cites onto blank lines, § rules still at end.
