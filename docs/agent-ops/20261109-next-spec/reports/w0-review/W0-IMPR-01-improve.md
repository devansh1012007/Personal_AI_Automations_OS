<!-- ORCH-DIGEST:START -->
agent: W0-IMPR-01 (IMPLEMENTER) | task: T-N0-1-IMPR-01 (fix MAJOR-1 + MAJOR-2 in markdown-link lock) | status: DONE
code_commits: 2f26fb8 | report_commit: 21ef990 (fix-up commit amends wording only)
files_changed: 1 (tests/conventions/test_markdown_links.py)
contracts_changed: none
tests: changed 1 (test_the_lock_catches_a_broken_link extended: 5 -> 8 synthetic links, 3 -> 6 defects, + _existence_defects assertions); scoped run: pass (8/8 markdown-links, 5/5 docs-references, 34/34 conventions; ruff All checks passed; mypy exactly 3 baseline)
ownership_violations: none
deferred_dependencies: 1 (MAJOR-3 ../ root-containment — explicitly out of scope per brief §6; see §14/§13)
out_of_scope_findings: 4 (MAJOR-3, MINOR-1, MINOR-2, MINOR-3/NIT-1/NIT-4 untouched — see §13)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: none — delegation chosen per brief's "ideally" branch; equivalence proved by code-read + 34-pass suite + dual-path probe
confidence: high (pre-fix probe reproduced 0-vs-3 divergence myself; post-fix probes pass through the real granular function; ruff 0 / mypy 3 / conventions 34 green)
last_comms_line_read: 2026-11-09T07:30:00Z (re-read at commit time; no new lines)
<!-- ORCH-DIGEST:END -->
# Implementation report — W0-IMPR-01 — T-N0-1-IMPR-01 Fix MAJOR-1 + MAJOR-2 in the markdown-link lock

## 1. Task and acceptance criteria (restated)

Fix two MAJOR false-negatives in `tests/conventions/test_markdown_links.py` (the lock
added in T-N0-1, commit `d22f0d4`), with minimal changes, and re-green the gate.

- **MAJOR-1:** `test_every_relative_link_names_an_existing_path` skipped the
  `<unparseable-link>` sentinel (line ~500) — must report it as a defect (mirror
  `check_link` lines 434–438). Verify `[x](<unterminated` and `[x](guide.md`
  (missing paren) now FAIL the existence test (or `_violations`) instead of skipping.
  Ideally delegate to `check_link`/`_violations` so sentinel handling lives in one
  place (MINOR-4 meta-fix); a minimal one-line report-as-defect is acceptable only if
  MAJOR-2 is also fixed and a synthetic test covers the sentinel.
- **MAJOR-2:** existence test allowlist skip lacked `and not fragment` (line ~503) —
  add the fragment check so `0001-...#foo` is NOT allowlisted (it must be reported,
  not skipped), matching `check_link`/`_violations`.

