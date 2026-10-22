# Wave 2 context — run 20261022-w6-w7-w9

**Read this first.** It is the shared context for every wave-2 agent. Your own
brief says which lens you wear and what your write-set is.

## The wave

Wave 2 is the **module suites** — the ROADMAP's Phase 0 (0.1–0.4) — plus the
**live S3 suite** (W6's third bullet) and the **marketplace's untrusted body**
(a finding this run's own recon added). Six tasks, six disjoint write-sets.

| Task | Subject | Plan |
|---|---|---|
| **T-002** | the parametrised S3 lifecycle and failure-mode suite, against SeaweedFS and RustFS | `docs/plans/w6-w7-w9.md` §2.1 |
| **T-005** | ROADMAP 0.1 — test `validation/`, and fix the malformed-keyword crash | §4 (0.1) |
| **T-006** | ROADMAP 0.2 — test `agents/`, and fix `HARD_STOP_ESCALATE_TO_USER` | §4 (0.2) |
| **T-007** | ROADMAP 0.3 — test `models/`, and fix the API-key leak and the escalation counter | §4 (0.3) |
| **T-008** | ROADMAP 0.4 — test `observability/` | §4 (0.4) |
| **T-011** | the marketplace's HTTP registry lets the server choose the local exception type | §3.4 |

**Wave 1 is closed and green** — `gates/gate-w1-2.md`: ruff 0, mypy 0
(137 source files), **2139 passed / 233 skipped**, every BLOCKER and MAJOR from
wave 1's two review rounds FIXED. **Your changes must not regress that.**

## What wave 1 changed, and what it means for you

* **The blob substrate has a neutral error vocabulary** — ADR-0032,
  `src/paa/storage/coldlake/errors.py`: `BlobError` and six siblings
  (`NotFound`, `AccessDenied`, `Unreachable`, `Corrupt`, `NotSupported`,
  `MalformedRequest`, `BlobFailure`), and `translate_blob_error(exc, *,
  substrate, **details)`. The translation is **idempotent**. A missing
  **bucket** and a missing **object** are now distinguishable, and the
  distinction is *earned* by asking the server (`head_bucket`), because a HEAD
  carries no body and S3 answers both with a 404.
  ⟹ **T-002 asserts against this vocabulary.** The names are the contract.
* **The deployment script now reads a merged compose stack** and refuses what
  it cannot read, loudly. Not T-002's concern, but T-002 must not disturb it.
* **Seven `TYPE_CHECKING` imports became runtime imports**, and
  `storage/relational/__init__.py` now binds `Settings` and
  `PostgresDatabase` at module scope so `get_type_hints` resolves. The
  classification of the remaining blocks is a table in
  `tests/conventions/test_import_conventions.py` with a completeness check.
  ⟹ **If your task adds a `TYPE_CHECKING` block or converts one, the lock will
  go red** — and that is the lock working. Update the table in the same commit
  (and only if the file is yours).

## The decisions already taken

`docs/plans/w6-w7-w9.md` §6 — **D-1…D-6 are given, not open.** Two of them
bear on this wave:

* **D-4** — the `HARD_STOP_ESCALATE_TO_USER` path must **park** the task.
  The event's name, the directive's name and the RFC all say the task waits
  for a human; the current behaviour is the phase map overwriting the event.
  This is a behaviour change on a live path, so it gets **its own ADR**
  (`docs/adr/0034-*.md`).
* **D-6** — the `BlobStore` contract does **not** grow a list operation. The
  live suite uses a raw boto3 client for the one assertion that needs it, and
  ADR-0032 records the asymmetry.

## The rules that are not negotiable

1. **Edit only your write-set.** Anything else: post `NEED` in `COMMS.md` and
   report `DEFERRED-DEPENDENCY` with the exact change required.
2. **Git: explicit paths only.** `git add -- <paths>` then
   `git commit -m … -- <paths>`. Never `git add -A`, `.`, `-u`, `commit -a`,
   `--amend`, `stash`, `reset`, `checkout`, `restore`, `switch`, `clean`,
   `rebase`, `merge`, `cherry-pick`, `pull`, `fetch`, `push`, `config`, or
   `--no-verify`. Stay on `ops/20261022-w6-w7-w9`.
3. **Scoped checks only.** Never the full suite, never a container restart,
   never a migration against a shared database, never a fixed port.
4. **The gate is zero and must stay zero** — ruff 0, mypy 0. No `# noqa`, no
   `# type: ignore`, no widened ruff ignore, no relaxed `pyproject.toml` rule.
   A finding that is genuinely not a finding is **deleted**, not silenced.
5. **A test that cannot fail without the fix is not a test.**
6. **Never stop, restart, rebuild or prune another project's containers.** The
   forbidden list: `mindspace-testdb`, `debugging_humans-*`,
   `paa-agent-postgres`, `paa-stack-*`, `paa-research-pg`. A test that needs a
   server starts **its own**, with a unique name, and removes it in teardown.

## The house conventions your tests must follow

* `asyncio_mode = "auto"` — async tests need no decorator.
* `filterwarnings = ["error::DeprecationWarning:paa.*"]` — a
  `DeprecationWarning` raised from `paa` **fails** the test.
* An extras-gated test carries **both** a `pytest.mark.requires_*` marker
  **and** a `skipif`/`importorskip` guard. `--strict-markers` is on, so an
  unregistered marker is an error.
* **Hypothesis** covers the invariants that must hold for all inputs. A
  hypothesis test that calls a function which can raise must handle the
  raising input, and it must **draw** the adversarial value from the strategy
  — a strategy that *might* produce one makes the test a coin flip.
* `-> None` on every test; a module docstring saying *why* the file exists;
  `pytest.mark.parametrize` for tables.
* Docstrings carry the reasoning — a comment explaining *what* instead of *why*
  is a minor finding.

## The environment

* `.venv/bin/python` is the interpreter. `boto3` is **not installed** and is
  **not** in the `dev` extra — T-002 owns both.
* The live PostgreSQL the gate uses is a disposable container the run created
  (`paa-research-pg`, port 55461). The `PAA_TEST_POSTGRES_DSN` convention
  names a *server*; each test makes and drops its own database.
* The sandbox builds its child `PATH` as `/usr/local/bin:/usr/bin:/bin`, so a
  plan step that runs `python` needs a system interpreter — this host has one.
* Kill any process you start.
