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
| — | the records:

## Tasks

| Task | Plan items | Domain | Class | Root cause (1 line) | Write-set | Contract-set | Env-set | Depends on | Conflicts | Wave | Agent | Status | Commits | Report |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| T-001 | W7-b1, W7-b2 | storage/coldlake | **DEEP** | The blob substrate translates nothing, so a driver's exception crosses the boundary and the contract is silent about failure | `src/paa/storage/coldlake/{errors,minio_backend,base,cas,__init__}.py`, `tests/storage/{test_blob_errors,test_minio_backend,test_blobstore_contract}.py`, `docs/adr/0032-*.md` | the `BlobStore` protocol's error clause; the exception types the S3/CAS stores raise | none | — | C2 with T-002 (both touch the cold lake) | 1 | W1-IMPL-01 | **DONE** (T-001) — ce51df8, ddef4cc | — | `reports/w1-impl/W1-IMPL-01-T-001.md` |
| T-003 | W6-b4 | deploy | SURFACE | The script reads one compose file and the parity test hard-codes the service set, so a merged stack is invisible to both | `deploy/fetch-images.sh`, `tests/deployment/test_fetch_images.py`, `docker-compose*.yml`, `docs/adr/0030-*.md`, `docs/deployment/docker.md`, `.env.example` | the script's CLI and its env surface (`PAA_COMPOSE_FILE`, `PAA_FETCH_*`) | none | — | C1 with T-001 on `docs/deployment/docker.md` → T-001 does not touch it | 1 | W1-IMPL-02 | **DONE** (T-003) — 954705d, 257b80f, 7532c9c | — | `reports/w1-impl/W1-IMPL-02-T-003.md` |
| T-004 | W7-b3 | core imports | SURFACE | One commit left eight siblings expressing the same dependency two different ways | `src/paa/memory/{contradiction,decay,curator}.py`, `src/paa/marketplace/installer.py`, `src/paa/skills/registry.py`, `src/paa/agents/{policy,context_agents}.py`, `tests/conventions/test_import_conventions.py` | the import graph of eight modules | none | — | C1 with T-001 on `coldlake/__init__.py` → T-004 does not touch it | 1 | W1-IMPL-03 | **DONE** (T-004) — 7747fa0, a3e6819 | — | `reports/w1-impl/W1-IMPL-03-T-004.md` |
| T-002 | W6-b3 | storage + deploy | SURFACE | The S3 path is only ever tested against a stub, so the six raw-escape sites were invisible | `tests/storage/test_s3_live.py`, `tests/storage/conftest.py`, `pyproject.toml`, `docs/adr/0033-*.md` | the `PAA_TEST_S3_*` convention; the `requires_s3` marker | SeaweedFS + RustFS containers | T-001 | C2 with T-001 (the error names it asserts) | 2 | W2-IMPL-01 | **DONE** — be649ad, 4e0c967 | ADR-0033, amended by W2-IMPR-05 | `reports/w2-impl/W2-IMPL-01-T-002.md` |
| T-005 | W9-0.1 | validation | SURFACE | The security layer's docstrings are unproven | `src/paa/validation/schema_validator.py`, `tests/validation/*` | the malformed-keyword policy | none | — | — | 2 | W2-IMPL-02 | **DONE** — 7ca81c8, cedf3c7 (+ F-2/F-3: 4a5bac0, 77195f7, d484c04, 2bbacf5, 01c1484) | one keyword table, all three validators | `reports/w2-impl/W2-IMPL-02-T-005.md` |
| T-006 | W9-0.2 | agents | SURFACE | The exact event sequence and the mediated cycle are unasserted; `HARD_STOP` does not park | `src/paa/agents/orchestrator.py`, `tests/agents/*`, `tests/integration/test_end_to_end.py` | the `HARD_STOP` behaviour (D-4) | none | — | — | 2 | W2-IMPL-03 | **DONE** — 8527fe7, 487c6d5, c504dd6 (+ F-1: c3e14fd, 7ef532c; + R-W2-201: ab3f386, 78cce0b, d8726db, bf13357) | ADR-0034, amended by W2-IMPR-01 | `reports/w2-impl/W2-IMPL-03-T-006.md` |
| T-007 | W9-0.3 | models | SURFACE | The key leaks on the escalation path; the semaphore and the LOCKDOWN spy are unasserted | `src/paa/models/{router,base}.py`, `tests/models/*` | the `MODEL_ESCALATED` discriminator scope | none | — | — | 2 | W2-IMPL-04 | **DONE** — 514cfad, b2a21a8, 14cd23a, 0e60264 (+ R-W2-203/F-4/F-7: 8841216, 39aba5c, 9674a81, d36bb7a) | the key-secrecy claim | `reports/w2-impl/W2-IMPL-04-T-007.md` |
| T-008 | W9-0.4 | observability | SURFACE | The percentiles are labelled but not valued; `logging.py` has zero coverage | `src/paa/observability/{runs,logging}.py`, `tests/observability/*` | none | none | — | — | 2 | W2-IMPL-05 | **DONE** — 4808982, 9a92f19, 6117c4a, 377c160, efced61 | the `TYPE_CHECKING` deferral, fulfilled | `reports/w2-impl/W2-IMPL-05-T-008.md` |
| T-011 | W7-new | marketplace | SURFACE | The remote registry chooses the local exception type | `src/paa/marketplace/registry_client.py`, `tests/marketplace/*` | the `HttpRegistry` error behaviour | none | — | — | 3 | W3-IMPL-03 | **DONE** — 01b5adb, efd18f6, e214428 (+ `__init__.py` export per report §12) | the one error type, one bounded text | `reports/w3-impl/W3-IMPL-03-T-011.md` |
| T-010 | W7-b4 | docs | SURFACE | The line-number references rotted and nothing checks them | `docs/**` (named files), `README.md`, `docs/adr/0019-*.md`, `tests/conventions/test_docs_references.py` | none | none | all code tasks | — | 3 | W3-IMPL-02 | **DONE** — 3c500de, a949bbf | the checker is a lock, not a report | `reports/w3-impl/W3-IMPL-02-T-010.md` |
| T-009 | W9-0.5 | ci | SURFACE | Nothing enforces the gate on a push | `.github/workflows/*.yml` | the CI contract | a PostgreSQL service container | all code tasks | — | 3 | W3-IMPL-01 | **DONE** — dad08e4, 07baff0 (workflow run UNVERIFIED: no GitHub/Docker here) | gate on every push, `. [dev]`, `python-is-python3` | `reports/w3-impl/W3-IMPL-01-T-009.md` |
| T-012 | — | records | lead | the track document and the index still describe work that is done | `docs/adr/README.md`, `docs/MCP_AND_PLUGINS.md`, `AGENTS.md`, `docs/ROADMAP.md` | none | none | all | — | 3 | ORCH | **DONE** — eb849ff, f54e7cf | ADR index rows for 0032–0035 added by the orchestrator | `reports/w3-impl/W3-IMPL-04-T-012.md` |

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
* **Gate results:** `gates/gate-w1-1.md` → `gates/gate-w1-2.md` (final: ruff 0, mypy 0, **2139 passed / 233 skipped**)
* **Review findings → dispositions:** round 1 — **1 BLOCKER, 11 MAJOR, 20 MINOR, 3 NIT**; round 2 (delta) — **0 BLOCKER, 1 MAJOR, 9 MINOR**. **Every BLOCKER and MAJOR is FIXED.** R-W1-216 (second half) and R-W1-112 are DEFERRED with reasons; both were later closed by `W1-IMPR-05` (the timeout) and by the plan correction (the classification).
* **Review/improve cycles used:** **3/2** — the third was a repair of a finding the second cycle *claimed* to fix and had not (R-W1-301). The orchestrator reproduced the open hole and the fix; the transcript is in `gates/gate-w1-2.md`.
* **Status:** **CLOSED** — green, reviewed, improved, and the records corrected (the plan's `TYPE_CHECKING` figures were wrong and are now right).

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

## Open items (carried, not closed)

Wave 2's delta review (`reports/w2-review/W2-REV-03-delta.md`) closed all five
MAJORs and left these. None blocks the wave; each needs a decision or a
later task, and none may be quietly forgotten.

*W2-FIX-03 (wave 2, last round) closed six of the eight. What is left is
below, and the closure list is under the table.*

| # | Item | Where | Why it is open |
|---|---|---|---|
| 2 | The **unreachable egress check** in `_check_lockdown` | `src/paa/agents/policy.py` | W2-FIX-02 deliberately did not patch it, and W2-FIX-03 assessed it and **did not patch it either**: making it reachable changes *which* refusal the ledger records, and no prose, ADR or name says which of the two should win. The set of refused plans does **not** change either way. W2-FIX-03's report §6 has the three candidate changes with their consequences and a recommendation: **leave the code alone and let the operator choose between reordering the checks (the air-gap refusal answers, and the `POLICY_BLOCKED` payload's `reason` changes for a LOCKDOWN plan) and deleting the dead half (behaviour-preserving, and the reason never changes)**. It is a judgement about what the policy should *say*, not a defect |
| 4 | Four **pattern-semantics findings** that survive ADR-0035: `git push -f` (the pattern wants `--force`), `rm -rfv` / `rm --recursive --force` (the flag spellings), `DELETE FROM public.users` (the table name is not one word) and `TRUNCATE events` without `TABLE` — all **allowed** | `src/paa/agents/policy.py` | each **widens** a security rule over prose, so each is a policy decision, carried forward with reproductions and unpatched. ADR-0035 closed the *fifth* of the five — the delete rule's **position dependence** — because that one was an anchor that did not match the renderer's own separator, not a rule anybody wrote |
| 9 | The **"24 silently dropped keywords"** summary line | `docs/agent-ops/20261022-w6-w7-w9/briefs/W2-REV-03.md:111` (the W2-REV-03 **brief**) — *not* in `reports/w2-improve/W2-IMPR-02-F-2-F-3.md`, which never carried it | the figure is the **brief's**, and the delta review already said so (`reports/w2-review/W2-REV-03-delta.md:408`); the report's own §3.3 table now carries the counts (6 `TypeError` + 1 `AttributeError` + 19 ignored + 1 wrong answer = 27). The brief is the orchestrator's file; fixed in commit 04b7479 — its summary line now matches the matrix (6 `TypeError` + 1 `AttributeError` + 19 ignored + 1 wrong answer = 27). |