Acceptance: both probes from review §5 (#1, #2, #6) produce violations in the real
suite (not just `_violations`); `pytest tests/conventions -q` green (34+); `ruff` 0;
`mypy src` exactly 3. Do NOT attempt MAJOR-3 (`../` containment — follow-up), MINOR-1
(nested brackets), MINOR-3 (dedup), NIT-1/3/4 (optional only if 3-line change each
with no corpus impact).

## 2. Verification that the problem exists (evidence gathered myself)

[VERIFIED tests/conventions/test_markdown_links.py:500-505 (pre-fix)] — the granular
existence test contained `if is_external(path) or link.destination == "<unparseable-link>": continue`
and `if _is_allowlisted(link.document, path): continue` (no fragment check), while
`check_link` at lines 434–439 reported the sentinel and required
`allowlisted and not fragment`. The two paths diverged exactly as the review states.

[RAN `.venv\Scripts\python.exe ...\opencode\w0-impr01-repro.py` -> REPRODUCED]:
synthetic tree (2 malformed links, healthy link, bare allowlisted name, fragmented
allowlisted name). `find_links` yields the sentinel for both malformed forms;
`_violations` flags 3 defects; the current granular existence test body (faithful
in-line copy of lines 495–509, pointed at the synthetic root) reports **0**
(`sentinel in granular: 0`, `fragmented in granular: 0`). MAJOR-1 and MAJOR-2 both
confirmed by my own run, not just the review's probes.

[RAN `pytest tests/conventions/test_markdown_links.py tests/conventions/test_docs_references.py -q`
-> 13 passed, exit 0] — pre-change baseline green, so the defects were silent
false-negatives, not test failures.

## 3. Root cause

Guard duplication (review MINOR-4, the meta-finding). The synthetic self-test drives
`_violations`/`check_link`, which handled both classes correctly, but the two real
granular tests re-encoded the guard policy inline (external skip, allowlist skip,
sentinel skip) and silently diverged from it. The lock therefore proved the *helper*
correct while the *suite* — the thing CI actually runs — let exactly the broken-link
classes the task exists to catch pass silently: malformed destinations (render as
literal brackets on GitHub, never 404) and allowlisted-name-plus-fragment links.

## 4. Approach chosen and why (alternatives rejected)

**Chosen: full delegation with a single source (the brief's "ideally" branch).**

- New `_existence_defect(link, document_path, root)` holds the existence rules in
  one place: external → no defect; `_UNPARSEABLE` → defect; `resolve(...) is None`
  → defect unless (`_is_allowlisted` AND not fragment). Order and messages are
  verbatim from the old `check_link` (external → sentinel → resolve → allowlist),
  and `_existence_defect` is called by both `check_link` and the granular path.
- `check_link` now delegates existence to `_existence_defect`; after it returns None,
  `if not fragment: return None`, then the existing fragment checks unchanged
  (dedented), with `assert target is not None` guarding the invariant (message
  names the link). The only other None case of `_existence_defect` — the allowlisted
  bare name — implies no fragment and returns by the guard, so the assert cannot
  fire while the invariant holds; if a future refactor breaks it, the suite fails
  loudly at the link rather than with an `AttributeError` on `None`.
- New `_existence_defects(root)` wraps `all_links(root)` + `_existence_defect`; the
  granular existence test is now a two-line call to it. The anchor test is left
  untouched: it already delegates to `check_link`, and its `if target is None: continue`
  partition (avoid double-reporting a missing file under the anchor label) is
  intentional — sentinel destinations have no fragment and fragmented-allowlist
  links resolve to None, so both report through the existence test exactly once.
- `_UNPARSEABLE` module constant replaces the magic string at the three sites
  (`find_links`, `check_link`→`_existence_defect`, existence test→`_existence_defect`
  — now two literal sites gone plus the emitter). This is review NIT-3; it is part
  of the MAJOR-1 "one place" goal, is behavior-identical, and has no corpus impact,
  so it falls under the brief's "3-line change, no corpus impact" allowance. NIT-1
  (strict UTF-8 read) and NIT-4 (`*.MD` glob) were left untouched.

**Rejected alternative (minimal two-line patch):** just un-skip the sentinel and add
`and not fragment` in the existence test. It would fix the two symptoms but leave
MINOR-4 standing — the next guard change would hand-diverge again (the review
already proved this failure mode is active, not hypothetical). The delegation is
small (same file, tests only) and the brief explicitly prefers it, so the larger
but architectural fix is the correct scope.

**Rejected:** collapsing both granular tests into one `_violations` call. That
would lose the existence-vs-anchor partitioning that makes CI failures actionable
("broken links" vs "broken anchors") and would change test names/count. The chosen
design keeps both test faces and shares the policy underneath.

## 5. What changed (per file)

Only `tests/conventions/test_markdown_links.py` (WRITE-SET ✓). Code SHA `2f26fb8`,
125 insertions / 53 deletions (deletions dominated by the `check_link` dedent).

| Area | Change |
|---|---|
| Sentinel constant | New `_UNPARSEABLE = "<unparseable-link>"` after `_NON_SLUG` (line ~160). |
| `find_links` | Emits `_UNPARSEABLE` instead of the literal. |
| `_existence_defect` (new) | Single source for existence rules (external/sentinel/allowlist+not-fragment/missing). |
| `check_link` | Delegates existence to `_existence_defect`; fragment checks unchanged apart from dedent; assert guards the exists-invariant. |
| `_existence_defects` (new) | `all_links` + `_existence_defect` over a root, link order — the function the real granular test runs. |
| `test_every_relative_link_names_an_existing_path` | Two lines: `defects = _existence_defects()` + assert. Docstring now states sentinel is reported, not skipped. |
| `test_the_lock_catches_a_broken_link` | Synthetic tree +2 malformed links (`<unterminated`, missing-paren), +1 fragmented allowlist link. Links 5→8, `_violations` defects 3→6, plus 5 new assertions through BOTH `_violations` and `_existence_defects` (sentinel count exactly 2, fragmented present, bare allowlist absent from defects, existence defects exactly 4 and anchor-free). |

No other file touched. No docs links edited (not needed — see §6).

## 6. Blast radius and contracts

Test-only change; no `src/**`, no docs, no config, no public interface. The module's
helpers (`check_link`, `_violations`, `resolve`, `find_links`) keep their signatures
and defect strings (messages kept verbatim — sentinel message, "no such file or
directory", directory/non-markdown/anchor messages all unchanged), so downstream
assertions in the same file (`test_allowlist_only_covers_genuinely_missing_adr_bodies`,
`test_the_lock_is_not_vacuous`, scope-drift test, slug tests) are unaffected.

Corpus impact verified zero: [RAN post-fix probe -> `real corpus:
existence defects=0 total violations=0`]. Both newly-reported classes are absent
from the current corpus (review confirms 0 sentinels / 0 fragmented allowlist links
in 349 links), so the suite stays green without touching any doc.

Behavior deltas, all in the defect-producing direction: (a) sentinel → defect in
existence test (was: skipped); (b) allowlisted-name+fragment → defect in existence
test (was: skipped); (c) nothing else. Previously-passing links still pass:
external, empty destination (resolves to the linking document, no fragment —
unchanged), directory targets, bare allowlisted names.

## 7. Tests (how each confirmed to fail without fix)

`test_the_lock_catches_a_broken_link` is the regression pin for both classes. The
new assertions were validated against the **pre-fix behavior** by the §2 repro probe:
under the old granular logic the sentinel/fragmented existence counts were 0, so
`existence_joined.count("parenthesis never closes") == 2` and
`"0001-sqlite-over-postgres.md#foo" in existence_joined` would fail against any
code that skips them — including the old code. Conversely, the `_violations` half
of the assertions passed pre-fix (helper was already correct) and still passes,
pinning parity. Mutation mental check: deleting `_existence_defect`'s sentinel
branch → count 0 ≠ 2 → fail; deleting `and not fragment` → fragmented link resolves
None and is allowlisted-skipped → `#foo` assertion fails; the bare-name-must-not-appear
assertion (`joined.count("0001-sqlite-over-postgres.md") == 1`) guards against
over-broad allowlist removal (if the allowlist were dropped entirely, the bare
`0001` link would appear as a defect and the count would be 2 → fail).

[RAN `pytest tests/conventions/test_markdown_links.py -v` -> 8 passed in 1.45s]
[RAN `pytest tests/conventions/test_docs_references.py -q` -> 5 passed, exit 0]
[RAN `pytest tests/conventions -q` (34 collected) -> 34 passed in 195.85s]
[RAN post-fix probe `w0-impr01-postfix.py` -> all review §5 probes #1/#2/#6
reported by `_existence_defects(root)` (2 sentinel defects incl. both forms,
fragmented de-US + de-IR), `_violations` agreement, real corpus 0/0]

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)

- **Source.** The fix is at the source of the divergence: existence policy now has
  exactly one implementation (`_existence_defect`), consumed by `check_link`
  (synthetic self-test, anchor test) and the granular existence test
  (`_existence_defects`). A future guard change cannot silently desync the suite
  from the helper — MINOR-4 closed, not patched around.
- **Load.** The delegation adds one extra `Path.exists()` stat per link in
  `check_link` (existence resolve, then fragment-section resolve) — ~350 links,
  negligible; measured: markdown-links file 1.45s, full conventions 195.85s
  (dominated by the pre-existing import sweep, unchanged from review's 214s).
  No new collections, loops, or state; `_existence_defects` is a single linear pass.
- **Hostile input.** Malformed destinations from fuzzer-ish inputs (`<unterminated`,
  missing `)`) still produce the sentinel, now reported by both paths. Oversized
  lines are bounded by line length (unchanged parser). No new I/O, network, or
  subprocess surfaces; reads unchanged.
- **Operability.** Failure output is unchanged in shape: `document:line` +
  destination + defect class — now including `the link parenthesis never closes on
  the line` and `no such file or directory` for the two closed classes, from both
  granular tests. Rollback = revert `2f26fb8`; no migration, no data, test-only.
- **Reversal.** Revert any break of the fix and the suite goes red for the right
  reason: restore the sentinel `continue` (or delete the sentinel branch) →
  `count("parenthesis never closes") == 2` fails; restore allowlist-without-fragment
  → `#foo` assertions fail; stub `_existence_defects` to `[]` → counts fail. The
  reversal test that §3 of the review said did not exist now does — through the
  same function the real test runs.

## 9. Edge cases and abuse scenarios

Traced against the re-read diff: external schemes/`//` (skipped first, both paths);
empty destination (resolves to the linking document — pass, unchanged, verified
by suite); directory target (existence pass, fragment still fails in `check_link`
— unchanged); non-markdown+fragment (unchanged); percent-encoding (unchanged
decode in `resolve`); `..` escapes (MAJOR-3, untouched — §13); multi-`#`
destinations (`split_destination` partitions on first `#` — unchanged). The
`assert target is not None` in `check_link` names the link in its message, so a
hypothetical future divergence fails with a diagnosis instead of a crash or a
silent pass. Assert-stripping (`-O`) is not this repo's tooling (bare asserts are
the file's house style and ruff already accepts them).

## 10. Performance and security

Test code only; no new imports, no network/subprocess/writes added. `_existence_defect`
re-splits the destination (linear in destination length). ruff 0 — no new finding;
the new `assert` is consistent with file convention. No type suppression, no `noqa`,
no `cast`, no `any` added. `mypy` unaffected (test file is outside `mypy src` by
repository design; the 3 remaining errors are the documented Windows baseline).

## 11. Commands run and results (verbatim summaries)

- `[RAN .venv\Scripts\python.exe C:\...\opencode\w0-impr01-repro.py -> _violations 3 defects; current granular existence 0 defects; sentinel-in-granular 0; fragmented-in-granular 0 (MAJOR-1 + MAJOR-2 reproduced)]`
- `[RAN pytest tests/conventions/test_markdown_links.py tests/conventions/test_docs_references.py -q (pre-change) -> 13 passed, exit 0]`
- `[RAN pytest tests/conventions/test_markdown_links.py -v -> 8 passed in 1.45s]`
- `[RAN pytest tests/conventions/test_docs_references.py -q -> 5 passed, exit 0]`
- `[RAN python C:\...\opencode\w0-impr01-postfix.py -> _existence_defects 4 defects (2 sentinel: <unterminated + missing paren; fragmented 0001 + 0002); _violations agreement; real corpus 0/0; POST-FIX PROBE OK]`
- `[RAN .venv\Scripts\python.exe -m ruff check src tests -> All checks passed!, exit 0]`
- `[RAN .venv\Scripts\python.exe -m mypy src -> Found 3 errors, all src/paa/sandbox/subprocess_backend.py:548 attr-defined (killpg/getpgid/SIGKILL) — exactly the documented Windows baseline A-001]`
- `[RAN pytest tests/conventions --collect-only -> 34 collected; pytest tests/conventions -q -v -> 34 passed in 195.85s]`
- `[RAN git diff --stat -> 1 file changed, 125 insertions(+), 53 deletions(-); git status -> only the WRITE-SET file modified + this report untracked + pre-existing ORCH run-trail untracked files]`
- `[RAN git log --oneline -2 -> 2f26fb8 on top of 1ccf3f0; branch ops/20261109-next-spec verified before commit]`

Full-suite `pytest tests/ -q` NOT run (scoped-checks rule).

## 12. Deviations from plan/brief

- **Chosen the "ideally" branch** (delegation + single source) instead of the
  minimal two-line patch — explicitly permitted by the brief, and it closes
  review MINOR-4 rather than leaving it. Documented in §4.
- **NIT-3 included** (sentinel constant): behavior-identical, no corpus impact,
  within the brief's "3-line change, no corpus impact" allowance, and directly
  part of the "sentinel handling in one place" goal. NIT-1/NIT-4 left for separate
  follow-ups.
- **No COMMS.md write.** COMMS is append-only but ORCH-only for commits; my
  WRITE-SET is the test file. The one deferral (MAJOR-3) is documented here as
  DEFERRED-DEPENDENCY (§13/§14) so ORCH can post it at the wave boundary. No NEED
  arose: no change outside the WRITE-SET was required, and no docs link broke.

## 13. Out-of-scope findings

1. **MAJOR-3 (`../` root containment) — DEFERRED-DEPENDENCY.** `resolve`
   (test_markdown_links.py:379-400) follows `../` out of the repository and accepts
   the link if the outside target exists (non-deterministic dev-vs-CI). Proper fix:
   require the resolved candidate to be inside `root` (`candidate.resolve().is_relative_to(root.resolve())`)
   in `resolve`/`check_link`, with review §5 probe #9 as the regression test. A
   separate follow-up task; explicitly excluded by brief §6. My delegation does not
   touch `resolve`, so this task neither fixes nor widens it.
2. **MINOR-1 (nested bracket text missed)** — `_CANDIDATE` regex; left untouched
   per brief. Corpus has none.
3. **MINOR-2 (repo-root fallback leniency)** — by design per plan wording; review
   suggests only a docstring note. Left untouched.
4. **MINOR-3 / NIT-1 / NIT-4** — anchor dedup collision, strict-UTF-8 read consistency,
   `*.MD` glob. Left untouched per brief (NIT-3 done — see §12).

## 14. Known limitations, risks, rollback notes

- LIMITATION: MAJOR-3 stands (see §13.1) — a `../../outside.md` link that resolves
  outside the repo still passes. Corpus contains none; filed as follow-up, not
  bolted on, per brief.
- LIMITATION: the `assert target is not None` in `check_link` assumes the filesystem
  state is stable between the existence resolve and the fragment resolve. True for
  the test's read-only use; if a target vanished mid-scan the lock fails with a
  named message (loud, not silent) — strictly better than the old silent divergence.
- RISKS: minimal. Test-only; corpus verified 0 defects through both paths; no doc
  edits needed; no contract change.
- ROLLBACK: `git revert 2f26fb8` (this task's code commit only). This report is a
  separate follow-up docs-only commit.

## 15. Where I am least confident

Low residual uncertainty. The equivalence argument for `check_link`'s restructure
rests on code-read (order of external→sentinel→resolve→allowlist preserved) plus
the full conventions suite passing, plus the dual-path probe agreeing on the
synthetic tree and on the real corpus. [UNKNOWN] (not run): Linux behavior — this
host is Windows; CI will be the first Linux run, but the change is pure path/string
logic with no platform surface beyond `Path.exists()` (already exercised).

## 16. Self-review checklist

- Correctness/root cause: existence policy had two implementations and diverged;
  now one (`_existence_defect`) shared by all three consumers. Sentinel and
  fragment conditions match the old `check_link` verbatim. CRITICAL: none.
  MAJOR: none. MINOR: the `assert` instead of a fallback missing-defect return —
  deliberate (loud > silent), message names the link.
- Security/edge: as §§8–9; no new attack surface; boundary cases re-traced.
- Maintainability: `_existence_defect`/`_existence_defects` documented with the
  single-source rationale; `_UNPARSEABLE` removes magic-string coupling; guard
  duplication (MINOR-4) removed, not worked around.
- Test rigor: the extended synthetic test fails on the old behavior by direct
  pre-fix probe evidence (§7); the bare-allowlist `count == 1` assertion guards
  against over-correction; all 8+5 scoped tests + 34 conventions tests pass.
- Conventions: ruff 0, mypy exactly 3; no `noqa`/`type: ignore`/suppressions
  added.

## 17. Commits

- Code commit: `2f26fb8` — `git add -- tests/conventions/test_markdown_links.py` then
  `git commit -m "Fix MAJOR-1/MAJOR-2: single-source existence rules in markdown-link
  lock (T-N0-1-IMPR-01) ..." -- tests/conventions/test_markdown_links.py` [RAN ->
  committed, `git log --oneline -2`: `2f26fb8` on top of `1ccf3f0` (W0-REV-01);
  branch `ops/20261109-next-spec` verified with `git branch --show-current` before
  committing]. 1 file changed, 125 insertions, 53 deletions.
- Report commit (docs-only, separate): `21ef990` — this file, committed with an
  explicit path and a commit message listing code commit `2f26fb8`. (A follow-up
  wording fix-up records this SHA in the file.)
