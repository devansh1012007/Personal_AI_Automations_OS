# Report — W2-IMPR-DOCS (IMPR) · T-003/T-004 docs-reference fix (R-W2-DOCS)

## ORCH-DIGEST
STATUS: COMPLETE | Agent: W2-IMPR-DOCS | Task: T-003/T-004 (R-W2-DOCS) | Report: docs/agent-ops/20261106-memory-remembering-agent/reports/w2-improve/IMPR-DOCS.md | Commits: pending (docs + report) | Blockers: none
Result: all 21 broken citations fixed (1 past-end + 20 blank) + api-reference.md:134/:159 FIXED + 4 same-cause adjacent fixes; `[RAN .venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q -> 5 passed]` [VERIFIED tests/conventions/test_docs_references.py]. Docs-only; `src/` untouched.
Caution: `[FACT]` W2-IMPR-01 edited `src/paa/daemon.py` live (+4 lines at 63-66) during this task; daemon mappings were re-derived against the working tree (class 45, `start` 70, `_claim_and_run` 127, maintenance log 181). Re-run the gate after IMPR-01 commits — any further daemon shift re-breaks these numbers (see COMMS CONFLICT 2026-11-06T18:58:09Z and §6).

## §0 Context (investigation)
- Root causes (two, distinct): (1) T-004 rewrote the daemon maintenance path in `src/paa/daemon.py` (commits 70dd91f on top of c5766c8: +1 line at curator import, +1 at `_curate_next_phase`; then W2-IMPR-01 added +4 lines at 63–66 for the REV R-W2-002 preview flag). Citations at `daemon.py:44` (now blank), `daemon.py:116` (now blank), `daemon.py:165` (now blank), `daemon.py:113` (blank after +4) broke mechanically. (2) Operator README rewrite (88124f2; current README 345 lines vs old ~296/428): old "Known limitations"-style lines 48/160/161/169/175/351 no longer exist; content moved to the Known-limitations table (240/247/250), quick-start (66), and ADR/lessons paragraph (213). Each fix was located by semantic search, not by adding the shift delta (e.g. maintenance-failure now cites the `daemon.maintenance_failed` log line 181, not the old function-comment line +2).
- Old README lines confirmed via `git show c574733:README.md` (48 "No Docker…", 160 "No concrete tools…", 161 "No UI beyond CLI… no web dashboard, no streaming output…", 169 "No ingestion bridges… Telegram, email, or Slack…", 175 "No auth or multi-user…") and `git show f8c12a6:README.md:351` (OFFSET-pagination defect).
- [RAN pytest docs-refs at start -> 2 failed: 1 past-end `docs/data/data-lifecycle.md:126 'README.md:351'` (README 345 lines); 20 blank (12 daemon, 8 README)] [VERIFIED gates/gate-w2-1.md R-W2-DOCS].
- COMMS read at start; last line read: `[2026-11-06T18:51:53Z] W2-REV-01 -> ORCH | DONE …`.

