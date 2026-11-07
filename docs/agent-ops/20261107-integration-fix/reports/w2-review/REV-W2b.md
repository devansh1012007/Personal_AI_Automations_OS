<!-- ORCH-DIGEST:START -->
agent: REV-W2b | scope: wave-2 fixes (f009484 code+tests, 8d39e5c docs) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 0 | MINOR 3 | NIT 2
claims_audit: 24 checked / 3 unsupported-or-false (1 count, 1 same-content labelling, 1 UNRESOLVABLE overstatement)
band_aids_found: none (no swallowing/mocking/suppression; fresh TierPolicy() per call is a documented decision, not a workaround)
top_3_risks: docker build still CI-only (docker absent here) | PG v5 migration still CI-only (no DSN) | docs lock-blind semantic residuals (6 citations land on non-blank but wrong lines)
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WITH-IT 1
reversal_check: done (probes + test-logic proof; no src revert executed, read-only mandate)
confidence: high (all 3 prior blockers closed by executed probes/tests; docker/PG halves honestly NOT-RUN)
<!-- ORCH-DIGEST:END -->

# Code review — REV-W2b (independent re-review of wave-2 fixes)

## 0. Method

Diff range reviewed: `c42f4ae..HEAD` (f009484 code+tests, 8d39e5c docs, 0546308+492d4b2 report commits). Blind pass first on the full code/test diff + surrounding source (Dockerfile:246,249 USER chain, `memory/domains.py` TierPolicy, `storage/vector/embeddings.py` lazy load, `_arrival_seq_step` PG shape, `test_vector.py` header/guard, ci.yml embed header, Dockerfile:65 wheelhouse groups) and a semantic spot-read of ≥30 re-pointed docs citations (reading the cited line, not just non-blank). Implementer reports W2-FIX-01 + W2-FIX-DOCS read only after blind drafting. Prior findings REV-W2 §§4-5 and TREV-W2 §§5-6 read first. COMMS last line read at start: line 80 (W2-FIX-01 DONE). Read-only review: no source edited, no commit; this report is the only file written.

Commands run by me [RAN]:
- `ruff check src tests` -> All checks passed (0)
- `mypy src` -> 3 errors, all `sandbox/subprocess_backend.py:548` baseline (killpg/getpgid/SIGKILL on win32) — unchanged
- W2-FIX scoped (touch + tier_vocab + tier_replay + tier_migration + deployment embed + vector T06) -> 67 passed, 2 skipped (69 collected: 9+38+6+10+5+1) [RAN, exit 0]
- `test_schema_parity` (6 collected) + `test_import_conventions` (21 collected) + `test_docs_references` (5 collected) -> all passed [RAN, exit 0]
- Skip reasons confirmed: T06 `sentence-transformers is not installed`, PG `needs the 'postgres' extra and PAA_TEST_POSTGRES_DSN` [RAN -rs]
- Probes (python -c): tier fold (T9/t2/empty/None -> T2; float/NaN/inf fact_id junk keys; bool -> no-op), `from paa.memory import TierPolicy` OK, yaml jobs `['gate','embed-image']`, `Get-Command docker` -> absent
- Blank-citation reconstruction via lock's own `find_references` over `git show <rev>:<path>` for both docs and src: c42f4ae -> 42 blank, f009484 -> 51 blank, 8d39e5c -> 0 blank [RAN]

## 1. Blind-pass findings (before reading implementer reports)

