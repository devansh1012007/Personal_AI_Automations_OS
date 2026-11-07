# Brief — W5-IMPL-02 (IMPL) · wave 5 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-011 (T-011) — pilot `coding` specialist ONLY (AgentRole, registration, `mediate_delegation` routing, depth-block emit over the EXISTING `STATE_ROLLBACK_TRIGGERED` event, router-enum tool surface, policy globs). Why: pilot-first (the "21 specialists" count is a phantom — RFC §11.8 lists 10 examples); the delegation + rollback infrastructure exists but has no specialist client.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (coder agent section: tool-name policy globs — a new mutating tool MUST be added to the tuple or `--mode plan` silently exempts it; constrained-JSON tool surface; `ModelTurn.tokens` always 0 → `max_steps` bounds runs; `class X(str,enum.Enum)`; NewType IDs)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (hotspots: `core/types.py` single-writer this wave)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P11 row VERBATIM (AgentRole.CODING_SPECIALIST, DEFAULT_MUTATING_TOOLS globs, recursion caps asserted, mediate_delegation, ADR-0041 rollback EventType "explicit, terminal in replay" — NOTE: the "terminal" sub-claim is MISCLASSIFIED (see §6); routing + rollback section) + §H T12 + §J (recursion depth>2 block + validation rollback event), §G D10 (coding-only + 4 gated at ≥90% over 50 scripted tasks — [ASSUMPTION] threshold; ADR-0041 may raise)
5. ADR `docs/adr/0041-coding-specialist-pilot.md` (FULLY — routing, enum surface, terminal-vs-resumable resolution, gating; ADR wins over plan where they differ; do NOT edit the ADR)
6. Investigation: `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-03.md` §P-011 (AgentRole 9+ROUTER at types.py:424-442, specialists pkg 0 lines, `DEFAULT_MUTATING_TOOLS = _MUTATING_TOOLS` coder/permissions.py:127-140, `CoderAgent.build` mutating_tools tuple coder/agent.py:202, `mediate_delegation` orchestrator.py:833-886 (the ONLY agent→agent path — missing target → failure; RecursionGuardError → failure), `DelegationRegistry` depth/cycle/node defaults delegation.py:147, registration dict `runtime.py:583-591` (8th entry pattern), `RouterToolCallingModel.propose` constrained enum model_adapter.py:79-185, `STATE_ROLLBACK_TRIGGERED` already wired end-to-end (types.py:165, replay.py:166 `→ ROLLED_BACK`, :266-276, recovery.py:398-418, reflection.py:131,154) + replay:83 `_RESUMABLE_PHASES` vs TERMINAL (NOT terminal — code is the authority, plan sub-claim MISCLASSIFIED), orchestrator block today returns failure WITHOUT emitting (orchestrator.py:862-870 — the new emit site), Fix A endorsed, rollback idempotency via full key, replay rebuilds ROLLED_BACK + rollback_count; `W0-INV-05.md` §P-014 (SpecialistsSettings nested CodingSettings declared W1).
7. W-wave evidence: `reports/w1-implementation/W1-IMPL-01.md` (SpecialistsSettings shape + `specialists.coding.enabled` nested env); `docs/coder-agent.md` (coder agent write-up — read before adding to `coder/`).
8. Template: implementation report ORCH-DIGEST first.
9. Code at HEAD (verify line numbers yourself): `src/paa/core/types.py` AgentRole (additive member), `src/paa/agents/specialists/__init__.py` (0 lines — new module `coding.py` or `__init__` content — decide and record), `src/paa/coder/permissions.py` globs + `src/paa/coder/agent.py` mutating_tools tuple, `src/paa/agents/orchestrator.py` `_agents` entry + block path, `src/paa/runtime.py` registration dict (embedder field is there — ADDITIVE, don't disturb), `src/paa/agents/delegation.py` guards, `src/paa/coder/model_adapter.py` enum surface, `src/paa/config.py` SpecialistsSettings (read-only), existing orchestrator/delegation/coder tests (glob `tests/agents/test_delegation.py`, `tests/agents/test_orchestrator*.py`, `tests/coder/test_agent.py` patterns).

## 3. Task and acceptance criteria
Task T-011-P011. Specifically (Fix A scoped from INV-03 §P-011):
- `AgentRole.CODING_SPECIALIST = "coding_specialist"` in `core/types.py` (additive, `class X(str,enum.Enum)`; pass `.value` explicitly everywhere round-tripped). Registration: `runtime.py` agents dict + orchestrator `_agents` entry (`"coding": …` per the dict pattern; router-bypass per ADR-0041 when the caller names the target).
- `agents/specialists/` package: implement the coding specialist (new module; reuse `CoderAgent` loop through `RouterToolCallingModel` — constrained enum surface, no native tool calling in this task). Tool names MUST match `DEFAULT_MUTATING_TOOLS` globs or be appended to the `mutating_tools` tuple in `CoderAgent.build` (AGENTS coder section — a new mutating tool added to that tuple or it is read-only-by-accident; audit every tool the specialist offers).
- Delegation: ONLY via `mediate_delegation` (the sole path). Depth/cycle/node guards asserted (reuse `DelegationRegistry` defaults; STANDARD=1/COMPLEX=2 ceilings are the T12 expectation).
- Block emit (the new site): depth>2 (RecursionGuardError at `orchestrator.py:862-870` path) emits `STATE_ROLLBACK_TRIGGERED` over the EXISTING event (payload additive `reason/depth/ceiling` fields alongside existing drift keys; full ADR-0008 key for idempotency) BEFORE returning failure — ledger-before-side-effect + idempotent-by-full-key. NO new EventType member, NO TERMINAL change (ADR-0041 + live code rule: ROLLED_BACK is resumable; if ADR-0041 says anything else the ADR description is wrong and code wins — record it).
- `specialists.coding.enabled` (W1 nested Settings — CONSUME only): disabled → `mediate_delegation` to coding returns the no-agent failure (not a crash, not a silent queue).
- Other 4 gated specialists (research/writing/data-analysis/ops): explicitly NOT built (ADR-0041 names the deferral; no stubs, no empty modules, no "forthcoming" code).
- Tests (must FAIL before, pass after): T12 — 50 scripted coding tasks routed ≥90% through `mediate_delegation` (scripted model per tests/integration/conftest.py `ScriptedModel` pattern — answers by schema shape; the "no model in the security loop" claim is separate and unrelated); depth>2 blocked + `STATE_ROLLBACK_TRIGGERED` in ledger with task `correlation_id` matching; replay rebuilds `ROLLED_BACK` + `rollback_count`; idempotency — redelivered rollback with the same key suppressed once; unknown `target` → failure (existing behavior pinned); self-delegation/cycle → `RecursionGuardError`; payload=empty packet → specialist path does not crash. `policy_agent_has_no_model_dependency`-style structural assertion for the new agent if the pattern applies (read that test; mirror only if it does). New `tests/agents/test_coding_specialist_p011.py`.
- Trace tag `PLAN:integration-fix:P011`.
- Acceptance: T12 green with fail-before shown; delegation/coder/ledger-replay suites green unmodified; ruff 0, mypy total 3; `grep CODING_SPECIALIST src/` live hits; NO new EventType member; no `config.py` edits.

## 4. Ownership
WRITE-SET (only these): `src/paa/agents/specialists/__init__.py`, `src/paa/agents/specialists/coding.py` (new), `src/paa/core/types.py` (AgentRole member only), `src/paa/runtime.py` (registration entry only), `src/paa/agents/orchestrator.py` (agents entry + block emit only), `src/paa/coder/permissions.py` (only if globs added), `tests/agents/test_coding_specialist_p011.py` (new). READ-ONLY: everything else. FORBIDDEN: `src/paa/config.py`, `docs/adr/*`, `src/paa/coder/model_adapter.py` (surface reuse, no changes), `src/paa/ledger/*` (replay untouched — event reuse), `src/paa/coder/agent.py` (verify-only unless the mutating tuple genuinely needs a tool).
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: COMMS CLAIM before editing `core/types.py` (hotspot; sole owner this wave) + `runtime.py` + `orchestrator.py`.

## 5. Parallel siblings this wave
W5-IMPL-01 (cost gate — `metrics.py`/`curator.py`), W5-IMPL-03 (allocator scheduling — `daemon.py`/`runner.py`/`creator.py`), W5-DOCS (docs). Disjoint; you own `types.py` (+ orchestrator block path) exclusively.

## 6. Skip list and known traps
- The plan's "terminal in replay" sub-claim is WRONG (ROLLED_BACK ∈ `_RESUMABLE_PHASES`, excluded from `TERMINAL_EVENTS`). Code is the authority; ADR-0041 re-states it — follow code + ADR, never the plan phrase. Do NOT create a terminal event.
- 21 specialists at once = rejected. Pilot = ONE `coding`. The 50/90 gate threshold is plan-assumed; ADR-0041 may raise it — follow the ADR number in tests.
- `ModelTurn.tokens` always 0 → bound runs with `max_steps`, never `max_tokens`.
- A mutating tool not matching globs / not in the tuple becomes read-only-by-accident (AGENTS coder rule) — audit every tool.
- Baseline failures are not yours.

## 7. Environment and tests
Scoped only: your new test file + `tests/agents/test_delegation.py` + `tests/coder/` + `tests/ledger/test_replay.py` (replay unchanged proof) + ruff/mypy scoped. Tmp fixtures + ScriptedModel only; NO real model tokens. No full suite.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: T-011-P011`, `Agent: W5-IMPL-02`, `Wave: W5`, `Report: docs/agent-ops/20261107-integration-fix/reports/w5-implementation/W5-IMPL-02.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w5-implementation/W5-IMPL-02.md` (create at start, ORCH-DIGEST first, append as you work; long-run answers incl. pinpointed writer for the 4 deferred specials). COMMS ≤6 appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
