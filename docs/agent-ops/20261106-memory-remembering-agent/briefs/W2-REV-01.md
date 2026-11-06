# Brief — W2-REV-01 (REV) · wave 2 review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Independently audit Wave 2 (T-003 touch-on-recall + T-004 curator scheduling) — try to break both.
Role: code reviewer (READ-ONLY on source). Write only your report file.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (gate, ledger invariant, conventions; derived retention state exception in IMPL-03 report)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline — do not re-report pre-existing failures)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — items C03, C04, T04, T05
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-code.md
6. COMMS.md (read at start)

## 3. Task
Review diff: `git diff 0cdf531..HEAD -- src/paa/agents/context_agents.py src/paa/coder/runtime_tools.py src/paa/api/app.py src/paa/cli/main.py src/paa/daemon.py tests/agents/test_context_agents.py tests/coder/test_runtime_tools.py tests/api/test_memory.py tests/cli/test_memory.py tests/daemon/` (code commits 3506e30 T-003, 70dd91f T-004).
Method: BLIND PASS first (diff + surrounding code, findings before implementer reports), THEN claims audit (reports: reports/w2-impl/W2-IMPL-03-T-003.md, reports/w2-impl/W2-IMPL-04-T-004.md).
T-003 angles: recall failure still returns results; worker narrative filtering (touch only returned ids, never superset); superseded-id touches; touch write amplification; `_semantic` id validity; coder dict contract unchanged (no id added to output).
T-004 angles: curator exception kills daemon?; ingestion blocked by maintenance?; budget respected; degrade fallback when vector_store None; double-run with sweep; delete phases corrupting (consolidate/orphans) — especially R3 first-run risk; archive_writer=None degrade documented?
Also flag: IMPL-03 says handle-level touch placement — is the touch placed on the returned elements or inside read helpers that callers may reuse with different filtering? That is your key design probe.
Run scoped tests only.

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/w2-review/REV-code.md
READ-ONLY: everything else. FORBIDDEN: editing source.

## 5. Deliverables
Report with ORCH-DIGEST verdict (APPROVE/APPROVE-WITH-CHANGES/REJECT), findings by severity with file:line evidence + root-cause fix suggestions, optimizations DO NOW/LATER/NOT WORTH IT.
Final ≤8 lines: STATUS|agent|scope|report path|verdict|counts.