1. **Dockerfile.embed USER chain is correct.** `USER root` at Dockerfile.embed:86 sits after the COPYs (74-75) and before the install RUN (100-116); `USER paa` at :120 is the last instruction. Base runtime ends `USER paa` (Dockerfile:249) over root-owned `/opt/venv` (Dockerfile:246 `COPY --from=builder`, builder creates it as root) [VERIFIED]. The trap comment (embed:77-85) names both lines and forbids "tidying" — root-cause fix, not a workaround. Residual: COPYs still run as inherited `paa`, so `/build` is paa-owned and `rm -rf /build` as root is fine; no ownership bug introduced.
2. **Tier fold now shares the canonical validator, no cycle.** `replay.py:28` imports `TierPolicy` from `paa.memory.domains` at runtime; `domains.py:22-24` imports stdlib only (enum, dataclasses, typing) [VERIFIED], so no circular import — and the fresh-interpreter sweep passes (import_conventions 21) [RAN]. `TierPolicy().resolve` (domains.py:317-326) is the single vocabulary: unknown/None/case-mismatch -> T2. Fresh instance per tier event (replay.py:365) is O(5-entry dict) per event — negligible, and justified in the comment by R-W2-006 mutability. No VALID_TIERS duplication.
3. **falsy/non-scalar fact_id no-op works, with one hole.** `not fact_id or isinstance(bool) or not isinstance((str,int,float))` (replay.py:350-354) correctly no-ops `""`/`0`/`False`/`None`/list/dict/`True` [RAN probe]. But `float` is admitted: `1.5 -> {'1.5': 'T1'}`, `nan -> {'nan': ...}`, `inf -> {'inf': ...}` [RAN probe] — junk keys of the same class R-W2-103 exists to kill, while the comment (replay.py:347-349) says "integer ids". See R-W2b-001 [MINOR].
4. **TierPolicy export is complete and symmetric.** Added to both the `paa.memory.domains` import (memory/__init__.py:56) and `__all__` (:90, alphabetical position between SignalEnvelope and WorldModel) [VERIFIED]; `from paa.memory import TierPolicy` works [RAN].
5. **ci.yml rewording is accurate.** New header says torch is downloaded once for the base build (gate's `all` shape) and once into the derived image, noting the base wheelhouse excludes embeddings per Dockerfile:65 [VERIFIED Dockerfile:65 groups = vector,api,redis,postgres,s3,otel — no embeddings]. The phrasing "once for the base build (the gate's `all` shape...)" slightly conflates the image build with the CI `all` pip leg, but the parenthetical carries the correction; R-W2-008 closed.
6. **Migrations docstring correction is accurate.** Verified against `_arrival_seq_step` (migrations.py:616,696-743): its PostgreSQL half is a single `GuardedStatement` with `schema_scoped=True` covering all six statements, no separate data guard [VERIFIED]. The new `_tier_step` docstring (migrations.py:948-958) states exactly that contrast, keeps the single-schema caveat, and changes no behaviour. R-W2-004 closed as wording-only.
7. **Static Dockerfile test is good but has a comment-pollution hole.** `test_pip_check_is_the_last_pip_command` collects every line containing `"pip "`, which includes comments at Dockerfile.embed:26 (`` `pip install --no-index` ``), :44 (`` `pip check` ``), :91 (`pip considered`) [VERIFIED grep]. The `any("pip install" ...)` leg is therefore satisfiable by comment :26 alone — deleting the real install RUN would not fail that leg. The last-line leg still holds (real `pip check` at :115 is the last pip-containing line), and FROM/USER/retry legs are sound. See R-W2b-002 [MINOR].
8. **T06 test is skip-safe and cheap.** Marker `requires_embeddings` is registered (pyproject.toml:97) [VERIFIED]; `skipif(not HAS_SENTENCE_TRANSFORMERS)` uses the file's existing `find_spec` guard (:43), no module-scope heavy import, function-local `SentenceTransformerEmbedder` import [VERIFIED test_vector.py:1048-1065]. `SentenceTransformerEmbedder.__init__` does not load weights (embeddings.py:119-148: "Constructing this class must stay free"), so the test proves factory selection + `dimensions == 384` (config default 384, config.py:633) without a model download — correct scope for T06; A5b parity remains deferred. Skips honestly here [RAN -rs].
9. **Docs re-point: the large jumps are semantically right; three +1 anchors preserve pre-existing wrongness.** Spot-verified ≥30 citations by reading the target line: `cli/main.py:77/106/293/356` are `def submit/run/serve/tui`, `replay.py:412` is `async def project`, `:379` is `_NO_PHASE_CHANGE`, `:171` is `def apply_event`, `api/app.py:54/71/94/295` are `def create_app/health/submit_task/_admit`, `runtime_tools.py:85` is `def build_runtime_tools`, `domains.py:76/100/138/251/261` are the named defs, `context_agents.py:446` is `if packet.vacant_slots:` (the hydration_required gate, :446-452) [RAN line dump]. The three failures are listed in §1 of claims audit / R-W2b-003: `api/app.py:180` (duplicate flag), `api/app.py:200` (backpressure gate), `context_agents.py:296` (builder classes) all land on non-blank lines that do not hold the cited claim — and all three were already wrong at wave-2 base (verified at 8faa64e: :179 was `if repo is None:`, :199 was the section comment, :295 was `initial = ...`), so FIX-DOCS preserved, not created, them.
10. **Guard-level migration test genuinely closes R-W2-106.** `test_v5_step_guards_are_satisfied_on_a_v5_database` evaluates every v5 SQLite guard from `MIGRATIONS` against a v5 DB and asserts no row — an always-true guard returns a row and fails [read]. No transport-bound runner needed; raw sqlite3 matches the file's existing construction.