### Closed in wave 2's last round (W2-FIX-03)

| # | Item | What closed it |
|---|---|---|
| 1 | The **SAFE-mode hole** — `DELETE FROM users` allowed unless it was the last step of the plan | **ADR-0035**: the delete pattern's terminator set is now the renderer's own (`;`, a newline, or the end of the text), so the rule's answer no longer depends on the step's *position*. A refusal that did not happen now happens, and the ADR states the cost and the rollback |
| 3 | The **Anthropic override** did not apply the malformed-schema rule | one implementation of the rule (`paa.models.base.refuse_malformed_schema`), called by the base class **and** by the override; the strict xfail is gone, replaced by an assertion that no request was made |
| 5 | The docstring that said the air-gap branch "cannot fire" | corrected: the `_NET_PATTERNS` half fires (and the suite now asserts which half answered); only the declared-permission half is dead |
| 6 | One import edge quoted as 26 ms in one place and 19 ms in another | re-measured once (60 interleaved pairs of fresh interpreters): **about 20 ms**, quoted in the three places that mention it, and a lock now checks that they agree |
| 7 | A **human-granted clearance recorded no risk score** | the gate event carries the score the policy computed, and the clearance carries it through — with no score in the lineage, no score in the clearance |
| 8 | A report's **summary line** contradicting its body | the drift was in the W2-REV-03 **brief**, not in the report; the report's §3.3 table now carries the counts, and the brief is reported above |

## Out-of-plan discoveries

* The malformed JSON-Schema keyword value (reproduced) — folded into T-005.
* The marketplace's untrusted body (reproduced) — T-011.
* The API-key leak on the escalation path (reproduced live) — T-007.
* `HARD_STOP_ESCALATE_TO_USER` does not park the task — T-006 (D-4).

## Temporary mitigations (user-approved)

* None. No stopgap has been proposed, and none is approved.

## Assumptions

See `00-mission.md` §"The assumptions" — A-001…A-004.
