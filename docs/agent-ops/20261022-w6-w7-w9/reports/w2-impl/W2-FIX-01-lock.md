<!-- ORCH-DIGEST:START -->
agent: W2-FIX-01 | task: W2-FIX-01 (the import-conventions lock) | status: DONE
code_commits: 8c189d1, 80ba09b | report_commit: (this commit)
files_changed: 1 (tests/conventions/test_import_conventions.py)
contracts_changed: none
tests: added 0, changed 0; scoped run: pass — pytest tests/conventions -> 17 passed; pytest tests/observability -> 136 passed; ruff check src tests -> 0; mypy src -> 0 errors in 137 files
ownership_violations: none
deferred_dependencies: none
out_of_scope_findings: 2 (both NITs; neither is a tree defect — see §13)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: whether the *next* agent to need a deferral will reintroduce the machinery correctly, now that the map is gone
confidence: high (the change is a deletion of a fulfilled facility, it is proved by four transcripts against mutated copies of the tree, and every factual claim in the touched docstrings was re-checked against the tree)
last_comms_line_read: 2026-10-22T04:38:31Z (the last line in COMMS.md — W2-IMPL-01's DONE for T-002; I read COMMS.md at the *end* of my task rather than at the start, which is a process miss on my part and is recorded in §12)
<!-- ORCH-DIGEST:END -->

# Implementation report — W2-FIX-01 — the import-conventions lock, after the last deferral

## 1. Task and acceptance criteria (restated)

`tests/conventions/test_import_conventions.py` is wave 1's file (task **T-004**,
`docs/plans/w6-w7-w9.md` §5). Wave 2's **T-008** (agent W2-IMPL-05, commits
`4808982` / `9a92f19` / `6117c4a`) converted `src/paa/observability/runs.py`
from a lazy `if TYPE_CHECKING:` import of `RelationalBackend` to a runtime
import, which **fulfilled** the map's one deferral entry. The lock was
designed to be self-invalidating, so it went red:

* `test_every_deferral_names_the_task_that_owns_the_file` — the deferred module
  no longer resolves the contract lazily, so the entry is stale;
* `test_every_type_checking_block_is_classified` — the classification table
  still lists `paa.observability.runs`, which no longer has a
  `if TYPE_CHECKING:` block.

No wave-2 agent owned the lock, so the coordination edit was mine.

**Acceptance criteria**

1. The lock describes the tree as it now is — no false statement about the
   tree survives in any docstring or comment I touched.
2. The lock keeps its teeth: a reverted conversion, a reverted
   `paa.observability.runs`, and an unclassified `TYPE_CHECKING:` block each
   make it **red**; the tree as it stands makes it **green**.
3. All eight `FAMILY` entries stay, and are **checked** (the eighth is
   converted like the other seven, so it belongs in the checks, not out of
   them).
4. The now-empty deferral facility is removed rather than left in place.
5. `ruff` 0, `mypy src` 0, and the wave-1/wave-2 suites do not regress.
6. The experiments are run in a **copy**; the working tree is left unmodified
   by them.
7. Write-set: `tests/conventions/test_import_conventions.py` and nothing else
   (plus this report).

## 2. Verification that the problem exists

Gathered by me, not copied from the investigation.

`[RAN .venv/bin/python -m pytest tests/conventions -q -> 2 failed, 16 passed]`
— the two failures, verbatim, from the lock at `HEAD~1` run against the tree
as it stands (transcript: `/tmp/opencode/w2fix01/transcripts/before-the-fix.txt`,
the pre-fix file copied to a scratch tree and run there):

```
E  AssertionError: paa.observability.runs is deferred but no longer resolves
   RelationalBackend behind `if TYPE_CHECKING:` — the deferral has been acted
   on, so the entry is stale and has to be deleted with the change that made
   it unnecessary.
E  assert ('paa.storage.relational.base', 'RelationalBackend') in set()

E  AssertionError: the classification and the tree disagree about which
   modules still have an `if TYPE_CHECKING:` block.
E    in the tree but not classified: []
E    classified but no longer in the tree: ['paa.observability.runs']
```

`[VERIFIED src/paa/observability/runs.py:39]` — the contract is now a runtime
import: `from paa.storage.relational.base import RelationalBackend`, and the
module has no `if TYPE_CHECKING:` block left (`grep -n TYPE_CHECKING
src/paa/observability/runs.py` → no match).

`[VERIFIED git show --stat --name-only 1c9ffa3 -- src/paa]` — commit `1c9ffa3`
touched exactly nine modules that name the contract: `memory/creator`,
`memory/contradiction`, `memory/decay`, `memory/curator`,
`marketplace/installer`, `skills/registry`, `agents/policy`,
`agents/context_agents`, `observability/runs`. Nine is the number the
docstring's history refers to, and it is the number that is still true.

