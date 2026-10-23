# Brief — W4-REV-FINAL (final whole-branch reviewer) · run 20261022-w6-w7-w9

## 1. Role

You are a fresh reviewer with **no prior context** of this run. You did not write any of it. You do not edit code. You try to break the whole branch.

Your job: review the **complete diff** against the **original mission** and decide whether the branch is safe to merge. Report findings; the orchestrator decides.

## 2. The mission (the original task)

Execute the substrate track's W6–W8 and ROADMAP Phase 0 (0.1–0.5) from `docs/MCP_AND_PLUGINS.md` §"The substrate track (W3–W8)", plus ROADMAP Phase 0 items 0.1–0.5:

* **W6 (the suites):** a parametrised S3 lifecycle and failure-mode suite against two real S3 servers; the validation, agents, models, and observability suites against ROADMAP clauses.
* **W7 (the cleanup):** the blob error vocabulary, the fetch-images↔compose parity, the `TYPE_CHECKING` cleanup, the marketplace's untrusted HTTP body, the stale docs and a docs-reference checker.
* **W9 (ROADMAP Phase 0 0.1–0.5):** validation ✓, agents ✓, models ✓, observability ✓, CI ✓.
* Plus decisions D-1…D-6 from `docs/plans/w6-w7-w9.md`: repeatable `--compose <path>` (merged in file order); SeaweedFS + RustFS test-owned containers; S3 live suite in CI; `HARD_STOP_ESCALATE_TO_USER` parks; docs sweep covers all of `docs/`; `BlobStore` does not grow a list operation.

## 3. The diff

```bash
git log --oneline 02f3636..HEAD        # everything on this branch since base
git diff 02f3636..HEAD --stat          # scope
git diff 02f3636..HEAD -- src/ tests/ docs/ .github/ pyproject.toml
```

The branch tip is `ops/20261022-w6-w7-w9`; the base is `02f3636`. Rough shape: ~350 files changed (code + tests + docs + run trail).

## 4. What to check

* **Requirements coverage:** every ROADMAP clause in 0.1–0.5 and every plan clause the mission named — is it pinned by a test? Is anything claimed as done that is not actually done?
* **Root cause vs symptom:** for each fix, is the cause addressed (e.g. the malformed-keyword rule in all three validators; the human-gate identity; the shared keyword table; the exception-chain redaction; the shared redaction helper)?
* **Tests prove what they claim:** every mutation the fixers/reviewers claimed would fail — did you run at least one reversal per task? Did it fail?
* **Conventions:** `-> None` on every test; `pytest.mark.requires_*` marker + `skipif`/`importorskip` for extras-gated tests; hypothesis draws the adversarial value; `filterwarnings = ["error::DeprecationWarning:paa.*"]`; no `# noqa`, no `# type: ignore`, no widened ruff ignore, no relaxed `pyproject.toml` rule; `-> None` everywhere.
* **The gate:** `ruff check src tests` = 0, `mypy src` = 0, full suite green — run all three yourself and report actual output.
* **The lock:** `tests/conventions/test_import_conventions.py` — the `DEFERRED` facility was removed because the last deferral was fulfilled; does the lock still have teeth? Run one mutation yourself.
* **The docs-reference lock:** `tests/conventions/test_docs_references.py` — green? does it have teeth?
* **Records:** does `01-work-ledger.md` list the two operator decisions (the unreachable egress check; the five pattern-semantics findings) as open? Do `AGENTS.md`/`ROADMAP.md`/`MCP_AND_PLUGINS.md` overstate coverage anywhere?
* **Safety/security:** no secrets in logs or code; the marketplace body is bounded; the key never reaches a log/payload/chain; `paa[s3]` in the dev extra; `python-is-python3` landmine handled; the operator's containers untouched.
* **Scope creep:** did anything land outside its task's write-set?
* **Commit discipline:** focused commits with clear messages; nothing unrelated.

## 5. What to run

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/ -o addopts="--strict-markers --strict-config" -q
```

Run at least one reversal per major task (pick the ones that matter most), and one mutation of each lock file.

## 6. The report

Write `docs/agent-ops/20261022-w6-w7-w9/reports/final-review.md`:

* **VERDICT** — one line: merge-ready as-is, or merge-ready after fixes, or not safe.
* **PER-TASK** — T-001, T-002, T-003, T-004, T-005, T-006, T-007, T-008, T-009, T-010, T-011, T-012: CLOSED / PARTLY CLOSED / OPEN / NOT DONE, each with evidence.
* **FINDINGS** — severity (critical / major / minor / nit), file:line, what is wrong, evidence, suggested fix.
* **VERIFIED** / **UNVERIFIED**.
* **GATE OUTPUT** — the actual output of all three commands.
* **REVERSALS RAN** — which you ran and what happened.

Commit **only** the report, docs-only, explicit path, on branch `ops/20261022-w6-w7-w9`:

```bash
test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
git commit -m "docs(agent-ops): the final whole-branch review" -- docs/agent-ops/20261022-w6-w7-w9/reports/final-review.md
```

Final message ≤ 14 lines: VERDICT, one line per task, each finding as `SEVERITY | file:line | one line`, report path, commit SHA. **It must not be empty.**
