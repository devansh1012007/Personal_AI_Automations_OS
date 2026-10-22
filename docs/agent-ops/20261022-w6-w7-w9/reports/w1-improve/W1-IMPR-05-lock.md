<!-- ORCH-DIGEST:START -->
agent: W1-IMPR-05 | scope: R-W1-308 (the aggregate import probe's uncaught TimeoutExpired) + R-W1-309 (the durable, checkable classification of the remaining TYPE_CHECKING blocks) | status: DONE
findings: FIXED 2 | REJECTED 0 (with evidence) | DEFERRED 0
blockers_major_open: none
code_commits: 6bf2343 | report_commit: (this commit)
tests_added_or_strengthened: 5
ownership_violations: none
confidence: high (every number re-derived with my own AST sweep and my own import measurements; 12 mutations run, 12 caught)
<!-- ORCH-DIGEST:END -->

# Improvement report — W1-IMPR-05 (the import lock)

**Agent:** W1-IMPR-05 (improver, second and final improve cycle of wave 1).
**Task:** R-W1-308 and R-W1-309, both in `tests/conventions/test_import_conventions.py`.
**Write-set:** `tests/conventions/**` only (brief §5). Nothing outside it was edited.
**Branch:** `ops/20261022-w6-w7-w9` ([VERIFIED `git branch --show-current`]).
**Last COMMS line read before committing:** `2026-10-22T02:31:47Z W1-IMPR-04 -> ALL | START
| W1-DELTA` — the parallel improver's write-set is `deploy/fetch-images.sh`,
`tests/deployment/test_fetch_images.py`, `docs/adr/0030-*`, `docs/deployment/docker.md`,
`src/paa/storage/coldlake/{minio_backend,base}.py`, `docs/adr/0032-*`,
`tests/storage/{test_minio_backend,test_blobstore_contract}.py`. **No overlap with
`tests/conventions/**`**, and no file of theirs was touched.

> **Note on the brief.** `docs/agent-ops/20261022-w6-w7-w9/briefs/W1-IMPR-05.md` did **not
> exist** when this agent started ([VERIFIED `ls docs/agent-ops/20261022-w6-w7-w9/briefs/` —
> W1-IMPL-01..03, W1-IMPR-01..03, W1-REV-01..02, W1-REVIEW-CONTEXT, W1-TREV-03 only; the
> parallel improver W1-IMPR-04 reports the same gap for its own brief]). The orchestrator's
> task message is therefore used as the brief, and every instruction in it is treated as
> binding: the write-set restriction, the two findings, the orchestrator's approval of the
> durable-table design, and the instruction to re-derive the classification rather than
> copy it.

---

## 1. Finding-by-finding disposition

| Finding | Severity | Disposition | What I did / why rejected or deferred (with evidence) | Commit |
|---|---|---|---|---|
| **R-W1-308** — the aggregate import probe's `TimeoutExpired` is uncaught | MINOR | **FIXED** | The `subprocess.run` at the aggregate probe had `timeout=300` and no `except`, while the two per-module probes already caught it. A hung child is exactly what the timeout exists to catch, and it escaped as a traceback that named neither the timeout nor a module. The probe now announces every import it starts, catches the timeout, and fails with a verdict naming the timeout, the module it was on, and the child's stderr. A new test hangs a real child on purpose and pins the verdict. [RAN 1 mutation — the catch removed — the new test FAILED] | `6bf2343` |
| **R-W1-309** — the classification of the remaining `TYPE_CHECKING` blocks lived only in a run-scoped report, and the plan's two figures are wrong | MINOR | **FIXED** (the second half: the durable, checkable table) + **reported** (the first half: the plan's figures are the orchestrator's to correct) | The classification is now a table **beside `DEFERRED`** in the lock module, keyed by module with the class as the value, with a **completeness check** that re-runs the same AST sweep and compares the two sets. The class is not prose: five of the six classes are mechanical properties of the block and are enforced, the sixth is a judgement whose evidence (the measured price of every block) is recorded beside it. The plan's figures are **not** mine to correct — `docs/plans/w6-w7-w9.md` is orchestrator-owned and outside my write-set — so the corrected statement is in §7 below and on COMMS. [RAN 9 table mutations + 3 tree mutations — every one caught] | `6bf2343` |

Nothing was rejected and nothing was deferred.

---

## 2. Code changes (per file, with rationale; root-cause level, no band-aids)

One file changed: **`tests/conventions/test_import_conventions.py`** (+594 / −24 lines).
It is the only file in my write-set, and the only file the two findings are in.

### 2.1 R-W1-308 — the aggregate probe

**The defect.** [VERIFIED `tests/conventions/test_import_conventions.py:622-631` at HEAD]
the aggregate probe was:

```python
script = "\n".join(f"import {name}" for name in importable)
completed = subprocess.run(
    [sys.executable, "-c", script], capture_output=True, text=True, timeout=300,
)
assert completed.returncode == 0, (...)
```

`subprocess.TimeoutExpired` was **not** caught, while the two per-module probes
(`_probe`, `_probe_without`) both caught it and turned it into a `Probe` with
`error_type="TimeoutExpired"`. The asymmetry is the finding: the one probe whose
timeout was *longest* was the one that could not report a hang.

**The fix, and why it is not a band-aid.** The root cause is that the probe had
**no way to say which module it was on** when it died — the child imported
silently, so a timeout produced a traceback and a guess. The fix therefore has
two halves:

1. the child **announces** every import before it makes it
   (`print('importing\t<module>', flush=True)`), so the parent's partial output
   names the module that was in flight;
2. the parent **catches** the timeout and turns it into a verdict that names the
   timeout, that module, and the child's stderr.

The verdict is returned as a string by `_aggregate_import()` and asserted in the
test, rather than raised from the `except` block. That is a deliberate choice:
`pytest.fail` inside the `except` renders as *"During handling of the above
exception, another exception occurred"*, and a raised `AssertionError` renders
as a full Python traceback of `subprocess.py`. A returned verdict renders as
one line of prose — [RAN] the rendering was compared on a scratch test before
the shape was chosen.

**Two details that are easy to get wrong, and were got right here:**

- `subprocess.run` re-raises `TimeoutExpired` with the output the pipe reader
  had collected, and on POSIX that output is **bytes** even when the call asked
  for `text=True` — the decoding happens inside `Popen.communicate`, and the
  timeout path never gets there. [VERIFIED by running it: `TimeoutExpired.stdout`
  was `b'importing\tpaa.foo\n'`.] `_as_text()` therefore checks the type
  instead of assuming it; a handler that assumed `str` would raise
  `AttributeError` inside the very handler that exists to report a hung child.
- The timeout became a named constant, `AGGREGATE_IMPORT_TIMEOUT_SECONDS = 300`,
  separate from `IMPORT_TIMEOUT_SECONDS = 120`. The two answer different
  questions — "this module hung" and "the package hung" — and a failure message
  that named the wrong one would send a reader to the wrong place.

The non-timeout failure path also names the last module the child reached, which
it could not do before.

### 2.2 R-W1-309 — the durable classification

**The design the orchestrator approved** (task message): *a table beside the
existing `DEFERRED` list in the lock module, keyed by module, with the class as
the value, plus a check that the table is complete — run the same AST sweep the
table was derived from and assert that the set of modules in the table equals the
set of modules that still have a `TYPE_CHECKING` block.*

Implemented as three module-level constants in
`tests/conventions/test_import_conventions.py`, immediately after `DEFERRED`:

- `TYPE_CHECKING_CLASSES: dict[str, str]` — **the table**: module → class.
- `TYPE_CHECKING_CLASS_KEYS: frozenset[str]` — the six class keys.
- `TYPE_CHECKING_NOTES: dict[str, str]` — module → why it is in that class,
  including the **measured** price of converting it.

**The class is not prose.** Five of the six classes are *mechanical* properties
of the block, and the checks enforce them:

| Class | Meaning | How it is enforced |
|---|---|---|
| **A** | a real import cycle | proved: the block is converted in a **copy** of the package and the import must fail, with a cycle marker in the error |
| **B** | an optional extra | proved: with the extra blocked the module must import (the guard is what keeps it importable), and with the block converted and the extra blocked it must not |
| **C** | deliberate laziness | a **judgement** — the only class not mechanically derivable; the measured price of every block is recorded so it can be re-examined |
| **D** | the guard buys nothing | mechanical: the block's imports are **all** standard library |
| **E** | first-party, cheap, inert | mechanical: the block names **no** third-party package, and is not all-stdlib |
| **F** | the family | mechanical: the set of class-F modules equals the set of `DEFERRED` modules |

The biconditionals are enforced in both directions: only **B** may name a
third-party package, only **D** may name *only* the standard library. A class
assignment that is wrong in either direction fails with a message that says
which class it should have been.

**Class C is a judgement, and the report says so.** The class says the guard was
written *on purpose*, and the price does not separate C from E by itself:
`paa.models` is classed **E** and costs **+15.1 ms** to convert — more than four
of the ten **C** members cost. The table's docstring says this in as many words,
so a later reader is not left to infer a threshold that is not there. The
mechanical facts that *are* in the tree are recorded per module in
`TYPE_CHECKING_NOTES` (which module the guard keeps out, and the measured price).

**The completeness check** is the one the orchestrator asked for, and it is
strict in both directions:

```
the modules that still have a TYPE_CHECKING block  ==  the keys of the table
```

so a new block has to be classified before the lock lets it through, and a
converted block has to leave the table. The sweep it compares against is
`ast.walk` over the whole module — **not** only module-level guards — because a
guard nested inside a function still defers an import, and a table that only saw
the module-level ones would be incomplete against the tree it claims to
describe. [VERIFIED: at HEAD all 33 guards in the tree are at module level, so
the two readings agree today; the wider one is used so that a future nested
guard is caught rather than ignored.]

Three further invariants are checked with the same sweep, because a table that
can go stale in one direction can go stale in the others too:

- every classified block has a note, and no note outlives its block;
- every class used is a known class, and **no class is left without a member**
  (a class the tree has outgrown is a class the table has to drop);
- the class-F set equals the `DEFERRED` set — the family and the deferral map
  are two views of one fact, and they have to agree.

**Two supporting changes to the existing helpers**, both needed by the proofs
and both backwards compatible:

- `_probe()` and `_probe_without()` gained an optional `cwd`. The reason is
  real: a child started in a directory that *contains* a `paa` package would
  import that one instead of the copy on `PYTHONPATH`, and the copy is the thing
  under test. The default is unchanged, so the module-scoped `probes` fixture
  (136 subprocesses) behaves exactly as before.
- `PACKAGE_NAME` is now annotated `str`. `paa.__name__` is `Any` to a type
  checker, and an `Any` in a path expression makes the whole expression `Any`
  — which mypy reported as `Returning Any from function declared to return
  "Path"` in the new code. [VERIFIED `reveal_type(PACKAGE_NAME)` → `Any`.]

**The proofs never touch the repository.** `_shadow_package()` copies
`src/paa` into `tmp_path` (excluding `__pycache__`), `_hoisted_source()` rewrites
the *copy's* file with the guard converted to a runtime import (`ast.unparse`),
and the probe imports the copy. The tree is not the thing under test — the tree
would be, and the tree must not change.

---

## 3. Test changes

Five tests were added to `tests/conventions/test_import_conventions.py`; the
file went from 13 tests to 18 ([RAN `pytest tests/conventions -q` → **18 passed
in 10.56 s**]).

| Test | What it pins | Fails without the fix? |
|---|---|---|
| `test_a_hung_aggregate_import_is_a_verdict` | a hang is a **verdict** naming the timeout and the module | yes — with the catch removed, `TimeoutExpired` escapes and the test FAILS [RAN] |
| `test_every_type_checking_block_is_classified` | the table is complete, every block has a note, every class is known and non-empty | yes — a dropped entry, a spurious entry, a missing note and an emptied class each FAIL [RAN ×4] |
| `test_the_classification_says_what_it_claims` | the class is the mechanical property it claims; the family equals the deferral map | yes — four wrong-class mutations FAIL [RAN ×4] |
| `test_the_classified_cycle_is_still_a_cycle` | the class-A block really closes a cycle, today | yes — with the cycle removed the converted copy imports cleanly and the test FAILS [RAN] |
| `test_the_classified_extra_still_needs_the_extra` | the class-B block really keeps the module importable without the extra | yes — with the extra no longer named by the guard the test FAILS [RAN] |

**The hang test hangs a real child.** The timeout is monkeypatched to one second
and the child is `time.sleep(30)` — a real hang, produced by a real sleep, not a
mock of the probe. The thing under test is what the *parent* does with a child
that stops answering, and a mock would test the mock. The test costs ~1 s.

**The class-A and class-B proofs cost two subprocesses each** and are the only
new tests that touch the filesystem; both work in `tmp_path` and copy 4.9 MB.
Total suite time went from ~9 s to ~10.5 s.

**No test was weakened, skipped, or deleted.** The existing thirteen are
untouched except for the aggregate probe's body (the test's *name, docstring and
assertion* are the same, and the assertion is now `assert verdict == "",
verdict`).

