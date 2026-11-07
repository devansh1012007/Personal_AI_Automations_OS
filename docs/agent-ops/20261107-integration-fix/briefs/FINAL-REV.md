# Brief — FINAL-REV (fresh reviewer) · final Review Step 8 · run 20261107-integration-fix

## 1. Role and mission
Full-task final review of the COMPLETE combined diff (all waves, fixes, and sweep together) against the original requirements. You are a fresh instance with no prior context on this run. Your job is to catch what per-wave reviews could not see: cross-task interactions, merge-conflict artifacts, leftover debug code, documentation accuracy, migrations rollback, and whether the tests prove correctness as a whole.

## 2. Required reading
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md` (read-only rules)
2. `docs/agent-ops/20261107-integration-fix/templates/review-code.md` (report template)
3. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` §§A–L (success criteria, P01..P14, T01..T15, risks RSK1-5, open RS2)
4. `docs/agent-ops/20261107-integration-fix/00-mission.md` (baseline: ruff 0 / mypy 3 / 14 pre-existing failures, hotspots, A-001..A-005)
5. `docs/agent-ops/20261107-integration-fix/01-work-ledger.md` (full task table + waves + conflicts C1–C7 + deferred/open items)
6. `docs/agent-ops/20261107-integration-fix/COMMS.md` (full log — alerts you of every NEED/CONFLICT/DEFERRED)
7. Per-wave review reports: `reports/w{1,2,3,4,5}-review/REV-W*.md` + `TREV-W*.md` (digest blocks at least; each wave's verdict + residuals)
8. ADRs `docs/adr/0036-*` … `0044-*` (titles + decision sections)

## 3. Task and acceptance criteria
Task FINAL-REV. Review range: `88e648b..HEAD` on branch `ops/20261107-integration-fix` (run base `88e648b` → HEAD at your review; get it via `git rev-parse HEAD`). Your verdict gates "done" — APPROVE only if no BLOCKER/MAJOR remains on the COMBINED diff (prior wave REJECTs were on sub-diffs; do not re-litigate closed wave findings unless the combined tree re-opens them).

Specifically verify (each with evidence):
- P-001..P-014 each actually present + working: `grep PLAN:integration-fix:P0` in src; no sub-task left at "callable only" without a tracked residual (known: allocator producer decision is operator-fed per W5-IMPL-03 — verify the statement exists and the loop is gated-off by default; NOT a blocker if stated).
- Cross-task interactions: drain throttle vs test_105 (C1 closed?) + `drain.max_per_second` consumption at all 5 MemoryCreator sites; allocator tick vs tier sweeper vs curator ordering; specialist emit before failure return; cost gate suggestion accumulation vs tier moves; attention_top uses allocator output (fresh); norm columns on all writers (graph) — is there any OTHER INSERT INTO hot_serving_entity_index writer left un-normalised? (grep).
- Merge artifacts: conflict markers, commented-out code, TODOs standing in for work, dead code introduced by fixes, `uv.lock` / scratch files in tree (`git status` clean except untracked run-folder? — any untracked scratch = finding).
- Full gate evidence: re-run `ruff check src tests`, `mypy src`, the docs lock, import-conventions lock, and the full `pytest tests/ -q` if feasible on this host (else a broad scoped set + explicit statement of what ran; if you run the full suite, compare the failure list id-by-id against the 00-mission baseline 14 and prove no NEW failure IDs — the 2 stale tests + casualties were updated; the suite should be at most the baseline-minus-fixed minus none).
- Migrations: v5 + v6 both present, parity lock green, SCHEMA_VERSION=6, rollback story per plan §K (flag off, columns ignored).
- Docs: ADR-0036..0044 all filed + indexed; run-folder docs accurate (spot-check ledger statuses vs git log SHAs); known open items (PG suites NOT-RUN, Docker build NOT-RUN, R3 dry-run gate, v5-STRICT wedge NEED, R-W5-006 threading, drain-window survivor, cap kept-set order) are all VISIBLE in the ledger/reports (hidden residue = finding).
- Test quality: no weakened/deleted test without justification (grep the diff for `-def test_` removals); every behavior change has a test; the 2 deliberately-updated stale tests + C1–C5 casualty updates carry justifications.

## 4. Ownership
WRITE-SET: your report file only (`docs/agent-ops/20261107-integration-fix/reports/final-review/FINAL-REV.md`). READ-ONLY: all source. DO NOT commit.

## 5. Deliverables
Report with ORCH-DIGEST (verdict APPROVE / APPROVE-WITH-CHANGES / REJECT; findings BLOCKER/MAJOR/MINOR/NIT; full-suite-vs-baseline table; per-P-item verdict table; claims on the run's success criteria from plan §A). Final message ≤8 lines: `STATUS | agent | tasks | report path | blockers`.
