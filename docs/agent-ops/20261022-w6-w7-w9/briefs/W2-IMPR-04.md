# Brief — W2-IMPR-04 (fixer) · wave 2 improve · run 20261022-w6-w7-w9

**Task:** fix **R-W2-201** of
`docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-02-tests.md`
(MAJOR · `tests/agents/test_policy.py:107`). Read the finding in full
before you start. **You are the fixer**, not the original implementer.

## The defect

`src/paa/agents/policy.py::_IRREVERSIBLE_PATTERNS` has **nine** entries.
The wave-2 policy tests cover **three** of them. The reviewer's mutation
result: **deleting all six uncovered patterns leaves 128 agents+integration
tests green** — nothing in the tree notices that the policy stopped refusing
to delete production data.

The plan's clause 0.2 named this as one of the ROADMAP's own gaps — *"six
untested patterns"* — and **nobody delivered it and nobody recorded that they
had not**. The gap is the finding; the missing tests are the easy half.

## What to do

1. **Cover all nine patterns.** One test per pattern, each of which fails
   when that pattern is removed from `_IRREVERSIBLE_PATTERNS` — the reviewer
   will check that claim, so make it true by construction: the test must
   exercise the pattern through the **policy agent's own decision**, not
   through a hand-written list of its own.
2. **Pin the count and the identity.** A test that asserts the tuple has nine
   entries *and* that the set of covered patterns equals
   `set(_IRREVERSIBLE_PATTERNS)` is what stops the next pattern from being
   added uncovered. Derive the expected set **from the source**, the way the
   validation suite derives the scanner's rule names — a hand-maintained copy
   of the list is a second place to forget.
3. **If a pattern is wrong**, say so in the report as a FINDING with the
   reproduction; the policy module is yours to edit if the fix is small and
   obviously right.

## Acceptance criteria

* Every one of the nine patterns has a test that **fails when that pattern is
  removed** — verify it by removing each in turn (or by a mutation that
  removes all six at once, if that is cheaper and equally decisive) and put
  the transcript in your report.
* The "no pattern is uncovered" test exists and is derived from the source.
* The suite is **deterministic** — the policy agent must not be given a
  model, or the test must not depend on one. Check what the existing policy
  tests do and follow them.
* Scoped: `.venv/bin/python -m pytest tests/agents -q`,
  `ruff check src/paa/agents tests/agents`, `mypy src/paa/agents` — green,
  and the gate must not regress.

## Ownership

**WRITE-SET:** `tests/agents/test_policy.py`, `src/paa/agents/policy.py`.

**READ-ONLY:** `src/paa/agents/{orchestrator,delegation,base,reasoning}.py`
— in particular `orchestrator.py` is **another agent's** this wave (F-1), so
do not edit it — `tests/agents/{test_orchestrator,test_delegation,test_reasoning}.py`
(another agent owns `test_orchestrator.py` this wave),
`tests/integration/**`.

**FORBIDDEN:** `src/paa/**` outside `agents/policy.py`, `tests/**` outside
`tests/agents/test_policy.py`, `pyproject.toml`, `deploy/**`, `docs/**` (no
ADR is needed — say so in your report), `docs/adr/README.md`, `docs/plans/**`,
`AGENTS.md`.

## Deliverables

Report `docs/agent-ops/20261022-w6-w7-w9/reports/w2-improve/W2-IMPR-04-R-W2-201.md`
(ORCH-DIGEST first). Commit code first, then the report, docs-only, explicit
paths, on branch `ops/20261022-w6-w7-w9`, trailers `Task: R-W2-201`,
`Agent: W2-IMPR-04`, `Wave: 2-improve`.
Final message ≤ 8 lines: `STATUS | agent | finding | report path | commit SHAs | blockers`.
