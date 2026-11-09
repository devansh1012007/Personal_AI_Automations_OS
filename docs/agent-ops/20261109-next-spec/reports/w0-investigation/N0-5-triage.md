<!-- ORCH-DIGEST:START -->
agent: W0-IMPL-05 | task: T-N0-5 (N0.5) | status: DONE
code_commits: none (report-only) | report_commit: <filled at commit time>
files_changed: 1 (docs/agent-ops/20261109-next-spec/reports/w0-investigation/N0-5-triage.md)
contracts_changed: none
tests: added 0, changed 0; scoped run: pass (4 xfail verified) + 1 failed reproduced as env
ownership_violations: none
deferred_dependencies: none
out_of_scope_findings: 2 (test fragility on Windows/root; spec baseline note "needs Docker daemon" is imprecise)
temporary_mitigation: no
deep_issue_flag: yes (3 real validation bugs behind strict xfails + 1 operator policy decision)
least_confident_about: exact per-group counts of the 245 Linux skips (full suite not run per brief)
confidence: high (failure + 4 xfails verified first-hand; skips verified by guard inspection, not by full run)
last_comms_line_read: 2026-11-09T00:00:00Z (WAVE-START)
<!-- ORCH-DIGEST:END -->
# Triage report — W0-IMPL-05 — T-N0-5 (N0.5) baseline failure/skip/xfail triage

## 1. Task and acceptance criteria (restated)

Triage the Linux baseline on `ops/20261109-next-spec` (from `main @ b8570a6`):
1 failed (`test_user_is_the_workspace_owner_not_root`), 245 skipped, 4 xfailed
[VERIFIED docs/2026-11-09-next-spec.md:36-37]. Label each as environment-specific
or real, with evidence (`path:line`, command output). For real items, file
issue-ready task descriptions (no code edits — this task is report-only; any fix
belongs to a Wave 1+ coder). Acceptance: every one of the 1 failed + skip groups
+ 4 xfailed has a label, evidence, and, if real, a task description.

## 2. Method and what was actually verified myself

- Read the brief, standing orders, mission, plan §N0.5, spec §2, COMMS, report template [VERIFIED via `read`].
- Grepped for the failing test name, all `xfail` markers, all `requires_*` /
  `importorskip` / `skipif` sites in `tests/` [VERIFIED via `grep`].
- Read the failing test, its backend implementation, each xfail test + reason,
  and each skip guard/context [VERIFIED via `read` with `path:line` below].
- Ran scoped tests only (per brief §6/§7), on this Windows host
  (`D:\dev work\coding\Projects\Personal_AI_Automations_OS`, branch
  `ops/20261109-next-spec` [VERIFIED via `git branch --show-current`]):
  - The failing test alone: **FAILED with `AttributeError: module 'os' has no
    attribute 'getuid'`** [RAN `.venv/Scripts/python.exe -m pytest tests/sandbox/test_docker_backend.py::test_user_is_the_workspace_owner_not_root -q`].
    This is a *different mechanism* from the Linux baseline failure (see §4);
    both are environmental, proven below. On Windows `Path('.').stat().st_uid
    == 0` and `hasattr(os,'getuid') == False` [RAN `python -c` probe].
  - The 4 xfailed tests together: **`xxxx` (4 xfailed, 0 xpassed, 0 failed)**
    [RAN `pytest ... 4 nodeids ... -q -o addopts="-q --strict-markers --strict-config"`].
    Confirms all four strict xfails are still failing as expected, not flipped.
- Did **NOT** run the full suite (brief forbids it) [VERIFIED as method constraint].
  Skip-group *membership* is by guard inspection; exact per-group counts of the
  245 are therefore **[ASSUMED/UNKNOWN]** — §5 gives the groups with evidence,
  not a fake count reconciliation.

## 3. Headline verdicts

| Item | Count | Label | Action |
|------|-------|-------|--------|
| `test_user_is_the_workspace_owner_not_root` (Linux baseline) | 1 failed | **ENVIRONMENT** (test-host assumption, not product bug) | File test-robustness task TASK-1 (skip on no-`getuid` / root host). No Wave-1 blocker. |
| Skip groups (postgres, s3, vector, graph, embeddings, redis, api, platform, conditional-content) | 245 skipped | **ENVIRONMENT** (all guards are extra/server/platform gates by design) | No tasks. Do not "fix" — skipping without the extra/server is the documented contract. |
| Strict xfails | 4 xfailed | **REAL (3) + OPERATOR-DECISION (1)** | File TASK-2, TASK-3, TASK-4 (validation bugs) and TASK-5 (policy decision, already in AGENTS.md open items). |