## 2. Claims audit

| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| FIX-01: USER root before install, USER paa after, trap comment | YES | Dockerfile.embed:77-86,118-120 [VERIFIED]; static test locks ordering [RAN 5 passed] |
| FIX-01: fold shares TierPolicy.resolve; T9/garbage/empty -> T2; no cycle (domains has no paa imports) | YES | replay.py:28,358-365; domains.py:22-24 [VERIFIED]; probes T9/t2/''/None -> T2 [RAN]; import_conventions 21 [RAN] |
| FIX-01: falsy + non-scalar fact_id no-op, totality preserved, int still coerces | YES (with float hole) | replay.py:350-356; probes [RAN] — but float/NaN/inf junk keys remain (R-W2b-001), comment claims "integer ids" only |
| FIX-01: TierPolicy export, `from paa.memory import TierPolicy` works | YES | memory/__init__.py:56,90 [VERIFIED]; probe [RAN] |
| FIX-01: migrations docstring wording-only, ci.yml comment-only | YES | migrations.py:943-958 diff is docstring text; ci.yml diff is comment text; yaml jobs parse [RAN] |
| FIX-01: 67 passed, 2 skipped (PG + T06) | YES | 69 collected (9+38+6+10+5+1), 2 skips with confirmed reasons [RAN] |
| FIX-01: ruff 0, mypy 3 baseline, parity 6, import 21 | YES | all [RAN]; counts match collect output |
| FIX-01: docs lock 44 -> 48 blank (+4 from replay shifts) | FALSE (detail) | Reconstruction: 42 at c42f4ae, 51 at f009484 [RAN script] — +9, not +4; REV-W2's 44 also off by 2 from 42. Direction + ownership correct; counts approximate. End state 0 verified by both script and pytest. R-W2b-004 [NIT] |
| FIX-01: docker absent, build NOT-RUN honestly stated | YES | Get-Command docker -> absent [RAN]; never claimed otherwise |
| FIX-01: no band-aids (no noqa/type:ignore; setattr/B010 anecdote) | YES | diff contains no noqa/type:ignore/setattr [VERIFIED grep of diff]; anecdote reasoned |
| FIX-01: reversal list (USER/resolve/redact/call-site/guard) | YES | each test's assertion logic read; tier reversal REV-proven pre-fix; USER/guard/call-site legs are construction-true |
| FIX-DOCS: lock 5 passed at end, 0 blank | YES | 5 collected [RAN exit 0]; reconstruction 0 at 8d39e5c [RAN script] |
| FIX-DOCS: broken_at_start 42 | YES | reconstruction at c42f4ae = 42 [RAN script] — exact |
| FIX-DOCS: "never blind +N; every citation located by reading, symbol def never decorator" | FALSE (detail, 3 citations) | persistence `api/app.py:179->:180` labelled "same-content" for the duplicate-flag sentence (line is `if repo is None:`; claim lives at :197/:182-183); persistence/api-ref `:199->:200` labelled same-content for the backpressure-gate sentence (line is the section comment; `_admit` def is :295); component-interactions `context_agents.py:295->:296` labelled same-content for the Planner/Worker class row (line is `initial = ...` inside `_semantic`; classes are :372/:414). All three pre-existing at base (§1.9) — preserved, not created — but the per-citation evidence labels overclaim. R-W2b-003 [MINOR] |
| FIX-DOCS: large def moves (cli run:89->106, serve:248->293, tui:307->356, replay project:322->412, _admit:87->295, build_runtime_tools:47->85, vacant_slots:303->446) | YES | each target line read and holds the claim [RAN line dump §1.9] — these are the substantive repairs and they are correct |
| FIX-DOCS: ADR scan (0036/0037 replay:291->379, cli:635->636, 0041 replay:166->167) | YES | replay.py:379 `_NO_PHASE_CHANGE`, cli/main.py:636 `_MAX_DRAIN_ERROR_CHARS`, replay.py:167 ROLLED_BACK mapping [VERIFIED] |
| FIX-DOCS: sequencing (FIX-01 landed mid-task; replay finalised after f009484) | YES | commit order f009484 (15:19) -> 8d39e5c (15:38) [VERIFIED git log]; final lock green against committed tree [RAN] |
| FIX-DOCS: 3 UNRESOLVABLE continuations (workflow-01 :295/:327/:379) + agents.md:90 | OVERSTATED | The targets exist and are locatable: orchestrator `run` :396, `is_terminal` early-return :399, `_context_for` :441, HARD_STOP park :516 [VERIFIED]; context_agents:379 is `declared = ...` (not the park). "UNRESOLVABLE" really means "re-targeting changes the paragraph's carried file — needs orchestrator decision." Honestly flagged as residual, but the label overstates difficulty. R-W2b-003 |
| FIX-DOCS: no src/tests/agent-ops/plans edits; diff is citation numbers only | YES | git show 8d39e5c --stat = 34 docs files, 138+/138- [VERIFIED]; one duplicate `_admit` continuation collapsed (disclosed) |
| FIX-DOCS: out-of-scope FINDINGs (agents.md:90, cli.md:56/:48 bare-number staleness) | YES | agents.md:90 `:236`/`:271` read — inside method bodies, classes at :372/:414 [VERIFIED]; bare numbers not lock-visible, correctly flagged not fixed |

