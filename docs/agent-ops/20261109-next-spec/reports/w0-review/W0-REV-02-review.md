<!-- ORCH-DIGEST:START -->
agent: W0-REV-02 | scope: T-N0-2 rename be51bba + T-N0-5 triage c90d835 | verdict: APPROVE
findings: BLOCKER 0 | MAJOR 0 | MINOR 2 | NIT 2
claims_audit: 14 claims checked / 0 unsupported or false (1 proof-gap noted as MINOR, 1 spec-wording correction noted and agreed)
band_aids_found: none
top_3_risks: pre-rename untracked original not anchored in git history so byte-identical rests on reported hash; triage per-group skip counts still unmeasured on Linux; TASK-3/4 owners lack named wave/task slot
optimizations: DO-NOW 0 | DO-LATER 1 | NOT-WORTH-IT 1
reversal_check: done (reasoned + scoped test evidence)
confidence: high (hash + links + 4 xfails + 1 failure re-ran first-hand; Linux-only paths inferred from code, not re-run)
<!-- ORCH-DIGEST:END -->
# Code review — W0-REV-02

## 0. Method

Blind pass first, then claims audit against reports and code, per brief §9. Read-only on all source/docs/tests; no code edits. Scoped commands only, per brief §7 and standing orders §16: `git show`/`git hash-object`/`git ls-files`, `read`/`grep` of cited `path:line`s, and focused `pytest <nodeids> -q -o addopts=""` for the 4 xfails and the 1 failure. Full suite explicitly not run.

Required reading followed in order: STANDING-ORDERS, 00-mission, plan N0.2/N0.5/§M, N0-2 and N0-5 reports (after blind pass), review-code template, COMMS, diff stats. Verdict on the two claims under audit:

- **T-N0-2 (be51bba): APPROVE.** Rename is a pure path operation: 1 file created, 207 insertions, zero modifications to any other file [RAN git show be51bba --stat -> 1 file, 207 insertions]. Current working-tree SHA256 `015CFB1B...` and git blob `dbb379a1...` exactly match the agent-reported pre- and post-rename values [RAN Get-FileHash + git hash-object -> identical]. Slug path exists and `git ls-files docs/research/` lists only it [RAN git ls-files]. VISION/ROADMAP research links now resolve (see §2). No content edit detected.
- **T-N0-5 (c90d835): APPROVE.** Verdicts are correct: 1 failed = ENVIRONMENT (test-host assumption, product correct), 245 skips = ENVIRONMENT (all guards are extra/server/platform gates by design), 4 xfails = 3 REAL bugs + 1 OPERATOR-DECISION [VERIFIED xfail markers + code + RAN scoped pytest -> xxxx]. No real product regression is mislabelled as env. TASK-1..5 are issue-ready (see §2.4).

## 1. Blind-pass findings

Independent of the impl reports, before reading them against the code:

1. **Rename diff is create-only.** `git show be51bba` creates exactly one path `docs/research/2026-11-deepseek-super-agent.md`, 207 lines, mode 100644, no deletions/modifications elsewhere [RAN]. This is the expected shape for a rename of an untracked file (source was never committed, so there is no `git mv` deletion to show). Nothing in the diff edits, reformats, or normalises content.
2. **Slug and link targets align.** The created filename is exactly the slug the roadmap and vision already cite: ROADMAP line 4 cites `research/2026-11-deepseek-super-agent.md` [VERIFIED docs/ROADMAP.md:4], VISION cites `research/2026-11-deepseek-super-agent.md` at two places [VERIFIED docs/VISION.md:11, docs/VISION.md:76]. The research note itself cites `../VISION.md` and `../ROADMAP.md` from `docs/research/` [VERIFIED docs/research/2026-11-deepseek-super-agent.md:5-6], which resolves to `docs/VISION.md` / `docs/ROADMAP.md` — the locations after sibling T-N0-1 (`d22f0d4`) moved VISION into `docs/`.
3. **1-failed mechanism is test-side, not product-side.** The failing test at `tests/sandbox/test_docker_backend.py:510-522` asserts `--user == f"{os.getuid()}:{os.getgid()}"` [VERIFIED]. The backend's `_container_user` deliberately refuses uid 0 for a root-owned workspace and falls back to the image user / `10001:10001` [VERIFIED src/paa/sandbox/docker_backend.py:894-939]. A root-owned workspace therefore correctly yields a non-0 user while the test expects `0:0`. Product invariant holds; test expectation breaks on root hosts. On Windows the same test fails earlier at `os.getuid` (absent on win32) [RAN single-test repeat].
4. **All 4 xfails are `strict=True` and self-documenting.** `grep xfail tests/` returns exactly 4 hits [RAN grep], one per triage item, each with a reason naming the fix location. `strict=True` means a fixed bug with a forgotten marker fails the build — the guard is self-maintaining, so none of them can silently turn green.
5. **Skip surface matches the documented contract.** `grep` for `requires_*`/`importorskip` finds the postgres/s3/vector/graph/api/redis gates [RAN grep], each with a named missing extra or missing server (see §2.3). `tests/ledger/test_recovery.py` returns zero skip hits [RAN grep], so the chaos/recovery trio is not hidden behind skips.