Bottom line: **zero real product regressions hide in the "1 failed" or the
"245 skipped"**. The 4 xfails are the only real items, and they are
self-documenting (each pins a known defect/proposal with its fix location).
Wave 1's green gate can honestly treat the baseline as green-modulo-env, provided
the two test-fragility notes in §4 are handled.

## 4. The 1 failure — `test_user_is_the_workspace_owner_not_root`

### 4.1 Identity and what it asserts

- Defined at `tests/sandbox/test_docker_backend.py:510-522` [VERIFIED via `read`].
- It creates `tmp_path / "ws"`, builds `DockerSandbox(SandboxSettings())`, calls
  the pure function `sb._build_args(...)`, and asserts the `--user` flag equals
  `f"{os.getuid()}:{os.getgid()}"` [VERIFIED tests/sandbox/test_docker_backend.py:519-522].
- **No Docker daemon is involved.** The file header says so explicitly: "Docker
  is not installed on the Windows dev box, so nothing here starts a real
  container. The ``docker create`` argv is a pure function of the spec and the
  detected runtime, so it is tested directly" [VERIFIED
  tests/sandbox/test_docker_backend.py:3-9]. The spec's parenthetical "(needs a
  Docker daemon and a non-root host user)" [VERIFIED docs/2026-11-09-next-spec.md:37]
  is therefore **imprecise**: a daemon is neither needed nor used here. The true
  prerequisites are POSIX `os.getuid`/`os.getgid` and a non-root workspace owner.

### 4.2 Why it fails on the Linux baseline host (env, not product)

Backend behaviour, in order [VERIFIED src/paa/sandbox/docker_backend.py:894-939]:

1. `_workspace_owner(workspace)` stats the workspace and returns `(st_uid, st_gid)`
   [VERIFIED src/paa/sandbox/docker_backend.py:881-892].
2. `_container_user`: if `owner[0] != 0`, return `"{uid}:{gid}"`. If the workspace
   is root-owned, log `sandbox.docker.root_owned_workspace` warning and **refuse
   step 1**; fall back to the image's declared user, then to
   `_FALLBACK_CONTAINER_USER = "10001:10001"` [VERIFIED
   src/paa/sandbox/docker_backend.py:103, 923-939].
3. The never-root invariant is deliberate and structural: "--user 0:0 would put
   the workload back in container-root, and the exact harm the flag exists to
   prevent ... is what would happen" [VERIFIED src/paa/sandbox/docker_backend.py:915-921].
   The sibling test `test_user_is_never_root_even_for_a_root_owned_workspace`
   pins this at tests/sandbox/test_docker_backend.py:525-548.

On a Linux host running as root (e.g. a root container/CI user), the workspace
owner is `(0, 0)`, so the backend correctly returns the fallback (`10001:10001`
or image user), while the test expects `0:0`. **The product code is doing the
right thing; the test's expected value assumes a non-root host.** Verdict:
ENVIRONMENT (test-host assumption). Evidence: the warning fires on my Windows
run too ("the workspace is owned by root; the container will not run as root")
in the captured stdout, and my probe shows `st_uid == 0` — same rule, different
host. [RAN focused pytest, stdout captured].

### 4.3 Why it also fails on this Windows host (env, different mechanism)

[RAN focused test -> FAILED `AttributeError: module 'os' has no attribute
'getuid'` at tests/sandbox/test_docker_backend.py:522; probe confirms
`sys.platform == "win32"` and no `getuid`.] Windows has no `getuid`/`getgid`;
`Path.stat().st_uid` reports 0, so the backend takes the root-owned-workspace
branch and returns the fallback. Again: env-gated test asserting POSIX process
identity on a non-POSIX host. Same label (ENVIRONMENT), same fix direction.

### 4.4 What this is NOT

- Not a Docker-daemon issue (no daemon call is made) [VERIFIED file header + test body].
- Not a regression in `_build_args`/`_container_user` — the fallback path is the
  intended behaviour and is separately pinned by
  `test_user_is_never_root_even_for_a_root_owned_workspace`,
  `test_user_is_always_present`, `test_user_falls_back_to_the_images_own_user` [VERIFIED test_docker_backend.py:525-573].
