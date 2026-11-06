<!-- ORCH-DIGEST:START -->
agent: DOCS-README-01 | scope: docs/README.md index only | status: PARTIAL (README work complete + own refs lock-clean; lock-green blocked by post-SUMMARY operator rename outside write-set)
indexed: docs/README.md 168 lines (was 158); +memory post-run bullet; +runtime/glossary; +memory-polish/plans-and-audits; +RFC pair honest pair; +run folder SUMMARY/ledger/gates; fixed 5 stale path:line + VISION link + ADR counts + E coverage/baseline
findings: lock FAIL after 2 identical scoped runs — 3 blank-line refs in docs/plans and audits/w6-w7-w9.md (cas.py:62, api/app.py:42, conftest.py:55) — pre-existing after commit 15f67e3 renamed docs/plans→docs/plans and audits (out of exclusion scope); docs/README.md own 9 refs all pass in isolation (verified)
blockers_major_open: none in write-set; DEFERRED-DEPENDENCY: w6-w7-w9 3 refs (owner of plans-and-audits/lock-exclusion to fix; see §3)
code_commits: this report's docs-commit hash (see §5) | report_commit: this file's commit hash (see §5)
tests_added_or_strengthened: none (docs index only)
ownership_violations: none (write-set: docs/README.md + this report only; COMMS not appended; src/tests/sub-docs untouched)
confidence: high (disk census + every new/kept path:line read at target; SUMMARY content not copied)
last_comms_line_read: [2026-11-06T23:06:32Z] FINAL-INSTALL-01 -> ORCH | START | FINAL-INSTALL | FACT Retrying install/launch smoke retry-2 at HEAD a05c08a clean; running preflight/install/gate/smoke | ev: briefs/FINAL-INSTALL-01.md §4 (COMMS.md:67, end of file at start and again before commits; COMMS NOT appended per brief)
<!-- ORCH-DIGEST:END -->

# Report — DOCS-README-01 — docs/README.md index update

## 1. What was indexed/fixed (docs/README.md only)

Read sources in brief §2 order in full first: `docs/README.md` (158 lines), `SUMMARY.md` (143 lines), `00-mission.md` (35 lines), `01-work-ledger.md` (44 lines), `reports/final/IMPR-01.md` (73 lines), `AGENTS.md` (550 lines), `COMMS.md` (67 lines, read-only). No `src/`/`tests/` edits; no RFC delete/track/move; no COMMS append.

### Disk census [RAN pathlib rglob + git ls-files]

In-scope (non-`agent-ops`) docs on disk: 66 `.md` (see `DOCS-AUDIT-01` census 282 total / 66 live). Findings vs old index:

- Module docs (14) matched old §C list exactly. Operations (2) matched. Architecture (3), Backend (3), Data (3), Interfaces (2), Flows (3 workflows), Security (1), Development (2), Reviews (1) all present.
- Missing from old §C: `glossary.md`, `runtime/background-jobs-and-events.md`, `memory-polish/plans/Plan1.md`, `plans and audits/` (4 files), 2 RFC files at `docs/` root, run folder `agent-ops/20261106-memory-remembering-agent/` (SUMMARY/ledger/gates). **All added.**
- Broken/stale in old index: `VISION.md` linked to `docs/VISION.md` (deleted in `15f67e3`; live file is repo-root `VISION.md`); ADR counts stale on 2 lines; §E zero-coverage + six-failure claims stale; §A `(untested)` stale; 5/8 `path:line` semantically wrong (see §2). **All fixed.**

### Memory bullet (modules/memory.md line) [VERIFIED]

One-line summary without copying SUMMARY: sink/drain wired, touch on recall (`last_queried_at`/`use_count`), curator in daemon, extractor counters, explicit-only floor/deprecation, e2e proof (`tests/memory/test_e2e_remember_recall.py`, 839 lines, exists), pointer to run SUMMARY + operator runbook *Memory curation* (`operations/deployment-and-runtime.md:67-107`, runbook head verified). Operations bullet for `deployment-and-runtime.md` now names the runbook's four moves. Cross-checked against `docs/modules/memory.md:88-105` (creator drain, curator cadence) and SUMMARY §3 (touch/counters/floor wordings).

### RFC pair (honest, one line each + SUMMARY §8) [VERIFIED]

- Doc A: vision only, scorer deviates per ADR-0012 (index-only; `docs/adr/README.md:33` lists 0012, bodies 0008/0010/0015/0019–0026/0028–0035 per `AGENTS.md:323-326`), see SUMMARY §8 line 95 (formula contradiction).
- Doc B: v4.0+v4.1, 21 specialists empty (verified `src/paa/agents/specialists/__init__.py` 0 lines), vault 11/13 missing, see SUMMARY §8 line 96.
- Header states "not normative; no ADR adopts them" per SUMMARY §8 line 93. Neither claimed as a build spec. Tracking untouched (already tracked post-`29d60e4`/`15f67e3`; verified `git ls-files` lists both).

### Run folder (trail, lock-excluded) [VERIFIED]

SUMMARY + ledger + gates links (plain `.md` links, no `:line`, so no new lock surface); header notes whole `docs/agent-ops/` excluded per `tests/conventions/test_docs_references.py:108-114,540` (reason: run trail, allowed to rot). SUMMARY pointer notes C01–C07 closed + §K NOT DONE at summary time (SUMMARY:7,23).

## 2. path:line fixes (each verified by reading target ± context)

Kept (semantic hold, no change): `submit:74` (decorator, def at 75), `doctor:225` (decorator, def at 226), `Runtime.build():260` (`async def build(` at 260). Fixed:

