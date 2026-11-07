# Brief — W2-IMPL-01 (IMPL) · wave 2 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-004 touch unification (T-004a, T05 only — NOT the vector fallback, which is W3/T-004b). Why: 4 drifted copies of one helper; single home removes the drift class INV-04 proved (annotation/import/docstring only, executable core identical).
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (zero-baseline; "contract resolved the same way everywhere" — runtime import of `RelationalBackend`; module docstring + `__all__` conventions)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (baseline; the category-D stale test `test_remember_then_recall_round_trips` is NOT yours — see §6)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P04 row + §H T05 (NOT T04 — fallback is W3)
5. Investigation (authoritative): `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-04.md` §P-004 — 4 defs (`api/app.py:346`, `coder/runtime_tools.py:886`, `agents/context_agents.py:403`, `cli/main.py:699` + 8 call sites), AST diff result (executable core identical; drift = annotation `db: Any` vs `RelationalBackend | None`, function-local vs module-level imports, docstring wording), recommended Fix A shape, `memory/touch.py` home conventions (decay.py house style: module docstring, `from __future__ import annotations`, `__all__`, module-level structlog, runtime import of `RelationalBackend` from `paa.storage.relational.base`), test landscape (all 4 surfaces pin via `DecaySweeper.touch_many` patch on the class — survives the move).
6. Template: implementation report ORCH-DIGEST first (shape of `templates/report-investigation.md` + `templates/gate-report.md`)
7. Code at current HEAD (verify line numbers yourself — Wave 1 shifted them): the 4 `_touch_recalled` defs + call sites, `src/paa/memory/decay.py` (`DecaySweeper.touch_many`), `src/paa/memory/__init__.py` (export decision), `tests/coder/test_runtime_tools.py::TestRecallTouch`, `tests/api/test_memory.py::TestRecallTouch`, `tests/cli/test_memory.py`, `tests/agents/test_context_agents.py` touch tests.

## 3. Task and acceptance criteria
Task T-004a-P004a. Specifically:
- Create `src/paa/memory/touch.py` with `touch_recalled(db: RelationalBackend | None, fact_ids: list[str]) -> None`: verbatim executable core (dedupe order-preserving dict.fromkeys; `DecaySweeper(db).touch_many(ids)`; `except Exception: log.warning("memory.recall_touch_failed", count=…, error=redact(str(exc)))`; `log.debug("memory.recall_touched", count=…)`), module-level `log`/`redact`, the stricter `RelationalBackend | None` annotation, runtime import of the contract (no `TYPE_CHECKING`), one docstring carrying the "documented derived-state exception" policy sentence once. Export from `src/paa/memory/__init__.py` + package docstring (the `__init__` re-exports `DecaySweeper` etc. — follow it) or deliberately leave internal and say so in the module docstring; pick one and record the choice.
- At all 4 sites: import the shared helper and DELETE the local defs. Verify by grep the full `grep -rn "_touch_recalled" src/` result and that no call site remains pointing at a deleted def. Do NOT change any call-site arguments/ordering (touch is behavior-identical — prove with the 4 per-surface suites).
- `recall_facts` vector fallback is OUT OF SCOPE (W3). Do not touch `_RecallFacts.run` logic; the stale class docstring claiming "Semantic recall absent" is a W3 cleanup (record as FINDING if you touch the file near it, do not fix).
- Tests: T05 structural — a test (new file `tests/memory/test_touch_unification.py`) asserting (a) `memory.touch.touch_recalled` exists with the contract signature, (b) exactly one definition of the touch helper exists in `src/` (walk `src/paa` for `def _touch_recalled` / `def touch_recalled` — replicate the grep lock deterministically, no shell-out), (c) behavior: duplicate ids deduped (touch_many called once with deduped list), `db=None` no-op, empty list no-op, touch failure tolerated (warning, no raise). The 4 per-surface suites must stay green untouched. Prove fail-before: before your change, assertion (b) fails (4 defs); after, passes.
- Trace (comments): `PLAN:integration-fix:P004` in touch.py docstring.
- Acceptance: `grep -rn "def _touch_recalled" src/` = 0; `grep -rn "def touch_recalled" src/` = 1; the 4 per-surface touch suites green; your new test file green; ruff 0, mypy total stays 3; `tests/coder/test_runtime_tools.py` shows no NEW failures (the baseline category-D `test_remember_then_recall_round_trips` may still fail — it is pre-existing; do not fix it, do not hide it).

## 4. Ownership
WRITE-SET (only these): `src/paa/memory/touch.py` (new), `src/paa/memory/__init__.py`, `src/paa/api/app.py`, `src/paa/coder/runtime_tools.py`, `src/paa/agents/context_agents.py`, `src/paa/cli/main.py`, `tests/memory/test_touch_unification.py` (new). READ-ONLY: everything else. FORBIDDEN: `runtime.py` (embedder field is W3), `config.py`, `docs/**`, `src/paa/tui/**`, `recall_facts` fallback logic.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: post COMMS CLAIMs before editing shared files (`context_agents.py` is shared with W1 history but no one edits it this wave; still claim).

## 5. Parallel siblings this wave
W2-IMPL-02 (tier vocab — `core/types.py`/`replay.py`/`domains.py`), W2-IMPL-03 (tier DDL — sql+migrations), W2-IMPL-04 (Dockerfile.embed — Dockerfile/CI), W2-GEN-01 (minor P-002 test additions — `tests/agents/test_semantic_derivation_p002.py` only). All disjoint; you own touch unification exclusively.

## 6. Skip list and known traps
- The `_touch_recalled` in `context_agents.py` was edited in Wave 1 (module-level `redact` now at :56, local import already removed by W1-FIX-CODE) — diff the current file, do not assume INV-04 line numbers.
- `api/app.py` is also P-006's file in W3 — sequence by function (touch sites only here).
- Baseline category-A docker failures + B strftime + C docs-lock (now fixed/green) are not yours. A scoped failure from a sibling's in-flight edit = FINDING, retry once.
- Do not update the stale class docstring or the category-D test (W3 owns the note semantics + fallback).

## 7. Environment and tests
Scoped only: your new test file + the 4 touch suites + ruff/mypy scoped. No full suite, no containers.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: T-004a-P004a`, `Agent: W2-IMPL-01`, `Wave: W2`, `Report: docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-IMPL-01.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-IMPL-01.md` (create at start, ORCH-DIGEST first, append as you work; long-run test answers: source/load/hostile/operability/reversal). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
