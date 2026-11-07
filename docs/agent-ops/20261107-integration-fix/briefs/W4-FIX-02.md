# Brief — W4-FIX-02 (IMPL) · wave 4 fix-2 · run 20261107-integration-fix

## 1. Role and mission
Fix REV-W4b R-W4b-001 (MAJOR, test-isolation defect): `tests/agents/test_allocation_callsite_p007.py`'s `_enable` stub binds a lambda returning `SimpleNamespace(allocator=...)` into `paa.config.get_settings`; a lazy import of `paa.context.allocator` inside the stub window re-runs `paa/context/__init__` → `gatherer.py` binds the lambda permanently at module scope; a later test (`TestVectorSinkSemanticRoundTrip`) then fails with `SimpleNamespace has no attribute 'context'`. Why: deterministic order-dependent suite failure (narrower runs break; full-suite collection masks it).
Your role: implementer (coder, test-only fix). You edit one file. Commit only your file.

## 2. Required reading
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `docs/agent-ops/20261107-integration-fix/reports/w4-review/REV-W4b.md` §4 R-W4b-001 + §3 long-run + §7 O1 (fix shape: hoist `allocate_attention` to test-module scope so the package `__init__` binds the original before any monkeypatch, OR return a full settings object from the stub)
3. `tests/agents/test_allocation_callsite_p007.py` (the `_enable` stub), `src/paa/context/__init__.py:42`, `src/paa/context/gatherer.py:30`

## 3. Task and acceptance criteria
Task W4-FIX-02. Specifically:
- Fix the stub so `paa.context.gatherer` can never bind a test lambda: implement ONE of the reviewer's two shapes (prove which you chose): (a) module-level `from paa.context.allocator import allocate_attention` (or orchestrator import) at the TOP of the test file so collection binds the real `get_settings` before any monkeypatch; or (b) the stub returns a FULL settings object (real Settings via the paa_home fixture pattern — the allocator/tier tests' `paa_home` idiom: real Settings with only the allocator fields overridden), so even a re-bound module attribute is a real settings object. Record the choice. The 3 callsite tests must keep asserting the same behaviors (allocated path, underfed, disabled) — no coverage weakening; the raising-queue guard pin stays.
- Prove the fix reproduces the exact REV pair: `pytest tests/agents/test_allocation_callsite_p007.py tests/memory/test_e2e_remember_recall.py::TestVectorSinkSemanticRoundTrip -q` green in BOTH orders (allocation-first and reverse), plus the callsite file isolated, plus `tests/context/test_gatherer.py` + allocation together.
- Do NOT touch `src/` (this is test-side; any src change = NEED + stop).
- Trace: none (test fix; the file already carries P007 tags where applicable — keep).
- Acceptance: pair-order proof green both ways; callsite file green isolated; ruff 0 on the test file; mypy unaffected (tests disallow_untyped_defs relaxed — your file must still typecheck: `mypy src` unaffected).

## 4. Ownership
WRITE-SET (only this): `tests/agents/test_allocation_callsite_p007.py`. READ-ONLY: everything else. FORBIDDEN: `src/**`, `docs/**`.
Branch: `ops/20261107-integration-fix`. Worktree: current directory.

## 5. Parallel siblings
None running. W5 opens after you (REV-W4b second-chance re-review covers your diff + W5 then final review covers all).

## 6. Skip list
R-W4b-002–005 semantics, R-W4b-003 docs sweep, R-W4b-004 STRICT wedge, drain-window survivor, formatter-dedup unpinned: tracked residuals (see ledger), not yours.

## 7. Environment and tests
Scoped only: the pair-order proof + callsite file + `tests/context/test_gatherer.py` + ruff scoped. No full suite.
Commit protocol: test commit first, then report in a separate docs-only commit. Trailers `Task: W4-FIX-02`, `Agent: W4-FIX-02`, `Wave: W4-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w4-review/W4-FIX-02.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w4-review/W4-FIX-02.md` (ORCH-DIGEST first; pair-order evidence). COMMS ≤6 appends. Final message ≤8 lines.
