# Gate — wave 4 run 1 — 2026-11-07

- Tree state: COMMS.md modified · HEAD: 326b16d (T-007) + docs 29463e5 · Baseline: see 00-mission.md

| Step | Command | Result | vs baseline |
|---|---|---|---|
| Lint | `ruff check src tests` | All checks passed! | same (0 findings) |
| Typecheck | `mypy src` | 3 errors | identical to baseline (win32 pre-existing) |
| Scoped memory + docs refs | `pytest tests/memory/ tests/daemon/ tests/conventions/test_docs_references.py -q` | 3 failures (below) | **NEW — Wave 4 regression (stale tests)** |

New failures (all in `tests/daemon/test_maintenance_loop.py` — tests encode pre-Wave-4 behavior; code behavior is the new approved contract):
- `test_maintenance_tick_falls_back_to_sweep_when_curator_construction_fails`: asserts first-tick fallback deletes (remaining==0); now correctly previews (dry_run, remaining==1) per R-W2-D-01.
- `test_import_failure_falls_back_to_sweep`: same first-tick-delete expectation; now previews.
- `test_orphan_purge_skips_when_entity_index_empty`: asserts empty-index orphans are skipped; Wave 3 provenance refinement (R-W3-REG1) means they ARE swept (test_curator empty-index test passes). Duplicate coverage contradicting refined semantics.
Triage: code is the approved design; stale daemon tests must be updated (second-tick assert for fallback tests; align/remove duplicate orphan test) — routed to Wave 4 improver. Proceed to review, then improver.
