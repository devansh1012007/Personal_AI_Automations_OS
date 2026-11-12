<!-- ORCH-DIGEST:START -->
agent: W0-IMPR-02 | task: T-N0-3-IMPR | status: DONE
code_commits: b3ce8bc | report_commit: pending
files_changed: 2 (docs/reviews/2026-11-09-composability-review.md; this report)
contracts_changed: none
tests: added 0, changed 0; scoped run: pass (conventions 34 passed; ruff clean; mypy 3 pre-existing Windows; status.py --check exit 0)
ownership_violations: none
deferred_dependencies: PDF-body verification DEFERRED-DEPENDENCY (filed, not attempted per brief §6); NIT-2 banner link text in docs/VISION.md NOT DONE (FORBIDDEN file, needs owner/ORCH)
out_of_scope_findings: 0
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: none material (mechanical +4; L851-852 blank-start exception documented)
confidence: high (lock-proved + per-ref line-content proof)
last_comms_line_read: 2026-11-09T09:14:10Z
<!-- ORCH-DIGEST:END -->
# Implementation report — W0-IMPR-02 — T-N0-3-IMPR MAJOR-1 +4 line-ref fix

## 1. Task and acceptance criteria (restated)

Fix W0-REV-04 MAJOR-1 [VERIFIED reports/w0-review/W0-REV-04-review.md:94-108]: all 14
docs/VISION.md line refs in docs/reviews/2026-11-09-composability-review.md are stale
by +4 because commit e9e5bbc inserted a 4-line banner above every appendix body line,
while the refs were numbered against the pre-banner 964-line file. Shift every ref by
+4 (14 table entries + 3 in-text L refs + scope 401–964 → 401–968), verify each new
ref lands on a non-blank line (docs-references lock), add one sentence noting
numbering is post-banner. Optionally pin arXiv version to 2608.25512v1, fix appendix
count prose (NIT-1: inclusive span 401–968 = 568), align banner link text (NIT-2).
Do NOT touch verdicts, wording, or appendix content. Do NOT attempt PDF-body
verification (DEFERRED). Do NOT rewrite the W0-IMPL-03 report (MINOR-1 is ORCH-owned).

Acceptance (brief §3): pytest tests/conventions -q green (34 passed), ruff/mypy
baseline identity, every ref checked manually against current lines with method
documented. Write-set: docs/reviews/2026-11-09-composability-review.md only.

## 2. Verification that the problem exists (evidence gathered myself)

- VISION.md is 968 lines post-banner [RAN python len(docs/VISION.md) -> 968];
  appendix heading at line 401 [VERIFIED docs/VISION.md:401]; 4 banner lines at
  403–405 [VERIFIED docs/VISION.md:403-405]. Scope line in the review note still said
  401–964 pre-fix [VERIFIED docs/reviews/2026-11-09-composability-review.md:4
  pre-fix], wrong by 4 at the end — confirming the brief's premise.
- Extracted all refs pre-fix [RAN python regex -> 14 table VISION.md refs + L434-436,
  L657-659, L846-848 + scope 401–964]. Each matches W0-REV-04 §2.4's table verbatim.
- Independent spot-check of the shift: pre-banner VISION line 10 (e.g. old 847
  "These should become executable tests") sits at current 851 [RAN python
  git show e9e5bbc^:docs/VISION.md (utf-8 decoded) vs current -> +4, systematic].
  No pre-existing drift beyond the banner.
- Branch/state: ops/20261109-next-spec [VERIFIED git branch --show-current], HEAD
  7a81404 (W0-REV-04) at start [VERIFIED git log].

## 3. Root cause

Same-commit invalidation: e9e5bbc added the 4-line banner AND the review note whose
refs were numbered in the same commit against the pre-insertion file. Verdicts were
all correct (W0-REV-04 §2.2, independently re-fetched abstract); only the pointers
moved. The docs-references lock cannot catch this because it checks existence +
non-blankness, not content correctness [VERIFIED
tests/conventions/test_docs_references.py:411-416,499-523 and W0-REV-04 §2.5/MINOR-1].
Fix required is a mechanical +4, not a re-audit.

