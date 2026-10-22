<!-- ORCH-DIGEST:START -->
agent: W1-REV-02 | scope: wave 1 / T-003 (the merged compose stack) + T-004 (the import conversion) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 4 | MINOR 6 | NIT 2
claims_audit: 29 claims checked / 4 unsupported or false
band_aids_found: none
top_3_risks: (1) two YAML shapes the refusal set claims to cover — the `<<:` merge key and the flow form `include: [..]` — are silently mis-read, so the script reports success while planning a stack Compose will not run; (2) a profile-gated service is dropped from the plan *before* the pin rule runs, so an unpinned image passes the gate; (3) the plan's §0/§3.2 TYPE_CHECKING evidence is wrong on two numbers and the correction lives only in a run-scoped report
optimizations: DO-NOW 5 | DO-LATER 3 | NOT-WORTH-IT 2
reversal_check: done
confidence: high (every finding is reproduced against the tree at 3ac9d6b and against `docker compose config` v5.6.0; the two T-004 measurements were re-derived independently)
<!-- ORCH-DIGEST:END -->

# Code review — W1-REV-02

**Lens:** correctness and design. **Subjects:** T-003 (`deploy/fetch-images.sh` reads a merged
compose stack) and T-004 (seven `TYPE_CHECKING` imports of `RelationalBackend` became runtime
imports, with a lock in `tests/conventions/`).

---

## 0. Method

**Diff range reviewed.** `git diff 02f3636..3ac9d6b -- . ':!docs/agent-ops'`, narrowed to
`954705d` (T-003 code), `db99c66` (ADR-0030 amendment) and `7747fa0` (T-004 code).

**Files read in context.** `deploy/fetch-images.sh` (all 899 lines), `docker-compose.yml`,
`docker-compose.gvisor.yml`, `tests/deployment/test_fetch_images.py` (the merged reader,
`_services_of` / `_merged_rows` / `_REFUSED_SHAPES` / `_PARITY_SHAPES`),
`tests/conventions/test_import_conventions.py` (all 494 lines), the seven converted modules,
`src/paa/storage/relational/__init__.py`, `src/paa/storage/relational/postgres.py`,
`src/paa/storage/relational/base.py`, `src/paa/storage/coldlake/{base,cas,__init__}.py`,
`docs/adr/0030-deployment-image-contract.md` (the amendment), `docs/plans/w6-w7-w9.md` (§0,
§2.2, §3.2, §6, §7), `AGENTS.md`, the shared context, the standing orders and the report
template.

**Order followed.** Blind pass first: I read the diff, the script, the test file and the
plan, and formed the findings in §1 **before** opening either implementer report. The
claims audit (§2) is against `reports/w1-impl/W1-IMPL-02-T-003.md` and
`reports/w1-impl/W1-IMPL-03-T-004.md`, read afterwards. §5 (attempts to break it) was run
against the tree as it stands, in scratch directories under `/tmp`, with copies of the
repository extracted by `git archive` (no git state in the shared tree was touched, no
worktree, no stash, no checkout).

**Commands run (all scoped).**

```
[RAN] .venv/bin/python -m pytest tests/conventions -q                          -> 7 passed
[RAN] .venv/bin/python -m pytest tests/deployment/test_fetch_images.py -q      -> 55 passed
[RAN] .venv/bin/python -m pytest tests/deployment/test_fetch_images.py -q      (in the 02f3636 tree,
        with the NEW test file)                                                 -> 18 failed, 37 passed
[RAN] .venv/bin/python -m ruff check src tests                                 -> All checks passed!
[RAN] bash -n deploy/fetch-images.sh                                            -> exit 0
[RAN] mypy src            (02f3636 / 602d18c / 3ac9d6b trees)                  -> 0 errors (136 / 137 / 137 files)
[RAN] mypy src tests      (02f3636 / 602d18c / 3ac9d6b trees)                  -> 190 / 184 / 184 errors
[RAN] docker compose version                                                   -> Docker Compose version v5.6.0
[RAN] docker compose -f <fixture> config                                        -> many times, read-only
[RAN] a 34-block AST sweep: un-guard each `if TYPE_CHECKING:` block in a shadow copy of
        src/paa, then import the module in a fresh interpreter                  -> 34/34 tried, 1 breaks
[RAN] the same sweep with ALL 34 blocks un-guarded at once, 137 modules probed  -> 1 cycle
        (coldlake/base↔cas) + 1 missing extra (kuzu)
[RAN] PYTHONPATH=<copy> pytest tests/conventions -q, after reverting one of the seven
        modules by hand                                                        -> exactly 2 FAILED
[RAN] typing.get_type_hints(get_database)                                      -> NameError: name 'Settings' is not defined
[RAN] typing.get_type_hints(<7 constructors>) on 02f3636 and on 3ac9d6b        -> all 7 raise before, all 7 resolve after
[RAN] import paa.storage.relational.postgres with psycopg/psycopg_pool blocked -> imports cleanly
```

---

## 1. Blind-pass findings (before reading implementer reports)

These are the findings I formed from the code alone, in the order I found them. The
severities were re-checked after the claims audit (§2), which is where the numbering and
the cross-references live.

### B-1 — The refusal set has two holes, and both produce "success" with the wrong plan

