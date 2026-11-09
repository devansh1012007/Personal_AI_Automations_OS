<!-- ORCH-DIGEST:START -->
agent: W0-IMPL-04 | task: T-N0-4 | status: DONE
code_commits: c09b0f1 | report_commit: TBD
files_changed: 4 (scripts/status.py, .github/workflows/ci.yml, README.md, reports/w0-investigation/W0-IMPL-04-N0-4.md)
contracts_changed: none
tests: added 0, changed 0; scoped run: pass (scripts/status.py print/--check/--write incl. hand-edit negative test, pytest tests/conventions -q 34 passed, ruff 0, mypy src exactly 3 pre-existing)
ownership_violations: none
deferred_dependencies: none (README hotspot free: W0-IMPL-01 DONE T-N0-1 per COMMS 2026-11-09T07:22:50Z, ORCH anchor fix 07:30:00Z, both before this task's edits)
out_of_scope_findings: 1 (ROADMAP drift left untouched per brief §6, noted below)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: outcome-counts exclusion is a deviation from spec §N0.4 wording; argues flapping wins over completeness (see §12)
confidence: med (all acceptance criteria demonstrated locally; CI job not run on a runner — only YAML-parsed and step-replicated locally)
last_comms_line_read: 2026-11-09T07:30:00Z
<!-- ORCH-DIGEST:END -->
# Implementation report — W0-IMPL-04 — T-N0-4 scripts/status.py + README markers + CI --check

## 1. Task and acceptance criteria (restated)
T-N0-4 (N0.4): create `scripts/status.py` markdown-block generator; README status block between `<!-- STATUS:BEGIN/END -->`; CI job runs `python scripts/status.py --check`. Acceptance: `--check` fails if block hand-edited, passes after regeneration; method pinned in script docstring + plan addendum; `python scripts/status.py` output stable on same checkout; ruff 0, mypy src exactly 3 (scripts/ outside src mypy gate — ensure ruff passes on script); README numbers updated to real counts (not 1,185). Depends on N0.1 (README hotspot serial, single-writer). [VERIFIED docs/agent-ops/20261109-next-spec/briefs/W0-IMPL-04.md:15]

## 2. Verification that the problem exists (evidence gathered myself)
- `scripts/` did not exist; no `status.py` anywhere [RAN `ls scripts` -> ObjectNotFound]; grep `STATUS:BEGIN|status.py` across md/yml/toml -> no hits.
- README stale snapshot: `**Recorded test snapshot:** 1,185 collected — 1,104 passing, 75 skipped, 6 failing.` [VERIFIED README.md:295 pre-edit]
- ROADMAP hand-typed figures: `about 52,000 lines of source, 3,145 tests collected, 2,899 passed, 241 skipped, 4 xfailed, 1 failed (environmental)` [VERIFIED docs/ROADMAP.md:30-33]; H1.7 requires the script + CI disagree-fails [VERIFIED docs/ROADMAP.md:82].
- Measured now (this checkout, .venv): 142 `src/**/*.py` files, **57,326 physical lines, 48,877 non-blank lines** [RAN .venv/Scripts/python.exe count -> 57326 physical / 48877 nonblank]; `pytest --collect-only -q -o addopts=""` -> **3,762 collected** [RAN .venv collect-only -> 3762]. Baseline was 3,754 [VERIFIED docs/plans/2026-11-09-next-spec-revised.md:8]; +8 is exactly W0-IMPL-01's new `tests/conventions/test_markdown_links.py` [RAN collect subset -> test_markdown_links.py: 8].
- Method drift proven with the tree itself: spec 57,300 [VERIFIED docs/2026-11-09-next-spec.md:34] ≈ physical (57,326); INV 48,879 [VERIFIED docs/plans/2026-11-09-next-spec-revised.md:201] ≈ non-blank (48,877). The two differ only by blank-line definition; spec method was unstated [VERIFIED revised plan:201 "spec method unstated — N0.4 pins method"].
- CI had jobs `gate` (matrix minimal/dev/all) and `embed-image`; no status job [VERIFIED .github/workflows/ci.yml].

## 3. Root cause
Hand-typed status numbers with no generator and no check: README's 1,185 snapshot predates the current tree by ~2,500 tests, ROADMAP's figures are a different vintage again, and nothing in CI compares either against the tree. Any proposed fix that embeds full-suite outcome counts would flap per host (Docker/PG/S3/OS/extras decide skipped/passed/failed), which is exactly the failure the plan's `--check`-instead-of-pytest design (reviewer M-6) exists to avoid. The root cause is therefore twofold: (a) no pinned counting method, so every figure is a different method; (b) no machine-checkable block, so staleness is invisible until a human notices.

## 4. Approach chosen and why (alternatives rejected)
**Chosen.** A stdlib-only `scripts/status.py` that generates the block from two pinned, host-stable measurements:
1. Source lines = sum of `len(text.splitlines())` over sorted `src/**/*.py` via `Path.read_text(encoding="utf-8")` (physical lines; blanks/comments counted). Pure-Python `cloc`-equivalent; `cloc` binary never run (its totals vary with version/flags).
2. Tests collected = subprocess `sys.executable -m pytest --collect-only -q -o addopts=""` from repo root; count = last `N tests collected` line. `[dev]` install shape pinned (minimal collects fewer because `tests/api/conftest.py` et al. skip whole directories at import); `PYTEST_ADDOPTS` stripped from the child env so caller filters cannot change the count.
3. **Outcome counts are deliberately NOT measured by the generator.** The block states this explicitly and gives the measurement command (`python -m pytest tests/ -q`). Rationale (decisive): outcomes require *running* the suite, and running depends on Docker daemon, PG/S3 servers, OS user, and extras — the exact flap the plan cites ("host counts vary by Docker env — spec baseline shows Docker-dependent failure" [VERIFIED revised plan:53]) and the M-6 rejection of a per-host pytest check. A generated outcome number would make `status --check` red/green by host, destroying the check's purpose. Collected count is host-stable because collecting ≠ running (`requires_*` tests collect regardless of server presence).
4. CLI: print (default, markers included for verbatim paste), `--check` (exit 0 match / 1 mismatch-with-diff / 2 error), `--write` (in-place replace, EOL-preserving, temp-file+replace), `--readme` (override, for testing). Exact byte comparison after CRLF→LF normalisation; markers must occur exactly once each, in order.
5. CI: separate `status` job (not a gate step) pinning `[dev]`, no services, no `python-is-python3` (collection only imports; nothing runs through the sandbox). Reuses gate conventions (pinned actions, venv fallback, pip retry loop).

**Rejected alternatives:**
- *Script runs the full suite and records outcomes.* Rejected: ~minutes per run, host-flapping (Docker/OS/servers decide outcomes), and it would make `--check` fail on hosts with different environments rather than on hand edits — the precise failure mode in the mission ("CI flaps per host"). Also violates the operator's scoped-check rule (never run full suite) for anyone verifying locally.
- *Script counts markers at collection to predict outcomes* (e.g. count `requires_postgres`). Rejected: predicts nothing about outcomes; a predicted "outcome" that was never run is a fabricated number.
- *`cloc` binary for source lines.* Rejected: not installed everywhere; totals vary by version/flags. Pure-Python equivalent is portable and auditable.
- *Add `--check` as a step inside the existing `gate` matrix.* Rejected: matrix shapes collect different counts (minimal skips API/S3 dirs at import); `--check` would fail on two of three shapes by design.
- *Non-blank lines as the source measure.* Rejected: matches INV's 48,879 but contradicts spec's 57,300 and the plan's pinned "physical lines" wording; blank counting is the simpler, more standard total (cloc total = blank+comment+code).

## 5. What changed (per file)
- **`scripts/status.py` (new, ~290 lines, stdlib only, `from __future__ import annotations`, fully typed).** Module docstring pins both methods with file/line citations for the 57,300-vs-48,879 drift. Functions: `count_source_lines` (sorted rglob, UTF-8, `splitlines`, loud `StatusError` on unreadable/non-UTF-8 files — never silently miscounted), `count_collected` (pinned argv, `PYTEST_ADDOPTS` stripped, UTF-8 decode, non-zero exit → error with output tail, last `N tests collected` parsed), `generate_body`/`generate_snippet`, `extract_middle` (exactly-once markers, order-enforced), `check_readme` (unified diff on mismatch), `write_readme` (validates before counting, preserves CRLF, temp-file + replace), `parse_args`/`main` (mutually-exclusive `--check`/`--write`). Exit codes 0/1/2 as documented. One per-line `# noqa: S603` on the `subprocess.run` call with justification (argv is fixed: interpreter + constant tuple, no shell, no caller input) — see §10/§16.
- **`.github/workflows/ci.yml` (+81 lines): new `status` job.** Checkout (pinned 3d3c42e5, fetch-depth 1) → setup-python 3.12 (pinned 6d4fd22b, pip cache) → venv (with `--without-pip` fallback, mirroring gate) → `pip install -e ".[dev]"` behind the 5-attempt retry loop (`PIP_RETRIES=10`, `PIP_TIMEOUT=120`) → `.venv/bin/python scripts/status.py --check`. Long comment documents the three measured separations (install shape, no services, no python-is-python3). No change to `gate` or `embed-image`.
- **`README.md` (status block only, 1 line → 4 lines at old line 295).** Stale snapshot replaced by the exact generated snippet: `<!-- STATUS:BEGIN -->`, generated body (142 files, 57,326 lines, 3,762 collected; method note; outcomes-not-measured note), `<!-- STATUS:END -->`. Subsystem table, prose, and all other sections untouched; LF EOLs preserved (diff is 5 changed lines, no reflow).
- **This report** (docs-only, separate commit per standing orders).

## 6. Blast radius and contracts
Blast radius: documentation-truth only. No `src/` code, no public API, no event/schema/config change, no dependency change, no test change. Consumers: (a) README readers (numbers are now generated, not typed); (b) CI (adds a job; does not alter existing jobs' verdicts — `status` failing would block merge by branch protection, but its inputs are checkout-only, no services, so it cannot flap on Docker/server presence); (c) future agents (regen via `scripts/status.py --write`). Backward compatibility: markers are new; nothing parsed them before. The one contract the script enforces: exactly one `STATUS:BEGIN`/`STATUS:END` pair in README, exact text between them.

## 7. Tests (how each confirmed to fail without fix)
No new test files (brief confines verification to scoped commands; a pytest wrapper around `--check` is the explicitly banned "pytest that flaps per host"). Verification instead, each demonstrated to fail/passing legitimately:
- **Print stability:** ran `python scripts/status.py` three times on this checkout -> byte-identical `142 / 57,326 / 3,762` each time [RAN -> stable].
- **T-N0-3 on a temp README** (deleted after): stale block -> `--check` rc=1 with diff; `--write` rc=0; `--check` rc=0; hand-edit `3,762`→`9,999` -> rc=1; `--write` -> rc=0; missing markers -> rc=2; duplicated markers -> rc=2; CRLF file -> `--write` preserves CR, `--check` rc=0 [RAN scripts/_selftest.py (removed) -> all 8 cases as wanted]. Reversal: without the fix (no script), every one of these commands errors (file absent).
- **T-N0-3 on the real README:** hand-edited `3,762`→`9,999` -> `--check` rc=1 with unified diff; `--write` restored; `--check` rc=0 [RAN -> 1 then 0]. Reversal: with the pre-change README (no markers), `--check` rc=2 [RAN -> as wanted].
- **Existing locks:** `pytest tests/conventions -q` -> 34 passed, exit 0 [RAN -> pass]; generated block contains no `path:line` citation, so the docs-reference lock's corpus is unchanged except the block lines themselves, which it parses and finds citation-free.
- **Gates:** `ruff check src tests` -> All checks passed [RAN]; `ruff check scripts/status.py` -> All checks passed [RAN]; `mypy scripts/status.py` (strict) -> Success, no issues [RAN]; `mypy src` -> exactly 3 errors, all at `src/paa/sandbox/subprocess_backend.py:548` (Windows host, pre-existing A-001) [RAN].
- **CLI errors:** `--check --write` together -> argparse error (mutual exclusion, exit 2); `--readme` to nonexistent path -> `status: error: cannot read ...` exit 2; `--help` renders the three modes [RAN].

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)
- *Source:* generation is O(total source bytes + test count); a 100× larger tree would only lengthen the collect run (~10 s today). No unbounded collections: the source walk fixes the file set eagerly (142 today) and sums ints; pytest's collector bounds the test list.
- *Load:* `--check` in CI runs on a clean checkout with pinned `[dev]`; no concurrency hazards. Concurrent edits mid-run (src changed between line-count and collect) could pair counts from adjacent tree states — cosmetic in a status block; CI uses clean checkouts.
- *Hostile input:* README text is never interpreted (no YAML/eval/shell); malformed marker layouts (missing/duplicated/reversed) are loud errors, not guesses. `--readme` to an arbitrary path only reads it and (with `--write`) replaces between valid markers via temp+replace — narrow blast radius. A non-UTF-8 source file fails loudly (exit 2) rather than being miscounted; a non-UTF-8 README similarly errors.
- *Operability:* `--check` failure output is a unified diff plus one-line regen instruction. Rollback is `git revert` of this commit (no migration, no data). No flags or modes left behind; nothing to clean up.
- *Reversal:* remove `scripts/status.py` -> `python scripts/status.py --check` errors (no such file) and CI `status` job fails. Restore old README -> `--check` exits 2 (markers absent). Remove the CI job -> no check runs. Each layer fails independently for the right reason.

## 9. Edge cases and abuse scenarios
- Markers missing / duplicated / reversed -> exit 2 with counts (`found 0 and 0`, `found 2 and 2`) [RAN].
- CRLF README (Windows checkout): `--write` preserves the file's EOL style; `--check` normalises before comparing, so CRLF and LF twins compare equal [RAN]. Mixed EOLs are normalised to the file's first EOL on write (noted, acceptable).
- `PYTEST_ADDOPTS=-k ...` on the caller env cannot filter the count (stripped in child env; documented) [code-verified, not run with a real filter — see §15].
- Pytest collection error (broken import) -> non-zero exit propagates as script error exit 2 with the output tail, never a guessed count [code-verified].
- File without trailing newline still counts its final line (`splitlines` semantics, documented).
- `--readme` with a non-`.md` suffix no longer crashes (temp name is `name + ".tmp"`, not `with_suffix`) [fixed during self-review].

## 10. Performance and security
Performance: generation costs one full-tree file read (57k lines) + one pytest collection (~10 s observed). No caching (counts must reflect the checkout, not a cache). No O(N²) anything.
Security: `subprocess.run` argv = `[sys.executable] + constant tuple` — no shell, no untrusted interpolation; the single `subprocess` call matches the pinned command literally. README/README-path text is never executed. No secrets, tokens, or env values printed or committed; `PYTEST_ADDOPTS` is dropped from the child environment, not logged. The `--write` temp file lives beside the README in the same directory and replaces atomically, so a crash cannot leave a half-written README. The one `# noqa: S603` is per-line, justified (fixed argv), and mirrors the repo's per-line-`noqa` convention for S608 (AGENTS.md: a single legitimate line takes a narrow noqa) rather than a per-file allow-list, which AGENTS.md forbids adding.

## 11. Commands run and results (verbatim summaries)
- [RAN `.venv/Scripts/python.exe -m pytest --collect-only -q -o addopts=""` -> `3762 tests collected in 10.28s`]
- [RAN `python scripts/status.py` -> markers + `142 src/**/*.py files, 57,326 physical lines; 3,762 tests collected.` + method line; byte-stable across runs]
- [RAN `python scripts/status.py --check` (pre-README-edit) -> exit 2, `found 0 and 0`]
- [RAN temp-README suite -> stale 1, write 0, fresh 0, hand-edit 1, rewrite 0, no-markers 2, dup 2, CRLF-preserved 0]
- [RAN real-README hand-edit `3,762`->`9,999` -> `--check` exit 1 with unified diff, then `--write`, then `--check` exit 0]
- [RAN `python -m pytest tests/conventions -q` -> `34 passed`, exit 0]
- [RAN `python -m ruff check src tests` -> `All checks passed!`]
- [RAN `python -m ruff check src tests scripts/status.py` -> `All checks passed!`; `ruff format --check` applied to script]
- [RAN `python -m mypy src` -> `Found 3 errors in 1 file`, all `src/paa/sandbox/subprocess_backend.py:548` (killpg/getpgid/SIGKILL, Windows pre-existing)]
- [RAN `python -m mypy scripts/status.py` -> `Success: no issues found in 1 source file`]
- [RAN YAML parse of ci.yml -> jobs `gate`, `status`, `embed-image`; status step 5 is `.venv/bin/python scripts/status.py --check`]
- NOT RUN: full `pytest tests/ -q` (forbidden by scoped-check rule; outcomes intentionally excluded from the block); CI runners (no runner access — job is YAML-validated and step-replicated locally).

## 12. Deviations from plan/brief
1. **Outcome counts excluded from the generated block** (spec §N0.4 wording: "prints source lines, tests collected, and outcome counts"; revised plan:53: "Status generator (source lines, tests collected, outcome counts)"). The block instead carries an explicit refusal plus the measurement command. Justification is the plan's own logic: the revised plan redefines the number-pinning to `cloc`-equivalent + `--collect-only` only, and orders `--check` *instead of* a per-host-flapping pytest (M-6). Measuring outcomes requires executing the suite, which depends on Docker/servers/OS — the cited host-flap source. This deviation is declared here and in the script docstring + block text itself, so no reader can mistake absence for omission. If ORCH wants outcomes in the block, the honest way is a pinned nightly measurement pasted outside the markers — never machine-asserted per host.
2. **`--write` added** (not in brief §3/plan §53). Justification: "passes after regeneration" (T-N0-3) needs a regeneration step; paste-by-hand is error-prone and unscriptable in T-N0-3's manual test. It is opt-in, explicit, and covered by the same marker/validation paths as `--check`. Default mode remains print.
3. **README block placement replaces only the stale snapshot line** (old README line 295), leaving the Build-status table/prose outside the markers. The plan says "README status section between markers" without fixing the exact bytes; replacing the whole section would have swallowed the human-maintained subsystem table (not generated content) and widened the blast radius. The generated numbers were the only stale part, and they are the only part now generated.
4. **CI `status` job pins `[dev]` only** (not the gate matrix). Required by the measured install-shape fact (AGENTS.md + `tests/api/conftest.py` skip behaviour); a matrix run would be red by design on minimal.

## 13. Out-of-scope findings
- **ROADMAP drift (per brief §6, noted, not fixed):** docs/ROADMAP.md:30-33 still quotes 52,000 / 3,145 / 2,899 / 241 / 4 / 1 from `4ac96db`, vs current 57,326 physical / 3,762 collected [VERIFIED docs/ROADMAP.md:30-33]. H1.7 says ROADMAP should "quote its output", but brief §6 explicitly forbids editing ROADMAP numbers beyond the README block. ORCH decision needed: either accept the drift note or widen write-set for a follow-up.
- VISION.md also "quotes its output" per H1.7 and was not touched (outside write-set; W0-IMPL-03 owns docs/VISION.md in W0b.tsv).

## 14. Known limitations, risks, rollback notes
- The collected count (3,762) is true for *this* checkout including W0-IMPL-01's +8 tests. Sibling commits that add tests after this generation will make `--check` red until regeneration — by design (the check is the point), but ORCH should expect one regen right before merge.
- The CI `status` job was validated by YAML parsing and by replicating its two meaningful steps locally (`.venv` + `[dev]` already installed; `scripts/status.py --check` passes). It has not run on a GitHub runner; a runner-side surprise (e.g. runner `pip` index behavior) would surface as job failure, not as a false pass — the failure direction is safe.
- `python scripts/status.py` output stable on same checkout [verified]; cross-checkout stability is intentionally absent (numbers move as the tree moves — that movement is what the check detects).
- Rollback: `git revert` the code commit (3 files); no data migration, no flags.

## 15. Where I am least confident
Outcome-counts exclusion (§12.1) is a judgment call against the spec's literal wording and could be over-read by a reviewer who weighs "complete block" over "non-flapping block" despite M-6. It is the only item I would re-open on ORCH instruction. Secondary: `PYTEST_ADDOPTS` stripping and the pytest-error path are code-read, not fault-injected and run (standing orders discourage wasting runs; the behaviour is direct).

## 16. Self-review checklist
- [x] Re-read diff from scratch (`README.md` 5 lines, `ci.yml` +81, `scripts/status.py` new). Correctness: counting is a pure sum over sorted files; collect parse takes the last match; `--check` compares normalised middle text; exit codes 0/1/2.
- [x] Edge cases: no-trailing-newline counted; CRLF both directions; duplicate/missing/reversed markers; nonexistent README; mutual exclusion; non-`.md` `--readme` suffix (fixed).
- [x] No band-aids: failures raise `StatusError` (never silent defaults); no `any`/ignores except one justified per-line S603; no sleeps/timeouts/retries in code (retry loop is in CI YAML, the repo convention for network fragility); no test weakening; no duplication.
- [x] Tests prove the fix: hand-edit → 1, regen → 0 (twice, temp + real README); missing fix → commands error.
- [x] Conventions: stdlib-only script, line-length 100 (`ruff check` clean), docstrings carry the pinned method + citations, README diff preserves EOL.
- [x] Scope: exactly the three write-set paths (+ this report); `src/**` read-only; ROADMAP/VISION untouched; only scoped commands run (no full suite).

## 17. Commits
- Code: `c09b0f1` — `Add scripts/status.py generator, README status markers, and CI status job (T-N0-4)` (3 files: `scripts/status.py` new, `.github/workflows/ci.yml`, `README.md`). Branch `ops/20261109-next-spec` [RAN `git branch --show-current` -> ops/20261109-next-spec].
- Report: TBD (this file, docs-only commit following).
