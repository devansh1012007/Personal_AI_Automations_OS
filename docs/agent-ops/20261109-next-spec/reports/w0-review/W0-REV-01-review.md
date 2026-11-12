<!-- ORCH-DIGEST:START -->
agent: W0-REV-01 | scope: wave 0 / T-N0-1 (VISION move + markdown-link lock, d22f0d4) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 3 | MINOR 4 | NIT 4
claims_audit: 11 claims checked / 1 partially false
band_aids_found: none
top_3_risks: malformed links invisible to real suite; allowlisted-name+fragment divergence; ../ escape to outside file passes
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WORTH-IT 2
reversal_check: done (resolution probes, no file edits)
confidence: high (all central claims re-run; 349 links 0 violations independently reproduced)
<!-- ORCH-DIGEST:END -->
# Code review — W0-REV-01 (T-N0-1, commit d22f0d4)

## 0. Method

Blind pass first: read `tests/conventions/test_markdown_links.py` (657 lines) and the diff (`git diff b8570a6..d22f0d4 -- docs/README.md docs/ROADMAP.md` + `git show d22f0d4 --stat`) before accepting the implementer report (`reports/w0-investigation/W0-IMPL-01-N0-1.md`). Then claims audit, then adversarial probes run from scratch files in `$env:TEMP\opencode` (outside the repo; nothing in the repo was written except this report) against the test module's own functions, against synthetic trees, and against the real tree by read-only resolution queries. Finally, scoped runs: `pytest tests/conventions/test_markdown_links.py`, `... test_docs_references.py`, the whole `tests/conventions`, `ruff check src tests`, `mypy src`. No full-suite run, no docker, no edits on any source/test file (per brief WRITE-SET).

Evidence tags follow STANDING-ORDERS §5: `[VERIFIED path:line]`, `[RAN cmd -> result]`, `[ASSUMED]`, `[UNKNOWN]`.

## 1. Blind-pass findings

### 1.1 What the diff actually is

Three mechanical changes plus one new test file. `git mv VISION.md docs/VISION.md` with 0 content diff [RAN `git show d22f0d4 --stat` -> "VISION.md => docs/VISION.md ... 0 changes"]. Two one-line text edits: `docs/README.md:127` drops the now-wrong `../VISION.md (lives at the repository root)` to `VISION.md`, and `docs/ROADMAP.md:33` `#status` -> `#testing`. That the move alone fixes the nine links from the two directions (README:168/236/337 read from root pre-move expecting `docs/VISION.md`; ROADMAP:3/6/65/92/124 + VISION:11/12/76/223 read as if VISION were already under `docs/`) matches the plan's §E N0.1 table [VERIFIED docs/plans/2026-11-09-next-spec-revised.md:50]. The scope of the change is minimal and the move-vs-edit-links choice is right: editing call sites to point at a root VISION would contradict D-9 ("README/ROADMAP already assume docs/") [VERIFIED docs/plans/2026-11-09-next-spec-revised.md:128].

### 1.2 The lock's architecture (blind reading)

The new module enforces existence + anchor validity for inline `[text](target)` links. It mirrors the `path:line` lock's scope constants verbatim (directories `("docs",)`, files `("README.md","AGENTS.md")`, exclusions for `docs/agent-ops` and `docs/plans`, and the `IGNORED_DIRECTORIES` set) [VERIFIED tests/conventions/test_markdown_links.py:62-97], and a test pins the constants against the sibling lock by loading it through `importlib` [VERIFIED tests/conventions/test_markdown_links.py:609-631]. Links are extracted with fence and inline-code-span skipping [VERIFIED tests/conventions/test_markdown_links.py:258-289]; destinations parsed for `<...>` spaces, balancing parens, titles [VERIFIED tests/conventions/test_markdown_links.py:212-255]; anchors are GitHub-style slugs with `-1`/`-2` dedup [VERIFIED tests/conventions/test_markdown_links.py:292-348]; path resolution is doc-relative then repo-root-relative [VERIFIED tests/conventions/test_markdown_links.py:379-400]; the one allowlist is exactly 15 bare ADR file names scoped to `docs/adr/README.md` [VERIFIED tests/conventions/test_markdown_links.py:99-126,403-416]. The shape of the work is sound — this is a faithful second lock, not a marketing test.

