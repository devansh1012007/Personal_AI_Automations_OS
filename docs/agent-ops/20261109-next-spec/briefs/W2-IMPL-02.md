# Brief — W2-IMPL-02 (IMPLEMENTER) · wave 2b · run 20261109-next-spec

## 1. Role and mission
Mission: Journal the coder loop (N2.2) per spec: Ledger-before-side-effect, post-hook PROPOSED with persisted intent, idempotency. Why: coder loop must be in ledger before N2.3 recovery can classify crashes. Failure looks like: PROPOSED written pre-hook so ledger attests unredacted/pre-hook args; crashes between PROPOSED and COMPLETED repeat work.
Your role: implementer (coder), ledger journal.

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N2.2 + §C invariants (ledger-before-side-effect, persisted intent pre_sha/post_sha/patch_sha, _NO_PHASE_CHANGE coder lineage exemption needed in N2.3)
3. src/paa/coder/loop.py (ToolCtx/ToolJournal frozen protocol, AgentLoop._handle_call ordering: PRE hook → permission → ASK → execute → POST), src/paa/coder/agent.py journal slot (Any|None raises NotImplementedError — replace with real wiring), src/paa/ledger/events.py (canonical_json, compute_idempotency_key), src/paa/ledger/store.py (append dedupe-then-seal), src/paa/coder/journal.py (does NOT exist — you create it)
4. ADR-0046 docs/adr/0046-coder-event-forward-compatibility.md (v1 payload contracts, discriminator session:call_id:kind, persisted intent fields)
5. W2-REV-01 review (MINOR-1 runtime import bundling — do runtime import IsolationLevel + delete E entry + add get_type_hints test in same commit; MINOR-2 tier fold fact_id guard + MINOR-4 discriminator validation + NIT-1 exports + NIT-2 agent message fix here where in scope)
6. Template: report-implementation.md, COMMS.md

## 3. Task and acceptance criteria
Task T-N2-2 (N2.2): coder/journal.py LedgerToolJournal implementing ToolJournal protocol (before/denied/after async, ToolCtx 9 fields frozen). Order: PRE hook (may modify/redact) → journal PROPOSED (post-hook args_sha256, persisted intent pre_sha256/post_sha256/patch_sha256, args_preview redacted, isolation_level = actual enum NONE/PROCESS/NAMESPACE/VM member name string, v=1) → permission → ASK → execute → journal COMPLETED/FAILED; on deny journal DENIED. Persisted intent must be computed from actual file hashes (for file edits) — N2.3 will classify via these SHAs. Idempotency key discriminator `session:call_id:kind` (kind distinguishes PROPOSED vs COMPLETED/FAILED/DENIED; 4 distinct keys per call; duplicate PROPOSED append returns existing event). Also fix: (MINOR-1) convert TYPE_CHECKING import of IsolationLevel to runtime import in loop.py + remove E classification + add get_type_hints(ToolCtx) test; (MINOR-4) reject `:`-bearing and empty session_id/call_id before discriminator; (NIT-2) update agent.py error message "no ToolJournal protocol exists yet" (now exists); (optional) add exhaustive set(EventType) == _PHASE_MAP | _NO_PHASE_CHANGE lock test if you are touching test_replay. Acceptance: tests/coder/test_journal.py (or test_coder_journal.py) with duplicate-append returns existing, denied-never-executes, order post-hook proven; ruff 0; mypy 3; conventions 34; ledger 133.
Commands: pytest tests/coder/test_journal.py tests/ledger/test_coder_events.py -q, pytest tests/conventions -q, ruff check src tests, mypy src.

## 4. Ownership
WRITE-SET: src/paa/coder/journal.py (new), src/paa/coder/loop.py (wire journal + IsolationLevel runtime import), src/paa/coder/agent.py (replace journal guard with real wiring), tests/coder/test_journal.py (new), tests/ledger/test_coder_events.py (re-point golden tests at real producer output if needed for fact_id coverage)
READ-ONLY: src/paa/core/types.py, src/paa/ledger/replay.py, src/paa/ledger/events.py (do not change canonical_json)
FORBIDDEN: ledger/mandate.py + runtime.py + orchestrator.py (N2.4 parallel), ledger/recovery.py (N2.3 2c)

## 5. Siblings (parallel 2b)
W2-IMPL-03 (N2.4 mandate — owns mandate.py/runtime/orchestrator/cli/api, disjoint), W2-IMPL-04 (N2.5 canary — owns test_secret_hygiene.py only). Disjoint.

## 6. Skip list
Do NOT implement recovery sweep (N2.3) or canary test (N2.5). Do NOT change payload v1 contracts without ADR.

## 7. Environment
Scoped pytest ledger + coder only.

## 8. Deliverables
Report: reports/w2-impl/W2-IMPL-02-N2-2.md. Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final ≤8 lines.

## 9. Role notes
Post-hook ordering is the spec's hard gate for redaction (B-7); persisted intent is what makes N2.3 possible (B-6). Never attest pre-hook args.
