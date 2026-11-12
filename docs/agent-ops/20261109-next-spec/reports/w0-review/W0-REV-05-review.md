<!-- ORCH-DIGEST:START -->
agent: W0-REV-05 | scope: wave 0b / T-N0-4 (c09b0f1) | verdict: APPROVE
findings: BLOCKER 0 | MAJOR 0 | MINOR 1 | NIT 1
claims_audit: 12 / 0
band_aids_found: none
top_3_risks: plan N0.4 wording still says outcome counts (stale vs pinned method); subprocess.run has no timeout (CI job timeout is backstop); collected count will go red on any sibling test-adding commit until regen (by design — one regen before merge)
optimizations: DO-NOW 0 | DO-LATER 1 | NOT-WORTH-IT 3
reversal_check: done
confidence: high (all probes run locally with .venv interpreter; only CI-on-runner unverified)
<!-- ORCH-DIGEST:END -->
# Code review — W0-REV-05 (T-N0-4 status generator c09b0f1)

## 0. Method
Standing orders + brief read first. Blind pass on the diff alone (`git show c09b0f1 --stat` = 3 files, 447 insertions: `scripts/status.py` new 362 lines, `ci.yml` +81, `README.md` 5 lines), then `scripts/status.py`, README markers, and the `status` CI job read in full. Only then opened the W0-IMPL-04 report for the claims audit. All probes run with the repo venv (`.venv/Scripts/python.exe`) — the system interpreter has no `paa` installed and `--check` fails with exit 2 there, which is itself correct loud-failure behaviour, not a defect.

## 1. Blind-pass findings
- **Script shape is right.** Stdlib-only, fully typed, no third-party imports at module scope (stable across install shapes). Exit codes are disciplined: 0 match, 1 mismatch-with-diff, 2 error. Failures raise `StatusError` (never silent defaults) — a wrong count in the README is worse than a red check, and the code embodies that.
- **Counting is a pure sum over sorted files** (`count_source_lines`, status.py:105-125) via `Path.read_text(encoding="utf-8")` + `splitlines()`. Physical lines, blanks and comments counted, no-trailing-newline counted. Deterministic order; sum is order-independent anyway.
- **Collected count** (status.py:128-162) pins argv as interpreter + constant tuple, strips `PYTEST_ADDOPTS` from the child env, parses the last `N tests collected` line, and treats non-zero exit as error with output tail. `subprocess.run` has `shell=False`, fixed argv, one justified per-line `# noqa: S603` — matches the repo's per-line-noqa convention, not a per-file allow-list.
- **Check semantics** (status.py:252-275) compare the exact text between markers after CRLF→LF normalisation; markers must occur exactly once each and in order, else exit 2. `--write` validates markers *before* spending a collection run (status.py:289), preserves CRLF EOL style, writes via temp-file + replace.
- **README block** at README.md:295-298 replaces only the stale snapshot line; the human-maintained subsystem table and prose outside the markers are untouched. LF EOLs preserved.
- **CI `status` job** (ci.yml:462-514) is separate from `gate`, pins `[dev]`, declares no services, omits `python-is-python3`, reuses pinned action SHAs (checkout 3d3c42e5, setup-python 6d4fd22b — same as gate), venv fallback, and the pip retry loop. Final step is `.venv/bin/python scripts/status.py --check` — no flaky pytest run.
- No band-aids visible: no sleeps/timeouts in code, no swallowed errors, no hardcoding, no test weakening (no tests changed at all).

## 2. Claims audit
W0-IMPL-04 report claims, each checked:

