<!-- ORCH-DIGEST:START -->
agent: W1-IMPL-03 | task: T-N1-1 (N1.1) | status: DONE
code_commits: a74f5f0 | report_commit: pending
files_changed: 2 (src/paa/coder/fs_read_tools.py, tests/coder/test_fs_read_tools.py)
contracts_changed: none (new module only; no workspace.py / __init__.py / permissions edits)
tests: added 57, changed 0; scoped run: pass (56 initial + 1 post-review = 57)
ownership_violations: none
deferred_dependencies: none
out_of_scope_findings: 1 (sibling patch.py/docs validation.md citation, CONFLICT in COMMS)
temporary_mitigation: no
deep_issue_flag: yes (rejected a positional UTF-8 heuristic for an incremental-decoder design at self-review)
least_confident_about: none — jail semantics are the frozen module's; my code only orders correctly
confidence: high (57 tests, ruff 0, mypy 0 on file, mutation check + hypothesis property executed)
last_comms_line_read: 2026-11-09T11:00:00Z
<!-- ORCH-DIGEST:END -->
# Implementation report — W1-IMPL-03 — T-N1-1 (N1.1) fs read tools

## 1. Task and acceptance criteria (restated)
Implement `src/paa/coder/fs_read_tools.py` with `read_file`, `glob`, `grep` using the frozen `Workspace` jail (N1.0a, approved in W1-REV-01). Size-capped (64 KB read, truncated+marked, never silent), UTF-8 round-trip, CRLF preserved on read passthrough, BOM handled, binary refused with clear error. Every path: `assert_inside_workspace` then `is_protected` (denied with frozen reason string), symlink escape denied. Export names frozen: `read_file`/`glob`/`grep`. Tests `tests/coder/test_fs_read_tools.py`: Hypothesis path-jail property (NIT-4), protected (.git/.env*/PAA_HOME) denied with reason, UTF-8/CRLF/BOM/binary, truncation marked, read-only posture in PLAN/ASK/LOCKDOWN. Acceptance: new tests pass, ruff 0, mypy 3 pre-existing, conventions 34.

## 2. Verification that the problem exists (evidence gathered myself)
- [VERIFIED src/paa/coder/loop.py:85-97] `Tool` protocol: `name`/`description`/`schema()`/`run(arguments: Mapping) -> str`.
- [VERIFIED src/paa/coder/runtime_tools.py:73-82] `READ_ONLY_TOOLS` frozenset + `build_runtime_tools(rt)` dict factory + errors-as-values convention (`_emit` JSON payload, bad input reported as `{"error": ...}`, never raised).
- [VERIFIED src/paa/coder/workspace.py:55-61,239,279] frozen `__all__` (5 names), ws-first `assert_inside_workspace`, `is_protected -> str | None` returning the 3 frozen strings.
- [VERIFIED src/paa/coder/permissions.py:40-56,186-218] coder `PermissionMode` = ASK/ACCEPT_EDITS/PLAN/BYPASS (no LOCKDOWN member); PLAN allows non-mutating tools; `_MUTATING_TOOLS` globs at 127-137.
- [VERIFIED] `src/paa/coder/fs_read_tools.py` did not exist before this task (directory listing showed only `agent/commands/hooks/loop/model_adapter/permissions/runtime_tools/session/workspace/__init__.py`).
- [VERIFIED tests/conftest.py:23-30] `paa_home` fixture sets `PAA_HOME` + `reset_settings_cache()` — required because `is_protected` re-reads `get_settings().home` per call.

## 3. Root cause
N/A (greenfield task). The gap is absence: the coder catalogue had no filesystem-read tool, so the agent could not see the working tree.

