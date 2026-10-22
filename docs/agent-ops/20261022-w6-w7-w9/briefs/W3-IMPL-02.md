# Brief — W3-IMPL-02 (implementer) · wave 3 · run 20261022-w6-w7-w9

**Task T-010 — W7-b4: the stale documentation, and the checker that stops it
returning.**

## The requirement (the plan's §3.3, verbatim)

> Fixing ~26 wrong `path:line` references without a guard means they rot again
> inside a month. T-010 does both: the sweep, and a **docs-reference
> checker** in the suite that extracts every `path:line` from `docs/**/*.md`
> and asserts the file exists, the line exists, and **the line is not blank**
> (a blank cited line is almost always a wrong reference — that is how
> `cli/main.py:208` and `daemon.py:61` were found).

The sweep covers: the five documents W7 names, the two worst offenders in
`docs/operations/`, the three out-of-range references, README's three
contradictory test counts, and ADR-0019's stale paragraph.

**T-010 runs last** (the plan says so) because the code moves the cited lines
— the code has now settled (wave 2 is closed), so the sweep is safe now.

## Two parts

### Part 1 — the sweep

* Find every wrong reference yourself: write the checker **first**, let it
  find them, then fix them. Do not fix from a list you made by hand — the
  point is that the list is derived.
* A wrong reference is not only a *wrong* line: a reference to a file that
  does not exist, a line past the end of the file, a **blank** line, or a
  line that no longer contains what the sentence claims. The last kind is the
  expensive one — the checker cannot see it, so **read** each corrected
  reference and make sure the sentence and the code still agree. Say in the
  report how many references you checked by hand.
* README's three contradictory test counts: the plan records that the
  per-subsystem counts are approximate and that *"the total line is stale
  too, so reconcile neither against it"*. Fix the counts so they are
  **true**, and do not invent precision the numbers do not have.

### Part 2 — the checker

`tests/conventions/test_docs_references.py`:

* extract every `path:line` reference from the markdown;
* assert the **file** exists;
* assert the **line** exists (not past the end);
* assert the cited line is **not blank**;
* and it must be a **lock**, not a report: it runs in the suite, so the gate
  fails on a stale reference.

**The scope question, which you must decide and write down.** The checker's
scope must be **stated in the checker itself** and justified. Two things
argue for a scope that is not "everything":

* `docs/agent-ops/**` is this run's own **written trail** — a directory the
  next run will not read, whose references were true when they were written
  and are allowed to have rotted since. The repo says so itself: a
  run-scoped report is *"a directory the next run will not read"*. **This
  one is settled: exclude `docs/agent-ops/**` from the checker's scope, and
  do not edit anything in it** — a record of what was true when it was
  written is not a stale reference, it is a record.
* An ADR is a **permanent record** of a decision, and its references describe
  the code as it was when the decision was taken. Read ADR-0019 and decide
  whether its references are claims about *now* or about *then* — and if the
  answer is "then", either fix them to point at what they claim, or exclude
  them with a stated reason.

Whatever you decide: the exclusions must be **explicit, narrow, and
documented in the file**, and the checker must still catch a reference that
rots *inside* its scope. Prove it: break a reference in a scratch copy and
show the checker goes red.

## Constraints

* **Do not** make the checker pass by excluding the documents that fail.
  Each exclusion must be a *reason*, stated in the file.
* Do not rewrite a document's *meaning* to make a reference fit. The
  reference is wrong, or the sentence is — decide which, and if it is the
  sentence, say so in the report.
* Every fix must keep the document's voice: the house style is the *why*, and
  a docstring that carries an RFC or ADR reference carries a **real** one.

## Ownership

**WRITE-SET:** the markdown the checker covers, except the four files below;
`README.md`; `docs/adr/0019-*.md`; `tests/conventions/test_docs_references.py`
(new).

**RESERVED for the orchestrator (do NOT edit):** `docs/adr/README.md`,
`docs/MCP_AND_PLUGINS.md`, `docs/ROADMAP.md`, `AGENTS.md` — the records
commit lands after this wave. Your checker may **read** them; if one of them
fails your checker, **report it** (with the exact reference) rather than
editing the file.

**FORBIDDEN:** `src/**`, `tests/**` except `tests/conventions/test_docs_references.py`,
`pyproject.toml`, `deploy/**`, `.github/**`, `docs/plans/**`.

## Acceptance criteria

* The checker exists, runs in the suite, and is a **lock**: a broken
  reference in its scope turns it red (prove it, with the transcript).
* Every reference the checker flags is **fixed** in the documents you own, or
  is in a documented exclusion with a stated reason.
* The README's test counts are **true**.
* The report says how many references were fixed, how many were checked **by
  hand** for the "the sentence still matches the code" property, and which
  files are excluded and why.
* `ruff check tests/conventions` is 0, `mypy src` is 0, and the gate does not
  regress.

## Deliverables

Report `docs/agent-ops/20261022-w6-w7-w9/reports/w3-impl/W3-IMPL-02-T-010.md`
(ORCH-DIGEST first). Commit the checker and the sweep first, then the report,
docs-only, explicit paths, on branch `ops/20261022-w6-w7-w9`, trailers
`Task: T-010`, `Agent: W3-IMPL-02`, `Wave: 3`.
Final message ≤ 8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
