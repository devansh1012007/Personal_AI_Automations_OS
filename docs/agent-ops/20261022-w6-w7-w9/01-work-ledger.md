# Work ledger — 20261022-w6-w7-w9

*(orchestrator-owned; source of truth for resume. The plan's §5 is the
authoritative task table; this file records the live state.)*

## Plan coverage

| Plan item | Summary | Disposition |
|---|---|---|
| **W6-b1** | the cross-engine parity suite | **DONE** by W3 — `tests/storage/test_entity_parity.py` |
| **W6-b2** | the PostgreSQL contract suite | **DONE** — `test_postgres_contract.py`, `test_postgres_migrations.py` |
| **W6-b3** | the parametrised S3 lifecycle and failure-mode suite | **T-002** (wave 2) |
| **W6-b4** | the fetch-images ↔ compose parity test | **T-003** (wave 1) — extends W4's half |
| **W7-b1** | the `BlobStore` protocol vs the cold lake's own store | **T-001** (wave 1) — the signature mismatch is already fixed; the residue is behavioural |
| **W7-b2** | the S3 error path is not neutral | **T-001** (wave 1) |
| **W7-b3** | the `TYPE_CHECKING` leftovers | **T-004** (wave 1) |
| **W7-b4** | the stale comments and documentation | **T-010** (wave 3) |
| **W9-0.1** | test `validation/` | **T-005** (wave 2) |
| **W9-0.2** | test `agents/` | **T-006** (wave 2) |
| **W9-0.3** | test `models/` | **T-007** (wave 2) |
| **W9-0.4** | test `observability/` | **T-008** (wave 2) |
| **W9-0.5** | wire CI | **T-009** (wave 3) |
| — | the marketplace's untrusted HTTP body (a recon finding, not in the track document) | **T-011** (wave 2) |
| — | the records: the ADR index, the track document, `AGENTS.md`, the ROADMAP | **T-012** (wave 3, orchestrator) |

## Tasks

| Task | Plan items | Domain | Class | Root cause (1 line) | Write-set | Contract-set | Env-set | Depends on | Conflicts | Wave | Agent | Status | Commits | Report |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| T-001 | W7-b1, W7-b2 | storage/coldlake | **DEEP** | The blob substrate translates nothing, so a driver's exception crosses the boundary and the contract is silent about failure | `src/paa/storage/coldlake/{errors,minio_backend,base,cas,__init__}.py`, `tests/storage/{test_blob_errors,test_minio_backend,test_blobstore_contract}.py`, `docs/adr/0032-*.md` | the `BlobStore` protocol's error clause; the exception types the S3/CAS stores raise | none | — | C2 with T-002 (both touch the cold lake) | 1 | W1-IMPL-01 | pending | — | `reports/w1-impl/W1-IMPL-01-T-001.md` |
| T-003 | W6-b4 | deploy | SURFACE | The script reads one compose file and the parity test hard-codes the service set, so a merged stack is invisible to both | `deploy/fetch-images.sh`, `tests/deployment/test_fetch_images.py`, `docker-compose*.yml`, `docs/adr/0030-*.md`, `docs/deployment/docker.md`, `.env.example` | the script's CLI and its env surface (`PAA_COMPOSE_FILE`, `PAA_FETCH_*`) | none | — | C1 with T-001 on `docs/deployment/docker.md` → T-001 does not touch it | 1 | W1-IMPL-02 | pending | — | `reports/w1-impl/W1-IMPL-02-T-003.md` |
| T-004 | W7-b3 | core imports | SURFACE | One commit left eight siblings expressing the same dependency two different ways | `src/paa/memory/{contradiction,decay,curator}.py`, `src/paa/marketplace/installer.py`, `src/paa/skills/registry.py`, `src/paa/agents/{policy,context_agents}.py`, `tests/conventions/test_import_conventions.py` | the import graph of eight modules | none | — | C1 with T-001 on `coldlake/__init__.py` → T-004 does not touch it | 1 | W1-IMPL-03 | pending | — | `reports/w1-impl/W1-IMPL-03-T-004.md` |
| T-002 | W6-b3 | storage + deploy | SURFACE | The S3 path is only ever tested against a stub, so the six raw-escape sites were invisible | `tests/storage/test_s3_live.py`, `tests/storage/conftest.py`, `pyproject.toml`, `docs/adr/0033-*.md` | the `PAA_TEST_S3_*` convention; the `requires_s3` marker | SeaweedFS + RustFS containers | T-001 | C2 with T-001 (the error names it asserts) | 2 | — | pending | — | `reports/w2-impl/…` |
| T-005 | W9-0.1 | validation | SURFACE | The security layer's docstrings are unproven | `src/paa/validation/schema_validator.py`, `tests/validation/*` | the malformed-keyword policy | none | — | — | 2 | — | pending | — | — |
| T-006 | W9-0.2 | agents | SURFACE | The exact event sequence and the mediated cycle are unasserted; `HARD_STOP` does not park | `src/paa/agents/orchestrator.py`, `tests/agents/*`, `tests/integration/test_end_to_end.py` | the `HARD_STOP` behaviour (D-4) | none | — | — | 2 | — | pending | — | — |
| T-007 | W9-0.3 | models | SURFACE | The key leaks on the escalation path; the semaphore and the LOCKDOWN spy are unasserted | `src/paa/models/{router,base}.py`, `tests/models/*` | the `MODEL_ESCALATED` discriminator scope | none | — | — | 2 | — | pending | — | — |
| T-008 | W9-0.4 | observability | SURFACE | The percentiles are labelled but not valued; `logging.py` has zero coverage | `src/paa/observability/{runs,logging}.py`, `tests/observability/*` | none | none | — | — | 2 | — | pending | — | — |
| T-011 | W7-new | marketplace | SURFACE | The remote registry chooses the local exception type | `src/paa/marketplace/registry_client.py`, `tests/marketplace/*` | the `HttpRegistry` error behaviour | none | — | — | 2 | — | pending | — | — |
| T-010 | W7-b4 | docs | SURFACE | The line-number references rotted and nothing checks them | `docs/**` (named files), `README.md`, `docs/adr/0019-*.md`, `tests/conventions/test_docs_references.py` | none | none | all code tasks | — | 3 | — | pending | — | — |
| T-009 | W9-0.5 | ci | SURFACE | Nothing enforces the gate on a push | `.github/workflows/*.yml` | the CI contract | a PostgreSQL service container | all code tasks | — | 3 | — | pending | — | — |
| T-012 | — | records | lead | the track document and the index still describe work that is done | `docs/adr/README.md`, `docs/MCP_AND_PLUGINS.md`, `AGENTS.md`, `docs/ROADMAP.md` | none | none | all | — | 3 | ORCH | pending | — | — |