| # | Claim | Verdict |
|---|---|---|
| 1 | 142 files, 57,326 physical lines | [RAN independent recount -> 142 files, 57,326 physical, 48,877 non-blank] **VERIFIED** |
| 2 | 3,762 collected = baseline 3,754 + 8 new | [RAN `pytest --collect-only tests/conventions/test_markdown_links.py` -> 8] **VERIFIED** |
| 3 | `--check` fails on hand-edit, passes after regen | [RAN on temp copy: edit 3,762→9,999 -> exit 1 with unified diff; `--write` -> exit 0; `--check` -> exit 0; real README `--check` -> exit 0] **VERIFIED** |
| 4 | `pytest tests/conventions -q` 34 passed | [RAN -> 34 dots, exit 0] **VERIFIED** |
| 5 | ruff 0 findings | [RAN `ruff check src tests scripts/status.py` -> All checks passed] **VERIFIED** |
| 6 | mypy src exactly 3 pre-existing; mypy script clean | [RAN `mypy src` -> exactly 3 at `sandbox/subprocess_backend.py:548` (A-001); `mypy scripts/status.py` -> Success] **VERIFIED** |
| 7 | CI job uses `--check` not pytest; pins `[dev]`; no services | [VERIFIED ci.yml:462-514 read in full] **VERIFIED** |
| 8 | README stale 1,185 gone | [VERIFIED grep `1,185` README.md -> no matches; block at :295-298 shows 57,326 / 3,762] **VERIFIED** |
| 9 | ROADMAP drift left untouched | [VERIFIED docs/ROADMAP.md:31 still 52,000 / 3,145 — acceptable per brief §6] **VERIFIED** |
| 10 | Method drift: spec 57,300 ≈ physical, INV 48,879 ≈ non-blank | non-blank measured 48,877 (2 off INV — tree drift since INV, immaterial); plan R9 admits method was unstated [VERIFIED revised plan:201] **VERIFIED** |
| 11 | `PYTEST_ADDOPTS` stripping | [RAN with `$env:PYTEST_ADDOPTS='-k test_markdown_links'` -> `--check` still exit 0 with 3,762] **VERIFIED** |
| 12 | Missing/duplicate markers -> exit 2; CRLF round-trip | missing-marker path [RAN -> exit 2 with counts]; CRLF path code-read only (EOL logic at status.py:292-300 reviewed, sound) — **PARTIALLY VERIFIED** |

0 unsupported or false. One agent claim I did *not* re-run: their temp-README 8-case selftest (their scratch file was deleted); cases 3, 11, 12 above cover the load-bearing subset independently.

## 3. Long-run test (independent answers)
- **Source:** generation is O(tree bytes + test count). No unbounded collections; file set fixed eagerly. Sound.
- **Load (100×):** a 100× tree means ~100× collection time; today's collect is ~10 s, so 100× ≈ 1,000 s > CI job's 20-minute timeout. The check would go red by timeout, not by false pass — safe direction, but at that scale the method needs a faster counter. Not a current risk.
- **Hostile input:** README text is never interpreted (no eval/shell/YAML). Marker spoofing (marker string in prose) → count ≠ 1 → loud exit 2, fail-safe. Non-UTF-8 source or README → loud exit 2, never miscounted. `--readme` to arbitrary path only reads + replaces between valid markers. Good.
- **Operability:** mismatch prints a unified diff plus one-line regen instruction. Rollback is `git revert` of one commit, no migration. No flags left behind. Good.
- **Reversal:** remove `scripts/status.py` → CI `status` job fails (file absent). Restore pre-change README → `--check` exits 2 (markers absent) [RAN conceptually via nomark probe]. Remove CI job → no check runs. Each layer fails independently for the right reason.

## 4. Findings
**MINOR-1 — Plan §N0.4 wording is now stale vs the pinned method.** The plan's N0.4 WHAT column still reads "Status generator (source lines, tests collected, outcome counts)" [VERIFIED revised plan:53], while the implementation deliberately excludes outcome counts. The exclusion is *justified* (see below) and is declared in three places (report §12, script docstring status.py:40-45, generated block itself). But the plan text itself was outside the implementer's write-set, so the contradiction persists for the next reader. ORCH should amend the plan's WHAT column ("outcome counts measured by the gate job, not the generator") in a plan addendum. Not a code defect; no code change needed.