The script's contract — stated in its own header (`deploy/fetch-images.sh:60-65`), in the
ADR amendment (`docs/adr/0030-deployment-image-contract.md`, "What is refused, and why a
refusal is the right answer") and in the implementer's COMMS line — is that **five shapes
outside the merge subset are refused, each with its own message, before a byte is
fetched**: `extends:`, a YAML anchor or an alias, a multi-line `image:`, a custom YAML
tag, and the top-level `include:`.

Two of those five are not actually refused.

**(a) The YAML merge key `<<:`.** The refusal for an alias is attached to *two places*:
the service key (`deploy/fetch-images.sh:274-280`, `inline ~ /^[&*]/`) and the `image:`
value (`:298-307`, `/^\*/` and `/&/`). YAML's canonical use of an alias is neither: it is
the **merge key**, `<<: *base`, which imports a whole map into a service.

```yaml
x-base: &base
  image: postgres:16@sha256:0000…
services:
  pg2:
    <<: *base          # <- 4-space indent; matches no rule
  other:
    image: redis:7@sha256:0000…
```

`<<:` is at four-space indent, so it does not match the two-space service rule, and it is
not `image:`/`extends:`/`build:`/`profiles:`, so no rule in the reader sees it at all. The
reader never learns that `pg2` has an image, `pg2` therefore has neither `image:` nor
`build:` and is dropped from the plan — **silently**, with exit status 0.

```
[RAN] PAA_COMPOSE_FILE=…/merge_key.yml bash deploy/fetch-images.sh --list
      -> pull  redis:7@sha256:0000…  (other)
      -> exit=0        (pg2 is absent; its image is not in the plan)
[RAN] docker compose -f merge_key.yml config
      -> services: {other: {image: redis:…}, pg2: {image: postgres:16@sha256:0000…}}
```

So the deployment Compose runs pulls `postgres:16@sha256:0000…` and the script never
fetched it, and the script said everything was fine. This is exactly the failure mode the
brief names: *a script that reports success while having fetched the wrong thing*. It is
also precisely what the ADR says the refusal exists to prevent — the ADR's own words for
the alias case are *"an unresolved reference would be pulled as the literal text of the
alias"*, and here the unresolved reference is not pulled as literal text, it is **dropped**,
which is the same class of wrongness in the other direction.

**(b) The flow form of the top-level `include:`.** The refusal is

```awk
if ($0 ~ /^include:[[:space:]]*($|#)/) { refuse(...) }
```

(`deploy/fetch-images.sh:242`) — it matches a **bare** `include:` on its own line (the
block form) or `include:` followed by a comment, and nothing else. The flow-sequence form
`include: [other.yml]` — a *list*, which is the form Compose's schema wants, and which
Compose accepts — does not match, and is ignored. The ADR's stated reason for the refusal
is *"the script does not follow it, and the services it names would be missing from the
plan"*. For the flow form, that is exactly what happens.

```
[RAN] PAA_COMPOSE_FILE=…/flow_include.yml bash deploy/fetch-images.sh --list
      -> pull  postgres:16@sha256:0000…  (postgres)
      -> exit=0        (the included service is absent)
[RAN] docker compose -f flow_include.yml config
      -> services: {included: {image: alpine:3@sha256:0000…}, postgres: {image: postgres:…}}
```

(The *inline-scalar* form `include: other.yml` is not a hazard: Compose rejects it itself
— `[RAN] docker compose -f inline_include.yml config -> "include must be a list, got
other.yml"` — so no deployment can be running that file.)

**Why the two holes have the same root cause.** The refusal detects the *spelling of a
line* rather than the *presence of a construct*. The merge key is an alias that appears in
a third place; the flow form is a key that appears on a line with content. A reader that
refused "any alias token (`*name`) anywhere under `services:`, and any `<<:` key" and
"any top-level `include:` **key**, whatever follows it" would close both.

**Why the parity lock did not catch either.** The test's independent Python reader
(`tests/deployment/test_fetch_images.py::_services_of`) reproduces the same wrong answers
— `_services_of(merge_key.yml)` returns `pg2` with `image=None`, and
`_services_of(flow_include.yml)` returns only `postgres` — so "the two readers agree" is
satisfied by two readers that are wrong together. That is G2 (the lock compares two
hand-rolled readers, not Compose), which the ADR records as *not fixed*; but it means the
refusal set, which the ADR presents as the safety net, has two holes that no test in the
suite can see. The refusal corpus tests **one spelling per refused shape**, and each of
these shapes has more than one spelling.

### B-2 — A profile-gated service is removed from the plan *before* the plan's invariants run

The reader drops a profile-gated service in its `END` block
(`deploy/fetch-images.sh:338-341`, `printf "skip\tprofile\t%s\n"`), and `build_plan`
filters the `skip` row out of `PLAN` (`:427-437`). The pin rule then runs **over `PLAN`
only** (`:447-468`, `[ "$action" = "pull" ] || continue`). So a profile-gated service's
image is never checked for a digest, at all.

```
[RAN] PAA_COMPOSE_FILE=…/profile_unpinned.yml bash deploy/fetch-images.sh --list
      -> warn  gpu is gated behind a compose profile, so it is not fetched: …
      -> pull  postgres:16@sha256:0000…  (postgres)
      -> exit=0        (nvidia/cuda:latest is in the file and is NOT refused)
```

ADR-0030:90 is unambiguous — *"A pulled image must be digest-pinned, and a tag-only
reference is an error."* The script's own header says the same. A stack whose
profile-gated service names `nvidia/cuda:latest` passes the gate. The same run also shows
the second half of the problem: the final message (`deploy/fetch-images.sh:895`) says
**"every image the compose file(s) name is present"**, and `--list` prints no row for the
skipped service — so both the report and the gate are *silent about a service that was
not fetched*.

The ordering is the root cause. The exclusion is a property of the **row**, not of the
**plan**: if the row carried its profile state (say `pull<TAB>image<TAB>service<TAB>profile`)
then the pin rule, `--list` and the final message could all still see the service, and the
question "fetch it or not?" would be a decision about the *fetch*, made after the
*invariants* have been checked.

### B-3 — `profiles: []` over-excludes

The rule is "the **key's presence** gates the service" (`deploy/fetch-images.sh:328-331`).
Compose's rule is "a non-empty profile list gates the service":

```
[RAN] docker compose -f profiles_empty.yml config --services   -> gpu, postgres
[RAN] PAA_COMPOSE_FILE=…/profile_empty.yml bash deploy/fetch-images.sh --list
      -> warn  gpu is gated behind a compose profile … ; only postgres is planned
```

So a service Compose *starts* is one the script does not fetch. It is announced, which is
better than silence, but it is still a divergence in the same direction as B-2.

### B-4 — The `profiles:` merge rule is recorded wrongly

The implementer's record says Compose *"replaces the key when a later file declares it"*
and lists *"how Compose merges a `profiles:` sequence … is a detail of Compose's
specification that this implementation does not reproduce exactly"* as **Unknown**. It is
knowable, and it is the other thing:

```
[RAN] base2.yml (profiles: [p]) + over_prof.yml (profiles: [q])
      docker compose --profile p config --services -> x
      docker compose --profile q config --services -> x
```

Compose **concatenates** the sequences. The script's outcome (gated, if any file gates the
service) is the same either way, so there is no behavioural defect — but the *stated
reason* in the record is wrong, and a wrong reason in a record is what a future reader
will build on.

### B-5 — The `storage/relational/__init__.py` defect is real, and it is cheap

```python
# src/paa/storage/relational/__init__.py:42-51
if TYPE_CHECKING:
    from paa.config import Settings
    from paa.storage.relational.postgres import PostgresDatabase

# :65
def get_database(settings: Settings | StorageSettings) -> Database | PostgresDatabase:
```

```
[RAN] typing.get_type_hints(get_database) -> NameError: name 'Settings' is not defined
```

The defect is real. Three things sharpen it:

* it is **the same defect class T-004 exists to remove** — an annotation naming a name
  that is not bound — in the package that **owns the contract**;
* the module's docstring gives a rationale that does not hold (`:21-24`): *"PostgresDatabase
  is imported lazily inside the factory so a laptop install without the postgres extra
  (psycopg) stays importable"*. `postgres.py` imports psycopg **lazily**
  (`src/paa/storage/relational/postgres.py:119-124`, `_require_psycopg()`), so the module
  imports perfectly well without psycopg — `[RAN] import paa.storage.relational.postgres
  with psycopg/psycopg_pool blocked -> imports cleanly`. The extra was never the reason;
  the docstring's reason is a fiction;
* the fix is therefore **two runtime imports** and nothing else: `Settings` (from
  `paa.config`, which line 33 already imports at runtime) and `PostgresDatabase` (from
  `paa.storage.relational.postgres`, which needs no extra). After that,
  `typing.get_type_hints(get_database)` resolves.

**Severity judgement.** The runtime impact *today* is **nil** — nothing in the tree
resolves that function's annotations: `get_database` is not a FastAPI dependency
(`grep` finds one call site, `src/paa/runtime.py:286`), it is not a pydantic model field,
and nothing calls `get_type_hints` on it. So I do **not** call it a blocker. I call it
MAJOR because (a) it is the exact defect the task was chartered to eliminate, in the
contract's own package, (b) it is a latent trap for the next person who puts
`get_database` behind `Depends()` — FastAPI resolves a dependency's signature with
`get_type_hints`, so the failure would land at route-registration time, (c) the rationale
written next to it is false, and the house rule is explicit that a wrong reason is how a
future reader mis-decides, and (d) it currently has **no owner**: T-004 was read-only for
`storage/relational/**`, and the plan's write-set table has no task for it.

### B-6 — T-004's fresh-interpreter number is one too high, in two places

`tests/conventions/test_import_conventions.py::_module_files` (`:167-180`) skips
`paa/__init__.py` (`if not parts: continue`), so the sweep covers **136** modules, not 137
— and in this environment **135** of them import; `paa.storage.graph.kuzu_backend` fails
for the missing `graph` extra, which the test tolerates. The implementer's report says
*"137/137 modules import in a fresh interpreter"* in the acceptance-criteria table (§1,
criterion 3) and *"**137/137** modules import in their own subprocess"* in the tests table
(§7), while its own command log (§11) says *"modules checked: 137 / failures: 1"*. Three
numbers, one measurement. The aggregate import is over the **135** importable modules, not
136.

### B-7 — The classification of the 33 remaining blocks is not in the repository

The plan (§3.2) asks for the other `TYPE_CHECKING` blocks to be *"documented and deferred,
with the cycle proof"*. The lock's `DEFERRED` map names **one** module
(`tests/conventions/test_import_conventions.py:84-89`). The rest of the classification —
which block is the one real cycle, which are bound to an optional extra, which are inert —
exists only in the implementer's report, in a **run-scoped** directory
(`docs/agent-ops/20261022-w6-w7-w9/`). Nothing in the tree records it, and the plan's own
§0/§3.2 numbers are wrong (§2 below), so the wrong number is the one a future reader will
find first.

### B-8 — A stale `DEFERRED` entry is never caught

`test_every_deferral_names_the_task_that_owns_the_file` (`:404-412`) checks that the
reason is non-empty, names a `T-nnn`, and names a module that exists. It does **not**
check that the module is *still* a lazy resolver. When T-008 converts
`paa/observability/runs.py`, the entry becomes a statement about a file that is no longer
lazy, and the lock passes. The implementer names this in §14; the fix is one assertion —
*every deferred module must still resolve the contract lazily* — which turns the deletion
of the entry into a hard requirement of the change that makes it unnecessary.

