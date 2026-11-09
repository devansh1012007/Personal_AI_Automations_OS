# FINAL-REV-01 — Final full-diff review (Waves 0–3)

## ORCH-DIGEST

- Results: `ruff 0`, `mypy 3 (pre-existing subprocess_backend.py:574, Windows stubs)`, `conventions 34/34`, targeted coder/ledger/sandbox suites green, dry-run re-run all checks held, fixture red pre-run reproduced.
- Verdict: **APPROVE-WITH-CHANGES** — no BLOCKER/MAJOR; 2 MINOR (stale `remember_fact` comment, untracked spec/uv.lock housekeeping) + NITs. No invented evidence: real-model rung honestly NOT RUN.

## Verdict: APPROVE-WITH-CHANGES

Summary for ORCH: complete diff `b8570a6..HEAD` (58 files, +15,776/−92 across src/tests/scripts/examples/docs/operations) matches plan Waves 0–3 acceptance criteria per task and cross-task. All 10 audit axes checked below with file:line evidence. The only items requiring a change are a 2-line stale comment and untracked-file housekeeping; neither blocks the merge.

## 1. Uncommitted changes in scope — CLEAN

`git status --porcelain=v1 -- src/ tests/ scripts/ examples/ docs/operations/` → empty (no modified/untracked files in scope) [RAN shell → empty]. Overfull status shows only run-trail untracked files (`docs/agent-ops/.../briefs/*`, `gates/*`, `docs/plans/2026-11-08-*.md`), modified `COMMS.md`, and untracked `docs/2026-11-09-next-spec.md` (the spec itself), `docs/New shortcut.lnk`, `uv.lock` — see MINOR-2.

## 2. Stall/artifacts — NONE

Diff grep for `TODO|FIXME|XXX|HACK|print(|noqa:|type: ignore|cast(`: prints only in `scripts/drill_kill_resume.py`/`scripts/status.py` CLI output (legitimate); 3 `noqa: S603` on fixed-argv `subprocess.run` in `git_tools.py`/`drill_kill_resume.py` (argv literal, no shell/caller input — narrow per-line, not a per-file widening); 1 `noqa: S311` seeded RNG in drill script; `type: ignore[no-any-return]` on `json.loads` return + `cast(Any, rt)` in `tests/coder/test_runtime_tools.py` (pre-existing test pattern). No commented-out code, no debug prints in `src/`, no TODO band-aids [VERIFIED via diff grep].

## 3. Public contract changes beyond ADR-0045/0046 freezes — NONE

- `src/paa/core/types.py:230-256`: exactly 8 new `EventType` values added (`CODER_SESSION_STARTED`, `TOOL_CALL_PROPOSED/DENIED/COMPLETED/FAILED`, `CODER_SESSION_ENDED`, `MANDATE_GRANTED/REFUSED`). `TERMINAL_EVENTS` diff: only the new comment mentioning it — **no entry added**; the 8 are documented as never-terminal.
- `src/paa/ledger/replay.py`: all 8 mapped to `_NO_PHASE_CHANGE` (ledger/replay.py:402-425) and to a `pass` arm in `apply_event` (replay.py:285-296) — phase-neutral, tier-fold excluded. No existing event's semantics changed (diff shows pure additions + comment).
- `src/paa/ledger/store.py:368-388`: `scan(..., descending=False)` — default preserves ascending order; only call site needing newest-first (resume scan) opts in. Backward-compatible.
- `src/paa/coder/workspace.py:55-61` `__all__` is exactly the ADR-0045 frozen surface (`Workspace`, `WorkspaceViolation`, `assert_inside_workspace`, `is_protected`, `resolve_workspace`).
- `canonical_json` (`ledger/events.py:40`) byte-identical to base (diff touches only the appended blob section, `:256+`); sorted-keys/no-whitespace hash chain untouched [VERIFIED git diff events.py].

## 4. Security — PASS

**Secret redaction (journal preview shape-only):** `src/paa/coder/journal.py:165-196` — `args_preview_redacted` renders strings via `_render_preview_value` (`journal.py:207-231`) as `<str len=N sha256=xxxxxxxx>` (shape + 8-hex fingerprint, never content); secret-named keys (`_SECRET_KEY_HINTS`, journal.py:110-111) mask outright to `key=***`; containers recurse shape-only; preview capped at `_PREVIEW_MAX_CHARS=240` with ellipsis. `args_sha256` (journal.py:152-157) attests post-PRE-hook args only (loop builds `ToolCtx` after the hook returns — `loop.py` step 2). Secret-hygiene canary suite green (test_secret_hygiene.py in targeted run).