**NIT-1 — `subprocess.run` (status.py:144) has no timeout.** A hanging test-module import would hang `--check` until the CI job's 20-minute timeout kills it. The job timeout is an adequate backstop and a local hang is operator-visible, so this is a nit, not a blocker. DO-LATER: add `timeout=` with a clear error.

**On (c) outcome-counts exclusion — justified, not a gap.** Outcome counts require *running* the suite, whose results depend on Docker daemon, PG/S3 servers, OS user, and install shape — exactly the per-host flap the plan's M-6 rationale and N0.4 WHY column ("CI job regenerates `--check` instead of failing on every host") exist to prevent. The plan's own method pin ("cloc-equivalent ... + `pytest --collect-only`") already excludes outcomes; only the WHAT-column shorthand names them. Collecting is host-stable — verified: requires_postgres tests collect with no server present [RAN `pytest --collect-only tests/storage/test_postgres_migrations.py` -> 19 collected, exit 0]. The break-the-check failure mode (Docker env varying) does not apply to this method.

## 5. Attempts to break it
- Hand-edit count in a copy → exit 1 with diff [RAN]. `--write` restores → exit 0 [RAN].
- Remove a marker → exit 2 with counts [RAN].
- Caller env `PYTEST_ADDOPTS=-k ...` cannot filter the count [RAN].
- Run with the wrong interpreter (no venv) → exit 2 with the pytest stderr tail, never a guessed count [RAN accidentally, then confirmed by design].
- Collected count without Docker/PG/S3 servers → still collects [RAN, §4]. Count does not flap on server presence.
- Could not break: order-dependence, duplicate markers mid-prose, non-UTF-8 handling — all fail loud by code path (code-read, not fault-injected).
- One soft spot confirmed but harmless: collected count (3,762) reflects this checkout; any sibling commit adding tests makes `--check` red until regen. That is the check's purpose, but ORCH should expect one regen right before merge (implementer already flagged this).

## 6. Dimension checklist
- Correctness: counting pure-sum, parse takes last match, exit codes consistent. ✅
- Error handling: `StatusError` everywhere, tail-included pytest failures, marker validation before generation. ✅
- Security: fixed argv, no shell, env stripped, README never executed, atomic write. ✅
- Tests: no new tests (correct — a pytest wrapper around `--check` is the banned flap); T-N0-3 demonstrated manually twice by implementer, once by me. ✅
- Conventions: line-length 100 (ruff clean), docstrings carry method + citations, per-line noqa only, LF preserved. ✅
- CI: `[dev]` pin, no services, no python-is-python3, pinned SHAs, retry loop, separate job. ✅
- Reversibility: single-commit revert, no data migration. ✅

## 7. Optimization and simplification opportunities
- DO-NOW: none.
- DO-LATER: subprocess timeout (NIT-1).
- NOT-WORTH-IT: `cloc` binary (totals vary by version/flags); caching counts (defeats the check); embedding outcome counts in the block (reintroduces host flap); adding `--check` to the gate matrix (minimal/all shapes collect different counts by design).

## 8. Cross-agent interactions
- README hotspot: N0.1 done before N0.4 edits (COMMS 2026-11-09T07:22:50Z + ORCH anchor fix 07:30:00Z); current `--check` passes on the merged tree, so no conflict remains.
- Collected count stays 3,762 on today's tree because wave-0b siblings are reviewers (no test-adding commits). Verified by the passing `--check`.
- ROADMAP drift and VISION "quotes its output" remain open follow-ups for ORCH (brief §6 forbids touching them here).

## 9. What I could not verify
- The CI `status` job on an actual GitHub runner (no runner access). Mitigated: YAML read in full, SHAs match gate job, both meaningful steps replicated locally (`.venv` + `[dev]` installed; `--check` passes). Failure direction is safe (red, not false-green).
- CRLF round-trip by execution (agent's deleted selftest covered it; I reviewed the code path). Missing-marker and hand-edit paths were executed by me.
- Full suite (`pytest tests/ -q`) — forbidden by scoped-check rule and intentionally irrelevant: outcomes are excluded from the block by design.
