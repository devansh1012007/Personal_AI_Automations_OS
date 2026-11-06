# Work ledger — 20261106-memory-remembering-agent (source of truth)

## Plan coverage

| Plan item | Summary | Disposition |
|---|---|---|
| C01 | Wire `VectorEmbeddingSink` in runtime | Task T-001 (Wave 1) |
| C02 | `drain_pending_embeddings` + 3 drain callers | Task T-002 (Wave 1, same file as T-001 — merged into Wave 1 single writer) |
| C03 | Touch on recall (5 paths) | Task T-003 (Wave 2) |
| C04 | Schedule curator in daemon | Task T-004 (Wave 2, daemon shares with T-002 callers — sequence after Wave 1) |
| C05 | Extractor counters + pronoun mapping | Task T-005 (Wave 3) |
| C06 | `prune_confidence_floor` wiring + `decay_lambda` deprecation | Task T-006 (Wave 3, config.py single writer) |
| C07 | E2E + observability tests/logs | Task T-007 (Wave 4, after C01–C06) |
| T01–T08 | Tests | mapped per task above |

## Waves

- Wave 0 — Investigation (read-only, 3 investigators). Status: pending.
- Wave 1 — T-001+T-002 (`runtime.py`, `memory/creator.py`). Status: pending.
- Wave 2 — T-003+T-004 (recall paths + `daemon.py`). Status: pending.
- Wave 3 — T-005+T-006 (`creator.py` extractor + `config.py`/`decay.py`). Status: pending.
- Wave 4 — T-007 e2e + observability. Status: pending.

## Conflicts

- C01/C02/C05 all touch `memory/creator.py` → sequenced across waves (W1, W1, W3), never parallel.
- C02/C04 both touch `daemon.py` → W1 then W2.
- `config.py` hotspot → only T-006 writes it (W3).

## Deferred / open items

- Q1 (MemoryCurator deploy cron): Wave-0 grep.
- R1 precision check, R3 cold-lake snapshot: before C04 enablement.
- Pre-existing Windows failures (mypy 3, pytest 12) stay untouched; final gate compared vs baseline.
