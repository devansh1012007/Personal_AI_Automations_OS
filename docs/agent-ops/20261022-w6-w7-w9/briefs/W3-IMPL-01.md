# Brief — W3-IMPL-01 (implementer) · wave 3 · run 20261022-w6-w7-w9

**Task T-009 — ROADMAP 0.5: GitHub Actions.** Nothing enforces the gate on a
push: there is no `.github/`, no Makefile, no tox, no pre-commit. This task
builds the workflow.

## The requirement (the plan's §4, item 0.5, verbatim)

> a workflow that mirrors the repo's **network-fragility conventions** (the
> Dockerfiles already encode them), installs `.[dev]` (not `.` — otherwise the
> whole `tests/api/` directory skips at import, which is the exact failure the
> `dev` extra was widened to prevent), runs a **PostgreSQL service container**
> and exports `PAA_TEST_POSTGRES_DSN`, and — the landmine — installs
> **`python-is-python3`**, because the sandbox builds its child `PATH` as
> `/usr/local/bin:/usr/bin:/bin` and `actions/setup-python`'s tool-cache
> interpreter is not on it. Without that line, six assertions fail on the
> runner for a reason that has nothing to do with the code.

The ROADMAP's own clause adds: *"the full suite + `ruff` + `mypy --strict` on
every push, a matrix over the optional extras."*

## What to build

1. **`.github/workflows/ci.yml`** — the gate on every push and pull request:
   ```bash
   .venv/bin/python -m ruff check src tests
   .venv/bin/python -m mypy src
   .venv/bin/python -m pytest tests/ -o addopts="--strict-markers --strict-config" -q
   ```
   That is **the** gate — the same three commands, in that order, with the
   same strictness flags. Do not add a `-k` filter, a `--ignore`, or a
   marker exclusion to make it faster: the gate is the gate.
2. **The extras matrix.** The ROADMAP asks for a matrix over the optional
   extras. A minimal install must also pass — the plan's reason: *"never
   import one at module scope in a core path, or a minimal install stops
   importing"*. So the matrix is over install shapes (a minimal one and the
   `dev`/`all` ones), and every job runs the same three commands.
3. **The PostgreSQL service container**, with `PAA_TEST_POSTGRES_DSN`
   exported. The convention is in `AGENTS.md` and in
   `tests/storage/test_postgres_migrations.py`: **the DSN names a server**,
   each test creates and drops its own database. The runner must therefore
   point at the server, not at a database the tests will drop.
4. **The live S3 suite in CI** (decision **D-3**): SeaweedFS and RustFS as
   service containers, with `PAA_TEST_S3_ENDPOINT` and its two credentials
   exported, so the suite that wave 2 wrote actually runs instead of
   skipping. Read `tests/storage/test_s3_live.py`'s header and ADR-0033 for
   the exact variable names — do not invent your own.
5. **`python-is-python3`** — the landmine. The sandbox builds its child
   environment's `PATH` as `/usr/local/bin:/usr/bin:/bin` (a security
   property — never widen it), so `actions/setup-python`'s interpreter is
   invisible to it. Without the package, six assertions fail on the runner
   for a reason that has nothing to do with the code. **Prove the line is
   needed**: read `AGENTS.md`'s account of it and the six tests, and say in
   your report why it is there.
6. **The network-fragility conventions**: `PIP_RETRIES=10`, `PIP_TIMEOUT=120`,
   and a retry loop around the install — the Dockerfiles already encode the
   rule, and a CI install that gives up on one dropped connection is a red
   build that says nothing about the code.

## Constraints

* **You cannot run the workflow.** There is no GitHub here. So: the YAML must
  be **parsed and checked** (a Python `yaml.safe_load` is the minimum; a
  linter if one is installed), the *steps* must be **executed locally** as
  far as they can be — the gate commands themselves, the install shape, the
  environment variables the suite reads — and anything you could not verify
  must be marked **UNVERIFIED** in your report, with the reason.
* **Never** touch the operator's `paa-stack-*` containers or the operator's
  database. If you run the live S3 suite locally, your servers must be your
  own, uniquely named, and removed in teardown.
* **Do not** weaken the gate to make it pass. `ruff` 0 and `mypy` 0 are the
  bar, and the suite is the whole suite.
* The workflow must not contain a secret, a credential, or a token — the
  runner has none of the operator's keys, and the live S3 suite's credentials
  are **the suite's own**.

## Ownership

**WRITE-SET:** `.github/workflows/*.yml` (new), and — only if the plan's
checks require it — `pyproject.toml` (no new dependency) and
`docs/deployment/ci.md` (new; the workflow's own documentation, if the house
style wants one — read `docs/deployment/docker.md` for the shape).

**READ-ONLY:** `docs/plans/w6-w7-w9.md` §4 (0.5), `AGENTS.md`,
`tests/storage/test_s3_live.py`, `tests/storage/test_postgres_migrations.py`,
`docs/adr/0033-*.md`, `docs/deployment/docker.md`.

**FORBIDDEN:** `src/**`, `tests/**` (except reading them), `docs/**` except a
new `docs/deployment/ci.md`, `docs/plans/**`, `docs/adr/**`, `AGENTS.md`,
`deploy/**`, `docker-compose*.yml`.

## Acceptance criteria

* The workflow's YAML parses; every step's command is one the repo actually
  supports; the three gate commands are the repo's three gate commands.
* The extras matrix exists and every job runs the same gate.
* The PostgreSQL service container and the S3 service containers are declared
  with health checks, and the environment variables the suites read are
  exported with the names the suites read.
* `python-is-python3` is installed, and the report explains why.
* The install uses the retry convention.
* The report states, for each claim, whether it was **verified locally** or
  is **UNVERIFIED** (the workflow itself cannot be run here — say so plainly).

## Deliverables

Report `docs/agent-ops/20261022-w6-w7-w9/reports/w3-impl/W3-IMPL-01-T-009.md`
(ORCH-DIGEST first). Commit the workflow first, then the report, docs-only,
explicit paths, on branch `ops/20261022-w6-w7-w9`, trailers `Task: T-009`,
`Agent: W3-IMPL-01`, `Wave: 3`.
Final message ≤ 8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
