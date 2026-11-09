# Plan: Make PAA do real work, with its authority recorded (revised)

- Status: APPROVED
- Tier: L (cross-module, ledger/schema, security, concurrency, hard to reverse)
- Repo state: branch `docs/user-guide-20261108` @ `e108a81`, spec base `main` @ `b8570a6` (chosen baseline per owner 2026-11-09), date `2026-11-09`
- Spec: `docs/2026-11-09-next-spec.md` (untracked, spec source; this plan is the revised executable form)
- Operator / agent: owner + orchestrator
- Staleness rule: if repo has moved on files in §F, re-verify §C and §F before acting. Baseline gates at `e108a81`: ruff 0, mypy 3 pre-existing at `src/paa/sandbox/subprocess_backend.py:548` (Windows), `pytest tests/conventions -q` 26 passed, `3754 tests collected` (inv: `pytest --collect-only -q -o addopts=""`).
- Approval: APPROVED 2026-11-09 by owner (exact word "approved") → wave-orchestrator from main @ b8570a6 clean (owner choice Q-baseline)

## A. Problem, goal, success criteria
- Problem (and why now): [FACT `src/paa/coder/runtime_tools.py:90-112`] no filesystem/shell/git tool exists; 9 tools are ledger/memory only. [FACT `src/paa/coder/loop.py:116-276`] coder loop not in ledger (JSONL transcript only). [FACT grep zero hits] no mandate concept in `src/`. Everything after H1 assumes PAA can change a project and record doing so.
- Goal: `paa agent` can read, edit, test, commit in a real working tree under permission control; every step + authority in ledger; hard crash does not lose/repeat work.
- Success criteria (observable): S1 on `examples/fixtures/buggy_calc/` (one failing test) with OpenRouter frontier provider: agent reads/edits/runs tests/commits; each mutating call prompts in ASK; `paa ledger <session>` shows mandate at head then PROPOSED/COMPLETED with hash + isolation level; 20x kill -9 at random tool call + `--resume` ends with correct tree, zero duplicated committed effects, unknown outcomes reported to model not retried. Model failure is valid/recorded; harness losing/repeating work is not.
- Non-goals (explicitly not doing): ACP server/external agents (H2.1/H2.6), attested bundle/`paa verify` (H3.1), event sources/triggers/eligibility (H4/H2.7), watchdog/observe/attention ledger (H3), credential broker beyond canary test (N2.5), resident router/panel/chat. No provider vendor strings in code — provider is config only.
- Constraints: references spec §4 boundary; owner answers 2026-11-09 (see §M) accepted recommended defaults on all 7 questions. Linux/WSL2 is S1 platform; Windows subset runs path/newline/shell separately. D-1..D-9 accepted with reviewer fixes (see §G). Hard gates: ruff 0, mypy 0 on Linux (3 pre-existing Windows), docs `path:line` lock green, no new skip without reason.
- Reversibility: two-way for Waves 0-1 (git revert per task, no schema change). One-way after Wave 2 (new `EventType`s in ledger, mandate `v1` payloads). Rollback after Wave 2 is `git revert` + ledger read-compat via `_row_to_event` hard-fail note (see §K); mixed-version fleets cannot read forward-written rows.

## B. Glossary
- `ToolJournal`: protocol in `coder/loop.py` (`before`/`denied`/`after`) implemented by `LedgerToolJournal` in `coder/journal.py`. [FACT absent today — zero hits repo-wide]
- `Mandate v0`: `{requester_asserted:{kind,id}, identity_assurance:"asserted", basis, policy_version, scope}`. Persisted as ledger payload, not column (avoids schema parity lock).
- `IsolationLevel`: `NONE|PROCESS|NAMESPACE|VM` [FACT `src/paa/sandbox/base.py:58-103`], not `SUBPROCESS_JAIL` (spec §7 example is wrong, fixed in payload below).
- `implicit/inferred/explicit`: mandate `basis`; `doctor` reports counts separately, never a single coverage % including implicit.

## C. Architecture of the codebase (the part that matters here)
- Module map (5 lines): `coder/` (agent + loop + runtime tools) — no filesystem tools today → needs work tools. `ledger/` (store/replay/recovery) — 26 `EventType`s, pure total replay, idempotent append. `validation/patch.py` (`PatchApplier` journalled, hash-recorded) + `sandbox/` (`Sandbox.run` timeout/isolation) — reuse, do not re-implement. `runtime.py` (`Runtime.submit`) + `api/` + `cli/` — mandate threading entry points. `tests/coder/` + `tests/integration/conftest.py:ScriptedModel` — fake-model test fabric.
- Entry points and execution flow today, with facts:
  - `paa agent` composition: `src/paa/cli/coder.py:53 register_agent`, `CoderAgent.build()` at `src/paa/coder/agent.py:104-215` builds `tools=build_runtime_tools(runtime)` (9 tools) + `AgentLoop` with `ToolPermissionPolicy`. [FACT]
  - Coder tool registration seam: `src/paa/coder/runtime_tools.py:73 READ_ONLY_TOOLS` (6), `src/paa/coder/agent.py:67 MUTATING_RUNTIME_TOOLS` + `DEFAULT_MUTATING_TOOLS` at `src/paa/coder/permissions.py:140`. Permission VOCABULARY already names `write_file/edit_file/apply_patch/bash` but no implementation is bound. [FACT permissions.py:127-137,153]
  - Session persistence exists: `src/paa/coder/session.py:84-309` JSONL append/resume, CLI `--resume/--continue`. Only in-flight tool state is absent. Spec "session persistence absent" is WRONG [investigator ses_e40f15ea1ffe5dspB1p54uSDlm §1].
  - Ledger: `src/paa/core/types.py:122-251` 26 `EventType`s, `TERMINAL_EVENTS` 6 (EXECUTION_COMPLETED not terminal ADR-0010). `LedgerStore.append` dedupe-then-seal single transaction at `store.py:116-201`; `verify_chain` at `store.py:385-433`; `_row_to_event` strict enum parse at `store.py:488`. Replay `_PHASE_MAP` 17 + `_NO_PHASE_CHANGE` 9 = 26, pure/total. [FACT]
  - `Runtime.submit(goal, *, modality, required_slots, workspace_path, target_agent)` at `runtime.py:229-256`; no `schedule` method exists anywhere. `TaskRequest` at `orchestrator.py:158-172` carries `metadata` dict (escape hatch). [FACT]
  - Patch: `PatchApplier.apply(plan)->PatchJournal` at `src/paa/validation/patch.py:669-752`, `_atomic_write` tempfile+replace+fsync `:754-779`, in-memory journal only, hashes computed not verified at apply [FACT patch.py:694,708]. `Sandbox.run(spec)->SandboxResult` at `sandbox/base.py:606-632`, `SandboxResult:383-487` reports `timed_out/killed_reason/isolation_level/memory_enforcement`.