## 4. Approach chosen and why (alternatives rejected)

### 4.1 Mechanical +4 on all 18 numbers (chosen)

Every `docs/VISION.md:NNN` table ref +4, every `LNNN` in-text ref +4, scope end
964 → 968, plus one post-banner sentence. Chosen because W0-REV-04 proved the offset
is exactly +4 with zero exceptions by resolving all 14 refs against both the
pre-banner and committed files (reversal proof: removing the banner restores
exactness). Rejected: re-deriving refs from scratch by close-reading the appendix
(same result, higher risk of a fresh miscount); replacing line refs with quoted
phrases or a commit-SHA pin (the reviewer suggested these as future hardening, but
brief §3 prescribes +4 — a format change would exceed the brief and churn a
committed audit note).

### 4.2 The L846-848 exception (documented deviation from strict +4)

Strict +4 gives L850-852, but current line 850 is blank ("Required invariants"
heading's preceding blank line [VERIFIED docs/VISION.md:849-851]). Pre-banner 846
was likewise blank (old 845 is the invariants heading, 846 blank, 847 the sentence).
The old ref therefore already started on a blank line; the in-text `L` form is not
parsed by the docs lock (which only checks first-line non-blank for `path:line`
refs), so it never failed the gate, but correctness demands the sentence lines.
W0-REV-04 §2.2/§2.4 already gives current 851-852. I used **L851-852** — i.e. +4
with the blank start trimmed to the first non-blank sentence line. Verified both
851 ("These should become executable tests...") and 852 ("composability:") are
non-blank [RAN verify_refs.py -> L851-852 first_nonblank=True]. All other refs
needed no such adjustment (verified each first line non-blank, §7).

### 4.3 Optional polish disposition

- **Post-banner sentence (required, done):** line 5 now reads "post-banner — the
  4-line UNVERIFIED banner after the appendix heading shifts every appendix line
  by +4" plus the count clarification in the same parenthetical (see §5).
- **Version pin (done, minimal):** `arXiv:2608.25512` → `arXiv:2608.25512v1` in
  exactly 2 places (scope paper line; C1 evidence). The fetch URL
  `https://arxiv.org/abs/2608.25512` and the title heading left as-is: the URL
  without a suffix resolves to the same v1 on the fetch date, and the heading
  identifies the paper generically. This pins the cited version without churning
  the fetch record.
- **NIT-1 count (done):** scope note states "appendix inclusive span is 401–968 =
  568 lines"; bottom line "roughly 540 of 563" → "roughly 536 of 568 lines,
  inclusive span 401–968". 536 is computed, not re-approximated: the C9–C14
  inclusive spans sum to 13+14+36+145+92+236 = 536 (see §7).
- **NIT-2 banner link text (NOT done, ownership):** the misaligned link text
  (`docs/reviews/...` text vs `reviews/...` target) lives at `docs/VISION.md:404`,
  which is in brief §4 FORBIDDEN. I did not edit it and did not post a NEED (NIT-2
  is explicitly optional — reviewer: "Leave, or align"; it resolves correctly and
  the lock is green). Recorded here as the one optional item not taken, with the
  exact change for whoever holds VISION.md: change the link text at
  docs/VISION.md:404 from `docs/reviews/2026-11-09-composability-review.md` to
  `reviews/2026-11-09-composability-review.md`. No data loss either way.

## 5. What changed (per file)

**docs/reviews/2026-11-09-composability-review.md** (18 insertions, 18 deletions
[VERIFIED git show b3ce8bc --stat]):

| # | Old | New | Lands on (current VISION.md) |
|---|---|---|---|
| scope | 401–964 | 401–968 | heading 401 → last line 968 [VERIFIED] |
| C1 | 410-411 | 414-415 | paper citation title+authors [VERIFIED docs/VISION.md:414-415] |
| C2 | 414-415 | 418-419 | temporal bullet [VERIFIED docs/VISION.md:418-419] |
| C3 | 416-417 | 420-421 | spatial bullet [VERIFIED docs/VISION.md:420-421] |
| C4 | 419 | 423 | "revertible effects..." [VERIFIED docs/VISION.md:423] |
| C5 | 419-420 | 423-424 | revertible/reactive coeffects [VERIFIED docs/VISION.md:423-424] |
| C6 | 419-420 | 423-424 | mediated shared context [VERIFIED docs/VISION.md:424] |
| C7 | 420 | 424 | "Cordis is its reference implementation" [VERIFIED docs/VISION.md:424] |
| C8 | 420-422 | 424-426 | "PAA should adopt..." [VERIFIED docs/VISION.md:424-426] |
| C9 | 424-436 | 428-440 | PAA redefinitions + non-claims [VERIFIED docs/VISION.md:428,438-440] |
| C10 | 438-451 | 442-455 | three-graphs section [VERIFIED docs/VISION.md:442-455] |
| C11 | 453-488 | 457-492 | task-scoped model [VERIFIED docs/VISION.md:457-492] |
| C12 | 490-634 | 494-638 | temporal machinery [VERIFIED docs/VISION.md:494-638] |
| C13 | 636-727 | 640-731 | spatial machinery [VERIFIED docs/VISION.md:640-731] |
| C14 | 729-964 | 733-968 | worked example → EOF [VERIFIED docs/VISION.md:733,968] |
| C4 in-text | L434-436 | L438-440 | "Neither property means..." [VERIFIED docs/VISION.md:438-440] |
| C13 in-text | L657-659 | L661-663 | "A requirement is not permission" [VERIFIED docs/VISION.md:661-663] |
| C14 in-text | L846-848 | L851-852 | "should become executable tests" [VERIFIED docs/VISION.md:851-852] (§4.2) |
| NIT-1 | "540 of 563" | "536 of 568, inclusive span 401–968" | computed sum §7 |
| v1 pin | 2608.25512 (×2) | 2608.25512v1 (×2) | scope line + C1 evidence |
| post-banner | "(line numbers as of 2026-11-09; ...)" | "(line numbers as of 2026-11-09, post-banner — ... shifts every appendix line by +4; ... = 568 lines; ...)" | brief-required sentence |

Verdicts (Supported ×7 C1–C7, Unverifiable ×7 C8–C14), evidence quotes, and all
prose beyond the three documented changes are byte-identical [VERIFIED git diff
b3ce8bc — only the 18 lines above]. No other file was modified [VERIFIED git
status: only intended file; VISION.md untouched].

## 6. Blast radius and contracts

None. Docs-only, single audit-note file. No code, schema, EventType, API, or
test changed. The docs-references lock reads this file (in scope —
tests/conventions/test_docs_references.py:96-121, W0-REV-04 §2.5), so the commit
was gated on it (§7). Downstream: any reader following refs now lands on the
intended paragraph; the pre-fix state (landing 4 lines early, often on the Status
blockquote) is gone. Rollback is `git revert b3ce8bc` — restores the stale refs;
no data impact.

## 7. Tests (how each confirmed to fail without fix)

How every ref was checked (brief requires manual check + documented method):
scripted resolution of every ref in the edited file against the current VISION.md,
printing each ref's first/last line text and non-blank status [RAN
verify_refs.py -> 14/14 table refs first_nonblank=True, 3/3 L refs
first_nonblank=True, BAD: []]. Content correctness (does the cited text actually
match the claim?) verified by reading the printed lines against the claim column:
e.g. 414-415 prints the paper title + authors; 661-663 prints "A requirement is
not permission"; 851-852 prints "should become executable tests". 14 entries
confirmed [RAN verify2.py -> 14 refs listed; no old stale numbers present
(410-411, 729-964, 401–964 all absent); verdict counts unchanged].