---

## 4. Optimizations applied and evidence of benefit (and ones declined, with reason)

- **The aggregate probe now names the hung module** instead of only saying
  "timed out". Cost: 135 extra `print` calls in a child that is already
  importing 135 modules — [RAN] the aggregate's wall clock is unchanged
  (the suite's total went from ~9 s to ~10.5 s, and ~1 s of that is the new
  hang test). Benefit: the timeout becomes actionable.
- **The completeness check parses the tree once** per run (137 files,
  `ast.parse` + `ast.walk`), and it is independent of the 136-subprocess sweep
  the lock already pays for. Measured cost: under a second.
- **Declined: re-measuring the import prices inside the test.** The prices in
  `TYPE_CHECKING_NOTES` are host-specific and would drift; making them a
  *test* would mean a threshold, and a threshold is a flakiness generator
  (the same import costs 129 ms on one host and 175 ms on another). The prices
  are therefore **evidence recorded once**, with the method and the date in the
  docstring, and the *classification* — the thing the lock depends on — is
  checked mechanically. A reader who disagrees with a class can re-run the
  measurement; they do not have to trust the number.
- **Declined: converting the class-C and class-D blocks.** The blocks are in
  `src/paa/**`, which is **not** in my write-set. The classification now
  records them as facts about the tree, which is what the finding asked for;
  the conversion is a separate task with an owner.