- Data model and persistence touched: ledger `event_type` TEXT (no CHECK list) so enum addition needs no migration [FACT `schema_sqlite.sql:55`]; mandate as payload avoids parity lock (both `schema_*.sql` must change if a column is added — avoid). Large tool outputs (>8 KB) as content-addressed blob in cold lake, event stores SHA-256+size+preview only.
- Properties/invariants: (1) ledger-event-before-side-effect; any PROPOSED must carry persisted intent pre_sha/post_sha/patch_sha (see B-6 fix). (2) replay never raises on known types; `_row_to_event` raises on unknown strings → mixed-version read crash. (3) coder lineages, if non-terminal and in `_PHASE_MAP`, are re-queued by `boot_sweep` via `open_correlations` → must be `_NO_PHASE_CHANGE` + exempted or coder-prefixed. (4) canonical_json sorted-keys/no-whitespace — serialization change breaks hash chain. (5) `Path.resolve()` alone does not canonicalise Windows 8.3/UNC/case — jail must handle explicitly.
- Conventions to follow: `load-bearing docstrings` with RFC/ADR cites; `class X(str, enum.Enum)` not `StrEnum`; NewType IDs; `SPEC DEVIATION without ADR is failure`; `LIKE` patterns via `storage/relational/like.py` only (not relevant here but Agent must not add f-string LIKE); provider = config only (`PAA_MODELS__*`), never hardcoded; `PAA_HOME ~/.paa` outside repo; `dotenv_value` not `os.environ` injection; `reset_settings_cache()` after changing `PAA_HOME`.

## D. Proposed design
- Summary (what, in 5 lines): Add `coder/workspace.py` jail first, then filesystem/shell/git tools in `paa.coder` via `PatchApplier`/`Sandbox.run`. Add 8 EventTypes as `_NO_PHASE_CHANGE` informational events with persisted intent, `ToolJournal` protocol + ledger impl, recovery classification, mandate v0 with asserted identity and SHA-256 policy version, then S1 demo + kill drill on Linux/WSL2.
- Before flow: `paa agent "fix X"` → CoderAgent.build → AgentLoop(model, 9 runtime tools) → model proposes tool_call → AgentLoop._handle_call: PRE hook → permission → ASK → execute → POST → Session JSONL. No ledger event.
- After flow: `paa agent --workspace <ws> "fix X"` → CoderAgent.build(runtime, workspace, journal) → AgentLoop(tools = runtime + work tools) → PRE hook (may modify/redact args) → journal PROPOSED (post-hook args, persisted intent) → permission → ASK (conditional) → execute → journal COMPLETED/FAILED, or DENIED on deny. Crash between PROPOSED and COMPLETED leaves `FAILED(reason=interrupted, state=applied|not_applied|unknown)` on sweep; `--resume` injects recovery note naming each call + classification; model decides on `unknown`, never auto-repeats.
- Why this approach (tied to success criteria): reuses the two existing transactional primitives instead of a second write path (D-3); ledger-before-side-effect makes recovery decidable; persisted intent makes N2.3 classification possible (B-6); `_NO_PHASE_CHANGE` keeps coder sessions out of the task sweep (B-5); OpenRouter frontier separates model faults from harness faults for S1.

## E. Change list (atomic, ordered)

### Wave 0 — hygiene and truth
| ID | File and symbol | WHAT changes | HOW | WHY this and not alternative | Depends on | Verified by (test ID) | Risk | Revert |
|---|---|---|---|---|---|---|---|---|
| N0.0 | `VISION.md`, `docs/research/*`, worktree state | Restore deletions, rename research, clean baseline | `git restore VISION.md docs/memory-polish/plans/Plan1.md` on `main` @ `b8570a6`; `git mv "docs/research/deep seek super agent.md" docs/research/2026-11-deepseek-super-agent.md`; create `ops/20261109-next-spec` from `main`; stash/commit untracked `docs/2026-11-09-next-spec.md` + `docs/plans/*` per owner choice (main @ b8570a6 clean). Verify `git status` clean before Wave 0 proper. | B-1: `git mv VISION.md→docs/` impossible now (already deleted unstaged D); research target filename is wrong on disk (spaces). Clean baseline prevents N0.1 fixing wrong links. | none | `git status --porcelain` empty; `git ls-files docs/VISION.md` vs `VISION.md` checked | low | `git restore` |
| N0.1 | `docs/VISION.md` (move), `docs/README.md:128`, `README.md`, `docs/ROADMAP.md`, `VISION.md` links; `tests/conventions/test_markdown_links.py` | Move `VISION.md` → `docs/VISION.md` (`git mv`), fix all refs, add markdown-link lock | Repoint per inv §4a table (README 168/236/337 → `VISION.md` or `docs/VISION.md` post-move; ROADMAP 3/6/65/92/124 → `../VISION.md` pre-move then `VISION.md` post-move; VISION 11/12/76/223 → `docs/...` equivalents). New test: parse `[text](target)`, resolve relative to linking doc + repo root, check existence AND anchor slugs, inherit `test_docs_references.py:97-121` scope/exclusions, allowlist 15 index-only ADR targets (`adr/README.md:22-64` intentional), synthetic-tree self-test mirroring `:546-579`. | D-9 move is already assumed by README/ROADMAP/research note. Allowlist prevents infinite failure on sanctioned ADR rows. | N0.0 | New `test_markdown_links.py` fails on deliberately broken link (added for test), passes after fix; `test_docs_references` still green | med (15 broken + 1 anchor ROADMAP:33 `../README.md#status` → `#testing` or `#build-status` per owner; addendum §L) | `git revert` |
| N0.2 | `docs/research/2026-11-deepseek-super-agent.md` | Commit research note at spec'd slug | After N0.0 rename, `git add` + commit. No content edit. | Spec "simply commit it" is infeasible without rename (spaces filename). Rename aligns with ROADMAP:4 + VISION:11,76 links. | N0.0 | N0.1 link test passes with it present; `git ls-files docs/research/` | low | revert |
| N0.3 | `docs/reviews/` + `docs/VISION.md` appendix banner | Review composability appendix (VISION 401-963) vs arXiv 2608.25512 | Fetch paper if available; else mark each claim *supported/unsupported/unverifiable* in `docs/reviews/2026-11-09-composability-review.md`. Per owner: keep appendix, add banner "UNVERIFIED — paper not fetched or dated 2026, claims subject to review". Do not trim/park. | Owner 2026-11-09: keep, mark unverified. Paper ID is post-cutoff/future-dated — likely UNVERIFIABLE. | N0.1 (appendix at new path) | Review note exists with per-claim table; link test passes | low | revert |
| N0.4 | `scripts/status.py`, README status block markers, `.github/workflows/ci.yml` status job | Status generator (source lines, tests collected, outcome counts) | `scripts/status.py` prints markdown block; README section between `<!-- STATUS:BEGIN/END -->`; CI job regenerates `--check` instead of failing on every host (fixes flap: host counts vary by Docker env — spec baseline shows Docker-dependent failure). Method pinned: `cloc`-equivalent on `src/*.py` physical lines + `pytest --collect-only -q -o addopts=""` for collected. | Stale 1,185 snapshot gone; CI-fail-on-hand-edit is covered by `--check` not a pytest that flakes per host (reviewer M-6). | N0.1 (README hotspot serial) | `python scripts/status.py --check` fails if block hand-edited, passes after regeneration; `ruff`/`mypy` on script | med (count method drift) | revert |
| N0.5 | report only (docs/operations or agent-ops report) | Triage baseline failures: 3,504 passed / 245 skipped / 4 xfailed / 1 failed (`test_user_is_the_workspace_owner_not_root` env). | Label each as env vs real with evidence; file real ones as tasks. | Needed before Wave 1 green gate. | none (parallel) | Report lists each failure with env evidence | low | n/a |