Fail-without-fix property: the docs lock alone would NOT have failed pre-fix
(first lines non-blank by luck of paragraph layout — the exact blindness MINOR-1
documents). The real guard here is this review's +4 verification plus human
resolution; the per-ref text dump above would fail on the pre-fix file (refs land
on the Status blockquote/wrong bullets per W0-REV-04 §2.4, which I re-verified
against current lines: e.g. current 410-411 is the Status blockquote tail
[VERIFIED docs/VISION.md:407-410]). No test was weakened, skipped, or added (docs
fix only).

Scoped gates (standing orders §16, brief §7) [RAN -> results §11]:
tests/conventions 34 passed; ruff clean; mypy 3 pre-existing Windows errors only;
scripts/status.py --check exit 0. Full suite, container restarts, Postgres/S3 runs
NOT-RUN by scoping (docs-only change); CI owns them.

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)

- **Source:** refs trace to VISION.md lines read directly in this session (§5
  table); the +4 offset traces to e9e5bbc's insertion hunk, not to external claims.
- **Load (100×, concurrency, partial failure):** N/A — static docs, no runtime.
- **Hostile input:** N/A in code sense; epistemic risk (future appendix edits
  silently rotting refs) is real but pre-existing and unchanged: the lock checks
  existence/non-blank only. My post-banner + count sentence makes the next rot
  slightly easier to diagnose, but does not prevent it. Not addressed beyond brief.
