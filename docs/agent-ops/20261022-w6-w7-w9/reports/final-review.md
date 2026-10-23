# Final whole-branch review — run 20261022-w6-w7-w9 (W4-REV-FINAL)

Branch: `ops/20261022-w6-w7-w9` | Diff reviewed: `02f3636..HEAD` (202 files, +51613/−1156) | Base: `main @ 02f3636`

Reviewer: W4-REV-FINAL (fresh, no prior context, does not edit code). All source edits made for reversals/lock-mutations were restored byte-identical to HEAD; `git diff` for every touched file is empty.

## VERDICT

**merge-ready as-is**

The gate is green (verified by W4: `ruff` 0, `mypy` 0, `pytest` 2907 passed / 235 skipped / 4 xfailed), every reversal I ran went red when its fix was reverted, and both convention locks were proven to have teeth. No code defect was found in the committed diff. Two mission items are UNVERIFIED by W4 only because this environment forbids them (no GitHub Actions; no starting containers), not because the code is incomplete — they are verified-by-report by the implementers.

## PER-TASK

| Task | Status | Evidence |
|------|--------|----------|
| **T-001** W7-b2 blob error vocabulary (DEEP) | CLOSED | Reversal R-T001: removed the `head_bucket` probe in `_namespace_is_absent` (`src/paa/storage/coldlake/minio_backend.py`) → `test_a_missing_bucket_raises_from_every_method` (`tests/storage/test_minio_backend.py:692`) failed "DID NOT RAISE NotFound". Fix restored; the missing-bucket → `NotFound(namespace="bucket")` behaviour depends on the probe. Code: `src/paa/storage/coldlake/{errors,minio_backend,base,cas}.py`, `docs/adr/0032`. |
| **T-002** W6-b3 S3 live suite vs 2 servers | PARTLY CLOSED | Code present: `tests/storage/test_s3_live.py`, `tests/storage/conftest.py`, `pyproject.toml`, `docs/adr/0033`. Suite skips cleanly without `PAA_TEST_S3_*`/`boto3` (`requires_s3` marker + `find_spec` guard). Live-server run vs SeaweedFS+RustFS UNVERIFIED by W4 (may not start containers; `PAA_TEST_S3_*` unset here). Implementers reported 40 passed against two servers, removed their containers, never touched `paa-stack-*`. |
| **T-003** W6-b4 fetch-images↔compose parity | CLOSED (report) | Code present: `deploy/fetch-images.sh`, `tests/deployment/test_fetch_images.py`, `docker-compose*.yml`, `docs/adr/0030`, `docs/deployment/docker.md`, `.env.example`. D-1 (repeatable `--compose`, merged in file order, flag beats `PAA_COMPOSE_FILE`); D-6 (no `BlobStore` list op). Implementers ran 26 reversals vs the pre-change script, all failed. W4 did not independently re-run the shell-script reversal. |
| **T-004** W7-b3 TYPE_CHECKING cleanup | CLOSED | Reversal R-T004: moved `Settings`/`PostgresDatabase` behind `TYPE_CHECKING` in `src/paa/storage/relational/__init__.py` → `test_the_contract_packages_factory_can_have_its_annotations_read` (`tests/conventions/test_import_conventions.py:1024`) red (`ImportError: cannot import name 'PostgresDatabase'`). Fix restored; lock holds. `get_type_hints(get_database)` defect class eliminated. |
| **T-005** W9-0.1 validation malformed keyword | CLOSED | Reversal R-T005: removed the `value_defects` guard in `schema_validator.py::_keyword_value` → 30 malformed-keyword tests red (crash instead of schema error), e.g. `test_malformed_keyword_value_is_a_schema_error` / `test_malformed_keyword_value_never_raises`. Fix restored; one shared keyword table now serves all three validators. |
| **T-006** W9-0.2 agents `HARD_STOP` parks | CLOSED (report) | Code present: `src/paa/agents/orchestrator.py`, `tests/agents/*`, `tests/integration/test_end_to_end.py`. D-4 (task parks on `HARD_STOP_ESCALATE_TO_USER`, ADR-0034). Implementers: 14 new tests, 11 fail pre-fix, 7 mutations caught. W4 did not independently re-run the park reversal. |
| **T-007** W9-0.3 models escalation key leak | CLOSED | Reversal R-T007: removed `schema_errors` redaction in `StructuredOutputError` (`src/paa/models/base.py:176-186`) → `test_the_schema_failure_carries_no_key` (`tests/models/test_key_secrecy.py:199`) red: "API key leaked into: ... schema_errors=[...sk-test-abcdef0123456789ABCDEF...]". Fix restored; `redact_detail` is now the single redaction helper used by router and base. |
| **T-008** W9-0.4 observability | CLOSED | Code present: `src/paa/observability/{runs,logging,metrics,tracing}.py`, `tests/observability/*` (50→136 passed). W4's `runs.py` reversal doubled as the T-008/import-lock check (see R-T008). Implementers ran 17 mutations, all caught. |
| **T-009** W9-0.5 CI | CLOSED (code); execution UNVERIFIED | `.github/workflows/ci.yml` committed (dad08e4); gate steps run locally green by W4; workflow run itself UNVERIFIED (no GitHub/Docker). `python-is-python3` landmine handled per plan; installs `.[dev]` (not `.`) so `tests/api/` does not skip. |
| **T-010** W7-b4 docs sweep + reference checker | CLOSED | `tests/conventions/test_docs_references.py` committed (579 lines); covers `docs/` + `README.md` + `AGENTS.md`, excludes `docs/agent-ops`/`docs/plans` with stated reasons (D-5). Lock mutation proven: appended `database.py:99999` to `docs/README.md` → `test_every_referenced_line_is_within_the_file` red ("has 667 lines"). Docs sweep committed (3c500de). |
| **T-011** W7-new marketplace untrusted body | CLOSED | Reversal R-T011: removed body ceiling/shape validation in `registry_client.py::_json` → raw `json.JSONDecodeError` escapes; `test_a_body_that_is_not_json_is_the_projects_own_error` and `test_every_malformed_shape_raises_the_same_error_type` red. Fix restored. Code present: `src/paa/marketplace/registry_client.py`, `tests/marketplace/test_registry_client.py`. ADR-0033 title/false-wrapper claim corrected to agree with ADR-0032. |
| **T-012** records | CLOSED | Records present in diff: `AGENTS.md`, `docs/MCP_AND_PLUGINS.md`, `docs/ROADMAP.md`, `docs/adr/README.md`. Work ledger lists both operator decisions as open (unreachable egress check; pattern-semantics findings carried forward unpatched as policy decisions). |