## §1 Old → new table (the 21 + api-ref 2; E-rows = same-cause adjacent fixes)
| # | Doc location | Old | New | Target line text now (verified) |
|---|---|---|---|---|
| 1 | component-interactions.md:99 | `daemon.py:44` | `daemon.py:45` | `class Daemon:` |
| 2 | repository-map.md:128 | `src/paa/daemon.py:44` | `src/paa/daemon.py:45` | same |
| 3 | glossary.md:154 | `src/paa/daemon.py:44` | `src/paa/daemon.py:45` | same |
| 4 | modules/daemon.md:58 | `daemon.py:44` | `daemon.py:45` | same |
| 5 | deployment-and-runtime.md:51 | `src/paa/daemon.py:44` | `src/paa/daemon.py:45` | same |
| 6 | background-jobs-and-events.md:12 | `daemon.py:44` | `daemon.py:45` | same |
| 7 | request-lifecycle.md:15 | `daemon.py:116` | `daemon.py:127` | `async def _claim_and_run(self) -> int:` |
| 8 | workflow-03-crash-recovery.md:138 | `daemon.py:116` | `daemon.py:127` | same |
| 9 | deployment-and-runtime.md:54 | `daemon.py:116` | `daemon.py:127` | same |
| 10 | background-jobs-and-events.md:18 | `daemon.py:116` | `daemon.py:127` | same |
| 11 | background-jobs-and-events.md:33 | `daemon.py:116` | `daemon.py:127` | same |
| 12 | modules/daemon.md:77 | `daemon.py:165` | `daemon.py:181` | `log.error("daemon.maintenance_failed", …)` |
| 13 | data-lifecycle.md:126 | `README.md:351` | `README.md:213` | `Additional defects found through tests included OFFSET pagination skipping rows during deletion, …` |
| 14 | architecture/overview.md:239 | `README.md:161` | `README.md:250` | `Product surface \| CLI and loopback HTTP exist; a web dashboard, mobile access, and multi-user/team mode are later and conditional.` |
| 15 | backend/overview.md:70 | `README.md:161` | `README.md:250` | same |
| 16 | modules/cli.md:69 | `README.md:161` | `README.md:250` | same (PARTIAL — see §2) |
| 17 | architecture/overview.md:244 | `README.md:175` | `README.md:250` | same (multi-user half) |
| 18 | security-model-and-trust-boundaries.md:179 | `README.md:175` | `README.md:250` | same |
| 19 | developer-workflow.md:18 | `README.md:48` | `README.md:66` | `No Docker, server, or GPU is required for the lightweight local setup. …` |
| 20 | integrations-and-communication.md:7 | `README.md:169` | `README.md:247` | `Ingestion \| Minimal event-source integration is planned; broad Telegram/email/Slack and ambient bridges are not a current commitment.` |
| 21 | integrations-and-communication.md:132 | `README.md:169` | `README.md:247` | same |
| A1 | api-reference.md:134 | `api/app.py:219` | `api/app.py:315` | `def _signal_repo(runtime: Runtime) -> Any:` — FIXED |
| A2 | api-reference.md:159 | `api/app.py:232` | `api/app.py:337` | `def _parse_uuid(value: str) -> uuid.UUID:` — FIXED |
| E1 | architecture/overview.md:241 | `README.md:160` | `README.md:240` | `Real-world capabilities \| The coder loop exists, but the permissioned file, shell, and Git tool path must be completed …` (adjacent same-revision fix; was non-blank stale pointing at "Keep three graphs distinct") |
| E2 | backend/overview.md:69 | `README.md:160` | `README.md:240` | same |
| E3 | component-interactions.md:99 (same row as #1) | `daemon.py:62` | `daemon.py:70` | `async def start(self) -> None:` |
| E4 | modules/daemon.md:22 | `daemon.py:113`–`:166` | `daemon.py:119`–`:182` | `# -- loops ---` … end of `_maintenance_loop` (became test-visible only after IMPR-01's +4 shift) |

Notes on rows 7-11: the old `:116` pointed into `_dispatch_loop`'s body (pre-T-004 line 116 was the `while` inside the dispatch loop); every citing sentence describes claiming/ack/nack/`Runtime.run`, which lives in `_claim_and_run` — def line 127 in the current tree. Row 12: the old `:165` was the maintenance-loop header comment; the new target is the actual `maintenance_failed` log statement, which is what "Maintenance failure → logged" claims. E4 loops-range endpoints cover the `# -- loops ---` header (119) through the end of `_maintenance_loop` (182).

## §2 Verification per citation (claim vs new line)
- Rows 1-6: every sentence introduces `Daemon`/`Daemon(runtime)`; `class Daemon:` at 45 is the class definition. FULL.
- Rows 7-11: each sentence pairs the citation with claim/run/ack/nack behaviour; all of it is `_claim_and_run` (127), verified by reading lines 127-148 (claim loop, `runtime.run(cid)` at 143, `ack` 144, `nack` 147). FULL.
- E3: row cell names `Daemon.start()`; def at 70 (which also awaits `boot_recovery()` at 72). FULL.
- Row 12: `daemon.py:181` is the `except` → `log.error("daemon.maintenance_failed", …)` statement; the loop then sleeps (182), i.e. logged+skipped. FULL.
- Row 19 (48→66): old "No Docker, no server, no GPU required." vs new "No Docker, server, or GPU is required for the lightweight local setup. …" FULL.
- Row 13 (351→213): old f8c12a6:351 OFFSET-pagination defect vs new 213 "OFFSET pagination skipping rows during deletion". FULL.
- Rows 20-21 (169→247): old "No ingestion bridges… Telegram, email, or Slack… CLI/HTTP only" vs new 247 "broad Telegram/email/Slack and ambient bridges are not a current commitment". FULL.
- Rows 14-18 (161/175→250): new README 250 covers "web dashboard … later and conditional" and "multi-user/team mode … later and conditional", matching the dashboard/multi-user halves of all five sentences. PARTIAL: the "streaming terminal output" (rows 14-16) and "no auth" (rows 17-18) halves no longer appear verbatim anywhere in the rewritten README (searched: no `stream*` token for terminal output; the streaming/TUI content survives only in the operator's `docs/VISION.md:103-110`, which is outside this write-set's edit rights and was not substituted here). The docs' sentences stand as design-gap statements; their README provenance now rests on the product-surface row (CLI+loopback-HTTP-only surface). [VERIFIED README.md lines 66/213/240/247/250/297; VISION.md:103-110]
- E1/E2 (160→240): old "no file editing, no bash, no web search, no git [wired]" vs new 240 "permissioned file, shell, and Git tool path must be completed and proven end to end". FULL.
- A1: api-reference:133-134 "Builds SignalRepository over runtime.db + cold-lake CAS (`_signal_repo`, …); 503 if cold lake not configured" vs `def _signal_repo` at 315 (its body builds the repo + CAS; the 503 path is the `ingest_signal` handler at 174-178 which calls it). FULL. Old 219 was `source_signal_id=f.source_signal_id` (inside `list_facts`) — unrelated.
- A2: error table "400 | Invalid correlation_id UUID | `_parse_uuid` (…)" vs `def _parse_uuid` at 337. FULL. Old 232 was an unrelated 400 raise inside `search_facts`.

## §3 §4 Standing-order §8 (long-run test)
- Source: two independent edits (T-004, operator README rewrite) plus one live parallel edit (IMPR-01) shifted numbers; every cited line was re-read, not delta-adjusted. Recurrence risk is process-level: any `daemon.py`/`README.md` edit re-breaks these coordinates; the conventions test catches blank/past-end, but non-blank staleness (the 161/160/api-ref class) is invisible to it and needs a human read — exactly the AGENTS.md "read, not re-number" rule.
- Load / Hostile input: N/A (docs-only; no executable change, no new inputs).
- Operability: reverting any of rows 1-13, 19-21, E4 makes `tests/conventions/test_docs_references.py` fail (blank/past-end); verified by the initial run. Reverting A1/A2/E1-E3 does NOT fail any test — those are human-verified only (§2). Rollback path is a plain revert of the docs commits; no migrations, no generated artifacts (citations are hand-written prose). If IMPR-01's final daemon.py lands with different line numbers, rows 1-12/E3/E4 must be re-derived (see §6).
- Reversal (mutation check): if `_claim_and_run` were moved out of reach, rows 7-11 would point at its old line and claim text would mismatch — the test cannot see it; §2's manual check is the only guard, which is why each mapping above quotes the new line.

## §5 Self-review audit
- Phase 1 (investigation+edits): reviewed the full `git diff` of the 16 docs files. Findings: (a) initial daemon run executed against the moving IMPR-01 tree and flagged `daemon.py:113` — resolved by re-deriving all daemon numbers against the current tree (E4 + 70/127/181 corrections), test green. (b) First api-reference edit attempt failed on indentation (3 vs 4 spaces) — `repr()` check fixed it; exactly 1 replacement each. (c) `replaceAll` used only where two identical citations needed the same new value (background `daemon.py:116`×2→127, integrations `README.md:169`×2→247); post-edit `Select-String` confirms no leftover `daemon.py:44/:116/:165`, `README.md:48/160/161/169/175/351`, `api/app.py:219/:232` in write-set docs. (d) No `src/`, config, or README.md/VISION.md edits; `git status` shows only the 16 docs files + COMMS line + report under my ownership.
- Final review (after test green): re-read every edited diff hunk against §2 verified lines. MINOR, accepted: README 161/175 fixes cite the product-surface row (250) for "streaming"/"auth" halves that the rewrite dropped verbatim — documented as PARTIAL rather than inventing a target (brief §3 rule). No CRITICAL/MAJOR findings.

## §6 Verification, assumptions, remaining risks
- [RAN `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` -> 5 passed (no F/E)] (final run after re-derivation).
- [NOT-RUN] Full suite / ruff / mypy — out of scope (docs-only; standing order 16, scoped checks only). No source file was edited, so no lint/type surface changed.
- Assumption: W2-IMPR-01's `src/paa/daemon.py` diff (+4 lines at 63-66; docstring additions after line 205) is final. If his committed version differs, rows 1-12/E3/E4 shift again — flagged in COMMS (CONFLICT, 2026-11-06T18:58:09Z) and in the ORCH-DIGEST. `api/app.py:315/:337` verified against the working tree including IMPR-01's 1-line diff (line 356, no count change) — stable for now.
- Remaining (out-of-scope, DEFERRED — observed, not fixed; same files but outside the brief's 21+api2 mandate): (i) `docs/modules/daemon.md` other daemon cites (113-range fixed as E4; `:22` done; remaining `:25/:26/:28/:30/:32/:73/:88/:94/:103/:121/:136/:138/:148/:150/:151/:158/:170/:173` etc. are non-blank but pre-T-004 coordinates, several now semantically off — e.g. :173 (`DecaySweeper.sweep()` direct call) no longer exists inline after T-004); (ii) AGENTS.md non-blank stales in write-set docs (`AGENTS.md:33/:97/:132/:146` — e.g. overview.md:242 "Observability code exists but is untested (`AGENTS.md:97`)" now points at the storage-parity heading); (iii) more api-reference stales (`api/app.py:87/:109/:135/:150/:168/:172` vs defs at 294/113/135/155/294/174). All are non-blank, so the gate passes; a follow-up brief with a full read pass could clear them. [VERIFIED by spot reads: AGENTS.md:97/:132, api/app.py def list]
- COMMS last line read before finishing: `[2026-11-06T18:58:09Z] W2-IMPR-DOCS -> ORCH | CONFLICT …` (my own line).

## §7 Out-of-scope findings
See §6 (three deferred groups with evidence). No code defects observed. One process observation: parallel live edits to `src/paa/daemon.py` while a docs-number fix is in flight is inherently racy — the conventions test gives a green snapshot, not stability; a short coordination window (edit freeze on cited files during the docs pass) would have avoided the mid-task re-derivation.

## §8 Git state
Branch `ops/20261106-memory-remembering-agent` (never switched). Working tree also contains other agents' uncommitted `src/`/`tests/` changes (W2-IMPR-01, W2-IMPL-03/04 artefacts) — untouched. Commits (explicit paths only, trailers per brief): 1) docs (16 files), 2) this report (1 file). COMMS append left uncommitted for ORCH per COMMS rule 8.