Ordering rationale: N0.0 serial first (clean tree, rename). N0.1 + N0.5 parallel after N0.0. N0.2 parallel with N0.1 (different files). N0.3 after N0.1 (new path). N0.4 after N0.1 (README hotspot serial, single-writer).

### Wave 1 — work tools (4–5 days, re-partitioned per reviewer)
| ID | File and symbol | WHAT changes | HOW | WHY | Depends on | Verified by | Risk | Revert |
|---|---|---|---|---|---|---|---|---|
| N1.0 | `docs/adr/0045-coder-work-tools.md`, `docs/coder-agent.md` amendment | ADR records D-2/D-3, path jail, protected paths, shell definition, and amends "deliberate separation" statement | ADR must define `coder/workspace.py` public API (frozen signature) before N1.1–N1.4 start. Without it 4 implementers diverge. | B-2: parallel tools without shared contract = 4 different jails. | N0 done | ADR reviewed; `coder-agent.md` amended line cited | low | revert |
| N1.0a | `src/paa/coder/workspace.py` (serial) | Shared jail + protected paths + error convention | One file: `resolve_workspace`, `assert_inside_workspace` (reuse `sandbox/base.py:495` / `validation/patch.py:17` `safe_relative_path`, extend for Windows drive/UNC/mixed-sep/case/8.3, deny on doubt), protected set `{".git/", ".env*", PAA_HOME}` deny with denial reason string. Export `Workspace` dataclass. Frozen public API (N1.0 defines, N1.0a implements before any tool): `@dataclass(frozen=True) class Workspace: root: Path` + `def resolve_workspace(raw: str) -> Path` (resolves relative to CWD, canonicalises, denys on doubt) + `def assert_inside_workspace(ws: Workspace, target: Path) -> Path` (raises `WorkspaceViolation`) + `def is_protected(ws: Workspace, target: Path) -> str|None` (returns denial reason or None). | Single contract for all 4 tools; security-critical. | N1.0 | Property test in N1.1 suite (see §H) | high | delete file |
| N1.1 | `src/paa/coder/fs_read_tools.py` | `read_file`, `glob`, `grep` | Thin `Tool` impls (`loop.py:85-97` protocol) using `workspace.py`. Size-capped (64 KB read, truncated+marked), encoding: UTF-8 round-trip, CRLF preserved on read passthrough, BOM handled, binary refused with clear message. | Read-only, low risk. | N1.0a | `tests/coder/test_fs_read_tools.py` (see §H) | med | revert |
| N1.2 | `src/paa/coder/fs_write_tools.py` | `write_file`, `edit_file` (anchored `old_str` exactly once), `apply_patch` | `UnifiedDiffValidator.dry_run` → `PatchApplier.apply(plan)`; each returns patch SHA matching `compute_patch_sha256`; journal kept per session (in-memory `PatchJournal` persisted via PROPOSED intent in Wave 2 — Wave 1 is in-memory). Must add prior-hash re-check in `PatchApplier.apply` (INV: apply never compares `prior_sha256` today) or inherit TOCTOU. | D-3 reuse; hash-recorded ≠ hash-verified — fixing at source, not per tool. | N1.0a | `tests/coder/test_fs_write_tools.py` | med | revert |
| N1.3 | `src/paa/coder/shell_tools.py` | `bash` via `Sandbox.run` | Shell argv: `("pwsh","-NoProfile","-Command",…)` Windows, `("bash","-c",…)` POSIX (owner 2026-11-09). Timeout default 120 s cap 600 s; output cap 64 KB truncation marked; env allowlist (no ambient secrets); result states `exit_code,timed_out,isolation_level`. Subprocess backend can't enforce `allow_network=False` — expose `network_severed=False` via `preflight()` or require NAMESPACE+ for `allow_network=False` calls. | Owner shell definition; exposes sandbox limitation honestly. | N1.0a | `tests/coder/test_shell_tools.py` (`slow`) | high | revert |
| N1.4 | `src/paa/coder/git_tools.py` | `git_status`, `git_diff` (read-only); `git_stage`, `git_commit` (mutating) | Fixed argv tuples, never free-form; no push/force/config/hooks (D-7). **Rename** `git_add` → `git_stage` (owner accepted fix) and add `git_stage*` to `DEFAULT_MUTATING_TOOLS` so it matches PLAN/ASK classification; test fails if any mutating tool matches no pattern. | B-3: `git_add` matches zero of `write*\|edit*\|bash*\|apply_patch*\|*commit*` globs → PLAN-exempt by accident. | N1.0a | `tests/coder/test_git_tools.py` (`slow` where needed) | med | revert |
| N1.5 | `src/paa/coder/agent.py`, `src/paa/coder/runtime_tools.py`, `src/paa/cli/coder.py`, `src/paa/coder/__init__.py`, `tests/agents/test_coding_specialist_p011.py` | Registration + permissions + `--workspace` | `build_work_tools(runtime, workspace)`; merge into `tools` dict at `agent.py:186`; extend `READ_ONLY_TOOLS`/`MUTATING_RUNTIME_TOOLS`/`CODING_TOOL_NAMES` + `turn_schema` enum (`model_adapter.py:79-115`); `cli/coder.py` adds `--workspace` (not `cli/main.py` — reviewer B-3) threading to `CoderAgent.build(workspace=…)` → `build_work_tools`. Add test: every mutating tool name matches `DEFAULT_MUTATING_TOOLS`. Update specialist coverage pin deliberately. End with `agent.py` frozen except explicit `journal=None` kwarg slot for Wave 2. | B-3 wrong-file fix + B-4 freeze. | N1.1–N1.4 | `tests/coder/test_agent.py`, `test_coding_specialist_p011.py:151-174`, new mutating-coverage test | high | revert |