### B-9 — `OPTIONAL_EXTRA_MODULES` carries a wrong reason

`tests/conventions/test_import_conventions.py:95-102` lists
`paa.storage.relational.postgres` with the comment *"psycopg — paa[postgres]"*. That module
imports cleanly without psycopg (`[RAN]`, psycopg blocked) — `postgres.py` imports it
lazily. The entry is harmless (a tolerated failure that does not happen), but the comment
is the same false rationale as B-5, in the same wave.

---

## 2. Claims audit

`✔` = verified, `✘` = unsupported or false, `~` = verified with a correction.

### T-003 — `reports/w1-impl/W1-IMPL-02-T-003.md`

| # | Implementer claim | Verified? | Evidence / finding |
|---|---|---|---|
| 1 | "`--compose <path>` is repeatable and merges in file order; later wins; the flag beats `PAA_COMPOSE_FILE`" | ✔ | `deploy/fetch-images.sh:806-809, 825-839`; `[RAN] --list --compose A --compose B` merges; `[RAN] --compose X` with `PAA_COMPOSE_FILE=Y` → X is read. Reproduced by real Compose: `[RAN] docker compose -f base.yml -f over_build.yml config` → the merged service keeps the base's `build:` **and** takes the override's `image:` — the script's "build wins for the action, later image wins for the value" matches Compose exactly. |
| 2 | "The expected service set in the parity test is now **derived**, not hard-coded" | ✔ | `tests/deployment/test_fetch_images.py:1105-1131` derives it from `_services_of(COMPOSE)`. **Not circular w.r.t. the script**: the derivation uses the test's own Python reader, so a disagreement between the script and the compose file still fails (`assert {service for _,_,service in rows} == expected`). It *is* circular w.r.t. the test's reader — both readers share the blind spots of §1/B-1, which is G2, recorded. |
| 3 | "`profiles:`-gated services are excluded, with a stderr notice" | ~ | The exclusion and the notice are there (`deploy/fetch-images.sh:338-341, 427-437`). But the exclusion happens **before** the pin rule, so the notice is the only signal and the pin contract is skipped (R-W1-103), and `profiles: []` over-excludes (R-W1-105). |
| 4 | "`extends:` / anchors / aliases / a multi-line `image:` / a custom tag / a top-level `include:` are **refused** rather than mis-read" | ✘ | **Two of the six are not refused.** The `<<:` merge key (an alias in a third place) and the flow form `include: [other.yml]` are silently mis-read, with exit 0. See R-W1-101 and R-W1-102; the ADR repeats the same false claim. |
| 5 | "The merge subset was cross-checked against real Compose v5.6.0" | ✔ | `[RAN] docker compose version -> v5.6.0`; I re-ran the same five checks and got the same answers. The cross-check is real and was well chosen — it is also how I found the `profiles: []` and `profiles:`-merge facts. |
| 6 | "18 of the new tests fail against the pre-change script" | ✔ | `[RAN]` new test file against the `02f3636` script → **18 failed, 37 passed**. Exactly 18. |
| 7 | "G2 and G6 are **recorded, not fixed**" | ✔ | `docs/adr/0030-deployment-image-contract.md`, "What was deliberately NOT done" — both are stated as such. ✔ |
| 8 | "Compose replaces the key when a later file declares it" (about `profiles:`) | ✘ | Compose **concatenates** the sequences: `[RAN] profiles [p] + profiles [q]` → the service is enabled by `--profile p` **and** by `--profile q`. The script's outcome is unchanged, the stated reason is not. R-W1-106. |
| 9 | "`profiles: []` … Compose's own treatment is the other way round (`docker compose config` → a service with `profiles: []` IS in the model)" | ✔ | `[RAN] docker compose -f profiles_empty.yml config --services -> gpu, postgres`. The implementer found and documented this; I only add that the ADR states the rule without the exception (R-W1-105). |
| 10 | "No shape the repository's compose files use is affected — the two files parse to the same plan as before" | ✔ | `[RAN] --list` over `docker-compose.yml` and over the merged pair → the same six rows. |
| 11 | "the reader now strips trailing whitespace from an `image:` value … a pull reference with a space on the end of it" | ✔ | `[RAN]` a compose file with trailing spaces → the pull reference is `postgres:16@sha256:0000…` with no trailing space. The test at `:1053-1077` proves it at the fetch, not only at the report — good test design. |
| 12 | "the `profiles:` decision: **EXCLUDED**, not included … the reader's job is to fetch what the deployment runs" | ✘ (as a design answer) | The *exclusion* is defensible for the default profile set; the *implementation* is not, because it drops the service before the invariants run and then reports "every image the compose file(s) name is present". I answer the brief's question in §4: the plan should carry the service, the fetch should decide, and `COMPOSE_PROFILES` should be honoured. R-W1-103/104. |
| 13 | "G8 — `.env.example` and `docs/deployment/docker.md` document all five" | ✔ | `git show 954705d -- .env.example docs/deployment/docker.md` — both files gained the five knobs. |
| 14 | "the refusal path runs before the daemon gate, so a file the reader does not understand is refused on a host with no Docker at all" | ✔ | `[RAN] PAA_COMPOSE_FILE=…/ext.yml bash deploy/fetch-images.sh --list` → `fail …: the service paa uses extends:.` + exit 1, with no Docker on PATH. Loud, names the file, the service and the construct. ✔ |
| 15 | "The Compose checks ran in `/tmp/opencode/compose-check`, on copies … No container was created" | ✔ (consistent) | `docker compose config` renders a model; I ran it repeatedly with no container involved and no daemon state change. |

### T-004 — `reports/w1-impl/W1-IMPL-03-T-004.md`