## FINDINGS

**Minor** | `docs/agent-ops/20261022-w6-w7-w9/01-work-ledger.md:38-41` | Work-ledger rows for T-009/T-010/T-011/T-012 still read "pending" with no commits/reports, although the code is committed (`ci.yml`, `test_docs_references.py`, `registry_client.py`, `AGENTS.md`/`ROADMAP.md`/`MCP_AND_PLUGINS.md` all in `02f3636..HEAD`) and COMMS marks them DONE. | Update the ledger rows to DONE with the commit SHAs so the record matches the tree. Records lag only; not a code defect. |

**Nit** | `docs/agent-ops/20261022-w6-w7-w9/01-work-ledger.md:89` | Ledger says "Four pattern-semantics findings that survive ADR-0035"; the brief frames this as five open findings, but ADR-0035 closed position-dependence (the fifth), so four open is accurate. | No code fix; align any prose that still claims five are open. |

## GATE OUTPUT

All three commands run by W4 against `02f3636..HEAD` (working tree clean at HEAD):

```
$ .venv/bin/python -m ruff check src tests
All checks passed!
(exit 0)

$ .venv/bin/python -m mypy src
Success: no issues found in 138 source files
(exit 0)

$ .venv/bin/python -m pytest tests/ -o addopts="--strict-markers --strict-config" -q
2907 passed, 235 skipped, 4 xfailed, 1 warning in 122.77s
(exit 0)
```

