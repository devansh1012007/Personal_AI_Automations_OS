# Brief — W2-FIX-01 (IMPL) · wave 2 fix · run 20261107-integration-fix

## 1. Role and mission
Fix REV-W2 BLOCKER R-W2-002 (Dockerfile.embed USER — cannot build) + MAJOR R-W2-003 (fold accepts invalid tiers) + MINORs R-W2-004/005/008 and TREV-W2 MAJORs R-W2-101/102 + MINORs R-W2-103/104/106/107 + write the deferred T06 test from W2-IMPL-04's NEED (R-W2-007, test half only). Why: USER bug makes the embed image unbuildable; fold/DB tier vocab disagreement breaks T10b's rebuilt==live guarantee.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (zero-baseline; runtime contract import; `class X(str,enum.Enum)`; per-line S608 only)
3. `docs/agent-ops/20261107-integration-fix/reports/w2-review/REV-W2.md` §§4–5 (R-W2-002/003/004/005/007/008 + §2 false-claim notes), §9 (what can't be verified here)
4. `docs/agent-ops/20261107-integration-fix/reports/w2-review/TREV-W2.md` §§5–6 (R-W2-101/102/103/104/105/106/107 + P1/P2 sketches)
5. `docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-IMPL-04.md` §2 (exact T06 test content delivered) + COMMS NEED line
6. Code: `Dockerfile.embed` + `Dockerfile:246,249` (USER chain proof), `src/paa/ledger/replay.py` fold (~:294-345) + `src/paa/memory/domains.py` `TierPolicy.resolve`, `src/paa/memory/__init__.py`, `tests/memory/test_touch_unification.py`, `tests/storage/test_tier_migration_p009.py` docstring, `tests/storage/test_vector.py` (requires_embeddings site + marker), `tests/ledger/test_tier_replay_p009.py` (discriminator tests), `.github/workflows/ci.yml` embed header comment.

## 3. Task and acceptance criteria
Task W2-FIX-01. All items:
- **R-W2-002 (BLOCKER, root cause)**: `Dockerfile.embed` — add `USER root` before the install RUN, `USER paa` after it (runtime stage must never ship as root), with a comment citing the inherited-user trap (base sets `USER paa` over root-owned `/opt/venv` — the exact chain REV proved). No other Dockerfile changes. Docker build remains NOT-RUN here (absent); state so.
- **R-W2-003 (MAJOR)**: `apply_tier_event` — validate `to_tier` against the canonical set (share `TierPolicy.resolve` or a single `VALID_TIERS` constant — domains.py has no paa imports, cycle-safe; replay.py must not gain a circular import), fall back to T2 on invalid (incl. `"T9"`, `"not-a-tier"`, empty). Empty/non-scalar `fact_id` → no-op (treat falsy fact_ids as absent per R-W2-103 — do NOT special-case silently; totality = never raise). Add tests: `T9`/garbage/empty `to_tier` → T2; `""`/0/False `fact_id` → no-op. (Third touched test file for these: wherever the fold hostile tests live — follow the existing layout; W2-IMPL-02 split vocab/replay tests, you add to the hostile/fold files.)
- **R-W2-005**: export `TierPolicy` (+ `DEFAULT_TIER` if namespaced there) from `src/paa/memory/__init__.py` mirroring `DomainPolicy` (verify `from paa.memory import TierPolicy` works after).
- **R-W2-104**: pin TierPolicy defensive properties (ctor rejects `DEFAULT_TIER=`, per-instance dict isolation) — 3 small tests in the vocab file.
- **R-W2-101 (MAJOR)**: extend touch-unification AST walk in `tests/memory/test_touch_unification.py` to assert each of the 4 consumer modules (`api/app.py`, `cli/main.py`, `coder/runtime_tools.py`, `agents/context_agents.py`) imports `touch_recalled` from `paa.memory.touch` AND calls it (or assert `DecaySweeper.touch_many` has exactly one caller in `src/paa` outside `decay.py` — implementer's choice, record it).
- **R-W2-102 (MAJOR)**: extend touch failure test — secret-shaped exception (`sk-ABCDEF1234567890`), capture logs (`structlog.testing.capture_logs` pattern from W2-GEN-01), assert `***REDACTED***` + token absent in `memory.recall_touch_failed`.
- **R-W2-105**: rename/comment the discriminator tests as W3 placeholders (they pin `compute_idempotency_key`, not a construction site — say so in the docstring; do NOT delete). One-line-comment fix in the tier_replay test file.
- **R-W2-106**: add ONE test re-executing the v5 step statements against a v5 DB via the runner's step API (guard-level idempotency), or — if the step API makes it impractical in tmp fixtures — document the runner-vs-guard honesty in the migration test file docstring (implementer's proven choice, record which).
- **R-W2-107**: new `tests/deployment/test_dockerfile_embed.py` — static lock on `Dockerfile.embed` content: `FROM paa/runtime:4.1` exact pin, retry loop present, `PIP_RETRIES`/`PIP_TIMEOUT`, `pip check` last, no `--no-index` in a RUN line, `USER root` before install + `USER paa` after (pins R-W2-002). File-read tests via `tmp_path`/repo-root path pattern (follow `test_compose_config.py` for how it locates repo files).
- **R-W2-007 test half**: write the T06 `requires_embeddings` test from W2-IMPL-04 report §2 into `tests/storage/test_vector.py` (APPEND the one function only — verify minimal install import safety: the test must skip if the extra is absent, following the file's existing `importorskip` pattern; never import sentence_transformers at module scope). It will SKIP on this host (no extra/DSN/image) — record skip honestly. Do NOT attempt the A5b weights fixture here (deferred — see §6).
- **R-W2-008**: reword the ci.yml embed header comment ("once into the base image's wheelhouse" → base build + derived torch install, Dockerfile:65 excludes embeddings).
- **R-W2-004**: correct the inaccurate docstring in `migrations.py` (`_tier_step` PG data guard is unqualified vs schema-qualified statement; single-schema correct — fix the WORDING only, no behavior change).
- Trace: keep existing `PLAN:integration-fix:P00x` tags; no new tags.
- Acceptance: your touched/suite tests green (touch + tier vocab/replay + migration + deployment + semantic), ruff 0, mypy total 3, `from paa.memory import TierPolicy` works, Dockerfile.embed grep shows USER root/USER paa, docs-lock unaffected by you (docs are W2-FIX-DOCS's file — you edit no docs/** at all).

## 4. Ownership
WRITE-SET (only these): `Dockerfile.embed`, `.github/workflows/ci.yml`, `src/paa/ledger/replay.py`, `src/paa/memory/__init__.py`, `src/paa/storage/relational/migrations.py` (docstring only), `tests/memory/test_touch_unification.py`, `tests/memory/test_tier_vocab_p009.py`, `tests/ledger/test_tier_replay_p009.py`, `tests/storage/test_tier_migration_p009.py` (docstring or guard test), `tests/storage/test_vector.py` (append T06 function only), `tests/deployment/test_dockerfile_embed.py` (new). READ-ONLY: everything else. FORBIDDEN: `docs/**` (W2-FIX-DOCS owns docs this fix wave), `Dockerfile`, `docker-compose.yml`.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: post COMMS CLAIMs for shared hotspot files (`replay.py`, `migrations.py`) before editing.

## 5. Parallel siblings this fix wave
W2-FIX-DOCS (docs re-point — disjoint: you edit zero docs). If your line shifts re-break docs citations, W2-FIX-DOCS's final lock re-run covers it (coordinate via its report's residual list: run the lock only as a check, do not fix docs yourself).

## 6. Skip list and known traps
- R-W2-006 (TierPolicy mutable/unhashable) = DO LATER — do not refactor here (documented in REV as pre-W3 non-blocking). R-W2-009/010/011/012 NITs = accept + note.
- A5b weights fixture + CI in-container pytest (R-W2-007 remainder) deferred to W3 — record as deferred with the exact open question, do not invent a fixture.
- `Dockerfile.embed` USER fix cannot be build-proven here (docker absent) — reviewer prediction + CI proof. State the verification limit honestly.
- PG migration path stays NOT-RUN locally (no DSN) — CI only, unchanged by you.
- Do not touch `docs/adr/0042` (wording supersede would be a new ADR decision — the Dockerfile comment + ADR together are the record).

## 7. Environment and tests
Scoped only: your test files + `tests/storage/test_schema_parity.py` + ruff/mypy scoped + docker-absence probe + yaml parse. No full suite, no docker build, no shared-DB migration.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: W2-FIX-01`, `Agent: W2-FIX-01`, `Wave: W2-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w2-review/W2-FIX-01.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w2-review/W2-FIX-01.md` (create at start, ORCH-DIGEST first, append as you work; long-run test answers). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
