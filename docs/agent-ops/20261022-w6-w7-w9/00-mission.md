# Mission — 20261022-w6-w7-w9

**Run id:** `20261022-w6-w7-w9`
**Branch:** `ops/20261022-w6-w7-w9`
**Base SHA:** `02f363606b5a40447e67940be898c8d3307a917c` (`main`)
**Plan:** `docs/plans/w6-w7-w9.md` — the authoritative scope, the evidence, the
decisions (D-1…D-6), the task table and the wave structure. **This file is the
operational companion to it, not a restatement of it.**
**Started:** 2026-10-21T20:16:22Z

## Scope

The remaining substrate-track work in `docs/MCP_AND_PLUGINS.md` §"The substrate
track (W3–W8)" — **W6** (the suites) and **W7** (the cleanup) — plus the
ROADMAP's Phase 0, `docs/ROADMAP.md` 0.1–0.5 ("W9").

**Out of scope, explicitly:** the MCP / plugin track (Phase 1 and later). It is
a different track, and the substrate track exists so that work does not have to
discover the substrate's behaviour. **W8 is done** — the gate is zero, the final
whole-diff review happened, the commits split.

## The commands

The gate, in this order, with **no writer active**:

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/ -o addopts="--strict-markers --strict-config"
```

Two environment variations matter:

```bash
# a live PostgreSQL (the DSN names a SERVER; each test makes and drops its own DB)
export PAA_TEST_POSTGRES_DSN="postgresql://paa:paa@127.0.0.1:55461/postgres"

# a live S3 (from wave 2)
export PAA_TEST_S3_ENDPOINT=… PAA_TEST_S3_ACCESS_KEY=… PAA_TEST_S3_SECRET_KEY=… PAA_TEST_S3_BUCKET=…
```

The `--strict-markers --strict-config` is redundant with the project's own
`addopts` (`pyproject.toml:83` already sets `-q --strict-markers
--strict-config`); it is spelled out here so an agent that overrides `addopts`
still gets the strictness.

**The baseline (measured at `02f3636`, before anything was touched):**

| Step | Command | Result |
|---|---|---|
| Lint | `ruff check src tests` | **All checks passed!** — 0 findings |
| Typecheck | `mypy src` | **Success: no issues found in 136 source files** — 0 errors |
| Unit | `pytest tests/ -o addopts="--strict-markers --strict-config"` | **1915 passed, 233 skipped**, 1 warning (~93 s) |
| Unit + live PostgreSQL | same, with `PAA_TEST_POSTGRES_DSN` set | **2070 passed, 78 skipped** |
| Build | `hatchling` build backend | not run in the gate; the package is installed editable |
| Container restart + smoke | — | **not part of the gate**; the suite starts no container. The deployment stack (`paa-stack-*`) is the operator's, not the run's, and is **not** restarted, rebuilt or pruned by this run. |
| Load / abuse tooling | — | the repo has none |

**Pre-existing failures: none.** The suite is green at the base SHA.

**A host landmine, recorded so nobody re-diagnoses it:** the sandbox builds
its child environment with `PATH=/usr/local/bin:/usr/bin:/bin`
(`src/paa/sandbox/base.py:589`) — deliberately not the host's `PATH`. A plan step
that runs `python -c …` therefore needs `python` in a *system* directory. This
host has it (`/usr/bin/python → python3`). A host that does not will fail six
assertions. **Do not widen the sandbox's `PATH`** — that is the security
property.

## The hotspots — one writer each

| File | Why it is a hotspot | Owner |
|---|---|---|
| `pyproject.toml` | the extras, the markers, the pytest/ruff/mypy config — a second place to forget | **T-002** (wave 2) only |
| `docker-compose.yml`, `docker-compose.gvisor.yml` | the deployment's image set and its merge semantics | **T-003** (wave 1) only |
| `.env.example` | the operator's contract with the compose file | **T-003** (wave 1) only |
| `src/paa/storage/coldlake/**` | the blob substrate, the `BlobStore` protocol | **T-001** (wave 1) only |
| `src/paa/config.py` | the settings tree; every env var is a public contract | **no task in this run touches it** — if one needs to, it posts `NEED` |
| `docs/adr/README.md` | the ADR index | **the orchestrator only** |
| `docs/MCP_AND_PLUGINS.md`, `AGENTS.md`, `docs/ROADMAP.md` | the records | **the orchestrator only** |

## The environment

* **Never stop, restart, rebuild or prune another project's containers.** The
  forbidden list, by name: `mindspace-testdb`, `debugging_humans-*`,
  `paa-agent-postgres`, `paa-stack-*`, `paa-research-pg`. A test that needs a
  server starts **its own**, with a unique name, and removes it in teardown.
* **Never push, merge, rebase or amend** during the operation unless the
  operator asks.
* The live PostgreSQL the gate uses is a **disposable container the run
  creates** (`paa-research-pg`, port 55461), started by the orchestrator and
  removed at the end of the run. The `PAA_TEST_POSTGRES_DSN` convention names a
  *server*; each test makes and drops its own database, so a run can never
  touch a deployment's database.
* `boto3` is **not** installed in `.venv` and **not** in the `dev` extra. The
  S3 tasks install it through the extra; nothing imports it at module scope.

## The assumptions

| # | Assumption | If it is wrong |
|---|---|---|
| **A-001** | The operator accepts the plan's D-1…D-6 as written. | The task that depends on it stops and posts `QUERY`; the decision is re-taken and recorded here. |
| **A-002** | The wave-orchestrator's `task` tool is available and reliable for this run. | If a dispatch fails three times, the run stops and reports the blocker rather than pretending the work was done. |
| **A-003** | `docs/agent-ops/` is a legitimate place for the run folder in this repo (it is the skill's convention; the repo has no precedent). | The folder is removed at the end, or moved, on the operator's word. |
| **A-004** | The 12 tasks in the plan's §5 are correctly partitioned, and the wave-1 write-sets do not overlap. | Verified mechanically before every dispatch (§8 of the protocol): `cut -f2 ownership/W<N>.tsv \| sort \| uniq -d` must print nothing. |

## The rules that apply to every agent

`templates/STANDING-ORDERS.md` is binding and is read by every agent before it
does anything. The four that bite most often here:

1. **Edit only your write-set.** Anything else: post `NEED` in `COMMS.md` and
   report `DEFERRED-DEPENDENCY` with the exact change required.
2. **Git: explicit paths only.** `git add -- <paths>` then
   `git commit -m … -- <paths>`. Never `git add -A`, `.`, `-u`, `commit -a`,
   `--amend`, `stash`, `reset`, `checkout`, `restore`, `switch`, `clean`,
   `rebase`, `merge`, `cherry-pick`, `pull`, `fetch`, `push`, `config`, or
   `--no-verify`. Stay on `ops/20261022-w6-w7-w9`.
3. **Scoped checks only.** Never the full suite, never a container restart, never
   a migration against a shared database, never a fixed port.
4. **Report first, then the ≤ 8-line final message.** The report path is in the
   brief.
