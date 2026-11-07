# Brief — W1-FIX-CODE (IMPL) · wave 1 fix · run 20261107-integration-fix

## 1. Role and mission
Fix REV-W1/TREV-W1 findings on the Wave 1 P-002 implementation: R-W1-002 (MAJOR, new unredacted log + wrong event name), R-W1-006 + R-W1-007 (MINOR, per-hit crash + superseded filter), TREV P1-a/P2-a/P2-b tests. Why: P-001 class must not grow; hydration degrade promise must hold per-hit, not just batch.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (zero-baseline; runtime import of contract; redact via `models.base`)
3. `docs/agent-ops/20261107-integration-fix/reports/w1-review/REV-W1.md` §§4–5 (R-W1-002, R-W1-006, R-W1-007), §7 DO-NOW items 1+3, §8 (module-level redact import note)
4. `docs/agent-ops/20261107-integration-fix/reports/w1-review/TREV-W1.md` §6 P1-a, P2-a, P2-b (exact test sketches)
5. `docs/agent-ops/20261107-integration-fix/reports/w1-implementation/W1-IMPL-03.md` (what W1-IMPL-03 built)
6. Code: `src/paa/agents/context_agents.py` `_semantic` (~lines 260-330), the `_hot_facts` superseded filter (~:200) to mirror, `src/paa/models/base.py` redact, `tests/agents/test_semantic_derivation_p002.py` (`_FakeVectors` pattern).

## 3. Task and acceptance criteria
Task W1-FIX-CODE. Specifically, all in `_semantic` (`context_agents.py`) + its test file:
- **R-W1-002**: the hydration-failure log currently `log.warning("context.vector_search_failed", error=str(exc))` — two defects: unredacted, wrong event. Fix: `error=redact(str(exc))` + rename to a distinct event `context.hydration_failed`. Import `redact` at MODULE level (file already imports `effective_confidence` at module level; do NOT add a function-local import — same drift INV-04 flagged; if `_touch_recalled`'s function-local `redact` import becomes unused, remove that local import — verify by grep before removing). Update the degrade test to pin the NEW event name + redaction (assert a secret-shaped exception in the DB failure is redacted). The vector-store failure log (`context.vector_search_failed` for the actual vector path) stays as-is — do not rename it.
- **R-W1-006**: wrap per-hit derivation (`float(...)`, `from_iso`, `effective_confidence`, `float(importance)`) in try/except `(ValueError, TypeError)` (keep the existing `KeyError` unknown-domain branch): on corrupt row, fall back to the payload snapshot FOR THAT HIT + warning, never propagate, tier stays non-empty. Add a corrupt-row test: UPDATE `last_queried_at` to garbage string, assert tier non-empty + fallback value. Comment "hydration failure must never empty the tier" must now be true per-hit as well as batch — keep it and make it true, or soften it to match.
- **R-W1-007**: add `AND superseded_by IS NULL` to the hydration SELECT (mirror `_hot_facts :200`). Add a superseded-fact test: superseded fact's vector hit must NOT hydrate (payload fallback or absent — assert whichever the code does: it must not derive as if live).
- **TREV P1-a** partial hydration test (2 hits, 1 live row + 1 deleted id → live derives, deleted uses payload exactly, both present).
- **TREV P2-a** `db=None` test (payload fallback, no crash). **TREV P2-b** empty-hits test (early `[]`, zero hydration queries — counting `fetch_all`).
- Trace: these are review-fix lines on P-002 code; keep tag `PLAN:integration-fix:P002` (already present; do not invent new tags).
- Acceptance: existing 9 semantic tests still green (with the degrade test updated to the new event) + new tests green; `grep -n "str(exc)" src/paa/agents/context_agents.py` shows the hydration log redacted; `grep -rn "_touch_recalled" src/paa/` unchanged by you; ruff on changed files 0; mypy total stays 3.

## 4. Ownership
WRITE-SET (only these): `src/paa/agents/context_agents.py`, `tests/agents/test_semantic_derivation_p002.py`. READ-ONLY: everything else. FORBIDDEN: docs/** (W1-FIX-DOCS owns docs this fix wave), `config.py`, `runtime.py`, `cli/main.py`.
Branch: `ops/20261107-integration-fix`. Worktree: current directory. CLAIMS: post COMMS CLAIM for `context_agents.py` before editing (it is the wave's shared file).

## 5. Parallel siblings this fix wave
W1-FIX-DOCS (docs re-point — disjoint write-set: docs/** only). No shared files; if your new `redact` module-level import shifts line numbers further, that is fine — W1-FIX-DOCS runs against final HEAD and its gate re-run lands after both fixes.

## 6. Skip list and known traps
- Do not touch the pre-existing `context.vector_search_failed` vector-path log — only the hydration log changes. If you find the vector-path log's `str(exc)` is also unredacted, that is a FINDING (pre-existing, out of scope), not a fix — post NEED/COMMS FINDING, do not fix silently.
- ANSI residual (R-W1-005) is NOT in scope (disclosed, pre-existing, DO LATER).
- `inf` threshold (R-W1-008) is NOT in scope (config.py owned by no one this fix wave).
- Baseline failures A–D are not yours.

## 7. Environment and tests
Scoped only: `pytest tests/agents/test_semantic_derivation_p002.py tests/agents/test_context_agents.py tests/memory/test_decay.py -q`, `ruff check` scoped, `mypy` scoped to your files. No full suite.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: W1-FIX-CODE`, `Agent: W1-FIX-CODE`, `Wave: W1-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w1-review/W1-FIX-CODE.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w1-review/W1-FIX-CODE.md` (ORCH-DIGEST first; long-run test answers). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