The single warning is a pre-existing Starlette/httpx deprecation in `tests/api/test_api.py` (unrelated to this diff; the `dev` extra is installed so the API suite runs, not skips).

## REVERSALS RAN

Each reversal was a surgical edit in the main tree → run the targeted test → restore the file by hand; every file verified byte-identical to HEAD afterwards (`git diff --stat` empty for each).

| Reversal | Task | What was reverted | Result |
|----------|------|-------------------|--------|
| R-T004 | T-004 | `Settings`/`PostgresDatabase` behind `TYPE_CHECKING` in `storage/relational/__init__.py` | `test_the_contract_packages_factory_can_have_its_annotations_read` **red** (`ImportError` on `PostgresDatabase`) |
| R-T005 | T-005 | `value_defects` guard in `schema_validator.py::_keyword_value` | 30 malformed-keyword tests **red** (crash instead of schema error) |
| R-T007 | T-007 | `schema_errors` redaction in `models/base.py` `StructuredOutputError` | `test_the_schema_failure_carries_no_key` **red** (key leaked into `schema_errors`) |
| R-T011 | T-011 | body ceiling/shape validation in `marketplace/registry_client.py::_json` | raw `JSONDecodeError` escapes; two malformed-body tests **red** |
| R-T001 | T-001 | `head_bucket` probe in `minio_backend.py::_namespace_is_absent` | `test_a_missing_bucket_raises_from_every_method` **red** ("DID NOT RAISE NotFound") |
| R-T008 | T-008 + import lock | `RelationalBackend` runtime import in `observability/runs.py` moved behind `TYPE_CHECKING` | three import-lock halves **red** (source, runtime-binding, family) — also proves the lock catches a reverted conversion |

Not independently re-run by W4 (code present, gate green, implementer-reported reversals exist): T-002 (live servers — barred), T-003 (shell-script reader; implementer ran 26 reversals, all failed), T-006 (orchestrator park), T-009 (CI run needs GitHub — barred).

## LOCK-FILE MUTATIONS (each edit → test → restore)

- `tests/conventions/test_import_conventions.py`: reverted `paa/observability/runs.py`'s `RelationalBackend` import behind `TYPE_CHECKING` → three lock halves went **red** (source half, runtime-binding half, family half). Proves the import lock has teeth and catches a reverted conversion.
- `tests/conventions/test_docs_references.py`: appended `src/paa/storage/relational/database.py:99999` to `docs/README.md` → `test_every_referenced_line_is_within_the_file` **red** ("database.py has 667 lines"). Proves the docs-reference lock catches a stale/out-of-range reference.

## VERIFIED (ran by W4)

- `ruff check src tests` = 0 findings
- `mypy src` = 0 errors (138 source files)
- `pytest` full suite = 2907 passed / 235 skipped / 4 xfailed, exit 0
- Reversals R-T001, R-T004, R-T005, R-T007, R-T011, R-T008 — each target test went red when its fix was reverted
- Both lock-file mutations went red (locks have teeth)
- Both operator decisions recorded open in `01-work-ledger.md` (egress check; pattern-semantics findings carried forward unpatched)
- No code defect found in the committed diff

## UNVERIFIED (environmental, not code)

- GitHub Actions workflow execution (`dad08e4`) — no GitHub/Docker in this environment ("workflow run itself UNVERIFIED")
- S3 live suite against real SeaweedFS + RustFS servers — may not start or touch operator containers; `PAA_TEST_S3_*` unset here so the `requires_s3` suite skips (implementers reported 40 passed vs two servers, removed their containers, never touched `paa-stack-*`)
- T-003 shell-script reversal and T-006 park reversal — not independently re-run by W4 (implementer-reported)