`[VERIFIED grep -rln RelationalBackend src/paa --include=*.py -> 22 files]` —
22 modules *name* the contract; nine of them are the family commit `1c9ffa3`
touched. The other thirteen never had a lazy import and are unaffected.

`[VERIFIED docs/plans/w6-w7-w9.md §5]` — T-004's write-set is the seven family
modules wave 1 converted; T-008's write-set includes
`src/paa/observability/runs.py`. So "seven by wave 1 (T-004), the eighth by
wave 2 (T-008)" is not a guess — it is the plan's own split.

## 3. Root cause

Not a bug in the tree: the tree is right and the **lock** was describing the
tree as it used to be. Wave 1's lock was written to be *self-invalidating* —
the map's entry asserted "a deferred module must **still** resolve the
contract lazily", so the moment T-008 converted the module, the entry stopped
being true and the lock went red. That is the mechanism working, not failing.

The root cause of the *red* is therefore: **a fulfilled deferral whose
fulfilment is not yet reflected in the lock's three tables** (`DEFERRED`,
`TYPE_CHECKING_CLASSES`, `TYPE_CHECKING_NOTES`) and in the prose that
describes them.

The deeper question the brief asks — what an *empty* `DEFERRED` map means —
has an answer that is not a matter of taste, because **the file's own check
forbids it**:

```python
empty = sorted(TYPE_CHECKING_CLASS_KEYS - used)
assert not empty, (
    f"class(es) {empty} have no member left — the tree has outgrown them, so "
    "the classification has to say so"
)
```

`[VERIFIED tests/conventions/test_import_conventions.py:1231-1234 at HEAD~1]`
— this assertion sits inside `test_every_type_checking_block_is_classified`.
Keeping `"F"` in `TYPE_CHECKING_CLASS_KEYS` with no `"F"` row in the table
would have made the lock **red on its own**, with a message that says exactly
what happened: *the tree has outgrown them*. The same standard the file
applies elsewhere is stated twice in its own words:

* *"A class with no member is a class the tree has outgrown, and the check
  below refuses one."* (`TYPE_CHECKING_CLASS_KEYS`'s comment)
* *"no class in the family names the contract in its own annotations, so this
  check is vacuous — it covered …"* (the annotation half's own refusal of a
  vacuous check)

So the answer is **removal**, and it is not a judgement call: an empty map
plus a check that iterates it is a facility with no user and a check that can
no longer fail, and the file's own logic already says so out loud.

## 4. Approach chosen and why

**Chosen:** delete the deferral facility whole — the map, the class `"F"`,
the `"F"` key in `TYPE_CHECKING_CLASS_KEYS`, the check that iterated the map
(`test_every_deferral_names_the_task_that_owns_the_file`), and every
`not in DEFERRED` / `in DEFERRED` condition in the other checks — and keep
all eight `FAMILY` entries, now all checked.

**Why this is removal and not "keep an empty map"**

1. *The file's own logic forbids the alternative.* Keeping `"F"` in the key set
   — which is what a half-finished removal leaves behind — makes
   `test_every_type_checking_block_is_classified` fail, with a message that
   says the tree has outgrown the class. So "keep the empty map and keep the
   class" is not an option that leaves the file green.
2. *A check that iterates an empty map cannot fail.* The removed check was
   `for module, reason in DEFERRED.items(): ...` — with one entry it had four
   assertions; with none it has none. A test that cannot fail is not a test
   (standing order 5), and the file says so of its own vacuous checks.
3. *The mechanism had exactly one user, and that user is gone.* The map was
   a **handover**, not a permanent escape hatch: it existed so that wave 1
   could convert seven of eight and wave 2 the eighth without either wave
   owning the other's file. That handover has completed.

**Why the other halves got *stronger*, not weaker**

Removing the map removes an *exception*. Every condition that referenced it
became unconditional:

| Check | Before | After |
|---|---|---|
| source half | `... in type_checking and name not in DEFERRED` | `... in type_checking` — no exception |
| runtime half | `if name in DEFERRED: continue` | the skip is gone |
| family half | `if name not in DEFERRED and not (...)` | `if not (...)` — all eight |
| annotation half | skipped the deferred module; asserted `checked == {n for n in FAMILY if n not in DEFERRED}` | no skip; asserts `checked == set(FAMILY)` — all eight |
| classification half | `family == set(DEFERRED)` cross-check | removed with the class it checked |

The annotation half is the sharpest example: before, it walked **seven**
family members; now it walks **eight**, and `paa.observability.runs` is in it.

**Alternatives considered and rejected**

* *Keep `DEFERRED = {}` and keep the check.* Rejected: the check becomes
  vacuous, and the file's own prose forbids a vacuous check. It would also
  leave a reader with the impression that a deferral is a normal thing to
  reach for, which is precisely the drift the map was meant to prevent.
* *Keep the class `"F"` for the family members that are already converted.* A
  class with no member — the file refuses one by name. Rejected.
