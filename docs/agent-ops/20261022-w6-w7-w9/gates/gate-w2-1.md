# Gate — wave 2 (W2-1)

**Run:** `20261022-w6-w7-w9` · **branch:** `ops/20261022-w6-w7-w9` · **date:** 2026-10-22
**Run by:** the orchestrator, with **no writer active** (every wave-2 agent had
returned before this gate started).

## The gate

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/ -o addopts="--strict-markers --strict-config" -q
```

## Results

| Check | Baseline (wave-1 close `4c9300d`) | Wave-2 gate | Verdict |
|---|---|---|---|
| `ruff check src tests` | 0 findings | **0 findings** (`All checks passed!`) | hold |
| `mypy src` | 0 errors, 136 files | **0 errors, 137 files** (`Success: no issues found in 137 source files`) | hold |
| `pytest tests/` | 2139 passed / 233 skipped | **2678 passed / 235 skipped / 3 xfailed** (118.61 s) | **+539 passed** |

The baseline is **zero findings** and it stayed zero. No `# noqa` was added, no
`# type: ignore` was added, no ruff per-file ignore was added, and no
`pyproject.toml` rule was relaxed — the only `pyproject.toml` change is the
`dev` extra gaining `paa[s3]` and the `requires_s3` marker being registered
(both **additions**, neither a relaxation).

## What the wave added

| Task | Agent | Commits |
|---|---|---|
| T-002 — the live S3 suite (two servers) | W2-IMPL-01 | `be649ad`, `4e0c967` |
| T-005 — the validation suite + the malformed-keyword crash | W2-IMPL-02 | `7ca81c8`, `cedf3c7` |
| T-006 — the agents suite + `HARD_STOP` parking + ADR-0034 | W2-IMPL-03 | `8527fe7`, `487c6d5`, `c504dd6` |
| T-007 — the models suite + the key leak | W2-IMPL-04 | `514cfad`, `b2a21a8`, `14cd23a`, `0e60264` |
| T-008 — the observability suite | W2-IMPL-05 | `4808982`, `9a92f19`, `6117c4a`, `377c160`, `efced61` |
| the import-conventions lock, after the fulfilled deferral | W2-FIX-01 | `8c189d1`, `80ba09b`, `536336b`, `9acad73` |

Diff against the wave-1 close: **37 files, +12081 / −136**.

## What the gate does *not* cover

* **The live S3 suite skipped.** `PAA_TEST_S3_ENDPOINT` is unset, so
  `tests/storage/test_s3_live.py` skipped as one module (its 40 bodies never
  ran here). The suite is proven by its own agent against two real servers —
  **that is a claim, not a gate result**; see the wave-2 review.
* **The live PostgreSQL suite skipped.** `PAA_TEST_POSTGRES_DSN` is unset, so
  the `requires_postgres` tests skipped, exactly as in every baseline.
* The three `xfailed` tests are the wave-2 agents' **deliberate** markers for
  defects they found in files outside their write-sets (`strict=True`, so
  they fail the moment the defect is fixed). Each is a FINDING in its
  agent's report.

## The skips

235 skips against the baseline's 233: **one** is the S3 live module (no
server configured) and **one** is the PostgreSQL-gated module set. Neither is
a regression — both are the extras-gated convention working as documented.