### 1.3 What is good (blind)

- **Non-vacuity guards are real.** Corpus >200 links asserted, README/AGENTS scanned, exclusions asserted, and the presence of `docs/VISION.md` links asserted [VERIFIED tests/conventions/test_markdown_links.py:530-544]. A stub `find_links -> []` fails with `the lock found only 0 links` — good mutation coverage.
- **The synthetic self-test proves detection, not just green.** Five links, three defects, healthy + allowlisted asserted absent, exact defect count 3 [VERIFIED tests/conventions/test_markdown_links.py:547-585]. Deleting `check_link`'s missing-file branch makes it fail; deleting the allowlist makes `0001-sqlite-over-postgres.md` appear — so the allowlist path is exercised for the right reason.
- **Allowlist freshness.** `test_allowlist_only_covers_genuinely_missing_adr_bodies` asserts 15 entries, each missing on disk and linked, and set-equality `missing == ALLOWLISTED` [VERIFIED tests/conventions/test_markdown_links.py:588-606] — no stale/extra entries can hide.
- **Security surface is nil by construction.** No network, no subprocess; reads with `errors="replace"` in one place; `importlib` load of the sibling executes only constants/defs [VERIFIED tests/conventions/test_markdown_links.py:49-56,609-631]. Zero `noqa` / `type: ignore` in the new file [RAN grep -> No matches].
- **Scope pin works.** The drift test exec's the sibling under a unique module name and unregisters it in `finally` — the fix for the observed `AttributeError: 'NoneType' object has no attribute '__dict__'` is correct and needed.
- **Slug engine matches GitHub for corpus-relevant shapes.** Colon-drop, double-space->double-hyphen, code-span and emphasis stripping, link-text extraction, HTML-tag removal, dedup suffixes were all traced and are consistent with the github-slugger rule tested at [VERIFIED tests/conventions/test_markdown_links.py:634-646]. Setext handling [VERIFIED tests/conventions/test_markdown_links.py:329-348] correctly treats a paragraph-following `---` as H2 (matching CommonMark render) and skips list/quote/table lines that would falsely become headings.

### 1.4 Blind-pass defects (detail in §4)

Three MAJOR false-negatives, all with the same structural root cause: the two "real" granular tests re-implement the guards inline instead of calling the single `check_link` that the synthetic test exercises, so they silently diverge from it. (1) Malformed destinations sentinel `<unparseable-link>` is *skipped* by both real tests. (2) The existence test's allowlist skip lacks `check_link`'s `and not fragment`, so an allowlisted name with a fragment is skipped though `check_link` says it is not allowlisted. (3) `resolve` follows `../` out of the repository and accepts the link if the outside file happens to exist — non-deterministic between a dev machine and CI, and silently accepts cross-repo references. Two MINOR false-negatives (nested/double-bracket links missed by the candidate regex; repo-root fallback that passes `src/thing.py` from a `docs/` doc though GitHub would 404) plus two MINOR false-positive risks (dedup collision `creation-1`; trailing-`#`/empty edge) and four NITs.

## 2. Claims audit (implementer report W0-IMPL-01-N0-1.md, 11 claims)