24 claims checked, 3 unsupported-or-false details (count, same-content labelling, UNRESOLVABLE label) — none affects a code fix; all three are reporting-precision issues on docs work whose end state (lock green) is verified.

## 3. Long-run test (independent answers)

- **Source**: tier vocabulary now has one validator (TierPolicy.resolve) shared by fold and policy; DB CHECK remains the writer-side enforcer. USER chain restores the base image's contract at the source (derived image declares its own user transition, with a do-not-tidy comment). Docs lock is green by claim-reading for the def-anchored citations.
- **Load (100x/concurrency/partial)**: fold adds one 5-entry dict construction per tier event — O(1), no state, no concurrency surface. Guard test proves v5 re-execution is a no-op at guard level. Dockerfile retry budget unchanged. No new queues/loops.
- **Hostile**: T9/garbage/empty/case-mismatch tiers -> T2 (rebuilt == live CHECK-legal); falsy/non-scalar/bool fact_id -> no-op; secret-shaped touch exception -> redacted log (pinned); unpinned FROM / dropped retry / offline flag each fail the static lock (except the comment-pollution leg, R-W2b-002). Float fact_ids remain a junk-key vector (R-W2b-001) — dict pollution only, no crash/SQL.
- **Operability**: v5 migration behaviour unchanged (docstring only); rollback unchanged (ADR-0037 dry_run-off). Embed image rollback = do not build/use derived image. Docs residuals (R-W2b-003) mislead a reader following those 6 citations but break no gate — same class the lock itself documents as human-work-only.
- **Reversal**: revert USER root -> static USER test fails (presence + ordering asserted). Revert resolve-sharing -> T9 test fails (REV-proven verbatim recording). Revert falsy check -> falsy test fails (junk `""` key). Remove `redact()` in touch -> redaction test fails (token present). Inline touch_many at one surface -> call-site AST test fails (import+call both asserted). Break a v5 guard to always-true -> guard test fails (row returned). Revert any def re-point -> lock re-blanks where old line is blank; claim-based def moves revert to decorator/body lines (lock-blind, reasoned by the readings in §2).

## 4. Findings

### R-W2b-001 [MINOR] Float fact_id admitted as junk dict key, contradicting the new comment
- Location: `src/paa/ledger/replay.py:350-354` · Evidence: [RAN probe -> `1.5 -> {'1.5': 'T1'}`, `nan -> {'nan': ...}`, `inf -> {'inf': ...}`] while comment (:347-349) says scalar ids are "strings, and integer ids coerced". Why it matters: same junk-key class R-W2-103 was raised to kill; a rebuilt tier dict can hold `"nan"`/`"inf"` keys a sweeper would inherit. Totality holds (never raises), no SQL path — data-quality only. · Recommended fix: drop `float` from the admitted tuple (keep `str, int`), or document float coercion as deliberate with a pinning test. One-line.