* *Drop `paa.observability.runs` from `FAMILY`.* Rejected by the brief and by
  the file's design: `FAMILY` is the set of siblings the conversion was
  about, named so that "a regression points at the sibling that drifted rather
  than at the package as a whole". Dropping a converted member would make the
  set a record of what *was* converted rather than what *must* be, and it
  would remove the very module whose conversion this task exists to record.
  All eight stay, and all eight are checked.

**Root cause, not symptom.** The symptom is "two tests are red". The cause is
"a fulfilled handover is still recorded as an outstanding one, in three tables
and in prose". The fix removes the record everywhere it appears — the tables,
the class, the check, and the comments that asserted the deferral was still
outstanding.

## 5. What changed (per file)

| File | Change | Why |
|---|---|---|
| `tests/conventions/test_import_conventions.py` | the whole diff (+53 / −95, across two commits: `8c189d1` +53/−91 and `80ba09b` +1/−5) | the lock is wave 1's file; no wave-2 agent owned it |

The diff, in eight parts:

1. **Module docstring, opening** — "Nothing in the tree needed the class yet"
   was stale in two ways: the tree now *does* need it (the lock's own
   annotation half is the thing that needs it), and the historical claim had
   to be anchored in time. Now: *"Nothing needed the class then, which is
   exactly why the inconsistency survived a review; the lock's own annotation
   half needs it now, and reads every family member's hints at runtime to
   prove the class is there."* A new paragraph states the present truth: all
   eight converted (seven by `T-004`, the eighth by `T-008`), and the lock is
   what holds that true.
2. **Module docstring, the halves list** — "seven halves" → **"six
   halves"**; the **deferral** bullet is gone; the source half's bullet lost
   its *"except a documented deferral that names the task that owns the
   file"* clause.
3. **`FAMILY`'s comment** — the false *"Seven are converted here; the eighth
   belongs to wave 2 and is recorded in `DEFERRED`"* is replaced by the truth
   and by the reason the set is named at all. **All eight entries stay.**
4. **`DEFERRED`** — the map is deleted. In its place, an 18-line comment
   block (plain `#`, not `#:`, because it documents an *absence* and `#:`
   would attach it to the next attribute) says that there is no deferral map,
   why there used to be one, why the entry was self-invalidating, and that
   the machinery has to be **brought back with a check of its own** if a
   module genuinely cannot import the contract at runtime.
5. **`TYPE_CHECKING_CLASSES`** — the class-list docstring loses the `"F"`
   bullet, and *"Five of the six are mechanical"* becomes *"Four of the five
   are mechanical"* (A, B, D, E are mechanical; C is the judgement). The
   `"paa.observability.runs": "F"` row is deleted.
6. **`TYPE_CHECKING_CLASS_KEYS`** — `"F"` removed. Its own comment now does
   real work: it is the rule that forbids re-adding `"F"` without a member.
7. **`TYPE_CHECKING_NOTES`** — the `paa.observability.runs` note is deleted.
   The check requires `set(NOTES) == set(CLASSES)`, so a note outliving its
   block is refused by the file's own words.
8. **The checks** — `test_every_deferral_names_the_task_that_owns_the_file`
   is deleted; the other four lose their `DEFERRED` conditions and become
   unconditional; the source half's failure message now names class `A` (a
   real import cycle) as the only legitimate reason to keep the contract
   lazy — `base.py` imports nothing but the standard library and two
   first-party modules, so no *extra* can be the reason.

**No `src/` change. No other test changed.** `git show --stat 8c189d1` →
one file.