**Path traversal (work tools):** single jail in `src/paa/coder/workspace.py` — `resolve_workspace`/`assert_inside_workspace` canonicalise both sides (`expanduser().resolve()` after Windows pre-checks for drive-relative `^[A-Za-z]:(?![\\/])`, UNC `^(?:\\\\|//)`, NUL/control `[\x00-\x1f\x7f]`), containment via `Path.is_relative_to` (never string prefix). Protected paths (`.git` subtree, any-depth `.env*`, `PAA_HOME`) consulted **after** the jail, inside `Tool.run()`, non-overrideable. All of `fs_read_tools.py`, `fs_write_tools.py`, `shell_tools.py` (cwd=root, `shell_tools.py:349`), `git_tools.py` (fixed-argv `create_subprocess_exec`, no shell) import the one jail. `compute_persisted_intent` (journal.py:233+) hashes only the jailed resolved path and yields `None` intent on jail failure rather than raising a second gate.

**Mandate `policy_version` forge surface:** `src/paa/api/app.py:324-353` — `_mandate_from_body` ignores any client-supplied policy version and stamps `policy_version=effective_policy_version(runtime.settings.policy)` server-side; `ValueError`→422, refused mandate→409 via `PaaError`. `runtime.py::submit` then refuses a mismatched `policy_version` (stale-policy) and non-explicit basis in strict/LOCKDOWN mode, recording `MANDATE_REFUSED` as its own hash-chained lineage before raising. Client asserts only requester + basis, as designed [VERIFIED api/app.py diff + mandate.py header].

## 5. Ledger invariant — PASS

- Every tool call: PRE hook → `journal.before` (`TOOL_CALL_PROPOSED` with `args_sha256`, preview, persisted intent) → permission/ASK → execute → `after` (`COMPLETED`/`FAILED`) or `denied` (`DENIED`) — ledger event strictly before side effect; journal raise on ambiguous ids (`_validate_ids`, journal.py:127-148) aborts execution rather than executing unjournalled.
- Cancellation: `loop.py:258-310` — steps 3–5 inside one `CancelledError` arm; shielded `FAILED` journal write, then re-raise (never swallow). `subprocess_backend.py:412-446` reaps the tree on cancel and re-raises.
- No new non-replayable state: mandate, intent, and outcomes all travel in ledger payloads (`v=1` per ADR-0046); blobbing stores canonical bytes in the CAS and commits only the descriptor stub to the chain (blob `put` precedes append but is a content-addressed, idempotent derived store — an orphan blob after a crash is harmless, never a corrupt chain entry; `resolve_blobbed_payload` re-verifies digest, size, and canonical re-encode).
- `canonical_json` unchanged (see §3); blob threshold measured on canonical bytes (`events.py:297-318`).

## 6. Test integrity — PASS

`git diff b8570a6..HEAD -- tests/` removed-assertion grep: only 2 lines removed, both replaced by strictly stronger equivalents:
- `tests/agents/test_coding_specialist_p011.py`: mutating set expanded from 3 to 9 names (N1.5 deliberate pin growth, documented in the test).
- `tests/coder/test_runtime_tools.py`: membership check rebuilt against the merged catalogue (`build_work_tools` + runtime tools) — same `READ_ONLY`/`MUTATING` disjointness assertions retained and extended.
- `tests/integration/test_end_to_end.py`: event sequence **extended** with `MANDATE_GRANTED` head + new implicit-basis assertion; LOCKDOWN egress test now submits an explicit mandate to reach the policy check (stronger, not weaker).
No test deleted, skipped, xfailed, or loosened to pass.

## 7. Docs accuracy — PASS (spot-checked 5 cites + s1-report)

`tests/conventions/test_docs_references.py` re-run by reviewer: **5/5 pass** [RAN → 5 dots]. Semantic spot-checks (exist + non-blank + right content): `ledger/recovery.py:282` `MODEL_ESCALATED` in `_CODER_EVENT_TYPES`; `coder/agent.py:236` `blob_store=get_blob_store(runtime.settings.storage)`; `coder/journal.py:207` shape-only preview renderer; `api/app.py:324` `_mandate_from_body` server stamp; `ledger/replay.py:402` `_NO_PHASE_CHANGE` 8-event block — all correct.

**s1-report** (`docs/operations/2026-11-09-next-spec/s1-report.md`): 47/47 dry-run claim **reproduced by reviewer** [RAN `scripts/drill_kill_resume.py --dry-run` → `dry-run: all checks held`, 13-event ledger `v1 MANDATE_GRANTED … v13 TOOL_CALL_COMPLETED git_commit`, hash chain verifies, prompts mutating-only, pre/post hashes match]. Fixture pre-run red **reproduced** [RAN pristine copy `pytest test_calc.py` → `1 failed, 4 passed`, `test_divide_returns_fraction`]. `fix.patch` one-line `//`→`/`, applies to pristine copy and suite goes green (asserted by harness, re-observed in the dry-run output). Shell limitation honestly disclosed (pwsh absent, 0/2 bash children executed). Real-model rung declared NOT RUN with reproduction steps — honest.

