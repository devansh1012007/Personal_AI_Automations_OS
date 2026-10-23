# Brief — W3-REV-01 (independent reviewer) · wave 3 · run 20261022-w6-w7-w9

## 1. Role

You are a fresh independent reviewer of wave 3. You did not write any of it.
You are not the orchestrator and you are not any of W3-IMPL-01..04. You do
not edit code. You try to break the change.

The wave's four tasks are:
* **T-009** — the GitHub Actions CI workflow (W3-IMPL-01, commits dad08e4, 07baff0)
* **T-010** — the docs sweep and the docs-reference lock (W3-IMPL-02, commits 3c500de, a949bbf)
* **T-011** — the marketplace's untrusted HTTP body (W3-IMPL-03, commits 01b5adb, efd18f6, e214428)
* **T-012** — the records (MCP_AND_PLUGINS.md, ROADMAP.md, AGENTS.md, docs/adr/README.md) (W3-IMPL-04, commits eb849ff, f54e7cf)

The reports are under `docs/agent-ops/20261022-w6-w7-w9/reports/w3-impl/`.

## 2. What to check

### T-009 (CI workflow) — `.github/workflows/*.yml`

* **The gate is exactly the repo's gate:** `ruff check src tests`, `mypy src`, `pytest tests/ -o addopts="--strict-markers --strict-config" -q` — no ignores, no marker exclusions, no `-k` filters.
* Install shape is `.[dev]` (not `.`) — the documented setup.
* `python-is-python3` is installed — say in your report whether the rationale (the sandbox's child `PATH` is `/usr/local/bin:/usr/bin:/bin`, so `actions/setup-python`'s interpreter is invisible to it) is present and correct.
* PostgreSQL service container + `PAA_TEST_POSTGRES_DSN` exported; SeaweedFS + RustFS service containers + `PAA_TEST_S3_ENDPOINT` + its two credentials exported (decision D-3: the live S3 suite runs in CI).
* Install uses the retry convention (`PIP_RETRIES=10`, `PIP_TIMEOUT=120`, retry loop).
* An extras matrix over install shapes, every job running the same gate.
* **You cannot run GitHub Actions.** Parse-check the YAML (e.g. `yaml.safe_load`), and verify each step's command is one the repo actually supports locally. Mark anything unverifiable as UNVERIFIED.

### T-010 (docs sweep + checker) — `tests/conventions/test_docs_references.py` + 38 docs files

* **The lock passes** — run `pytest tests/conventions -q` yourself and report the actual output.
* **It has teeth** — break a reference in a scratch copy and show it goes red.
* **The 38 corrected references** — each one: does the cited line exist, is it not blank, and does the sentence around it still match what is on that line? Pick a sample (say ten) and read the cited lines yourself; report whether they agree. (This is the half the lock cannot see.)
* **Exclusions** — `docs/agent-ops` and `docs/plans` are excluded with stated reasons in the file. Are they justified? Is any other exclusion present without a reason?
* **README's three test counts** — are they true?

### T-011 (marketplace) — `src/paa/marketplace/registry_client.py`

* A malformed remote body raises **the project's own error** — on every path that reads a body (parse success, error body, not-JSON, wrong shape, empty, enormous). Read the code and enumerate them yourself; did the agent miss one?
* **The server's text is bounded** — a concrete maximum length, stated in the code — and it does **not** reach a log line unescaped. Check the logging paths.
* **Every caller** of the old exception types — did the agent find them, and what happens to them now? (`src/paa/marketplace/installer.py` is wave 1's file — read-only for this wave; check whether it calls `HttpRegistry` and whether anything breaks.)
* Did anything in `src/` change besides `marketplace/`?

### T-012 (records)

* Every claim in the four files is **checkable** against the tree or the gate records — check a sample.
* Coverage claims are not overstated — where a ROADMAP Phase-0 clause is only partly met, does the record say so and what is left?
* The two open items (the unreachable egress check in `_check_lockdown`; the five pattern-semantics findings) are visible as **open**, not closed — in `01-work-ledger.md` and anywhere a status line might imply otherwise.
* The ADR index rows for 0032–0035 are present and correct.

## 3. What to run

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/conventions tests/marketplace -q
.venv/bin/python -m pytest tests/ -o addopts="--strict-markers --strict-config" -q
```

Report the actual output of at least the conventions and marketplace suites.

## 4. The report

Write `docs/agent-ops/20261022-w6-w7-w9/reports/w3-review/W3-REV-01.md`:

* **VERDICT** — one line: is wave 3 safe to close?
* **PER-TASK** — T-009 / T-010 / T-011 / T-012: each one CLOSED / PARTLY CLOSED / OPEN, with evidence.
* **FINDINGS** — severity (BLOCKER / MAJOR / MINOR / NIT), file:line, what is wrong, evidence, suggested fix.
* **CLAIMS CHECKED** — which report claims you verified, with actual output.
* **VERIFIED** / **UNVERIFIED**.
* **NOT A FINDING**.

Commit **only** the report, docs-only, explicit path, on branch `ops/20261022-w6-w7-w9`:

```bash
test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
git commit -m "docs(agent-ops): the wave-3 review" -- docs/agent-ops/20261022-w6-w7-w9/reports/w3-review/W3-REV-01.md
```

Final message ≤ 12 lines: VERDICT, one line per task, each finding as `SEVERITY | file:line | one line`, report path, commit SHA. **It must not be empty.**
