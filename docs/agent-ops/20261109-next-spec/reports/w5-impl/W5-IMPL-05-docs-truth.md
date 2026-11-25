<!-- ORCH-DIGEST:START -->
agent: W5-IMPL-05 | task: T-C5-docs (C5 docs truth pass) | status: DONE
code_commits: none (orchestrator commits after review) | report_commit: pending
files_changed: 1 (this report) — the truth pass verified the wave's 16 in-flight
  docs and made no edits: every anchor defect found was conventions-green, so
  per the brief's fix-only-when-breaking rule each is recorded as a finding
contracts_changed: none
tests: conventions suite run twice, both green (34 passed — run 1 before, run 2
  after a sibling's 12:41Z docs/security re-anchor landed mid-verification);
  test_every_referenced_line_is_not_blank green standalone (1 passed); ruff
  src+tests clean; scoped counts verified: tests/sandbox 201, test_session 21
  (21 passed), tests/observability 185
ownership_violations: none (no file outside this report written; sibling-owned
  README.md, mcp-server.md, skills.md, docs/security were read and verified but
  not edited)
deferred_dependencies: 5 one-line anchor fixes left for a central pass with
  owners (glossary.md:110 + overview.md:122 base.py:55→:59; component-
  interactions.md:102 + data-lifecycle.md:184 runtime.py:77→:92; modules-and-
  services.md:383 base.py:625→:622; cli.md:81 base.py:405→:59/:738; the
  README.md:172 :1320-vs-:1336 nuance) — all conventions-green, so recorded
  not fixed, per brief
out_of_scope_findings: 3 (the 5s-probe prose staleness in AGENTS.md + 4 docs —
  already filed twice by earlier runs, component-interactions.md's copy fixed
  this wave and verified against runtime.py:79; the mid-flight docs/security
  landing at 12:41Z between my two suite runs, both green; W5-IMPL-01's 8
  blank-cite failures from this wave's source shifts — all now re-anchored
  green, verified same-content)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: that the verified state is final — sibling docs were
  landing while I worked (docs/security 12:41:39Z; a sibling report appeared at
  ~12:44Z during my final suite run); every claim below is pinned to the state
  the second green run executed against
confidence: high (every disposition verified by reading both sides myself —
  doc line and target source line, against HEAD content where the claim is a
  re-anchor)
last_comms_line_read: 2026-11-09T18:19:12Z W3-IMPR-01 (COMMS.md's last line on
  disk; no Wave 5 entries have landed yet)
<!-- ORCH-DIGEST:END -->
# Docs truth pass — W5-IMPL-05 — T-C5-docs (C5), wave 5, 2026-11-23

Task: spec `docs/plans/2026-11-21-next-spec-2.md` §4, task 4-d — the
docs/conventions truth pass over Wave 5's in-flight tree: re-run the
conventions suite and confirm green; verify `docs/README.md:172`'s citation
(orchestrator saw it citing `sandbox/subprocess_backend.py:1289`, a blank
line); sweep the wave's modified docs against the implementation
(`base.py::SandboxSession`, `skills/adapters/mcp.py`'s sandboxed spawn) and
report mismatches, fixing trivially-wrong anchors **only** where the failure
would otherwise break conventions. The original agent finished its fixes but
ran out of context before writing the report; this document verifies that
work and is the deliverable. No commit is made here; the orchestrator commits
after review.

## 1. Method

- **The suite first, twice.** Run 1 at ~12:34Z, run 2 at ~12:45Z — because
  `docs/security/security-model-and-trust-boundaries.md` was re-anchored by a
  sibling at **12:41:39Z**, between the two runs (my first read of its diff
  saw `sandbox/base.py:55`, `:306`, `:275`; the on-disk state minutes later
  read `:59`, `:628`, `config.py:467`). Both runs green, so the lock held
  across the sibling's edit; the findings below describe the post-12:41Z
  state, with the pre-landing intermediate noted where it matters.
- **Every changed citation read on both sides.** The `+` lines of
  `git diff` across the wave's 16 modified docs were extracted; for each
  cited `file:line` the target source line was read, and where the claim is a
  re-anchor (the wave's source files shifted), the HEAD content at the old
  number was read (`git show HEAD:…`) to confirm the anchor still names the
  same code — the wave's own rule for a truthful re-anchor: *same content,
  new number*. A citation whose number changed but whose content did not is
  verified, not merely tolerated.
- **The implementation side read, not assumed**: `base.py` (SandboxSession ABC
  at :491, `Sandbox.open` :753, `run_within` :767), `mcp.py` (`MCPClient.start`
  :220–285, the `sandbox.open(spec)` at :273), the refuse-open trio
  (`wsl_backend.py:272`, `docker_backend.py:1028`, `dryrun_backend.py:127`),
  and the test counts the docs now claim (201 / 21 / 185 — collected and, for
  `test_session.py`, run: **21 passed**).
- **No edits made**: every anchor defect found was conventions-green, and the
  brief's rule is explicit — fix only where the failure would otherwise break
  conventions; otherwise record. Nothing broke; everything is recorded.

## 2. Commands run and results (verbatim)

| Command | Result |
|---|---|
| `python -m pytest tests/conventions -p no:cacheprovider` (run 1, ~12:34Z) | `34 passed in 244.66s (0:04:04)` |
| `python -m pytest tests/conventions -p no:cacheprovider` (run 2, ~12:45Z, post-12:41Z tree) | `34 passed in 153.73s (0:02:33)` |
| `python -m pytest tests/conventions -q -p no:cacheprovider` | `..................................  [100%]` (34 dots) |
| `python -m pytest tests/conventions/test_docs_references.py::test_every_referenced_line_is_not_blank -p no:cacheprovider` | `1 passed in 1.93s` |
| `python -m ruff check src tests` | `All checks passed!` |
| `python -m pytest tests/sandbox/test_session.py -q -p no:cacheprovider` | `.....................  [100%]` (21 passed) |
| collect-only counts | `tests/sandbox`: 201; `test_session.py`: 21; `tests/observability`: 185 (4 files) |

Suite composition: `test_docs_references.py` 5 + `test_import_conventions.py`
21 + `test_markdown_links.py` 8 = **34**. The lock the orchestrator saw red —
`test_docs_references.py::test_every_referenced_line_is_not_blank`, failing on
`docs/README.md:172` citing `sandbox/subprocess_backend.py:1289` (a blank
line) — is green, standalone and in the full suite, on the final state.

## 3. `docs/README.md:172` — the citation the brief named

- **History**: at HEAD the sentence cited `sandbox/subprocess_backend.py:548`;
  the wave's diff changes it to `:1320` (the orchestrator's failing run saw an
  intermediate `:1289`). The sentence: "Windows-host baseline for the
  20261106-memory-remembering-agent run: `mypy` 3 pre-existing errors
  (`sandbox/subprocess_backend.py:1320`, win32)…".
- **Both sides read**: `src/paa/sandbox/subprocess_backend.py:1320` is
  `if IS_WINDOWS:` — non-blank, inside `_kill_tree` (the tree-kill method
  whose docstring names the Windows/POSIX split). At HEAD, `:548` pointed
  into that same method's docstring ("Windows way to kill a tree without a
  job"); the mission baseline it records (`00-mission.md`: 3 win32 mypy
  errors — `os.killpg`/`getpgid`/`SIGKILL` missing on win32) has always been
  cited as a *region* pointer into this method, never as the literal error
  line.
- **Verdict: truthful.** The anchor names the win32 branch of the tree-kill
  the baseline is about. Nuance, recorded rather than fixed (the line is
  non-blank, conventions is green either way, and `docs/README.md` is
  sibling-owned): the literal line the three errors name —
  `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` — is now `:1336`, sixteen
  lines below the anchor, in the method's POSIX else-branch. A central pass
  that wants the exact line changes `:1320` → `:1336`; the citation's
  contract to a reader is already honest as it stands.

## 4. Citations fixed/verified — the wave's re-anchors, both sides read

Every anchor below was verified by reading the cited line in the worktree;
"same content" additionally means the HEAD line at the old number held the
same code (a truthful re-anchor, not a random new number).

- **docs/modules/sandbox.md** — `base.py:59` (`class IsolationLevel`),
  `:98` (`is_real_containment`), `:26` ("guard against a *confused* agent"),
  `:357` (`from_profile`), `:338` (`_reject_ambient_env_smell`), `:384`
  (`class SandboxResult`), `:605` (`resolve_workspace`), `:628`
  (`assert_inside_workspace`), `:660` (`build_child_env`), `:491`
  (`class SandboxSession`), `:753` (`Sandbox.open`), `:767` (`run_within`),
  `:544`/`:564`/`:577` (read/write/close), `:71` (`PROCESS = 1` — the level
  whose docstring is the "same OS user, same network stack" honesty),
  `:228` (`timeout_seconds` field); `subprocess_backend.py:292`
  (`class SubprocessSandboxSession`), `:303` (the "no setrlimit" docstring),
  `:1111` (`run_within`), `:1127` (`run` as thin wrapper); `wsl_backend.py:272`,
  `docker_backend.py:1028`, `dryrun_backend.py:127` (all three `open` refusals
  — read, they refuse exactly as documented); `__init__.py:100`
  (`get_sandbox`); `runtime.py:617` (`_acquire_sandbox`). **All truthful.**
  The new prose matches the implementation: the session contract (lifetime
  wall-clock not an idle timeout; refuse-at-open rather than a weaker session;
  whole-tree kill on close/cancel/breach/root death) is `base.py:491-524`'s
  own docstring; the "no `setrlimit`" story is `subprocess_backend.py:303`'s;
  the test-count claim is exact (**201** collected in `tests/sandbox/`,
  **21** in `test_session.py`, all 21 passed here).
- **docs/security/security-model-and-trust-boundaries.md** (sibling-owned;
  post-12:41Z state) — `validation/engine.py:238` (the hard-failure
  short-circuit — same content as HEAD's `:226`, +12 from W5-IMPL-01's
  no-checks-run verdict block); `sandbox/base.py:59` (`class IsolationLevel`),
  `:98`, `:26`, `:605`+`:628` (jail pair), `:660` (`build_child_env`),
  `:338`; `ast_scanner.py:125`–`:247` (`_DEFAULT_DENIED_MODULES` →
  `_DEFAULT_SENSITIVE_PATTERNS` — same span as HEAD's 122–244), `:360`
  (`def scan`, same content as HEAD's `:342`); `config.py:467`
  (`max_capture_bytes` field, default 8 MiB — the *correct* home for the
  claim). **All truthful.** The 12:41Z landing improved three anchors my
  first read had flagged (`:55`→`:59`, `:306`→`:628`, `base.py:275`→
  `config.py:467`) — the truer set is now on disk, and both suite runs
  bracket it green.
- **docs/modules/validation.md** — `engine.py:214` (`validate`), `:179`
  (`raise_for_verdict`), `:227` (the checks tuple), `:245` (`if
  artifact.run_tests:` — tests last), `:402` (`_check_tests`, the no-sandbox
  FAIL), `:353` (the patch workspace guard); `ast_scanner.py:125–:247`,
  `:360`, `:462` (`scan_or_raise`); `runtime.py:92` (`class
  CriticValidatorAdapter` — the correct cite, see finding 2), `:692` (the
  lazy import). **All truthful.**
- **docs/data/data-lifecycle.md** — `validation/engine.py:214`, `:238`
  (both sides read, same content as HEAD's `:208`/`:226`);
  `validation/patch.py:467` (`dry_run`). **Truthful** (its `runtime.py:77`
  is a pre-existing defect, finding 2).
- **docs/architecture/component-interactions.md** — `engine.py:191` (`class
  DeterministicValidationEngine`), `:214`; `runtime.py:617`; and the
  **semantic truth fix**: "Sandbox probe is bounded at 5s … `timeout=5.0`"
  became "bounded at 30s … `timeout=_SANDBOX_PROBE_BOUND_SECONDS`" — verified
  against `runtime.py:79` (`_SANDBOX_PROBE_BOUND_SECONDS = 30.0`, landed in
  commit 1c9ffa3). The old sentence was false; the new one is true.
  **Truthful** (its `runtime.py:77` is pre-existing, finding 2).
- **docs/backend/modules-and-services.md** — `sandbox/base.py:338` ✓;
  `engine.py:402`, `:353`, `:235` (`if not result.passed:` — same content as
  HEAD's `:236`) ✓; `sandbox/base.py:625` is finding 3.
- **docs/glossary.md / docs/architecture/overview.md** — `base.py:605` ✓
  (glossary, the jail pair); both still cite `base.py:55` for the
  IsolationLevel claim — finding 1.
- **docs/reviews/documentation-audit.md** — `validation/engine.py:214`,
  `validation/patch.py:270` (`safe_relative_path`) ✓; its `:67` "5s probe"
  line is out-of-scope finding 1.
- **docs/adr/0051-facade-and-capability-tokens.md** —
  `skills/adapters/mcp.py:336` (`_read_loop`), `:449` (`call` — the
  request/response correlation through the futures table). Both sides read;
  same-content re-anchors. **Truthful.**
- **"docs/plans and audits/MCP_AND_PLUGINS.md"** — the rewrites say what the
  code now does, and it does: `mcp.py` is **963 lines** (wc), the
  SkillContract mapping is `:682-735` (`_tool_to_contract`), the launch row
  describes `:220-285` (`start`, `sandbox.open(spec)` at `:273`, SandboxSpec
  env allowlist/workspace jail/memory ceiling/tree kill), the containment
  story is `:8-38` (module docstring "Containment, honestly"), server-initiated
  requests are ignored at `:429-431` (read: the "ignore rather than misroute"
  comment), `base.py:716-742` is the one-shot `Sandbox.run`, `:660-714`
  `build_child_env`, `docker_backend.py:91` (`DEFAULT_IMAGE`), `:1395`
  (`stdin=DEVNULL`), `:239-245` (prefer_gvisor/runtime), `:382-393`
  (isolation_level). **All truthful**, and the §0/§2 prose ("was … has since
  changed (C5.3)") is the honest tense for a dated plan.
- **docs/modules/skills.md** (sibling-owned; verified anyway) — the
  sandboxed-spawn bullet matches `mcp.py`: `MCPClient` docstring `:180-185`
  ("There is no unsandboxed spawn: `start` requires the containment
  backend"), `start` builds the `SandboxSpec` and calls `sandbox.open(spec)`
  (`:273`); the `manifest ∩ mandate ∩ policy` gate and `TOOL_CALL_DENIED`
  rows are the module docstring's own words (`:42-60`) and the adapter's
  construction (`:548-596`). **Truthful.**
- **docs/interfaces/mcp-server.md** (sibling-owned; verified anyway) —
  `scripts/mcp_stdio_smoke.py` exists and does what the doc says: asserts the
  catalogue carries `memory_remember`/`memory_search`/`sessions_list`
  (`:159`), drives initialize → tools/list → sessions_list → chat_send →
  memory_remember → memory_search, prints `SMOKE OK`, fails loudly on
  non-JSON stdout. The chat-turn claim ("the turn's remember() runs the
  MemoryCreator inside the turn and writes the `MEMORY_SIGNAL_EXTRACTED` row
  itself") matches `chat/loop.py:290`. **Truthful.**
- **README.md (root, sibling-owned)** — the observability honesty rows now
  claim 185 tests; **185 collected** in 4 files. **Truthful.**

## 5. The implementation spot-checks the brief mandated

- `src/paa/sandbox/base.py::SandboxSession` (ABC at :491-597): abstract
  `pid`/`alive`/`read`/`write`/`close`; the docs' description — env
  allowlist never ambient, cwd jail, resource envelope held open, whole-tree
  kill however the session ends, lifetime-not-idle timeout, refuse-at-open
  for backends that cannot deliver a duplex child — is the class's own
  docstring restated. No contradiction found in any of the five docs swept.
- `src/paa/skills/adapters/mcp.py` sandboxed spawn: `start(sandbox, …)`
  requires the backend, builds `SandboxSpec(command=argv,
  workspace_path=<mkdtemp jail>, env=<allowlist>, timeout_seconds=<lifetime>,
  memory_mb, cpu_cores, allow_network)` and opens through it (`:273`); a
  failed open cleans the scratch dir and re-raises as a containment failure.
  There is no unsandboxed code path. skills.md, MCP_AND_PLUGINS.md, ADR-0051
  and mcp-server.md all describe exactly this.

## 6. Docs-truth sweep findings (per-doc, honest — all conventions-green, so recorded, not fixed)

1. **glossary.md:110 and architecture/overview.md:122** — cite
   `sandbox/base.py:55` for the IsolationLevel claim. `:55` is
   `"sandbox_report_rows",` — an `__all__` entry. The class is `:59`
   (`class IsolationLevel`), which docs/security now cites correctly
   (post-12:41Z). Pre-existing at HEAD (`:54`, same `__all__` entry); the
   wave's +1 shift faithfully preserved content, so this is inherited rot,
   not a regression — but it is the lock's own blind spot ("wrong without
   being broken") and both files are in this task's write-set. Fix when a
   central pass can: `:55` → `:59` in both. Left unedited per the brief's
   fix-only-if-breaking rule.
2. **component-interactions.md:102 and data/data-lifecycle.md:184** — cite
   `runtime.py:77` for `CriticValidatorAdapter`. `:77` is a comment about
   the probe bound; the class is `:92` (which validation.md cites
   correctly). Pre-existing: `runtime.py` is untouched by this wave and
   HEAD:77 held the same comment. Fix: `:77` → `:92` in both.
3. **backend/modules-and-services.md:383** — "resolve_workspace raises
   `SandboxError` if the directory doesn't exist (`sandbox/base.py:625`)" —
   `:625` is `return resolved`; the raises are `:622` ("workspace does not
   exist") and `:624` ("workspace is not a directory"). Pre-existing
   (HEAD:515 = same content). Fix: `:625` → `:622`.
4. **modules/cli.md:81** — the `paa doctor` isolation-level honesty claim
   cites `sandbox/base.py:405`, which is a line of the
   `SandboxResult.duration_ms` docstring ("actually paid for."). Pre-existing
   (HEAD:404 = same content). The truthful anchor is `:59`
   (`class IsolationLevel`) or `:738` (`Sandbox.isolation_level`).
5. **docs/README.md:172** — the region-vs-exact-line nuance of §3
   (`:1320` `if IS_WINDOWS:` vs `:1336` `os.killpg(os.getpgid(proc.pid),
   signal.SIGKILL)`).

## 7. Out-of-scope findings (not fixed here, per brief)

1. **The 5s-probe prose staleness, still live in five places.** The code says
   30s (`runtime.py:79`, `_SANDBOX_PROBE_BOUND_SECONDS = 30.0`, landed in
   commit 1c9ffa3). This wave fixed component-interactions.md's copy
   (verified truthful). Still saying "5s": `AGENTS.md` ("5s probe timeout"),
   `docs/modules/storage.md:86`, `docs/operations/deployment-and-runtime.md:40`,
   `docs/reviews/documentation-audit.md:67`,
   `docs/architecture/repository-map.md:26`. Already filed twice by earlier
   runs (`docs/plans/2026-11-08-agents-update.md:13`; the memory-run
   `IMPR-02` note, which explicitly left prose values out of scope). Prose
   values are outside this task's line-cite write-set — re-filed here with
   owner: solo maintainer / the central docs pass. AGENTS.md is in the lock's
   scope but a semantic staleness is exactly what the lock cannot see.
2. **Mid-flight landings, recorded for the timeline**: docs/security was
   re-anchored at 12:41:39Z, between my two suite runs — both green, so the
   lock held across the sibling's edit, and the landed anchors are the truer
   set (§4). A sibling report (W5-IMPL-03) appeared at ~12:44Z during my final
   run — `docs/agent-ops/` is outside the lock's scope, no effect.
3. **W5-IMPL-01's out-of-scope item 1 is closed**: the 8 blank-cite failures
   it recorded (4 from its own `engine.py`/`ast_scanner.py` shifts, 4 from
   the sibling C5.2 `base.py` shifts) are all re-anchored and green — each
   verified as a same-content re-anchor (§4).

## 8. Deviations

- **None on scope or write-set: zero files edited.** The brief anticipated
  anchor fixes "only where the failure would otherwise break conventions";
  nothing broke, so the entire disposition is findings. The one judgment
  recorded: I read "siblings own README.md" as covering `docs/README.md`
  (the brief's own in-tree list includes it), so its §3 nuance is recorded,
  not edited — the safer reading, since sibling edits were landing while I
  worked.
- The original agent's fixes (which I verified rather than re-made) included
  one semantic truth fix beyond anchors — component-interactions.md's
  5s→30s probe bound. I verified it against the code; it is correct, and the
  four stale copies of the same sentence elsewhere are re-filed (finding 7.1)
  rather than silently left.

## 9. Checklist

- [x] Conventions suite green, twice, verbatim quotes in §2 (34 passed; the
      named `test_every_referenced_line_is_not_blank` green standalone).
- [x] `ruff check src tests` — `All checks passed!`.
- [x] `docs/README.md:172` citation verified truthful (region anchor into
      `_kill_tree`'s win32 branch; exact-line nuance `:1336` recorded §3).
- [x] sandbox.md / skills.md / mcp-server.md / security-model / ADR-0051
      swept against `base.py::SandboxSession` and `mcp.py`'s sandboxed spawn —
      no contradictions; every changed anchor read on both sides.
- [x] Per-doc findings recorded honestly (§6: 4 + 1 nuance; all
      conventions-green, all one-line fixes for a central pass).
- [x] Out-of-scope findings recorded (§7: the 5s-prose set with prior filing
      history; mid-flight landings; W5-IMPL-01's item closed).
- [x] No `git add`/`git commit`/`git push`/`git stash`/`git checkout --`
      performed; no file outside this report written.
