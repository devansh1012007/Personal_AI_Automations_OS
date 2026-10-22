# Brief — W2-IMPL-02 (implementer) · wave 2 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** make `src/paa/validation/` **trustworthy by test** — the layer
that stops agent-authored code from touching the host — and fix the one
place where it **crashes instead of rejecting**.

**Why this task matters:** this package is a *security* layer whose docstrings
are its only evidence. `ast_scanner.py` says — in the module docstring, in
bold — *"Unparseable source is REJECTED, never skipped. This is the single most
important line in the module."* `patch.py` says *"safe_relative_path is the one
function in this module that must be correct."* `workspace.py` says
*"Determinism is the whole contract."* **Not one of those three claims has a
test.** The package is 3,398 lines with 19 tests, all in one file, all about
the JSON Schema subset. An untested security layer is decoration, and the
ROADMAP says so.

**And there is a real defect in it.** A model-supplied schema with a
malformed keyword value **crashes** the deterministic gate:

```
{"type": "string", "minLength": "abc"}  ⟹  TypeError: '<' not supported between instances of 'int' and 'str'
```

The gate that decides whether an artifact may be committed raises a
`TypeError` instead of returning a verdict. A crash is neither a pass nor a
fail — it is the gate absent.

**What failure looks like:** a test file that restates the docstring, or one
that asserts the scanner's *implementation* rather than its *verdict*.

## 2. Required reading

1. `docs/agent-ops/20261022-w6-w7-w9/briefs/W2-CONTEXT.md` — **read first**.
2. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
3. `docs/plans/w6-w7-w9.md` — **§4 (0.1)** (your task, clause by clause, with
   what is covered and what is not), **§0** (the evidence).
4. `src/paa/validation/ast_scanner.py` — the scanner, its 19 rule names, its
   42 denied modules, 38 denied attributes, 7 denied builtins, and the
   `ScannerPolicy`.
5. `src/paa/validation/patch.py` — `safe_relative_path` (the four escape
   classes), `UnifiedDiffValidator.dry_run`, `PatchApplier.apply` /
   `rollback` (the exact-prior-bytes journal).
6. `src/paa/validation/workspace.py` — `WorkspaceSnapshot`, the three
   normalisations (sorted entries, `/` separators, bytes-not-text).
7. `src/paa/validation/schema_validator.py` — where the crash is.
8. `src/paa/validation/engine.py` and `test_runner.py` — the five ordered
   checks, and the "never report a false pass" rule.
9. `tests/validation/test_schema_validator.py` — the house style in this
   package.
10. `docs/agent-ops/20261022-w6-w7-w9/templates/report-implementation.md`.

## 3. The task

**T-005.** The security layer's docstrings are unproven, and one of them is
false.

### The five ROADMAP clauses, and what each needs

| Clause | What exists today | What you add |
|---|---|---|
| the AST scanner catches **each** forbidden construct | **nothing** — 21 rule names, **0** named in any test; the only exercise of the scanner outside this package is one `os.system` case in the marketplace's installer | one test per rule name, one per denied module/attribute/builtin, and the **configurability** of the policy (the allowlist, the longest-prefix-wins rule, `blocking_severity`, `workspace_root`) |
| unparseable source is **rejected**, not skipped | **nothing** asserts it — and it is the module's own headline | `SyntaxError` → one `CRITICAL` `unparseable_source`; a **NUL byte** → `null_byte_in_source`; a file that is not UTF-8 → `undecodable_source`; an unreadable file → `unreadable_source`; and the **never raises** property for hostile input (including deeply nested source, which must not escape as `RecursionError`) |
| patch apply + rollback restores **exact bytes** | **nothing** — `UnifiedDiffValidator`, `PatchApplier`, `dry_run`: 0 hits in `tests/` | the apply/rollback round trip, byte-exact; the journal's contents; **and the failure path** — a write that fails mid-patch must unwind the whole patch, leaving the prior bytes intact |
| path-traversal patches are refused | **nothing** — `safe_relative_path`: 0 hits in `tests/` | all **four** escape classes the module names: `../` traversal (including forms that only traverse after normalisation), absolute POSIX/Windows/UNC paths, the **symlink escape** (the one a string-only check misses), and NUL/control characters |
| the workspace manifest is **deterministic** | **nothing** — the real `WorkspaceSnapshot` has zero coverage, because `tests/conftest.py` deliberately uses a *fake* | the same manifest across two captures; independence from write order and from directory iteration order; **content-addressed, not mtime-addressed**; CRLF and LF versions of one file hash **differently**; the excluded directories and suffixes; a symlink is skipped, not followed |

### The fix

**A malformed keyword value is a schema error, not a crash.** Decide the
policy explicitly and write it down:

