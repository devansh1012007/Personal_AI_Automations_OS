# Wave 1 review context — run 20261022-w6-w7-w9

**Read this first.** It is the shared context for all three wave-1 reviewers.
Your own brief (`briefs/<your-ID>.md`) says which lens you wear.

## The wave

Wave 1 is the substrate fixes. Three agents worked in parallel on disjoint
write-sets; the ownership table is `ownership/W1.tsv` and the overlap check
(`cut -f2 ownership/W1.tsv | sort | uniq -d`) is empty.

| Task | Agent | Subject | Diff |
|---|---|---|---|
| **T-001** | `W1-IMPL-01` | the blob substrate gets a neutral error vocabulary | `ce51df8` (code + ADR-0032), `602d18c` (report) |
| **T-003** | `W1-IMPL-02` (re-dispatched as `W1-IMPL-02R`) | the fetch script reads the **merged** compose stack | `954705d` (code), `db99c66` (ADR amendment), `3ac9d6b` (report) |
| **T-004** | `W1-IMPL-03` | one contract, one way of resolving it (`TYPE_CHECKING` → runtime) | `7747fa0` (code), `de5586a` + `ea3604d` (report) |

**Diff range:** `git diff 02f3636..3ac9d6b -- . ':!docs/agent-ops'`

The plan the wave executed is `docs/plans/w6-w7-w9.md`. **§0** states the
evidence the work rests on, **§3.1 / §2.2 / §3.2** state each task's design,
and **§6** records the decisions D-1…D-6 that were taken before the wave
started (they are *given*, not open — a reviewer who thinks one of them is
wrong should say so with evidence, but the implementation was not required to
re-litigate them).

## The gate at the wave's end

`gates/gate-w1-1.md`: ruff **0**, mypy **0** (137 source files), **2019
passed / 233 skipped** (baseline 1915/233), ownership audit clean.

## The implementer reports (read AFTER your blind pass)

* `reports/w1-impl/W1-IMPL-01-T-001.md` (T-001) — 51 KB, includes the deep-dive addendum
* `reports/w1-impl/W1-IMPL-02-T-003.md` (T-003)
* `reports/w1-impl/W1-IMPL-03-T-004.md` (T-004) — includes the deep-dive addendum

## The claims to audit

Every one of these is an implementer claim. **Verify each against the code**;
an unsupported claim is a finding.

### T-001 (the blob vocabulary)

1. "No botocore exception escapes the boundary" — every exception leaving
   `S3BlobStore` and `ContentAddressedStore` is a member of the new vocabulary
   or a `PaaError` that is not ours.
2. "`put()` and `put_stream()` on a missing bucket raise the **same** type."
3. "The translation is **idempotent** — an error that is already ours comes
   back unchanged."
4. "The `BlobStore` protocol now states the error behaviour, and a test
   asserts the two implementations agree with it."
5. "The `sha256=key` mislabelling is fixed — the not-found path carries the
   **digest**."
6. "28 of the new tests fail against the pre-change sources."
7. **The behaviour change the implementer flagged:** a missing S3 bucket now
   raises `NotFound` from `exists()` / `stat()` / `delete()`, where it
   previously answered `False` / `None` / `False`. *Is that the right call?*
   Is it consistent with the protocol's new error clause? Does any caller
   depend on the old answer?

### T-003 (the merged compose stack)

1. "`--compose <path>` is repeatable and merges in file order; later wins;
   the flag beats `PAA_COMPOSE_FILE`."
2. "The expected service set in the parity test is now **derived**, not
   hard-coded."
3. "`profiles:`-gated services are excluded, with a stderr notice."
4. "`extends:` / anchors / aliases / a multi-line `image:` / a custom tag /
   a top-level `include:` are **refused** rather than mis-read."
5. "The merge subset was cross-checked against real Compose v5.6.0
   (`docker compose config`, read-only, copies in /tmp)."
6. "18 of the new tests fail against the pre-change script."
7. "G2 and G6 are **recorded, not fixed**."

### T-004 (the import consistency)

1. "Seven `TYPE_CHECKING` imports of `RelationalBackend` became runtime
   imports; `observability/runs.py` was left alone (deferred to T-008)."
2. "Before the change `get_type_hints` on all seven constructors raised
   `NameError`; after, they resolve. Pydantic model fields are byte-identical."
3. "137/137 modules import in a fresh interpreter; a 136-module aggregate
   import also succeeds."
4. "The lock is 7 tests, and reverting the change fails two of them."
5. **The implementer's correction to the brief:** *"the brief's 'nine blocks
   are load-bearing for a real runtime cycle' is overstated — converting each
   of the 34 blocks one at a time, and eight groups of them together, broke an
   import only for `cas.py`."* The plan's §3.2 and §0 therefore carry a
   **wrong piece of evidence**. Verify the measurement, and say what the
   correct statement is — the plan and the records must be corrected.
6. The implementer found two **new** defects it could not fix (both are
   out of its write-set): `storage/relational/__init__.py:42-43` keeps
   `PostgresDatabase` and `Settings` behind `TYPE_CHECKING` while
   `get_database`'s return annotation names them, so
   `typing.get_type_hints(get_database)` raises `NameError`. **Is that a real
   defect, and what should be done about it?**

## The house rules a reviewer is asked to hold

* The gate is **zero and must stay zero**. A `# noqa`, a `# type: ignore`, a
  widened ruff ignore or a relaxed `pyproject.toml` rule is a **finding**,
  every time. The house precedent: a finding that is genuinely not a finding
  is *deleted*, not silenced.
* A `LIMIT` cut needs a total order; every `LIKE` pattern comes from
  `storage/relational/like.py`.
* Docstrings carry the reasoning; a comment explaining *what* instead of *why*
  is a minor finding.
* No band-aids: no swallowed errors, no blanket `try`/`except`, no
  sleep/retry to mask a race, no special-cased input to satisfy a test, no
  weakened test, no suppressed type, no feature flag disabling a failing path.
* A test that cannot fail without the fix is not a test.

## The environment

* `.venv/bin/python` is the interpreter. `python` on the host `PATH` is
  `/usr/bin/python` (the sandbox's child `PATH` is
  `/usr/local/bin:/usr/bin:/bin`, so it resolves).
* Scoped runs only — a reviewer may run a single test file, a lint of a
  subset, or a type check. **Do not run the full suite** and do not restart or
  prune any container.
* `boto3` is **not installed** — the S3 tests must pass without it.
* **Never stop, restart or rebuild another project's containers.**