## 8. Baselines (reviewer-ran)

| Check | Result |
|---|---|
| `ruff check src tests` | **0 findings** [RAN → All checks passed!] |
| `mypy src` | **3 errors, all `subprocess_backend.py:574`** (`os.killpg/getpgid/SIGKILL` attr-defined) — **pre-existing**: branch diff to that file is only the `CancelledError` arm at `:412-446`; line 574 (`_kill_tree` POSIX branch) untouched. Windows mypy stubs lack POSIX attrs; Linux CI baseline stays 0. |
| `tests/conventions` | **34/34 pass** [RAN → 34 dots] |
| Targeted: cancellation + journal + secret hygiene + mandate + coder events | **green** [RAN → exit 0] |
| Targeted: replay + coder events + blobbing | **green** [RAN → 88 dots] |
| Targeted: fs_read/fs_write/git/shell/sandbox cancellation | **green** [RAN → exit 0] |
| `tests/sandbox/` (full) | 9 failures in `test_docker_backend.py` (`test_user_is_the_workspace_owner_not_root`, `test_host_path_*`, `test_home_and_tmpdir_*`) — **pre-existing/environmental**: `git diff b8570a6..HEAD --name-only` proves the branch did not touch `docker_backend.py` or its test file (last touch `4a0f761` predates the run); failures are Windows host-path semantics, not branch code. Spec baseline recorded the same class of docker failure as environmental. |

## 9. s1-report + fixture evidence — NO INVENTION

All evidence re-run (§7). No invented counts (47/47 corrected from 50/50 by W3-REV-01 and confirmed), no invented hashes (pre/post sha256 assertions observed passing), no invented kernel/shell execution (0/2 disclosed). Real-model rung: NOT RUN, explicitly.

## 10. Cross-wave interactions — PASS

- **Recovery sweep vs journal PROPOSED:** `recovery.py:375-416` `find_interrupted_calls` closes every PROPOSED lacking DENIED/COMPLETED/FAILED as `FAILED(reason=interrupted)`; coder-only lineages take the dedicated `SKIPPED_CODER_LINEAGE` branch (`recovery.py:677-693`) without requeue or workspace rollback; mixed lineages close coder calls then follow the task path.
- **MODEL_ESCALATED exemption (Wave 2c delta):** present in `_CODER_EVENT_TYPES` at `recovery.py:272-283` with rationale comment, fixing W2-REV-04 MAJOR-1.
- **Prod blob-store injection:** `coder/agent.py:230-237` builds `LedgerToolJournal(..., blob_store=get_blob_store(runtime.settings.storage))` by default (W2-IMPR-06 fix of W2-REV-05 MAJOR-1); oversized journal rows resolve via the configured cold lake instead of failing the tool call.

## Findings

- **MINOR-1** `src/paa/coder/agent.py:71-80` — `MUTATING_RUNTIME_TOOLS` comment still says `remember_fact` writes `hot_serving_*` tables directly; Wave 2 memory writes go via the cold-lake signal path. Behavior is correct (planner matrix test pins it); s1-report already discloses this drift. Fix: 2-line comment edit, ORCH-owned.
- **MINOR-2** Untracked run files outside the commit scope: `docs/2026-11-09-next-spec.md` (the spec itself), `uv.lock`, a `.lnk` shortcut, and the agent-ops briefs/gates set. Decide whether the spec + `uv.lock` belong in the branch or stay untracked; do not let `uv.lock` drift silently either way.
- **NIT-1** `tests/coder/test_runtime_tools.py` — `return json.loads(result)  # type: ignore[no-any-return]` and `cast(Any, rt)`: pre-existing test style, not weakened checks. No change needed.
- **NIT-2** `subprocess_backend.py:574` mypy attrs stay red on Windows even after the branch; on the Linux CI gate they pass. Optionally gate the sandbox test job on Linux or stub the POSIX attrs — ORCH/CI decision, not a code fix here.

## Remaining risks

- Windows host cannot exercise real shell execution (pwsh absent) or Docker host-path semantics; both are disclosed and covered on the Linux rung/CI.
- `MODEL_ESCALATED` exemption assumes the router only escalates on coder lineages; a mixed task lineage carrying both remains correctly non-exempt via the subset test.
- Blob orphan rows after a crash between CAS `put` and ledger append are harmless but unbounded in principle; no compaction is implemented (out of scope).

*Reviewer ran every command cited above on this worktree/branch; nothing is claimed from another agent's report without re-execution.*
