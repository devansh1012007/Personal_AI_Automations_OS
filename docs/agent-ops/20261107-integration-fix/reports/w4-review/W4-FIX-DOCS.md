<!-- ORCH-DIGEST:START -->
agent: W4-FIX-DOCS | task: W4-FIX-DOCS (R-W4-001 + F6) | status: DONE_WITH_RESIDUAL
broken_at_start: 20 (1 past-EOF + 19 blank) | broken_at_end: 50 (all orchestrator.py, FIX-01 in-flight; 0 of my 51 refs remain broken)
lock: pytest tests/conventions/test_docs_references.py -q -> ..F.. (within_file passes; not_blank fails on 50 orchestrator cites only; re-run at HEAD before FIX-01 had 2 FAILED as expected)
docs_commit: 48b9b5a | report_commit: (next commit)
last_comms_line_read: 2026-11-07T13:05:06Z ORCH -> ALL | DECISION | RESIDUAL (COMMS line 121 at start; re-read before final re-run)
<!-- ORCH-DIGEST:END -->

# W4-FIX-DOCS report

## Context
- Branch verified `ops/20261107-integration-fix` before committing [VERIFIED git branch --show-current].
- Wave-4 base `45b542d`; shifts from 0136693 (types +36 at enum end, replay +10 at _NO_PHASE_CHANGE, creator +69: +1 param +17 ctor +37 drain +13 EOF), 620ab30 (daemon +95: +3 early, +10 loops, +91 reflection), b14228f (facts +34, dialect -13).
- [RAN `.venv/Scripts/python.exe -m pytest tests/conventions/test_docs_references.py -q` at start -> 2 FAILED (within_file: dialect.py:588 past EOF 583 lines; not_blank: 19 blanks, all facts.py/daemon.py)].
- W4-FIX-01 concurrently edited (uncommitted at my final re-run, `git log --oneline -3` still at dc06ed5): orchestrator +1 import at line 46 shifting all later lines, plus allocator/graph/migrations/dashboard/runner. Final re-run therefore shows 50 NEW blanks all in orchestrator.py — residual for a DOCS2 after FIX-01 lands (W3 precedent), not fixed against uncommitted state.
- Write-set respected: 17 docs files only; no src/** or tests/** edits. Base-vs-HEAD differential used to reproduce F6; every re-point read (citing sentence + base line + current def line), never blind +N. Where sentence names a symbol, target is def line even when base line was a docstring/comment/VALUES line (W1 precedent).

## Citation mapping (old → new, one-line evidence each)
Evidence = current target line content [VERIFIED by read/grep at final numbers].

### Lock-visible (20; 1 past-EOF + 19 blank)
- docs/adr/0029:301 `dialect.py:588` → `:575` [VERIFIED dialect.py:575 `def _fts_broad(`; base was `_fts_broad` def, file now 583 lines]
- docs/adr/0029:160 `facts.py:203` → `:214` [facts.py:214 `def _trigrams(`; base :203 was docstring line inside `_trigrams`]
- docs/data/data-lifecycle.md:80 `memory/facts.py:347` → `:363` [facts.py:363 `async def upsert_entity(`]
- docs/data/data-lifecycle.md:99 `facts.py:157` → `:158` [facts.py:158 `def trigram_similarity(`]
- docs/data/data-lifecycle.md:99 `:203` → `:214` [`def _trigrams(`]
- docs/data/data-lifecycle.md:226 `daemon.py:128` → `:137` [daemon.py:137 `async def _claim_and_run(`; base :128 was its docstring]
- docs/data/data-models-and-relationships.md:138 `daemon.py:130` → `:147` [daemon.py:147 `StreamName.ORCHESTRATOR_CORE,` claim line; base :130 was `# No queue backend` comment]
- docs/data/persistence-and-storage.md:134 `daemon.py:130` → `:146` [daemon.py:146 `messages = await self._rt.queue.claim(`; same base comment, different sentence → different holder]
- docs/data/persistence-and-storage.md:160 `daemon.py:123` → `:133` [daemon.py:133 `claimed = await self._claim_and_run()`; base same]
- docs/modules/daemon.md:25 `src/paa/daemon.py:136` → `:146` [claim call; anchor for `_claim_and_run` dispatch sentence]
- docs/modules/daemon.md:34 `src/paa/daemon.py:312` → `:403` [daemon.py:403 `if report.completed_fully and report.errors == 0:`; base same]
- docs/modules/daemon.md:81 `src/paa/daemon.py:111` → `:121` [daemon.py:121 `log.error("daemon.loop_crashed"...`; base same]
- docs/modules/daemon.md:84 `src/paa/daemon.py:159` → `:169` [daemon.py:169 `log.error("daemon.ingestion_failed"...`; base same]
- docs/modules/memory.md:48 `memory/facts.py:347` → `:363` [same as data-lifecycle:80]
- docs/modules/memory.md:56 `memory/facts.py:157` → `:158`
- docs/modules/memory.md:58 `memory/facts.py:213` → `:214` [base was `def _trigrams`, cur `def _trigrams` at 214]
- docs/operations/deployment-and-runtime.md:54 `src/paa/daemon.py:136` → `:146` [same as daemon.md:25]
- docs/operations/deployment-and-runtime.md:79 `src/paa/daemon.py:312` → `:403` [same as daemon.md:34:312]
- docs/operations/deployment-and-runtime.md:81 `src/paa/daemon.py:302` → `:393` [daemon.py:393 `log.info(` opening `"daemon.curation_started"` call at :394; base :302 was `log.info(`]
- docs/reviews/documentation-audit.md:62 `facts.py:429` → `:451` [facts.py:451 `" created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"`; base :429 was same VALUES line with 8 placeholders]

### Same-sentence companions (6; same doc line or same parenthetical as a visible cite)
- docs/adr/0029:301 `dialect.py:563` → `:550` [dialect.py:550 `def _fts_phrase(`; same line as :588]
- docs/data/data-lifecycle.md:81 `:789` → `:823` [`async def add_fact(` at 823; continuation in same `(...:347, :789)` parenthetical split across lines 80–81]
- docs/modules/daemon.md:34 `src/paa/daemon.py:265` → `:356` [daemon.py:356 `dry_run = not self._curate_preview_done`; base same; same doc line as visible :312]
- docs/operations/deployment-and-runtime.md:78 `src/paa/daemon.py:265` → `:356` [same parenthetical as visible :312 on next line]
- docs/operations/deployment-and-runtime.md:81 `src/paa/daemon.py:330` → `:421` [daemon.py:421 `log.info("daemon.curation_completed"...`; base same; same line as visible :302]
- docs/reviews/documentation-audit.md:62 `daemon.py:142` → `:152` [daemon.py:152 `if cid:`; base same; same chain line as visible facts:429]

### F6 lock-blind (25 verified; brief stated 22 — +3 at data-lifecycle:40, memory.md:90, memory.md:91, all read and stale)
Types (+36 at enum end):
- docs/adr/0041-coding-specialist-pilot.md:19 `src/paa/core/types.py:424` → `:479` [types.py:479 `class AgentRole(`; base :424 was `),` end of MODALITY_PROFILES dict — already wrong at base, def per symbol rule]
- docs/glossary.md:84 `src/paa/core/types.py:375` → `:430` [types.py:430 `MODALITY_PROFILES: Final[...]`; base :375 was SPEC NOTE comment line at 411-cur; sentence names MODALITY_PROFILES → dict def]
- docs/glossary.md:86 `src/paa/core/types.py:232` → `:343` [types.py:343 `class ModalityProfile:`; base :232 was `WRITE_HOT` (Permission field) — already wrong at base; gloss's "Nothing in it is advisory" is class docstring verbatim]
Replay (+8 comment +2 members after :379):
- docs/adr/0036:77 `src/paa/ledger/replay.py:379` → `:387` [replay.py:387 `_NO_PHASE_CHANGE = frozenset(`]
- docs/adr/0037:25 `src/paa/ledger/replay.py:379` → `:387`
- docs/data/data-lifecycle.md:40 `replay.py:412` → `:422` [replay.py:422 `async def project(`]
- docs/flows/workflow-01:37 `replay.py:412` → `:422`
- docs/flows/workflow-03:39 `ledger/replay.py:412` → `:422`
- docs/modules/ledger.md:91 `replay.py:412` → `:422`
- docs/modules/ledger.md:130 `replay.py:437` → `:447` [replay.py:447 `events = await store.read_correlation(...`; base same]
- docs/reviews/documentation-audit.md:63 `replay.py:412` → `:422`
Creator (+18 before drain: +1 param/+17 ctor; drain body +37):
- docs/adr/0036:31 `src/paa/memory/creator.py:1497` → `:1515` [creator.py:1515 `async def drain_pending_embeddings(`]
- docs/adr/0036:98 `src/paa/memory/creator.py:1497` → `:1515`
- docs/data/persistence-and-storage.md:102 `creator.py:1462` → `:1480` [creator.py:1480 `await conn.execute(` UPDATE signals inside `_commit`; base same]
- docs/modules/memory.md:90 `src/paa/memory/creator.py:1225` → `:1243` [creator.py:1243 `facts_written=fact_ids,`; base same]
- docs/modules/memory.md:91 `src/paa/memory/creator.py:1497` → `:1515`
- docs/operations/deployment-and-runtime.md:101 `src/paa/memory/creator.py:1282` → `:1300` [creator.py:1300 `"memory.batch_completed",`; base same]
- docs/operations/deployment-and-runtime.md:110 `src/paa/memory/creator.py:1490` → `:1515` [sentence names `drain_pending_embeddings` → def 1515; base :1490 was `_enqueue_embeddings` enqueue line (7 lines before def) — def per symbol rule]
- docs/operations/deployment-and-runtime.md:114 `src/paa/memory/creator.py:1556` → `:1620` [creator.py:1620 `"memory.embedding_drain",` success log with batch_size/indexed/pending_count; base :1556 was `return 0` after `_failed` log (wrong event) — success log holds quoted event]
- docs/memory-polish/plans/Plan1.md:66 `creator.py:1133-1212` → `:1134-1230` [start: `self._sink = embedding_sink` (+1 param only); end: `log.info(` before `signal_processed` (+18); range covers ctor-tail through `process_signal`]
- docs/memory-polish/plans/Plan1.md:96 `creator.py:1305-1451` → `:1323-1469` [start: `ORDER BY received_at...` inside `_claim`; end: `candidate.domain,` inside `_commit`; covers `_resolve`+`_detect`+`_commit`]
- docs/memory-polish/plans/Plan1.md:198 `creator.py:1216-1271` → `:1234-1289` [start: `facts=len(fact_ids),`; end: `report.processed += 1`]
- docs/memory-polish/plans/Plan1.md:203 `creator.py:1282-1309` → `:1300-1327` [start: `"memory.batch_completed"`; end: `if rows:` inside `_claim`]
- docs/memory-polish/plans/Plan1.md:214 `creator.py:1400-1451` → `:1418-1469` [start: `# Contention is a *type*...` inside `_commit_with_retry`; end: `candidate.domain,`]
- docs/memory-polish/plans/Plan1.md:230 `[creator.py:1198, 1181, 1652]` → `[1216, 1199, 1743]` [1216 `conflicts=len(conflicts),`; 1199 `outcome="ignored",`; 1743 `log.warning("memory.signal_malformed"...` — base :1652 was docstring inside `_quarantine_malformed`, log was at 1687; order preserved (pre-existing Q,P swap), numbers re-pointed]

## Verification
- [RAN `.venv/Scripts/python.exe -m pytest tests/conventions/test_docs_references.py -q` at start -> 2 FAILED (1 within_file + 19 blanks, list matches REV-W4 R-W4-001)].
- [RAN same command at end (FIX-01 uncommitted) -> `..F..`: within_file now PASSES, names_exactly_one passes, not_blank fails on 50 orchestrator.py blanks only (FIX-01 +1 shift, see Residual). Zero of my 51 refs remain broken; all 50 other blanks are orchestrator].
- [RAN `git branch --show-current` -> `ops/20261107-integration-fix` before each commit].
- [RAN `git log --oneline -3` before final re-run -> still dc06ed5 (FIX-01 not landed); residual recorded instead of guessing against uncommitted lines].
- Self-review of full docs diff: numbers-only changes (51 refs, 17 files), no prose rewrites, no src/tests edits, no agent-ops/plans edits (COMMS.md left uncommitted for ORCH). Re-read diff line-by-line against current target contents above; each new line verified non-blank (lock proves it for visible; F6/companions verified by read/grep).
- Mutation mental frame: reverting any single re-point to its old number re-breaks the lock (old lines blank/EOF) or lands on same-content-wrong text (companions/F6: e.g. `replay.py:412` is now `events = list(events)`, not `def project`; `creator.py:1497` is now an async-generator comment, not the drain def; `types.py:232` is `PERM_WRITE_HOT`, not ModalityProfile). The F6/companions' correctness rests on the readings above, not on the lock.

## Residual / blockers
- RESIDUAL (FIX-01-caused, needs DOCS2 after FIX-01 lands): 50 blank-line orchestrator citations from FIX-01's `+1 TYPE_CHECKING import` at orchestrator.py:46 (uncommitted). Exact list from final lock run: component-interactions.md:87 (:188,:396), :98 (:1104), :215 (:467,:811), :220 (:833); overview.md:149 (:292), :161 (:467,:811), :172 (:833); modules-and-services.md:119 (:188), :121 (:292); backend/overview.md:61 (:188); request-lifecycle.md:18 (:292), :23 (:396), :24 (:295), :29 (:467), :42 (:542), :53 (:441), :66 (:494), :73 (:515), :96 (:577); data-lifecycle.md:11 (:188); workflow-01:18 (:188), :20 (:247), :23 (:263), :36 (:292), :44 (:467), :60 (:542), :90 (:515), :112 (:577); workflow-02:12 (:467), :22 (:515), :40 (:1015); api-reference.md:61 (:188), :101 (:1015); integrations:103 (:467,:811); agents.md:45 (:295), :46 (:467), :48 (:542), :49 (:441), :53 (:515), :109 (:471), :110 (:833), :141 (:247), :150 (:247); observability.md:28 (:50); flexability-audit.md:22,24,54 (ranges); documentation-audit.md:59 (:188), :63 (:1104); background-jobs:45 (:737); security:153 (:1015). Best-effort +1 would be blind +N against uncommitted state — refused. Not a code defect; the same W3 pattern (FIX-DOCS left recovery residual, DOCS2 cleared it).
- No other residual: within_file, names_exactly_one, lock_not_vacuous, catches_broken all pass. No UNRESOLVABLE (all targets found); no NEED (no src change needed).
- F6 count note: I verified 25 lock-blind for types/replay/creator vs F6's stated 22; the +3 (data-lifecycle:40 replay:412, memory:90 creator:1225, memory:91 creator:1497) are all DIFFs verified stale by reading and fixed under the same mandate. If orchestrator treats 22 as exact, the +3 are still in-scope files and stale — leaving them would leave known-wrong citations.

## Out-of-scope findings
- FINDING (wave shift, lock-blind, not fixed per §6 scope): non-blank stale cites to daemon.py/facts.py/dialect.py/cli/main.py beyond the 19 blanks (daemon shift +95, facts +34, dialect -13). Examples at base-vs-HEAD: daemon.md:22 `:119-:182`, :28 `:156`, :83 `:146`, :85 `:181`, :90 `:52-:55`; memory.md:49 `:789,:860`, :50 `:892`, :52 `:914`, :54 `:466`, :78-79 `:492,:135,:518`; 0029 dialect :121,:196,:399,:469 plus facts :214,:236-238,:308,:324,:334,:348; cli :293,:356,:636. They remain lock-green but wrong-without-broken. Needs a full semantic sweep (W1 left ~200 the same way). I did not touch them.
- FINDING (pre-existing, not wave-caused): glossary ModalityProfile `:232`→WRITE_HOT, 0041 AgentRole `:424`→dict-end, Plan1:230 cite order (Q,P,M), deployment:110 `:1490`→`_enqueue` line, deployment:114 `:1556`→failure-branch return, daemon.md:26 `:121`→`_dispatch_loop def` for the `queue is None` sentence (true holder is :141 sleep). First two fixed here as they were in F6; the rest (except the two deployment ones, also F6, fixed to def/log holders) are left as recorded.
- FINDING: `docs/plans and audits/MCP_AND_PLUGINS.md:220` `types.py:204-221` is DIFF (in-scope dir, not `docs/plans/`) but non-blank and not in F6 — left per scope, needs the sweep above.

## Long-run test answers
- Source: every new citation verified against its target line's actual content (evidence above), not arithmetic. Load/Hostile/Operability: docs-only; no runtime behaviour, no new failure modes.
- Reversal: reverting a re-point re-breaks the lock (visible) or restores same-content-wrong text (F6/companions, e.g. `replay.py:412`→`events = list(events)`).
- What was NOT verified: full test suite (scoped check only, per standing order 16); PostgreSQL/Docker paths (no impact); orchestrator residual semantics (FIX-01 not landed).