| Doc line | Was | Now | Target read |
|---|---|---|---|
| serve | `main.py:248` (`table.add_row(graph…)` in doctor) | `main.py:290` (`@app.command()`, def `serve` at 291) | main.py:290-291 |
| recover | `main.py:191` (`rt = …_build_runtime()` in gate) | `main.py:207` (decorator, def at 208) | main.py:207-208 |
| ledger | `main.py:134` (`if p.awaiting_reason:` in status) | `main.py:145` (decorator, def at 146) | main.py:145-146 |
| agent | `coder.py:44` (import) | `coder.py:56` (`@app.command("agent")`, defs at 53/57) | coder.py:53-57 |
| terminal | `core/types.py:132` ("Members are ordered…") | `core/types.py:176` ("Events after which a correlation is closed…") | types.py:176-187 |

New (1): `sandbox/subprocess_backend.py:548` (`os.killpg(os.getpgid…)`; 3 win32 mypy errors per `00-mission.md:18`) — relative path resolves via `src/paa/` fallback. No other new `:line` introduced; new index entries use plain links.

Self-review of diff: re-read edits from scratch — 5 line-number changes + VISION `../VISION.md` + ADR counts on 2 lines (match `adr/README.md:57-58`) + §A `(untested)` drop + §E rewrite (19-test validator per `AGENTS.md:194-195`; baseline mypy-3/pytest-12 per `00-mission.md:18-23` + SUMMARY §5; Linux-zero gate per `AGENTS.md:39-40`) + missing entries. No sub-doc content touched; no SUMMARY tables copied; no normative claim for RFC pair. Mutation check: reverting any fix re-points to blank/unrelated lines (e.g. serve→248 doctor table row); conventions isolation run below fails if any README ref goes blank/orphaned.

## 3. Conventions result (real output, scoped pytest only)

[RAN `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` → EXIT 1, run twice (retry per standing orders §18, identical). Failure is NOT in `docs/README.md`:

```
FAILED tests/conventions/test_docs_references.py::test_every_referenced_line_is_not_blank
docs/plans and audits/w6-w7-w9.md:71: 'src/paa/storage/coldlake/cas.py:62' — blank
docs/plans and audits/w6-w7-w9.md:74: 'api/app.py:42' — blank
docs/plans and audits/w6-w7-w9.md:134: 'tests/storage/conftest.py:55' — blank
```

Cause (pre-existing, outside write-set): commit `15f67e3` (operator, after SUMMARY head `a05c08a`) renamed `docs/plans/` → `docs/plans and audits/` (plus deleted `docs/VISION.md`, renames at 0-line change). The lock excludes only `docs/plans/` (`test_docs_references.py:115-120` prefix match), so the renamed dir fell into scope and its 3 rotted refs surfaced. Targets verified blank independently (`cas.py:62`, `api/app.py:42`, `conftest.py:55` all empty). Previous green (SUMMARY §5, IMPR-01 §5: 5 passed) predates the rename. This agent may not edit `w6-w7-w9.md` or the lock exclusions (both outside WRITE-SET) — **DEFERRED-DEPENDENCY** for the owner of `docs/plans and audits/` / ORCH: either fix the 3 refs in `w6-w7-w9.md` (read-target correction) or extend the exclusion list with a stated reason. The README's own "in scope" note for those files is accurate post-rename and documents this; removing the note would not change scope (scope is file-based).

Own-refs isolation [RAN python import of `test_docs_references`, filtered to `docs/README.md` → 9 refs, violations `[None]*9`]. Ruff N/A (markdown only, per brief). Long-run test: source = read-target correction at the copy (decorator lines match the author's original submit/doctor convention); load 100x = no runtime change (docs strings only); hostile input = no new SQL/LIKE surface, no secrets; operability = README now points at live paths (VISION root, RFC honesty header, runbook anchor); reversal = breaking any one of the 9 README refs turns the isolation check red (missing/blank), and the full lock goes red on w6-w7-w9 regardless of this task until the DEFERRED item lands.

## 4. DEFERRED / out-of-scope (not edited)

- `docs/plans and audits/w6-w7-w9.md` 3 refs (above) + lock-exclusion decision for the renamed dir. NEED for ORCH/owner.
- `docs/modules/memory.md` touch-on-recall + extractor-counter details (file has drain/curator/floor but no `touch`/`counter` sections) — detail belongs to memory-module owner; README summary covers the index duty without duplicating SUMMARY.
- `docs/operations/deployment-and-runtime.md:146-147` still says observability untested (README now notes the drift in §E; doc itself outside write-set).
- Root `README.md` still cites `docs/VISION.md` (moved to repo root in `15f67e3`) — same break as fixed in `docs/README.md`, outside write-set.
- `docs/adr/README.md` index-only bodies (0001–0007/0009/0011–0014/0016/0018 etc.) still unwritten — declared in AGENTS "Known-stale", not chased.

## 5. Commits

- Docs: `git add -- docs/README.md` + `git commit -m "docs(README): index memory run, RFC pair honest status, fix stale links" -- docs/README.md` — SHA below.
- Report: `git add -- docs/agent-ops/20261106-memory-remembering-agent/reports/final/DOCS-README-01.md` + commit listing docs SHA — SHA below. Trailers: `Task: DOCS-README / Agent: DOCS-README-01 / Wave: FINAL / Report: docs/agent-ops/20261106-memory-remembering-agent/reports/final/DOCS-README-01.md`. Explicit paths only; branch `ops/20261106-memory-remembering-agent`.
