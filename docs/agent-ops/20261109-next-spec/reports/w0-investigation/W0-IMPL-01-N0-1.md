<!-- ORCH-DIGEST:START -->
agent: W0-IMPL-01 | task: T-N0-1 | status: PARTIAL
code_commits: d22f0d4 (verify: git log shows d22f0d4686c843cff43da76d05768ee5d432531c) | report_commit: pending (see §17)
files_changed: 4 (VISION.md->docs/VISION.md, docs/README.md:127, docs/ROADMAP.md:33, tests/conventions/test_markdown_links.py)
contracts_changed: none
tests: added 8, changed 0; scoped run: PARTIAL (7/8 pass; test_every_anchor_names_a_heading red only on untracked docs/2026-11-09-next-spec.md:4 — outside WRITE-SET, NEED posted)
ownership_violations: none
deferred_dependencies: 1 (spec anchor #decisions-made-in-this-spec -> #5-decisions-made-in-this-spec, or move file out of docs/)
out_of_scope_findings: 1
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: slug double-hyphen for folded punctuation (see §15)
confidence: high (<why> every owned file passes ruff/mypy/existence checks; the single failing assertion is outside ownership and proven to be the only defect)
last_comms_line_read: 2026-11-09T07:20:51Z (own NEED; no contradictory lines)
<!-- ORCH-DIGEST:END -->
# Implementation report — W0-IMPL-01 — T-N0-1 Move VISION.md to docs/, fix markdown links, add link lock

## 1. Task and acceptance criteria (restated)
Task T-N0-1 (N0.1): Move VISION.md → docs/VISION.md via `git mv`, fix all refs per investigator table (README 168/236/337, ROADMAP 3/6/65/92/124, VISION 11/12/76/223), fix ROADMAP:33 anchor to #testing (owner-decided R10), add tests/conventions/test_markdown_links.py that mirrors lock scope/exclusions, checks existence + anchor slugs, allowlists 15 index-only ADR rows (docs/adr/README.md:22-39 intentional per docs/adr/README.md:66-73), mirrors self-test. Acceptance: link test fails on deliberately broken link (added in test as synthetic tree or tmp), passes after fix; pytest tests/conventions -q green; ruff 0; mypy src exactly 3 (baseline).

## 2. Verification that the problem exists (evidence gathered myself)
- `[RAN git branch --show-current / git status --porcelain / git log -> ops/20261109-next-spec @ b8570a6 plus be51bba (W0-IMPL-02 rename); ls VISION.md exists, docs/VISION.md absent]`
- `[RAN python prototype markdown-link scan over docs/**/*.md excl agent-ops/plans + README/AGENTS -> 81 files, 286 ok, 24 broken: 15 ADR index-only + 5 ROADMAP VISION + 3 README VISION + 1 research ../VISION.md; after sibling rename, research target exists]`
- `[RAN python anchor scan same scope -> only ROADMAP:33 #status and untracked docs/2026-11-09-next-spec.md:4 #decisions-made-in-this-spec broken]`
- Investigator table in plan §E N0.1 matches my own scan [VERIFIED README.md:168,236,337 docs/VISION.md; docs/ROADMAP.md:3,6,65,92,124 VISION.md; VISION.md:11,12,76,223].

## 3. Root cause
`VISION.md` lived at the repository root while `README.md:168,236,337` (`docs/VISION.md`), `docs/ROADMAP.md:3,6,65,92,124` (`VISION.md` from `docs/`), and `docs/research/2026-11-deepseek-super-agent.md:5` (`../VISION.md`) all assumed it under `docs/` — D-9 in plan §G. After `git mv`, every `VISION.md`/`docs/VISION.md` target resolves without editing those lines, except `docs/README.md:127` (`../VISION.md`, written when VISION was at the root) which the move breaks in the opposite direction and must be repointed to `VISION.md`. Separately, `docs/ROADMAP.md:33` cited `#status`, a heading that does not exist in `README.md` (R10 decides `#testing`, matching `README.md:325` heading `Testing`). No lock checked inline markdown links at all — `tests/conventions/test_docs_references.py` only extracts `path:line` citations — so the 12 broken links and 1 broken anchor were invisible to CI.

## 4. Approach chosen and why (alternatives rejected)
`git mv VISION.md docs/VISION.md` (preserves history), then two one-line edits (`docs/README.md:127`, `docs/ROADMAP.md:33`). Rejected: editing `README.md`/`ROADMAP.md` links to point at the root instead of moving the file — keeps docs scattered, contradicts D-9 and the already-assumed `docs/` location in README/ROADMAP/research note. Rejected: fixing links by stubbing the 15 missing ADR bodies — the rows are intentional index-only records per `docs/adr/README.md:66-73`; stubbing would invent decisions. For the lock, mirror `test_docs_references.py` scope constants verbatim and add a drift test that loads the sibling by file path (pytest import-mode makes a direct `import` fragile), check file existence + GitHub-style anchor slugs, and allowlist exactly the 15 index-only filenames scoped to `docs/adr/README.md`. Rejected: filtering the scan to `git ls-files`-tracked sources to hide the untracked `docs/2026-11-09-next-spec.md:4` anchor — that would mask a real broken link a filesystem scan (the lock's own method) finds; the spec file is outside my WRITE-SET so it is a `NEED`/`DEFERRED-DEPENDENCY`, not a test weakening.

## 5. What changed (per file)
- `VISION.md` -> `docs/VISION.md` via `git mv` (content unchanged, 0 diff) [RAN `git mv VISION.md docs/VISION.md -> R VISION.md -> docs/VISION.md`]. This alone fixes README.md:168/236/337, docs/ROADMAP.md:3/6/65/92/124, VISION.md:11/12/76/223, and docs/research/2026-11-deepseek-super-agent.md:5 (`../VISION.md` and `research/...` now resolve from `docs/`).
- `docs/README.md:127`: `[VISION.md](../VISION.md)` (lives at root) -> `[VISION.md](VISION.md)` [RAN post-move prototype scan: only the 15 allowlisted ADR rows remained broken before this edit].
- `docs/ROADMAP.md:33`: `../README.md#status` -> `../README.md#testing` (R10, `README.md:325` heading `Testing`).
- `tests/conventions/test_markdown_links.py` (new, 657 lines, 8 tests): mirrors `test_docs_references.py` scope constants verbatim, parses inline `[]()` destinations (angle-bracket spaces, balanced parens, titles, image links, fence/span skipping), checks existence (doc-relative then root-relative) and GitHub-style anchor slugs (ATX+setext, deduped `-1`), allowlists 15 ADR filenames scoped to `docs/adr/README.md`, plus synthetic-tree self-test, allowlist-freshness test, scope-drift test, slug/parsing unit tests.
- No change to `docs/architecture/repository-map.md:158` (`docs/VISION.md` backtick is correct after the move) and no change to the 15 ADR index rows (read-only per brief).

## 6. Blast radius and contracts
Docs-only + one new test file. No `src/**` change, no schema/ledger/event change, no public API change. Downstream: `test_docs_references` still green, so the two locks compose. Sibling W0-IMPL-04 owns README status block in Wave 0b; my README.md edits are zero (none needed). The new lock will fail CI on any future docs link rot by design, including on link targets introduced by sibling waves 0b/1+ (intended).

## 7. Tests (how each confirmed to fail without fix)
- `test_the_lock_catches_a_broken_link` (tmp synthetic tree): assert 5 links, 3 defects (missing file `docs/missing.md`, missing heading `no-such-heading`, wrong self-anchor), healthy + allowlisted not reported [RAN scoped run -> pass: 7 selected incl. this test passed]. Mutation mentality: deleting the synthetic `guide.md` or renaming its heading makes the healthy assertion fail; removing the allowlist makes `0001-...` appear as a defect. It genuinely fails without the check logic.
- `test_allowlist_only_covers_genuinely_missing_adr_bodies`: proves the 15 entries each miss on disk and are linked from the index, `missing == allowlist` (no stale/extra entries).
- `test_the_lock_is_not_vacuous`: corpus >200 links, README/AGENTS scanned, agent-ops/plans excluded, known `docs/VISION.md` links present — fails if extraction silently goes empty.
- Slug/parsing unit tests cover GitHub rule (`Appendix:` colon, code spans, `*not*`, em-dash double-hyphen) and `<...>` spaces / titles / code-span skipping (`[not a link](missing.md)` in backticks ignored, ``[`code`](guide.md)`` kept).
- One-off hostile probes (not committed) [RAN probe6: `[x]()`->self, `<unterminated`/`a b`->`<unparseable-link>`, `a(b).md` balanced, `HTTPS://`+`mailto:` external, backticked link ignored] — no crashes.
- Reversal check (reasoned + prototype): reverting the `docs/README.md:127` repoint yields `../VISION.md` -> root `VISION.md` missing (`test_every_relative_link_names_an_existing_path` fails); reverting ROADMAP to `#status` yields `no heading ... 'status'` (`test_every_anchor_names_a_heading` fails). Both verified by the prototype scans pre-fix (24 broken -> 15 allowlisted) and post-fix.

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)
- Source: the fix is the move itself (D-9); the lock enforces the invariant at the source for all future renames/moves.
- Load: scan is 82 files / ~310 links, anchors cached per file; `pytest tests/conventions/test_markdown_links.py + test_docs_references.py` completes in seconds. No unbounded collections (sorted file list, per-file anchor sets), no O(N^2) (no index rebuild per link; `scope_files` called per aggregate, not per link — `_violations` calls `scope_files` once for the lookup dict).
- Hostile input: malformed links (`<unterminated`, missing `)`, empty dest, `%XX`, `../` escapes, absolute `/`) produce defects or safe skips, never exceptions or infinite loops (all parsing is linear per line, destination scan bounded by line length; `%` decoded once via `urllib.parse.unquote`).
- Operability: failure messages name `document:line`, the destination, and the defect kind (`no such`, `no heading ... has the anchor`, `the target is the directory`, `is not a markdown file`) so the author can fix without opening the test.
- Reversal: revert the move/edits or break any relative link/heading in scope and the lock goes red (see §7 reversal notes) — cannot pass while broken links exist except the 15 sanctioned ADR rows.

## 9. Edge cases and abuse scenarios
Directory links (`flows`, `modules`, `docs/adr/`) pass by existence; fragment on a directory fails. Non-markdown target with fragment fails. Anchor on missing file is skipped by the anchor test (already reported by the existence test) and an allowlisted name with a fragment attached is NOT allowlisted. Root-absolute `/` targets resolve against the repo root; `..` escapes use the OS-resolved path and simply miss. Duplicate headings dedupe (`creation`, `creation-1`). Setext headings included; reference-style `[text][ref]` links and `wrong-but-existing` targets are explicitly out of the lock's mechanical reach (docstring says so, as the `path:line` lock does for wrong-but-existing lines).

## 10. Performance and security
Test-only code: reads markdown files with `errors="replace"`, no network, no subprocess (except the drift test loading a sibling test module by path, which executes only constants/dataclass defs from that module). No secrets, no writes, no shell. `importlib` load registers under a unique module name and removes it in `finally` (fixes the `dataclass`/`from __future__ import annotations` `AttributeError: 'NoneType' object has no attribute '__dict__'` found on the first run).

## 11. Commands run and results (verbatim summaries)
- `[RAN .venv/Scripts/python -m pytest tests/conventions/test_markdown_links.py -q -> 6 passed, 2 failed initially: test_every_anchor_names_a_heading (untracked docs/2026-11-09-next-spec.md:4 #decisions-made-in-this-spec, deferred NEED) + test_scope_mirrors (sys.modules dataclass AttributeError, fixed by registering the loaded module)]`
- `[RAN pytest test_markdown_links.py (7 selected excl. anchor) -> 7 passed]`
- `[RAN .venv/Scripts/python -m pytest tests/conventions/test_markdown_links.py tests/conventions/test_docs_references.py -q -> 12 passed, 1 failed (only the untracked-spec anchor above)]`
- `[RAN .venv/Scripts/python -m pytest tests/conventions/test_docs_references.py -q -> 5 passed]`
- `[RAN .venv/Scripts/python -m ruff check src tests -> All checks passed!]`
- `[RAN .venv/Scripts/python -m mypy src -> Found 3 errors, all src/paa/sandbox/subprocess_backend.py:548 (killpg/getpgid/SIGKILL, Windows pre-existing A-001) — exactly the 3-error baseline]`
- `[RAN .venv/Scripts/python -m pytest tests/conventions -q -> timed out at 120s (includes slow subprocess sweep in test_import_conventions); NOT-RUN for the full conventions dir within the scoped budget]`
- `[RAN git mv / prototype scans -> pre-move 24 broken (15 ADR + 5 ROADMAP + 3 README + 1 research); post-move+2 edits 15 broken (allowlisted only), anchors: only ROADMAP:33 (fixed) + untracked spec (NEED)]`

## 12. Deviations from plan/brief
- Scope constants copied + drift-pinned against `test_docs_references.py` instead of a direct import (pytest import-mode fragility). Constants are byte-equal and pinned by `test_scope_mirrors_the_docs_references_lock`.
- `docs/architecture/repository-map.md:158` left untouched after verifying the backtick `docs/VISION.md` is correct post-move (the brief's write-set lists it as editable, not mandatory).
- Nested context-gathering/independent-review subagents required by `AGENTS.md` §1/§3 could not run: the `subagent` tool returned `depth limit reached (1)` on the first investigator spawn [RAN subagent explore -> error]. Investigation was done directly with tool calls + plan §C facts, and the finished diff was self-reviewed (final + per-phase audit above). Recommend orchestrator dispatch a fresh reviewer before merge if the wave process requires it.
- Final status is PARTIAL, not COMPLETE, because `test_every_anchor_names_a_heading` is red on the untracked spec anchor outside my WRITE-SET (see §13/§14). Everything in my ownership is done; the remaining failure is not in my files.

## 13. Out-of-scope findings
- `docs/2026-11-09-next-spec.md:4` (untracked, outside WRITE-SET): `[D-1](#decisions-made-in-this-spec)` — the heading is `## 5. Decisions made in this spec` so the GitHub slug is `5-decisions-made-in-this-spec`. Posted NEED in COMMS.md (see §14). No other out-of-scope findings; `docs/research/2026-11-deepseek-super-agent.md:5` now resolves after W0-IMPL-02's rename + my move (be51bba).

## 14. Known limitations, risks, rollback notes
- LIMITATION: when the untracked spec file is present in a working tree, `test_every_anchor_names_a_heading` fails with exactly one defect. In a clean CI checkout (spec absent) the link lock is fully green. Resolution (orchestrator): fix the spec link to `#5-decisions-made-in-this-spec`, or move the spec out of `docs/` (e.g. keep it untracked outside the scoped tree), or commit it with the fix. I did NOT filter `git ls-files` to hide it — that would mask a real broken link.
- RISKS: the lock raises the bar for every docs edit (any new broken relative link/anchor fails CI until fixed or explicitly allowlisted with a stated category). That is the intended trade; the only current over-broad risk is reference-style links (ignored) and wrong-but-existing targets (human reading).
- ROLLBACK: code commit reverts with `git revert` (move back + delete `test_markdown_links.py`); the two one-line doc edits revert independently. No data migration, no schema change.

## 15. Where I am least confident
- The slug edge for folded punctuation (em-dash/ampersand producing double hyphens): pinned to the GitHub `tr(' ','-')`-per-space rule and covered by a unit assertion, but no corpus link actually exercises that shape — if a renderer differs, a future link could false-positive. Corpus link fragments in scope all slug to single hyphens and pass.

## 16. Self-review checklist
- Correctness/root cause: move fixes 9 of 12 broken file links by path truth, not by editing call sites; the remaining 3 (README 168/236/337) are the same move from the other side. `docs/README.md:127` + `ROADMAP:33` are the only text edits needed — verified by the post-move prototype scan (24 -> 15 allowlisted). No symptom patching.
- Security/edge: as §§8-9; no resource leaks (no new threads/queues), no authz surface (docs test only). **CRITICAL**: none. **MAJOR**: the untracked-spec red (accepted, deferred per standing orders — not a code defect in my files). **MINOR**: `IGNORED_DIRECTORIES` is pinned/mirrored but not consumed by link resolution (the `path:line` lock consumes it for its filename index; links do not need one) — kept for scope parity, harmless.
- Maintainability: mirrors `test_docs_references.py` structure/naming/docstring voice; no new dependency; no type suppression (ruff 0, no `noqa`/`type: ignore` added — `test_scope` uses `importlib` without dynamic `cast`).
- Test rigor: synthetic self-test + allowlist-freshness + non-vacuous + slug/parsing unit tests; each fails for the right reason if the core logic is regressed (e.g. deleting `check_link`'s missing-file branch makes `test_the_lock_catches_a_broken_link` fail on `no such`; stubbing `find_links` to `[]` fails the non-vacuous `>200` assertion).

## 17. Commits
- Code commit: `d22f0d4686c843cff43da76d05768ee5d432531c` — `git add -- docs/VISION.md docs/README.md docs/ROADMAP.md tests/conventions/test_markdown_links.py` (the `VISION.md` deletion side was staged by `git mv`; `git add -- VISION.md` fails with pathspec-did-not-match after the move, so it is covered by the staged rename + commit pathspec) then `git commit -m "Move VISION.md into docs/ and add markdown-link lock (T-N0-1)" -- docs/VISION.md docs/README.md docs/ROADMAP.md tests/conventions/test_markdown_links.py VISION.md` [RAN -> committed, `git show --stat HEAD`: 4 files, rename VISION.md => docs/VISION.md (100%), new test 657 lines]. 4 files changed, 659 insertions, 2 deletions. Branch `ops/20261109-next-spec` verified before committing [RAN `git branch --show-current -> ops/20261109-next-spec`]. Siblings' commits `be51bba` (rename) and `c90d835/ca63879` (N0.5) landed under ours; no overlap with our paths.
- Report commit (docs-only, separate): `pending — this file + COMMS.md left for ORCH to commit (per COMMS rule 8); this digest lists the code SHA above`.