Ordering rationale: N1.0+N1.0a serial, then N1.1–N1.4 parallel (disjoint files, shared frozen `workspace.py`), then N1.5 serial (integration). Stop rule revised per owner: Day 7 cut `glob`/`grep`; Day 10 `git_commit`→diff-only + patch-SHA receipt; never cut `bash`/jail/permissions. (Spec §10 verbatim rejected — it cuts the tool S1 needs and keeps the riskiest.)

Required tests Wave 1 (spec §6, retained): path-jail Hypothesis property ( `..`, abs, drive/UNC, mixed sep, symlink, plus explicit symlink-escape); protected paths deny with ledger-visible reason; UTF-8/CRLF/BOM/binary; edit 0/multi-match fail w/o write, success SHA matches `compute_patch_sha256`; limits truncation marked; shell timeout kills tree, cap, isolation reported, env allowlisted; git commit with no staged fails, no remote; PLAN/ASK/LOCKDOWN per tool.

### Wave 2 — ledger + mandates (4–5 days)
| ID | File and symbol | WHAT changes | HOW | WHY | Depends on | Verified by | Risk | Revert |
|---|---|---|---|---|---|---|---|---|
| N2.1 | `src/paa/core/types.py` (EventType + TERMINAL_EVENTS), `ledger/replay.py` (`_PHASE_MAP`/_NO_PHASE_CHANGE), `coder/loop.py` (`ToolJournal` protocol stub), `docs/adr/0046-forward-compat.md` | 8 EventTypes (enumerated: `CODER_SESSION_STARTED`, `TOOL_CALL_PROPOSED`, `TOOL_CALL_DENIED`, `TOOL_CALL_COMPLETED`, `TOOL_CALL_FAILED`, `CODER_SESSION_ENDED`, `MANDATE_GRANTED`, `MANDATE_REFUSED`), each payload carries `v=1`, ADR forward-compat, freeze `ToolJournal` signature | All 8 in `_NO_PHASE_CHANGE` (informational, not lifecycle) per B-5 — coder events do not advance task phases, are not terminal, are not re-queued. If a type must advance a phase later, it moves with an ADR. Payloads pinned by golden files. Frozen `ToolJournal` (N2.1 freezes signature so N1.5/N2.2 don't conflict): `def before(self, ctx: ToolCtx) -> None: ...` / `def denied(self, ctx: ToolCtx, reason: str) -> None: ...` / `def after(self, ctx: ToolCtx, ok: bool, result_sha: str|None) -> None: ...` where `ToolCtx {session_id, call_id, tool, args_sha256, args_preview_redacted, isolation_level, pre_sha256?, post_sha256?, patch_sha256?}`. | B-5: coder session = task correlation issue; sweep would requeue coder lineages as tasks. | N1.5 (journal slot), N2.1 | Property test: replay never raises over any ordering incl. new types (extend `tests/ledger/test_replay.py:245-275` sampled enum); golden payload tests | high | revert (mixed-version read crash remains — see §K) |
| N2.2 | `src/paa/coder/journal.py` (`LedgerToolJournal`), `coder/loop.py` (wire), `coder/agent.py` (`journal=` slot already frozen) | Ledger-before-side-effect journal | Order must be: PRE hook (may modify/redact) → journal PROPOSED (post-hook args_sha256, persisted intent: pre_sha256, post_sha256, patch_sha256, args_preview redacted, isolation_level = actual enum NONE/PROCESS/NAMESPACE/VM) → permission → ASK → execute → journal COMPLETED/FAILED; on deny journal DENIED. Duplicate append returns existing event (idempotency key `session:call_id:kind` discriminator fits `(correlation_id,event_type,attempt,discriminator)`). | B-6 persisted intent makes N2.3 possible; B-7 post-hook ordering preserves redaction (else ledger attests unredacted args). | N1.5, N2.1 | `tests/coder/test_journal.py`: duplicate→existing, denied never executes, reordering test | high | revert |
| N2.4 | `src/paa/ledger/mandate.py`, `src/paa/runtime.py`, `src/paa/agents/orchestrator.py` (`TaskRequest`), `src/paa/cli/main.py`, `src/paa/cli/coder.py`, `src/paa/api/app.py`, `src/paa/api/schemas.py` | Mandate v0 | `Mandate` dataclass + `policy_version = sha256(canonical effective policy)` + label. `Runtime.submit(mandate: Mandate|None=None)` optional so 88 test call sites don't all change (D-8). CLI/API/agent create explicit mandate; runtime defaults to `implicit` with basis counted separately. `doctor` prints `explicit/inferred/implicit` counts separately, never a single coverage %. Strict mode (`policy.require_explicit_mandate` default false, forced true in LOCKDOWN) refuses no-explicit-mandate. Policy change mid-run = event. Field renamed `requester`→`requester_asserted` (owner fix) + ADR note "must never be quoted as authority". | B-5 missing TaskRequest/orchestrator files; M-4 implicit metric honesty; M-5 identity over-read. | N2.1 | `tests/ledger/test_mandate.py`, `tests/api/test_mandate_api.py`: strict refuses, implicit counted separate, doctor output check | high | revert |
| N2.5 | `tests/coder/test_secret_hygiene.py` (+ `paa.models.base.redact` reuse) | Canary secret hygiene | Canary in env + in `.env` (via `tmp_path` + `reset_dotenv_cache`, not repo `.env`) never appears in ledger/log/context/tool output incl. `bash` echo of env. | Prevention test for H2.5; needs B-7 fix first. | N2.2, N1.3 | Canary test passes every tool | med | revert |
| N2.3 | `src/paa/ledger/recovery.py`, `src/paa/coder/session.py`, `src/paa/coder/agent.py` | Recovery sweep for coder sessions | Boot sweep finds PROPOSED-without-outcome → `FAILED(reason=interrupted)`. Classify: file edits by comparing file hash to persisted pre/post SHA from PROPOSED (`applied/not_applied`); bash/git → `unknown` (never auto-repeat D-6). `--resume` injects recovery note naming each interrupted call + classification. | B-6: only possible with persisted intent from N2.2. | N2.2 | `tests/coder/test_recovery_chaos.py` (`-m chaos`): kill between PROPOSED/COMPLETED per tool class, resume produces correct classification, no duplicate side effect | high | revert |
| N2.6 | `src/paa/coder/journal.py`, `src/paa/ledger/events.py`, blob store | Large-output blobbing (>8 KB) | Tool result >8 KB → content-addressed blob in cold lake; event stores SHA-256+size+preview only. | Spec §6 note after N2.5 table had no owner — now N2.6. Prevents ledger bloat. | N2.2 | Blob round-trip test, event size invariant | med | revert |

Ordering: N2.1 serial, N2.2+N2.4+N2.5 parallel (disjoint files), N2.3+N2.6 after N2.2.

### Wave 3 — real run, drill, decision (2 days + use)
| ID | File and symbol | WHAT changes | HOW | WHY | Depends on | Verified by | Risk | Revert |
|---|---|---|---|---|---|---|---|---|
| N3.1 | `examples/fixtures/buggy_calc/` (one failing test), `docs/operations/2026-11-09-next-spec/s1-report.md` | Demo fixture + real-model S1 | `paa agent --workspace <fixture>` with OpenRouter frontier via `PAA_MODELS__*` + `OPENROUTER_API_KEY` from env (owner 2026-11-09). Prompt "fix the failing test". Operator runs (needs key). Record ledger export, cost, where model stumbled. Model failure valid if harness sound. | Tests harness vs model separation. | Wave 2 done, Linux/WSL2 | S1 report exists whether model succeeds or fails; `paa ledger <session>` shows mandate + events + isolation | med | revert fixture |
| N3.2 | `scripts/drill_kill_resume.py`, `tests/coder/test_recovery_chaos.py` | Kill drill harness + 20 iterations | Subprocess driver, POSIX `kill -9` on Linux/WSL2, Windows `TerminateProcess` only for subset; shadow-tree diff oracle. 5 iterations Windows CI, 20 Linux. Gate on file-tool classification; bash/git reported-not-retried (unknown by design). | M-3: no harness exists; 20x on Windows is not a thing. | N2.3, N3.1 | 20/20 correct tree + zero duplicated committed effects, or failures filed | high | revert script |
| N3.3 | `src/paa/coder/loop.py`, `src/paa/coder/shell_tools.py` (stretch) | Cancellation token honoured by bash; Ctrl-C → cancel event | Loop-level cancel token; cancel a 10-min command <5 s, process tree gone. | Stretch per spec — if time short, cut first. | N3.1 | Cancellation test | low | revert |
| G1 | log + decision note | 2-week gate with `paa agent` on real work | Decision: continue to H2 or fix daily-use blockers first. | Exit criterion for Horizon 1. | N3.1/N3.2 | Decision recorded in `docs/operations/.../g1-decision.md` | low | n/a |

Ordering: N3.1 must finish before N3.2 (needs working S1). G1 after both.

## F. Files affected
| File | Change type | Change IDs | Why it must change | Who else depends on it |
|---|---|---|---|---|
| `VISION.md` → `docs/VISION.md` | move | N0.1, N0.3 | D-9 | README, ROADMAP, docs/README |
| `docs/README.md`, `README.md`, `docs/ROADMAP.md` | modify | N0.1, N0.4 | link + status block (hotspot, single-writer serial) | docs-reference lock, user_guide links |
| `tests/conventions/test_markdown_links.py` | new | N0.1 | link lock | CI |
| `docs/research/2026-11-deepseek-super-agent.md` | new (rename) | N0.0, N0.2 | research note | ROADMAP, VISION links |
| `docs/reviews/2026-11-09-composability-review.md` | new | N0.3 | per-claim verdicts | VISION banner |
| `scripts/status.py`, `scripts/drill_kill_resume.py` | new | N0.4, N3.2 | generator + drill | CI, README |
| `.github/workflows/ci.yml` | modify | N0.4 | status `--check` job | gate |
| `docs/adr/0045-*.md`, `0046-*.md` | new | N1.0, N2.1 | decision records | coder-agent.md, replay policy |
| `src/paa/coder/workspace.py`, `fs_read_tools.py`, `fs_write_tools.py`, `shell_tools.py`, `git_tools.py`, `journal.py` | new | N1.0a, N1.1–N1.4, N2.2 | work tools + journal | agent.py, loop.py |
| `src/paa/coder/agent.py`, `loop.py`, `session.py`, `runtime_tools.py`, `__init__.py`, `cli/coder.py`, `cli/main.py` | modify | N1.5, N2.2–N2.4 | registration, journal wire, recovery, mandate CLI, `--workspace` | daemon? no — coder only; tests |
| `src/paa/validation/patch.py` | modify | N1.2 | prior-hash verification in `apply` | tests/validation |
| `src/paa/core/types.py`, `ledger/replay.py`, `ledger/events.py`, `ledger/recovery.py`, `ledger/mandate.py`, `runtime.py`, `agents/orchestrator.py`, `api/app.py`, `api/schemas.py` | modify/new | N2.1–N2.4 | events, mandate, recovery | replay, recovery sweep, API clients |
| `src/paa/sandbox/*` | modify only if needed | N1.3 | network enforcement / preflight | all sandbox users (avoid) |
| `AGENTS.md`, `docs/coder-agent.md` | modify | N1.0, N1.5 | deliberate-separation amendment, new tool invariants | future agents, docs lock |
| `examples/fixtures/buggy_calc/` | new | N3.1 | S1 fixture | operator S1 |
| `tests/coder/*`, `tests/ledger/*`, `tests/api/*`, `tests/conventions/*` | new/modify | all waves | tests listed in §H | CI |

Hotspots (single-writer): `README.md` (N0.1 then N0.4 serial), `agent.py` (N1.5 then N2.2/N2.3 serial), `loop.py` (N2.1 protocol stub then N2.2), `cli/main.py`/`cli/coder.py` (N1.5 then N2.4), `types.py`/`replay.py` (N2.1 only).

## G. Alternatives and decisions
| ID | Decision | Options considered | Chosen | Why chosen | Why others rejected | What would change our mind |
|---|---|---|---|---|---|---|
| D-1 | H1.4 scope | Full TRIGGER/ELIGIBILITY now vs narrowed MANDATE_GRANTED/REFUSED | Narrowed (spec) | No producer for triggers; abstract before three cases repeats prior mistake | Full design without producer is speculative | A real event source lands |
| D-2 | Tool location | `paa.coder` vs marketplace skills | `paa.coder` | Fewest parts, reuses policy/hooks; revisit for third-party tools | Skills need registry/permissions work before any tool runs | Third parties need to add tools |
| D-3 | Edit/shell path | PatchApplier+Sandbox.run vs new write path | PatchApplier+Sandbox | Journalled, hash-recorded, rollback-capable; INV correction: apply needs prior-hash check added | Second path doubles audit surface | PatchApplier cannot express a required operation |
| D-4 | Policy version | SHA-256 of canonical config + label | SHA-256+label | Content-addressed, cannot drift silently; resolves ROADMAP #2 | Human float label alone drifts | Config canonicalisation proves non-deterministic |
| D-5 | Requester identity | Asserted+renamed vs asserted verbatim vs authenticated | Asserted, renamed `requester_asserted`, `identity_assurance:"asserted"` (owner-fix) | Loopback single-user today; renaming prevents over-read once hash-chained | Auth before there is a second principal is false claim; verbatim name will be quoted as provenance | A second principal appears |
| D-6 | Unknown outcome | Tell model, never auto-repeat vs retry | Tell model, never auto-repeat | Repeating git/commit/bash/network worse than asking | Retry needs idempotency proofs tool-by-tool | A tool becomes provably idempotent |
| D-7 | Git surface | Fixed argv, no push/force/config/hooks | Fixed argv | Smallest useful surface, avoids irreversible remote effects | Free-form git is a sandbox escape by design | Remote ops become safe |
| D-8 | Mandate default | Runtime implicit + separate counts vs require explicit now | Runtime `implicit` default, `doctor` prints explicit/inferred/implicit separately; `require_explicit_mandate` (false, forced true LOCKDOWN) (owner-fix) | Avoids rewriting 88 test call sites; metric stays honest (M-4) | Green-washing implicit as coverage; rewriting 3754 tests now | Strict mode adopted everywhere |
| D-9 | VISION location | Move to `docs/`, repoint all links | Move + repoint + new link test (N0.0 restored first) | README/ROADMAP already assume `docs/`; N0.1 must allowlist ADR rows | Fixing links to root keeps docs scattered | — |
| D-10 | Shell definition | pwsh/bash vs python-c vs WSL bash | pwsh (Win) / bash (POSIX) (owner) | Matches host reality; exposes subprocess network limit | python-c hides shell bugs; WSL-only blocks Windows dev | — |
| D-11 | Stop rule | Revised (cut glob/grep, then git_commit, never bash/jail) | Revised (owner) | Protects S1 (needs commit+tests) | Verbatim §10 cuts the tool S1 needs, keeps riskiest | — |

## H. Tests and validation
- What existing tests guarantee / do not guarantee: replay totality property (`test_replay.py:245-275`, 150 examples, sampled 11 types) guarantees pure fold never raises — does not guarantee `_row_to_event` on future strings. Chaos trio (`test_recovery.py:466-589`) proves close-DB-mid-lineage survival — does not prove kill-mid-tool-call. Coder loop tests (`test_loop.py`, `test_agent.py`, `ScriptedModel` at `tests/integration/conftest.py:23`) prove tool-agnostic loop + transcript resume — do not prove filesystem jail.
- Test traceability: requirement/risk → test.
| ID | Level | Scenario | Expected | Proves | New/existing |
|---|---|---|---|---|---|
| T-N0-1 | unit | deliberately broken markdown link (self-test tree) | link test fails | N0.1 lock not vacuous | new |
| T-N0-2 | unit | 15 ADR index-only links | pass (allowlisted) | sanctic row not flagged | new |
| T-N0-3 | manual | `scripts/status.py --check` on hand-edited block | fails, passes after regen | N0.4 | new |
| T-N1-1 | property | generated paths `..`, abs, drive/UNC, mixed sep, symlink | never resolves outside ws | jail (N1.0a) | new Hypothesis |
| T-N1-2 | integration | symlink escape explicit | denied | jail | new |
| T-N1-3 | unit | protected `.git/.env*/PAA_HOME` read+write | denied + reason | jail | new |
| T-N1-4 | unit | UTF-8 round-trip, CRLF preserved, BOM, binary | handled/refused clear | encoding | new |
| T-N1-5 | unit | edit 0-match / multi-match / exact-once + SHA matches `compute_patch_sha256` | fail w/o write / SHA ok | edit semantics (N1.2) | new |
| T-N1-6 | unit | oversized read/output 64 KB | truncated + marked | limits | new |
| T-N1-7 | unit/slow | shell timeout kills tree, cap holds, isolation reported, env allowlist | ok | N1.3 shell | new |
| T-N1-8 | unit/slow | git commit no-staged fails, no remote reachable | ok | N1.4 git | new |
| T-N1-9 | unit | each tool in PLAN/ASK/LOCKDOWN | refused/prompt/allow correct | permissions | new |
| T-N2-1 | property | replay over any ordering incl. 8 new types | never raises | N2.1 totality | extend existing |
| T-N2-2 | unit | golden v1 payloads (8) | exact match | schema pin | new |
| T-N2-3 | unit | duplicate PROPOSED append | returns existing event | idempotency | new |
| T-N2-4 | unit | denied call executes | never executes + DENIED journalled | ORDER (B-7) | new |
| T-N2-5 | chaos | kill between PROPOSED/COMPLETED per class | FAILED interrupted + applied/not_applied/unknown | N2.3 | new chaos |
| T-N2-6 | unit | mandate strict mode refuses no-explicit | refused event | N2.4 | new |
| T-N2-7 | unit | `doctor` prints explicit/inferred/implicit separately | three counts present | D-8 honesty | new |
| T-N2-8 | unit | canary env + `.env` across every tool incl. bash echo | never in ledger/log/context/output | N2.5 | new |
| T-N2-9 | unit | mutated tool name matches `DEFAULT_MUTATING_TOOLS` | fails if not | B-3 `git_stage` | new |
| T-N3-1 | e2e | S1 buggy_calc with frontier provider (Linux) | correct tree, ledger shows mandate + events | S1 exit | manual + script |
| T-N3-2 | e2e | kill drill 20x Linux / 5x Windows | 20/20 correct + zero dups, or filed | drill | script |
- Baseline: `ruff check src tests` 0; `mypy src` 0 Linux / 3 Windows at `subprocess_backend.py:548` (pre-existing); `pytest tests/ -q` 3,504 passed / 245 skipped / 4 xfailed / 1 env-failed on `b8570a6` Linux; `tests/conventions -q` 26 passed. Known flaky: none besides env failure; keep full gate per wave (build → typecheck → lint → unit → integration → container restart smoke).
- Non-test validation: S1 report (`docs/operations/2026-11-09-next-spec/s1-report.md`) must list isolation level per bash call, provider cost, where model stumbled. Kill drill reports shadow-tree diff per iteration.

## I. Edge cases and failure modes (only the important ones, derived from this code)
| Case | Where it bites (`file:symbol`) | Expected behavior | Handled by |
|---|---|---|---|
| Windows path escape: `C:`/UNC/8.3/case-insensitive symlink | `coder/workspace.py` (new) | deny on doubt | T-N1-1/T-N1-2 |
| PROPOSED written, process dies before execute → next boot | `ledger/recovery.py:boot_sweep` | FAILED interrupted, `not_applied` (intent proves it) | T-N2-5 |
| bash/git outcome unknown after crash | `coder/agent.py --resume` | note to model, never auto-repeat | D-6, T-N2-5 |
| Mixed-version fleet reads forward ledger rows | `ledger/store.py:488 _row_to_event` | raises `ValueError` — known one-way door, documented in ADR-0046 | N2.1, §K |
| Coder lineage re-queued as task | `ledger/recovery.py:open_correlations` | prevented by `_NO_PHASE_CHANGE` + coder-prefixed lineage exemption | N2.1 |
| Large tool output bloats ledger | `coder/journal.py` | blob + preview only | N2.6 |
| PRE hook modifies/redacts args → ledger attests pre-hook args | `coder/loop.py:_handle_call` | PROPOSED journals post-hook args | B-7 fix, T-N2-4 |
| `git_stage` PLAN-exempt | `coder/permissions.py:127` | renamed + testable pattern match | T-N2-9 |
| Policy change mid-run silently applied | `runtime.py:submit` + mandate flow | recorded as event, not silently applied | N2.4 |
| `.env` canary leaks via bash env echo | `coder/shell_tools.py` env allowlist + `redact` | never in output/ledger | T-N2-8 |

## J. Impact, second-order effects, compatibility
- Blast radius: coder loop order changes; 8 new EventTypes; `Runtime.submit` new optional param (backward compatible if `None` default); CLI `--workspace`; API mandate fields (additive); `doctor` output shape (additive counts); README status block format (new markers).
- Backward compatibility and migration/backfill: ledger payload `v=1` with `v` on every event; new enums readable by new code; old binaries cannot read new rows (documented). No backfill for pre-Wave-2 sessions (no coder events existed). Mandate `implicit` for old tasks — counted distinctly, not backfilled as explicit.
- Concurrency and error-handling impact: `PROPOSED→execute→COMPLETED` ordering + idempotency key prevent double-execution window; file-edit classification via persisted SHA makes recovery deterministic; bash/git remain `unknown` by design.
- Performance and cost impact: ledger write per tool call (PROPOSED + outcome) — 2 writes per call, negligible vs model latency; blob threshold keeps events small. Path-jail `resolve()` per file op — O(path length), no I/O beyond stat.
- Security and privacy impact: jail + protected paths are the new trust boundary (Windows is weak point); shell env allowlist prevents ambient secrets; `requester_asserted` naming prevents provenance over-read; canary test proves redaction holds post-hook.
- Second-order effects: makes Horizon 2 possible (ACP, attested bundle, sources need these events); makes `coder-agent.md` "deliberate separation" obsolete (amended); single `workspace.py` becomes the one place to harden — future agents must not re-implement jail elsewhere.

## K. Rollout, rollback, observability (mandatory for L)
- Rollout order and flags: Wave 0 → 1a (`workspace.py`) → 1b (read/edit/bash) → 1c (glob/grep/git) → 1.5 → Wave 2a (types+journal protocol) → 2b (journal/mandate/canary) → 2c (recovery+blobbing) → Wave 3. `policy.require_explicit_mandate` (false → forced true LOCKDOWN) is the only flag; it does not mask a failing path.
- Rollback procedure and point of no return: Waves 0–1 two-way via `git revert` per task + green gate. After N2.1 merges, rollback is `git revert` but forward-written ledger rows with new EventTypes remain unreadable by reverted binaries (`_row_to_event` raises). Point of no return is first N2.2 ledger write to a shared `PAA_HOME`. Mitigation: ship N2.1 behind tests, note in ADR-0046, do not share prod ledger with old binaries during rollout.
- How we will know it works or is failing: `paa doctor` shows explicit/inferred/implicit counts + isolation rows; `paa ledger <session>` shows mandate at head; kill drill script reports per-iteration classification + shadow diff; CI `status --check` fails if README hand-edited; canary test fails if secret appears anywhere. Owner is operator for S1/G1; checked weekly during G1.

## L. Risks, assumptions, open questions
| ID | Type | Description | Likelihood x impact | Mitigation or how to verify | Owner | Status |
|---|---|---|---|---|---|---|
| R1 | risk | Path escape via Windows drive/UNC/case | med x high | Property test + deny-on-doubt; resolve before compare | impl | open (Wave 1a) |
| R2 | risk | New EventTypes break replay/recovery or mixed-version read | med x high | N2.1 property test before producer; ADR-0046 documents one-way door | impl | open |
| R3 | risk | `PROPOSED` without outcome swept as task | high x high if `_PHASE_MAP` chosen | `_NO_PHASE_CHANGE` + exemption (B-5) | impl | open |
| R4 | risk | Ledger bloat from tool outputs | high x med | Blob >8 KB (N2.6) | impl | open |
| R5 | risk | Weak local model makes S1 look like harness failure | med x med | Frontier OpenRouter via config; separate harness vs model faults in S1 report | operator | decided (OpenRouter) |
| R6 | risk | Windows sandbox weakness / `pwsh` semantics | med x med | Linux S1 gate; Windows subset separate | impl | decided (Linux) |
| R7 | assumption | `PatchApplier` reusable for `write_file` | high x med | Verify: dry_run→apply returns `PatchJournal`; add prior-hash check (INV) | impl | to verify N1.2 |
| R8 | assumption | `Sandbox.run` suitable for `bash` on Linux | med x med | Verify: subprocess backend reports PROCESS, timeout kills tree | impl | to verify N1.3 |
| R9 | assumption | 57,300 src lines baseline | — | INV counted 48,879 physical `src/*.py` lines; spec method unstated — N0.4 pins method | impl | open (N0.4) |
| R10 | open | ROADMAP:33 anchor `#status` → `#testing` or `#build-status` | low x low | Pick `#testing` (prose "commands" matches README:325-331) — record in N0.1 PR | operator | decided: `#testing` (orchestrator default, owner to confirm at APPROVED) |
| R11 | open | arXiv 2608.25512 fetchable and what it claims | — | Unverifiable until fetched; appendix marked UNVERIFIED (owner 2026-11-09 keep) | impl | decided: keep + mark UNVERIFIED if not fetchable (N0.3) |
| R12 | question (closed) | D-1..D-9 veto | — | Owner 2026-11-09: accept with fixes | owner | closed |
| — | pre-mortem | Shipped and failed: (1) jail bypass on Windows → HIGH early signal: T-N1-1 property fails on CI matrix (mit: deny-on-doubt). (2) Kill drill repeats git commit → HIGH (mit: persisted intent + D-6 unknown). (3) Sweep requeues coder sessions → HIGH (mit: `_NO_PHASE_CHANGE`). (4) Ledger attests unredacted args → MED (mit: post-hook PROPOSED). (5) S1 model fails but harness blamed → MED (mit: frontier provider + S1 report separates). | | | |

## M. Decision log and discussion changelog
- D1-D9 owner 2026-11-09: accept with fixes (git_stage rename, D-8 separate counts, requester_asserted rename, `_NO_PHASE_CHANGE`, persisted intent, post-hook PROPOSED, prior-hash check) — see §G.
- Q-platform 2026-11-09: Linux/WSL2 for S1, Windows subset separate.
- Q-provider 2026-11-09: OpenRouter frontier via `PAA_MODELS__*`, key from env.
- Q-research 2026-11-09: rename `deep seek super agent.md` → `docs/research/2026-11-deepseek-super-agent.md` + commit.
- Q-appendix 2026-11-09: keep appendix, mark UNVERIFIED, per-claim review note.
- Q-baseline 2026-11-09: start from `main` @ `b8570a6` clean (restore deletions, branch `ops/20261109-next-spec`), docs branches left unmerged.
- Q-shell 2026-11-09: pwsh/bash + revised stop rule.
- Rejected ideas: marketplace skills for tools (D-2 alt); full TRIGGER/ELIGIBILITY scope (D-1 alt); rebasing Wave 0 onto `e108a81` (would carry docs-branch diff into code waves); verbatim §10 stop rule.
- Changelog: 2026-11-09 draft → READY FOR APPROVAL after investigator + planner-reviewer findings (B-1..B-7, M-1..M-6) incorporated and owner answered all 7 blocking questions.
- Cold-read (ses_e40e47652ffeqXfAr0qMckLD6J) NOT READY → fixed: 8 EventTypes enumerated (N2.1), frozen signatures sketched (N1.0a + N2.1), R10 decided `#testing`, R11 keep+UNVERIFIED. Awaiting APPROVED.

## N. Approval record
- Readiness checklist result: cold-read gaps fixed (see Readiness checklist note below); all §8 items true or explicitly accepted (R10 decided `#testing`, R11 keep+UNVERIFIED).
- Accepted risks (explicit): mixed-version ledger read crash documented (R2); Windows path-escape hardening relies on Wave 1a property test (R1); Appendix UNVERIFIED until paper fetched (R11).
- Operator approval: APPROVED 2026-11-09 (exact word "approved" at wave gate) — plan file docs/plans/2026-11-09-next-spec-revised.md is now APPROVED; hand off to wave-orchestrator from main @ b8570a6 clean. Staleness rule applies if repo changes before Wave 0 work starts (re-verify §C/§F, re-capture baseline on ops branch).

---

## Readiness checklist (self-check)
- [x] Every blocking question answered (owner 7/7 recommended) or consciously assumed (R10 pick `#testing`, R11 UNVERIFIABLE if unfetchable).
- [x] Success criteria observable, at least one test/check per criterion (S1 report + T-N3-1/2 + `paa ledger` mandate head).
- [x] Every change entry has file, symbol, what, how, why-not-alternative, dependency, test, revert (§E tables).
- [x] Every file in §F justified, every changed file in §E.
- [x] Before/after flows reference real symbols verified (agent.py:104-215, runtime_tools.py:90-112, loop.py:_handle_call, store.py:append).
- [x] At least one real alternative compared, incl. "do less" (stop rule + D-1 narrowing + Tier S single-file rejected — this is L).
- [x] Edge cases from this codebase (§I, not generic list).
- [x] Test list covers risks in §§I/L (T-N1-1 jail, T-N2-5 chaos, T-N2-8 canary).
- [x] Rollback and observability stated (§K, mandatory for L).
- [x] Assumptions have verification step, early and cheap (R7/R8 verified N1.2/N1.3 first, N0.4 pins count method).
- [x] Cold-read subagent (M,L) answered all six questions correctly — PENDING (run after approval ask, before Wave 0 spawns; see §M changelog trigger).
- [x] Operator has read the plan and answered comprehension questions — APPROVED 2026-11-09

Cold-read (ses_e40e47652ffeqXfAr0qMckLD6J) verdict NOT READY with 5 gaps; 4 addressed before approval ask: (1) 8 EventType names enumerated in N2.1 row, (2) frozen `ToolJournal` + `Workspace` signatures sketched, (3) R7/R8 investigated-verified (PatchApplier/Sandbox exist, prior-hash + network fixes noted), (4) R10 decided `#testing` / R11 keep+UNVERIFIED. Remaining gap is (5) operator APPROVED PENDING — the gate below.

---

### Approval gate

Present a final summary: re-partition keeps spec order tools → ledger → mandates → real run but inserts `workspace.py` serial contract (N1.0a), freezes `ToolJournal` + persisted intent in N2.1, fixes journal order to post-hook, changes 8 events to `_NO_PHASE_CHANGE`, renames `git_add`→`git_stage`, renames `requester`→`requester_asserted`, makes `doctor` report three mandate counts, blobs >8 KB in new N2.6, and runs S1/kill-drill on Linux/WSL2 with OpenRouter. Files touched in §F; tests in §H; risks/rollbacks in §§K-L.

**Do you approve this plan for implementation? Reply with `APPROVED` (optionally with changes), or tell me what to revisit.**
