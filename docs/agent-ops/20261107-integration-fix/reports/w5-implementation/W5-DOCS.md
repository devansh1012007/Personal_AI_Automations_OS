<!-- ORCH-DIGEST:START -->
agent: W5-DOCS | task: W5-DOCS wave-5 docs sweep + ADR-0044 | status: DONE
findings: 0 Stale left in the R-W4b-003 RUN list at final W5 HEAD (HEAD 5d0f17d at last re-run; lock 5 passed); ADR-0044 filed + indexed; W5 lock-visible runtime/orchestrator/daemon shifts re-pointed after W5 landed (6e1819a, f06b5bc, 3a5da25, 2de06e6, 5d0f17d)
claims_audit: ~120 re-points spot-verified by symbol/statement reads; 3 false-holders found and fixed (workflow-01 bare continuations resolving to reasoning/context_agents; persistence :740 guessed holder flagged)
band_aids_found: none (no src/tests edits, no blind +N)
top_3_risks: (1) post-commit W5 edits would re-stale this sweep — none known at final run; (2) PG half of ADR-0044 closure unrun locally (no DSN), stated in ADR; (3) extractor still blind to backtick-wrap class (agents.md:59 fixed at source; no hardening, per brief scope)
optimizations: DO-NOW: none | DO-LATER: lock extractor wrap-hardening note | NOT-WORTH-IT: none
reversal_check: docs-only, revert restores old (stale) numbers; lock re-passes either way — semantic correctness rests on the table below
confidence: high (lock 5 passed at final HEAD + per-cite reads)
<!-- ORCH-DIGEST:END -->

# W5-DOCS report — wave-5 docs sweep + ADR-0044 amendment (T: W5-DOCS)

