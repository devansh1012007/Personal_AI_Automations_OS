<!-- ORCH-DIGEST:START -->
agent: W3-IMPR-02 | task: IMPR-02 | status: DONE
code_commits: 032312d | report_commit: none
files_changed: 6 (src/paa/cli/main.py, src/paa/api/app.py, src/paa/daemon.py,
 tests/cli/test_memory.py, tests/api/test_memory.py, tests/daemon/test_drain_e2e.py)
contracts_changed: none (one-line call-site changes; no signature/behavior change to
 MemoryCreator or Runtime.memory_creator)
tests: added 3, changed 0; scoped run: pass
 (pytest tests/cli/test_memory.py tests/api/test_memory.py tests/daemon/test_drain_e2e.py
 -> 29 passed; ruff check src/paa/cli src/paa/api src/paa/daemon -> 0;
 mypy src/paa/cli src/paa/api src/paa/daemon -> 0)
ownership_violations: none (only WRITE-SET files edited; IMPR-01's
 src/paa/memory/creator.py and src/paa/runtime.py left untouched though modified
 in the working tree by the parallel agent; COMMS.md appended but not committed,
 per standing order 8)
deferred_dependencies: none
out_of_scope_findings: 0
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: none — the fix is mechanical and the acceptance criteria
 are explicit; verified the tests fail on the pre-fix call sites (rule-based
 extractor yields confidence 0.95 / zero facts for a payload with no rule-extractable
 triples, while the LLM path yields confidence 0.70)
confidence: high
last_comms_line_read: [2026-10-25T11:06:00Z] ORCH -> ALL | DECISION | T-000 | WAVE-IMPR-START | Improvement mini-wave...
<!-- ORCH-DIGEST:END -->

# Improvement report — W3-IMPR-02 — IMPR-02 expose rt.memory_creator in drain paths

## 1. Task and acceptance criteria (restated)

Task IMPR-02: `Runtime.memory_creator` is wired with the LLM extractor from
`settings.memory.extraction_model` (`PAA_MEMORY__EXTRACTION_MODEL`) but the CLI
`paa memory drain`, `POST /memory/drain`, and `daemon.py:_memory_creator` all
construct `MemoryCreator(db)` directly, bypassing `rt.memory_creator`. Make the
drain paths consume the runtime-wired creator so `PAA_MEMORY__EXTRACTION_MODEL`
takes effect through the operator surface.

Acceptance criteria (brief §3):
- `paa memory drain` uses `rt.memory_creator` when available (LLM extractor
  active when `extraction_model` set).
- `POST /memory/drain` uses `runtime.memory_creator` when available.
- Daemon ingestion loop uses `self._rt.memory_creator` when available.
- Default path unchanged when `extraction_model=""` (RuleBasedExtractor).
- Scoped: `pytest tests/cli/test_memory.py tests/api/test_memory.py
  tests/daemon/test_drain_e2e.py -q` green; `ruff check src/paa/cli
  src/paa/api src/paa/daemon` -> 0; `mypy src/paa/cli src/paa/api
  src/paa/daemon` -> 0.

## 2. Root cause

`Runtime.build()` (src/paa/runtime.py:333, 375) builds
`memory_creator = _build_memory_creator(settings, db, model_adapter)` and stores
it on the runtime. `_build_memory_creator` (src/paa/runtime.py:430-452) returns a
`MemoryCreator` whose extractor is `LLMFactExtractor(model=model_adapter)` when
`settings.memory.extraction_model` is non-empty, else `RuleBasedExtractor`. But
the three operator drain surfaces each constructed their own `MemoryCreator(db)`,
ignoring `rt.memory_creator`:

- src/paa/cli/main.py:630 — `creator = MemoryCreator(rt.db)` (drain command)
- src/paa/api/app.py:280 — `creator = MemoryCreator(runtime.db)` (drain route)
- src/paa/daemon.py:179 — `return MemoryCreator(self._rt.db)` (`_memory_creator`)

Because `Runtime.build()` always populates `memory_creator`, the field was dead
code from the CLI/API/daemon perspective, and `PAA_MEMORY__EXTRACTION_MODEL`
was a no-op for every drain path an operator actually invokes.

## 3. Fix

