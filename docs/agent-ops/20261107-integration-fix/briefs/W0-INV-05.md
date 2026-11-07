# Brief — W0-INV-05 (INV) · wave 0 · run 20261107-integration-fix

## 1. Role and mission
Mission: verify plan items P-005 (derived embed image), P-013 (weekly reflection wiring), P-014 (ADRs 0036–0043 + Settings surface) against live code at HEAD `88e648b`. Why: build/CI/config are cross-cutting — wrong assumptions here break the gate or the ADR numbering.
Your role: investigator (READ-ONLY on source; you edit only your report file).

## 2. Required reading (in this order)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md` (binding)
2. `AGENTS.md` (Docker network-retry convention, lazy extras, `PAA_` Settings conventions, ADR index)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md`
4. Plan: `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — items P05, P13, P14 verbatim + tests T06/T14 + §K rollout flags
5. Template: `templates/report-investigation.md`
6. Code: `Dockerfile` (:9-18 wheelhouse, :27-30 base), `docker-compose.yml` (:67 image tag), `pyproject.toml` (extras, markers :97-99), `.github/workflows/ci.yml`, `src/paa/improvement/reflection.py` (:101,:169), `src/paa/config.py` (Settings classes), `docs/adr/README.md` (index through 0035), `docs/adr/0030-deployment-image-contract.md` (existing image contract — P05 must not contradict it)

## 3. Task and acceptance criteria
Task T-005: classify P-005, P-013, P-014 with evidence.
Specifically verify:
- P05: current Dockerfile stage layout (wheelhouse→builder→runtime) — where exactly does `FROM paa/runtime:4.1` fit as a derived image; does `docker-compose.yml:67` really pin `paa/runtime:4.1`; what does ADR-0030 (image contract) already say about extras/images (P05 must amend, not contradict); `embeddings` extra contents in pyproject (:40-42 area); CI matrix install shapes (minimal/dev/all) — where would an embed-image build job slot in; `sentence_transformers` import guard pattern (how do optional imports degrade today — grep `_try_`).
- P13: `ReflectionEngine.run_weekly` — is there ANY scheduler/cron wiring today (grep `run_weekly` callers, scheduler modules, OS cron docs)? What does `/doctor` (cli) currently check — where would `reflection.last_run_age_days` hook in?
- P14: confirm ADR sequence 0036–0043 free (glob docs/adr, README index); Settings pattern in config.py (class shape, `PAA_` prefix, env nesting) — list where each new flag (`allocator.enabled`, `allocator.max_results`, `tier.dry_run`, `cost_gate.enabled/threshold`, `vault_sync.enabled`, `specialists.coding.enabled`, `reflection.enabled`, `drain.max_per_second`) would be declared per convention; any Settings tests to extend (grep `Settings` in tests/config).
Acceptance: report at `reports/w0-investigation/W0-INV-05.md`, ORCH-DIGEST first, claims tagged.

## 4. Ownership
WRITE-SET: `reports/w0-investigation/W0-INV-05.md` only. READ-ONLY: all source. FORBIDDEN: src edits, Dockerfile edits, commits, COMMS beyond one printf append.

## 5. Parallel siblings this wave
INV-01..04 consume your Settings/ADR findings (flags they need). You only report; P14's actual config.py edits belong to a later wave's single writer.

## 6. Skip list and known traps
- `paa[minio]` alias / `storage/coldlake/minio_backend.py` naming — deliberate per ADR-0026, not a finding.
- Docker build network-fragility convention is mandatory (retry loops) — P05 must follow it; note any deviation in plan vs Dockerfile reality.
- `docs/agent-ops/` and `docs/plans/` are excluded from `test_docs_references.py` path:line checks — but your report is in agent-ops, fine.

## 7. Environment and tests
Scoped: grep, git, `pytest tests/config tests/development -q` if present. Do NOT run `docker build` (RS2: Docker availability unknown — record as UNKNOWN if you cannot verify read-only). No full suite.

## 8. Deliverables
Report: `reports/w0-investigation/W0-INV-05.md`. Final message ≤ 8 lines.
