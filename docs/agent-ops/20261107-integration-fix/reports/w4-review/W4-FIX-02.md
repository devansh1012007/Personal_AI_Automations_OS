<!-- ORCH-DIGEST:START -->
agent: W4-FIX-02 | task: fix REV-W4b R-W4b-001 allocation-stub pollution | status: COMPLETE
shape: (a) module-scope early import — Collapse: no src/, no full-settings stub, no noqa
proof: allocation↔TestVectorSinkSemanticRoundTrip pair green BOTH orders; allocation isolated 4/4; test_gatherer↔allocation both orders 66/66; ruff 0 on file; mypy src = 3 baseline
pin: test_gatherer_binding_survives_enable fails (AttributeError at gatherer.py:327) when early import removed, passes with it — reversal executed, not reasoned
blockers: none. Note: brief named tests/memory/test_e2e_remember_recall.py::TestVectorSinkSemanticRoundTrip; that class lives in tests/memory/test_creator.py:297 — proof used the real path.
commits: tests 6fec6bb (docs commit follows)
<!-- ORCH-DIGEST:END -->
# Report — W4-FIX-02 (fix REV-W4b R-W4b-001)

## 1. Root cause (verified in code + reproduced)

`tests/agents/test_allocation_callsite_p007.py::_enable` monkeypatched
`paa.config.get_settings` to a lambda returning
`SimpleNamespace(allocator=...)` (no `.context`). `ChiefOrchestrator.run_allocation`
[src/paa/agents/orchestrator.py:1139-1140] lazy-imports
`from paa.config import get_settings` then
`from paa.context.allocator import allocate_attention` inside the patched window.
The second import runs `paa/context/__init__.py:42`, which imports
`paa/context/gatherer.py:30` (`from paa.config import ... get_settings`) and
budget/allocator likewise. Because `paa.config.get_settings` is currently the
lambda, those modules bind the lambda at module scope. Monkeypatch teardown
restores `paa.config.get_settings` but not the already-bound module globals.
A later `BoundedContextGatherer()` [gatherer.py:327,
`get_settings().context`] then raises
`AttributeError: 'types.SimpleNamespace' object has no attribute 'context'`.
Full-suite collection masked it because gatherer tests bind the real function first.

## 2. Fix chosen — shape (a), root cause

Added one module-scope line at the top of the test file:

```python
from paa.context.allocator import allocate_attention
```

This executes at pytest COLLECTION time, before any monkeypatch, so
`paa.context.__init__` (gatherer, budget) and the allocator module bind the
REAL `get_settings`. `run_allocation`'s lazy import is then a `sys.modules`
hit and never re-executes module code inside the patched window. Existing
`get_settings().allocator` seam is untouched: `from paa.config import
get_settings` re-resolves per call, so `_enable` still drives enabled/disabled.

Rejected shape (b): returning a full Settings object would only make the
poisoned binding less lethal — a later test would silently see the stub's
allocator values through the gatherer/allocator bindings. The violation would
remain; the reviewer asked for either, the brief required one, and (a) is the
root-cause fix with no config leakage. The narrow SimpleNamespace stub is kept
intentionally (documented in `_enable` INVARIANT).

To satisfy ruff F401 without a noqa, the import is used, not suppressed:
`test_gatherer_binding_survives_enable` (new pin, 4th test in file) asserts
`sys.modules["paa.context.allocator"].allocate_attention is allocate_attention`
(lazy import resolves to the early object) and constructs
`BoundedContextGatherer()` inside the patched window. The 3 original callsite
tests + the raising-queue guard pin are unchanged in behavior.

## 3. Evidence (commands + verbatim outcomes)

Pre-fix reproduction (before any edit):

- [RAN `pytest tests/agents/test_allocation_callsite_p007.py tests/memory/test_creator.py::TestVectorSinkSemanticRoundTrip -q` -> 5 passed, 1 FAILED: `AttributeError: 'types.SimpleNamespace' object has no attribute 'context'` at `src/paa/context/gatherer.py:327` via `PlannerContextBuilder -> BoundedContextGatherer()`]
- [RAN reverse `pytest tests/memory/test_creator.py::TestVectorSinkSemanticRoundTrip tests/agents/test_allocation_callsite_p007.py -q` -> 6 passed — order-dependence confirmed]
- [RAN brief's literal `tests/memory/test_e2e_remember_recall.py::TestVectorSinkSemanticRoundTrip` -> `ERROR: not found` — brief filename typo; class is at `tests/memory/test_creator.py:297`. All pair proof below uses the real path.]

Post-fix (same commands, fixed file):

- [RAN `pytest tests/agents/test_allocation_callsite_p007.py -q` -> `....` 4/4 passed (3 callsite + 1 pin)]
- [RAN allocation-first pair `pytest tests/agents/test_allocation_callsite_p007.py tests/memory/test_creator.py::TestVectorSinkSemanticRoundTrip -q` -> `.......` 7/7 passed]
- [RAN reverse pair `pytest tests/memory/test_creator.py::TestVectorSinkSemanticRoundTrip tests/agents/test_allocation_callsite_p007.py -q` -> `.......` 7/7 passed]
- [RAN `pytest tests/context/test_gatherer.py tests/agents/test_allocation_callsite_p007.py -q` -> 66/66 passed]
- [RAN `pytest tests/agents/test_allocation_callsite_p007.py tests/context/test_gatherer.py -q` -> 66/66 passed]
- [RAN `ruff check tests/agents/test_allocation_callsite_p007.py` -> `All checks passed!`]
- [RAN `mypy src` -> 3 errors, all `src/paa/sandbox/subprocess_backend.py:548` (pre-existing baseline, identical to REV-W4b count; this task touches no `src/`)]

Reversal (executed, not reasoned): temporarily commented out the early import,
then [RAN `pytest tests/agents/test_allocation_callsite_p007.py::test_gatherer_binding_survives_enable -q` -> 1 FAILED with `AttributeError: 'types.SimpleNamespace' object has no attribute 'context'` at gatherer.py:327]. File restored from backup; pair re-run green. The pin fails for the right reason with and without the pair present.

## 4. Files changed

- `tests/agents/test_allocation_callsite_p007.py` ONLY (early import + comment,
  `_enable` INVARIANT docstring, new pin test, `import sys`). No `src/`, no
  `docs/` code edits (this report is the docs deliverable).
- `docs/agent-ops/20261107-integration-fix/reports/w4-review/W4-FIX-02.md`
  (this file, separate docs-only commit after test commit).

## 5. Long-run test

- Source: production lazy import unchanged; tests now guarantee no first
  context import can happen under a mock. No production code path altered.
- Load: collection-time import is one-time; no runtime cost. Concurrency: none
  (modules already in `sys.modules`). Partial failure: pin makes any future
  reintroduction fail loudly with the exact AttributeError.
- Hostile input: n/a to this change (stub shape unchanged; allocator pinning
  untouched).
- Operability/reversal: verified above by execution.
- Remaining risk: if another test file creates the same stub-then-first-import
  pattern, it could still poison gatherer; this fix scopes the invariant to
  this file's stub (annotated INVARIANT). Full-suite run NOT-RUN (scoped
  orders only, per standing orders); full suite should be run by CI.

COMMS last line read: 2026-11-07T13:52:40Z W4-FIX-DOCS2 DONE (line 131).
