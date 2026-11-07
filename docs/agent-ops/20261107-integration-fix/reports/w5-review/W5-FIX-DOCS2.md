<!-- ORCH-DIGEST:START -->
agent: W5-FIX-DOCS2 | domain: docs-only re-point (W5 FIX-01 residuals) | status: DONE
items: 1 lock-visible + ~49 lock-blind companions re-pointed across 13 docs files; lock green (5/5)
proposed_tasks: none
top_risks: 3 judgment calls on pre-existing drift (pattern cites to def lines, documented below); sandbox.md:193 holder corrects FIX-01 report's :446 (sentence names _acquire_sandbox, not _try_queue)
cross_domain_conflicts: none (sole editor this wave)
out_of_plan_discoveries: none
confidence: high (every new line verified by reading holder text at HEAD; lock executed)
last_comms_line_read: 2026-11-07T20:00:00Z W5-FIX-01 DONE
<!-- ORCH-DIGEST:END -->

# Report — W5-FIX-DOCS2 (docs-only citation re-points, W5 fix-2)

## 1. Status: DONE

All RUN-list citations at HEAD `2d62a44` re-pointed by reading each citing
sentence (def line / holder line, never blind-shift), including every
lock-blind companion into the three FIX-01-shifted files that carry line
cites (`runtime.py`, `memory/creator.py`, `memory/curator.py`);
`tui/runner.py` carries zero `path:line` cites in scope, so its `+26`
shift breaks nothing. Docs commit `cef92c2` (13 files, explicit paths);
this report is a separate docs-only commit listing that SHA.

## 2. Root cause & context (verified at HEAD before editing)

- **Lock state at HEAD** [RAN `pytest tests/conventions/test_docs_references.py -q`
  → 4 pass, 1 fail]: exactly one lock-visible failure,
  `docs/modules/sandbox.md:193` (`runtime.py:441`, blank). Note the brief's
  "RUN list" is this single cite; everything else is lock-blind.
- **FIX-01 shifts** [VERIFIED `git diff 0bcb62c..c2e746c`]: `runtime.py` `+5`
  at the `Runtime.build` call site (~344) and `+1` (`ledger=` param) at
  `_build_memory_creator` (~456) → `+5` up to the signature, `+6` after;
  `memory/creator.py` `+11` (module docstring) with a second `+11` at ~2016
  (no doc cite reaches it); `memory/curator.py` `+9` (cost-gate constant)
  + ctor `+6` → `run_maintenance` 160→175, `_phase_decay` 248→263;
  `tui/runner.py` `+26` inside `_attention_top` (no line cites).