### R-W2b-002 [MINOR] Static Dockerfile lock's pip-install leg is satisfiable by a comment
- Location: `tests/deployment/test_dockerfile_embed.py` (`test_pip_check_is_the_last_pip_command`) vs `Dockerfile.embed:26,44,91` · Evidence: [VERIFIED grep] three comment lines contain `"pip "`; the test's `pip_lines` includes them, so `any("pip install" ...)` passes via comment :26 even if the RUN install were deleted. The pip-check-last leg still holds (real check at :115 is last). Why it matters: the lock's purpose is to catch a dropped install before CI; this leg cannot. · Recommended fix: collect pip lines only from non-comment lines (or RUN-continuation lines), e.g. `if "pip " in line and not line.lstrip().startswith("#")`.

### R-W2b-003 [MINOR] Six docs citations remain semantically wrong (lock-blind), three mislabelled as same-content
- Location: persistence-and-storage.md / api-reference.md (`api/app.py:180` duplicate flag; `api/app.py:200` backpressure gate), component-interactions.md (`context_agents.py:296` builders), workflow-01-submit-and-run.md (`:295` is_terminal, `:327` _context_for, `:379` park) · Evidence: target lines read [RAN §1.9]; correct lines are api/app :197/:295, context_agents :372/:414, orchestrator :399/:441/:516 [VERIFIED]. Why it matters: the lock is green, so these survive silently — exactly the "wrong without broken" class test_docs_references.py:53-62 says only a reader can catch. All six are pre-existing (verified at 8faa64e base), so this is preservation, not regression; FIX-DOCS honestly flagged the workflow-01 three + agents.md:90 as residual (though "UNRESOLVABLE" overstates — targets exist, §2) but labelled the first three "same-content". · Recommended fix: orchestrator task to re-target the six (continuations become full `orchestrator.py:NNN` / `api/app.py:NNN` cites); no code change.

### R-W2b-004 [NIT] Blank-count reporting was approximate in REV-W2 (44) and FIX-01 (48)
- Location: REV-W2.md §0/§5, W2-FIX-01.md §3-4 · Evidence: [RAN reconstruction -> 42 at c42f4ae, 51 at f009484, 0 at 8d39e5c]. DOCS's 42 matches exactly. Why it matters: counts are the gate's language; approximate counts erode trust in exact ones. End state (0) is verified both ways, so no action beyond noting.

