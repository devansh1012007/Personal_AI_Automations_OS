# Brief — W1-IMPL-03 (IMPL) · wave 1 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-002 (derive confidence at query time on the vector path; kill frozen-payload ranking, T02). Why: paraphrase recall ranks by a confidence snapshot frozen at ingest; every touch makes it staler; plan D13 forces query-time derivation (never stored).
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (relational contract resolved at runtime — import `RelationalBackend` at runtime, not under `TYPE_CHECKING`; LIMIT needs unique tie-break; LIKE via `like.py` if you write any LIKE — you should not need one)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (baseline failures A–D; `tests/memory/test_decay.py` must stay green unchanged)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P02 row verbatim + §H T02 + D13 decision (derive at query, never store; time basis `last_queried_at`)
5. Investigation (authoritative): `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-01.md` §P-002 — derivation primitive, hot path already derives (do NOT touch), vector path frozen, Fix A (hydrate in `_semantic`, batch), gatherer stays pure, payload `confidence` becomes provenance-only, unknown-domain passthrough, hydrate-failure degrade-to-payload-snapshot. Also §P-009 note: `_FACT_COLUMNS` must not auto-include tier; `Fact` contract stable; sweeper SELECT tier only. And `W0-INV-04.md` §P-004 last paragraph (P02 must land before P04 fallback; `context_agents.py` coordination, disjoint functions expected).
6. Template: implementation report with ORCH-DIGEST first (shape of `templates/report-investigation.md` + verification per `templates/gate-report.md`)
7. Code: `src/paa/memory/decay.py:54-65` (`effective_confidence`), `src/paa/memory/domains.py` (`domain_policy`), `src/paa/memory/facts.py:332-336` (`_FACT_COLUMNS`), `:1183-1224` (`_to_fact` derivation + unknown-domain passthrough), `src/paa/agents/context_agents.py:53,194-262` (`_hot_facts` derives — the pattern to copy; `_semantic` frozen — the code to fix), `src/paa/memory/creator.py:1073-1091` (payload writer), `src/paa/context/gatherer.py:103-152` (ContextElement — READ ONLY, no change), `tests/agents/` existing `_semantic` test pattern (glob for exact file name).

## 3. Task and acceptance criteria
Task T-002-P002. Specifically:
- In `ContextBuilder._semantic` (`context_agents.py`): after `vector_store.search`, batch-hydrate the hit fact rows (ONE batched query — prefer `FactRepository` reuse / single `WHERE id IN (...)` over N `get_fact` calls; bounded by the ≤30-hit limit) and derive each `ContextElement.confidence` via `effective_confidence(initial_confidence, last_queried_at, memory_domain, now=<injected clock>)` with live `last_queried_at`. Ranking must use the DERIVED value; the Qdrant payload `confidence` becomes a provenance snapshot only (keep the payload key — no schema migration, no re-index cost; `confidence` is not in `keyword_payload_fields`). `importance`/`memory_domain` handling: verify whether hydration should also supply those or payload defaults stay (INV-01 §P-002; record your choice — payload defaults 0.8/0.5 must not resurrect as ranking truth after hydration for the hydrated fields).
- Degrade rules (must implement, must test): hydration DB failure → fall back to payload snapshot + `context.vector_search_failed`-style warning, semantic tier still serves (never empty the tier); unknown `memory_domain` → stored-passthrough + `memory.fact_unknown_domain` warning, not crash; vector-store failure → existing `[]` path unchanged.
- `src/paa/memory/creator.py`: comment-only change on the payload literal — document `confidence` as enqueue-time provenance snapshot, never ranking truth (no field add, no widening: INV-01 proved enriching with `last_queried_at` goes stale on first touch).
- `src/paa/context/gatherer.py`: ZERO changes (pure gatherer stays pure — explicit non-goal). If you believe a ContextElement change is required, stop and ESCALATE; do not widen the projection.
- `src/paa/memory/facts.py`: ZERO changes expected; `_FACT_COLUMNS` already selects the derivation inputs. If a batch-hydrate needs a new query, put it in the usage site (helper near `_semantic`) rather than extending the repository contract — unless a repository batch method already exists, in which case reuse it. Post NEED if a repository change is unavoidable (it is outside your write-set).
- Tests (must FAIL before, pass after): T02 — seed fact initial_confidence 1.0, enqueue to vector store via the sink (payload conf = 1.0 snapshot, assert payload unchanged for provenance), advance injected `now` past λ half-life, assert `_semantic` `ContextElement.confidence == approx(effective_confidence(1.0, last_queried, domain, now))` ≤1e-6 tolerance style of the suite (use `pytest.approx`), and assert the gatherer `confidence_floor` filter drops it on the derived value even though the payload says 1.0. Boundary: hit missing `confidence` key → hydrated value wins (default must not leak). Partial-failure: hydration DB explodes → payload-snapshot fallback + warning, tier non-empty. Idempotency: same search twice → identical derived value (deterministic injected now). Unknown-domain passthrough test.
- Trace tag `PLAN:integration-fix:P002` comment at the hydration site.
- Acceptance (measurable): T02 green; `tests/memory/test_decay.py` 51 tests green unchanged (run it); `tests/context/test_gatherer.py` green unchanged; `grep -n "payload.get(\"confidence\"" src/paa/` still 1 hit but `_semantic` no longer trusts it for ranking (the derivation line exists); ruff on changed files 0; mypy total stays 3.

