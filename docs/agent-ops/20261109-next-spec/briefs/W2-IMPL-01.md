# Brief — W2-IMPL-01 (IMPLEMENTER) · wave 2a · run 20261109-next-spec

## 1. Role and mission
Mission: N2.1 event types + schema policy + ADR-0046 + frozen ToolJournal protocol. Why: coder events must not break replay or sweep (coder lineages would requeue as tasks, _row_to_event hard-fails on unknown strings). Failure looks like: replay raises on new types, boot sweep requeues coder sessions, mixed-version read crash undocumented.
Your role: implementer (coder), ledger + protocol freeze.

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N2.1 + §C invariants (replay purity, TERMINAL_EVENTS, _row_to_event, _NO_PHASE_CHANGE, canonical_json)
3. src/paa/core/types.py (EventType, TERMINAL_EVENTS), src/paa/ledger/replay.py (_PHASE_MAP, _NO_PHASE_CHANGE), src/paa/ledger/events.py (canonical_json, compute_idempotency_key), src/paa/ledger/store.py (_row_to_event, verify_chain), src/paa/coder/loop.py (ToolJournal protocol must be frozen here), docs/adr/README.md + docs/adr/0045-*.md structure
4. W1-IMPL-07 agent.py journal slot (Any|None, raises NotImplementedError — you replace guard with real wiring interface contract, but N2.1 itself only freezes protocol; journal impl is N2.2. Do NOT wire journal into agent.py here except via protocol stub export)
5. Template: report-implementation.md, COMMS.md

## 3. Task and acceptance criteria
Task T-N2-1 (N2.1 serial): Add 8 EventTypes enumerated: CODER_SESSION_STARTED, TOOL_CALL_PROPOSED, TOOL_CALL_DENIED, TOOL_CALL_COMPLETED, TOOL_CALL_FAILED, CODER_SESSION_ENDED, MANDATE_GRANTED, MANDATE_REFUSED. Every payload carries v (schema version 1). All 8 in _NO_PHASE_CHANGE (informational, not lifecycle) per planner B-5 — coder events do NOT advance task phases, NOT terminal, NOT re-queued. ADR-0046 records forward-compatibility policy (v field, _row_to_event hard-fail one-way door: old binaries cannot read forward rows, mixed-version fleet note). Freeze ToolJournal protocol signature in coder/loop.py or coder/journal.py protocol file (N2.1 freezes: before(ctx)->None, denied(ctx,reason)->None, after(ctx,ok,result_sha)->None with ToolCtx {session_id, call_id, tool, args_sha256, args_preview_redacted, isolation_level, pre_sha256?, post_sha256?, patch_sha256?}). Define persisted intent fields in PROPOSED payload (pre_sha256/post_sha256/patch_sha256). Do NOT implement LedgerToolJournal (N2.2 owns journal.py impl). Tests: property test replay never raises over any ordering incl. 8 new types (extend tests/ledger/test_replay.py sampled enum or new tests/ledger/test_coder_events.py), golden-file tests pin v1 payloads (8 payloads + idempotency discriminator session:call_id:kind fits existing tuple). Acceptance: replay property green, golden payload tests green, ruff 0, mypy 3 baseline, conventions 34.
Commands: pytest tests/ledger/test_coder_events.py tests/ledger/test_replay.py -q, pytest tests/conventions -q, ruff, mypy.

## 4. Ownership
WRITE-SET: src/paa/core/types.py, src/paa/ledger/replay.py, src/paa/coder/loop.py (ToolJournal protocol stub ONLY — exports ToolCtx/ToolJournal, no ledger calls), docs/adr/0046-*.md, tests/ledger/test_coder_events.py (new), tests/ledger/test_replay.py (extend sampled enum only)
READ-ONLY: src/paa/ledger/store.py, src/paa/ledger/events.py, src/paa/coder/agent.py (journal slot frozen — do not wire), src/paa/coder/journal.py (leave for N2.2)
FORBIDDEN: coder/journal.py impl, agents/orchestrator.py, runtime.py, api/*, cli/* (mandate is N2.4)

## 5. Siblings
No parallel implementers (serial freeze). Next Wave 2b N2.2/N2.4/N2.5 parallel after you.

## 6. Skip list
Do NOT add mandate impl (N2.4), recovery (N2.3), secret canary (N2.5). Do NOT change TERMINAL_EVENTS to include coder types (would make sessions terminal).

## 7. Environment
Scoped pytest ledger only.

## 8. Deliverables
Report: reports/w2-impl/W2-IMPL-01-N2-1.md. Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final ≤8 lines.

## 9. Role notes
Protocol freeze is the contract N2.2/N2.3 build on — get _NO_PHASE_CHANGE + persisted intent right here.
