# Brief — W1-IMPR-03 (improver) · wave 1 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** fix the wave-1 review's findings on the **import-consistency
lock**, and take ownership of the one defect the implementer found but could
not fix — the `get_type_hints` `NameError` in
`src/paa/storage/relational/__init__.py`.

**Your role:** improver. You are the **last change of wave 1** for this
write-set. You act on a reviewer's findings; you do not re-litigate them.
Every finding you were given ends **FIXED**, **REJECTED** (with evidence), or
**DEFERRED** (with a reason).

**You are not the implementer.** You did not write this code.

## 2. Required reading (in this order)

1. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
2. **The findings you must address, in full:**
   `docs/agent-ops/20261022-w6-w7-w9/reports/w1-review/W1-REV-02-code.md`
   §4 — findings **R-W1-110** (MAJOR — the `get_type_hints` defect),
   R-W1-111, R-W1-112, R-W1-113, R-W1-114 (MINOR/NIT);
   and `docs/agent-ops/20261022-w6-w7-w9/reports/w1-review/W1-TREV-03-tests.md`
   §5 — findings **R-W1-210**, **R-W1-211**, **R-W1-212** (MINOR).
   **Read the findings in full — each carries the evidence, the reproduction
   and the recommended fix.** Do not work from this brief's summary.
3. `src/paa/storage/relational/__init__.py` — the file with the defect, and
   the file whose docstring's stated reason the reviewer says is **false**.
4. `src/paa/storage/relational/postgres.py` — check what it imports at module
   scope (the reviewer says it does **not** import `psycopg` there, which is
   what makes a runtime import safe).
5. `tests/conventions/test_import_conventions.py` — the lock you are fixing.
6. `docs/plans/w6-w7-w9.md` §3.2 — the design, and the deliverable the
   reviewer says lives only in a run-scoped report (R-W1-112).
7. `docs/agent-ops/20261022-w6-w7-w9/templates/report-improvement.md` — **your
   report template.**

## 3. The findings, and what "done" means for each

| Finding | Severity | What "done" means |
|---|---|---|
| **R-W1-110** | **MAJOR** | `src/paa/storage/relational/__init__.py` keeps `PostgresDatabase` and `Settings` behind `TYPE_CHECKING` while `get_database`'s return annotation names them, so `typing.get_type_hints(get_database)` raises `NameError`. **This is real, latent and cheap to fix, and it was unowned — you now own it.** Two requirements: (a) `get_type_hints` must resolve; (b) the file's **docstring rationale must be true** — the reviewer says the stated reason (`psycopg` is an optional extra) does not hold, because `postgres.py` does not import `psycopg` at module scope. **Verify that yourself**, then write the reason that is actually true, and keep the lazy behaviour the file legitimately needs (the *factory* must not construct a Postgres client it was not asked for, and `paa.storage.relational` must keep importing on a machine without the `postgres` extra). Add a test that fails without the fix. |
| R-W1-111 | MINOR | The "137/137 modules import in a fresh interpreter" figure is wrong in two places in the implementer's report (it is 136 module names). **Correct the report** — it is a run-folder file, and a wrong number in a record is a defect in the record. |
| R-W1-112 | MINOR | The classification of the remaining `TYPE_CHECKING` blocks — the plan's §3.2 deliverable — lives only in a run-scoped report. **Put the classification somewhere durable in the repository.** The plan (`docs/plans/w6-w7-w9.md`) is the intended home, but it is **orchestrator-owned** and outside your write-set: instead, write it into a file you own — the natural home is the lock module's own docstring or a short `docs/` note **only if you add it to your write-set with the orchestrator's agreement** (post a `NEED` and, if the orchestrator does not answer before you finish, put the classification in your report and mark the finding DEFERRED with that reason). |
| R-W1-113 | MINOR | A stale `DEFERRED` entry is never caught: the lock passes after its owner converts the file. Make the deferral **self-invalidating** — a deferral that has been acted on must fail, not silently pass. |
| R-W1-114 | NIT | `OPTIONAL_EXTRA_MODULES` names `paa.storage.relational.postgres` for an extra it does not need. Fix the list. |
| R-W1-210 | MINOR | One of the seven locks is **vacuous on revert** — it passes either way. Either make it a real regression test or **delete it**; a lock that cannot fail is decoration. |
| R-W1-211 | MINOR | A stale deferral is silently accepted (see R-W1-113 — the two findings describe the same defect from two sides; fix it once). |
| R-W1-212 | MINOR | "137/137 modules" is a file count; the lock probes 136. Make the number in the test and the number in the report **the same number**, and make it a *counted* assertion rather than a claim in prose. |

## 4. Constraints

* **The `TYPE_CHECKING` conversion must not create a cycle.** Exactly **one**
  block in the tree is load-bearing for a real runtime cycle:
  `src/paa/storage/coldlake/cas.py` (`base.py` imports `cas` for `BlobRef`, so
  a runtime `cas → base` import closes it). **Do not touch it** — it belongs
  to `W1-IMPR-01`, which is editing that file in this wave.
