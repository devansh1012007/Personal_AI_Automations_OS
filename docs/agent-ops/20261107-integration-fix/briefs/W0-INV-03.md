# Brief — W0-INV-03 (INV) · wave 0 · run 20261107-integration-fix

## 1. Role and mission
Mission: verify plan items P-007 (allocator half), P-008 (cost gate), P-011 (coding specialist pilot) against live code at HEAD `88e648b`. Why: these are the DEEP candidates (new EventType wiring, delegation graph) — investigators must map contracts before any wave writes them.
Your role: investigator (READ-ONLY on source; you edit only your report file).

## 2. Required reading (in this order)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md` (binding)
2. `AGENTS.md` (EventType checklist: types.py + replay _PHASE_MAP/NO_PHASE + emitter; delegation guards)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md`
4. Plan: `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — items P07, P08, P11 verbatim + tests T08/T09/T12/T15 + §I edge cases (P/R=0, starvation, recursion)
5. Template: `templates/report-investigation.md`
6. Code: `src/paa/context/{metrics.py,gatherer.py}`, `src/paa/agents/{orchestrator.py,delegation.py,policy.py,base.py}`, `src/paa/agents/specialists/__init__.py`, `src/paa/core/types.py` (EventType :122, AgentRole :424), `src/paa/ledger/replay.py`, `src/paa/memory/curator.py`, `src/paa/config.py`

## 3. Task and acceptance criteria
Task T-003: classify P-007(allocator), P-008, P-011 with evidence.
Specifically verify:
- P07: exact `attention_allocation_score` signature/inputs (metrics.py:315-376) — are K/I/F/P/R all available from hot rows or metrics? Where would `allocate_attention()` live (module placement vs `context/` conventions)? `workers:alloc` producer/consumer today (grep). Full EventType add checklist per AGENTS (name the exact new members + which emitters). Drain throttle site `creator.py:1490` — current signature of the drain call, where a `drain.max_per_second` Setting would thread.
- P08: `total_memory_cost` inputs — where do storage/retrieval/noise/maintenance/utility numbers come from today (metrics.py has entropy/pollution/utility — map each Doc A term to an existing metric or mark missing)? Curator ANALYZE phase hook point.
- P11: `AgentRole` enum + `DEFAULT_MUTATING_TOOLS` (grep location), `mediate_delegation` (orchestrator.py:833-886) flow, depth/cycle guards (delegation.py:87-121), how a specialist would be registered/wired (runtime.py:583-591 pattern), `RouterToolCallingModel` constraint (coder/model_adapter.py), what `STATE_ROLLBACK_TRIGGERED` needs (types.py:162 area + replay).
Acceptance: report at `reports/w0-investigation/W0-INV-03.md`, ORCH-DIGEST first, claims tagged.

## 4. Ownership
WRITE-SET: `reports/w0-investigation/W0-INV-03.md` only. READ-ONLY: all source. FORBIDDEN: src edits, commits, COMMS beyond one printf append.

## 5. Parallel siblings this wave
INV-01 (P-009 replay tier events — same replay.py/types.py hotspot; you both only report, no conflict yet), INV-02 (queue `workers:alloc` semantics — ask via COMMS NEED if producer contract unclear), INV-04 (coder recall), INV-05 (config Settings for your flags).

## 6. Skip list and known traps
- ADR-0012 formula keep is settled (D7) — verify wiring only.
- P08 must remain advisory (never-auto-resolve) — flag any code path that would auto-delete.
- `StreamName(StrEnum)` breach is a documented waiver (ADR-0036), not a fix target.

## 7. Environment and tests
Scoped: grep, git, targeted `pytest tests/context tests/agents -q` if needed (tmp_path fixtures). No full suite, no containers.

## 8. Deliverables
Report: `reports/w0-investigation/W0-INV-03.md`. Final message ≤ 8 lines.