## Context
- Branch verified `ops/20261107-integration-fix` [VERIFIED git branch --show-current].
- HEAD at start = 1d7c5bf. W5 code landed mid-task (6e1819a coding specialist, f06b5bc allocation loop, 3a5da25 cost gate, 2de06e6, 5d0f17d); sweep re-pointed against final HEAD and lock re-run there.
- COMMS read at start; last line: 2026-11-07T14:20:47Z ORCH -> ALL | DECISION | W5.
- WRITE-SET respected: paths under docs/ except docs/agent-ops/** and docs/plans/** only (includes docs/adr/0044-* + docs/adr/README.md). No src/** or tests/** edits. A re-point needing a src change was not required (no NEED).
- Required reading done in brief order: STANDING-ORDERS.md, AGENTS.md, REV-W4b §4 R-W4b-003 + §8, W4-FIX-DOCS + W4-FIX-DOCS2 enumeration reports, W4-IMPL-03 + ADR-0029 limitations (a)-(d) + prescription :551-556 + ADR-0043/0035 style, tests/conventions/test_docs_references.py.
- Enumeration: lock's own `all_references` -> 1274 refs at start; the R-W4b-003 RUN list (DOCS2 FINDING 1 ~65 + DOCS daemon/facts/dialect lock-blind + agents.md:59 invisible + W5 lock-visible) was swept semantically. Refs to files untouched by waves 4-5 and already swept by W1/W4 waves were classified Correct by prior verification and lock-green (not re-read in full — disclosed scope delta, see §Unverified).

## Sweep (a) — DONE
- Method: for each citing sentence, read the cited line at final HEAD; if the line did not hold the sentence's symbol/statement, re-point to the holder (def line for named symbols, statement line for behaviors, region endpoints for ranges). Never blind +N. Two bare-continuation traps were found where the shorthand resolves to a DIFFERENT file than the sentence claims (carried_path into reasoning.py / context_agents.py) and qualified them explicitly.
- agents.md:59 invisible class: VERIFIED lines 58-59 wrap `clear_human_\ngate()` in backticks, so `find_references` yields zero refs on line 59 (line 58's `:834` is extracted) — exactly the DOCS2 FINDING 2 mechanism. Fixed at source: wrap joined to `clear_human_gate()` (`:1042`), and `boot()` (`:1131`); both now lock-visible and lock-passing. Also verified its other stale cites (`:601`, `:708`, `:213`, `:812`, `:834`, `:670`, `:777`, `types:242`).

## Sweep table (cited file:line → re-pointed old→new; all verified at final HEAD)
Correct (no edit): all cites not listed below that were already re-pointed by W4-FIX-DOCS/DOCS2 or target unshifted files (e.g. replay.py:422 `project`, creator.py:1515 drain, replay.py:387 `_NO_PHASE_CHANGE`, facts.py:214 `_trigrams`, orchestrator.py:397→400-class sites already fixed). Lock-green at HEAD in any case.

| File:line | Old → New | Evidence (target line at final HEAD) |
|---|---|---|
| docs/adr/0029:169 | dialect.py:121 → :113 | `def fold_case` |
| docs/adr/0029:174 | dialect.py:196 → :185 | `def entity_candidates_sql` |
| docs/adr/0029:214 | facts.py:625 → :669 | `async def _fuzzy_resolve` |
| docs/adr/0029:225 | dialect.py:399/:469 → :388/:459 | ORDER BY count DESC, e.id ASC (both dialects) |
| docs/adr/0029:236 | facts.py:1117 → :717 | total-order docstring; :553 → :594 exact WHERE; :618 → :662 alias WHERE; :409 → :440 upsert lookup |
| docs/adr/0029:308 | facts.py:482 → :527 | `if _has_nul(term):` |
| docs/adr/0029:324 | facts.py:135 → :136; :508 → :553 | `_MAX_TERM_GRAMS = 400`; ceiling check |
| docs/adr/0029:334 | facts.py:506 → :551 | grams computed once |
| docs/adr/0029:348 | facts.py:1091 → :1172 | `def _clean_aliases` |
| docs/backend/request-lifecycle:15 | daemon.py:127 → :150 | `_claim_and_run` def |
| :25 | orchestrator:327 → :445 | `def _context_for` |
| :31 | :357 → :498 | CONTEXT_HYDRATION_REQUESTED emit |
| :35 | :379 → :936 | AWAITING emit in _open_gate |
| :36 | :499 → :538 | CONTEXT_HYDRATED emit |
| :46 | :408 → :551 | SIMPLE bypass check |
| :54 | :448 → :593 | PLAN_COMPILED (normal) emit |
| :58 | :461 → :604 | `def _check_policy` |
| :63 | :474 → :673 | blocked-verdict check |
| :68 | :505 → :711 | POLICY_CLEARED emit |
| :100 | :609 → :815 | `def _commit` |
| :117 | :310 → :428 | except PaaError |
| docs/backend/modules-and-services:123 | :523 → :721 | `def _execute_steps` |
| :125 | :631 → :837 | `def mediate_delegation` |
| :128 | :310 → :428 | except PaaError |
| :306 | facts:482 → :527 | NUL refusal |
| :307 | facts:135/:508 → :136/:553 | ceiling |
| :311 | memory/facts:881 → :894 | `def supersede` |
| :325 | daemon:148 → :173 | `_ingestion_loop` def |
| :478 | orchestrator:556 → :764 | VALIDATION_FAILED emit |
| docs/data/data-lifecycle:50 | types:132 → :242 | TERMINAL_EVENTS |
| :84 | memory/facts:860 → :894 | `def supersede` |
| :85 | :892 → :926 | `def supersession_chain` |
| :95 | facts:904 → :948 | `def query_facts` |
| :96 | facts:456 → :501 | `def resolve_entity` |
| :165 | orch:379 → :936 | AWAITING emit |
| :195 | orch:609 → :815 | `def _commit` |
| :207 | daemon:139 → :173 | ingestion def |
| :220 | orch:233 → :341 | queue enqueue |
| docs/flows/workflow-01:29 | orch:233 → :341 | enqueue |
| :38 | `:295` → `orchestrator.py:403` | is_terminal return (qualified: was resolving to replay.py) |
| :40 | `:327` → `orchestrator.py:445` | _context_for (qualified) |
| :55 | `:379` → `orchestrator.py:936` | AWAITING (qualified: was resolving to context_agents.py past EOF) |
| :56 | :534 → :538 | CONTEXT_HYDRATED |
| :63 | `:408` → `orchestrator.py:551` | SIMPLE bypass |
| :71 | `:441` → `orchestrator.py:584` | max_plan_nodes check (qualified: was resolving to reasoning.py) |
| :72 | `:448` → `orchestrator.py:593` | PLAN_COMPILED (qualified) |
| :76 | :461 → :604 | _check_policy |
| :84 | :673 → :673* | blocked check: old 673 happened to equal HEAD holder (no-op) |
| :85 | :909 → :936 | AWAITING emit |
| :86 | :709 → :711 | POLICY_CLEARED |
| :116 | :609 → :815 | _commit |
| docs/flows/workflow-02:17 | :461 → :604 | _check_policy |
| :20 | :909 → :936 | AWAITING emit |
| :33 | :380/:497 → :934/:936 | _open_gate emit (await/EventType) |
| :34 | :712 → :1098 | HUMAN_GATE_CLEARED/REJECTED emit |
| :35 | :712 → :1098 | same |
| :41 | `:703` → `:1077` | PaaError phase check |
| :43 | `:712` → :1098 | gate emit |
| :46 | :699 → :1042 | clear_human_gate def |
| :113 | :703 → :1077 | same |
| :115 | :699 → :1042 | same |
| docs/modules/agents.md:50 | `:461` → `:604` | _check_policy |
| :52 | `:505` → `:711` | POLICY_CLEARED |
| :55 | `:108` → `:214` | max_step_retries default |
| :56 | `:609` → `:815` | _commit |
| :58 | `:631` → `:837` | mediate_delegation |
| :59 | `:688` → `:1042`; `:737` → `:1131` | invisible wrap fixed at source |
| :133 | :474 → :673 | blocked check |
| :134 | :570 → :780 | exhausted raise |
| :142 | types:187 → :242 | TERMINAL_EVENTS |
| docs/modules/daemon.md:22 | :119–:182 → :144–:210 | dispatch..maintenance loops |
| :26 | :121 → :154 | queue-None sleep |
| :28 | :156 → :173 | ingestion def |
| :29 | :165 → :188 | drain call |
| :32 | :200 → :193 | maintenance def |
| :36 | :286 → :349 | _run_curation_tick def |
| :40 | :102 → :125 | _supervise def |
| :44 | :81 → :104 | stop def |
| :46 | :92 → :115 | run_forever def |
| :48 | :186 → :335 | _memory_creator def |
| :66 | :48 → :53 | class Daemon |
| :67 | :156 → :173 | ingestion def |
| :68 | :200 → :193 | maintenance def |
| :85 | :181 → :209 | maintenance_failed log |
| :90 | :52–:55 → :56–:65 | __init__ intervals |
| docs/modules/memory.md:49 | :789/:860 → :894 | supersede def |
| :50 | :892 → :926 | chain def |
| :52 | :914 → :948 | query_facts def |
| :54 | :466 → :501 | resolve_entity def |
| :78 | :492 → :527 | NUL refusal |
| :79 | :135/:518 → :136/:553 | ceiling |
| :84 | dialect:121 → :113 | fold_case |
| :92 | daemon:165 → :188 | drain call |
| :99 | curator:154 → :160 | run_maintenance def |
| :102 | curator:154 → :160 | same dry_run |
| :141 | daemon:148 → :173 | ingestion def |
| :147 | curator:141 → :248 | _phase_decay def |
| :160 | facts:492 → :527 | NUL refusal |
| docs/runtime/background-jobs:12 | daemon:45 → :53 | class Daemon |
| :17 | :64 → :91 | start def |
| :18 | :127 → :144 | dispatch def |
| :19 | :139 → :173 | ingestion def |
| :20 | :151 → :193 | maintenance def |
| :23 | :94 → :125 | supervise def |
| :29 | :70 → :104 | stop def |
| :30 | :81 → :115 | run_forever def |
| :33 | :127 → :150 | _claim_and_run def |
| :36 | :134 → :170 | nack call |
| :40 | :121 → :154 | queue-None sleep |
| :100 | orch:523 → :721 | _execute_steps |
| :120 | :100 → :134 | loop_crashed log |
| :121 | :134 → :170 | nack |
| :122 | orch:310 → :428 | except PaaError |
| :123 | orch:556 → :764 | VALIDATION_FAILED |
| docs/data/persistence:83 | daemon:139 → :173 | ingestion def |
| :134 | orch:233/daemon:146 → :341/:159 | enqueue/claim |
| :160 | orch:230/daemon:133 → :296/:154 | submit/queue-None sleep |
| :178 | orch:740 → :1131 | boot (recovery authority; best holder, flagged) |
| docs/data/data-models:138 | orch:233/daemon:147 → :341/:159 | enqueue/claim |
| :194 | facts:278/:252 → :294/:268 | Fact/Entity classes |
| docs/reviews/documentation-audit:60 | orch:301–:309 → :471–:815 | hydrate..commit range |
| :66 | orch:631 → :837 | mediate_delegation |
| docs/modules/storage:76 | facts:342 → :363 | upsert_entity def |
| docs/modules/context:64 | orch:374 → :471 | _hydrate def |
| docs/interfaces/integrations:103 | :468–:812 → :471–:815 | hydrate..commit |
| :118 | cli:316 → :307 | serve def |
| :124 | orch:108 → :214 | max_step_retries |
| docs/flows/workflow-03:12 | daemon:64 → :91 | start def |
| :138 | daemon:127 → :144 | dispatch def |
| :140 | orch:519 → :726 | resume check |
| docs/glossary:48 | types:132 → :242 | TERMINAL_EVENTS |
| :65 | types:67 → :122 | EventType class |
| :72 | types:149 → :259 | Permission class |
| :75 | types:169 → :279 | PermissionMode class |
| docs/README:47 + repo-map + backend/overview + component-interactions | cli:293 → :307; cli:356 → :370 | serve/tui defs |
| docs/interfaces/api-reference:61 | orch:293 → :296 | submit def |
| docs/memory-polish/plans/Plan1:65 | daemon:142-152 → :173-191 | ingestion range |
| :94 | daemon:142 → :173 | ingestion def |
| :101 | facts:914 → :948 | query_facts |
| :102 | facts:969 → :1003 | search_facts_by_text |
| :119 | daemon:189 → :335 | _memory_creator |
| :214 | daemon:142-152 → :173-191 | ingestion range |
| docs/operations/deployment:51 | daemon:48 → :53 | class Daemon |
| :52 | :72 → :91 | start def |
| :54 | :146 → :159 | claim call |
| :56 | :156 → :173 | ingestion def |
| :57 | :165 → :188 | drain call |
| :59/:70 | :200 → :193 | maintenance def |
| :60/:70 | curator:141 → :160 | run_maintenance |
| :64 | :102 → :125 | supervise def |
| :65 | :81 → :104 | stop def |
| :73 | :176 → :199 | maintenance sleep |
| :78 | :356/:403 → :414/:461 | dry_run/complete flag |
| :81 | :393/:421 → :451/:479 | started/completed logs |
| :85 | :321 → :379 | R3 safety |
| :89 | :273 → :422 | archive_writer |
| :112 | :165 → :188 | drain call |
| docs/plans and audits/flexability:22/:54 | :397-440 → :400-443 | run body |
| :29 | :640 → :837 | mediate_delegation |
| docs/plans and audits/w6-w7-w9:103 | orch:319 → :422 | post-policy phase check |
| docs/architecture/overview:96 | runtime:412/:425/:435/:509 → :414/:427/:437/:511 | _try_* + sandbox |
| :149/:161/:172 | orch:397/:468/:834 → :400/:471/:837 | run/hydrate/mediate |
| docs/architecture/component-interactions:87/:215/:220 | :397/:468/:834 → :400/:471/:837 | run/hydrate/mediate |
| :97 | runtime:109/:132 → :110/:133 | RouterModelAdapter/complete_structured |
| :102 | runtime:76 → :77 | CriticValidatorAdapter |
| docs/modules/models:18/:104 | runtime:109/:132 → :110/:133 | same |
| docs/modules/observability:27 | runtime:54 → :55 | log line |
| docs/modules/agents:111 | runtime:98 → unchanged | RouterModelAdapter wrap: 98 non-blank, correct (verified Correct, no edit) |
| :114/:118 | runtime:407 → :554 | _build_agents |
| :133 | orch:474 → :673 | blocked check |
| :134 | orch:570 → :780 | exhausted raise |
| docs/modules/ledger:52/:164 | types:132 → :242 | TERMINAL_EVENTS |
| docs/modules/cli:38 | daemon:64 → :91 | start def |
| docs/backend/overview:66 | types:169 → :279 | PermissionMode |
| docs/data/data-lifecycle:126 | README:214 → :213 | OFFSET sentence |
| :181 | runtime:66 → :77 | CriticValidatorAdapter |
| docs/data/persistence:154 | runtime:394–:400 → :414–:424 | _try_vector |
| :101/:112 | runtime:351/:364 → :414/:427 | _try_vector/graph |
| docs/operations/deployment:37 | :394–:400 → :414–:424 | _try_vector |
| :39 | :407 → :554 | _build_agents |
| :40 | :382 → :511 | _acquire_sandbox |
| :41 | :302 → :363 | orchestrator assembly |
| :43 | :331 → :264 | boot_recovery def |
| docs/reviews/documentation-audit:59 | runtime:208 → :153 | Runtime class |
| :61 | runtime:132 → :133 | complete_structured |
| :67 | runtime:382 → :511 | sandbox probe |

- Unresolvable: 0 (every stale target was found). NEED: 0 (no src change needed).
- Delta vs "~65": the RUN list at W5 HEAD was larger than W4's 65 because W5 landed mid-task (orchestrator +3/+23, daemon +5..+58, runtime +11, curator +82, cli shifts) and because the sweep also covered types/daemon/facts/dialect/runtime/cli lock-blind cites beyond orchestrator.

## Amendment (b) — DONE
- `docs/adr/0044-stored-normalisation-closes-adr-0029.md` filed amending ADR-0029: stored `canonical_name_norm`/`aliases_norm` (facts `upsert_entity` via `_aliases_norm_value` `src/paa/memory/facts.py:247`), Python v6 backfill (never SQL lower()), dialect reads (`368`-class ORDER BY + count proxy), FTS norms, graph second writer (fold_case, DO UPDATE preserves aliases_norm), limitations (a)(c)(d) closed, (b) converges identically, three LIKE residuals named (search_facts_by_text, gatherer, skills). Context/Decision/Alternatives/Consequences per ADR-0043 style. Every new `path:line` cite read before commit and lock-passing.
- Index row in `docs/adr/README.md` in existing format; "files on disk ... 0036–0044" updated.
- 0044 re-globbed free at write time [VERIFIED glob docs/adr/ — highest was 0043].

## Lock-visible (c) — DONE (W5 landed; residual: none known)
- W5 commits that shifted lines: 6e1819a (orchestrator +3/+23), f06b5bc (daemon allocation loop +14/+58), 3a5da25 (curator +82, metrics), 2de06e6, 5d0f17d. All cited lines re-pointed to final HEAD; final lock re-run after the last W5 commit (5d0f17d) covers them.
- Residual list: none at final HEAD. If a sibling commits after this report, cites to orchestrator.py/daemon.py/runtime.py/curator.py lines at/after their insertion points are the residual class (DOCS2 precedent).

## Verification
- [RAN `.venv/Scripts/python.exe -m pytest tests/conventions/test_docs_references.py -q` at start -> 5 passed]
- [RAN same command at end (final W5 HEAD) -> 5 passed]
- ruff/mypy untouched-by-me (docs-only — state so; not run).
- Self-review: every hunk numbers-only (plus ADR-0044 + index + wrap join); no src/**, tests/**, docs/agent-ops/**, docs/plans/** edits by me (COMMS.md left for ORCH).

## Unverified Assumptions & Remaining Risks
- The full 1274-ref corpus was not line-read: refs to files untouched by waves 4–5 rely on the lock (non-blank + in-range) + prior-wave verification. A wrong-but-non-blank cite in an untouched file would pass undetected — the same exposure R-W4b-003 names, now reduced to untouched files.
- `docs/data/persistence-and-storage.md:178` (`orchestrator.py:740` → `:1131` boot) is a best-holder choice: the sentence claims ledger-authoritative recovery and the deleted holder's identity could not be recovered; flagged here rather than upgraded to certain.
- PG gate for ADR-0044's closure (A3-PG) remains CI-only (no DSN here); stated in the ADR.
- Extractor hardening for the backtick-wrap class is recorded here (DOCS2 FINDING 2 mechanism verified at lines 58-59; wrap fixed at source) but not implemented — out of write-set (tests/** forbidden).

## Git state
- Branch: `ops/20261107-integration-fix` (verified before each commit).
- Docs commit: `1268d54` (35 files incl. new docs/subagent file docs/adr/0044-stored-normalisation-closes-adr-0029.md; docs-only).
- Report commit: this commit (docs-only, lists 1268d54).
- Pre-existing uncommitted COMMS.md and untracked run-trail files left alone (never staged).
