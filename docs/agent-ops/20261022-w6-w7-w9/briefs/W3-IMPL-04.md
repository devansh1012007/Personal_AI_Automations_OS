# Brief — W3-IMPL-04 (implementer) · wave 3 · run 20261022-w6-w7-w9

**Task T-012 — the records.** This is the only task in the run whose
write-set is *documentation of what the run did*, and it lands **after** the
other wave-3 tasks, because it reports on them.

## What you write, and where

| File | What it must say |
|---|---|
| `docs/MCP_AND_PLUGINS.md` | the substrate track's status lines: W3, W4, W5 (earlier waves, already done) and now **W6, W7** — with what each wave actually delivered, in the document's own voice. The track document is the plan of record for this subsystem; a status line that says "done" without saying *what* is a status line that lies |
| `docs/ROADMAP.md` | **Phase 0 (items 0.1–0.5)** — the ROADMAP's own clauses, each with its new state: 0.1 validation, 0.2 agents, 0.3 models, 0.4 observability, 0.5 CI. The ROADMAP was written when the repo had 755 tests; say what the coverage is **now**, and be honest where a clause is *partly* covered — the plan says three of the five were "already substantially done", and the record must not claim more than the suite proves |
| `AGENTS.md` | the house rules the run changed. Candidates: the **new** import-conventions lock's rules (the `DEFERRED` facility is gone — the contract is now resolved at runtime **everywhere**, with no exception mechanism), the **live S3 suite's** convention (`PAA_TEST_S3_*`, the `requires_s3` marker, `paa[s3]` in the dev extra), the **docs-reference checker**'s scope and why, the **CI workflow**'s commands and the `python-is-python3` landmine, and the **CI service containers**. Keep the file's voice: it is a list of *rules*, each with its *why*, and it is already long — do not pad it |
| `docs/adr/README.md` | the index. **The rows for 0032–0035 are already added** (the orchestrator did it); your job is to check the index's *shape* still describes the tree — that it claims nothing that is not there, and misses nothing that is |

## The rules for a records commit

1. **Every claim must be checkable.** "The validation layer is now covered"
  is not a claim; "the five headline clauses of `src/paa/validation/` each
  have a suite, named in `tests/validation/`" is. Prefer the second.
2. **Do not overstate.** The suites exist; the coverage is *not* total. Where
   the ROADMAP's clause is only partly met, say which part and what is left.
   A records commit that overstates is worse than no records commit.
3. **Do not restate the run's reports.** `docs/agent-ops/20261022-w6-w7-w9/`
   is the trail; these files are the *project's* records. Link, do not copy.
4. **House style.** The *why*, not the *what*; a rule with its reason; no
   emoji; no "simply", "just", "obviously".

## What you must read first

* `docs/plans/w6-w7-w9.md` — the authoritative scope, decisions D-1…D-6, and
  the evidence in §0.
* `docs/agent-ops/20261022-w6-w7-w9/01-work-ledger.md` — the live state, the
  **open items**, and what each task committed.
* `docs/agent-ops/20261022-w6-w7-w9/gates/` — the gate records (the numbers
  are the evidence for "the gate is green").
* `docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-03-delta.md` and
  `reports/w2-improve/W2-FIX-03-last-round.md` — the honest verdicts, including
  the two **operator decisions** the run leaves open.
* The ADRs this run wrote (0032–0035) — they are the record of the decisions
  your status lines refer to.

## Two things you must NOT do

* **Do not** close the two open items. They are the operator's decisions: the
  unreachable egress check in `_check_lockdown` (making it reachable would
  change what the policy refuses) and the five pattern-semantics findings.
  If a status line would otherwise imply they are closed, say they are open
  and point at the ledger.
* **Do not** edit the source, the tests, or anything outside your write-set.
  If a record needs a fact you do not have, say so in your report and mark it
  UNVERIFIED — do not guess a number.

## Ownership

**WRITE-SET:** `docs/MCP_AND_PLUGINS.md`, `docs/ROADMAP.md`, `AGENTS.md`,
`docs/adr/README.md`.

**READ-ONLY:** everything else — `docs/plans/**`, `docs/agent-ops/**`,
`docs/adr/00*.md`, `src/**`, `tests/**`.

**FORBIDDEN:** `src/**`, `tests/**`, `pyproject.toml`, `deploy/**`,
`docs/adr/00*.md` (the ADRs themselves — the orchestrator owns them),
`docs/plans/**`, `docs/agent-ops/**` (read; the orchestrator owns the trail).

**Conflict note:** W3-IMPL-02 (the docs sweep) owns the markdown *except*
your four files, and its checker may flag a reference **inside** your files.
If it reports one, fix it in your file — you own it — and say so in your
report. Its write-set and yours do not overlap.

## Acceptance criteria

* Each of the four files says what is true, and every claim in them is
  checkable against the tree or the gate records.
* The ROADMAP's Phase 0 table names, per item, **what now covers it** and
  **what does not**.
* `AGENTS.md` gains the rules the run introduced, in the file's voice, and
  loses nothing.
* The two open items are visible as open.
* The docs-reference checker (W3-IMPL-02's) is **green** after your edits —
  run it, and fix any reference your edits broke.

## Deliverables

Report `docs/agent-ops/20261022-w6-w7-w9/reports/w3-impl/W3-IMPL-04-T-012.md`
(ORCH-DIGEST first). Commit the records first, then the report, docs-only,
explicit paths, on branch `ops/20261022-w6-w7-w9`, trailers `Task: T-012`,
`Agent: W3-IMPL-04`, `Wave: 3`.
Final message ≤ 8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