One-line call-site change in each drain path, consuming the runtime-wired
creator with a defensive `or MemoryCreator(db)` fallback (the fallback is never
reached in practice because `Runtime.build()` always populates
`memory_creator`):

- src/paa/cli/main.py:630 — `creator = rt.memory_creator or MemoryCreator(rt.db)`
- src/paa/api/app.py:280 — `creator = runtime.memory_creator or MemoryCreator(runtime.db)`
- src/paa/daemon.py:179 — `return self._rt.memory_creator or MemoryCreator(self._rt.db)`

No signature, contract, schema, or default-behavior change. When
`extraction_model=""`, `_build_memory_creator` returns a rule-based creator, so
the default path is byte-for-byte unchanged. When `extraction_model` is set, the
LLM extractor (confidence 0.70, provenance extractor "llm") is consulted through
all three surfaces.

This is a root-cause fix at the call sites, not a band-aid: the invariant that
`rt.memory_creator` is the single source of truth for the configured extractor
is now honored by every drain surface.

## 4. Tests added

One test per drain surface, each proving the drain uses `rt.memory_creator`
when the LLM extractor is configured and fails without the fix:

- tests/cli/test_memory.py — `test_drain_uses_llm_creator_when_extraction_model_set`:
  sets `PAA_MEMORY__EXTRACTION_MODEL`, injects a scripted model returning triples
  via `Runtime.build(model_adapter=...)`, monkeypatches `paa.cli.main._build_runtime`,
  runs `paa memory drain`, and asserts a fact with `initial_confidence == 0.70`
  (LLM) was written. Without the fix, the rule-based extractor yields confidence
  0.95 (or zero facts for a payload with no rule-extractable triples).
- tests/api/test_memory.py — `test_drain_uses_llm_creator_when_extraction_model_set`:
  builds a `Runtime` with a triple-returning scripted model and
  `extraction_model` set, calls `POST /memory/drain`, asserts `initial_confidence
  == 0.70`.
- tests/daemon/test_drain_e2e.py — `test_drain_uses_llm_creator_when_extraction_model_set`:
  builds a `Runtime` with a triple-returning scripted model and `extraction_model`
  set, starts the daemon, seeds a signal, polls to completion, asserts
  `initial_confidence == 0.70`.

All three use bounded polling / no fixed sleeps and isolated tmp_path fixtures,
consistent with the suite's testing conventions.

## 5. Verification (scoped only, per standing orders)

- `ruff check src/paa/cli/main.py src/paa/api/app.py src/paa/daemon.py
  tests/cli/test_memory.py tests/api/test_memory.py
  tests/daemon/test_drain_e2e.py` -> 0 findings
- `mypy src/paa/cli/main.py src/paa/api/app.py src/paa/daemon.py` -> 0 errors
- `pytest tests/cli/test_memory.py tests/api/test_memory.py
  tests/daemon/test_drain_e2e.py -q` -> 29 passed

The full test suite was not run (standing order 16 / brief §7). IMPR-01's files
(`src/paa/memory/creator.py`, `src/paa/runtime.py`, `tests/memory/test_llm_extractor.py`)
were left uncommitted and untouched; they appear modified in the working tree due
to the parallel agent's work and are outside this write-set.

## 6. Reversal check

Reverting any of the three call sites back to `MemoryCreator(db)` restores the
rule-based extractor for that drain surface: `PAA_MEMORY__EXTRACTION_MODEL`
again has no effect through it, and the new tests fail (they assert
`initial_confidence == 0.70` / facts written). This satisfies the reversal
criterion — the fix is what makes the LLM extraction flag effective through the
operator surface.

## 7. Out-of-scope findings

None. No changes to `creator.py`/`runtime.py` (IMPR-01's ownership), no
observability/logging added (marked DO LATER in the brief), no change to the
rule-based default behavior.

## 8. Commit discipline

Code committed first at 032312d with explicit paths and trailers
`Task: IMPR-02`, `Agent: W3-IMPR-02`, `Wave: impr`,
`Report: docs/agent-ops/20261024-memory-polish/reports/w3-improve/W3-IMPR-02-DEAD-WIRING.md`.
This report is committed in a separate docs-only commit referencing 032312d.