- Not a Wave-1 blocker. The one real defect is the test's missing host guard.

### 4.5 TASK-1 (filed, issue-ready) — test-robustness, not product fix

Title: `test_user_is_the_workspace_owner_not_root` assumes a non-root POSIX host
and fails on root/Windows hosts.

Evidence: `os.getuid()` at `tests/sandbox/test_docker_backend.py:522`;
backend never-root rule at `src/paa/sandbox/docker_backend.py:894-939`.

Expected (one of): (a) `pytest.mark.skipif` when `not hasattr(os, "getuid")`
(Windows), and skip when the test process runs as root (`os.getuid() == 0`),
with a reason naming the assumption; or (b) monkeypatch `_workspace_owner`
to a fixed non-zero uid/gid (the pattern the sibling test at
`tests/sandbox/test_docker_backend.py:542-544` already uses) and assert `--user`
equals that value. Option (b) is preferred: it tests the assertion on every host
instead of skipping it away.

Acceptance: test passes on Linux non-root, Windows, and Linux root; no change to
`_container_user` logic; sibling never-root tests still pass. Verify with the
scoped single-test run and the surrounding file
(`pytest tests/sandbox/test_docker_backend.py -q`).

## 5. The 245 skips — groups, guard for each, verdict

Rule of thumb for this repo (from AGENTS.md, verified in code): a missing
*driver/extra* is a missing dependency and a missing *server* is a missing
server; neither is a failure. Every guard below is one of those, a platform
gate, or a "there is nothing else to test here" early exit. None masks a broken
assertion. Exact counts per group on the Linux baseline host are **[UNKNOWN]**
— the full run is out of scope for this agent — so groups are listed with their
guard evidence, and total consistency (245) is checked by the orchestrator's
full gate, not faked here.

### 5.1 `requires_postgres` — needs psycopg extra + `PAA_TEST_POSTGRES_DSN` server