- **Method**: enumerated all 1281 lock references through the lock's own
  `find_references`/`resolve`, filtered to the four edited files' resolved
  paths, then compared `0bcb62c` vs HEAD cited-line text. Cites whose cited
  line predates every hunk show identical old/new text → left untouched
  (e.g. every `runtime.py:271`/`110`/`133`/`77` cite). Each remaining cite
  was fixed by reading its sentence and locating the holder at HEAD
  (def line or the specific site the sentence describes), then confirmed by
  the lock + a fresh post-edit text audit (every new line's text matches the
  sentence's claim).

## 3. Phased changes executed (= one phase: re-point all companions)

**The lock-visible RUN-list cite** (docs/modules/sandbox.md):
- `:114 `/`:151 ` `runtime.py:497` → **`:517`** (`async def _acquire_sandbox`).
- `:193 ` `runtime.py:441` → **`:517`** (same def). DECISION: FIX-01's report
  proposes `runtime.py:446` (`return get_queue`, inside `_try_queue`), but
  the sentence names `_acquire_sandbox` probe timeout → subprocess fallback,
  so `:517` is the sentence-true holder (and agrees with `:114` in the same
  document). The old `:441` read `return get_queue` even at `0bcb62c`, i.e.
  the propose was a blind-shift outcome; this is a correction of a
  hypothesis, recorded here per standing orders §4, not a scope breach.

**runtime.py companions** (def lines at HEAD: `_try_vector` 419,
`_try_graph` 432, `_try_queue` 442, `_build_memory_creator` 452,
`_acquire_sandbox` 517, `_build_agents` 560):
- `adr/0036:115` `runtime.py:482/:474` → **`:502/:494`** (the redact-only
  `log.warning` sites; verified correct at `8d39e5c`, drifted by earlier
  waves, re-pointed to the true holders).
- `component-interactions:223` `:387` → **`:419`**; `storage.md:85` `:400` →
  **`:419`** (degrade pattern; both old cites pointed at `Runtime.build`
  kwargs/log lines already at `0bcb62c` — pre-existing drift, fixed to the
  def line of the first named factory).
- `component-interactions:226`, `storage.md:86`, `deployment-and-runtime:40`,
  `documentation-audit:67` `runtime.py:497/:511` → **`:517`**.
- `component-interactions` (chain lines pre-hunk) and every cite at
  `runtime.py:271/152/110/133/77/265/140/144/237/241/189/98/288/326/331/55/
  80/264/209` → untouched (old == new text).
- `overview:96` `:414,:427,:437,:511` → **`:419,:432,:442,:517`**.
- `persistence-and-storage:101` `:414`→`:419`, `:102` `creator.py:1480`→
  `creator.py:1491`; `:112` `:427`→`:432`; `:133` `:373`→ **`:393`**
  (`queue=queue` in the `Runtime` ctor, the sentence's subject; old `:373`
  was the `ChiefOrchestrator` ctor kwarg); `:154` `:414`–`:424`→**`:419`–
  `:429`** (`def _try_vector` … `return None, None`).
- `agents.md:114,118` `:554`→ **`:560`**; `deployment-and-runtime:37`
  `:414`–`:424`→`:419`–`:429`, `:39` `:554`→`:560`, `:41` `:363`→
  **`:368`** (`orchestrator = ChiefOrchestrator(`).
- `memory.md:143` `runtime.py:474` → **`:571`** (`anti_goals =
  _load_anti_goals(settings)` — the read site the table's Source cell names).
- `validation.md:83` `runtime.py:571` → **`:592`** (the lazy `from
  paa.validation.engine import …` inside `try:`; old `:571` was `worker_ctx`
  kwargs — pre-existing drift).
- `flexability-audit:26` `runtime.py:479-524` → **`runtime.py:560-619`**
  (`_build_agents` body, which composes `CriticValidatorAdapter`,
  `DeterministicValidationEngine`, sandbox, `WorkerCell`; the old range
  covered `_build_memory_creator` — pre-existing drift on a dated audit).

**creator.py companions** (`+11` through the cited region; defs at HEAD:
`run_batch` 1268, `drain_pending_embeddings` 1533):
- Drain cites `creator.py:1515` → **`:1533`** (adr/0036 ×2, memory.md:91,
  deployment:110). Old `:1515` was inside `_enqueue_embeddings` — the
  sentence names the drain function, so the def line is the holder.
- `memory.md:90` `:1243` → **`:1268`** (`run_batch` def; old `:1243` was the
  `dropped=` line inside `process_signal` — sentence names ingestion,
  i.e. `run_batch`).
- `deployment-and-runtime:101` `:1300` → **`:1268`** (same).
- `memory.md:95`, `deployment:104` `:185` → **`:196`** (the "Deterministic
  and replay-stable" docstring, holder text identical at both).
- `persistence-and-storage:102` `:1480` → **`:1491`** (commit-site line,
  unchanged text).
- `deployment:114` `:1620` → **`:1631`** (`indexed = …enqueue(pending)`).
- `Plan1.md` (dated plan, region-approximate even at `0bcb62c`, so
  re-pointed same-region with holder text verified identical at `+11`, not
  re-interpreted): `:66` `1134-1230`→`1145-1241`; `:95` `:455`→**`:466`**
  (`class RuleBasedExtractor`, which is `+11` anyway); `:96`
  `1323-1469`→`1334-1480`; `:117` `:321`→**`:333`** (`class
  FactExtractor`; `+11` lands on the `@runtime_checkable` line, def line
  chosen); `:198` `1234-1289`→`1245-1300`; `:201` `:1034`→**`:1045`**;
  `:203` `1300-1327`→`1311-1338`; `:214` `1418-1469`→`1429-1480`; `:230`
  `1216, 1199, 1743`→`1227, 1210, 1754`.

**curator.py companions** (holder-measured, not delta-arithmetic):
- `memory.md:99,102`, `deployment:60,70` `curator.py:160` → **`:175`**
  (`async def run_maintenance(`); `memory.md:147` `:248` → **`:263`**
  (`async def _phase_decay(`); `:64` `_PHASE_ORDER` → untouched.

No `src/**`/`tests/**` edits; no prose changed (numbers only); no other
`docs/**` file touched. COMMS.md appends (START, CLAIM) left uncommitted
for ORCH per W5-FIX-01 precedent.

## 4. Review audit (self-review as stranger's PR)

- **Correctness/root cause:** each fix lands on the line the sentence
  claims (verified by a post-edit audit printing every new cited line's
  text — all match). No blind-shifted cite was made: ranges keep identical
  old/new holder text; single-line cites name functions → def lines.
- **Invariants:** docs-only; `path:line` promise format preserved
  (including ranges/parens). Nothing in `src/` or `tests/` changed, so no
  contract moves.
- **Findings (all resolved in-phase):**
  - [MINOR] `docs/modules/AGENTS.md` does not exist (Windows
    case-folding made it look like a second copy of `agents.md`) → single
    file edited, verified via `git ls-files`.
  - [MINOR] FIX-01 report's `:446` proposal for `sandbox.md:193` does not
    match its own sentence → corrected to `:517` with rationale (§3).
  - [JUDGMENT] Pattern cites (`component-interactions:223`,
    `storage.md:85`) and `validation.md:83`, `flexability-audit:26` were
    already wrong at `0bcb62c` (pre-existing drift); re-pointed to
    sentence-true holders and disclosed as such — no holder guessing:
    each new line's text demonstrably implements the sentenced behavior.
  - Reversal check (mutation mental): reverting any single re-point puts
    its cited line back on non-holder text, so a reader (and the re-review)
    would catch it; the lock catches the blank-line subsets.
- **Independent review:** nesting unavailable in this session; mitigated by
  the independent post-edit audit above + full lock execution against the
  brief's acceptance. REV-W5b/TREV-W5b to follow per plan.

## 5. Verification evidence [RAN]

- `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q`
  → **5 passed** (`..... [100%]`), i.e. all pass (was 4 pass + 1 fail at
  HEAD before editing).
- Post-edit audit script (lock's own `all_references`/`resolve`): printed
  every new cited line's HEAD text (40 lines); each text matches the
  citing sentence's holder (defs/holders listed in §3).
- Full suite: NOT-RUN (brief §7: scoped only). Containers: none.
- Scratch audit scripts lived outside the repo
  (`%LOCALAPPDATA%\Temp\opencode\docs2_*.py/.txt`) and were deleted.

## 6. Long-run test

- **Source:** every number verified by reading holder text at HEAD
  (`runtime.py` 419/432/442/517/560/368/571/592/494/502;
  `creator.py` 1268/1533/196/1491/1631 + Plan1 regions;
  `curator.py` 175/263), not by delta arithmetic.
- **Load/Hostile/Operability/Reversal:** N/A to a docs-only re-point.
  Reversal = restore the old numbers → lock-visible failure returns for
  `sandbox.md:193`; lock-blind cites would again land on non-holder text
  for the re-review's reader.

## 7. Git state

- Branch [RAN `git branch --show-current` → `ops/20261107-integration-fix`].
- Docs commit: `cef92c2` (13 files: `sandbox.md`, `adr/0036*`,
  `component-interactions.md`, `overview.md`, `persistence-and-storage.md`,
  `memory.md`, `agents.md`, `storage.md`, `validation.md`,
  `deployment-and-runtime.md`, `documentation-audit.md`,
  `flexability-audit.md`, `Plan1.md`; explicit paths, no `add -A`).
- Report commit: separate docs-only commit (this file) listing `cef92c2`.
- COMMS.md: 2 appends (START, CLAIM), left uncommitted for ORCH.
- `docs/agent-ops/**` itself is excluded from the docs lock; no
  `src/**`/`tests/**`/`docs/plans/**` touched. Pre-existing untracked
  sibling files (`briefs/W4-*`, `reports/w4-review/*`) untouched.