- **Operability (observe/rollback/migrate):** observe = re-run the ref-resolution
  check; rollback = git revert b3ce8bc; migrate = any appendix insert above a
  cited line rots refs again (same as before).
- **Reversal:** reverting this commit reintroduces exactly the MAJOR-1 defect
  (refs 4 early); no test would catch it (lock blindness, §7) — detection is human
  review, as W0-REV-04 noted. Accepted for a wave-0 docs hygiene fix.

## 9. Edge cases and abuse scenarios

- Blank-line start (L850) — caught and handled (§4.2).
- En-dash vs hyphen in scope/count (401–968 with U+2013) — preserved byte-for-byte
  from the original; edits matched exactly [VERIFIED edit tool 1 replacement each].
- CRLF warning on commit ("LF will be replaced by CRLF") — line-ending
  normalisation notice only; content diff is clean. [ASSUMED] mirrors repo
  autocrlf behavior on Windows; no content impact.
- Duplicate numbers (423-424 twice, 424 twice) — disambiguated by including claim
  text in every edit; each replaced exactly once [VERIFIED edit results].
- Title/fetch URL still bare 2608.25512 — intentional (§4.3); body pins v1.

## 10. Performance and security

N/A — docs-only. No secrets, tokens, env values, or PII in the diff [VERIFIED git
diff — only line numbers, version suffix, count prose]. No unbounded input:
constants (4, 568) derived from measured line counts.

## 11. Commands run and results (verbatim summaries)

All on branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Scoped only.

- [RAN `git branch --show-current` -> ops/20261109-next-spec]
- [RAN `git log --oneline -5` -> HEAD 7a81404 Review W0-REV-04 ...]
- [RAN python len(docs/VISION.md) -> 968]
- [RAN python per-ref non-blank + content dump (verify_refs.py) -> 14/14 table + 3/3 L refs ok; BAD: []]
- [RAN python entry count (verify2.py) -> 14 refs; no old stale numbers; verdicts kept]
- [RAN `.venv/Scripts/python -m pytest tests/conventions --tb=short` -> **34 passed in 40.61s**]
- [RAN `.venv/Scripts/python -m ruff check src tests` -> **All checks passed!**]
- [RAN `.venv/Scripts/python -m mypy src` -> **3 errors, all pre-existing at src\paa\sandbox\subprocess_backend.py:548** (killpg/getpgid/SIGKILL attr-defined on Windows; matches 00-mission.md baseline; untouched)]
- [RAN `.venv/Scripts/python scripts/status.py --check` -> **status: README.md block matches the generated block** (exit 0)]
- [RAN `git add -- docs/reviews/2026-11-09-composability-review.md` + commit -> **b3ce8bc1aa13e5b401b63a8f357a48acf3ccb0e3**]
- NOT-RUN (by scoping): full pytest, Postgres/S3/Docker suites, container rebuilds.