**Why two commits.** The first is the substantive change. The second
(`80ba09b`) is a one-line tidy-up: dropping the `DEFERRED` condition
shortened the family half's comprehension to a length `ruff format` wants on
one line, and I had left it wrapped. `80ba09b` collapses it, which keeps the
file's `ruff format` deviations at exactly the four pre-existing hunks
(verified against `HEAD~1`'s file). `--amend` is forbidden by the standing
orders, so it is a separate commit rather than a rewrite of the first.

## 6. Blast radius and contracts

* Grep run: `grep -rn "DEFERRED\|TYPE_CHECKING_CLASSES\|TYPE_CHECKING_CLASS_KEYS\|TYPE_CHECKING_NOTES\|FAMILY" src tests --include=*.py` →
  **every hit is inside `tests/conventions/test_import_conventions.py`**.
  Nothing else in the repository reads these names, so deleting `DEFERRED`
  cannot break an importer.
* `grep -rn "TYPE_CHECKING\|deferral\|RelationalBackend" AGENTS.md README.md
  docs/ROADMAP.md` → **no output**. No durable document in the repo makes a
  claim about the deferral, the classification table, or the contract's
  resolution. The lock file was the only place the truth was written down.
* **Contracts changed: none.** The public surface of the repository
  (`paa`'s API, the CLI, the API routes) is untouched. The change is to a
  test file's internal tables.
* **Behavioural change to the lock itself, and it is a tightening:** the
  lock no longer accepts *any* module that resolves the contract lazily, and
  the family half and the annotation half now cover eight members instead of
  seven. Nothing that was green before is red now; §7 proves the reverse
  direction too (things that should be red are red).
* **Backward compatibility / migration:** none needed. A future module that
  genuinely cannot import the contract at runtime would fail the source half
  with a message that names class `A`, and would have to bring the deferral
  machinery back *with a check* — which is the behaviour the file's design
  asks for.

## 7. Tests

**No test was added.** The task is to *update* a lock, and the proof that the
lock still has teeth is a property of the lock, demonstrated by mutating a
copy of the tree. The transcripts are below; the harness is
`/tmp/opencode/w2fix01/mutate.py` (copies `src/paa` into a scratch tree,
mutates the copy with `ast`, never touches the repository).

The scratch tree is put on `PYTHONPATH`, which shadows the editable install
(a `.pth` that appends `src` to `sys.path`, and `PYTHONPATH` precedes it) —
verified: `[RAN PYTHONPATH=<copy>/src .venv/bin/python -c "import paa; print(paa.__file__)" -> /tmp/opencode/w2fix01/proof2/src/paa/__init__.py]`.

### The four proofs

| # | Mutation (in a **copy**) | Result |
|---|---|---|
| 1 | `paa/skills/registry` reverted to a lazy import | **5 failed, 12 passed** — RED |
| 2 | `paa/observability/runs` reverted to a lazy import | **5 failed, 12 passed** — RED |
| 3a | a stdlib-only `if TYPE_CHECKING:` block re-added to `paa/storage/relational/database` (a module with no classification row) | **1 failed, 16 passed** — RED (the classification half, *alone*) |
| 3b | a *contract* `if TYPE_CHECKING:` block re-added to the same rowless module | **3 failed, 14 passed** — RED |
| 4 | the tree as it stands | **17 passed** — GREEN |

**Proof 1** — `paa/skills/registry` reverted (a converted family member):

```
FAILED …::test_the_contract_is_not_imported_behind_type_checking
FAILED …::test_every_module_that_imports_the_contract_binds_it_at_runtime
FAILED …::test_the_family_resolves_the_contract_one_way
FAILED …::test_the_family_can_have_its_annotations_read
FAILED …::test_every_type_checking_block_is_classified
5 failed, 12 passed in 12.72s
```

with the messages:

```
E AssertionError: ['paa.skills.registry'] resolve RelationalBackend behind
  `if TYPE_CHECKING:`, which is how one contract came to be expressed two ways
  in a single commit. Import it at runtime — …
E AssertionError: ['paa.skills.registry'] mention RelationalBackend but do not
  bind it in their namespace — the contract is resolved lazily there, …
E AssertionError: the family does not resolve the contract at runtime:
  ['paa.skills.registry']
E NameError: name 'RelationalBackend' is not defined      ← the annotation half
```

The `NameError` is the annotation half doing exactly what its docstring
promises: the hint cannot be *evaluated* when the name is bound only for the
type checker.

**Proof 2** — `paa/observability.runs` reverted (the module this whole task
is about):

```
FAILED …::test_the_contract_is_not_imported_behind_type_checking
FAILED …::test_every_module_that_imports_the_contract_binds_it_at_runtime
FAILED …::test_the_family_resolves_the_contract_one_way
FAILED …::test_the_family_can_have_its_annotations_read
FAILED …::test_every_type_checking_block_is_classified
5 failed, 12 passed in 13.08s
```

**Proof 3** — a `TYPE_CHECKING:` block re-added to a module with **no
classification row**. Two variants, because the interesting question is
*which half* catches it:

*3a, a block naming only the standard library* — the classification half
alone:

```
FAILED …::test_every_type_checking_block_is_classified
1 failed, 16 passed in 13.78s
E AssertionError: the classification and the tree disagree about which modules
  still have an `if TYPE_CHECKING:` block.
  in the tree but not classified: ['paa.storage.relational.database']
```

*3b, a block naming the contract* — the classification half **and** the
source and runtime halves:

```
FAILED …::test_the_contract_is_not_imported_behind_type_checking
FAILED …::test_every_module_that_imports_the_contract_binds_it_at_runtime
FAILED …::test_every_type_checking_block_is_classified
3 failed, 14 passed in 13.39s
```

**Proof 4** — the tree as it stands: **17 passed**.

### The scoped runs

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/conventions` | **17 passed** in 13.17s |
| `.venv/bin/python -m pytest tests/observability` (wave 2 T-008's suite) | **136 passed** in 4.64s |
| `.venv/bin/python -m ruff check src tests` | **All checks passed!** (0) |
| `.venv/bin/python -m ruff check tests/conventions` | **All checks passed!** (0) |
| `.venv/bin/python -m mypy src` | **Success: no issues found in 137 source files** (0) |

The lock went from **18 tests** (2 red) to **17 tests** (all green): the one
removed test is the deferral check, whose subject no longer exists.

`mypy src` is unaffected by a test file, as the brief says; it is listed for
completeness and was run.

**Flakiness:** every proof was run at least twice with identical outcomes
(the timings move, the counts do not). The lock's own probes are
subprocess-based and were not touched.

**NOT-RUN, and why:** the full suite. Standing order 16 forbids it, and the
change touches one test file whose whole directory is `tests/conventions`
(17 tests). `tests/observability` was run because it is the suite of the task
whose deferral this edit closes. The wave-1 suites that could conceivably
observe a contract-resolution change (`tests/storage`, `tests/ledger`) are
untouched by a change that alters no `src/` line — but I did not run them,
and I am not claiming they pass.

## 8. Long-run test

* **Source** — the lock's input is the tree. The lock reads it three ways:
  an AST sweep (`_module_files`, `_imports_of`, `_modules_with_type_checking_guards`),
  a per-module fresh-interpreter probe, and an aggregate import. All three are
  unchanged by this edit, so the lock's *coverage* of the tree is exactly what
  it was; only the tables changed.
* **Load (100x, concurrency, partial failure)** — the probes already run 8
  interpreters at a time (`PROBE_WORKERS = min(8, os.cpu_count())`) and the
  aggregate import runs the whole package in one interpreter in
  reverse-alphabetical order. This edit adds no work: it removes one check
  that iterated a one-entry map, and it makes the family half and the
  annotation half cover one member more. The annotation half's cost is one
  extra `importlib.import_module` plus one extra `typing.get_type_hints` per
  class that names the contract in that module — microseconds.
* **Partial failure** — the interesting failure modes are the four proofs: a
  reverted conversion, a reverted `paa.observability.runs`, an unclassified
  block. All three make the lock red. The lock's *own* failure modes (a hung
  child, a cycle) are covered by the lock's own tests, which were not
  modified.
* **Hostile input** — the lock's input is source code in the repository, not
  user input. The one hostile-input surface is a module with a `TYPE_CHECKING`
  block that the table does not know about, and proof 3 is exactly that.
* **Operability** — when the lock goes red, the message names the modules on
  both sides of the disagreement ("in the tree but not classified: […]",
  "classified but no longer in the tree: […]"), so the reader knows what to
  change. The source half names the offending module and the reason. The
  removed deferral check's message named the stale entry and the task that
  owned it — that message is now unnecessary because there is no map.
* **Reversal** — **which test fails if the fix is reverted?** Proofs 1, 2, 3a
  and 3b *are* that answer, and they are the four the brief asked for. A
  reviewer can re-run them in under a minute each:
  `PYTHONPATH=<copy>/src .venv/bin/python -m pytest tests/conventions/test_import_conventions.py`.

## 9. Edge cases and abuse scenarios

* **A module with no classification row gains a `TYPE_CHECKING:` block** —
  proof 3a: red.
* **A family member is reverted to a lazy import** — proofs 1 and 2: red, by
  five different checks.
* **A module outside the family gains a lazy *contract* import** — the source
  half scans the **whole package**, not the family, so it is caught; proof 3b
  shows a rowless module doing it.
* **The classification table drifts from the tree in either direction** — the
  check compares sets in both directions; the "classified but no longer in the
  tree" direction is exactly the failure that opened this task.
* **A class with no member is re-introduced** — the `empty` assertion in
  `test_every_type_checking_block_is_classified` refuses it. This is the
  check that made keeping `"F"` impossible.
* **A note outlives its block** — `set(NOTES) == set(CLASSES)` refuses it.
* **Deliberately not handled:** a module that *genuinely* cannot import the
  contract at runtime. There is no such module today, and inventing an
  exception for a case that does not exist is exactly the drift the file
  exists to prevent. The comment block at the old map's site says what such a
  module would have to do: bring the machinery back, with a check.

## 10. Performance and security considerations

* **Performance:** the lock's cost is dominated by 136 fresh-interpreter
  probes plus one aggregate import; this edit changes neither. Removing one
  check that iterated a one-entry map removes work. Adding
  `paa.observability.runs` to the annotation half adds one `import_module`
  and one `get_type_hints` — a module that is *already* imported by the
  sweep's probes. Net: immaterial.
* **Security:** none of this touches the sandbox, the executor, or any
  trust boundary. The change makes one assertion *stronger* (no module may
  resolve the contract lazily) and removes an *escape hatch* that a future
  author could have used to reintroduce a lazy resolution. The comment at the
  removal site says, in the file's own terms, that the machinery has to come
  back with a check rather than as a hole. No secret, key, or PII is
  introduced; the file contains none.

## 11. Commands run and results (verbatim summaries)

```
$ .venv/bin/python -m pytest tests/conventions -rA        # BEFORE the fix, HEAD~1's file
======================== 2 failed, 16 passed in 14.16s =========================
FAILED …::test_every_deferral_names_the_task_that_owns_the_file
FAILED …::test_every_type_checking_block_is_classified

$ .venv/bin/python -m ruff check src tests
All checks passed!
$ .venv/bin/python -m ruff check tests/conventions
All checks passed!
$ .venv/bin/python -m mypy src
Success: no issues found in 137 source files

$ .venv/bin/python -m pytest tests/conventions -rA         # AFTER the fix
17 passed in 13.17s

$ .venv/bin/python -m pytest tests/observability
136 passed in 4.64s

# proof 1 — paa/skills/registry reverted to a lazy import (in a copy)
$ PYTHONPATH=/tmp/opencode/w2fix01/proof1/src .venv/bin/python -m pytest \
      tests/conventions/test_import_conventions.py -rA
5 failed, 12 passed in 12.72s

# proof 2 — paa/observability/runs reverted to a lazy import (in a copy)
$ PYTHONPATH=/tmp/opencode/w2fix01/proof2/src .venv/bin/python -m pytest \
      tests/conventions/test_import_conventions.py -rA
5 failed, 12 passed in 13.08s

# proof 3a — a stdlib-only TYPE_CHECKING block re-added to a rowless module
$ PYTHONPATH=/tmp/opencode/w2fix01/proof3a/src .venv/bin/python -m pytest \
      tests/conventions/test_import_conventions.py -rA
1 failed, 16 passed in 13.78s

# proof 3b — a contract TYPE_CHECKING block re-added to a rowless module
$ PYTHONPATH=/tmp/opencode/w2fix01/proof3b/src .venv/bin/python -m pytest \
      tests/conventions/test_import_conventions.py -rA
3 failed, 14 passed in 13.39s

# proof 4 — the tree as it stands
$ .venv/bin/python -m pytest tests/conventions/test_import_conventions.py -rA
17 passed in 13.17s

$ git status --porcelain -- src tests
 M tests/conventions/test_import_conventions.py      # only the lock; no experiment residue
```

## 12. Deviations from the plan/brief; ownership issues; deferred dependencies

* **Deviation — the independent review subagent could not be run.** AGENTS.md
  §3 and §4 require a reviewer subagent after every phase and again at the
  end. **Both calls failed** with
  `Subagent depth limit reached (1). Increase "subagent_depth" to allow nested subagents.`
  (tried with `code-reviewer` and with `general`). I am a subagent myself and
  this session does not permit nesting. Per AGENTS.md's own non-negotiable
  rule — *"If subagents or required verification are unavailable, stop and
  report the blocker instead of pretending the workflow was completed"* — I
  am **reporting the gap rather than claiming a review that did not happen**,
  and I substituted the strongest verification available to me: four
  mutations of a copy of the tree, each of which must turn the lock red, plus
  a claim-by-claim audit of every sentence I wrote against the tree (§15).
  **The orchestrator should route this diff through a reviewer.**
* **Deviation — one extra proof.** The brief asked for three red proofs; I ran
  four (proof 3 in two variants), because "a `TYPE_CHECKING:` block re-added
  to a module with no classification row" can be read two ways, and the
  stdlib-only variant isolates the classification half (1 failure) from the
  contract variant (3 failures). The isolated variant is the stronger
  evidence: it shows the classification half has teeth *by itself*.
* **Ownership:** no violation. One file, one purpose; `git commit -m … --
  tests/conventions/test_import_conventions.py`; branch verified
  `ops/20261022-w6-w7-w9` before committing. No `src/` change, no other test
  changed.
* **Deferred dependencies:** none. Nothing outside my write-set needed to
  change for this task to be completable.
* **This is a superset of W2-IMPL-05's `NEED` (COMMS.md, 03:39:13Z), on
  purpose.** W2-IMPL-05 asked for four deletions and proposed leaving
  `DEFERRED: dict[str, str] = {}` — an **empty map** — with the deferral
  check still in place (their report §12, rows a–d). My brief asks for the
  fuller removal, and the file's own logic agrees: with the map empty,
  `test_every_deferral_names_the_task_that_owns_the_file` iterates nothing
  and asserts nothing, so it cannot fail — the vacuity the file names in its
  own words. Both variants are green (W2-IMPL-05 measured 18 passing with
  theirs; mine is 17, the difference being that one vacuous test). W2-IMPL-05
  also reached the same conclusion about class `F` ("class **F** now has no
  member, and the lock asserts that a class with no member is a failure") and
  about the `FAMILY` tuple ("the *deferral map* is what expires, not the
  family") — which is what I did. **The divergence is the empty map and the
  check that iterates it, and it is the orchestrator's brief that decides
  it, not the earlier agent's `NEED`.** If the orchestrator prefers
  W2-IMPL-05's smaller variant, the difference is one commit: re-add the
  map and the check, and put the `in DEFERRED` skips back — at the cost of a
  check that can no longer fail.
* **Process miss — COMMS.md was read at the end, not at the start.**
  Standing order 20 and the COMMS header both say to read COMMS.md at the
  start and before committing. I read it after the code was committed. It
  changed nothing (my file was unclaimed by anyone else; the ownership
  table and the `NEED` line both agree with what I did), but it is a
  miss and it is recorded rather than glossed. The `NEED` line at 03:39:13Z
  is what the work was *for*, and I found it four hours later than I should
  have.
* **One process note on the proofs:** during the proof runs I mislabelled a
  scratch directory (`proof0` for the `observability.runs` revert, then a
  loop that looked for `proof2`) and briefly produced a **green** run for
  "proof 2". The green run was the *repository's* tree, not the mutated copy
  — the `PYTHONPATH` pointed at a directory that did not exist. I caught it
  because a revert of the eighth module cannot be green, rebuilt the
  directory correctly, and re-ran everything. The transcripts in §7 are all
  from the re-run. This is recorded because the *first* proof run was wrong,
  and a reviewer re-running it should not be handed a stale artifact.

## 13. Out-of-scope findings

Two, both NITs, neither a defect in the tree, neither fixed by me (outside my
write-set):

1. **`ruff format --check` disagrees with this file — pre-existing.**
   `[RAN .venv/bin/python -m ruff format --check tests/conventions/test_import_conventions.py -> "1 file would be reformatted", four hunks]`.
   The four hunks are all pre-existing (`_aggregate_import`'s f-string
   concatenation, `test_the_relational_package_imports_without_the_postgres_extra`'s
   assert message, and the two `target.write_text(...)` calls in the cycle and
   extra proofs). The repo's gate is `ruff check`, not `ruff format --check`
   (AGENTS.md, *Setup and verification*), so this is not a gate failure — but
   a `ruff format` sweep would touch this file and I did not want my change
   to be the one that triggers it. **Verified unchanged by my edit**: the
   same four hunks, at the same relative places, appear when the file at
   `HEAD~1` is put through `ruff format --check` (10 removed diff lines
   before, 10 after). One of my own lines initially made a *fifth* hunk — the
   family half's comprehension, which `ruff format` wanted on one line once
   the `DEFERRED` condition was gone — and commit `80ba09b` collapses it.
   **No action needed** unless the repo adopts `ruff format`.
2. **The wave-2 context brief states a now-stale count.**
   `docs/agent-ops/20261022-w6-w7-w9/briefs/W2-CONTEXT.md:38` says *"Seven
   `TYPE_CHECKING` imports became runtime imports"* — it is eight now.
   That file is run-scoped and owned by wave 3's docs work (T-010) or the
   orchestrator; I did not touch it. It is a brief, not a durable fact about
   the tree, so the drift is cosmetic.

I looked for a *third* class of finding — another place in the repository that
asserts the deferral is outstanding — and there is none:
`grep -rn "TYPE_CHECKING\|deferral\|RelationalBackend" AGENTS.md README.md
docs/ROADMAP.md` → no output. The lock file was the only place the fact was
written down, which is the whole point of the lock.

## 14. Known limitations, risks, rollback notes

* **Risk — the escape hatch is gone.** After this change, a module that
  *genuinely* cannot import the contract at runtime (a real cycle) will fail
  the source half, and there is no map to record it in. The comment at the
  old map's site says what such a module must do: reintroduce the machinery
  **with a check of its own**, not inherit a hole. This is deliberate — the
  file's own design treats a self-invalidating map as the *correct* shape,
  and the cost of that shape is that its removal is loud. The source half's
  failure message names class `A`, so the reader is pointed at the one class
  that covers the case, and at a check that *proves* the cycle rather than
  asserting it.
* **Risk — the lock now covers one member more.** The annotation half reads
  all eight family members' hints at runtime. If `paa.observability.runs`
  ever stops binding the contract, that half fails with
  `NameError: name 'RelationalBackend' is not defined` (proof 1's
  transcript). That is the intended behaviour, and it is a *stronger* lock
  than the one wave 1 left behind.
* **Limitation — `mypy` does not check this file.** The repo's mypy gate is
  `mypy src`; the test tree is covered by `ruff` and by pytest. The LSP
  diagnostics I saw while editing (`Import "pytest" could not be resolved`)
  are the LSP not using the venv, not a real error — `[RAN ruff check src
  tests -> All checks passed!]` and the 17 passing tests are the evidence.
* **Limitation — the lock is not `ruff format` clean** (finding 1 above).
  Pre-existing; unchanged by me; my own added lines are ruff-clean.
* **Rollback:** `git revert 8c189d1 80ba09b` restores the file exactly. The
  revert would make the lock red again in exactly the two ways it was red
  before (the deferral entry and the stale classification row), so a rollback
  is visible immediately and is not silent.
* **No hidden debt:** the only thing a maintainer needs to know is that the
  deferral facility is *gone on purpose*, and why.

## 15. Where I am least confident / what the reviewer should scrutinise

* **Least confident:** whether the 18-line comment block at the old
  `DEFERRED` site is the right amount of explanation, and whether it should
  live in the module docstring instead. My reasoning: a reader who goes
  looking for the escape hatch will look *there*, and a `#:`-marked comment
  would be misread as documenting the next attribute (so I used plain `#`).
  A reviewer who prefers the docstring may move it; the content is the same.
* **Second: the phrase "the nine modules that commit touched".** It refers to
  commit `1c9ffa3`, which touched exactly nine contract-naming modules —
  verified against `git show --stat --name-only 1c9ffa3 -- src/paa`. It is
  *not* the number of modules that name the contract today (22). A reader
  could misread it; the sentence names the commit, which is the only
  unambiguous referent.
* **Third: the wording of the source half's failure message.** It now ends
  with "a module that genuinely cannot is a real import cycle (class A),
  which the lock proves rather than asserts." That is a *claim about the
  future* — no such module exists today. It is grounded
  (`base.py` imports only the standard library and two first-party modules,
  so no optional extra can be the reason), but a reviewer may want it
  softened.
* **What the reviewer should scrutinise, in priority order:**
  1. That removing the map does not lose any *capability*. Read
     `test_the_contract_is_not_imported_behind_type_checking` before and
     after: the only difference is the removal of an exception. Everything
     else in the file gained coverage (eight family members instead of
     seven).
  2. That `TYPE_CHECKING_CLASS_KEYS` losing `"F"` is required, not merely
     tidy — the `empty` assertion in the same file refuses a class with no
     member. Read that assertion; it is the file arguing with itself, and
     the file wins.
  3. The truthfulness of every sentence in the diff. The claims I re-checked
     against the tree, one by one:
     * "seven by wave 1 (`T-004`) and the eighth by wave 2 (`T-008`)" —
       `docs/plans/w6-w7-w9.md` §5 write-sets. ✓
     * "the nine modules that commit touched" — `git show --stat 1c9ffa3`. ✓
     * "the lock's own annotation half needs it now, and reads every family
       member's hints at runtime" — `test_the_family_can_have_its_annotations_read`
       calls `typing.get_type_hints` on every family member's class that
       names the contract. ✓
     * "The lock has six halves" — the bullet list has six. ✓
     * "Four of the five are mechanical" — A, B, D, E mechanical; C the
       judgement; the class list has five. ✓
     * "``paa.models`` is classed ``E``", "the ten ``C`` members" — counted
       from the table: 10 `C` rows. ✓ (unchanged from wave 1's text)
     * "a module that genuinely cannot is a real import cycle (class A)" —
       `src/paa/storage/relational/base.py` imports only `collections.abc`,
       `contextlib`, `typing` and two first-party modules. ✓
     * `set(NOTES) == set(CLASSES)` — verified by the passing check. ✓
     * "All eight entries stay in `FAMILY` and are checked" — the diff. ✓
  4. The four proofs. They are cheap to re-run; the harness is at
     `/tmp/opencode/w2fix01/mutate.py` and the transcripts are at
     `/tmp/opencode/w2fix01/transcripts/`.

## 16. Self-review checklist

- [x] Root cause, not symptom — the fulfilled handover is removed from every
      table and every comment, not just from the failing assertion.
- [x] No band-aid patterns — no `noqa`, no `type: ignore`, no skipped test,
      no weakened assertion, no deleted test that hid a defect. The one
      deleted test's subject (a deferral map) no longer exists.
- [x] Tests fail without the fix — four mutations of a copy, each red.
- [x] Only write-set files changed — `git show --stat 8c189d1` → one file.
- [x] No secrets/PII.
- [x] Conventions from AGENTS.md followed — house-style docstrings carrying
      the *why*, no stale path/line references, `#:`-style attribute comments
      kept where they belong (and plain `#` where the block documents an
      absence), no new dependencies.
- [x] Commits contain only my files; trailers present (`Task:`, `Agent:`,
      `Wave:`, `Report:`).
- [ ] **Independent review subagent — NOT RUN.** Unavailable in this session
      (subagent depth limit). Reported in §12, not papered over.

## 17. Commits

code: `8c189d1` — *test(conventions): the lock, after the last deferral*
(the substantive change), and `80ba09b` — *style(conventions): the family
half's comprehension fits on one line* (the `ruff format` tidy-up that keeps
the file's pre-existing format deviations at exactly four hunks, verified
against `HEAD~1`).
report: this commit (docs-only, on top of `80ba09b`).
