<!-- ORCH-DIGEST:START -->
agent: W0-REV-03 | scope: wave 0 / delta 2f26fb8 (MAJOR-1/2 fix) | verdict: APPROVE
findings: BLOCKER 0 | MAJOR 0 | MINOR 0 | NIT 2
claims_audit: 11 checked / 0 unsupported
band_aids_found: none
top_3_risks: race-only assert in check_link (unreachable); still-open pre-existing holes MINOR-1/3 + MAJOR-3 by design; sentinel-partition depends on split_destination no-# invariant
optimizations: DO-NOW 0 | DO-LATER 1 | NOT-WORTH-IT 1
reversal_check: done (old-logic replay 1 vs 4 defects + full re-runs)
confidence: high (probes #1/#2/#6 via real granular path, corpus 349/0/0 reproduced, ruff/mypy/conventions re-run)
<!-- ORCH-DIGEST:END -->
# Code review — W0-REV-03 (delta 2f26fb8, MAJOR-1/MAJOR-2 fix)

## 0. Method

Blind pass first: read the full diff (`git show 2f26fb8 -- tests/conventions/test_markdown_links.py`) and the current file around the changed functions before re-reading the implementer report. Then claims audit against `reports/w0-review/W0-IMPR-01-improve.md`, then independent adversarial probes run from scratch files in `C:\Users\devan\AppData\Local\Temp\opencode` (outside the repo; nothing in the repo was edited except this report) against the post-fix module, a synthetic tree mirroring the extended self-test, and the real corpus by read-only resolution queries. Finally, scoped runs only: `pytest tests/conventions/test_markdown_links.py -q`, `pytest tests/conventions -q`, `pytest tests/conventions --collect-only -q`, `ruff check src tests`, `mypy src`. No full-suite run, no docker, no edits on any source/test file (per brief WRITE-SET read-only).

Evidence tags follow STANDING-ORDERS §5: `[VERIFIED path:line]`, `[RAN cmd -> result]`, `[ASSUMED]`, `[UNKNOWN]`.

## 1. Blind-pass findings

### 1.1 What the diff actually is

A single-file, test-only change: `tests/conventions/test_markdown_links.py` [RAN `git show --stat 2f26fb8` -> 1 file, 125 insertions, 53 deletions]. The deletions are dominated by the `check_link` dedent. Five mechanical moves, all inside that file:

1. New module constant `_UNPARSEABLE = "<unparseable-link>"` [VERIFIED tests/conventions/test_markdown_links.py:164] replacing the literal at the emitter (`find_links` line 292).
2. New `_existence_defect(link, document_path, root)` [VERIFIED lines 425-452] holding the existence policy in exactly one place: external -> `None`; sentinel -> defect; `resolve(...) is None` -> `None` only when `_is_allowlisted` **and** `not fragment`, else missing-target defect; else `None`. Order and every defect string are verbatim from the old `check_link`.
3. `check_link` [VERIFIED lines 455-500] now delegates existence to `_existence_defect` (line 472), then `if not fragment: return None` (475), then resolves again (477) behind `assert target is not None` (481) and runs the fragment checks (dir / non-`.md` / missing heading) unchanged apart from dedent.
4. New `_existence_defects(root)` [VERIFIED lines 531-543] = `all_links(root)` + `_existence_defect`; the granular existence test [VERIFIED lines 546-554] is now two lines (`defects = _existence_defects()` + assert). The anchor test `test_every_anchor_names_a_heading` [VERIFIED lines 557-572] is untouched — the diff contains no hunk for it [RAN `git diff 1ccf3f0..2f26fb8 | Select-String "def test_every_anchor"` -> no output].
5. `test_the_lock_catches_a_broken_link` [VERIFIED lines 592-664] extended: synthetic tree +2 malformed lines (unterminated angle, missing paren) [VERIFIED lines 619-620] and +1 fragmented allowlist link [VERIFIED line 629]; link count 5 -> 8, `_violations` defects 3 -> 6 [VERIFIED lines 633,648]; plus `_existence_defects` assertions pinning sentinel count == 2, fragmented present, bare allowlist absent-from-defects, existence defects == 4 and anchor-free [VERIFIED lines 651-657].

### 1.2 Equivalence of `check_link` (blind reading)

Traced every `None` case of `_existence_defect` against the new `check_link` control flow:

- **External.** `check_link` early-returns `None` at line 468 before calling the helper; the helper's own external branch (441) is the live one when `_existence_defects` calls it directly. Duplicated but harmless (see NIT-1). Same as old order (external before sentinel).
- **Sentinel.** Helper returns the verbatim "parenthesis never closes" defect [VERIFIED lines 443-447]; `check_link` returns it at 473-474. Old order (sentinel before allowlist/resolve) preserved.
- **Missing + bare allowlisted.** Helper returns `None`; `check_link` returns `None` at the `if not fragment` guard (475), because a bare allowlisted name has no fragment [VERIFIED `_is_allowlisted` + `split_destination` lines 358-362, 409-422: `partition("#")` gives `fragment == ""`]. Assert at 481 unreachable on this path.
- **Missing + (not allowlisted or fragmented).** Helper returns the missing-target defect; `check_link` returns it. Old logic (`allowlisted and not fragment` at old line 439) identical.
- **Exists.** Helper returns `None`; `check_link` returns `None` when no fragment, else resolves (477) and runs fragment checks. The only way `assert target is not None` fires is a TOCTOU race (target vanishes between the helper's `resolve` and line 477) — see NIT-1. Non-race behavior is identical to old code, including messages.

All four defect strings (sentinel, "no such file or directory", directory, non-markdown, missing heading) are byte-identical between old and new [VERIFIED diff: no message hunks changed]. `resolve`, `_is_allowlisted`, `split_destination`, `is_external` are untouched by the diff.

### 1.3 What is good (blind)

- **The meta-fix is real.** The existence policy now has one implementation consumed by all three paths (`check_link` for the synthetic + anchor tests, `_existence_defects` for the granular test). W0-REV-01's MINOR-4 ("guard duplication is the systemic risk") is closed by construction, not patched around: a future guard change in `_existence_defect` moves both paths together. [VERIFIED lines 425-452, 472, 538-542]
- **Partitioning is preserved (no double-report).** Sentinel destinations have no `#` (`split_destination` on `"<unparseable-link>"` gives `fragment == ""`), so the anchor test skips them (`if not fragment: continue` line 563) and they report exactly once via existence. Fragmented-allowlist links resolve to `None`, so the anchor test skips them as unresolved (line 567-568) and they report exactly once via existence. Proved by probe §5, not just by reading.
- **The synthetic test pins the right thing.** It asserts both paths: `_violations` (helper) **and** `_existence_defects` (the exact function the real test runs). `count("parenthesis never closes") == 2` on both, `len(defects) == 6` vs `len(existence) == 4` (the difference is exactly the 2 anchor-only defects), `"no heading" not in existence_joined`. Any regression that re-skips sentinel/fragmented in the granular path fails this test — the reversal test W0-REV-01 §3 said did not exist now does.
- **Bare-allowlist over-correction is guarded.** `joined.count("0001-sqlite-over-postgres.md") == 1` proves the bare name (no defect) is not reported while the fragmented one is. If the allowlist were dropped entirely, the bare link would also appear and the count would be 2 -> fail. Minimal, precise.
- **No magic-string coupling.** `_UNPARSEABLE` appears 3 times total (definition + 2 uses) [RAN python count -> 3]; `noqa: 0`, `type: ignore: 0` [RAN python count]. The old three literal sites are gone.

### 1.4 Blind-pass concerns (detail in §4)

Two NITs, neither blocking: (1) `check_link` resolves twice per fragmented link and asserts the second resolve — correct but wasteful and with a non-defect failure mode on an (unreachable-in-practice) race; the same duplicates the external check. (2) The sentinel/partition argument depends on the sentinel containing no `#` — true today and enforced by the constant, but implicit. No BLOCKER/MAJOR/MINOR. In particular, no new false-positive or false-negative class was introduced by the unification: the probes in §5 cover healthy, external, empty, dir, non-md, and every defect class through both paths.

## 2. Claims audit (implementer report W0-IMPR-01-improve.md, 11 claims)

| # | Claim | Verdict + evidence |
|---|---|---|
| 1 | 1 file changed, 125+/53-, commit `2f26fb8` on top of `1ccf3f0` | [RAN `git show --stat 2f26fb8` -> 1 file, 125 insertions, 53 deletions; `git log --oneline -6` -> `2f26fb8` on top of `1ccf3f0`]. **Verified.** |
| 2 | `_existence_defect` order/messages verbatim from old `check_link` | [VERIFIED lines 440-452 vs diff old lines 434-463]. External -> sentinel -> resolve -> (`_is_allowlisted` and `not fragment`) -> missing. **Verified.** |
| 3 | `check_link` delegates; fragment checks unchanged apart from dedent + assert | [VERIFIED lines 467-500; diff shows only dedent + assert hunk]. All defect strings identical. **Verified.** |
| 4 | Anchor test untouched; partitioning intentional (sentinel/fragmented report once via existence) | [RAN diff grep `def test_every_anchor` -> no hunk]; [RAN probe §5 anchor-partition trace: sentinel SKIP (no frag), fragmented SKIP (unresolved)]. **Verified.** |
| 5 | Sentinel constant replaces literal; NIT-1/NIT-4 untouched | [RAN count `_UNPARSEABLE` -> 3, `noqa` 0, `type: ignore` 0]; [RAN diff grep `read_text\|IGNORED\|rglob` -> no hunk]. **Verified.** |
| 6 | Synthetic test 5->8 links, 3->6 defects, + `_existence_defects` assertions (sentinel 2, fragmented present, bare absent, existence 4 anchor-free) | [VERIFIED lines 619-657]; [RAN probe -> 8 links, violations 6, existence 4, matching the test's own numbers]. **Verified.** |
| 7 | Probes #1/#2/#6 fail the real granular path (not just `_violations`) | [RAN probe `w0-rev03-probe.py` -> `_existence_defects` reports 2 sentinel + fragmented 0001; `_violations` agrees 6/4]. **Verified.** |
| 8 | Corpus impact zero (0 defects through both paths) | [RAN probe real corpus -> 349 links, existence 0, violations 0]. **Verified.** |
| 9 | Scoped runs: 8/8 markdown-links, 5/5 docs-references, 34/34 conventions; ruff 0; mypy exactly 3 | [RAN `pytest tests/conventions/test_markdown_links.py -q` -> 8 dots]; [RAN `pytest tests/conventions -q` -> 34 dots]; [RAN `--collect-only` -> 21+8+5 = 34]; [RAN `ruff check src tests` -> All checks passed]; [RAN `mypy src` -> exactly `subprocess_backend.py:548` x3]. **Verified (all re-run).** |
| 10 | Reversal: new assertions fail on old behavior | [RAN `w0-rev03-reversal.py`: old-logic replay on identical synthetic tree -> 1 defect vs new 4; `len==4` and sentinel-count assertions fail on old]. **Verified independently.** |
| 11 | MAJOR-3 deferred (resolve untouched), no docs links edited, no scope creep | [RAN `git show --stat` -> 1 file]; [RAN diff grep `is_relative_to\|outside` -> only resolve lines, no containment added]; [RAN probe -> `../../o.md#secret-heading` still returns `None` (passes) — deferred, not dropped, not fixed]; implementer §13.1/§14 records it as DEFERRED-DEPENDENCY. **Verified.** |

Summary: 11 checked, 0 unsupported. The report's evidence discipline (pre-fix repro showing 0-vs-3 divergence, dual-path probe, verbatim scoped runs) holds up under independent re-run.

## 3. Long-run test (independent answers)

- **Source.** The fix is at the source of the divergence: existence policy has exactly one implementation (`_existence_defect`) consumed by `check_link` (synthetic + anchor paths) and the granular existence test (`_existence_defects`). W0-REV-01's root-cause statement ("guard duplication" MINOR-4) is addressed where it broke, not at one call site. MAJOR-1 (sentinel skip) and MAJOR-2 (fragment-blind allowlist skip) were the two divergences; both now live in the single source.
- **Load (100x, concurrency, partial failure).** Delegation adds one extra `Path.exists()` stat per link-with-fragment in `check_link` (existence resolve, then fragment-section resolve) — ~350 links, negligible; measured markdown-links file passes in the same seconds as before, full conventions dominated by the pre-existing import sweep [RAN 34 dots, same order as review's 214s / implementer's 195.85s]. No new collections, loops, state, or threads; `_existence_defects` is a single linear pass over `all_links`. No partial-failure surface beyond "test crashes" (none observed). The only new failure branch (the `assert`) is race-only — see NIT-1.
- **Hostile input.** Malformed destinations (`<unterminated`, missing `)`) still produce the sentinel, now reported by both paths [RAN probe]. Oversized inputs bounded by line length (parser unchanged). `%2e%2e` traversal, balanced parens, titles, fences, spans — all untouched by this diff (parser/`resolve`/`_CANDIDATE` identical). `../` escapes: still accepted when the outside target exists (MAJOR-3) — intentionally deferred, not widened (probe confirms identical pre/post behavior on the outside link).
- **Operability.** Failure shape unchanged: `document:line` + destination + defect class, now including the sentinel and fragmented-allowlist classes from the granular test. Rollback = `git revert 2f26fb8` (single test file); no migration, no data, no contract change (helper signatures and defect strings unchanged).
- **Reversal.** Revert any break and the suite goes red for the right reason [RAN reversal probe]: restoring the sentinel `continue` (or deleting the sentinel branch) -> `count == 2` fails; restoring allowlist-without-fragment -> `#foo` assertions fail; stubbing `_existence_defects` to `[]` -> counts fail; reverting `2f26fb8` wholesale -> `len(existence) == 4` fails (old gives 1). The reversal test that §3 of W0-REV-01 said did not exist now does.

## 4. Findings

### NIT-1 — `check_link` resolves twice and asserts on a TOCTOU race (non-blocking)

**Where:** [VERIFIED tests/conventions/test_markdown_links.py:472,477-481] `check_link` calls `_existence_defect` (which `resolve`s) then `resolve`s again behind `assert target is not None`.

**Why not a MAJOR:** the assert is provably unreachable in non-race execution (case analysis §1.2); the failure mode on a race is a loud `AssertionError` naming the link, not a silent pass. Test-only code; filesystem is stable during the scan in CI. The implementer already self-assessed this (§16, §14) as deliberate loud-over-silent.

**Cost:** one redundant stat per fragmented link (~350 links — noise) and an exception instead of a defect string on an unkillable-in-practice race. A follow-up could return the resolved target from the helper (e.g. a small `(defect, target)` tuple) to eliminate both. Optional; does not justify holding the delta. No change required for approval.

**Duplicate external check (same area):** `check_link` line 468 checks `is_external` then `_existence_defect` line 441 checks it again. Harmless dead branch on the `check_link` path (kept for the direct `_existence_defects` call). Same NIT bucket.

### NIT-2 — Sentinel/partition relies on an implicit no-`#` invariant (non-blocking)

**Where:** sentinel constant [VERIFIED line 164] + `split_destination` [VERIFIED lines 358-362].

The claim "sentinel reports exactly once via existence" holds because `"<unparseable-link>"` partitions to `fragment == ""`. True today; a future edit that added a `#` to the sentinel would double-report (existence + anchor) without failing any test loudly. A one-line comment at the constant ("contains no `#`, so `split_destination` yields no fragment") would close the implicit coupling. Optional polish; behavior verified correct today [RAN probe anchor-partition trace].

### Explicit confirmations (not findings)

- **MAJOR-1 CLOSED.** `[x](<unterminated` and `[x](guide.md` now fail the existence test via `_existence_defects` [RAN probe: 2 sentinel defects, exact strings `the link parenthesis never closes`]. The acceptance probes #1/#2 are green.
- **MAJOR-2 CLOSED.** `0001-sqlite-over-postgres.md#foo` (and `0002-...` shape) now fail existence [RAN probe: `no such file or directory` defect; existence contains `#foo`, bare name `None`]. Acceptance probe #6 is green. Bare allowlisted name still passes [RAN probe `exist '0001...' -> None`].
- **MAJOR-3 DEFERRED CORRECTLY (not silently dropped, not silently fixed).** `resolve` [VERIFIED lines 385-406] untouched, no containment added; outside-repo link still passes silently [RAN probe `../../o.md#secret-heading -> None`]; deferral recorded as DEFERRED-DEPENDENCY in `W0-IMPR-01-improve.md §13.1, §14` and acknowledged here. Resolution: follow-up task with W0-REV-01 §5 probe #9 as regression test — out of this delta's scope per brief §6. **Do not fix here.**
- **No scope creep.** Code commit touches exactly 1 file [RAN `git show --stat`]; NIT-1 (strict UTF-8 `all_links` line 511), NIT-4 (`*.md` glob line 206), MINOR-1 (`_CANDIDATE` line 147), MINOR-3 (dedup lines 322-333) all untouched. Anchor test untouched. No docs links edited.
- **W0-REV-01 MINOR-4 (meta-finding) CLOSED** by construction (single source); MINOR-1/2/3 remain open as pre-existing, unrelated classes (see §8) — not introduced by this delta.

### No BLOCKER / MAJOR / MINOR

Nothing new crashes, misleads green, or breaks existing behavior. Healthy links, externals, empty destinations, dir targets, bare allowlist, non-md targets — all behavior-preserved via probes (§5).

## 5. Attempts to break it

All probes ran against the post-fix module on a synthetic tree (no repo edits), exercising `_violations`, `_existence_defects`, `check_link`, and the anchor-test partition logic.

| # | Adversarial input | Expected | Actual [RAN probe] | Result |
|---|---|---|---|---|
| 1 | `[x](<unterminated` | existence defect | sentinel defect in BOTH `_violations` and `_existence_defects` | ✅ MAJOR-1 closed |
| 2 | `[x](guide.md` (missing `)`) | existence defect | sentinel defect in both | ✅ MAJOR-1 closed |
| 3 | `[x]()` empty | pass (self-link) | `None` in `check_link` and `_existence_defect` | ✅ unchanged |
| 4 | `[a [b] c](x.md)`, `[[x]](y.md)` | (MINOR-1 gap, out of scope) | still missed (parser untouched) | ⚠️ pre-existing, by design |
| 5 | `[x](https://...)`, `mailto:`, `//cdn` | skipped (external) | `None` everywhere | ✅ unchanged |
| 6 | `[0001](0001-sqlite-over-postgres.md#foo)` in ADR index | existence defect | `no such file or directory` in both; anchor-test SKIPs (unresolved) | ✅ MAJOR-2 closed, no double-report |
| 7 | `[0001](0001-sqlite-over-postgres.md)` bare | pass (allowlisted) | `None` in both | ✅ unchanged (no over-correction) |
| 8 | `[x](%2e%2e/secret.md)` | defect | (covered by suite; `resolve` untouched) | ✅ unchanged |
| 9 | `[x](../../outside.md#secret-heading)` (outside repo, heading exists) | (MAJOR-3 gap, deferred) | `None` (passes silently) | ⚠️ deferred as recorded |
| 10 | `[x](../../does-not-exist.md)` | defect | (unchanged) | ✅ |
| 11 | `[x](docs#frag)` dir+fragment | anchor defect | `the target is the directory 'docs'` via `check_link` | ✅ unchanged |
| 12 | `[x](data.txt#frag)` non-md+fragment | anchor defect | `is not a markdown file` via `check_link` | ✅ unchanged |
| 13 | `[x](docs)` dir, no fragment | pass (exists) | `None` in both existence paths | ✅ unchanged |
| 14 | `[x](data.txt)` non-md, no fragment | pass (exists) | `None` | ✅ unchanged |
| 15 | `[x](docs/guide.md#the-guide)` healthy | pass | `None` everywhere | ✅ unchanged |
| 16 | Old-logic replay (reversal) | new assertions fail | old granular gives 1 defect vs new 4 | ✅ test proves fix |

Real corpus [RAN probe]: 349 links, `_existence_defects` 0, `_violations` 0 — no false positives [ASSUMED GitHub rendering beyond corpus shapes not oracle-checked, bounded to exotic shapes with no corpus exercise per W0-REV-01 §9].

No crash, hang, exception (except the race-only assert, not triggered), or unbounded behavior in any probe.

## 6. Dimension checklist

- **Correctness.** Delegation is faithful (§1.2 case analysis + §5 probes). MAJOR-1/2 closed (§4). Behavior deltas are exactly the two intended defect-producing direction changes; nothing else.
- **Security.** Test-only; no network/subprocess/writes added. `_existence_defect` re-splits destinations (linear). No new attack surface. MAJOR-3 untouched (lock-integrity hole stays open but recorded).
- **Error handling.** Malformed markdown -> sentinel, now reported by both paths. The only new exception path is the assert (NIT-1). Failure messages preserved verbatim.
- **Edge cases.** External/`//`/scheme, empty, dir, non-md, percent-encoding, multi-`#` (first-`#` partition) — traced and probed; bare-allowlist, fragmented-allowlist, sentinel each covered. Trailing-`#` (`fragment == ""`) behaves identically to old code (allowlisted) — no regression.
- **Conventions (zero-baseline).** [RAN `ruff check src tests` -> All checks passed]; [RAN `mypy src` -> exactly 3 baseline `subprocess_backend.py:548`]. Zero `noqa`/`type: ignore` in the new file [RAN count]. Line-length ≤100 (ruff). Docstrings carry the single-source rationale.
- **Maintainability.** Single source removes MINOR-4. Residuals: double resolve + duplicate external check (NIT-1), implicit no-`#` invariant (NIT-2) — both documented, both optional polish.
- **Test rigor.** Extended self-test drives both `_violations` and `_existence_defects` (the function the real test runs), with live and not_vacuous counts. Reversal proved independently (§5 #16 + §2 #10).
- **Performance.** One extra stat per fragmented link; negligible. No regression signal (34-dot run same order as baseline).
- **Operability / rollback.** `git revert 2f26fb8` (this task's code commit only); no migration, no data.

## 7. Optimization and simplification opportunities

- **DO-NOW (small):** none required for approval.
- **DO-LATER (follow-up, not this delta):** return the resolved target from `_existence_defect` to delete the second `resolve` + `assert` (NIT-1); add the no-`#` note at `_UNPARSEABLE` (NIT-2). Both are cosmetic cleanups for a future touch of this file.
- **DO-LATER (pre-existing, out of scope):** MAJOR-3 root containment (`resolve`/`check_link` requiring `candidate.resolve().is_relative_to(root.resolve())` with W0-REV-01 §5 probe #9 as regression test); MINOR-1 bracket-depth scan; MINOR-3 dedup tracking; MINOR-2 docstring note (root-fallback leniency by design). All recorded in W0-REV-01 §4 / W0-IMPR-01 §13 and untouched here — correctly not attempted.
- **NOT-WORTH-IT:** optimizing the double full-corpus scan (existence + anchor each scan ~80 files); wiring `IGNORED_DIRECTORIES` into resolution (scope-parity-only by design); `*.MD` glob without corpus need. Neither justifies churn.

## 8. Cross-agent interactions

- **W0-IMPL-01 (T-N0-1, d22f0d4):** this delta's parent. No interference; the 349/0/0 corpus reproduction matches both reviews.
- **W0-IMPL-02 (research rename, be51bba):** prerequisite file present in corpus; green with the delta. No issue.
- **W0-IMPL-05 (N0.5 triage):** disjoint files; no overlap with this diff.
- **W0-REV-01 (APPROVE-WITH-CHANGES, 1ccf3f0):** its MAJOR-1/2/3 and MINOR-4 expectations are the acceptance bar for this delta. MAJOR-1/2 + MINOR-4 met; MAJOR-3 deferred per that review's own recommendation ("filed as a follow-up ... rather than bolted on here"). MINOR-1/2/3 remain as filed.
- **W0-IMPR-01 (2f26fb8 + 21ef990):** report audited claim-by-claim in §2 (11/11 verified). No scope violation.
- **ORCH spec-anchor fix (COMMS 2026-11-09T07:30:00Z):** independent of this delta; the 349-link corpus is green with or without it (untracked file absent from CI checkout). No action.
- **No edits outside the WRITE-SET.** Scratch probes lived in the system temp dir; the repo received only this report.

## 9. What I could not verify

- **Historical pre-`2f26fb8` suite behavior by checkout** — would require stash/reset (forbidden). Reversal was proved by replaying the old granular logic in a probe instead, which is equivalent for the patched functions [RAN old-logic 1 vs new 4].
- **GitHub renderer as an oracle for sentinel/allowlist edge shapes** — no renderer in-repo; judgments rest on the documented semantics (unparseable -> literal brackets; bare name alone is the sanctioned reference) and code tracing. The corpus passes end-to-end (349/0 [RAN]).
- **Full `pytest tests/ -q`** — out of scoped brief (only `tests/conventions` run [RAN: 34 dots]). Full-suite green is therefore NOT claimed by this review.
- **Linux CI shape** — this host is Windows [RAN shows `C:\...` paths]; `Path.exists()`/string logic has no platform surface beyond what the suite exercises. [UNKNOWN] run on Linux.
- **`mypy` on the test file itself** — repo gate runs `mypy src` only; the test file is covered by ruff (0 findings [RAN]) but not mypy by design.

## Verdict

**APPROVE.** MAJOR-1 and MAJOR-2 are closed through the real granular path (not just the helper), MINOR-4 is closed by construction, the corpus stays at 349 links / 0 violations, and the scoped gates hold (conventions 34 passed, ruff 0, mypy exactly 3). MAJOR-3 is deferred correctly — recorded as DEFERRED-DEPENDENCY with its probe and proper fix, not silently dropped or half-fixed, and `resolve` is untouched. No unrelated files edited; no scope creep (NIT-1/4, MINOR-1/3 left alone per brief). The two NITs in §4 (double resolve + race-only assert; implicit no-`#` invariant) are optional polish for a future touch, not approval blockers.
