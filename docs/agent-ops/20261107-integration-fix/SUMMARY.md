# SUMMARY — 20261107-integration-fix

## Objective
Integrate the partially- and non-integrated parts of Doc A (Attention Allocation) and Doc B (Core Architecture RFC v4.0/v4.1) into `src/paa`, per approved plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` rev5, executed via the wave-orchestrator workflow (5 investigation agents → 5 implementation waves with per-wave REV/TREV review → final review).

## Outcome (pending FINAL-REV verdict — see §Verification)
All 14 plan items (P01..P14) implemented; 9 ADRs filed (0036–0044, including the ADR-0029 amendment); full gate (ruff/mypy/docs-lock) green at the run's baseline discipline; full-suite failure list reconciled against the pre-existing baseline.

## What changed per item
| Item | What shipped (one line each — full evidence in the wave reports) |
|---|---|
| P-001 | Redacted 2 unredacted `str(exc)` sites (runtime.py:481, cli/main.py:649) + 200-char best-effort bound; T01. |
| P-002 | `_semantic` hydrates + derives confidence live; payload confidence is provenance-only; T02. |
| P-003 | Stored normalisation columns (canonical/aliases) + Python backfill v6; T03 (SQLite halves green; PG gated). |
| P-004 | `memory/touch.py` single helper (4 sites); coder vector fallback with `Runtime.embedder`; T04/T05. |
| P-005 | `Dockerfile.embed` (pinned `paa/runtime:4.1`) + CI embed job (ADR-0042); T06 test written, docker build NOT-RUN (CI-only). |
| P-006 | 5 nullable Snapshot fields + dashboard sub-panels + additive `/health`; T07 (attention_top honest-null until P-007 fill). |
| P-007 | `AttentionAllocator` claim-score-emit-ack on `workers:alloc` + `ATTENTION_ALLOCATED`/`DRAIN_THROTTLED` + drain throttle; T08/T15. |
| P-008 | `total_memory_cost` pure + curator ANALYZE advisory suggestion (never deletes); T09. |
| P-009 | T0–T4 tier layer: columns + v5 T2 backfill + vocab/fold + sweeper (event-before-move, dry-run); T10/T10b. |
| P-010 | Dual vault 15 files + `vault/governance/` + fence conflict error + reflection single-writer; T11. |
| P-011 | Pilot `coding` specialist via `mediate_delegation` + resumable `STATE_ROLLBACK_TRIGGERED` block emit; T12. |
| P-012 | Edge-count phantom closure (ADR-0043 + T13 pin). No code change. |
| P-013 | Daemon weekly reflection loop + file last-run + `/doctor` age row (+ TUI mirror); T14. |
| P-014 | ADRs 0036–0044 + 8 Settings sub-models + T13. |

## Verification (fillable — see FINAL-REV report)
- Commands run + actual results: (full suite vs baseline-14 table in `reports/final-review/FINAL-REV.md`; per-wave gates in `01-work-ledger.md`).
- Review outcomes: REV-W{1..5}/TREV-W{1..5} verdicts + fix waves (REV REJECTs fixed and re-reviewed to APPROVE-WITH-CHANGES/APPROVE).

## Unverified / assumed (carried, not hidden)
- PostgreSQL + S3 live suites: gated, NOT-RUN (no DSN/servers on host) — CI executes.
- Docker build of `Dockerfile.embed` + in-image `sentence_transformers` import: NOT-RUN (no docker on host) — CI-only.
- R3 cold-lake snapshot restore drill before first non-dry-run tier tick (plan §K gate): operator/manual, still open.
- v5-shape STRICT wedge pre-repair (W4-FIX-01 NEED): pre-existing DBs in that exact shape need a guarded pre-repair step before v6 (new step + PG probe) — open.
- A5b real-embedder paraphrase criterion: CI-only (weights fixture deferred, not invented).
- Allocator producer: operator-fed by explicit decision (W5-IMPL-03) — if an ingress producer is later added it must respect the same payload contract.

## Decisions the operator should make
1. ADR-0029 amendment number 0044 — filed (W5-DOCS); confirm the amendment text matches intent.
2. R3 drill scheduling before enabling non-dry-run tier sweeps — the sweeper defaults `dry_run=True` and stays there until this passes.
3. v5 STRICT wedge: commission the pre-repair migration step (small guarded step) or accept the wedge for v5+corrupt SQLite DBs.
4. R-W5-006 allocator-settings threading (daemon gate vs global settings; same pre-existing pattern as tier sweeper) — minor, cosmetic split, fix when daemon/orchestrator next touched.