## Waves

### Wave 1 — the substrate fixes

* **WAVE_BASE_SHA:** `02f3636`
* **Agents:** `W1-IMPL-01` (T-001, DEEP), `W1-IMPL-02` (T-003), `W1-IMPL-03` (T-004)
* **Ownership file:** `ownership/W1.tsv`
* **Pre-mortem:** T-001 and T-004 both reach for `src/paa/storage/coldlake/` and
  `src/paa/agents/`; T-001 and T-003 both want `docs/deployment/docker.md`.
  ⟹ **T-001 owns the whole `coldlake/` package and does not touch the
  deployment docs; T-004 is forbidden from `coldlake/__init__.py` and from
  `docs/`; T-003 owns the deployment docs.** T-004 converts eight imports to
  runtime, and a cycle would only show under a specific import order ⟹ the
  acceptance criterion is a **fresh-interpreter import of every module in
  `src/paa`**, checked by a subprocess.
* **Gate results:** `gates/gate-w1-1.md` → …
* **Review findings → dispositions:** —
* **Review/improve cycles used:** 0/2
* **Status:** dispatching

### Wave 2 — the module suites (0.1–0.4) + the S3 live suite + the marketplace
### Wave 3 — the documentation, the CI, the records

## Deep dives

| Task | Agent | Design approved by | Report | Reviewers |
|---|---|---|---|---|
| T-001 | W1-IMPL-01 | the plan's §3.1 (D-1…D-6 taken as written) | `reports/w1-impl/W1-IMPL-01-T-001.md` | two lenses: correctness/design, then operability/scale |

## Deferred / open items

* The ~22 `TYPE_CHECKING` blocks outside the eight-file family — deferred with
  the cycle proof (plan §3.2).
* The `BlobStore` list operation — deliberately not added (D-6).
* The `docs/` sweep covers all of `docs/` (D-5); the checker is part of T-010.

## Out-of-plan discoveries

* The malformed JSON-Schema keyword value (reproduced) — folded into T-005.
* The marketplace's untrusted body (reproduced) — T-011.
* The API-key leak on the escalation path (reproduced live) — T-007.
* `HARD_STOP_ESCALATE_TO_USER` does not park the task — T-006 (D-4).

## Temporary mitigations (user-approved)

* None. No stopgap has been proposed, and none is approved.

## Assumptions

See `00-mission.md` §"The assumptions" — A-001…A-004.