* **The nine modules that are load-bearing for an *optional extra* or a
  deliberate laziness** (`fastapi`, `psycopg`, `boto3`, `paa.runtime`) must stay
  as they are. A CLI that prints its version must not import the whole
  runtime; `paa.storage.relational` must import on a machine without the
  `postgres` extra.
* **Do not convert the remaining ~30 blocks.** The plan defers them with the
  cycle proof. Your job is the deferral, not the sweep.
* **The lock must be a real regression test.** For every lock: name the
  defect it prevents, and state whether it **fails** when the change is
  reverted. "It describes the current state" is not a lock.
* **The gate must not regress.** `ruff` 0, `mypy src` 0, the full suite green
  — the orchestrator runs the full suite; you run your scoped files.
* **No new dependencies**, no `pyproject.toml` change.

## 5. Ownership

**WRITE-SET (you may edit exactly these):**

```
src/paa/memory/contradiction.py
src/paa/memory/decay.py
src/paa/memory/curator.py
src/paa/marketplace/installer.py
src/paa/skills/registry.py
src/paa/agents/policy.py
src/paa/agents/context_agents.py
tests/conventions/__init__.py
tests/conventions/test_import_conventions.py
src/paa/storage/relational/__init__.py      <- ADDED for R-W1-110
docs/agent-ops/20261022-w6-w7-w9/reports/w1-impl/W1-IMPL-03-T-004.md   (the factual corrections only)
```

**READ-ONLY:** everything else. In particular `src/paa/storage/relational/{base,connection,database,postgres,errors}.py`
(read them; do not edit them), `src/paa/storage/coldlake/**` (owned by
`W1-IMPR-01` in this wave), `src/paa/observability/runs.py` (deferred to
T-008 in wave 2 — do not touch it), `src/paa/memory/creator.py` (the model for
the conversion — do not edit it).

**FORBIDDEN:** `src/paa/storage/coldlake/**`, `src/paa/observability/**`,
`src/paa/models/**`, `src/paa/validation/**`,
`src/paa/marketplace/{registry_client,package,signing}.py`, `src/paa/config.py`,
`deploy/**`, `docker-compose*.yml`, `pyproject.toml`, `docs/**`
(except the report corrections listed above), `docs/adr/README.md`,
`docs/plans/**`.

## 6. Siblings in this wave

`W1-IMPR-01` (the blob-substrate findings, including `coldlake/cas.py`) and
`W1-IMPR-02` (the deployment findings). **No overlap with your write-set**
except that `W1-IMPR-01` owns `coldlake/cas.py` — which is why you must not
touch it. You cannot see their uncommitted work.

## 7. Environment and tests

* **Scoped commands only:**
  ```bash
  .venv/bin/python -m pytest tests/conventions -q
  .venv/bin/python -m pytest tests/storage/test_postgres_contract.py tests/storage/test_postgres_migrations.py -q
  .venv/bin/python -m pytest tests/memory tests/agents/test_policy.py tests/agents/test_context_agents.py tests/skills tests/marketplace -q
  .venv/bin/python -m ruff check src tests
  .venv/bin/python -m mypy src
  ```
  (`mypy src` is scoped by the *file set* it covers, but the **whole** `src`
  must be checked — a change in `src/paa/storage/relational/__init__.py` can
  change what mypy sees everywhere.)
* **Never** the full suite, never a container, never a port.
* The fresh-interpreter check is the one that legitimately enumerates
  `src/paa` — keep it bounded (a timeout, a cap) and deterministic.
* Kill any process you start.

## 8. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w1-improve/W1-IMPR-03-T-004.md`
  — using `templates/report-improvement.md`. **Create it early and append as
  you work.** Section 1 is the **finding-by-finding disposition table**.
* **Commits:** code first, then the report in a **separate docs-only commit**
  listing the code commit's SHA. Trailers: `Task: T-004`, `Agent: W1-IMPR-03`,
  `Wave: 1`, `Report: <path>`. Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- src/paa/storage/relational/__init__.py tests/conventions/…
  git commit -m "<msg>" -- src/paa/storage/relational/__init__.py tests/conventions/…
  ```
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | findings FIXED/REJECTED/DEFERRED | blockers`.

## 9. Notes

* **R-W1-110 is a real defect in a file nobody owned** — that is how defects
  survive. Fixing it is the most valuable thing you do in this wave, and it
  is one line of import plus one honest sentence in a docstring.
* **The plan carries a wrong number** ("nine `TYPE_CHECKING` blocks are
  load-bearing"; "48 blocks"). Both reviewers measured **one** cycle-load-bearing
  block and **41** blocks at the base. **The orchestrator owns the plan** and
  will correct it — you do not need to, but say so in your report so the
  correction is not lost.
* **Answer the long-run test** in your report: Source / Load / Hostile /
  Operability / Reversal.