* a keyword whose value has the wrong type (`minLength: "abc"`,
  `maxItems: "2"`, `minimum: "x"`, `multipleOf: "2"`, a `pattern` that is not
  a valid regex) is reported as a **schema error** — the same shape as any
  other invalid schema — and never raises;
* the policy must be **consistent**: the same rule for every keyword, not a
  fix for `minLength` that leaves `maxItems` crashing;
* `strict_keywords=True` already exists and already **rejects an unsupported
  keyword** — decide whether a malformed *value* is a supported-keyword
  problem or a value problem, and say which, and why;
* the fix must not make a **valid** schema fail: a schema that is correct must
  still validate the same instances it validated before.

### And the two modules the ROADMAP does not name but the package contains

`engine.py` (the five ordered checks, the short-circuit on a `CRITICAL`, the
`checks_run` / `skipped` accounting) and `test_runner.py` (the
**"never report a false pass"** rule: pytest absent, exit code 5, and zero
collected are all **explicit failures**, not passes). Both are untested, and
the second is the module's entire reason for existing.

## 4. Acceptance criteria (measurable)

* Every rule name the scanner can emit is asserted by a test that **fails
  without the fix** — and the enumeration must be **complete**: derive the
  rule list from the source (an AST sweep or the policy's own data) so a
  rule added later cannot slip past unnoticed.
* The scanner's **never raises** property is asserted over a hostile corpus
  (empty, whitespace, truncated keywords, a NUL, thousands of unbalanced
  brackets, a 10 000-character comment).
* `safe_relative_path` is asserted for **all four** escape classes,
  including the symlink escape, and a **symlinked workspace** is built in the
  test.
* The apply/rollback round trip is asserted **byte-exact**, and the
  **mid-patch failure** unwinds the whole patch.
* The manifest's determinism is asserted across the three normalisations the
  docstring names.
* The malformed-keyword fix: a table of malformed values, each asserting a
  **schema error and no exception**, plus a test that the **valid** schemas
  still validate.
* **Commands that must pass (scoped):**
  ```bash
  .venv/bin/python -m pytest tests/validation -q
  .venv/bin/python -m ruff check src/paa/validation tests/validation
  .venv/bin/python -m mypy src/paa/validation
  ```
* The wave-1 gate must not regress: ruff 0, mypy 0 across `src`.

## 5. Ownership

**WRITE-SET (you may edit exactly these):**

```
src/paa/validation/schema_validator.py
tests/validation/__init__.py
tests/validation/test_schema_validator.py
tests/validation/test_ast_scanner.py      (new)
tests/validation/test_patch.py            (new)
tests/validation/test_workspace.py        (new)
tests/validation/test_engine.py           (new)
tests/validation/test_test_runner.py      (new)
```

**READ-ONLY:** `src/paa/validation/{ast_scanner,patch,workspace,engine,test_runner,__init__}.py`
— **read them; do not edit them.** If a test you write proves a defect in
them, report it as a `FINDING` with the reproduction and the exact change
required, and mark the test `xfail(strict=True)` **with the reason** — a red
test the orchestrator can see is worth more than a green one that lies. Your
call, stated in the report.

**FORBIDDEN:** everything else. In particular `src/paa/**` outside
`validation/schema_validator.py`, `tests/**` outside `tests/validation/`,
`pyproject.toml` (T-002 owns it), `deploy/**`, `docker-compose*.yml`,
`docs/**` (no ADR is needed for a test suite and a policy — **say so in your
report**; if you think one is needed, `ESCALATE` and explain),
`docs/adr/README.md`, `docs/plans/**`, `AGENTS.md`.

**Note:** `src/paa/validation/__init__.py` is **read-only** for you — it
re-exports the package's symbols, and the plan's wave-1 lesson is that a
package `__init__` is where import cycles hide.

## 6. Environment

* Scoped checks only. **Never** the full suite, never a container, never a
  port.
* `tests/validation/` has **no `conftest.py`**; the nearest fixtures are in
  `tests/conftest.py` (`paa_home`, `settings`, `db`, `ledger`,
  `FakeSnapshotter`). The house convention here is self-contained test files.
* A test that writes files uses `tmp_path`. A test that builds a symlink
  needs a platform that has them — Linux does; if a case cannot be expressed
  portably, skip it with a reason rather than pretending.
* Kill any process you start.

## 7. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/W2-IMPL-02-T-005.md`
  — `templates/report-implementation.md`, ORCH-DIGEST first, appended as you
  work.
* **Commits:** code first, then the report in a separate docs-only commit.
  Trailers: `Task: T-005`, `Agent: W2-IMPL-02`, `Wave: 2`, `Report: <path>`.
  Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- src/paa/validation/schema_validator.py tests/validation/
  git commit -m "<msg>" -- src/paa/validation/schema_validator.py tests/validation/
  ```
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | blockers`.
  **It must not be empty.**