## 2. Claims audit

### 2.1 T-N0-2 rename claims (W0-IMPL-02-N0-2.md)

| # | Claim | Verdict + evidence |
|---|---|---|
| 1 | Filesystem rename (not `git mv`), because source untracked | **Supported.** `git ls-files -- docs/research/` post-rename shows only the slug [RAN]. Pre-rename untracked state is consistent with `git status` showing no tracked deletion for the spaces name (only the slug exists). `git mv` on an untracked source would fail, so rename-then-add is the correct remedy. |
| 2 | Byte-identical (SHA256 `015CFB1B...` pre and post) | **Supported with MINOR proof gap (F-1).** Current tree hash and blob match the reported values exactly [RAN]. But the pre-rename file was untracked, so there is no git-object anchor for "before" — the claim rests on the agent's reported pre-hash, cross-checked against the current hash, not on an independent before/after object pair. The numbers agree; they cannot be re-derived from history. See §4 F-1. |
| 3 | Git blob `dbb379a1...` identical pre/post | **Supported (same caveat).** Current blob [RAN git hash-object -> dbb379a...] matches the report. Same proof-gap note as #2. |
| 4 | Slug path exists; `git ls-files docs/research/` shows new path; `git status` clean for that path after commit | **Supported.** [RAN git ls-files -- docs/research/ -> slug only; git status --porcelain -- docs/research/ -> empty]. |
| 5 | No content edit; no links/config touched | **Supported.** Diff is one created file, zero other paths [RAN]. The research file's own links (VISION/ROADMAP) were not altered by this commit — they already pointed where the slug lands. |
| 6 | N0.1 link test can resolve ROADMAP:4 + VISION links | **Supported.** ROADMAP:4, VISION:11, VISION:76 all name the slug [VERIFIED]. After T-N0-1 moved VISION to `docs/VISION.md`, the research note's `../VISION.md` resolves [VERIFIED docs/research/2026-11-deepseek-super-agent.md:5-6 + docs/VISION.md exists]. The N0.2 report correctly scopes this as sibling-owned and does not claim to have run the link test itself. |
| 7 | Revert = rename back + remove commit | **Supported (reasoned).** Pure path operation, no migration. |

### 2.2 T-N0-5 failure verdict (spec §2: 1 failed)

Claim: `test_user_is_the_workspace_owner_not_root` is ENVIRONMENT, product correct. **Supported.**