## 4. Approach chosen and why (alternatives rejected)
- **Errors as JSON values, not exceptions** (root-cause-first, per the loop's contract). `AgentLoop._handle_call` (loop.py:237-241) turns any raised exception into `"error: ..."` with no structure and counts it as a failed call; the runtime tools established the "report the refusal as data" convention so the model can reason and retry (e.g. ask for a different path). Every expected refusal — escape, protected, missing, directory, binary, bad args, malformed regex — returns `{"error": ..., "path": ..., "reason": ...}`. Alternatives rejected: raising `WorkspaceViolation` bare (loses the reason string the ADR requires to surface verbatim), and returning the frozen reason as a bare string (breaks the JSON convention and the `path`/`reason` fields Wave 2 journalling will need).
- **Jail-then-protect order in one helper** (`_jail_then_protect`, module docstring + reviewer F3). Relative args resolve against `ws.root` (not CWD — see below), `assert_inside_workspace` canonicalises and returns the resolved path, `is_protected` is consulted on the *returned* path only. Alternatives rejected: per-tool ad-hoc checks; protect-only (is_protected alone is not a jail).
- **Byte-oriented reads, incremental UTF-8 decode.** Files are opened `rb` (never text mode — the FakeSnapshotter docstring in tests/conftest.py records exactly why), capped by reading `MAX_READ_BYTES + 1` and decoding with `codecs.getincrementaldecoder("utf-8-sig")` + final flush. The decoder was introduced in self-review to replace a positional heuristic (`exc.start >= len(raw) - 3`) that would have silently accepted a genuinely binary file whose only invalid byte sat within three bytes of the cap boundary (mutation-verified — see §7). The incremental feed distinguishes a cap-split character (raises only at flush, only when truncated) from invalid bytes (rais(d)ing during the feed) structurally. BOM stripping is inherent to `utf-8-sig`; CRLF/LF preservation is inherent to never using text mode.
- **Glob via translated regex + `os.walk(followlinks=False)`, not `Path.glob`.** Docstring at `_glob_to_regex` records why: stdlib `**` recursion follows symlinked directories, so a symlink loop would hang enumeration and a symlink pointing outside would leak outside entries. The walk never descends a symlinked dir; every *matched* entry is still individually jailed and protected (so a symlink *file* pointing outside is dropped and counted, not followed), and the top-level `.git` dir is pruned as a work bound only (entry-level `is_protected` is the security decision).
- **Grep walks + per-file jail/protect/binary-skip** with a 64 KB per-file cap, match/line caps, and `.git` prune. Binary/non-UTF-8 files are skipped (correct search semantics) rather than erroring the whole grep.
- **Absolute patterns denied explicitly** (`/`, `C:/`, `C:`, UNC, control chars) because a glob pattern is workspace-relative by definition; the literal base (leading non-meta segments, where `..` can only occur) is jailed+protected as a directory so `../*` and `.git/**` are denied with the frozen reason. Backslash is folded to `/` for matching (model-facing POSIX spelling); the `..` escape check splits on both separators.
- **Literal duplication of `_emit`/`_clamp_limit`/`_ESCAPES_REASON`.** Importing `runtime_tools._emit` (private) would couple this module to a sibling's internals; the workspace module itself documents that its reason constants are private and tools surface strings from return values (pathe pattern denials have no workspace call to surface from, so the literal is raised). Both dual-purpose DECISIONS are recorded in code comments. `FS_READ_TOOLS` mirrors `READ_ONLY_TOOLS`' posture; registration into `AgentLoop`/ASK standing-allow is N1.5's (out of write-set).
- **LOCKDOWN read posture expressed as non-mutation** (see §12 deviation): the coder `PermissionMode` has no LOCKDOWN member, so the brief's "allow in all three" is pinned as: PLAN → ALLOW, BYPASS → ALLOW (trivially), ASK + standing allow → ALLOW, ASK without allow → ASK (never DENY), plus the core `PermissionMode.LOCKDOWN.granted == {SANDBOX_RUN}` pin showing these tools need no sandbox grant.

## 5. What changed (per file)
- **`src/paa/coder/fs_read_tools.py` (new, 613 lines).** `build_fs_read_tools(ws) -> {read_file, glob, grep}`; `_ReadFile` (jailed path → dir/not-found errors → capped byte read → NUL/binary refusal → incremental UTF-8 decode → `{path, content, encoding, had_bom, truncated, size_bytes, note}`); `_Glob` (pattern-shape denial → literal-base jail → walk → regex match → per-entry jail/protect → `{pattern, paths, count, truncated, skipped_protected, skipped_escaped, note}`); `_Grep` (regex compile → jailed root → walk `is_file` entries → glob filter → per-file jail/protect → capped read → skip binary/non-UTF-8 → line matches → `{pattern, path, matches[{path, line, text}], count, files_scanned, files_truncated, files_skipped, truncated, note}`); `_decode_utf8`, `_glob_to_regex`, `_check_glob_pattern`, `_clamp_limit`, `_walk`, `_Denied`, `FS_READ_TOOLS`, `MAX_READ_BYTES`.
- **`tests/coder/test_fs_read_tools.py` (new, 57 tests).** Catalogue (exact frozen names, schema/description, no mutating-glob match); read_file basics (verbatim, UTF-8, CRLF+LF preserved, BOM, binary refused, non-UTF-8 refused, missing/dir/bad args, absolute-inside allowed, interior symlink allowed, escape symlink denied, symlink-into-protected denied, 64 KB truncation marked, char never split at cap, invalid byte near cap still refused); Hypothesis jail property (NIT-4); protected matrix (git case variants, env any-depth, check-before-existence, PAA_HOME inside workspace, PAA_HOME==root, escape-beats-protect ordering); glob (recursive+sorted, `*` single-segment, protected never returned, `.git` never listed, escape patterns denied incl. drive/UNC, symlinked dir never followed, limit marked, empty pattern); grep (path/line/text, regex, path+glob scoping, invalid regex, binary/non-UTF-8 skipped, protected/`.git` never searched, symlink escape never read, escape root denied, missing path, limit marked); permission posture (PLAN/BYPASS ALLOW, ASK+standing-allow ALLOW, ASK-never-DENY, core LOCKDOWN grant pin, real `AgentLoop` under PLAN executes a read).

## 6. Blast radius and contracts
- New module only; zero edits outside the write-set. No `workspace.py`, `loop.py`, `runtime_tools.py`, `__init__.py`, `permissions.py`, `agent.py`, or `cli/coder.py` modification, so no existing contract can break (the mutation set is empty: the module is additive).
- New public names: `build_fs_read_tools`, `FS_READ_TOOLS`, `MAX_READ_BYTES`. N1.5 (registration) and N1.2–N1.4 (siblings) consume them; N1.5 will import `build_fs_read_tools` + `FS_READ_TOOLS`.
- Tool output shape is JSON error-or-data (this module's own contract, mirroring runtime_tools). `reason` carries the frozen verdict verbatim for jail/protect denials.

## 7. Tests (how each confirmed to fail without fix)
- **Self-review decode mutation (MAJOR, fixed).** First version of `_decode_utf8` used a positional heuristic (`exc.start >= len(raw) - 3`) to excuse decode failures at the cap tail. Mutation check executed: for `b"a"*65534 + b"\xe9" + b"b"` (invalid byte at 65534, inside the "tail window"), the old heuristic ACCEPTS (`err.start=65534 >= len-3=65533` → would return 65534 `a`s as valid text). The incremental decoder RAISES during the feed → refused. The new test `test_invalid_byte_near_the_cap_is_still_refused` asserts refusal; it fails on the old logic for exactly this reason [RAN one-liner diff of both decoders].
- **Baseline test failures (run 1: 53 passed / 3 failed).** All three were test-side errors, not module bugs: (a) `"utf-8" in error` vs message's uppercase `UTF-8` — assertion fixed to `UTF-8`; (b) Hypothesis `function_scoped_fixture` health check — suppressed with documented justification (read-only sharing; sentinel rewritten identically per example); (c) `skipped_escaped >= 1` asserted against pattern `**/*.txt` which correctly never matches the dir-link entry — test now checks `**/*.txt` for non-leakage and `**/*` for the escape-count. Re-run: **57 passed** [RAN pytest tests/coder/test_fs_read_tools.py -q -o addopts=""].
- **Reversal.** Deleting `fs_read_tools.py` makes the whole test module fail at import (the security invariant is pinned by the 57 tests: 1 Hypothesis property + 5 symlink-escape + 10 protected + 15 glob/grep enumeration tests + truncation/encoding pins). Reverting only `_jail_then_protect`'s `is_protected` call (protected-check removal) would flip every protected test to success-with-secret and fail 10+ tests. Reverting the incremental decoder back to the positional heuristic fails the new near-cap refusal test (mutation-verified above).

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)
- **Source.** No new external dependency, no provider string, no ambient state. Per-call cost is `stat` + one capped read, plus one `Path.resolve()` in the jail per path touched (the ADR-mandated pattern). `is_protected` re-reads `get_settings().home` per call (lru-cached; cheap after first load).
- **Load (100×, concurrency, partial failure).** Bounded everywhere: `MAX_READ_BYTES + 1` per file, `_MAX_WALK_ENTRIES = 10_000` per enumeration, glob matches clamped ≤ 1000 (default 200), grep matches ≤ 200 (default 50) × ≤ 500 chars per line. A grep over a 10k-file tree costs ~10k × (2 resolves + capped read); acceptable for an interactive agent turn, and the walk bound makes worst case finite. The tools are stateless — concurrent calls race only on the caller's disk writes, not on tool state. A file vanishing between `is_file()` and `open()` raises `OSError`, caught and reported as an error value (never a crash). A file growing mid-read: `truncated` is derived from the bytes actually read, not from the `stat()`, so the marker stays truthful.
- **Hostile input.** Oversized file, binary, non-UTF-8, symlink file/dir escapes, symlink loops (walk never descends symlinked dirs), missing path, `../`, absolute/drive/UNC/mixed-separator shapes, empty/wrong-type args, invalid regex, control characters in pattern/path, `limit` 0/negative/huge (clamped). Traversal root cause is the frozen jail + per-entry jail, not string manipulation.
- **Operability.** Every refusal names its reason verbatim (frozen strings for jail/protect; plain messages otherwise), and truncation is flagged with `note` text. No secrets, file contents of protected files, or environment values are ever emitted (protected content is never read).
- **Reversal.** See §7 Reversal.

## 9. Edge cases and abuse scenarios
- **Windows separator/drive forms in patterns:** `C:/`, `C:`, UNC `\\\\`/`//`, control chars, backslash-folded `..\\` — all denied as escapes before any walk (§12 gives the absolute-pattern set).
- **`glob` of a `.git/**` base or `.env` file:** literal base is jailed+protected, so the whole call denies with the frozen reason rather than returning empty.
- **`.GIT/.ENV` case variants:** `is_protected` casefolds; pinned by tests.
- **`PAA_HOME` == workspace root:** every path protected (test-pinned); PAA_HOME inside workspace: subtree protected (test-pinned).
- **Char straddling the 64 KB boundary** (snowman test): dropped partial char, `truncated: true`, no error, no replacement glyph.
- **Invalid byte near the boundary** (mutation test): refused, never returned as text.
- **Empty file / empty pattern / empty path arg:** empty file reads `""` with `truncated: false`; empty pattern/path are error values.
- **Zero/clamped limits:** `limit` 0 → clamped to 1; huge → clamped to 1000 (glob) / 200 (grep).
- **Overlong grep lines:** capped at 500 chars + `…` marker (documented in §14).

## 10. Performance and security
- **Resource bounds.** See §8 Load. No unbounded loops/queues/payloads: walk is capped, reads are `cap + 1`, match/line lists are capped.
- **Security (trust boundary).** The module adds no trust surface of its own: containment is the frozen jail; protection is the frozen set; shell/network/process surfaces are absent. `OSError` messages use `exc.strerror` (a generic OS string, never a secret value, and never file content).
- **ReDoS residual (documented, out of scope).** Grep uses Python's backtracking `re` on model-authored patterns against repo files. Per-file work is bounded (64 KB, line-at-a-time search), and the walk is capped, but a pathological pattern such as `(a+)+$` over a long fill line could still take exponential time on one file. This matches the loop's existing posture (`re` is used in `permissions.py` rule matching) and is recorded as a known limitation, not patched with a timeout hack (forbidden). A future hardening could run the search under `timeout`/thread deadlines or migrate to RE2; either is a separate task.

## 11. Commands run and results (verbatim summaries)
- [RAN `pytest tests/coder/test_fs_read_tools.py -q -o addopts=""` → **57 passed in 1.99s**]
- [RAN `ruff check src/paa/coder/fs_read_tools.py tests/coder/test_fs_read_tools.py` → **All checks passed!** (initially 3 fixable findings — SIM300 Yoda, 2× UP012 — fixed by rewriting, not suppressing)]
- [RAN `mypy src/paa/coder/fs_read_tools.py` → **Success: no issues found in 1 source file** (venv python; system-python mypy fails entirely on its own numpy stubs — environment artifact, not this module)]
- [RAN `pytest tests/conventions -q -o addopts=""` → **33 passed, 1 failed in 208.66s**; single failure `test_every_referenced_line_is_not_blank`: `docs/modules/validation.md:46` cites `src/paa/validation/patch.py:786` which is now blank after sibling W1-IMPL-04's uncommitted `patch.py` edits. Retried once (single test, 14.84s) → still red. Reported as CONFLICT in COMMS.md; not fixed (outside write-set). My report/code introduce zero `path:line` references.]
- [RAN mutation one-liner (old heuristic vs incremental decoder) → old accepts, new refuses — proof the regression test is not vacuous]
- [RAN `git log --oneline -5 -- src/paa/validation/patch.py` → `9631b5e` + uncommitted `M src/paa/validation/patch.py` (sibling's working tree) — confirms CONFLICT attribution]
- NOT RUN per standing order 16: full suite, container rebuilds, DB migrations. Scoped checks only, as instructed.

## 12. Deviations from plan/brief
1. **LOCKDOWN read posture.** The brief asks for ALLOW in "PLAN/ASK/LOCKDOWN". Neither the coder `PermissionMode` (ASK/ACCEPT_EDITS/PLAN/BYPASS only — permissions.py:40-56) nor `ToolPermissionPolicy` has a LOCKDOWN member; LOCKDOWN exists only as the core `PermissionMode.LOCKDOWN` (grants `{SANDBOX_RUN}` alone, types.py:320), which the read tools never need. "Read tools allow in LOCKDOWN" is therefore pinned as: PLAN→ALLOW, BYPASS→ALLOW, ASK+standing-allow→ALLOW, ASK-never-DENY, mutating-glob non-match, and the core LOCKDOWN grant pin. No coder-side LOCKDOWN exists to test ALLOW against; inventing one is outside this task. (N1.5 registering `FS_READ_TOOLS` into the standing-allow set is also out of write-set and recorded in §7 as N1.5's step.)
2. **Glob semantics detail.** `*` matches within one segment, `**/` additionally matches zero directories — a documented subset of full glob (no `**` mid-segment sparing nuances, brace expansion, or `a/**/b` directory-spanning forms); sufficient for the S1 fixture and test-pinned.
3. The initial report write was truncated server-side mid-§7 (recovered by appending the remainder; no content lost — the cut was at the report text only, tests/code were already green).

## 13. Out-of-scope findings
- **CONFLICT (sibling collision, not a product defect):** `tests/conventions/test_docs_references.py::test_every_referenced_line_is_not_blank` fails on `docs/modules/validation.md:46` → `src/paa/validation/patch.py:786` blank line. Attribution: `src/paa/validation/patch.py` has uncommitted sibling edits (W1-IMPL-04, N1.2) that shifted lines. Posted to COMMS as CONFLICT within minute of the failure. Owner: W1-IMPL-04 (fix the citation) or ORCH (coordinate at the wave boundary). My diff adds no `path:line` references, so my deliverable is clean under the same lock.

## 14. Known limitations, risks, rollback notes
- **Best-effort TOCTOU** (same as ADR-0045): a concurrent symlink swap between jail check and open is not closed at this layer; the opened path is the jail-returned canonical path, so the swap window is only the resolve→open gap and needs a hostile process with workspace write access. PatchApplier's realpath is the write-side second line; these are read tools.
- **ReDoS residual** (§10): backtracking `re` on model patterns; bounded work per file, no per-pattern timeout. Documented; a timeout/sleep band-aid would be worse.
- **Isolation floor:** on the subprocess backend there is no network/mount containment beyond the jail; the jail is the boundary these tools promise. (No sandbox is used — correct for reads.)
- **Rollback:** two-way — `git revert a74f5f0` removes both files; no schema, ledger, config, or CLI change exists to migrate. `FS_READ_TOOLS` will be referenced by N1.5's registration; reverting after N1.5 merges breaks that import (fail-closed, same as ADR-0045's rollback note).

## 15. Where I am least confident
The least confident area is **glob-pattern edge semantics beyond the tested set** (character-class corner cases such as `[!]`-alone, and patterns like `a/**` where `**` is the final segment): the translation handles `**/`, `**`, `*`, `?`, and simple `[...]`/`[!...]` classes, with unterminated classes treated literally, but exotic combinations (e.g. `[]]` as a class, `a**b`) follow a reasonable-but-unpinned reading. None of these is security-bearing — worst case a pattern returns no matches — but I would extend the class-handling tests if a stricter globber migration (e.g. `wcmatch`) is ever adopted.

## 16. Self-review checklist
- [x] Re-read the full diff fresh (not from memory): frozen jail-then-protect order on every disk-touching path; no string-prefix containment; no raw arg reaches the filesystem.
- [x] No band-aid: no blanket try/catch (each except is narrow: `WorkspaceViolation`→verbatim reason, `OSError`→named error value, `UnicodeDecodeError`→frozen refusal, `re.error`→error value), no sleep/retry/timeout, no `Any`-casts or suppressions (ruff/mypy pass clean; the `_jail_then_protect(raw: Any)` annotation is honest input typing, not a cast).
- [x] `is_protected` in glob/grep is called on jail-returned paths only; the walk never descends symlinked dirs; grep's pre-jail calls are stats only, reads never happen pre-verdict.
- [x] Truncation always marked (`truncated` + `note`), never silent; output size bounds on every list; JSON round-trips CRLF through `\\r\\n`.
- [x] Dead code removed (`_PROTECTED_PREFIX`); descriptions updated to state default+cap accurately.
- [x] `git status` clean apart from declared writes; no COMMS/validation/sibling files committed; branch `ops/20261109-next-spec` throughout; explicit `git add --` only.
- [x] Verification fully evidence-tagged; what was NOT verified is stated in §11 (full suite, containers, DB migrations — standing order scoping) and §15.

## 17. Commits
Code: `a74f5f0` (`feat(N1.1): jailed fs read tools (read_file/glob/grep) per ADR-0045`, 2 files, explicit paths `git add -- src/paa/coder/fs_read_tools.py tests/coder/test_fs_read_tools.py`). Report: this file, committed separately in a docs-only commit listing `a74f5f0` (message `docs(N1.1): W1-IMPL-03 implementation report`).
