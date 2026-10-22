# Gate — wave 2, closed (W2-3)

**Run:** `20261022-w6-w7-w9` · **branch:** `ops/20261022-w6-w7-w9` · **date:** 2026-10-22
**Run by:** the orchestrator, with no writer active.

## The gate

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/ -o addopts="--strict-markers --strict-config" -q
```

## Results

| Check | wave-1 close | wave-2 gate | after improve | **wave 2 closed** |
|---|---|---|---|---|
| `ruff check src tests` | 0 | 0 | 0 | **0** |
| `mypy src` | 0 (136 files) | 0 (137) | 0 (138) | **0** (138) |
| `pytest tests/` | 2139 / 233 skipped | 2678 / 235 / 3 xfailed | 2859 / 235 / 5 xfailed | **2869 / 235 / 4 xfailed** |

The baseline is zero and it stayed zero through three gates. No `# noqa`,
no `# type: ignore`, no widened ruff ignore, no relaxed `pyproject.toml`
rule.

## What wave 2 did

| Task | Requirement (ROADMAP / plan) | Outcome |
|---|---|---|
| T-002 | the S3 path has never met a real server | a live suite over **two** S3 implementations, 20 bodies × 2 servers, `requires_s3`, `paa[s3]` in the dev extra (ADR-0033) |
| T-005 | the validation layer's three headline claims had no tests; a malformed schema keyword **crashed** the gate | 417 tests; a malformed keyword is a **schema error in all three validators**, never a `TypeError`, never silently dropped |
| T-006 | the exact event sequence; a **real** three-node cycle; the anti-goal 0.82 path; and `HARD_STOP` must **park** | the exact lists for four paths; a mediated cycle refused at `MAX`; the cosine path on both sides of the threshold; a hard stop parks until a human rules (ADR-0034) |
| T-007 | the escalation path leaked the API key | the key no longer reaches a log line, a ledger payload, an exception's message or details, **or the exception's chain**; the LOCKDOWN spy, the semaphore's cap, and the restart-surviving audit record |
| T-008 | percentiles labelled but not valued; `finish_run` never asserted; `logging.py` untested | hand-computed percentiles with the algorithm pinned by a property; the run repository's round-trips; six source defects fixed |

## The review trail

Two independent reviews (code and tests) found **0 BLOCKER, 5 MAJOR, 9
MINOR/NIT**. A delta review of the improve cycle re-ran the reversals itself
— 13 mutations, all caught — and closed all five MAJORs, leaving 4 MINOR and
3 NIT, which the last round closed. The four **strict xfails** that remain are
the agents' honest markers for defects in files outside their write-sets.

## What this gate does not cover

* The live S3 suite skipped (no `PAA_TEST_S3_ENDPOINT` configured here). The
  fixers ran it against two real servers and the reviewers re-ran it
  independently — that is a **claim with evidence**, not a gate result here.
* The `requires_postgres` tests skipped (`PAA_TEST_POSTGRES_DSN` unset), as
  in every baseline.
* The **two operator decisions** the wave leaves open are recorded in
  `01-work-ledger.md` (§"Open items"): the unreachable egress check in
  `_check_lockdown` (assessed, deliberately not changed — making it reachable
  would change what the policy refuses) and the five pattern-semantics
  findings (each widens a security rule over prose — a policy decision).
