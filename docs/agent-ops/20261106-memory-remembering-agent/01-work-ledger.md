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

## Synthesis (Phase 2, from Wave-0 digests + spot-checks)

- A-002 CONFIRMED: no deploy cron/systemd schedules `MemoryCurator` (compose/Dockerfile/deploy/CI/docs grep) — C04 daemon wiring carries no double-run risk.
- A-001 HALF-VERIFIED: sink/pending API present, but **payload-key mismatch** between `VectorEmbeddingSink.enqueue` (creator.py:842-848: entity_id/predicate/memory_domain/memory_scope/statement) and `context_agents._semantic` (251-262: reads object_value/confidence/importance) → semantic hits would have empty content. Added as explicit C01b subtask within T-001 (write object_value/confidence/importance, or read statement — implementer picks one, round-trip test required). Orchestrator spot-checked both sides.
- C03 mechanism nuance: retention effect flows via `last_queried_at → effective_confidence`, not `importance_index` (0 production callers; ranking uses column). Touch still correct; rationale corrected.
- C06 floor conflict: global 0.15 vs per-domain `prune_floor` (0.10–0.40). Do NOT blanket-override; implementer must pick and document: global floor applies only when explicitly set (`model_fields_set`), else per-domain (INV-02 recommendation). Check `hot_serving_memory_registry.decay_lambda` seed before touching config.
- C05 protocol: keep `FactExtractor` protocol-compatible; counters must not break `LLMFactExtractor`. Check worker narrative boundary before touching in context agents (read-only invariant) — touch insertion uses fact ids only, never alters content.
- T08 RE-SCOPED (operator decision): planner `_semantic` + direct vector search prove paraphrase recall; coder `recall_facts` stays keyword-only, documented. No coder-side vector wiring.
- `use_count` not rebuildable from cold lake on hot replay (derived retention state) — document, not a blocker.

## Waves

- Wave 0 — Investigation (read-only, 3 investigators). Status: DONE (3 reports, all DONE, confidence high).
- Wave 1 — T-001+T-002 (`runtime.py`, `memory/creator.py` incl. C01b payload alignment). Status: CLOSED. Code c5766c8 + improve e966b68 + docs-fix 5b8d6ee. Reviews: REV APPROVE-WITH-CHANGES (1 MAJOR), TREV APPROVE-WITH-CHANGES (3 MAJOR) → improver FIXED 13/REJECTED 2/DEFERRED 1 (+R-W1-DOCS fix 17 citations) → delta APPROVE (0 BLOCKER/MAJOR). Gate gate-w1-2 green vs baseline. Operator docs commits f8c12a6/aee3810 on-branch during run (docs only, no source conflict); VISION.md has uncommitted operator edit at wave close (left untouched).
- Wave 2 — T-003+T-004 (recall paths + `daemon.py`). Status: CLOSED. Code 3506e30 T-003 + 70dd91f T-004 + improve 2c72f96 + docs-fix 1541e0b. Reviews: REV APPROVE-WITH-CHANGES (2 MAJOR), TREV APPROVE-WITH-CHANGES (3 MAJOR) → improver FIXED 10/DEFERRED 4 → delta APPROVE-WITH-CHANGES (0 BLOCKER/MAJOR, 2 MINOR: R-W2-D-01 fallback bypasses preview on construction failure, R-W2-D-02 preview flag consumed by partial preview — DEFERRED to Wave 4). Gate gate-w2-2 green vs baseline. Operator README/VISION commits 90e1aae/88124f2 on-branch (docs only).
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
