# Module: Validation (`src/paa/validation/`)

## Module identity

- **Name:** Validation
- **Path:** `src/paa/validation/`
- **Main responsibility:** Deterministic, host-side validation that decides whether a candidate mutation may commit — binary PASS/FAIL, no model consulted for security decisions (RFC §13).
- **Architectural layer:** Security / validation
- **Entrypoints:** `DeterministicValidationEngine.validate(artifact)`
- **Public interfaces:** `DeterministicValidationEngine`, `ValidationArtifact`, `ValidationReport`, `ValidationVerdict` (`engine.py`); `AstSecurityScanner`, `Severity`, `Finding`, `ScannerPolicy` (`ast_scanner.py`); `UnifiedDiffValidator`, `PatchPlan`, `safe_relative_path`, `compute_patch_sha256` (`patch.py`); `SchemaValidator` (`schema_validator.py`); `PytestRunner`, `PytestResult` (`test_runner.py`); `WorkspaceSnapshot` (`workspace.py`)
- **Other modules that depend on it:** `runtime.py` (`CriticValidatorAdapter`, lazy import), `agents/reasoning.py` (CriticReviewer), `skills/usa.py` (output validation)

## Purpose and responsibilities

RFC §13: the runtime cannot act on "mostly valid" — a mutation is committed or
it is not. A confidence score at this boundary would relocate the security
decision to an LLM, which §13 says must not make security decisions
(`engine.py:9`).

Cheap-to-expensive ordering with short-circuit on hard failure:
1. Schema (microseconds)
2. AST security scan (milliseconds)
3. Patch dry run (milliseconds)
4. Workspace drift (~100ms)
5. Test suite (seconds — runs last, only if above cleared; executes the artifact)

## Internal implementation

### `engine.py` — DeterministicValidationEngine
`validate(artifact)` runs checks in order; short-circuits on `severity >=
CRITICAL` or `structural_failure`; ordinary failures accumulate so one report
lists everything wrong (`engine.py:208`). `ValidationReport` is binary
`PASS`/`FAIL`; `raise_for_verdict()` raises `ValidationError` on fail
(`:173`).

### `ast_scanner.py` — AstSecurityScanner
Deny-by-default static scan. Denied modules/attributes/builtins (`:122`–`:244`);
sensitive paths; obfuscation (decoder + dynamic exec). Unparseable source →
CRITICAL finding, **never skipped** (`:342`). `scan_or_raise()` raises
`SecurityScanError` on blocking findings (`:403`).

### `patch.py` — UnifiedDiffValidator
Pure-Python unified-diff parse/dry-run/apply/rollback with workspace jail
(`safe_relative_path` blocks traversal/absolute/symlink/NUL, `:270`).
Transactional apply with in-memory rollback journal (`PatchApplier.apply`,
`:672`). `compute_patch_sha256` normalises line endings (`:786`).

### `schema_validator.py` — SchemaValidator
Pure-Python JSON Schema subset; `$ref`/`if`/`then`/`else` always rejected;
unsupported keywords ignored unless `strict_keywords` (`:340`). Used for skill
output validation by the USA (`skills/usa.py:48`).

### `test_runner.py` — PytestRunner
Runs `python -m pytest` inside a sandbox; `ok` requires positive evidence
(pytest present, tests collected, exit 0, passed>0) (`:81`).

### `workspace.py` — WorkspaceSnapshot
Manifest capture + drift detection (`hash_file`).

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `Engine.validate(artifact)` | `ValidationArtifact` | `ValidationReport` | may run pytest in sandbox |
| `Scanner.scan(source)` | Python source | list of `Finding` | pure |
| `Validator.dry_run(patch, workspace)` | diff text + workspace | `PatchPlan` | reads files, no writes |
| `Applier.apply(plan)` | `PatchPlan` | `PatchJournal` | writes files (atomic + rollback) |
| `PytestRunner.run(workspace, ...)` | workspace path | `PytestResult` | executes tests in sandbox |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `CriticReviewer` → `Engine` | `validate(output)` | Review step output | `reasoning.py:275` |
| `CriticValidatorAdapter` (runtime) → `Engine` | adapt call shape | Bridge critic ↔ engine | `runtime.py:80` |
| `Engine` → `Scanner/Validator/PytestRunner/Snapshot` | delegate checks | Compose verdict | `engine.py:215` |
| `USA` → `SchemaValidator` | `validate_tool_output()` | Validate skill output | `usa.py` |
| `PytestRunner` → `Sandbox` | `run(spec)` | Execute tests contained | `test_runner.py` |

## Runtime behavior

- Lazy import in `runtime.py` (`try/except`) — validation unavailable degrades
  gracefully (`runtime.py:436`).
- Tests run last and only inside a sandbox (`engine.py:233`).
- `ok` requires positive evidence; absence of failure ≠ pass (`test_runner.py:81`).

## Failure behavior

- No sandbox/workspace + tests requested → FAIL (unrun suite is not passing)
  (`engine.py:377`).
- Patch without workspace → structural failure (`engine.py:328`).
- AST unparseable source → CRITICAL finding, never skipped
  (`ast_scanner.py:342`).
- Malformed operator rules logged and skipped (policy layer, not validation).

## Modification impact

- Adding a check to `engine.py` changes the ordered pipeline and short-circuit
  logic.
- `safe_relative_path` is the workspace jail — changes affect patch apply +
  drift checks.
- Tests: `tests/validation/test_schema_validator.py` (19 tests) covers the schema
  validator directly; the rest of the subsystem is exercised via agents.

## Known gaps

- Deterministic validation is exercised via agents/critic; only the schema
  validator has its own suite.

## Related documents

- [`security/security-model-and-trust-boundaries.md`](../security/security-model-and-trust-boundaries.md) — validation as security control.
- [`modules/agents.md`](agents.md) — CriticReviewer.
- [`modules/coder.md`](coder.md) — no direct use (coder uses runtime tools).
- [`data/data-lifecycle.md`](../data/data-lifecycle.md) — workspace/patch lifecycle.
- [`docs/adr/0028` pure-Python schema subset](../adr/0028-pure-python-json-schema-subset.md)
  (rationale: jsonschema optional)