| # | Claim | Verdict + evidence |
|---|---|---|
| 1 | 8 tests added; 7/8 green, 1 red solely on untracked `docs/2026-11-09-next-spec.md:4` anchor | **[RAN `pytest tests/conventions/test_markdown_links.py tests/conventions/test_docs_references.py -v` -> 13 passed]** (8+5). Current state fully green; the historical PARTIAL is explained by the now-fixed spec anchor (COMMS 2026-11-09T07:30:00Z, independently confirmed below). |
| 2 | Move fixes 9/12 broken file links; pre-move 24 broken -> 15 allowlisted -> post-fix 0 | **Supported as consistent, not re-run.** Current corpus independently reproduced: 349 links, 0 violations [RAN probe2 `all_links`/`_violations`]. Pre-move count requires checkout mutation; FORBIDDEN (no stash/reset), so not re-verified. The direction/magnitude agree with the verify-below facts. |
| 3 | `git mv` preserved history (0 diff) | [VERIFIED `git show d22f0d4 --stat` -> rename 100%, 4 files/2 deletions]. |
| 4 | `docs/README.md:127` + `ROADMAP:33` are the only required edits; reverting either turns the lock red | **[RAN probe2 resolution probes]**: `resolve(docs/README.md, "../VISION.md") -> None`, `resolve(..., "VISION.md") -> docs/VISION.md`; `"status" in heading_anchors(README) -> False`, `"testing" -> True`. That is exactly the reversal proof without editing files. **Verified.** |
| 5 | `git branch` verified `ops/20261109-next-spec` before commit | [RAN `git branch --show-current` -> `ops/20261109-next-spec`]. Confirmed current. |
| 6 | ruff 0; mypy 3 baseline (Windows) | **[RAN `ruff check src tests` -> All checks passed!; `mypy src` -> exactly `subprocess_backend.py:548` x3 attr-defined]** — verbatim baseline. |
| 7 | Full conventions dir timed out at 120s budget | **[RAN `pytest tests/conventions -v` -> 34 passed in 214.48s]** (26 baseline + 8 new). Timeout was budget, not failure. |
| 8 | `IGNORED_DIRECTORIES` pinned/mirrored but not consumed by resolution | [VERIFIED grep of `test_markdown_links.py`: 87-97 define; no consumer except drift-test comparison]. **True; and harmless** — `scope_files` never walks those dirs anyway. |
| 9 | "an allowlisted name with a fragment attached is NOT allowlisted" | **Partially FALSE in the real suite.** True in `check_link` [VERIFIED test_markdown_links.py:439] and `_violations`, **[RAN probe1: both fragmented allowlist links -> violations]**. But the real `test_every_relative_link_names_an_existing_path` [VERIFIED lines 503-505] skips on `_is_allowlisted(document, path)` *without* the `not fragment` guard, and the anchor test skips missing targets — so a fragmented allowlisted name passes both real tests. MAJOR-2. |
| 10 | Malformed forms yield `<unparseable-link>`, "no crashes" | Detection claim **true** [RAN probes]; the implication that the lock *catches* them is **false** for the real suite (both real tests skip the sentinel). MAJOR-1. |
| 11 | No network/subprocess; slug double-hyphen pinned to GitHub `tr(' ','-')`-per-space | [VERIFIED read of lines 49-56 + 292-304]; slug rule matches github-slugger semantics (each space -> hyphen, punctuation dropped). No corpus link exercises the double-hyphen shape — caveat acknowledged by implementer; **credible**; [ASSUMED] GitHub parity beyond the tested shapes is not independently oracle-verified (no oracle in-repo). |
| 12 (bonus) | ORCH fixed spec anchor; "gate-w0-1 now 34 passed" | **[RAN probe2]**: line 4 now `[D-1](#5-decisions-made-in-this-spec)`, slug present, old slug absent. **[RAN `pytest tests/conventions -v` -> 34 passed]** — matches gate claim exactly. **Verified.** |

Summary: 11 checked, 1 partially false (claim 9; consequence MAJOR-2), the rest supported or verified.

## 3. Long-run test (independent answers)

