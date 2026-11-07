<!-- ORCH-DIGEST:START -->
agent: W2-IMPL-01 | domain: integration-fix P-004 touch unification (T05 only) | status: DONE
items: pending
proposed_tasks: 0
top_risks: (1) 4 call sites must keep identical behavior; (2) shared file context_agents.py; (3) category-D stale test must not be touched
cross_domain_conflicts: none
out_of_plan_discoveries: 1 (sibling W2-IMPL-03 migrations.py ruff findings, not mine)
confidence: high (4 defs verified at HEAD)
last_comms_line_read: 2026-11-07T15:00:01Z W2-IMPL-01 CLAIM T-004a-P004a
<!-- ORCH-DIGEST:END -->

# Implementation report — W2-IMPL-01 — P-004 touch unification (T-004a-P004a, T05 only)

## 0. Scope and method
Mission: create src/paa/memory/touch.py::touch_recalled(db: RelationalBackend|None, fact_ids: list[str]) with verbatim executable core from the 4 drifted _touch_recalled copies; delete the 4 local defs; rewire 8 call sites to the shared helper; add structural+behavior test tests/memory/test_touch_unification.py. Vector fallback (T-004b/W3) OUT OF SCOPE. Category-D stale test test_remember_then_recall_round_trips NOT ours.
Required reading done in order: STANDING-ORDERS.md, AGENTS.md, 00-mission.md, W0-INV-04 P-004 section, plan refs via brief, live code verified.
Branch verified: [RAN git branch --show-current -> ops/20261107-integration-fix]
Verification: [RAN grep _touch_recalled src/ -> 12 hits: 4 defs (api:346, coder:886, context_agents:479 (was 403 pre-W1, shifted by W1-FIX-CODE), cli:721) + 8 call sites + 1 docstring ref in context_agents:105]. Executable core identical across all 4 [VERIFIED via reading each def body]. Drift only annotation (Any vs RelationalBackend|None), import binding (module-level log/redact in api vs function-local structlog+redact in coder/cli, module log+redact in context_agents), docstring wording.
Export decision: export touch_recalled from paa.memory (follow DecaySweeper precedent in memory/__init__.py) — public since 4 external modules import it; internal would force deep imports. Documented in touch.py module docstring.
Note: W1-FIX-CODE already removed local redact import in context_agents.py; agent brief in play. api/app.py module-level log+redact stay (used elsewhere in file — check before deleting any import).

## 1. What was done
- Created src/paa/memory/touch.py: touch_recalled(db: RelationalBackend | None, fact_ids: list[str]) -> None. Verbatim executable core (early-return, dict.fromkeys dedupe, DecaySweeper(db).touch_many, except -> log.warning memory.recall_touch_failed with redact(str(exc)), log.debug memory.recall_touched). Module-level log/redact (decay.py house style), runtime import of RelationalBackend from paa.storage.relational.base (no TYPE_CHECKING), DecaySweeper imported at module level. Derived-state-exception policy sentence carried once, in the function docstring. PLAN:integration-fix:P004 trace in module docstring. Export decision: public — exported from paa.memory (init import plus __all__ plus package-docstring layer entry), since 4 external modules import it; recorded in module docstring.
- 4 sites: added an import of the shared helper, renamed 8 call sites (_touch_recalled to touch_recalled, args/ordering untouched), deleted the 4 local defs. context_agents.py docstring ref updated. cli/main.py if-main block preserved (self-review caught an edit that had swallowed it; restored and verified via diff).
- Module-level import safety verified: `import paa.cli.main` already pulls paa.memory transitively, so the new import adds no new import-graph edge; models/base imports only paa.core.*, no cycle.
- New tests/memory/test_touch_unification.py (7 tests): contract signature, AST-walk exactly-one-def lock over src/paa (deterministic, no shell-out), dedupe order-preserving (2 params), db=None no-op, empty-list no-op, touch-failure tolerated.

## 2. Verification evidence [RAN]
- tests/memory/test_touch_unification.py -> 7 passed.
- tests/coder/test_runtime_tools.py plus tests/api/test_memory.py plus tests/cli/test_memory.py plus tests/agents/test_context_agents.py plus new file -> 133 passed, 1 failed = pre-existing category-D test_remember_then_recall_round_trips (tests/coder/test_runtime_tools.py:437 asserts old note text; untouched by this change, NOT fixed per brief).
- Grep lock (filesystem walk): def _touch_recalled = 0; def touch_recalled = 1 (memory/touch.py:30); _touch_recalled anywhere in src/paa = 0.
- ruff scoped to 7 owned files -> All checks passed. Full-tree ruff = 6 findings, ALL in src/paa/storage/relational/migrations.py (W2-IMPL-03 in-flight tier DDL) -> FINDING, not fixed (outside write-set, standing order 18).
- mypy src -> 3 errors, all pre-existing baseline sandbox/subprocess_backend.py:548.
- tests/conventions/test_import_conventions.py -> 21 passed (runtime-import contract lock holds for touch.py; fresh-interpreter sweep clean).

## 3. Self-review audit
- Correctness: executable core equivalent to the 4 originals; only annotation (stricter RelationalBackend|None), import binding (module-level), and docstring unified. Call-site args/ordering unchanged. No recall_facts logic, note text, or stale class docstring touched (W3).
- Invariants: log event names memory.recall_touch_failed / memory.recall_touched unchanged; redact semantics unchanged; degrade-never-raise preserved (failure -> warning + return).
- Tests: mutation check — restoring any second def fails the AST lock; breaking the dedupe fails 2 param tests; removing the except-tolerance fails the failure test. cast(RelationalBackend, object()) in behavior tests is deliberate: db is never dereferenced (touch_many patched on the class, same idiom as existing per-surface suites); no type suppression used.
- Risk: classified SURFACE (behavior-identical refactor, no schema/config/EventType). No independent reviewer required by brief; self-review per above.
- Left-over check: EOF trailing blank lines trimmed on the 3 files whose def was at EOF (api, coder, context_agents); cli/main.py keeps its trailing if-main block. git diff of all 5 edited src files read line-by-line.

## 4. Long-run test
- Source: 4 per-surface Touch suites green untouched plus new structural lock.
- Load: touch is one execute_many batch per recall; concurrency unchanged from before (same atomic increment in DecaySweeper.touch_many).
- Hostile: ids come from DB-returned rows; dedupe bounds the batch; no new SQL interpolation.
- Operability: single log event names preserved; dashboards/greps keep working.
- Reversal: revert any call site import -> that surface touch suite fails; restore a second def -> AST lock fails.

## 5. Out-of-scope findings
- FINDING (W2-IMPL-03): full-tree ruff shows 6 findings in src/paa/storage/relational/migrations.py (RUF001/RUF002 en-dashes, RUF100 unused noqa, S608) — sibling in-flight edit, not touched.
- The category-D stale note-string test and the stale _RecallFacts class docstring remain for W3, untouched.

## 6. Git state
Branch ops/20261107-integration-fix verified before commit. Code commit 3a966d8 (7 files, +162/-108); report in separate docs-only commit listing that SHA.


