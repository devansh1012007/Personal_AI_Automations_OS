# Module: Context engine (`src/paa/context/`)

## Module identity

- **Name:** Context engine
- **Path:** `src/paa/context/`
- **Main responsibility:** Build bounded context packets with a hard token ceiling for the planner and worker; rank and pack context elements under budget.
- **Architectural layer:** Control / context
- **Entrypoints:** `BoundedContextGatherer.compile()`, `compile_worker_packet()`
- **Public interfaces:** `BoundedContextGatherer`, `ContextElement` (`gatherer.py`); `TokenBudget` (`budget.py`); compaction (`compaction.py`); metrics (`metrics.py`)
- **Other modules that depend on it:** `agents/context_agents.py` (planner + worker builders), `runtime.py` (via agents), `daemon.py` (indirectly)

## Purpose and responsibilities

RFC §5 bounded context: the planner's context packet must fit a hard token
ceiling. The gatherer decides *what gets in* deterministically (ranking,
ceiling, routing directive); an optional AI expander only proposes *what to look
for* (query expansion, slot inference). This split prevents untrusted retrieved
text from arguing its way into the planner's context, which would break replay
reproducibility (`context_agents.py` module docstring).

## Internal implementation

### `gatherer.py` — BoundedContextGatherer
- `_candidate_pool()`: O(n) dedupe by `id`; quality floors `confidence_floor`
  (0.70) / `relevance_floor` (0.75); blocked domains dropped.
- `_rank_key()`: total order `(tier, -(relevance*confidence), -importance, id)`
  — deterministic tiebreak on `id`.
- `_pack()`: `TokenBudget(ceiling)`; each element's cost charged via
  `try_consume(cost)`; items that don't fit are `continue`d (no `break` on
  first oversized). Invariants are not exempt — exactly one append site,
  unconditionally gated on `try_consume`.
- Hard guarantee: `allocated_tokens <= ceiling` — `AssertionError` if exceeded
  (`gatherer.py:565`).
- `compile()` / `compile_worker_packet()` return a packet with
  `routing_directive` (`PROCEED_TO_PLANNER` / `TRIGGER_BACKGROUND_HYDRATION` /
  `HARD_STOP_ESCALATE_TO_USER`), `vacant_slots`, `density`,
  `allocated_tokens`, `token_ceiling`.

### `budget.py` — TokenBudget
- Sub-budget halving per delegation depth: `allocated >> depth` (RFC §15.7),
  capped at parent `remaining`, linked to parent for conservation.
- `try_consume(cost)`: rejects if `consumed + cost > remaining`; no mutation on
  rejection.

### `compaction.py`, `metrics.py`
- Compaction and quality metrics (density, pollution, utility, entropy).

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `compile(hot, semantic, required_slots, token_ceiling)` | elements + slots | `ContextPacket` | pure |
| `compile_worker_packet(...)` | same, worker ceiling | `ContextPacket` | pure |
| `TokenBudget.try_consume(cost)` | cost | bool | mutates `consumed` on success |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `PlannerContextBuilder` → `BoundedContextGatherer` | `compile()` | Pack planner packet | `context_agents.py:309` |
| `WorkerContextBuilder` → `BoundedContextGatherer` | `compile_worker_packet()` | Pack worker packet | `context_agents.py:296` |
| `Gatherer` → `TokenBudget` | `try_consume` | Enforce ceiling | `gatherer.py` |
| `ChiefOrchestrator` → context builders | `handle()` | Hydrate phase | `orchestrator.py:374` |

## Runtime behavior

- Pure computation; no persistence.
- Ceiling enforced identically on every machine: `CharEstimator` (not tiktoken)
  for cross-machine replay determinism (`gather synthesis`).
- `density >= density_proceed (0.85)` → proceed; `>= density_hydrate (0.40)` →
  background hydration; else `HARD_STOP_ESCALATE_TO_USER`
  (`config.py:150`, `:154`).

## Failure behavior

- Expander failure → deterministic `_KeywordExpander` fallback
  (`context_agents.py:122`).
- Missing vector/graph → semantic/graph searches return `[]` (degrade)
  (`context_agents.py:183`, `:214`).
- Ceiling exceeded → `AssertionError` (`gatherer.py:565`).

## Modification impact

- Changing `token_ceiling`/`worker_token_ceiling` affects packet size
  (`config.py:133`, `:137`).
- Changing `density_proceed`/`density_hydrate` changes routing behavior
  (`config.py:150`, `:154`) with validator `density_hydrate < density_proceed`
  (`config.py:160`).
- Tests: covered via ledger suite / agents; context engine 353 tests per
  README (`tests/context/`).

## Known gaps

- `MemorySettings.decay_lambda` duplicates `domains.py` values but isn't read by
  `decay.py`/`domains.py` directly (config/drift question — see
  `modules/memory.md`).

## Related documents

- [`modules/agents.md`](agents.md) — context builders.
- [`modules/memory.md`](memory.md) — memory domains/décay.
- [`backend/request-lifecycle.md`](../backend/request-lifecycle.md) — hydrate phase.
- [`data/data-lifecycle.md`](../data/data-lifecycle.md) — context packet lifecycle.