- Central fixture: `tests/storage/conftest.py:29,51-52` — `POSTGRES_DSN =
  os.environ.get("PAA_TEST_POSTGRES_DSN")`; fixture does
  `pytest.skip("needs PAA_TEST_POSTGRES_DSN")` when absent [VERIFIED via `read`;
  docstring at conftest.py:18-28 states the contract: names a *server*, the
  fixture creates/drops its own database, "a run can never write to the database
  a deployment is using"].
- Per-file `skipif` with reason `"needs the 'postgres' extra and
  PAA_TEST_POSTGRES_DSN"`: `tests/storage/test_postgres_migrations.py:82-85`,
  `tests/storage/test_postgres_contract.py:113-116`,
  `tests/storage/test_entity_parity.py:165-168`,
  `tests/storage/test_entity_normalisation_p003.py:830-833`,
  `tests/storage/test_tier_migration_p009.py:469-472,838-841` [VERIFIED via `grep`].
- Marker registered in `pyproject.toml:99` [VERIFIED via `grep`].
- Label: **ENVIRONMENT** (server not provisioned on a laptop/CI without services).
  In CI these run against a service container per AGENTS.md gVisor/CI section;
  on a bare `.[dev]` Linux box they skip, correctly. No task.

### 5.2 `requires_s3` — needs boto3 extra + `PAA_TEST_S3_*` server

- `tests/storage/test_s3_live.py:118` `pytest.importorskip("boto3", ...)` (module
  level guard); `:124-126` skips when no labelled/unlabelled server is configured
  ("needs a live S3 server: set PAA_TEST_S3_ENDPOINT ..."); `:130`
  `pytestmark = pytest.mark.requires_s3` [VERIFIED via `grep`+`read`].
- Conftest documents the server-naming convention at
  `tests/storage/conftest.py:91-142` (base bucket + pid/uuid suffix, labelled
  `PAA_TEST_S3_<LABEL>_*` for a second server) and `importorskip("boto3")` at
  `conftest.py:335` [VERIFIED via `grep`].
- Marker at `pyproject.toml:100` [VERIFIED]. Label: **ENVIRONMENT**. No task.
  (Two servers SeaweedFS/RustFS per `test_s3_live.py:73-78`.)

### 5.3 `requires_vector` / `requires_embeddings` — qdrant-client / sentence-transformers extras

- `tests/storage/test_vector.py:42-43`: `HAS_QDRANT`/`HAS_SENTENCE_TRANSFORMERS`
  via `importlib.util.find_spec`; `BACKENDS` qdrant param marked
  `requires_vector` + `skipif(not HAS_QDRANT, ...)` [VERIFIED test_vector.py:42-55].
  Whole-backend params skipf at `:556,637,989,1040` [VERIFIED via `grep`].
- Inverse guards at `:910,920`: skips when the extra *is* installed ("the real
  encoder is selected" / "nothing to fall back from") [VERIFIED via `grep`].
- Markers at `pyproject.toml:95,97` [VERIFIED]. Label: **ENVIRONMENT**. No task.

### 5.4 `requires_graph` — kuzu extra

- `tests/storage/test_graph.py:36` (`skipif(not kuzu_available(), ...)`) on the
  kuzu `BACKENDS` param, and `:504` for a whole test; sqlite backend still runs
  [VERIFIED via `read`+`grep`]. Marker at `pyproject.toml:96`. **ENVIRONMENT**. No task.

### 5.5 Redis queue — `redis` extra + live server at `127.0.0.1:6379`

- `tests/storage/test_queue.py:694` `importorskip("redis", ...)`;
  `:718-731` `_redis_queue_or_skip` imports the extra, pings
  `redis://127.0.0.1:6379/0`, and `pytest.skip(f"no reachable redis server:
  {exc}")` on any connection failure [VERIFIED via `read`]. Module import
  without the extra is separately pinned (`:705-710`). **ENVIRONMENT**. No task.

### 5.6 `requires_api` — fastapi/uvicorn extra

- `tests/api/conftest.py:22`: `pytest.importorskip("fastapi", reason="needs the
  api extra")` at module scope — the whole `tests/api/` directory skips at
  collection on a minimal install [VERIFIED via `read`]. The documented `.[dev]`
  setup carries `paa[api]` (AGENTS.md setup section), so on the baseline host
  this group contributes ~0 skips; it is large only on a bare install.
  Label: **ENVIRONMENT** (install shape, not code). No task. (This is exactly
  why AGENTS.md says the gate must install `.[dev]`, not `.` — a green minimal run
  that skipped the API suite would be vacuous.)

### 5.7 Platform gates (Windows vs POSIX)

- `tests/deployment/test_fetch_images.py:121-124`: whole module
  `skipif(sys.platform == "win32", "deploy/fetch-images.sh is a bash program...")` [VERIFIED via `read`].
- `tests/sandbox/test_win32.py:107,123,141`: skip the off-Windows-behaviour
  assertions when `IS_WINDOWS` [VERIFIED via `read`] — these skip on Windows,
  run on Linux, i.e. they contribute 0 to the Linux 245.
- `tests/sandbox/test_subprocess_backend.py:157`: skip when the fixed-system-PATH
  (POSIX) contract does not apply (Windows) [VERIFIED via `grep`].
- `tests/validation/test_workspace.py:253`: skip when running as root (root can
  read a mode-000 file, branch unreachable) — environment by definition.
  Label: **ENVIRONMENT**. No task. (The brief's Linux baseline includes only the
  POSIX ones of this group, if any.)

### 5.8 Conditional-content early exits

Each is an explicit `pytest.skip` inside one test for a branch that cannot be
exercised in that environment; each names its reason in code:

- `tests/marketplace/test_marketplace.py:260` — "real signing backend not
  available; dev fallback cannot authenticate" [VERIFIED via `grep`].
- `tests/context/test_budget.py:97` — "tiktoken is installed in this
  environment" (tests the fallback path when tiktoken is absent) [VERIFIED via `grep`].
- `tests/storage/test_minio_backend.py:893` — inverse: skipif boto3 *is*
  available (the no-driver branch) [VERIFIED via `grep`].
- `tests/storage/test_errors.py:1173` — `importorskip("psycopg._queries")` for
  one psycopg-specific error-shape test [VERIFIED via `grep`].
  Label: **ENVIRONMENT / INTENTIONAL**. No task.

### 5.9 Explicitly checked: `tests/ledger/test_recovery` has no skips

`grep` for `skip|postgres|s3|requires` in `tests/ledger/test_recovery.py` returns
**zero hits** [VERIFIED via `grep`]. The chaos/recovery trio runs unconditionally
on the baseline host — the 245 skips do not hide the crash-recovery evidence.
The "read tests/ledger/test_recovery" instruction in the brief is satisfied:
that file is skip-free, so nothing to triage there.

### 5.10 Skip verdict

All 245 are intentional environment gates. **No skipped test is a failed test
in disguise.** If Wave 1's gate needs the server-backed paths exercised, the
fix is to provision services/DSNs (CI already declares them), not to delete
skips. No tasks filed.

## 6. The 4 xfails — all strict, each a known real item

All four use `pytest.mark.xfail(strict=True)`, and my scoped run confirms
`xxxx` — they fail as expected today, so the suite is *not* silently green
over them: a strict xfail that starts passing fails the build [VERIFIED
tests/agents/test_policy.py:175, tests/validation/test_patch.py:390,
tests/validation/test_engine.py:218, tests/validation/test_ast_scanner.py:449;
RAN 4 nodeids -> `xxxx`]. Each reason names the fix location and why it is
deferred. They are real *known* items, not regressions.

### 6.1 `test_the_lockdown_air_gap_refusal_names_the_egress` — OPERATOR DECISION

- `tests/agents/test_policy.py:175-210`. The declared-permission half of
  `PolicyRiskAgent._check_lockdown` is unreachable: LOCKDOWN grants only
  SANDBOX_RUN, so `_check_permissions` refuses first. Same plans refused either
  way — only the recorded refusal *reason* differs.
- Deferred deliberately, not by accident: "the fix is a policy decision (which
  refusal should win), not a coverage one, and it is reported rather than made
  silently. The proposed change is to move `_check_lockdown` ahead of
  `_check_permissions` in the checks tuple at `src/paa/agents/policy.py:164`"
  [VERIFIED test_policy.py:177-191]. AGENTS.md "Two policy decisions are open"
  carries this same item as operator-owned [VERIFIED AGENTS.md end section].
- Label: **REAL-DECISION (not a bug)**. TASK-5 below.

### 6.2 `test_a_created_file_contains_exactly_what_the_diff_describes` — REAL BUG

- `tests/validation/test_patch.py:390-419`. A created file gains one trailing
  newline it was never given: the planner models an empty prior file as one
  empty line (`''.split('\n') == ['']`), so created content is `created\n\n`.
  The reason pins the fix: "`test_patch.py` ... is read-only for T-005; the fix
  is in `UnifiedDiffValidator._plan_file` — drop the phantom line for an empty
  prior file" (fix site `src/paa/validation/patch.py`) [VERIFIED test_patch.py:392-399].
- Why it is real and not cosmetic: the recorded ledger sha256 is over the wrong
  bytes; Wave 1's `write_file` reuses this planner, so the phantom newline
  would land in real trees. Label: **REAL**. TASK-2.

### 6.3 `test_an_artifact_with_nothing_to_check_does_not_pass` — REAL BUG

- `tests/validation/test_engine.py:218-243`. An artifact with nothing to check
  reports PASS, contradicting the `ValidationArtifact` docstring ("an empty
  artifact cannot quietly PASS"). The reason notes the real caller path:
  `CriticValidatorAdapter.validate` builds an artifact from worker output with
  no `source_files`/`patch`/`payload_schema`, gets `passed=True` back [VERIFIED
  test_engine.py:220-230]. Fix: "in `DeterministicValidationEngine.validate` —
  an artifact for which no check ran is a FAIL, not a PASS"
  (`src/paa/validation/engine.py`) [VERIFIED test_engine.py:227-229].
  Label: **REAL** (a gate that checks nothing passes things). TASK-3.

### 6.4 `test_deeply_nested_source_does_not_escape_as_a_recursion_error` — REAL BUG

- `tests/validation/test_ast_scanner.py:449-471`. `AstSecurityScanner.scan`
  guards `ast.parse` against `RecursionError` but not the `NodeVisitor.walk`
  after it: a 2000-deep attribute chain exhausts the stack in
  `generic_visit`, outside the try/except at
  `src/paa/validation/ast_scanner.py:364-392`. A scanner that raises has no
  verdict — the gate is absent on hostile input [VERIFIED test_ast_scanner.py:451-458].
  Fix: "wrap the visitor call in the same guard and return the
  `unparseable_source` CRITICAL finding." Label: **REAL** (DoS/escape on hostile
  source; hostile-input criterion in standing orders #10 would flag this).
  TASK-4.

## 7. Filed tasks (issue-ready, not code)

### TASK-1 (from §4.5) — test fragility, ENV
`test_user_is_the_workspace_owner_not_root` assumes non-root POSIX host.
Fix: monkeypatch `_workspace_owner` to a fixed non-zero uid/gid (sibling pattern)
or skip on Windows / when `geteuid()==0`. See §4.5 for acceptance.

### TASK-2 — patch phantom newline (REAL), owner: Wave-1 coder (N1.2 touches the same planner)
In `UnifiedDiffValidator._plan_file` (`src/paa/validation/patch.py`), drop the
phantom empty line for an empty prior file so a created file contains exactly
what the diff describes; the trailing newline must follow the patch body.
Evidence: `tests/validation/test_patch.py:390-399`. Done when
`test_a_created_file_contains_exactly_what_the_diff_describes` passes without
the xfail marker (strict xfail will fail the build until the marker is removed —
remove marker + test in the same commit after the fix). Must also verify the
recorded `compute_patch_sha256` matches the corrected bytes.

### TASK-3 — empty-artifact PASS (REAL), owner: validation/engine owner (pre-Wave-1 hygiene)
In `DeterministicValidationEngine.validate` (`src/paa/validation/engine.py`),
an artifact for which no check ran is FAIL, not PASS. Evidence:
`tests/validation/test_engine.py:218-230` incl. the `CriticValidatorAdapter`
caller path. Done when
`test_an_artifact_with_nothing_to_check_does_not_pass` passes and the xfail
marker is removed; re-check that legitimate all-skipped-but-counted cases
(`test_engine.py:205-215`) still behave as asserted there.

### TASK-4 — AST scanner walk RecursionError (REAL, hostile input), owner: validation owner
In `src/paa/validation/ast_scanner.py:364-392`, wrap the visitor call in the
same `RecursionError` guard as `ast.parse` and return the `unparseable_source`
CRITICAL finding. Evidence: `tests/validation/test_ast_scanner.py:449-458`.
Done when `test_deeply_nested_source_does_not_escape_as_a_recursion_error`
passes and the marker is removed; keep `test_the_scanner_never_raises_on_hostile_source`
green. Consider a recursion-limit-independent formulation (depth-bounded walk)
rather than relying on the interpreter limit — flag for the implementing agent.

### TASK-5 — LOCKDOWN refusal-order decision (OPERATOR), owner: operator (D-8-adjacent)
Decide which refusal wins for a LOCKDOWN plan that both declares an ungranted
permission and trips the air-gap check: (i) move `_check_lockdown` ahead of
`_check_permissions` at `src/paa/agents/policy.py:164` so the air-gap reason
("egress/external-write requested under LOCKDOWN") answers, or (ii) delete the
dead declared-permission half of `_check_lockdown`. Evidence:
`tests/agents/test_policy.py:177-191` + AGENTS.md open-items section.
Either way, remove the strict xfail and its companion pinning test in the same
commit as the decision. **Do not fix without the operator's word** — this is
recorded as an open policy decision, and the recorded `POLICY_BLOCKED` payload
reason changes for that plan class.

## 8. Long-run test (standing orders #8)

- **Source**: ledger/replay/convention files untouched by this task (report-only);
  no contract changes. The triage reads only.
- **Load (100x, concurrency, partial failure)**: N/A — no runtime change. Note
  TASK-4's scanner escape is itself a partial-failure (hostile input -> no
  verdict) item.
- **Hostile input**: TASK-2/3/4 are exactly the hostile/malformed-input family;
  each test uses adversarial input (empty prior file, empty artifact, 2000-deep
  chain) and must fail without the fix — verified by the strict-xfail returning
  `xxxx` today, and by construction once the marker is removed.
- **Operability (observe, roll back, migrate)**: this report is the record;
  rollback is `git revert` of the docs-only commit. Per-group skip counts are
  marked UNKNOWN and must be confirmed by the orchestrator's full gate, not
  assumed from this report.
- **Reversal (which test fails if the fix is reverted?)**: TASK-1: reintroducing
  the bare `os.getuid()` assertion fails on Windows/root hosts only. TASK-2/3/4:
  reverting the src fix makes the (now un-xfailed) test fail — exactly the
  strict-xfail tripwire. TASK-5: either decision removes the xfail; reverting
  the decision reinstates it.

## 9. Edge cases and abuse scenarios considered

- Skip-masking-a-bug: checked each guard names its missing extra/server/platform;
  none converts an assertion failure into a skip at runtime (all are setup-time
  `importorskip`/`skipif`/fixture-DSN gates except §5.8, which are named
  branch-unreachable exits, each with its reason in code).
- Xpass drift: strict markers mean a fixed bug with a forgotten marker fails the
  build — the guard is self-maintaining.
- Baseline transfer Windows -> Linux: the Linux failure mechanism (root owner
  -> fallback) is proven by the backend rule + my Windows reproduction of the
  same root-owned-workspace branch, not by running Linux. The Linux run itself
  was **not** executed by this agent [UNKNOWN as a direct run; INFERRED with
  high confidence from code + spec baseline].
- Spec imprecision: "needs a Docker daemon" for the 1 failure is wrong; flagged
  as an out-of-scope finding (§10), not edited (docs/plans is forbidden).

## 10. Out-of-scope findings

1. Spec §2 parenthetical "(needs a Docker daemon and a non-root host user)"
   [VERIFIED docs/2026-11-09-next-spec.md:37] overstates: the test never calls a
   daemon. Needs spec-wording fix by the spec owner; recorded here as FINDING
   via COMMS.
2. Same test-robustness class as TASK-1 exists for any test asserting
   `os.getuid()` directly — a repo-wide grep for `getuid` in `tests/` would
   scope it. Not done here (scoped triage). Suggest the TASK-1 agent grep first.

## 11. Commands run and results (verbatim summaries)

| Command | Result |
|---------|--------|
| `git branch --show-current` | `ops/20261109-next-spec` |
| `grep test_user_is_the_workspace_owner_not_root` | 5 hits; def at `tests/sandbox/test_docker_backend.py:510` |
| `grep xfail tests/` | 4 hits (listed in §6) |
| `grep requires_* / importorskip / skipif tests/` | 9 marker sites + 38 guard sites (§5 groups) |
| `.venv/Scripts/python.exe -m pytest ...::test_user_is_the_workspace_owner_not_root -q` | `FAILED`, `AttributeError: module 'os' has no attribute 'getuid'` at test_docker_backend.py:522; root-owned-workspace warning in stdout |
| `python -c` stat probe | `st_uid/st_gid = 0 0`, `sys.platform = win32`, `hasattr(os,'getuid') = False` |
| `pytest <4 xfail nodeids> -q -o addopts=...` | `xxxx` (4 xfailed, exact `TestLockdown` nodeid resolution via class path) |
| Def-per-file counts (coarse, incl. params) | s3_live 22, pg_migrations 19, pg_contract 7, graph 36, vector 70, queue 48, tier_p009 13, parity 28, normalisation 24 |
| `grep` in `tests/ledger/test_recovery.py` | 0 skip hits |
| Full suite | **NOT RUN** (brief-forbidden; orchestrator gates) |

## 12. Deviations from plan/brief

- None material. Required-reading list followed in order; report-only write-set
  honoured (this file only). The brief suggested reading `tests/ledger/test_recovery`
  and `tests/storage` skip sites — done by `grep`/`read` rather than running them,
  since the full suite is out of scope and skip-guards are static.

## 13. Known limitations, risks, rollback notes

- Per-group skip counts are repository-structure facts, not measured counts;
  the 245 total is taken from the spec baseline, not recounted.
- Linux-baseline failure mechanism is inferred from code + spec + Windows
  reproduction, not a Linux run by this agent.
- No code changed, so no rollback beyond reverting this report commit.
- TASK-2 has Wave-1 affinity (planner reuse); schedule it with N1.2, not after.

## 14. Where I am least confident

- Exact split of the 245 skips by group on the Linux baseline host (counts are
  guard-level facts, not a measured `-rs` report). Orchestrator's full gate
  should re-derive it with `pytest -rs` on Linux.

## 15. Self-review checklist

- [x] Every item in scope (1 failed + skip groups + 4 xfailed) has a label + evidence.
- [x] Real items have issue-ready tasks (TASK-1..5); no code edited.
- [x] Factual claims tagged [VERIFIED]/[RAN]/[ASSUMED]/[UNKNOWN].
- [x] No full-suite run, no container restart, no secret written.
- [x] Only the write-set file created.

## 16. Commits

(Report-only commit(s) to `ops/20261109-next-spec`, explicit paths, filled at
commit time; SHA(s) back-filled into the ORCH-DIGEST block
`report_commit` field if the digest requires it.)