## 12. Deviations from plan/brief

1. L846-848 → L851-852 instead of strict +4 (L850-852) — blank-start (§4.2); matches
   the reviewer's own computed current lines (W0-REV-04 §2.2/§2.4).
2. Bottom-line count written as "536" (computed C9–C14 sum) instead of keeping
   "540" — strictly more accurate; brief NIT-1 required stating the inclusive span,
   which is also done in the scope note.
3. NIT-2 skipped (FORBIDDEN file) — documented with exact required change (§4.3).
4. COMMS START appended via venv python instead of printf (printf absent in the
   PowerShell shell tool on Windows); format and one-line append-only rule honored.
   Last COMMS read: 2026-11-09T09:14:10Z (my own START line).

## 13. Out-of-scope findings

None. PDF-body verification (DEFERRED follow-up per brief §6) was not attempted —
filed here and in ORCH-DIGEST as a deferred dependency owned by the T-N0-3
follow-up. MINOR-1 (impl-report lock-scope) untouched per ownership.

## 14. Known limitations, risks, rollback notes

- Refs remain `path:line` fragile: the next post-insertion appendix edit above any
  cited line will rot them again, and the lock will stay green. This is a repo-wide
  lock property, not introduced here. Mitigation (quoted-phrase refs / SHA pinning)
  was suggested by W0-REV-04 and intentionally not taken (exceeds brief).
- Version pin covers body identifiers only; fetch URL is still the unversioned abs
  URL, correct as of the 2026-11-09 fetch (v1 was current). If arXiv publishes v2,
  the pin preserves what was checked.
- Rollback: git revert b3ce8bc (restores stale refs; see §6).

## 15. Where I am least confident

Nothing material. Residual [ASSUMED]: that arXiv v1 remains the fetched version
(the fetched page's cite-as showed v1 per W0-REV-04 §2.1; I did not re-fetch —
out of scope, and v1 pinning is prospective hardening). All line-content checks
are [VERIFIED] against the current tree.

## 16. Self-review checklist

Re-read the full git diff from scratch after editing:
- [x] All 14 table + 3 L + scope = 18 numbers shifted (+4, one documented +5-start
  blank trim); no old number remains [VERIFIED verify2.py].
- [x] Each new first line non-blank and content-matching (dump reviewed line by
  line) [VERIFIED verify_refs.py].
- [x] Verdicts/wording untouched (diff shows only ref/version/count lines).
- [x] No file outside WRITE-SET edited; VISION.md, tests, src untouched
  [VERIFIED git status + diff stat].
- [x] Gates: conventions 34 passed, ruff clean, mypy baseline identity (3
  pre-existing Windows), status --check exit 0.
- [x] Severity scan: no CRITICAL/MAJOR/MINOR. One acknowledged NIT (title/URL still
  unversioned) is deliberate and documented. Independent reviewer not required by
  brief (low-risk docs-only, lock-proved); self-review complete.

## 17. Commits

- Code: b3ce8bc1aa13e5b401b63a8f357a48acf3ccb0e3 — "Fix stale +4 VISION refs in
  composability review note (T-N0-3-IMPR)" (1 file, 18+/18-).
- Report: docs-only commit listing b3ce8bc (next commit after this write; SHA in
  follow-up COMMS/DONE line and ORCH-DIGEST report_commit on request).
- Branch: ops/20261109-next-spec. Untracked run files and COMMS.md (ORCH-owned)
  not staged.