- **Source.** The fix *is* at the source for the nine moved links (path truth, not call-site patching) and two anchor/text edits, each proven necessary by resolution probes. The lock enforces the invariant for future moves **except** for the three holes in §4 which are false-negatives at the source of the enforcement (the two granular tests) — that is where future rot will slip through.
- **Load (100x, concurrency, partial failure).** Real corpus is 349 links [RAN]; two full scans (existence + anchor) plus anchor cache per file complete the lock file in seconds; the whole conventions dir (34 tests, mostly a 21-test subprocess import sweep) took 214s [RAN]. Parsing is linear per line, destinations bounded by line length, no index rebuild per link; no unbounded state (sorted file list, per-file anchor sets). No threads/queues introduced — partial-failure surface does not exist beyond "test crashes" (§5: no crash found).
- **Hostile input.** Malformed/sentinel inputs are invisible to the real suite (MAJOR-1); `../` escapes are accepted when the outside target exists (MAJOR-3); reference-style links are ignored by design and the docstring says so [VERIFIED lines 44-46], with a corpus grep supporting "none in scope today" [RAN grep `^\s{0,3}\[[^\]]+\]:\s*\S+` over `docs/` -> 1 hit, in excluded `docs/agent-ops/`, and README -> no matches]. `%2e%2e` traversal is correctly rejected [RAN probe2]. Oversized inputs: files read whole into memory — fine for docs, not a threat.
- **Operability.** Failure messages name `document:line`, destination, and defect class (`no such file or directory`, `no heading ... has the anchor`, `the target is the directory`, `is not a markdown file`) [VERIFIED lines 435-463] — actionable without opening the test. No migration/rollback semantics (test-only). A CI failure will be clear which link to fix.
- **Reversal.** If the move is reverted: `resolve(docs/README.md, "VISION.md")` fails and `../VISION.md` resolves — existence test goes red [proof pattern RAN probe2]. If ROADMAP reverts to `#status`: anchor absent from README — anchor test goes red [RAN probe2]. If either granular test is gutted (e.g. `find_links -> []`): non-vacuity (`>200`) goes red [VERIFIED lines 530-544]. If `check_link`'s missing-file branch is deleted: synthetic test goes red [VERIFIED lines 578-585]. **The reversal test for MAJOR-1/2 currently does NOT exist** — a malformed link or fragmented allowlist link fails nothing (probe §5) — which is precisely the gap to close.

## 4. Findings

### MAJOR-1 — Malformed links are invisible to the real suite (false-negative)

**Where:** [VERIFIED `test_markdown_links.py:284-286`] `find_links` emits `"<unparseable-link>"` when `_parse_destination` returns `None`; [VERIFIED `test_markdown_links.py:434-438`] `check_link` reports it. But [VERIFIED `test_markdown_links.py:500-501`] the existence test skips it (`continue`), and [VERIFIED `test_markdown_links.py:516-518`] the anchor test skips it (no fragment). **[RAN probe1]** synthetic `[x](<unterminated` and `[x](guide.md` both yield the sentinel; `_violations` flags them, while simulated existence = `EXIST-SKIP` and anchor = `ANCH-SKIP`. **Consequence:** `[x](<unterminated` or `[x](guide.md` missing `)` renders as literal brackets on GitHub yet passes CI. A deliberate broken link of this class — the exact class the acceptance criterion names — will not turn the lock red. *Suggested fix (implementer, not this review):* existence test reports `"<unparseable-link>"` as a defect instead of skipping (mirror `check_link` lines 434-438); ideally both granular tests delegate to `check_link` so sentinel/external/allowlist handling exists in exactly one place.

### MAJOR-2 — Allowlisted name + fragment diverges between `check_link` and the real suite (false-negative)

**Where:** [VERIFIED `test_markdown_links.py:439`] `check_link`: `allowlisted = _is_allowlisted(link.document, path) and not fragment`. [VERIFIED `test_markdown_links.py:503-505`] existence test: `if _is_allowlisted(link.document, path): continue` (no fragment check). **[RAN probe1]** `0001-...#foo` and `0002-...#foo` are flagged by `_violations` but RAN existence simulation skips both (and the anchor test skips them as unresolved). Corpus has no such link today, so current suite is green either way — but the documented contract ("the name alone is the sanctioned reference") is unenforced in the suite that matters. **Root cause is identical to MAJOR-1:** guard duplication.** Suggested fix: add `and not fragment` to the existence test's allowlist skip (one token), or delegate (see MAJOR-1).