- **Declined: putting the classification in a new module** (e.g.
  `tests/conventions/type_checking_classes.py`). The brief said *beside* the
  `DEFERRED` list in the lock module, and a second module would split the lock
  across two files for no gain.

---

## 5. Commands run and results (scoped)

Every command below is scoped to my file or to a scratch copy. **The full suite
was not run**, no container was touched, no port was bound, no shared state was
modified.

```
[RAN] .venv/bin/python -m ruff check tests/conventions/            -> All checks passed!
[RAN] .venv/bin/python -m mypy tests/conventions/test_import_conventions.py
                                                                   -> Success: no issues found in 1 source file
[RAN] .venv/bin/python -m pytest tests/conventions/ -q             -> 18 passed in 10.56s
[RAN] .venv/bin/python -m ruff check src tests                     -> All checks passed!   (read-only, repo state)
[RAN] .venv/bin/python -m mypy src                                 -> Success: no issues found in 137 source files
```

**My own AST sweep** (independent of the lock's helpers, `/tmp/opencode/w1impr05/sweep.py`):

```
[RAN] sweep over src/paa at HEAD                       -> BLOCKS=33  MODULES=33
[RAN] sweep over src/paa at 02f3636 (git archive)     -> BLOCKS=41  MODULES=41
[RAN] sweep over src/paa at 7747fa0 (git archive)     -> BLOCKS=34  MODULES=34
[RAN] sweep over src/paa at a3e6819 (git archive)     -> BLOCKS=33  MODULES=33
[RAN] guard shapes: 0 guards with an else-branch, 0 negated `not TYPE_CHECKING` guards,
     0 guards with a non-import body, guard-set == type-checking-import-set (33 == 33)
```

**My own measurement of every block** (each block converted to a runtime import
in a copy of the tree; the importing module's own import time timed in a fresh
interpreter, five runs, minimum taken):

| Module | Δ import time | Hoisted import | Class |
|---|---:|---|---|
| `paa.daemon` | +128.8 ms | ok | C |
| `paa.models.embeddings` | +48.0 ms | ok | C |
| `paa.agents.base` | +39.7 ms | ok | C |
| `paa.runtime` | +32.7 ms | ok | C |
| `paa.api.app` | +176.9 ms | ok | **B** (needs `fastapi` at module scope) |
| `paa.models.router` | +14.4 ms | ok | C |
| `paa.models` | +15.1 ms | ok | E |
| `paa.coder.runtime_tools` | +13.4 ms | ok | C |
| `paa.cli.main` | +12.1 ms | ok | C |
| `paa.coder.agent` | +11.4 ms | ok | C |
| `paa.improvement.reflection` | +7.5 ms | ok | C |
| `paa.agents.orchestrator` | +1.1 ms | ok | C |
| `paa.skills.adapters.base` | +2.5 ms | ok | E |
| `paa.skills.usa` | +2.2 ms | ok | E |
| `paa.storage.vector.numpy_backend` | +1.9 ms | ok | E |
| `paa.observability.tracing` | +0.9 ms | ok | E |
| `paa.skills.adapters.claw_hub` | +0.7 ms | ok | E |
| `paa.storage.relational.postgres` | +0.4 ms | ok | D |
| `paa.models.registry` | +0.2 ms | ok | E |
| `paa.storage.vector.qdrant_backend` | +0.1 ms | ok | E |
| `paa.observability.logging` | −0.1 ms | ok | E |
| `paa.storage.relational.connection` | −0.1 ms | ok | D |
| `paa.context.budget` | −0.6 ms | ok | D |
| `paa.memory.creator` | −0.5 ms | ok | E |
| `paa.skills.adapters.mcp` | −0.5 ms | ok | E |
| `paa.skills.adapters.native` | −0.4 ms | ok | E |
| `paa.storage.graph` | −0.4 ms | ok | E |
| `paa.storage.vector` | −0.7 ms | ok | E |
| `paa.memory.world_model` | −0.9 ms | ok | D |
| `paa.coder.model_adapter` | −1.8 ms | ok | E |
| `paa.observability.runs` | −1.4 ms | ok | F |
| `paa.storage.coldlake` | −4.3 ms | ok | E |
| **`paa.storage.coldlake.cas`** | — | **FAILS** (`cannot import name 'BlobStore' from partially initialized module 'paa.storage.coldlake.base'`) | **A** |

The orderings and magnitudes reproduce W1-IMPR-03 §7's numbers on a different
host (mine: daemon +128.8, models.embeddings +48.0, agents.base +39.7, runtime
+32.7; the report's: +132, +53, +39, +38) — the same four modules at the top,
within the noise a different host explains. **Exactly one** block breaks the
import when converted, and it is the one the report named.

**Mutations** (scratch copy of `src/` and `tests/` under `/tmp`; the repository
was restored after each):

| # | Mutation | Caught by | Result |
|---|---|---|---|
| 1 | the aggregate no longer catches `TimeoutExpired` | `test_a_hung_aggregate_import_is_a_verdict` | **FAILED** |
| 2 | a block is dropped from the table | `test_every_type_checking_block_is_classified` | **FAILED** |
| 3 | a module with no block is added to the table | `test_every_type_checking_block_is_classified` | **FAILED** |
| 4 | a classified block loses its note | `test_every_type_checking_block_is_classified` | **FAILED** |
| 5 | a stdlib-only block is called `E` | `test_the_classification_says_what_it_claims` | **FAILED** |
| 6 | a first-party block is called `D` | `test_the_classification_says_what_it_claims` | **FAILED** |
| 7 | the extra block is called `E` | `test_the_classification_says_what_it_claims` | **FAILED** |
| 8 | the family block is called `E` | `test_the_classification_says_what_it_claims` | **FAILED** |
| 9 | class `A` is left with no member | `test_every_type_checking_block_is_classified` + `test_the_classified_cycle_is_still_a_cycle` | **FAILED** (×2) |
| 10 | **tree**: `context/budget.py`'s guard converted | `test_every_type_checking_block_is_classified` + `test_the_classification_says_what_it_claims` | **FAILED** (×2) |
| 11 | **tree**: `cas.py`'s cycle removed (the guard names a harmless module) | `test_the_classified_cycle_is_still_a_cycle` | **FAILED** |
| 12 | **tree**: `api/app.py`'s guard no longer names the extra | `test_the_classified_extra_still_needs_the_extra` | **FAILED** |

**12 mutations, 12 caught.**

### The long-run test (standing order 8)

- **Source.** The classification table + the lock's other six halves, in
  `tests/conventions/test_import_conventions.py`. One file, one table, one
  sweep.
- **Load (100×, concurrency, partial failure).** The completeness check parses
  137 files once per run — O(files), no concurrency, no shared state, and it is
  independent of the lock's 136-subprocess sweep. The two new proofs each copy
  4.9 MB and spawn two subprocesses; under a 100× parallel CI they are the
  most expensive tests in the file, and they are bounded (two modules today, one
  copy each). A partial failure — a copy that does not import — is caught by
  the *control* probe inside each proof, and reported as "the copy does not
  import, so the conversion below would prove nothing" rather than as a
  mysterious assertion.
- **Hostile input.** The table is source-controlled, so the hostile input is a
  hostile *table*: a fabricated entry (caught by the completeness check), a
  mislabelled class (caught by the per-class checks), a fabricated cycle (caught
  by the class-A proof), a fabricated extra (caught by the class-B proof). The
  tree's own input is the source file, which is valid Python by construction;
  `_hoisted_source()` parses it with `ast.parse` and cannot be fed a NUL or a
  traversal — the only path it touches is `tmp_path`, and the copy is written
  from `PACKAGE_ROOT`, never from a variable.
- **Operability.** The failure messages name the modules on **both** sides of a
  disagreement ("in the tree but not classified" / "classified but no longer in
  the tree"), the class each block should have had, and — for the aggregate
  timeout — the module the child was on. The table's own docstring carries the
  class definitions, so a reader who lands on the file does not have to go
  looking for the run-scoped report.
- **Reversal.** Reverting R-W1-308 fails `test_a_hung_aggregate_import_is_a_verdict`;
  reverting R-W1-309's table fails `test_every_type_checking_block_is_classified`
  and `test_the_classification_says_what_it_claims`. Measured, above.

---

## 6. Residual risks and anything the delta reviewer should check

1. **The class-C/E split is a judgement, and the table says so.** A reviewer who
   disagrees with a class should re-run the measurement (the method and the host
   are in the docstring) rather than argue with the number — the number does not
   decide the class. The two mechanically-defined classes (**D** and **B**) and
   the mechanically-proved ones (**A**, **F**) leave no room for judgement.
2. **The recorded prices are from one host on 2026-10-22** and will drift. They
   are evidence, not thresholds; nothing in the lock compares against them, so
   drift cannot break the suite.
3. **`ast.unparse` rewrites the whole file** of the copy it converts. That is
   safe here (the copy is thrown away, and `unparse` is semantics-preserving),
   but a reviewer who prefers a surgical edit should know the choice was made
   for speed and clarity, not necessity.
4. **The class-A proof copies the whole package** (4.9 MB, `__pycache__`
   excluded). If `src/paa` grows a lot, that copy becomes the most expensive
   thing in the file. It is bounded by the number of class-A modules, which is
   one.
5. **The aggregate probe's child now writes 135 lines to stdout.** They are
   discarded on success and parsed on failure. If a future reader wonders why
   the child's stdout is not empty, the answer is in `_last_module_started`.
6. **The lock now has a seventh half** and the module docstring says so. A
   reviewer should check that the docstring's list still matches the tests
   (seven bullets, seven halves).
7. **Two files in the tree were uncommitted by the parallel improver while this
   work ran** (`src/paa/storage/coldlake/{base,minio_backend}.py`,
   `tests/storage/test_minio_backend.py`, `deploy/fetch-images.sh`,
   `tests/deployment/test_fetch_images.py`, `docs/adr/0030-*`,
   `docs/adr/0032-*`, `docs/deployment/docker.md`). **None of them is in my
   write-set and none was touched.** The read-only `ruff check src tests` and
   `mypy src` runs reported above were taken against that shared state, so a
   reviewer re-running them after the other improver commits may see a different
   result for *their* files.
8. **Not verified here:** the lock's behaviour on a host without `fastapi`
   installed (the class-B proof blocks the extra with a `meta_path` finder
   rather than uninstalling it, so the *mechanism* is host-independent — but the
   class-B module was not re-probed on a genuinely minimal install), and the
   behaviour of the class-A proof on a host where `paa` is installed as a wheel
   rather than editable (the copy is placed on `PYTHONPATH`, which precedes the
   install path, so the copy still wins — measured here, not on a wheel
   install).

---

## 7. The corrected statement for the plan (orchestrator's to apply)

The plan (`docs/plans/w6-w7-w9.md:66-70`) says **"48 `if TYPE_CHECKING:` blocks"**
and **"nine are load-bearing"** for a real runtime cycle. **Both are wrong.**
The corrected statement, re-derived with my own sweep and my own measurements:

> **`src/paa` has 41 `if TYPE_CHECKING:` blocks in 41 modules at `02f3636`; 34
> after T-004 (`7747fa0`); 33 after the R-W1-110 fix (`a3e6819`) and 33 now.
> Exactly ONE of the 33 is load-bearing for a real runtime cycle
> (`src/paa/storage/coldlake/cas.py`, whose guard is at line **63** at HEAD — it
> was at line 62 when W1-IMPL-03 measured it, and moved when `ddef4cc` edited
> the file). The other 32 break no import when converted.**

[RAN — the four sweeps and the per-block conversion measurement are in §5. The
line number moved because W1-IMPR-01's `ddef4cc` edited `cas.py`; the *module*
did not change, and the table is keyed by module, so the table is unaffected.]

The **classification** the plan asked for now lives in
`tests/conventions/test_import_conventions.py` beside `DEFERRED`
(`TYPE_CHECKING_CLASSES`, with `TYPE_CHECKING_CLASS_KEYS` and
`TYPE_CHECKING_NOTES`), and it is **checked against the tree** rather than
claimed: 33 blocks, in 1 + 1 + 10 + 4 + 16 + 1, and a new block cannot appear
unclassified.

---

## 8. Commits

| SHA | What |
|---|---|
| `6bf2343` | the code: `tests/conventions/test_import_conventions.py` (R-W1-308 + R-W1-309) |
| (this) | this report, docs-only |
