# Brief — W2-IMPL-04 (IMPL) · wave 2 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-005 (T-005) — derived `Dockerfile.embed` with pinned `paa/runtime:4.1` + CI embed job. Why: base image stays torch-free; embeddings need an isolated derived image. Docker is ABSENT on this host — the deliverable is the file + CI wiring + static verification; the docker build itself is NOT-RUN here by design (record honestly, CI does it).
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (image builds are network-fragile: every network op retries — apt `-o Acquire::Retries=10`, pip `PIP_RETRIES=10`/`PIP_TIMEOUT=120` + outer `retry()` loop, SHA after download; three-stage wheelhouse/builder/runtime rationale — do NOT "simplify" the builder to a networked install)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (RS2: Docker UNKNOWN on host — never claim a build ran)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P05 row + §H T06 + §K (embed image CI-built, not deployed by default)
5. ADR `docs/adr/0042-embeddings-derived-image.md` (READ FULLY — W1-IMPL-01 RESOLVED the plan-vs-code contradiction: derived image installs ONLINE with retries because the runtime stage has no wheelhouse; your Dockerfile must match the ADR, and where ADR and plan disagree the ADR wins — record any residual discrepancy as FINDING, do not edit the ADR)
6. Investigation: `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-05.md` §P-005 (Dockerfile:27-30 torch-free contract, wheelhouse/builder/runtime stages, `docker-compose.yml:67` pin, `pyproject.toml:42` embeddings extra, `requires_embeddings` marker) + §P-013; `W0-INV-01.md` §P-002/§P-004 (A5b criterion: gated paraphrase test on the embed image)
7. Template: implementation report ORCH-DIGEST first
8. Code (verify at HEAD): `Dockerfile` (all 3 stages + retry conventions), `docker-compose.yml:54-90` (image pin + build blocks), `pyproject.toml` embeddings extra, `.github/workflows/ci.yml` (matrix shapes, timeouts — INV-05 warned the `all` shape timeout already justifies torch download; scope the embed job per ADR-0042), `docs/adr/0030-deployment-image-contract.md` (image contract), `docs/adr/README.md` network-fragility note location.

## 3. Task and acceptance criteria
Task T-005-P005. Specifically:
- New `Dockerfile.embed` (repo root, beside `Dockerfile`): `FROM paa/runtime:4.1` (exact pin from `docker-compose.yml:67` — never `latest`; if the compose pin has moved since, a FINDING — the ADR-0042 pin is the contract unless the ADR file you read says otherwise). Contents: install the `embeddings` extra (`sentence-transformers>=3.3`) per ADR-0042's online-install-with-retries decision (outer retry loop + `PIP_RETRIES`/`PIP_TIMEOUT`), `pip check` last line, no compiler/cache leftovers beyond what the base runtime stage already is (you inherit it — do not re-stage). Every network op retried per AGENTS.md convention. Document in the file header: derived-only, base stays torch-free, pin provenance.
- CI: add the embed job to `.github/workflows/ci.yml` EXACTLY as ADR-0042 scopes it (INV-05: second torch download path — `all`-only or nightly scope if the ADR says so; if the ADR is silent, scope to the `all` shape and say why in your report). The job builds `Dockerfile.embed` and asserts `python -c "import sentence_transformers"` succeeds INSIDE the embed image (T06). Base-image assertion (hash fallback + degraded marker) already exists or gets a minimal additive assertion — do not restructure existing jobs.
- `pyproject.toml`: NO CHANGES expected (embeddings extra exists) — verify `requires_embeddings` marker wiring; if T06 needs a test marked `requires_embeddings`, add it as a scoped addition to the existing vector test file ONLY if the brief's write-set allows (see §4 — if it needs a file outside, post NEED and record T06 test as deferred-to-CI with the exact file+content in your report). Never add a marker without `--strict-markers` compliance (check the marker is registered in `pyproject.toml`).
- Base `Dockerfile` + `docker-compose.yml`: NO MODIFICATIONS (verify the pin textually; a mismatch is a FINDING). If CI needs the embed image name, define it once in CI yaml (not compose) unless ADR-0042 says otherwise.
- Verification (honest): `docker` absent on this host — RUN: `Get-Command docker` (record absence), Dockerfile static checks you CAN do (pin grep, retry-convention grep, yaml parse of ci.yml via python, `test_compose_config.py` green if compose untouched), `pytest tests/deployment/test_compose_config.py -q`. NOT-RUN (must state): `docker build -f Dockerfile.embed`, `python -c "import sentence_transformers"` inside the image — these run in CI only. Never claim T06 green locally.
- Trace comment `PLAN:integration-fix:P005` in Dockerfile.embed header.
- Acceptance: file exists with `FROM paa/runtime:4.1` exact; every network op in the file grep-matches the retry conventions; ci.yml parses + embed job present with ADR-0042 scope; compose_config tests green; report states NOT-RUN for the docker build with CI pointer.

## 4. Ownership
WRITE-SET (only these): `Dockerfile.embed` (new), `.github/workflows/ci.yml`. READ-ONLY: everything else (including `Dockerfile`, `docker-compose.yml`, `pyproject.toml`, `docs/adr/*`). A T06 test file needs a NEED post first (do not edit `tests/` — it is outside this write-set; if the ADR/plan requires a test, deliver its exact content in your report as deferred-for-CI and let the orchestrator schedule it). FORBIDDEN: editing base `Dockerfile`, compose, pyproject, ADRs.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory.

## 5. Parallel siblings this wave
W2-IMPL-01 (touch — disjoint), W2-IMPL-02 (tier vocab), W2-IMPL-03 (tier DDL), W2-GEN-01 (P-002 minor tests — test file only). CI yaml is yours alone this wave.

## 6. Skip list and known traps
- Do NOT copy the plan's offline `--no-index` claim into the derived image — ADR-0042 already resolved it (online + retries, no wheelhouse in the runtime stage). If your reading of ADR-0042 contradicts this summary, the ADR file wins — re-read it and report the discrepancy as FINDING rather than guessing.
- Do NOT push torch into the base image or add the embeddings extra to the base wheelhouse.
- Do NOT claim the docker build passed. Docker absent → NOT-RUN (needs CI) is the correct, reviewable answer.
- Do NOT restructure existing CI jobs to make room — additive embed job only.

## 7. Environment and tests
Scoped only: `Get-Command docker` (absence probe), yaml parse, `pytest tests/deployment/test_compose_config.py -q`, `ruff`/`mypy` unaffected (no src edits — run `ruff check` on nothing new; say so). No docker build, no containers, no full suite.
Commit protocol: code commit first, then report in a separate docs-only commit. Trailers `Task: T-005-P005`, `Agent: W2-IMPL-04`, `Wave: W2`, `Report: docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-IMPL-04.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-IMPL-04.md` (create at start, ORCH-DIGEST first, append as you work; long-run test answers: source (CI T06)/load (build cost)/hostile (corrupt download → retry, SHA)/operability (not deployed by default)/reversal (delete file)). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