## 4. Ownership
WRITE-SET (only these): `src/paa/agents/context_agents.py`, `src/paa/memory/creator.py`, `tests/agents/test_semantic_derivation_p002.py` (new). READ-ONLY: everything else. FORBIDDEN: `gatherer.py`, `facts.py`, `decay.py`, `config.py`, `runtime.py`, `docs/adr/*`. Coordination note: W1-IMPL-03 owns `context_agents.py` this wave; P-004 (Wave 3) and P-007 will edit other functions in the same file later — sequence by function, claim via COMMS before editing.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory.

## 5. Parallel siblings this wave
W1-IMPL-01 (P-014: config/ADR — disjoint), W1-IMPL-02 (P-001 redaction — disjoint). P-004 depends on YOU (plan P04→P02): your derivation must be correct before the coder vector fallback ranks with it, but do NOT implement the fallback here.

## 6. Skip list and known traps
- Fix B (payload enrichment with `last_queried_at`) and Fix C (sweep-refresh) are REJECTED (INV-01): do not add timestamp fields to payload, do not write payloads at sweep time (second stored truth, breaks replayability).
- Do not cache derived confidence anywhere (no attribute, no memo) — derivation is per-query.
- Hydration must be batched (one query), not per-hit N+1 (long-run Load test: assert query count).
- `now` injection: follow the existing clock-injection pattern in `decay.py`/`_to_fact` (`now=None` default → wall clock) so tests pass an injected value; do not invent a new clock convention.
- Baseline category D stale test `test_remember_then_recall_round_trips` is NOT yours (P-004 updates it deliberately in Wave 3). A scoped failure from a sibling's in-flight edit = FINDING, retry once.
- If `_semantic` needs `FactRepository` and it isn't already available in `ContextBuilder`'s constructor, that is a NEED (constructor change is a contract change — post NEED, do not silently widen).

## 7. Environment and tests
Scoped only: your new test file + `pytest tests/memory/test_decay.py tests/context/test_gatherer.py -q` regression + ruff/mypy scoped. No full suite, no containers (fake-vector fixtures only — reuse the `_SynonymEmbedder`/`NumpyVectorStore` pattern from `tests/memory/test_e2e_remember_recall.py:150-207` if suitable; note that file also pins the old synonym-miss behaviour P-004 will later update — do not edit it). `PAA_TEST_VERBOSE=1` optional.
Commit protocol: code+tests commit first, then report in a separate docs-only commit listing code SHA. Trailers `Task: T-002-P002`, `Agent: W1-IMPL-03`, `Wave: W1`, `Report: docs/agent-ops/20261107-integration-fix/reports/w1-implementation/W1-IMPL-03.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w1-implementation/W1-IMPL-03.md` (create at start, ORCH-DIGEST first, append as you work; long-run test answers: source/load 100× hydration query count/hostile unknown-domain+missing keys/operability degrade/reversal). COMMS ≤6 one-line appends (CLAIM `context_agents.py` before editing). Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