### R-W2b-005 [NIT] Fresh `TierPolicy()` per tier event + DEFAULT_TIER still in three places
- Location: replay.py:365, replay.py:295 vs domains.py:315 vs SQL `'T2'` · Evidence: [VERIFIED]. Per-call construction is trivially cheap and explicitly justified by R-W2-006 mutability; unifying DEFAULT_TIER is the pre-existing DO-LATER (REV-W2 opt #2), not introduced here. No action required; recorded so a future "optimisation" does not reintroduce shared mutable state.

## 5. Attempts to break it

- Tier fold hostile sweep [RAN]: T9/t2/empty/None/non-string to_tier -> T2; `""`/0/False/None/list/dict/True fact_id -> no-op copy; int 123 -> `"123"` preserved; float/NaN/inf -> junk keys (R-W2b-001, the one survivor). Non-tier event after archive still does not resurrect (existing test, passed [RAN]).
- Import cycle probe: fresh `import paa.ledger.replay` + `from paa.memory import TierPolicy` in one interpreter [RAN] — no cycle; sweep (21, subprocess fresh interpreters) passes [RAN].
- Static lock mutation reasoning: delete `USER root` -> presence assert fails; swap USER order -> ordering assert fails; delete retry/PIP vars -> presence asserts fail; insert `--no-index` into RUN -> non-comment assert fails. Delete install RUN only -> pip-install leg survives on comment (R-W2b-002, confirmed by reading the collector, not by editing the file).
- Guard mutation reasoning: always-true guard (`SELECT 1`) returns a row on v5 DB -> `assert row is None` fails. Runner-level test alone would not catch it (R-W2-106's point, now closed).
- Touch mutation reasoning: remove `redact()` -> token appears verbatim in captured entry -> redaction test fails; inline `touch_many` at one surface -> that surface's import OR call leg fails. Import via `from paa.memory import touch_recalled` would false-fail the lock (brittleness, noted — current tree uses the deep import at all 4 sites [VERIFIED grep], so no false failure today).
- Docs: reverted-anchor reasoning per FIX-DOCS §Verification — blank-class reverts re-break the lock; def-move reverts are lock-blind but were verified by reading (§2). Docker build and PG migration: NOT-RUN (docker absent, no DSN) — I do not claim either passed; USER fix is by deterministic Dockerfile-text proof, same standard REV-W2 applied to the original finding.

## 6. Dimension checklist

- Correctness: FIXED (R-W2-002 USER, R-W2-003 tier vocabulary, R-W2-005 export — all probe/test-proven) · Root cause vs symptom: fold shares the validator (root cause was split validation); USER fix at the image-contract level; docs fixed by claim-reading for def anchors · Regression/compat: no prod regressions; scoped 67 + parity/import/docs suites green [RAN]; mypy 3 baseline unchanged, ruff 0 · Concurrency/idempotency: guard-level idempotency now proven; fold pure/idempotent; touch batch unchanged · Error handling: totality preserved on all new hostile inputs except float junk-key pollution (R-W2b-001) · Security: runtime USER restored to paa (never ships root); touch redaction now pinned; embed online-install unchanged per ADR-0042 · Performance: no issues (O(1) fold overhead, O(rows) backfill unchanged) · Migration/rollback: v5 behaviour untouched; rollback per ADR-0037 · Observability: touch event name unchanged (logger identity NIT from REV-W2 untouched, accepted) · Maintainability/conventions: runtime-import contract holds (replay binds TierPolicy at runtime, sweep green); S608-per-line, enum style, guard-on-effect untouched · Docs impact: lock 42 -> 0 [RAN]; 6 semantic residuals carried as R-W2b-003.

## 7. Optimization and simplification opportunities

| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | replay.py float admission | R-W2b-001 probe | junk-key hygiene | one-line | low | DO NOW (as R-W2b-001 fix, correctness-adjacent) |
| 2 | static test pip-line collector | R-W2b-002 grep | lock actually catches dropped install | 2 lines | low | DO NOW (test correctness) |
| 3 | DEFAULT_TIER x3 unification | R-W2b-005 | drift on ADR change | small | low | DO LATER (pre-existing) |
| 4 | shared TierPolicy instance in fold | replay.py:365 comment | saves 5-entry dict/event | trivial | medium (mutable table) | NOT WORTH IT |

## 8. Cross-agent / cross-task interactions observed

- W2-FIX-01's NEED-free execution held: every briefed item fit its write-set (replay + memory/__init__ + migrations-docstring hotspots sole-writer this fix wave, per COMMS CLAIM lines 78-79).
- FIX-DOCS correctly sequenced behind FIX-01's mid-task landing (replay citations finalised after f009484) — the 42-at-start -> 51-after-FIX-01 -> 0-at-end reconstruction confirms the handoff worked even though FIX-01's own count (48) understated the intermediate.
- FIX-DOCS's decision to collapse the duplicate `_admit` continuation (`:87`/`:168` -> single `:295`) is disclosed in its mapping and semantically sound (def anchor beats two body anchors).
- No scope creep in either fix: no P-003 columns, no sweeper, no A5b weights fixture (explicitly deferred with the exact open question), no config/schema behaviour change.

## 9. What I could not verify

- Docker build of `Dockerfile.embed` (docker absent on host [RAN Get-Command]) — R-W2-002 fix is proven by Dockerfile-text chain + static lock, prediction of CI green, not an observed build. CI embed-image job must execute it.
- PostgreSQL v5 migration end-to-end (no DSN; gated test skipped [RAN]) — PG guard/statement shape reasoned from the docstring correction and SQLite proof; CI must execute `test_postgres_version_4_is_migrated_and_backfilled`.
- T06 test passing with the extra installed (skips here [RAN]) — skip-safety and lazy-construction cheapness verified by reading embeddings.py; a pass with sentence-transformers present is CI/embed-image only.
- Full suite not run (scoped only per standing orders); baseline beyond mypy-3/ruff-0/docs-lock not re-baselined.
- Fail-before by my own execution (would require reverting src) — reversal claims are reasoned from test assertion logic + REV-W2's pre-fix probes, not re-executed. Read-only mandate.
- COMMS last line read: line 80 (W2-FIX-01 DONE, 2026-11-07T09:49:27Z). This report is the only file written; no commit made.