- Test header states no daemon is used: "nothing here starts a real container. The ``docker create`` argv is a pure function of the spec and the detected runtime" [VERIFIED tests/sandbox/test_docker_backend.py:3-9]. The spec parenthetical "(needs a Docker daemon and a non-root host user)" is therefore imprecise; triage §4.1 correctly flags this as an out-of-scope finding. Agreed — the true prerequisites are POSIX `os.getuid`/`os.getgid` and a non-root workspace owner.
- Backend rule verified: `_workspace_owner` stats the workspace [VERIFIED docker_backend.py:881-892]; `_container_user` refuses owner uid 0, logs `sandbox.docker.root_owned_workspace`, falls back [VERIFIED docker_backend.py:923-939]; never-root rationale at [VERIFIED docker_backend.py:915-921]. Sibling tests pin the fallback behaviour [VERIFIED test_docker_backend.py:525-573].
- Windows reproduction [RAN single nodeid -> FAILED AttributeError getuid at test_docker_backend.py:522; root-owned-workspace warning in captured stdout]. Linux mechanism (root owner -> fallback vs expected `0:0`) is inferred from code + the same root-owned branch firing on Windows, not from a Linux run. Triage states this honestly (§9: "Linux run itself was NOT executed... INFERRED with high confidence"). Accepted; see §9.

### 2.3 T-N0-5 skip verdicts (245)

Claim: all 245 are intentional env gates; none is a failed test in disguise. **Supported at guard level; per-group counts honestly UNKNOWN, not faked.** Spot checks:

- `requires_postgres`: fixture skips when `PAA_TEST_POSTGRES_DSN` absent [VERIFIED tests/storage/conftest.py:29,51-52 + grep hits at test_postgres_migrations.py:82, test_postgres_contract.py:113, test_entity_parity.py:165, test_entity_normalisation_p003.py:830, test_tier_migration_p009.py:469/838].
- `requires_s3`: module `importorskip("boto3")` [VERIFIED tests/storage/test_s3_live.py:118] + skip when no server configured [:124-126] + `pytestmark = pytest.mark.requires_s3` [:130].
- `requires_vector` / `requires_graph` / redis / api: `HAS_QDRANT`/`HAS_SENTENCE_TRANSFORMERS` via `find_spec` [VERIFIED test_vector.py:42-55]; kuzu `skipif(not kuzu_available())` [VERIFIED test_graph.py:36,504]; redis `importorskip("redis")` + ping/skip [VERIFIED test_queue.py:694,718-731]; api `importorskip("fastapi")` at module scope [VERIFIED tests/api/conftest.py:22].
- Platform + conditional-content gates [VERIFIED test_fetch_images.py:121-124, test_win32.py:107/123/141, test_subprocess_backend.py:157, test_workspace.py:253, §5.8 early exits with in-code reasons].

The triage does **not** present a fake 245 reconciliation: it says counts are `[UNKNOWN]` and defers to the orchestrator's full `-rs` gate. That is the correct posture and is not counted as an unsupported claim. The one directly verifiable sub-claim — `tests/ledger/test_recovery.py` has no skips — holds [RAN grep -> 0 hits].

### 2.4 The 4 xfails + TASK-1..5 filing quality

All four verified first-hand [RAN 4 nodeids -> `xxxx`]. Each reason matches code:

| Item | Verdict | Code check |
|---|---|---|
| `test_the_lockdown_air_gap_refusal_names_the_egress` (tests/agents/test_policy.py:175) | OPERATOR-DECISION, not a bug. Correct. | `checks` tuple orders `_check_permissions` before `_check_lockdown` [VERIFIED src/paa/agents/policy.py:164-171], so a LOCKDOWN plan declaring `PERM_NET_EGRESS` is refused by permissions first; same refusal, different recorded reason. Matches AGENTS.md open-decision section. TASK-5 correctly requires operator word before any reorder. |
| `test_a_created_file_contains_exactly_what_the_diff_describes` (tests/validation/test_patch.py:390) | REAL BUG. Correct. | Phantom-line mechanism (`''.split('\n') == ['']`) is real; planner lives in `UnifiedDiffValidator._plan_file` (src/paa/validation/patch.py). Ledger sha256 would be over wrong bytes, and Wave 1 `write_file` reuses the planner — genuine hostile/malformed-input-adjacent correctness bug with N1.2 affinity. TASK-2 correctly assigns it to the N1.2 owner and requires sha re-check + marker removal in the same commit. |
| `test_an_artifact_with_nothing_to_check_does_not_pass` (tests/validation/test_engine.py:218) | REAL BUG. Correct. | Empty artifact PASS contradicts the `ValidationArtifact` docstring; `CriticValidatorAdapter` caller path exists (worker output with no source_files/patch/schema). A gate that checks nothing passes things. TASK-3 correctly requires re-checking the all-skipped-but-counted case at test_engine.py:205-215. |
| `test_deeply_nested_source_does_not_escape_as_a_recursion_error` (tests/validation/test_ast_scanner.py:449) | REAL BUG (hostile input / DoS). Correct. | `ast.parse` is guarded by `RecursionError` [VERIFIED ast_scanner.py:364-392] but `visitor.visit(tree)` at :396 sits **outside** the try — the walk can exhaust the stack, so the scanner raises instead of returning a verdict. A scanner with no verdict is an absent gate on hostile source. TASK-4 correctly suggests considering a depth-bounded walk rather than relying on the interpreter limit. |

