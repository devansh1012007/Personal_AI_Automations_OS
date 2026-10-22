# Gate — wave 2, after the improve cycle (W2-2)

**Run:** `20261022-w6-w7-w9` · **branch:** `ops/20261022-w6-w7-w9` · **date:** 2026-10-22
**Run by:** the orchestrator, with **no writer active**.

## The gate

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/ -o addopts="--strict-markers --strict-config" -q
```

## Results — three gates, one run

| Check | wave-1 close `4c9300d` | wave-2 gate (W2-1) | after the improve cycle (W2-2) |
|---|---|---|---|
| `ruff check src tests` | 0 | **0** | **0** |
| `mypy src` | 0 (136 files) | **0** (137) | **0** (138) |
| `pytest tests/` | 2139 passed / 233 skipped | **2678 / 235 / 3 xfailed** | **2859 / 235 / 5 xfailed** |

The baseline is zero and it stayed zero. No `# noqa`, no `# type: ignore`, no
widened ruff ignore, no relaxed `pyproject.toml` rule.

## What the improve cycle changed

| Finding | Agent | What it fixed |
|---|---|---|
| **F-1** (MAJOR) — a second human-gate request in one lineage was swallowed by the ledger's idempotency key, and the task committed with the policy's risk gate unanswered | W2-IMPR-01 | a gate request is a *question* with an identity (`gate:<role>:<ordinal>`); the release names the question it answers; ADR-0034 amended |
| **F-2** (MAJOR) — the malformed-keyword rule reached one of three JSON-Schema validators | W2-IMPR-02 | one table of keyword shapes, read by all three; a malformed keyword is a schema error everywhere |
| **F-3** (MINOR) — `ModelUnavailableError` carried its details unscrubbed | W2-IMPR-02 | the same rule as its sibling |
| **F-4** (MINOR) — the escalation ordinal was a decorative extra ledger read | W2-IMPR-03 | the discriminator is the event's own uuid; the lineage's order is the ledger's `state_version` |
| **F-5** (MINOR) — the lock's own docstring still documented the removed class "F" | W2-IMPR-05 | the docstring describes the tree as it is |
| **F-6** (NIT) — ADR-0032's decision 4 mis-stated the mechanism | W2-IMPR-05 | ADR-0032 corrected; the boundary *holds* — the record was wrong |
| **F-7** (MINOR) — the LOCKDOWN-safe healthcheck was opt-in | W2-IMPR-03 | the router uses the mode it was given, and fails closed otherwise |
| **R-W2-201** (MAJOR) — six of the nine irreversible-operation patterns were uncovered | W2-IMPR-04 | 9/9 patterns, each with a case that fails when that pattern is removed |
| **R-W2-202** (MAJOR) — the S3 report's mutation 11 did not reproduce and the ADR-0032 correction it proposed was false | W2-IMPR-05 | measured on two live servers; the record corrected; two live tests added |
| **R-W2-203** (MAJOR) — the key survived on the exception's `__cause__` and in the traceback | W2-IMPR-03 | the router's raised errors carry **no chain at all** |
| the close-out (3 NEEDs, 1 deferral) | W2-FIX-02 | the keyword table's leaf home, the router's mode, one redaction helper, ADR-0033's title and claim |

## What this gate does *not* cover

* The live S3 suite skipped (no `PAA_TEST_S3_ENDPOINT`) — the fixers ran it
  against two real servers, and that is a **claim**, checked by the reviewers,
  not a gate result.
* The `requires_postgres` tests skipped (`PAA_TEST_POSTGRES_DSN` unset), as
  in every baseline.
* The **five xfails**: four are the wave-2 agents' strict markers for defects
  in files outside their write-sets, and the fifth is W2-FIX-02's marker for
  the unreachable egress check in `_check_lockdown` — which it deliberately did
  **not** patch, because making it reachable would change what the policy
  refuses. That is an **operator decision**, carried in the close-out report.
