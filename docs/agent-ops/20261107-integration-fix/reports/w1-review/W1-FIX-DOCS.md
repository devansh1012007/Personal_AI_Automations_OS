<!-- ORCH-DIGEST:START -->
agent: W1-FIX-DOCS | task: W1-FIX-DOCS (R-W1-001/003/004) | status: COMPLETE
broken_at_start: 63 (60 wave + 3 pre-existing w6-w7-w9) | broken_at_end: 0
lock: pytest tests/conventions/test_docs_references.py -q -> 5 passed
docs_commit: 789f501 | report_commit: (this commit, next)
last_comms_line_read: 2026-11-07T08:31:18Z ORCH -> ALL CORRECTION W1 (re-read before commit; FIX-CODE landed 10989e1/493034b in between)
<!-- ORCH-DIGEST:END -->

# W1-FIX-DOCS report

## Context
- Branch verified `ops/20261107-integration-fix` before committing [VERIFIED git branch --show-current].
- [RAN .venv/Scripts/python -m pytest tests/conventions/test_docs_references.py -q -> 1 failed, 63 broken citations at start; list identical to docs-lock-now.txt and REV-W1 §4. Same command at end -> 5 passed.]
- Root cause: wave-1 mid-file insertions (cli/main.py +1 import at top; config.py +7 `__all__`, +14 drain field, +110 sub-models before Settings; context_agents.py +1 import plus P-002 hydration block; runtime.py +1 comment; creator.py +7 P-002 comment). Each broken citation was re-pointed by reading its citing sentence, reading the base (294fb31) line content via `git show`, and finding the current line holding the claim by symbol/content search — never blind +N. Where the sentence names a symbol (`ensure_directories`, `dotenv_value`, `def submit`, `handle()`, …) the target is that symbol's def line, even when the base line was a decorator or an unrelated comment block (pre-existing wrong-without-broken, exposed when the wave shift landed it on a blank line).
- W1-FIX-CODE landed mid-task (code 10989e1, report 493034b, touching only context_agents.py + tests). Context citations were therefore finalised AFTER its landing; no residual context_agents citations remain. No src/** or tests/** file was edited by this agent; git diff --stat is docs-only (28 files, plus this report; COMMS.md left uncommitted for ORCH per standing orders).
- `docs-lock-now.txt` scratch file deleted as instructed.

## Citation mapping (old → new, one-line evidence each)
Evidence = current target line content [VERIFIED by grep/read at final line numbers after FIX-CODE landed].

### cli/main.py — decorator lines re-pointed to def lines (REV R-W1-001: need def, not decorator)
- docs/README.md:46, repository-map.md:118, backend/overview.md:13, request-lifecycle.md:11, workflow-01.md:10, documentation-audit.md:59 — `cli/main.py:74` → `:76` [VERIFIED cli/main.py:76 `def submit(`]
- README:47, repository-map:119, component-interactions:99 — `:290` → `:292` [def serve at 292]
- README:48, repository-map:120, backend/overview:19, ops config-env:222, ops deploy:143 — `:225` → `:227` [def doctor at 227]
- README:49, repository-map:121 — `:207` → `:209` [def recover at 209]
- README:50, repository-map:122 — `:145` → `:147` [def ledger at 147]
- backend/overview:15 — `:119` → `:121` [def status at 121]
- repository-map:123 — `:180` → `:182` [def gate at 182]
- repository-map:125 — `:353` → `:355` [def tui at 355]
- backend/overview:18, workflow-03:11 — `:191` → `:209` [sentence claims `paa recover`; base :191 was `rt = await _build_runtime()` inside gate, unrelated; def recover at 209 holds the claim]
- backend/overview:54, cli.md:36, ops deploy:46 — `:53` → `:54` [async def _build_runtime at 54]
- daemon.md:69 — `:309` → `:310` [rt = await _build_runtime(run_recovery=False) inside serve at 310, next line Daemon(rt)]
- Plan1.md:79 — `cli/main.py:68-98` → `:69-99` [range covers version decorator (69) through submit finally `await rt.close()` (99); base range was decorator 68 through close 98]

### runtime.py
- component-interactions:226, overview:96 (`:496`), sandbox.md:114, sandbox.md:151, storage.md:86 — `runtime.py:496` → `:497` [async def _acquire_sandbox at 497]

### config.py — symbol defs where sentence names the symbol; same-content where collective
- overview:195 — `config.py:59` → `:66` [SPEC DEVIATION (docs/adr/0001-0005) embedded-substrate text at 66]
- security:140 — `config.py:97` → `:115` [sentence: config stores secret names never values; :115 comment "credentials are read from these named environment variables… never stored in config or the ledger" holds it; base :97 was an S3-endpoint comment]
- context.md:86 — `config.py:133` → `:188`, `:137` → `:192` [token_ceiling at 188, worker_token_ceiling at 192; base :133/:137 were all_paths return lines]
- overview:187 — `config.py:381` → `:402`, `:460` → `:481` [collective citation list with no per-citation symbol claim; same comment text preserved: wall-clock budget at 402, "environment — and an operator who writes the obvious" at 481]
- modules-and-services:22, data-lifecycle:238, ops config-env:17, security:187 — `config.py:460` → `:896` [sentence names dotenv_value; def dotenv_value at 896, docstring "does not write to os.environ… chiefly each provider's API key" is verbatim the claim]
- modules-and-services:27, data-lifecycle:240, ops config-env:44, ops deploy:31 — `config.py:450` → `:886` [def ensure_directories at 886; base :450 was host_path_map docker comment]
- testing-and-quality:48, ops config-env:11 — `config.py:495` → `:931` [def reset_settings_cache at 931; base :495 was raise ValueError in host_path_pairs]
- models.md:138 — `config.py:491` → `:611` [escalate_at_or_above field at 611; base :491 was chunk.strip()]
- interfaces:126, background-jobs:102 — `config.py:630` → `:651` [max_delivery_attempts field at 651]

### context_agents.py (finalised after W1-FIX-CODE 10989e1 landed)
- agents.md:114, storage.md:75 — `context_agents.py:141` → `:143` [async def _hot_facts at 143]
- memory.md:142 — `:187` → `:189` [predicate = like_predicate("f.object_value", "f.predicate") at 189; sentence claims hand-rolled LIKE]
- agents.md:114 — `:241` → `:243` [async def _semantic( at 243; trio context facts/semantic/graph, so tier def is the claim]
- agents.md:114 — `:264` → `:340` [ADDITIONAL, not in broken list: same line, wave-stale wrong-without-broken; async def _graph_neighbours at 340 completes the trio; base :264 was the def, current :264 is a _semantic comment]
- request-lifecycle:33, data-lifecycle:142, workflow-01:47 — `:241` → `:376` [sentence claims PlannerContextBuilder.handle()/run; async def handle at 376 in PlannerContextBuilder (371)]
- data-lifecycle:168 — `:285` → `:427` [sentence claims WorkerContextBuilder.handle(); async def handle at 427 in WorkerContextBuilder (413); base :285 was confidence=0.9 in _graph_neighbours]

### Pre-existing w6-w7-w9 (files not touched this wave; re-pointed by claim)
- w6-w7-w9.md:71 — `cas.py:62` → `:63` [if TYPE_CHECKING: at 63; comment at 64 names base importing cas for BlobRef — the cycle-load-bearing block]
- w6-w7-w9.md:74 — `api/app.py:42` → `:43` [if TYPE_CHECKING: at 43, from fastapi import FastAPI at 44 — the optional-extra block]
- w6-w7-w9.md:134 — `tests/storage/conftest.py:55` → `:58` [name = f"paa_storage_test_{os.getpid()}_{uuid.uuid4().hex[:8]}" at 58; sentence says S3 bucket pattern mirrors this Postgres naming pattern]

## ADR-0036 (R-W1-003, R-W1-004) and ADR-0037–0043 semantic scan
- R-W1-003: creator.py:1490 → :1497 twice (0036 lines 31 and 98) [VERIFIED creator.py:1497 `async def drain_pending_embeddings(self, limit: int = 100)`; :1490 is a body line `indexed = await self._sink.enqueue(facts)`].
- R-W1-004: §P01 amended in place with one paragraph (no other 0036 content changed): the 200-char bound applies to rendered operator-console text at the CLI drain site (`src/paa/cli/main.py:635` `_MAX_DRAIN_ERROR_CHARS`, marker inside the 200 budget per cli/main.py:666-668); the runtime site (`src/paa/runtime.py:482`) is a structlog kwarg, redact-only and unbounded, mirroring `src/paa/runtime.py:474`. New citations verified non-blank and semantically exact.
- ADR-0036 other citations verified correct, unchanged: queue/base.py:48 (class StreamName), :60 (WORKERS_ALLOC), replay.py:291 (_NO_PHASE_CHANGE), models/base.py:256 (def redact), :278 (def redact_detail).
- ADR-0037–0043 scan [VERIFIED each cited line]: 0037 replay.py:291 ✓; 0038 world_model.py:42 (class BeliefDocument, members follow — acceptable class citation), :107 (def update_belief_state ✓), :89 (def path_for flat join ✓); 0041 core/types.py:424 (class AgentRole ✓), :165 (STATE_ROLLBACK_TRIGGERED ✓), :187 (TERMINAL_EVENTS ✓), replay.py:166 (STATE_ROLLBACK_TRIGGERED → ROLLED_BACK ✓); 0042 docker-compose.yml:67 (image: paa/runtime:4.1 ✓), pyproject.toml:42 (embeddings = sentence-transformers ✓); 0043 graph/base.py:68 (EDGE_TYPES ✓). No other semantically-stale ADR citation found.

## Verification
- [RAN .venv/Scripts/python -m pytest tests/conventions/test_docs_references.py -q -> ..... 5 passed] at end, after FIX-CODE landed. No residual citations (context_agents residual = 0).
- Self-review: re-read full git diff (28 docs files). Findings: none critical/major. All changes are citation numbers on existing lines plus the one intended §P01 paragraph; no prose rewrites, no src/tests edits, no agent-ops/plans edits (COMMS.md modified in working tree by appends but deliberately NOT committed — only ORCH commits COMMS).
- Mutation mental frame: reverting any single re-point to its old number makes the lock fail again (old line is blank) except the :264→:340 additional fix, which is wrong-without-broken — its correctness rests on the tier-trio reading documented above, not on the lock.

## Residual / blockers
- Residual = none. Lock fully green including the 3 pre-existing w6 cites. No NEED posted; no unresolvable citation.

## Out-of-scope findings (not fixed; for orchestrator)
- FINDING (pre-existing, wrong-without-broken, NOT wave-caused in the broken-list sense): backend/overview.md table rows run (:89), ledger (:134), gate (:164), serve (:248) and request-lifecycle run (:89) point inside previous commands' bodies, not at def run/ledger/gate/serve. They were already wrong at base 294fb31 and remain lock-green. Fixing them is a separate docs-correctness sweep, outside this brief's broken-list scope.
- FINDING (wave +1 shift, lock-blind): cli/main.py's +1 import shifts every cli citation by one; non-broken cli/config/context citations (~200 total to shifted files) are now off-by-one where the preceding line is non-blank. Only the 63 landing on blank lines were in scope. A future wave that touches these files should schedule a full semantic re-check, per REV §8.
- FINDING: docs/modules/cli.md bare line numbers (recover 206, doctor 224, serve 289, mode 336, tui 352) are not path:line citations, so the lock does not see them; they are stale by the same +1/+3 shifts. Left untouched per citation-lines-only scope.
- FINDING: docs/modules/context.md density citations (:150/:154/:160) were already wrong at base (actual fields at config.py:205/209). Left untouched (not broken).

## Long-run test answers
- Source: every new citation was verified against its target line's actual content (grep/read evidence above), not arithmetic.
- Load/Hostile/Operability: docs-only change; no runtime behavior, no new failure modes. Reversal: reverting a re-point re-breaks the lock (except :264, reasoned above).
- What was NOT verified: full test suite (standing order 16, scoped check only); non-broken citations' semantics beyond the ADR scan and the :264 same-line fix (see FINDINGs).