**Filing quality (issue-ready?):** TASK-1 through TASK-5 each carry title, evidence (`path:line`), root cause, fix location, and acceptance criteria (pass condition + marker removal in the same commit). That is issue-ready. Two nits (see §4): TASK-3/TASK-4 owners are named by subsystem ("validation/engine owner", "validation owner") rather than by wave/task, leaving scheduling to the orchestrator; and TASK-1's alternative (b) monkeypatch pattern is already proven by the sibling test at test_docker_backend.py:542-544, so the preferred fix is low-risk. Neither nit blocks acceptance.

**Claims checked: 14 / unsupported or false: 0.** (1 proof-gap MINOR + 2 NITs don't rise to "unsupported".)

## 3. Long-run test (independent answers)

- **Source:** Rename preserves source bytes exactly (hash-identical), so no information loss and no provenance invented for the research content. Triage TASK-2/3/4 have clear source files (`validation/patch.py`, `validation/engine.py`, `validation/ast_scanner.py`) with single fix sites, not diffuse workarounds.
- **Load (100x, concurrency, partial failure):** Rename has no runtime surface. Triage's TASK-4 is itself a partial-failure surface (hostile input -> no verdict). TASK-1's fix (monkeypatch `_workspace_owner` or skip on no-getuid/root) holds at any host load. Skip counts at 100x are still "missing extra/server", not load-dependent.
- **Hostile input:** TASK-2 (empty prior file), TASK-3 (empty artifact), TASK-4 (2000-deep chain) are exactly the hostile/malformed family. Each test uses adversarial input and must fail without the fix — today proven by strict `xxxx`; once markers are removed, the same tests become the regression pins. No hostile-input bug is dismissed as env (patch newline/empty PASS/AST walk are all labelled REAL, matching N1.2 affinity).
- **Operability (observe, roll back, migrate):** Rename reverts by rename-back; triage is a docs-only commit (`git revert`); TASK fixes revert by restoring the strict xfail. No migration. Observability: strict xfails turn red the day a fix lands without marker removal — self-maintaining.
- **Reversal (which test fails if the fix is reverted?):** Revert the rename -> `git ls-files docs/research/` empties and ROADMAP:4/VISION:11,76 dangle (N0.1 link test fails). Revert a TASK-2/3/4 src fix -> the un-xfailed test fails. Revert TASK-5's decision -> the xfail returns (`xxxx`). Revert TASK-1 -> failure returns on Windows/root hosts only. All reversals are covered by an existing test, not by prose.

## 4. Findings

### MINOR F-1 — T-N0-2 "byte-identical" has no git-history anchor for the pre-rename state (proof gap, not a defect)

The source file was untracked, so `be51bba`'s parent has no object for it. The before/after equality rests on the agent-reported pre-rename SHA256/blob compared against the current tree value. The values agree now [RAN], but a future verifier cannot re-derive "before" from history. Mitigation if desired: none required in Wave 0 — the two independent hash families (SHA256 file hash + git blob) agreeing is strong. Noting so the orchestrator doesn't over-read "proven byte-identical" as "history-proven".

### MINOR F-2 — Triage per-group skip counts (245 split) remain unmeasured on Linux

Triage correctly marks the split `[UNKNOWN]` and defers to the orchestrator's full `-rs` gate. Guard membership is verified; the numbers are not. This is honest, not misleading, but Wave 1's green gate should measure them rather than quote the spec baseline. No action for the triage author.

### NIT N-1 — TASK-3/TASK-4 owners are subsystem labels, not wave/task slots

"A validation/engine owner (pre-Wave-1 hygiene)" and "validation owner" don't name a task the orchestrator can schedule. Recommend mapping TASK-3 to a hygiene slot and TASK-4 alongside TASK-2's N1.2 affinity window (both touch `validation/`). Content is issue-ready; scheduling is the gap.

### NIT N-2 — TASK-1 cites `geteuid()` in the sketch

`os.geteuid()` does not exist (nor does the intent need it); the Windows guard is `hasattr(os, "getuid")`. The preferred option (b) — monkeypatch `_workspace_owner` to a fixed non-zero uid/gid per the sibling pattern at test_docker_backend.py:542-544 — avoids the issue entirely and is the first choice in the task text. No change needed; implementer should follow option (b).

## 5. Attempts to break it

Red-team attempts, each checked against code:

- **Hidden real bug among skips?** Grepped all `requires_*`/`importorskip`/`skipif` groups (§2.3). Every guard fires at setup/collection on a missing extra, missing server/DSN, or non-applicable platform — none converts a runtime assertion failure into a skip. `tests/ledger/test_recovery.py` is skip-free [RAN grep -> 0]. No skip masks a bug.
- **1-failed actually a product bug?** Re-read `_container_user` end-to-end [VERIFIED docker_backend.py:894-939]. The never-root rule is structural and separately pinned. On a root host the backend's fallback is the correct answer; the test's expectation is the defect. Not a product bug.
- **TASK-2 "cosmetic newline"?** Checked the mechanism: planner output feeds both the applied bytes and the ledger's `compute_patch_sha256`, and N1.2 reuses the planner for `write_file`. A wrong-bytes record with downstream reuse is real, not cosmetic. Triage's REAL label holds.
- **TASK-3 "theoretical"?** The caller path (`CriticValidatorAdapter.validate` over worker output lacking source_files/patch/schema) is cited in the xfail reason at test_engine.py:221-230. A gate returning PASS over nothing is a false-pass, the worst failure mode for a validator. REAL holds.
- **TASK-4 "just an error"?** `scan` promises findings, never a raise on hostile source ("I could not analyse this" must mean "this does not run", per the test docstring). `visitor.visit(tree)` outside the guard [VERIFIED ast_scanner.py:394-398] means deepest-input escapes as `RecursionError` — an absent verdict, not a rejection. REAL holds.
- **TASK-5 "should have been fixed"?** Reordering `_check_lockdown` ahead of `_check_permissions` changes which refusal the ledger records for that plan class. Either plan class is refused either way. That's a policy-meaning decision owned by the operator and recorded in AGENTS.md. Triage correctly refuses to fix it silently.
- **Rename "silently edited"?** Diff is create-only for the slug [RAN]; file hashes agree across the two families; no other path in the commit. Nothing to find. The only angle — a pre-history anchor — is F-1 above.

## 6. Dimension checklist

- **Correctness:** rename path-only; triage labels match code mechanisms; all 4 xfails + 1 failure re-ran (`xxxx` + Windows FAILED) [RAN].
- **Error handling:** TASK-4 fix correctly returns `unparseable_source` CRITICAL rather than raising; TASK-1 fix keeps backend logic untouched.
- **Security:** TASK-4 is a hostile-input DoS escape; fixing it restores the gate on hostile source. No new attack surface in rename (path quoting was the only hostile-ish trait; the slug removes it).
- **Compatibility:** no API/schema change in either commit. Rename is downstream-compatible with N0.1 links; triage is report-only.
- **Tests:** triage tests are pre-existing strict pins — each `xxxx` today, each trips red on fix-without-marker-removal. Fail-before/pass-after logic holds by construction of `strict=True`. No test was weakened or deleted by either commit.
- **Maintainability:** TASKs name single fix sites (`_plan_file`, `DeterministicValidationEngine.validate`, scanner guard, `policy.py:164` tuple). No band-aids (no swallow-error, no blanket skip, no special-case).
- **Observability:** triage report is the record; strict markers self-shout on drift; skip groups each need `--rs` re-measurement (F-2).

## 7. Optimization and simplification opportunities

- **DO-NOW 0.** Nothing under review is inefficient, duplicated, or over-complex at a level worth changing in Wave 0.
- **DO-LATER 1.** Measure the 245-skip split with `pytest -rs` on the Linux baseline host and record it in the Wave 1 gate (closes F-2 empirically). Optionally implement TASK-4 as a depth-bounded walk instead of relying on the interpreter recursion limit, as the task already flags.
- **NOT-WORTH-IT 1.** No value in adding a second hash anchor (e.g. committing the untracked original first) to re-prove a rename that already agrees across two hash families. F-1 is a note, not a re-do.

## 8. Cross-agent interactions

- **W0-REV-01 (N0.1):** No overlap. N0.2's slug is the link target N0.1's lock resolves; N0.2 relies on N0.1 to move VISION, N0.1 relies on N0.2 for the slug. After `d22f0d4` (VISION in `docs/`) + `be51bba` (slug committed), both directions resolve: ROADMAP:4/VISION:11/VISION:76 -> slug [VERIFIED], and research:5-6 `../VISION.md`/`../ROADMAP.md` -> `docs/VISION.md`/`docs/ROADMAP.md` [VERIFIED].
- **T-N0-5 -> Wave 1:** TASK-2 has N1.2 affinity (planner reuse) and should be scheduled with it; TASK-1 is a test-only robustness fix for the sandbox suite; TASK-5 needs an operator decision before code. None blocks Wave 1's green gate if handled as `green-modulo-env` with F-2 measured.
- **COMMS:** W0-IMPL-02's CONTRACT and W0-IMPL-05's FINDING lines match their reports (evidence links resolve to the report files). No contradiction with this review.

## 9. What I could not verify

- **Pre-rename file content from history:** the source was untracked and no longer exists; "byte-identical" is verified by agreement of reported pre-hash with current hash/blob, not by a git-object pair (F-1). Stated plainly, not papered over.
- **Exact per-group split of the 245 Linux skips:** guard membership verified by inspection; counts NOT-RUN (full suite forbidden by brief). Triage marks this UNKNOWN; so do I.
- **Linux execution of the 1 failed test:** verified by code mechanism + Windows reproduction of the same root-owned branch, not by a Linux run. The Windows failure mode (no `getuid`) differs from the Linux one (root owner -> fallback), and both are env. Direct Linux evidence remains with the orchestrator's gate.
- **Non-ASCII / locale behaviour of the rename on case-insensitive filesystems:** directory listing shows a single file and no collision; not tested beyond that.

## Verdict

**APPROVE.** Both claims survive audit. The rename is path-only and hash-consistent with all citing links resolvable; the triage correctly separates env from real, files five issue-ready tasks, and mislabels nothing as env. The 2 MINORs are proof/scheduling notes, not defects; the 2 NITs are drafting details for the implementers. No BLOCKER, no MAJOR, no band-aids, no hidden real bug found among the skips or the failure.

*Commands run (scoped): `git show --stat/--numstat be51bba, c90d835`; `Get-FileHash` + `git hash-object` + `git ls-files` on docs/research/; `git log --oneline b8570a6..HEAD`; `read` of ROADMAP/VISION/research + cited source/test files; `grep` for xfail/requires/skip sites; `pytest <4 xfail nodeids> -q -o addopts="-q --strict-markers --strict-config"` -> `xxxx`; `pytest tests/sandbox/test_docker_backend.py::test_user_is_the_workspace_owner_not_root -q -o addopts=""` -> FAILED (AttributeError getuid, Windows). Full suite NOT run.*
