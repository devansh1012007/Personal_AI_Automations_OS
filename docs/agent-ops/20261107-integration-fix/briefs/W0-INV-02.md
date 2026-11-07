# Brief — W0-INV-02 (INV) · wave 0 · run 20261107-integration-fix

## 1. Role and mission
Mission: verify plan items P-003 (non-ASCII parity), P-007 storage half (queue `workers:alloc`), P-009 storage half (schema/migration), P-012 (edge-count closure) against live code at HEAD `88e648b`. Why: schema/parity/queue contracts are hotspots — wrong write-sets break the parity lock or collide with INV-01.
Your role: investigator (READ-ONLY on source; you edit only your report file).

## 2. Required reading (in this order)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md` (binding)
2. `AGENTS.md` (parity lock: both SQL files same commit; LIKE-helper rule; lazy extras)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md`
4. Plan: `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — items P03, P07 (queue half), P09 (DDL/migration half), P12 verbatim + tests T03/T13
5. Template: `docs/agent-ops/20261107-integration-fix/templates/report-investigation.md`
6. Code: `src/paa/storage/relational/{like.py,schema_sqlite.sql,schema_postgres.py→.sql,migrations.py,database.py}`, `src/paa/storage/queue/base.py`, `src/paa/storage/graph/base.py`, `tests/storage/test_schema_parity.py`, `tests/storage/test_entity_parity.py`, `tests/storage/test_like.py`, ADR-0029

## 3. Task and acceptance criteria
Task T-002: classify P-003, P-007(queue), P-009(DDL), P-012 with evidence.
Specifically verify:
- P03: where does the non-ASCII divergence actually occur (ADR-0029 `ensure_ascii=True` + engine `lower()`)? Which exact query paths hit it? What would "one normalization point" mean at each call site (grep LIKE usage of user text)?
- P09 storage: `hot_serving_active_facts` current DDL in BOTH files; `Migration`/`MigrationStep` shape at migrations.py:399-453, current `SCHEMA_VERSION` tail; how a new step bumps it; what `test_schema_parity.py` parses (would a `tier` column pass it?).
- P07 queue: `workers:alloc` stream semantics — claim/ack API, idempotency keys available (events.py:68-102 ADR-0008 full key), who publishes/consumes today (grep `workers:alloc`).
- P12: confirm `EDGE_TYPES` = 12 = RFC §7.3 (11) + MUTATES; check `kuzu_backend.py` DDL + `schema_sqlite.sql` CHECK stay in sync; what would ADR-0043 enumerate (list the exact 12 + RFC citations).
Acceptance: report at `reports/w0-investigation/W0-INV-02.md`, ORCH-DIGEST first, every claim tagged.

## 4. Ownership
WRITE-SET: `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-02.md` only.
READ-ONLY: all source.
FORBIDDEN: src edits, COMMS edits beyond one printf append, commits.

## 5. Parallel siblings this wave
INV-01 (memory+replay half of P-009 — coordinate split), INV-03 (allocator queue consumer of `workers:alloc`), INV-04 (coder recall reads vector/relational), INV-05 (pyproject/CI). You own schema/migrations/queue/graph; INV-01 owns tier-policy/replay.

## 6. Skip list and known traps
- Phantom counts (21/9) already closed — P-012 is ADR+test only unless enumeration finds a real gap.
- `tests/storage/test_entity_parity.py::TestTheRecordedDivergence` currently PASSES while divergent — do not "fix" it to red without ADR-0029 amendment.
- Parity lock: any DDL claim must name BOTH files.

## 7. Environment and tests
Scoped: grep, git, targeted `pytest tests/storage/test_schema_parity.py tests/storage/test_like.py -q` (uses tmp_path; safe). No full suite, no migrations, no containers, no `PAA_TEST_POSTGRES_DSN` use.

## 8. Deliverables
Report: `reports/w0-investigation/W0-INV-02.md`. Final message ≤ 8 lines.