### MAJOR-3 — `../` links escaping the repository are silently accepted if the outside target exists (false-negative + non-determinism)

**Where:** [VERIFIED `test_markdown_links.py:379-400`] `resolve` builds `document.parent / decoded` with no containment check, then `candidate.exists()` follows it anywhere. **[RAN probe1]** `docs/a.md` -> `[xf](../../outside.md#secret-heading)` (outside `repo/`, heading exists) produced **no** violation; `[x](../../outside.md)` likewise. `[RAN probe2]** same link with a *non-existent* heading produced a normal "no heading" defect (no crash — `relative_to` works lexically since `root` is a path prefix; the earlier crash hypothesis is withdrawn), but a *matching* heading passes silently. **Consequence:** a link addressing a sibling/parent file outside the repo (e.g. a dotfile, an uncommitted spec, `/etc/*`-ish paths on the dev box) passes locally and fails (or passes) differently in the clean CI checkout — the lock becomes machine-dependent, the worst kind of flake. In-scope corpus has none [RAN: 0 violations all inside root]. *Suggested fix:* `resolve` (or `check_link`) should require the resolved candidate to be inside `root` — e.g. `candidate.resolve().is_relative_to(root.resolve())` (handles `..` + symlinks) — and otherwise report a defect ("outside the repository"). This is the correct containment for a repo-link lock.

### MINOR-1 — Nested/double-bracket link text is missed entirely

**Where:** `_CANDIDATE = r"!?\[[^\]]*\]\("` [VERIFIED line 147]. **[RAN probe1]** `[a [b] c](x.md)` -> `[]`; `[[x]](y.md)` -> `[]`. Both are valid markdown links GitHub renders. False-negative class. Corpus-free today, but a false-negative in the same direction as MAJOR-1/3. *Fix:* bracket-depth scan for the `]` that balances the opening `[` (or document as a limitation — the module already documents two others).

### MINOR-2 — Repo-root fallback passes links GitHub would 404 (intentional leniency, worth a docstring note)

**Where:** [VERIFIED lines 395-396] `root / decoded` second candidate. **[RAN probe1]** `docs/a.md` -> `[x](src/thing.py)` (exists at root, absent under `docs/`) -> violations `[]`, though GitHub renders `docs/src/thing.py` (404). This matches the plan's "document + repo root" wording [VERIFIED revised.md:50] and protects the corpus's root-relative spellings, so it is *chosen*, not accidental. Keep it, but the docstring should say explicitly that a root-resolving link that would 404 when rendered is tolerated — future readers will assume strict rendering and misjudge the lock's strength.

### MINOR-3 — Anchor dedup collision: `Creation-1` literal heading vs duplicate `Creation`

**Where:** [VERIFIED lines 321-327] counts keyed by base slug only. **[RAN probe1]** `"# T, Creation, Creation, Creation-1"` -> `{t, creation, creation-1}` — no `creation-1-1`, which GitHub assigns to the third heading. Link `#creation-1-1` would false-positive (reported broken though GitHub serves it). Ultra-rare; the corpus has no such shape (real-corpus 0 violations). Full fidelity requires tracking the set of emitted anchors during dedup, not just per-base counts.

### MINOR-4 — Granular-test duplication is the systemic risk (the meta-finding behind MAJOR-1/2)

Both real tests re-encode `check_link`'s policy (external skip, allowlist skip, fragment skip) inline [VERIFIED 495-527]. The synthetic self-test drives `_violations`/`check_link` [VERIFIED 574-585], so it proves the *helper* but not the *suite*. Any future guard change in `check_link` must be hand-mirrored — already proven to be an active failure mode (this review found the two divergences by reading, not by a test going red). **Recommended change:** one shared `is_checked(link)` / unified violation computation consumed by all three (existence, anchor, synthetic) — or drive the synthetic test through the same two functions the suite runs. This is a design finding, not a complaint about coverage count.

### NIT-1 — `all_links` reads with strict UTF-8 where `_anchors_for` uses `errors="replace"`

[VERIFIED line 475] `read_text(encoding="utf-8")` vs [VERIFIED line 374] `errors="replace"`. One undecodable byte in a docs file makes the lock ERROR (crash) rather than cleanly FAIL. Corpus is clean today. Suggest `errors="replace"` in `all_links` too for consistent, debuggable failures.

### NIT-2 — `IGNORED_DIRECTORIES` is dead weight in this module

Mirrored for parity and pinned by the drift test, but nothing in resolution consults it. The implementer already self-flagged this honestly. Harmless. Consider a one-line comment that it is scope-parity-only so the next reader does not "fix" it by wiring it into `resolve`.

### NIT-3 — Sentinel is a magic string in three places

`"<unparseable-link>"` appears in `find_links` [286], `check_link` [434], and the existence test [500] as literals. A typo in any one site would silently disable the handling. A module constant (`_UNPARSEABLE = "<unparseable-link>"`) would remove the coupling.

### NIT-4 — Scope glob is case-sensitive (`*.md`)

`base.rglob("*.md")` [VERIFIED line 200] misses `FOO.MD` on Linux though GitHub renders it. No such file in corpus today. One-glob note; fix is `*.[mM][dD]` or casefold compare.

### No BLOCKER

Nothing crashes, misleads green, or breaks existing behavior. `test_docs_references` is untouched and green inside the 34-pass run [RAN]. The VISION move itself is correct and complete. All three MAJOR findings are false-negatives in narrow classes; the lock still catches the dominant broken-link classes (missing file, wrong anchor, dir/non-md fragment).

## 5. Attempts to break it

All probes ran against the test module on synthetic trees (no repo edits). "Expected" is the lock's documented/promise behavior; "actual" is observed.

| # | Adversarial input | Expected | Actual [RAN] | Result |
|---|---|---|---|---|
| 1 | `[x](<unterminated` (unterminated angle) | defect | `find_links` -> sentinel; `_violations` flags; **real existence+anchor skip** | ❌ MAJOR-1 |
| 2 | `[x](guide.md` (missing `)`) | defect | sentinel; same skip | ❌ MAJOR-1 |
| 3 | `[x]()` empty destination | self-link, pass | `-> ''` passes (resolves to self) | ✅ defensible |
| 4 | `[a [b] c](x.md)`, `[[x]](y.md)` | link found | `[]` (silently missed) | ❌ MINOR-1 |
| 5 | `[x][ref]` + `[ref]: guide.md` | ignored (documented) | `[ ]` ignored, no crash | ✅ documented |
| 6 | `[0001](0001-sqlite-over-postgres.md#foo)` in ADR index | defect (not allowlisted) | `_violations` flags; **real existence skips** | ❌ MAJOR-2 |
| 7 | `[0001](docs/adr/0001-sqlite-over-postgres.md)` path form | not allowlisted | flagged ✓ (strict, consistent w/ bare-name convention) | ✅ |
| 8 | `[x](%2e%2e/secret.md)` traversal | defect | flagged ✓ | ✅ |
| 9 | `[x](../../outside.md#secret-heading)` (outside repo, heading exists) | defect | **passes silently** | ❌ MAJOR-3 |
| 10 | `[x](../../does-not-exist.md)` | defect | flagged ✓ | ✅ |
| 11 | `[x](docs#frag)` dir+fragment | defect | flagged ("directory ... no headings") ✓ | ✅ |
| 12 | `[x](file.txt#frag)` non-md+fragment | defect | flagged ("not a markdown file") ✓ | ✅ |
| 13 | `[x](https://...)`, `mailto:`, `//cdn` | skipped (external) | skipped ✓ | ✅ |
| 14 | `` `[not a link](missing.md)` `` code span | skipped | skipped ✓ | ✅ |
| 15 | `` [`code`](guide.md) `` text-in-span | kept | kept ✓ | ✅ |
| 16 | Fenced `[x](fenced.md)` | skipped | skipped ✓ (only `real.md` after fence found) | ✅ |
| 17 | `[x](a(b).md)` balanced parens | `a(b).md` | parsed ✓ | ✅ |
| 18 | `[x](a.md "title with ) paren")` | `a.md` | parsed ✓ | ✅ |
| 19 | `[x](src/thing.py)` from `docs/` (root-relative) | 404 on GitHub; lock passes by design | passes (lenient) | ⚠️ MINOR-2 by design |
| 20 | Duplicate `Creation` / `Creation-1` collision | `creation`, `creation-1`, `creation-1-1` | `{creation, creation-1}` only | ❌ MINOR-3 (false-positive) |
| 21 | Heading `## Closed ##` vs `## Hash #inside` | `closed`, `hash-inside` | correct both | ✅ |
| 22 | Emojis/unicode slugs (`🎉 Party`, `Café`) | no crash, GitHub-ish | `-party-time`, unicode preserved | ✅ |
| 23 | Empty fragment `[x](#)`-with-empty | pass (self) | passes (fragment empty -> no check) | ✅ defensible |

No crash, hang, exception, or unbounded behavior was produced by any input, including the escape-with-fragment probe (initial crash hypothesis refuted by probe2 — error path uses lexical `relative_to`, which succeeds for `../` spellings).

## 6. Dimension checklist

- **Correctness.** Move + two edits correct and minimal (§1.1). Anchor fix `#testing` correct (README has `Testing` at 325; no `status` heading) [RAN probe2]. Link machinery correct for the dominant classes; three MAJOR false-negatives + two MINOR false-negative classes + one false-positive class remain (§4).
- **Security.** No network/subprocess/writes in the test [VERIFIED read]. No secrets. `importlib` load executes sibling constants only. MAJOR-3 is a lock-integrity issue (acceptance of out-of-repo targets + machine-dependence), not a code-execution risk. No injection surface (docs are trusted input to a checker).
- **Error handling.** Malformed markdown -> sentinel, never exception [RAN all probes]. File decode: strict in `all_links` (NIT-1). Missing-root tolerance (`if not base.is_dir(): continue`) matches sibling lock. Test-failure messages are actionable.
- **Edge cases.** Covered: setext, fences, spans, titles, angles, balanced parens, dedup, dirs, non-md, externals, percent-encoding, traversal (correctly rejected), allowlist scoping (three bypass forms all correctly rejected by `check_link`). Not covered: MAJOR-1/2/3, MINOR-1/3 classes.
- **Conventions (zero-baseline).** [RAN `ruff check src tests` -> All checks passed!; `mypy src` -> exactly 3 baseline]. New file adds zero `noqa`/`type: ignore` [RAN grep]. Line-length ≤100 (ruff). Docstring voice matches sibling lock. No new dependency.
- **Maintainability.** Structure mirrors `test_docs_references.py`; drift test prevents scope divergence. Deduct for guard duplication (MINOR-4) and magic-string coupling (NIT-3): the file's own future is the risk.
- **Test rigor.** Synthetic test + freshness test + non-vacuity + 2 unit tests are each non-vacuous (§1.3). The gap is suite-vs-helper coverage (MINOR-4), not effort.
- **Performance.** ~7s for both locks [RAN 13 passed]; full conventions 214s dominated by sibling import sweep. Anchors cached. No regression risk.
- **Operability / rollback.** Test-only; rollback = `git revert d22f0d4` (move back + delete new test + revert two edits), no data migration. Failure output names the fix location.

## 7. Optimization and simplification opportunities

- **DO-NOW (implementer, small):** MAJOR-1 + MAJOR-2 one-token fixes (report sentinel in existence test; add `and not fragment` to its allowlist skip). Both are inside the new file and remove false-negatives immediately.
- **DO-LATER (implementer):** unify granular tests on `check_link` (removes MINOR-4 class permanently); add root-containment to `resolve` (MAJOR-3). Both are small but touch shared helpers — prefer a follow-up task with the two golden probes from §5 as regression tests.
- **DO-LATER:** MINOR-1 bracket-depth candidate scan; make it data-driven with property fixtures.
- **NOT-WORTH-IT:** `IGNORED_DIRECTORIES` wiring into resolution (NIT-2) — dead weight but cost-free; an `*.MD` glob change (NIT-4) without a corpus need. Neither justifies churn.
- **NOT-WORTH-IT:** optimizing the double full-corpus scan (existence + anchor each scan ~80 files). 7s total is noise next to the sibling sweep; micro-optimizing now would add code without measurable benefit.

## 8. Cross-agent interactions

- **W0-IMPL-02 (research rename, be51bba):** prerequisite file `docs/research/2026-11-deepseek-super-agent.md` is present on disk [observed via `git log`]; the link lock's corpus includes it (part of the 349) and is green with it — no interaction issue.
- **W0-IMPL-05 (N0.5 triage):** disjoint files; no overlap with this diff (different report paths). The 34-pass conventions result is independent of the N0.5 triage.
- **W0-REV-02:** reviews T-N0-2 + T-N0-5 separately; cannot see their work. No shared-file edits from this diff beyond the locked scope above.
- **ORCH spec-anchor fix (COMMS 2026-11-09T07:30:00Z):** verified independently (§2 #12) — this resolves the one red the implementer reported, and my probe confirms the current tree is green because of it. The fix was applied to the untracked-but-in-scope file `docs/2026-11-09-next-spec.md`, which remains untracked; in a clean CI checkout it is absent (not part of `d22f0d4`), so CI is green regardless. No action needed.
- **No edits outside the WRITE-SET.** Scratch probes lived in the system temp dir; the repo received only this report.

## 9. What I could not verify

- **Historical 24-broken count & the pre-move scan** (implementer §11) — would require a checkout/stash, forbidden and unnecessary; current-tree evidence is sufficient and consistent. Marked [ASSUMED]-consistent in §2.
- **GitHub renderer as an oracle for slug edge shapes** (double-hyphen, emoji leading, dedup collision) — no renderer in-repo; judgments rest on the documented github-slugger rule and code tracing. The corpus passes end-to-end (349/0 [RAN]), so this uncertainty is bounded to exotic shapes with no corpus exercise.
- **Full `pytest tests/ -q`** — out of scoped brief (only `tests/conventions` run [RAN: 34 passed]). Full-suite green is therefore NOT claimed by this review.
- **Linux CI shape** (case-sensitive filesystem, `/usr/bin/python` sandbox PATH) — this host is Windows; behavior on Linux for CRLF/`*.MD`/`resolve` case handling is reasoned from code, not run. [UNKNOWN] run on Linux.
- **`mypy` on the test file itself** — repo gate runs `mypy src` only; the test file is covered by ruff (0 findings [RAN]) but not mypy by design.

## Verdict

**APPROVE-WITH-CHANGES.** T-N0-1's VISION move and the migration of the two affected lines are correct, the allowlist is exactly right and freshness-pinned, scope mirrors the sibling lock, and no established check regresses. The link lock itself is well-built but has three false-negative holes (§4 MAJOR-1..3) — all narrow, all with one-line or small fixes — whose common cause is guard duplication between the granular tests and `check_link` (MINOR-4). None of them affects the move's correctness or the current corpus (349 links, 0 violations, independently reproduced); any one of them lets a future broken link through CI silently, which is the movement this task exists to stop. The MAJOR-1 and MAJOR-2 fixes should land in T-N0-1 (or its exact follow-up) before the wave gate; MAJOR-3 should be filed as a follow-up with the probe in §5 as its regression test rather than bolted on here.