| # | Implementer claim | Verified? | Evidence / finding |
|---|---|---|---|
| 1 | "Seven `TYPE_CHECKING` imports of `RelationalBackend` became runtime imports; `observability/runs.py` was left alone" | ✔ | `git show 7747fa0 -- src/paa/` — seven files, each `if TYPE_CHECKING:` block removed and the import hoisted; `paa/observability/runs.py` untouched and recorded in `DEFERRED` (`tests/conventions/test_import_conventions.py:84-89`). |
| 2 | "Before the change `get_type_hints` on all seven constructors raised `NameError`; after, they resolve." | ✔ | `[RAN]` on the `02f3636` tree: all seven raise `NameError: name 'RelationalBackend' is not defined`; `[RAN]` on `3ac9d6b`: all seven resolve, e.g. `db: paa.storage.relational.base.RelationalBackend | None`. Exactly the seven the report names. |
| 3 | "Pydantic model fields are byte-identical" | ✔ (structurally) | The report's argument is the strong one: a model with a `RelationalBackend`-annotated field would have raised `PydanticUndefinedAnnotation` at class-creation time **before** the change, so no such model can exist. I re-derived it: only three `BaseModel` subclasses are *defined* in the family (`paa/agents/base.py`), none of them annotates the contract. |
| 4 | "137/137 modules import in a fresh interpreter; a 136-module aggregate import also succeeds" | ✘ | **136** modules are enumerated (`_module_files` skips `paa/__init__.py`) and **135** import here; `paa.storage.graph.kuzu_backend` fails for the missing `graph` extra, which the test tolerates. The aggregate import is over the 135 importable modules. The report's own §11 says "137 / failures: 1". R-W1-111. |
| 5 | "The lock is 7 tests" | ✔ | `[RAN] pytest tests/conventions -q -> 7 passed`; seven `def test_` functions in the module. |
| 6 | "Reverting the change fails two of them" | ✔ | `[RAN]` reverted `src/paa/memory/decay.py` to the `TYPE_CHECKING` form **in a copy of the tree** (with `PYTHONPATH` pointed at the copy, so the test really saw the reverted file — a first attempt without it passed 7/7 and proved nothing) → exactly two FAILED: `test_the_contract_is_not_imported_behind_type_checking` and `test_the_family_resolves_the_contract_one_way`. Both name the offending module. |
| 7 | "the brief's 'nine blocks are load-bearing for a real runtime cycle' is overstated — converting each of the 34 blocks one at a time, and eight groups of them together, broke an import only for `cas.py`" | ✔ | **Independently reproduced.** I un-guarded each of the 34 blocks in a shadow copy of `src/paa` and imported the module in a fresh interpreter: 34/34 tried, **exactly one** breaks — `paa.storage.coldlake.cas` (*"cannot import name 'BlobStore' from partially initialized module 'paa.storage.coldlake.base'"*). I also did the complementary measurement the report did not: un-guarding **all 34 at once** and probing all 137 modules → the same single cycle, plus the kuzu extra. **The correction is right.** |
| 8 | "41 blocks before, 34 after" | ✔ | `[RAN] grep -c "if TYPE_CHECKING:" 02f3636 -- src/paa` → **41**; at `3ac9d6b` → **34**. ✔ (The *plan's* "48" is wrong — see §8.) |
| 9 | "`mypy src tests` = 184 errors, the same as the pre-change baseline" | ✔ | `[RAN]` at `602d18c` (the tree immediately before T-004's commit) → **184 errors in 35 files**; at `3ac9d6b` → **184 errors in 35 files**. `mypy src` → 0 in 137 files on both. ✔ (The `02f3636` baseline is 190 — T-001/T-003 lowered it by 6 — but the claim is about T-004's own baseline, and it is right.) |
| 10 | "the fresh-interpreter check would catch a cycle" | ✔ | `test_the_probe_would_notice_a_cycle` builds a real two-module cycle in `tmp_path` and asserts the probe reports it as a cycle, not as a missing extra. The mechanism is sound and self-proving. ✔ |
| 11 | "`postgres.py` does **not** import `psycopg` at module scope, so the module `__init__`'s own docstring … is inaccurate as a reason" | ✔ | `[RAN]` psycopg blocked at `__import__` → `paa.storage.relational.postgres` and `paa.storage.relational` both import cleanly. Confirmed. This is what makes the fix for the `get_database` defect cheap. |
| 12 | "The import-weight delta is real and concentrated in `paa.marketplace.installer` (+95 ms)" | ✔ (plausible; not re-measured end-to-end) | The mechanism is right: `from paa.storage.relational.base import …` executes the package `__init__` → `paa.config` + `paa.storage.relational.database` (aiosqlite + the migration runner). `[ASSUMED]` the absolute numbers; the shape is confirmed by the import graph. |
| 13 | "The classification … is in the task report" | ~ | The plan asked for the classification to be **documented and deferred**. It is documented — in a run-scoped report that will be superseded by the next run, and in a tree whose plan text carries the *wrong* numbers. R-W1-112. |
| 14 | "the deferral map … the lock passes either way" | ✔ | The assertion is `offenders == []` where `offenders` = lazy resolvers **not** in `DEFERRED` (`:334-352`). Correctly described as a subset relation, and correctly noted to go stale. R-W1-113 proposes the assertion that would close it. |

**Count: 29 claims checked, 4 unsupported or false** (T-003 #4, #8, #12; T-004 #4). Three of
the four are *records* that assert something false (a shape that is not refused, a merge
rule Compose does not use, a figure that is not what the measurement says); the fourth is
a design answer I disagree with, with the evidence given.

---

## 3. Long-run test (independent answers)

**Source.** The compose file is operator input and, in a compromised environment,
supply-chain input. The reader treats it as text. It refuses five shapes and, on my
evidence, *should* refuse seven (the two holes of R-W1-101/102). The `awk` program is
handed the file paths as **arguments** (`"${COMPOSE_FILES[@]}"`, `deploy/fetch-images.sh:390`),
never interpolated into the program text, so a path cannot inject program text. The shell
side quotes every expansion (`"$2"`, `"${compose_flags[@]}"`). The plan is a tab-separated
row stream; the refusal messages travel back as rows so the shell owns the wording of an
error and the `awk` program never writes behind the script's back.

**Load.** The plan is a handful of rows; the merge is O(services × files) inside one
`awk` invocation. The shell's string accumulation in `build_plan` is O(n²) in the number
of services — six today, and irrelevant for a file a human wrote. `--compose` repeated
ten times is ten files read in one `awk` pass. No finding.

**Hostile input.** The reader's weakness is not injection; it is **interpretation**. The
hostile input is a compose file using a shape the reader does not implement, and the
failure is a *silently wrong plan* rather than a crash. I found two such shapes (§1/B-1).
That is the finding, and it is the reason this section is the reason the change is not
finished. The path handling is sound; the *content* handling is not exhaustive.

**Partial failure.** A missing file is refused by name before the plan is built. A
refused shape stops the script before any mode's work — `[RAN]` the `extends:` fixture
exits 1 with no `docker` on PATH and nothing fetched. A failed *pull* remains a failed
*attempt* with the digest check inside the retry unit (unchanged). The `skip` (profile)
path is the one partial failure that is **not** surfaced as a failure — see R-W1-103/104.

**Operability.** `--list` needs neither the Docker CLI nor a daemon, so the plan is
inspectable on a host that cannot run it; that property survived the change and is the
reason the merge was implemented in the reader rather than by shelling out to
`docker compose config`. The refusals name the file, the service and the construct. The
profile exclusion names the service — but only on stderr, and not in the plan, so the
operator reading `--list` cannot see what was left out (R-W1-104). Rollback: one commit,
no `src/`, no schema, no configuration contract; reverting restores the single-file
behaviour.

**Reversal — which test fails if the fix is reverted?**
T-003: `test_an_image_added_to_the_gvisor_override_changes_the_plan` (the merge reaches
the plan) and `test_the_merged_plan_is_what_the_fetch_pulls` (the merge reaches the
fetch). `[RAN]` the whole new test file against the pre-change script → **18 failed**,
including both. ✔
T-004: reverting one of the seven modules fails exactly two tests —
`test_the_contract_is_not_imported_behind_type_checking` and
`test_the_family_resolves_the_contract_one_way`. `[RAN]` measured, not reasoned. ✔

---

## 4. Findings

### R-W1-101 [MAJOR] The YAML merge key `<<:` is not refused and is silently mis-read — the script reports success while planning a stack Compose will not run

* **Location:** `deploy/fetch-images.sh:250-331` (the reader's rules) and
  `deploy/fetch-images.sh:318-323` (the `extends:` refusal, the closest analogue);
  contract text at `deploy/fetch-images.sh:60-65` and at
  `docs/adr/0030-deployment-image-contract.md` ("What is refused, and why a refusal is the
  right answer").
* **Evidence:** `[RAN]` a compose file with `x-base: &base` / `services.pg2.<<: *base` →
  the script's plan is `pull redis:7@sha256:0000… (other)` and **exit 0**; `pg2` is
  absent. `[RAN] docker compose -f merge_key.yml config` → `pg2: {image:
  postgres:16@sha256:0000…}`. `[VERIFIED deploy/fetch-images.sh:274-280, 298-307]` — the
  alias refusal is bound to the *service key* and to the *`image:` value*; `<<:` at
  four-space indent matches no rule at all. `[RAN]` the test's own reader
  (`tests/deployment/test_fetch_images.py::_services_of`) returns `pg2` with
  `image=None` — the two readers agree on the wrong answer, so the parity lock is blind
  to it.
* **Why it matters:** this is the failure mode the brief names as the one that matters
  most — *a script that reports success while having fetched the wrong thing*. The
  deployment runs `postgres:16@sha256:0000…`, the script never fetched it, and every
  output of the script said the deployment was covered. The script's header, the ADR and
  the COMMS line all state that an alias is refused; a reader (and a reviewer) relying on
  that statement would not look here.
* **Recommended fix (root cause):** refuse the **construct**, not the spelling. In the
  reader, refuse any four-space key whose value begins with `*` (an alias) and any
  four-space `<<:` key, with one message naming the file, the service and the merge key.
  Refusing *all* alias tokens under `services:` also closes the flow form of a merged
  map and any future spelling. Add both spellings to `_REFUSED_SHAPES` in
  `tests/deployment/test_fetch_images.py` (one spelling per shape is what let this
  through) and add the shape to `_PARITY_SHAPES` so the *second* reader is taught the
  refusal too — the parity corpus is the only place a refusal shape can be pinned on
  both readers.

### R-W1-102 [MAJOR] The top-level `include:` refusal catches only the block form; the flow form `include: [other.yml]` is silently ignored

* **Location:** `deploy/fetch-images.sh:242-246`.
* **Evidence:** `[VERIFIED deploy/fetch-images.sh:242]` —
  `if ($0 ~ /^include:[[:space:]]*($|#)/)` matches a bare `include:` or `include:` plus a
  comment, and nothing else. `[RAN] PAA_COMPOSE_FILE=…/flow_include.yml bash
  deploy/fetch-images.sh --list` → `pull postgres:16@sha256:0000… (postgres)`, **exit 0**,
  the included service absent. `[RAN] docker compose -f flow_include.yml config` →
  `included: {image: alpine:3@sha256:0000…}` is in the merged model. (The *scalar* form
  `include: other.yml` is refused by Compose itself — `[RAN] docker compose -f
  inline_include.yml config -> "include must be a list, got other.yml"` — so it is not a
  hazard.)
* **Why it matters:** the ADR gives the refusal's reason as *"the script does not follow
  it, and the services it names would be missing from the plan"* — and for the flow form
  that is precisely what happens, quietly, with a zero exit status. The compose
  specification's own form for `include:` is a list, and the flow list is the one-liner
  an operator is most likely to write.
* **Recommended fix (root cause):** refuse the **key**, not the line's shape: match
  `^include:` and refuse whatever follows it (the message can name the form it saw).
  Add the flow form to `_REFUSED_SHAPES`, and — because a refusal must also be a
  *loud* refusal — assert in the test that the script fetched nothing (the existing
  test already asserts `stub.pulls() == []`; keep that for the new shape).

### R-W1-103 [MAJOR] A profile-gated service is dropped from the plan before the pin rule runs, so an unpinned image passes the gate

* **Location:** `deploy/fetch-images.sh:338-341` (the reader's `END` block drops it),
  `:427-437` (`build_plan` filters the `skip` row out of `PLAN`), `:447-468` (the pin
  rule runs over `PLAN` only); contract text at `docs/adr/0030-deployment-image-contract.md:90`.
* **Evidence:** `[RAN] PAA_COMPOSE_FILE=…/profile_unpinned.yml bash
  deploy/fetch-images.sh --list` → a `warn` line, `pull postgres:16@sha256:0000…`, and
  **exit 0** — the file also contains `image: nvidia/cuda:latest` under the profile, and
  no refusal is raised. `[VERIFIED deploy/fetch-images.sh:449]` — `[ "$action" = "pull" ]
  || continue`, and the profile-gated service never reaches `PLAN` at all.
* **Why it matters:** ADR-0030:90 says *"A pulled image must be digest-pinned, and a
  tag-only reference is an error."* The pin rule is the deployment's core safety property
  and the script is its gate; a tag-only reference now passes the gate. It is announced
  on stderr, so the *existence* of the service is not hidden — but the *violation* is,
  because the only thing the operator sees is a warning about a profile.
* **Recommended fix (root cause):** make the profile a property of the **row**, not of
  the plan's membership. The reader should emit the service with its profile state
  (e.g. `pull<TAB>image<TAB>service<TAB>profile`), the plan should keep it, and the
  **invariants** (digest pin, interpolation, empty image) should be checked over it
  exactly as over any other row. Only the *fetch* should then decide, and it should
  decide loudly. Honour `COMPOSE_PROFILES` (Compose's own environment variable —
  `[RAN] COMPOSE_PROFILES=gpu docker compose -f profiles_real.yml config --services ->
  gpu, postgres`) so an operator who intends to run a profile gets that profile's
  images fetched; a service behind a profile that is not enabled should be reported as
  *not fetched* in the `--list` output and in the final message, not erased from the
  plan.

### R-W1-104 [MINOR] The success message and the `--list` output both claim the plan is the whole image set, which is false when a service was skipped

* **Location:** `deploy/fetch-images.sh:895` (*"every image the compose file(s) name is
  present"*) and `:657-663` (`list_images`, which prints only `PLAN` rows).
* **Evidence:** `[RAN]` the profile fixture → stdout carries only `postgres`; the skipped
  service appears nowhere in the report, only in a stderr warning. The `--list` mode is
  documented as *both* a report and a gate (`deploy/fetch-images.sh:132-135`).
* **Why it matters:** a CI gate built on `--list` sees a plan that omits a service, and a
  human reading the success line is told a completeness that does not hold. The
  information exists — it is on stderr — but the *report* is the artefact people read.
* **Recommended fix:** same root cause as R-W1-103 — carry the skipped service in the
  plan, mark it, and print it in `--list` as `skip profile <service>` so the report and
  the warning agree. The final message must then say what it means: "every image the
  compose files name **that this stack runs** is present" plus the count of what was left
  out.

### R-W1-105 [MINOR] `profiles: []` over-excludes: Compose starts the service, the script does not fetch it

* **Location:** `deploy/fetch-images.sh:328-331` (the rule is the key's *presence*);
  contract text at the ADR amendment ("The key's *presence* is what counts, not the
  contents of its list").
* **Evidence:** `[RAN] docker compose -f profiles_empty.yml config --services -> gpu,
  postgres`; `[RAN] PAA_COMPOSE_FILE=…/profile_empty.yml bash deploy/fetch-images.sh
  --list` → `gpu` is skipped and announced.
* **Why it matters:** a service Compose *starts* is one the script did not fetch, which
  is the failure the script exists to prevent. It is announced, and the shape is
  degenerate, so the severity is MINOR — but the ADR states the rule without the
  exception, so the divergence is written down as if it were the rule.
* **Recommended fix:** the rule should be "a **non-empty** profile list gates the
  service", which is Compose's rule. Reading the list means reading two shapes (block
  and flow) in two readers — the implementer rejected that as a bad trade, and for the
  *default* case it is. The cheaper fix that removes the divergence without parsing a
  list: **refuse** `profiles:` whose value is empty or absent (`profiles:` with no value,
  `profiles: []`) rather than guessing — a refusal is loud, and the shape is rare enough
  that a refusal costs nothing. Record the choice in the ADR.

### R-W1-106 [MINOR] The record says Compose *replaces* a `profiles:` key when a later file declares it; it concatenates the sequences

* **Location:** `reports/w1-impl/W1-IMPL-02-T-003.md` §9 ("Compose replaces the key when
  a later file declares it") and §9's **Unknown** ("how Compose merges a `profiles:`
  sequence … is a detail of Compose's specification that this implementation does not
  reproduce exactly").
* **Evidence:** `[RAN] base2.yml` (`profiles: [p]`) + `over_prof.yml` (`profiles: [q]`) →
  `docker compose --profile p config --services -> x` **and** `docker compose --profile q
  config --services -> x` — the merged list is `[p, q]`.
* **Why it matters:** the script's *outcome* is the same either way (gated if any file
  gates it), so there is no behavioural defect — but a wrong reason recorded in a
  deliverable is the thing the next reader will build on, and the "Unknown" was
  answerable with two commands.
* **Recommended fix:** correct the record: Compose **concatenates** `profiles` across
  files, and the script's "any file gates the service" rule agrees with that. The
  "Unknown" can be closed.

### R-W1-107 [NIT] The refusal corpus tests one spelling per refused shape, and each of the two hole-making shapes has more than one spelling

* **Location:** `tests/deployment/test_fetch_images.py:389-420` (`_REFUSED_SHAPES`) and
  `:314-379` (`_PARITY_SHAPES`).
* **Evidence:** each refused shape appears once, in one spelling. The two shapes that
  slipped through (`<<:`, `include: [..]`) are the *other* spellings of shapes already in
  the corpus.
* **Why it matters:** the corpus is what makes the refusal set a lock rather than a
  promise, and a corpus with one sample per shape is a weak lock — as demonstrated.
* **Recommended fix:** when a shape enters the corpus, add **every** spelling of it, or
  state in the corpus's docstring that one spelling is the sample and the rest are
  uncovered. A cheap structural improvement: a corpus entry per *shape* with a list of
  spellings, so "all spellings of this shape" is the unit.

### R-W1-110 [MAJOR] `storage/relational/__init__.py` — the `get_type_hints` defect is real, latent, cheap to fix, and currently unowned; the docstring's stated reason is false

* **Location:** `src/paa/storage/relational/__init__.py:42-51` (the `TYPE_CHECKING`
  block), `:65` (the annotation that names `Settings` and `PostgresDatabase`), `:21-24`
  (the rationale).
* **Evidence:** `[RAN] typing.get_type_hints(get_database) -> NameError: name 'Settings'
  is not defined`. `[RAN]` with `psycopg`/`psycopg_pool` blocked at `__import__`,
  `paa.storage.relational.postgres` and `paa.storage.relational` both import cleanly —
  so the docstring's *"PostgresDatabase is imported lazily inside the factory so a laptop
  install without the postgres extra (psycopg) stays importable"* is **not the reason**:
  the module is importable without psycopg anyway
  (`[VERIFIED src/paa/storage/relational/postgres.py:119-124]`, `_require_psycopg()`).
  `[VERIFIED src/paa/storage/relational/__init__.py:33]` — `paa.config` is already
  imported at runtime, so `Settings` costs nothing to bind.
* **Why it matters, and how I judge it:** the defect is **real** and it is **the same
  defect class T-004 was chartered to eliminate**, in the package that owns the contract.
  Its **runtime impact today is nil** — `get_database` has one call site
  (`src/paa/runtime.py:286`), is not a FastAPI dependency, is not a pydantic model field,
  and nothing calls `get_type_hints` on it — so I do not call it a blocker. It is MAJOR
  because: it is a latent trap (FastAPI resolves a dependency's signature with
  `get_type_hints`, so the day `get_database` becomes a `Depends()` the failure lands at
  route registration); the reason written next to it is **false**, and the house rule is
  explicit that a wrong reason is how a future reader mis-decides; and it has **no
  owner** — `storage/relational/**` was read-only for W1-IMPL-03, and no task in the
  plan's write-set table owns it.
* **Recommended fix, and by whom:** the fix is two runtime imports —
  `from paa.config import Settings, StorageSettings` at module scope, and
  `from paa.storage.relational.postgres import PostgresDatabase` at module scope (safe
  without psycopg, as measured) — after which `get_type_hints(get_database)` resolves.
  **Owner:** a new, small task (it is not T-004's write-set, and T-004's handling —
  record it, do not fix it — was the right call under the ownership rules). The docstring
  at `:21-24` must be corrected in the same change: the "extra" rationale is wrong, and
  the accurate reason is that the annotation is a string under
  `from __future__ import annotations` and the *name* is only needed by a type checker.
  Until that task exists, the deferral should be **recorded in the tree** the way
  `paa.observability/runs.py` is (the `DEFERRED` map) rather than only in a
  run-scoped report.

### R-W1-111 [MINOR] The "137/137 modules import in a fresh interpreter" figure is wrong in two places in the report

* **Location:** `reports/w1-impl/W1-IMPL-03-T-004.md` §1 criterion 3 and §7's table row
  for `test_every_module_under_src_paa_imports_in_a_fresh_interpreter`; the measurement
  lives in `tests/conventions/test_import_conventions.py:167-180` (`_module_files`).
* **Evidence:** `[RAN]` `_module_files()` → **136** modules (it skips `paa/__init__.py`
  with `if not parts: continue`); the probe → **135** import cleanly, and
  `paa.storage.graph.kuzu_backend` fails with the tolerated *"KuzuGraphStore requires the
  'graph' extra"*. The report's own command log (§11) says *"modules checked: 137 /
  failures: 1"*. So: 137 is the count of `.py` files (including the package
  `__init__.py`), 136 is the count the lock probes, and 135 is the count that imports in
  this environment; the aggregate import is over the 135.
* **Why it matters:** the number is the *acceptance criterion* the task claims to meet
  ("Every module in `src/paa` imports cleanly in a fresh interpreter → 137/137"). A
  criterion stated as met at 137/137 when the measurement is 135/136 is a criterion
  written to be believed rather than to be checked.
* **Recommended fix:** state the measurement the test actually makes: *136 modules are
  probed, 135 import cleanly, one is tolerated for a named optional extra*, and — better
  — have the **test** assert the count it probes so the two cannot drift again. (Not
  asserted: whether `paa/__init__.py` should be probed at all; it is a one-line change
  to `_module_files` and it would make "every module" true.)

### R-W1-112 [MINOR] The classification of the 33 remaining `TYPE_CHECKING` blocks — the plan's §3.2 deliverable — lives only in a run-scoped report

* **Location:** `tests/conventions/test_import_conventions.py:84-89` (`DEFERRED`, one
  entry) and `:95-102` (`OPTIONAL_EXTRA_MODULES`); the classification is in
  `reports/w1-impl/W1-IMPL-03-T-004.md` §3.1 only.
* **Evidence:** `[RAN]` the lock's source half flags only `(paa.storage.relational.base,
  RelationalBackend)`; the 33 other blocks are not in the tree's record in any form. The
  report's own classification (1 cycle, 1 extra, 10 import-cost, 4 stdlib, 17 inert) is
  not in the repository.
* **Why it matters:** the plan asked for these blocks to be *"documented and deferred,
  with the cycle proof"*. They are documented — in `docs/agent-ops/20261022-w6-w7-w9/`,
  a directory named for this run, which the next run will not read; and the plan's own
  §0/§3.2 numbers, which a reader *will* find, are wrong (§8). The one genuinely
  important fact — *`storage/coldlake/cas.py` is the only real cycle in the tree* — is
  the fact most likely to be needed by whoever converts the rest.
* **Recommended fix:** put the classification in the tree, next to the lock that depends
  on it: a table in `tests/conventions/test_import_conventions.py` beside `DEFERRED`
  (module → class → one-line reason), or an ADR. A table is better than prose because
  it can be *checked*: the cycle class is checkable today (the probe already detects
  cycles — `test_the_probe_would_notice_a_cycle` proves the mechanism), so the one real
  cycle could be asserted rather than asserted-about.

### R-W1-113 [MINOR] A stale `DEFERRED` entry is never caught: the lock passes after its owner converts the file

* **Location:** `tests/conventions/test_import_conventions.py:334-352` (the source half)
  and `:404-412` (`test_every_deferral_names_the_task_that_owns_the_file`).
* **Evidence:** `[VERIFIED]` the assertion is `offenders == []` where `offenders` are
  lazy resolvers **not** in `DEFERRED`; the deferral test checks only that the reason is
  non-empty, names a `T-nnn`, and names an existing module. Nothing checks that a
  deferred module is *still* lazy.
* **Why it matters:** when T-008 converts `paa/observability/runs.py`, the `DEFERRED`
  entry becomes a stale statement about a file that no longer resolves the contract
  lazily — and the lock, which is the artefact that is supposed to keep the convention
  true, will not notice. The implementer names this in §14; a deferral that outlives its
  deferral is exactly the "drift nobody is coming back to" the `DEFERRED` docstring
  warns about.
* **Recommended fix:** add the complementary assertion — *every module in `DEFERRED` must
  still resolve the contract lazily*. Then T-008's change turns red until the entry is
  deleted, which is the behaviour the entry's own comment asks for ("delete it when its
  owner lands") and which no test currently enforces.

### R-W1-114 [NIT] `OPTIONAL_EXTRA_MODULES` names `paa.storage.relational.postgres` for an extra it does not need

* **Location:** `tests/conventions/test_import_conventions.py:95-102`.
* **Evidence:** `[RAN]` with `psycopg`/`psycopg_pool` blocked, `paa.storage.relational.postgres`
  imports cleanly — the module imports psycopg lazily
  (`[VERIFIED src/paa/storage/relational/postgres.py:119-124]`). The list entry's comment,
  *"psycopg — paa[postgres]"*, describes a constraint the module does not have.
* **Why it matters:** it is harmless (a tolerated failure that does not happen), but it
  is the same false rationale as R-W1-110 in the same wave, and a list whose comments are
  wrong is a list nobody trusts.
* **Recommended fix:** either drop the entry (the module imports) or correct the comment
  to the real reason it is listed (if there is one). The same question should be asked of
  `paa.api.app` (fastapi **is** installed here, so the entry is currently inert) and of
  `paa.storage.queue.redis_backend` (redis is **not** installed here and the module
  imports anyway — `[RAN] find_spec('redis') -> False`, and the module imported cleanly
  in the sweep).

---

## 5. Attempts to break it

| # | Input | Result | Verdict |
|---|---|---|---|
| 1 | `include: other.yml` (scalar) | script: plan printed, exit 0 | Compose **rejects** the file itself → not a hazard |
| 2 | `include: [other.yml]` (flow list) | script: `included` service missing, exit 0; Compose merges it | **BROKEN — R-W1-102** |
| 3 | `include:` (block) | script: refusal, exit 1, nothing fetched | ✔ refused |
| 4 | `<<: *base` merge key | script: service dropped silently, exit 0; Compose resolves it | **BROKEN — R-W1-101** |
| 5 | `postgres: &pg` (anchored service key) | script: refusal, exit 1 | ✔ refused |
| 6 | `image: *pg` / `image: &img …` | script: refusals, exit 1 | ✔ refused |
| 7 | `image: >-` block scalar | script: refusal, exit 1 | ✔ refused |
| 8 | `image: !override …` | script: refusal, exit 1 | ✔ refused |
| 9 | `extends:` | script: refusal naming file+service, exit 1 | ✔ refused |
| 10 | `profiles: [gpu]` + `image: nvidia/cuda:latest` | script: warned, **exit 0**, no pin refusal | **HOLE — R-W1-103** |
| 11 | `profiles: []` | script: excluded; Compose starts it | divergence — R-W1-105 |
| 12 | override-only image `extra:1:latest` (unpinned) | script: refused, exit 1, message names the service | ✔ the pin rule reaches the merged stack |
| 13 | base `image:` + override `build:` (same service) | script: `build` with the base's pinned image; Compose merges **both** keys and builds | ✔ matches Compose |
| 14 | base `build:` + override `image:` | script: `build`, image = the override's; Compose merges both | ✔ matches Compose |
| 15 | `profiles: [p]` in base + `profiles: [q]` in override | script: gated; Compose: enabled by **both** p and q | outcome ✓, recorded reason ✘ (R-W1-106) |
| 16 | a compose file that does not exist | script: "no such compose file: …", exit 1 | ✔ |
| 17 | the same file given twice (`--compose a --compose a`) | idempotent | ✔ |
| 18 | `PAA_COMPOSE_FILE=Y` **and** `--compose X` | X is read | ✔ the flag wins |
| 19 | no flag, no env var | `docker-compose.yml` at the repo root | ✔ the documented single-file path still works |
| 20 | `typing.get_type_hints(get_database)` | `NameError: name 'Settings' is not defined` | **real — R-W1-110** |
| 21 | `typing.get_type_hints` on the 7 constructors, before and after | 7 raise → 7 resolve | ✔ |
| 22 | un-guard each of the 34 `TYPE_CHECKING` blocks, import in a fresh interpreter | exactly 1 breaks (`coldlake.cas`) | ✔ the implementer's correction is right |
| 23 | un-guard **all 34** at once, probe all 137 modules | the same 1 cycle + 1 missing extra | ✔ (the complement of the implementer's group measurement) |
| 24 | revert one of the seven converted modules | exactly 2 lock tests fail, naming the module | ✔ the lock is a real regression test |
| 25 | `mypy src` on the base, the pre-T-004 tree and HEAD | 0 / 0 / 0 errors (136 / 137 / 137 files) | ✔ the gate holds |
| 26 | `mypy src tests` on the same three trees | 190 / 184 / 184 | ✔ the "same as the pre-change baseline" claim is right for T-004's own baseline |
| 27 | the new T-003 test file against the pre-change script | 18 failed, 37 passed | ✔ |
| 28 | `bash -n deploy/fetch-images.sh` | exit 0 | ✔ |
| 29 | `ruff check src tests` | All checks passed | ✔ no `# noqa`, no widened ignore |

---

## 6. Dimension checklist

| Dimension | Verdict | Notes |
|---|---|---|
| **Correctness** | **FAIL (T-003), PASS (T-004)** | Two YAML shapes outside the refusal set are silently mis-read with a zero exit status (R-W1-101/102); one profile-gated service bypasses the pin rule (R-W1-103). T-004's conversion is correct, complete for its write-set, and its lock is a real regression test. |
| **Root cause vs symptom** | MIXED | T-003's refusals are attached to *spellings* rather than to *constructs* — the root cause of both holes (R-W1-107). The profile hole is an ordering problem: the exclusion happens before the invariants, not after (R-W1-103). T-004's root cause ("one contract, two spellings") was correctly identified and removed at the source. |
| **Regression / compat** | PASS | The pre-change script is still the fallback (`PAA_COMPOSE_FILE`, no flag) and the plan is unchanged for the repository's own files (`[RAN]`). The seven converted modules gained a name in their namespace and lost none; no signature, schema or `__all__` changed. |
| **Concurrency / idempotency** | PASS | The reader is a single `awk` pass over a fixed list of files; the merge is idempotent (`--compose a --compose a`); the fetch's retry unit and digest-inside-the-retry property are untouched. |
| **Error handling** | MIXED | The refusals are loud, specific, name the file and the service, and happen before a byte is fetched — good. But two shapes are not refused (R-W1-101/102) and one is downgraded to a warning while the plan drops the service entirely (R-W1-103). No swallowed error, no blanket `try`, no sleep used to mask anything. |
| **Security / abuse** | PASS | The compose file is treated as untrusted input and the reader refuses rather than guesses — the right posture, and the two holes are the places where it does not. Path handling is sound: paths are `awk` **arguments**, never interpolated into the program text; every shell expansion is quoted. The `docker` command surface is unchanged and still asserted to be exactly `{info, image inspect, pull, tag, load}`. |
| **Performance / scalability** | PASS | One `awk` invocation for all files; the plan is a handful of rows. T-004's real cost is import time: `paa.marketplace.installer` +~95 ms because importing the contract executes the `paa.storage.relational` package `__init__` (`paa.config` + `paa.storage.relational.database` → aiosqlite + the migration runner). The CLI's version path does not regress (the implementer's measurement, plausible and consistent with the import graph). |
| **Migration / rollback** | PASS | No schema, no state, no data format. T-003 rolls back with one commit; T-004 with one commit plus two new files. |
| **Observability** | MIXED | The refusals and the profile warning are good. But the *report* (`--list`, the success line) does not show what was left out (R-W1-104), and the classification of the 33 deferred blocks is not in the tree (R-W1-112). |
| **Maintainability / conventions** | MIXED | Docstrings carry the "why" throughout, as the house requires. Two records carry a wrong fact (R-W1-106, R-W1-110/R-W1-114) and two figures do not match their own measurement (R-W1-111). `AGENTS.md`'s "the gate is zero and must stay zero" holds: `[RAN] ruff check src tests` clean, `[RAN] mypy src` 0, no `# noqa`, no `type: ignore`, no widened ignore, no relaxed `pyproject.toml` rule. |
| **Docs impact** | MIXED | ADR-0030 is **amended**, not rewritten, and states the subset, the refusals and the cost — good practice, and G2/G6 are recorded as "deliberately NOT done", which is what the plan asked. But the amendment repeats the two false refusal claims (R-W1-101/102) and states the `profiles:` rule without its `profiles: []` exception (R-W1-105). `.env.example` and `docs/deployment/docker.md` now carry the five knobs (G8) — verified. |

---

## 7. Optimization and simplification opportunities

| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | `deploy/fetch-images.sh:250-331` | R-W1-101/102 — two shapes are mis-read | closes the "reports success, fetched the wrong thing" hole | S (two rules + two corpus entries) | low | **DO NOW** |
| 2 | `deploy/fetch-images.sh:338-341, 427-437` | R-W1-103 — the pin rule runs over `PLAN`, which no longer holds the skipped service | restores ADR-0030:90 for profile-gated services | M (row shape + `build_plan` + `refuse_*` + `list_images` + one test) | low | **DO NOW** |
| 3 | `deploy/fetch-images.sh:895, 657-663` | R-W1-104 — the report omits what was skipped | the report and the gate stop lying | S (follows from #2) | low | **DO NOW** |
| 4 | `src/paa/storage/relational/__init__.py:42-51, 21-24` | R-W1-110 — the `NameError` is measured; the rationale is false | the contract's own package stops carrying the defect T-004 exists to remove | XS (two imports + a docstring) | very low | **DO NOW** — but it needs an **owner**: `storage/relational/**` was read-only for W1-IMPL-03, so this is a new task, plus a `DEFERRED`-style record in the tree until it lands |
| 5 | `tests/conventions/test_import_conventions.py:404-412` | R-W1-113 — a deferral can outlive its deferral | the deferral map cannot rot silently | XS (one assertion) | very low | **DO NOW** |
| 6 | `deploy/fetch-images.sh:328-331` | R-W1-105 — `profiles: []` diverges from Compose | removes a known divergence | S (refuse the empty form instead of parsing the list) | low | **DO LATER** — it is degenerate; a refusal is enough, and it is not worth blocking the other fixes on |
| 7 | `docs/adr/0030-deployment-image-contract.md` | R-W1-101/102/105/106 — the amendment repeats two false claims and one wrong merge rule | the ADR is the durable record; it must be true | S | none | **DO LATER** — it must land with the fixes it describes, not before |
| 8 | `docs/plans/w6-w7-w9.md` §0/§3.2/§7 | the "48 blocks" and "nine are load-bearing (real runtime cycles)" figures are wrong (41 and one) | the plan is the evidence every later task cites | XS | none | **DO LATER** — wave 3's T-012 owns the records; the corrected numbers are in this report and in W1-REV-01's R-W1-008 |
| 9 | `tests/conventions/test_import_conventions.py:167-180` | `_module_files` skips `paa/__init__.py` | "every module" would be literally true, and the count would stop needing three versions | XS | none | **DO LATER** — pair it with R-W1-111 |
| 10 | `src/paa/storage/relational/__init__.py` (package `__init__`) | the +95 ms import cost of the contract import is the package `__init__` (aiosqlite + the migration runner), not the contract | every consumer of the contract pays it | M | low | **DO LATER** — a lazy package `__init__` is a real change to the storage boundary; out of scope for wave 1. Note it does **not** fix R-W1-110: `get_type_hints` evaluates annotations against the module's `__dict__`, and PEP 562 `__getattr__` is not consulted there |
| 11 | `docker compose config` as the reader | the implementer rejected it (it would make `--list` need the Docker CLI) | it would close G2 *and* R-W1-101/102/105 at once | M | medium (a new dependency for the suite) | **NOT WORTH IT** for this wave — the rejection is right, and the ADR records G2 as deliberately open |
| 12 | A generic YAML parser in the script (PyYAML) | would remove the whole class of shape bugs | one dependency, and the project deliberately has none | M | medium | **NOT WORTH IT** — same reasoning as above, and the refusal set is the cheaper half of the answer |
| 13 | `profiles:` list parsing in both readers | R-W1-105 | closes the degenerate case exactly | M | low | **NOT WORTH IT** — refusing the empty form is enough; parsing a list in two hand-rolled readers is the cost the implementer identified, and it is right |

---

## 8. Cross-agent / cross-task interactions observed

* **W1-REV-01 reached the same T-004 measurement.** Its `R-W1-008` states that `src/paa`
  has 41 `TYPE_CHECKING` guards at `02f3636` (not 48) and 34 after T-004, and that
  hoisting each one to a runtime import breaks the import for exactly one module
  (`storage/coldlake/cas.py`). **I re-derived both numbers independently** — 41/34 by
  `git grep -c`, and the single cycle by an AST sweep that un-guards each of the 34
  blocks in a shadow copy of `src/paa` and imports the module in a fresh interpreter
  (34/34 tried, 1 breaks), plus the complementary measurement of un-guarding all 34 at
  once (1 cycle + 1 missing extra). The two reviews agree, and the plan's §0/§3.2
  numbers are wrong on both counts. **The plan and the records must be corrected** — the
  owner is wave 3's T-012 (`docs/adr/README.md`, `AGENTS.md`, `docs/ROADMAP.md`) plus
  whoever owns `docs/plans/w6-w7-w9.md`. The correct statement is: *41 blocks at the
  base, 34 after T-004's seven conversions; **one** of them
  (`src/paa/storage/coldlake/cas.py:62`) is load-bearing for a real runtime cycle; the
  plan's "nine are load-bearing (real runtime cycles, proved two ways)" and its "48
  blocks" are both wrong.*
* **`storage/coldlake/cas.py:62` is a real cycle and is owned by T-001's implementer,
  not by T-004's.** `coldlake/base.py:67` imports `cas` for `BlobRef` at runtime, so a
  runtime `cas → base` closes the loop. It needs a **seam** (move `BlobStore` into a
  third module, or `BlobRef` out of `cas`), not an import move. T-004's implementer
  recorded it correctly and did not touch it (T-001 owns `coldlake/**`).
  `[VERIFIED src/paa/storage/coldlake/base.py:67, src/paa/storage/coldlake/cas.py:62]`.
  **This is a cross-task item for the wave-1 improvement step**: it is the only genuine
  cycle in the tree, it is now measured and documented, and it is owned by nobody in
  this wave.
* **T-003 and T-001 both wanted `docs/deployment/docker.md`** — the plan's pre-mortem
  says T-001 must not touch it. `[VERIFIED git show 954705d --stat]` — T-003 touched
  `docs/deployment/docker.md` and T-001 did not. No collision. ✔
* **T-003 and T-004 do not interact at all**: T-003's write-set has no `src/paa` file
  and T-004's has no `deploy/` file. The only shared surface is the wave's own gate.
* **The `storage/relational/__init__.py` defect is a *third* instance of the class
  T-004 fixes**, and it is in the package that owns the contract — which makes it the
  most consequential of the three, and the one with no owner.

---

## 9. What I could not verify

* **The gate.** I did not run the full suite (standing orders: scoped runs only). I ran
  `tests/conventions` (7 passed), `tests/deployment/test_fetch_images.py` (55 passed), the
  same test file against the pre-change script (18 failed / 37 passed), `ruff check src
  tests` (clean) and `mypy src` on three trees (0 errors each). The wave gate's
  *"2019 passed / 233 skipped"* is the orchestrator's, not mine.
* **T-001.** Out of my scope; I read `coldlake/{base,cas}.py` only to locate the cycle.
  W1-REV-01 owns T-001.
* **The implementer's import-cost numbers** (`+95 ms` for `paa.marketplace.installer`,
  the per-block millisecond deltas). I confirmed the *mechanism* from the import graph
  and did not re-measure the absolute figures. `[ASSUMED]`
* **The implementer's "eight groups of them together" measurement.** I did the strictly
  stronger complement (all 34 at once) and got the same answer; I did not reproduce
  their eight specific groupings.
* **The pydantic-model byte-identity comparison.** I re-derived the *argument* (no model
  in the family annotates the contract, because such a model would have raised
  `PydanticUndefinedAnnotation` before the change) rather than the two JSON dumps.
* **Compose versions other than v5.6.0.** The merge subset was checked against one
  version, as the implementer says. Compose's merge is not frozen; the refusal set (once
  it is complete, per R-W1-101/102) is the defence.
* **The `usage()` breakage with a relative invocation path** — the implementer's
  out-of-scope finding 1. I did not re-measure it; it is pre-existing and outside my
  write-set, and it is the same class as the relative-path semantics in finding 2.
* **Whether `docker compose`'s `profiles` merge is stable across versions.** I measured
  v5.6.0 (concatenation). The conclusion in R-W1-106 is about this version.

---

**Reviewer's conclusion.** T-004 is, in my judgement, a good piece of work: the root cause
was identified correctly, the conversion is complete for its write-set, the lock is a real
regression test (two tests fail on a revert, measured), the import graph was *measured*
rather than asserted, and the implementer had the confidence to contradict the brief with
a measurement — which I reproduced independently and found correct. The two items it
correctly refused to fix (the `storage/relational/__init__.py` defect and the
`coldlake/cas.py` cycle) are real, are recorded, and need owners.

T-003 is a smaller, more careful change with a **hole in the safety net it advertises**:
the refusal set is presented — in the script's header, in the ADR and in the COMMS line
— as the thing that keeps a wrong merge from being silent, and two shapes walk straight
through it with a zero exit status. Both are cheap to close, and until they are, the
claim "the shapes outside the subset are refused rather than guessed" is false. The
`profiles:` rule is the other half of the same story: the exclusion is announced, but it
happens before the plan's invariants run, so the deployment's central rule — *a pulled
image is digest-pinned* — has a hole that the announcement does not cover.

**Verdict: APPROVE-WITH-CHANGES.** The four MAJOR findings (R-W1-101, R-W1-102,
R-W1-103, R-W1-110) should be closed by the wave-1 improvement step before the wave is
signed off; none of them requires a decision that has not already been taken.
