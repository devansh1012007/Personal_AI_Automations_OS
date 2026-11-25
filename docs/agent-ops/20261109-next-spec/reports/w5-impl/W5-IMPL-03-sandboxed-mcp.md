<!-- ORCH-DIGEST:START -->
agent: W5-IMPL-03 | task: T-C5-3 (C5.3) | status: DONE
code_commits: none (orchestrator commits after review) | report_commit: pending
files_changed: 6 (src/paa/skills/adapters/mcp.py, tests/skills/test_mcp.py,
  docs/modules/skills.md, docs/security/security-model-and-trust-boundaries.md,
  docs/adr/0051-facade-and-capability-tokens.md, docs/interfaces/mcp-server.md)
  + this report; src/paa/skills/registry.py named in the spec row but not needed
  (no registry code references the adapter; the seams are constructor parameters)
contracts_changed: none frozen (adapter constructor gains optional
  sandbox/permission_mode/mandate_permissions/ledger seams, additive with
  defaults; MCPClient.start now requires the containment backend — the old
  unsandboxed start is deleted by design, and no production consumer existed)
tests: added 4, changed 0, removed 0; scoped runs all green (skills 121, of
  which test_mcp.py 26; mcp_server+coder 532 passed + 1 named-reason skip;
  conventions 34; ruff clean on the write-set; mypy 0 in 173 files)
ownership_violations: none (write-set per brief; sandbox/, tests/sandbox/,
  tests/observability/, README.md, docs/README.md untouched)
deferred_dependencies: none new (W7/C7.3 recorder is the adapter's first
  production consumer; the SandboxError-escape reading is recorded for it here)
out_of_scope_findings: 3 (PROPOSED-with-no-FAILED on a session-open
  containment failure — judged deliberate, recorded for C7.3; stale
  "completely unwired" claim in docs/plans and audits/MCP_AND_PLUGINS.md:91
  for the W4 H-2 pass; repository-map.md's one-line mcp.py row predates the
  sandbox and is fine for the central pass)
temporary_mitigation: no
deep_issue_flag: no (implementation verified element-by-element against the
  spec row; every claim re-checked in-tree, not inherited)
least_confident_about: the SandboxError-escape reading — a session-open
  containment failure propagates without a FAILED row, which I judged consistent
  with the module's containment-is-not-a-workload-failure philosophy and the
  ledger's crash-between-event-and-work rule, but the MCP server chose a
  broader catch for the same shape; plus the scope call to fix four
  sandbox-cite line numbers inside my write-set's own doc hunks
confidence: high (every element of the row verified with named tests and line
  evidence; all scoped gates run by me after my own edits)
last_comms_line_read: 2026-11-22T00:00:00Z (WAVE-START; no later comms lines
  were available to this subagent)
<!-- ORCH-DIGEST:END -->
# Sandboxed MCP client — W5-IMPL-03 — T-C5-3 (C5.3), wave 5, 2026-11-22

Task: spec `docs/plans/2026-11-21-next-spec-2.md` §4 row C5.3 — "**Sandboxed
MCP client:** spawn via `SandboxSession`; tool allowlist from `manifest ∩
mandate ∩ policy`; delete the false-containment docstring; per-call policy
denial is a ledger event", verified by "`tests/skills/test_mcp.py` extended:
child runs under sandbox facts; denial path; no ambient env/FS; secrets via
the existing proxy only" (gap #3, the audit's #1 blocker). The original
implementation agent finished the code but ran out of context before its
report; this document is the review-and-finish: I verified the implementation
in full myself, filled three small real gaps (4 tests, 1 comment fix, 4 doc
line-cites), and recorded the evidence. No commit is made here; the
orchestrator commits after review.

## 1. Method and what was actually verified myself

- Read `AGENTS.md`, the spec row, `src/paa/skills/adapters/mcp.py` (963
  lines) and `tests/skills/test_mcp.py` **in full**, plus the contract
  sources the implementation claims to sit on: `sandbox/base.py` (the
  `SandboxSession`/`SandboxSpec`/`build_child_env` contract),
  `sandbox/subprocess_backend.py` (`open`, `SubprocessSandboxSession.read/
  write/close`, `killed_reason="closed"`, the process-group tree kill),
  `coder/journal.py` (`LedgerToolJournal.before/denied/after`),
  `ledger/recovery.py` (`_OUTCOME_TYPES`), `mcp/server.py` (the sibling
  server's own denial journalling), `skills/secrets.py` (`SecretValue.
  reveal`), and the old version of the adapter (`git show HEAD:...`) to
  confirm exactly what was deleted.
- Every claim in this report was re-verified in this working tree; nothing is
  inherited from the orchestrator's summary. Two of its numbers needed
  correcting (§6): `test_mcp.py` as landed had **22** tests in 845 lines —
  117 was the whole `tests/skills` directory.
- The `ledger` fixture the denial/cancel tests use is the real shared one
  (`tests/conftest.py:52`: a real `LedgerStore` over a per-test `Database`),
  and the sweep assertions call the real `find_interrupted_calls` — the
  rows are real ledger rows, not mocks.

## 2. The design as landed — verified element by element

### 2.1 Spawn via `SandboxSession`

`MCPClient.start` (`mcp.py:220-285`) requires the containment backend and
builds the `SandboxSpec` at `mcp.py:263-271`: `command` is argv (never a shell
string), `workspace_path` is the child's cwd jail — a fresh
`tempfile.mkdtemp` scratch the client owns and removes on close, unless the
caller supplies one — and `env=dict(env) if env else {}` is the **explicit
allowlist** (the host environment is never a source; `build_child_env`
builds from empty, `sandbox/base.py:660-713`). `sandbox.open(spec)` at
`mcp.py:273` establishes the duplex session; a failed open cleans the owned
scratch and lets the `SandboxError` propagate **as itself** — a containment
failure is not a workload failure. The wire is the session's: one background
read task over `session.read()` chunks (`mcp.py:336-356`; the session hands
back raw chunks, so `_next_payload` reassembles messages itself), writes via
`session.write()` with the session's `SandboxError`-on-a-dead-pipe mapped to
this client's `ConnectionError` contract (`mcp.py:485-499`). `close()`
(`mcp.py:301-332`) is idempotent, cancels the reader, closes the session —
the backend's **whole-tree** kill — returns the session's final
`SandboxResult` (the honest classification: `killed_reason="closed"`,
`exit_code`, `memory_enforcement`), and fails pending calls.

Evidence: `TestSandboxedSpawn::test_the_child_runs_under_the_sessions_facts`
asserts the **child's own self-report** (the `facts` tool): cwd == the
resolved workspace jail, pid == the session's root child, the deliberate
`PAA_MCP_ALLOWED` arrives while an ambient `PAA_TEST_AMBIENT_LEAK` sentinel
planted in the parent is absent, the whole env is < 20 keys, the child's
`probe.txt` write lands inside the jail, and the close result carries the
backend's honest fields (`backend="subprocess"`, `IsolationLevel.PROCESS`,
`memory_enforcement in ("watchdog","job_object")`, `killed_reason="closed"`).
`test_a_scratch_workspace_the_client_owns_is_removed_on_close` pins the
scratch teardown.

### 2.2 Tool allowlist from `manifest ∩ mandate ∩ policy`

- **manifest leg** — every discovered tool's contract declares
  `required_permissions = [PERM_SANDBOX_RUN, PERM_NET_EGRESS]`
  (`mcp.py:716-724`): `SANDBOX_RUN` is the claw_hub rule (an author who
  forgets it gets a contract that can never be dispatched), egress is the
  honest default for an out-of-process server. The old version declared
  egress alone — the sandbox permission is new, and required.
- **mandate leg** — the constructor seam `mandate_permissions`: `None`
  imposes no constraint, tighten-only when present (`_grants()`,
  `mcp.py:585-597`: `granted &= mandate`).
- **policy leg** — the bound `PermissionMode`'s `.granted`.

`discover()` (`mcp.py:643-680`) **hides** any tool that does not survive
`contract.missing_permissions(granted)` — logged with the mode and the
missing permissions, not hidden silently; discovery never surfaces a
capability the runtime would then refuse to dispatch.
`invoke()` re-checks the same intersection **before the server child is ever
spawned** (`mcp.py:818-842`) — a contract registered under an older policy
and dispatched by a caller that skipped discovery is still refused locally.

Evidence: `TestPolicyAllowlist::test_discover_hides_tools_the_mode_does_not_
grant` (ASK exposes `echo`; LOCKDOWN exposes nothing);
`test_a_denied_call_is_refused_locally_and_journalled` (the discovering
adapter runs under ASK, the invoking adapter under LOCKDOWN; the backend is
a `_NoSpawnSandbox` double whose `open()` **fails the test** — a child that
was never started is the strongest form of "the server never received the
call"); `test_a_mandate_that_omits_the_permission_denies_too` (an empty
mandate denies even under ASK, and the DENIED row's reason names the mandate
as the binding leg — `missing != contract.missing_permissions(mode.granted)`
is exactly the tighten-only comparison that makes that attribution honest,
`mcp.py:824-831`).

### 2.3 Per-call policy denial is a ledger event — the existing vocabulary

The refusal is journalled through the **coder's** `LedgerToolJournal` — the
same class, the same event vocabulary, and the same order the sibling
`src/paa/mcp/server.py` applies to its own tools (server.py:716-758:
`journal.before(ctx)` → gate → `journal.denied(ctx, reason[:200])` → raise;
the adapter mirrors it at `mcp.py:809-842`): `TOOL_CALL_PROPOSED` before
the gate and any effect, `TOOL_CALL_DENIED` as the refusal's outcome row.
The reason is bounded by `_MAX_REASON_CHARS = 200` (`mcp.py:167`), exactly
as the server bounds its own. The lineage is a fresh correlation per call
(`_journal_ctx`, `mcp.py:599-619` — the same shape the server uses for
session-less calls), `isolation_level` is the **backend's actual** level,
never a requested one, and `_result_sha256` is the identical computation the
server uses (`sha256(canonical_json(payload))`, `mcp.py:951-958` ==
`mcp/server.py:1292-1294`) — an MCP tool call is attested identically
whichever side of the pipe journalled it. Why this vocabulary and not a new
one: `TOOL_CALL_DENIED` is already in the recovery sweep's `_OUTCOME_TYPES`
(`ledger/recovery.py:346-352`) — a denial is an outcome the sweep resolves,
never an unknown to repeat — so reusing it costs zero replay/recovery
plumbing and keeps one tool-call audit family across coder, MCP server, and
MCP client.

Evidence: `test_a_denied_call_is_refused_locally_and_journalled` asserts the
row sequence `[TOOL_CALL_PROPOSED, TOOL_CALL_DENIED]`, the payload's `tool`
and `PERM_NET_EGRESS`-bearing `reason`, and `find_interrupted_calls(rows) ==
[]` — the real sweep over real rows. `TestKillAndCancel` and `TestAdapter`
pin the COMPLETED/FAILED halves of the same family.

### 2.4 The false-containment docstring is gone, and the behaviour inverted

The old `invoke` docstring — "``sandbox`` is unused: an MCP server is its
own subprocess and provides its own isolation boundary, so there is nothing
to mount here" (`git show HEAD:src/paa/skills/adapters/mcp.py`, lines
410-411) — is deleted from the tree. What replaced it is the opposite
behaviour: `discover()` refuses without a sandbox (`mcp.py:652-655`) and
`invoke()` refuses with neither a bound nor a per-call sandbox
(`mcp.py:789-794`), both raising `SkillContractError` — the claw_hub rule
("claw_hub skills require a sandbox; none was provided",
`adapters/claw_hub.py:423-425`), applied to MCP. The rationale is stated in
the module docstring and is the row's own: the bare
`asyncio.create_subprocess_exec` spawn this module used to perform — ambient
environment inherited, no jail, no ceiling, no tree kill — was the audit's
#1 trust gap. There is no unsandboxed path left to label, because there is
no unsandboxed path. Backends that cannot honestly deliver a duplex child
(dry-run, Docker, WSL today) refuse at `open()` with a `SandboxError` naming
themselves — verified present at `sandbox/dryrun_backend.py:127`,
`docker_backend.py:1028`, `wsl_backend.py:272` — and that refusal
propagates through `start()` rather than being collapsed into a workload
failure. Evidence: `TestAdapter::test_invoke_without_a_sandbox_refuses`,
`test_discover_without_a_sandbox_refuses`.

Docstring honesty beyond the row's letter: the module docstring keeps the
subprocess backend's PROCESS-level honesty verbatim ("a SubprocessSandbox is
PROCESS (same kernel, same user: it stops runaway resources and accidental
writes outside the workspace, not a hostile syscall)"), states that the
resource envelope is "whatever the chosen backend enforces", that stderr is
a teardown fact (the session contract carries no `read_stderr`; the backend
captures it in the background and reports it in the final result), and that
the session's wall clock is deliberately unbounded while the per-call
JSON-RPC budget is the real clock. I checked each of these against
`sandbox/base.py`'s own docstrings and code: all accurate.

### 2.5 Secrets via the existing proxy only

`_invocation_env` (`mcp.py:731-763`) resolves the contract's
`invocation.env_allowlist` names **through the bound secret proxy** and into
the spec's explicit env allowlist — `reveal()` is the one greppable way out
of redaction (`skills/secrets.py:121-123`) and this is its deliberate use
site; the value goes nowhere but the spec env. A contract that names
secrets with no broker bound is refused **before any ledger row**
(`mcp.py:799-807` — an unformable call costs no lineage, the same rule the
MCP server states for session resolution), and the parent environment is
never a source. Evidence: `TestSecretDelivery::
test_a_secret_reaches_the_child_only_through_the_proxy` — the same *name*
carries a different value in the parent's ambient environment than in the
broker, and the child sees the broker's value; `test_an_env_allowlist_
without_a_broker_refuses`; and the ordering pin I added (§3).

### 2.6 Kill and cancel

`TestKillAndCancel::test_close_mid_conversation_leaves_no_orphan` — closing
a live conversation kills the child classified (`killed_reason="closed"`,
`exit_code < 0`), returns well inside the 5-second budget, and
`_wait_until_pid_gone` polls the kernel (`os.kill(pid, 0)` — signal 0 is the
probe) until the process is gone. `test_cancel_mid_call_journals_a_
classified_outcome_and_kills_the_tree` — the `tree`-mode server spawns a
**real grandchild sleeper** and hangs; the cancel lands mid-call, the
adapter re-raises `CancelledError` promptly (< 5 s) **after** the FAILED row
(`mcp.py:848-855`), the session teardown has already killed the tree, and
both root and grandchild PIDs are polled until gone; the sweep sees
`[PROPOSED, FAILED]` with `result_sha256 is None` and never re-queues the
call as interrupted.

### 2.7 The robustness paths are real, and the tests are not vacuous

All five process-backed failure paths assert on their distinct exception
types, not a generic catch: crash mid-request → `ConnectionError`
(`test_server_crash_mid_request_raises_connection_error`); malformed JSON
line skipped → the good reply still resolves
(`test_malformed_json_line_is_skipped`); unknown response id ignored
(`test_unknown_response_id_is_ignored`); request timeout → `TimeoutError`
(`test_request_timeout_raises_timeout_error`); call after close →
`ConnectionError` (`test_call_after_close_raises`); Content-Length read
framing autodetected while writing line-framed
(`test_content_length_framing_autodetected_on_read`). The fake server is a
real child process with argv-chosen failure modes; no mock of the process
layer exists anywhere in the file. The one gap I found in this section — the
chunk-reassembly and bad-header properties were claimed but only
timing-exercised — is what my new `TestFrameReassembly` closes (§3).

## 3. What I changed and why (small gaps only; no restructuring)

1. **`tests/skills/test_mcp.py` — 4 new tests** (file 845 → 952 lines, 22 →
   26 tests; `tests/skills` 117 → 121 passed):
   - `TestFrameReassembly::test_a_body_may_span_chunks_and_messages_may_share_
     one` — feeds a whole `Content-Length` frame **one byte at a time** (the
     finest chunking a pipe can deliver) and asserts no payload emerges
     early, then two line-framed messages plus a header-framed third in
     **one** chunk; pins the module docstring's claims ("a body may span
     chunks; several messages may share one") deterministically — the
     process-backed tests exercise the framing paths, but which chunks the
     pipe delivers is timing.
   - `test_extra_header_lines_are_drained_not_parsed` — canonical LSP order
     (`Content-Length` first, extras after); the drain consumes the extra
     headers without ever handing them to the dispatcher as payloads.
   - `test_a_bad_content_length_hands_the_dispatcher_an_unparsable_payload`
     — a `Content-Length: nan` header becomes an empty payload the
     dispatcher's malformed-line path skips, so one bad frame cannot kill
     the loop (the code's own stated design, previously untested).
   - `TestSecretDelivery::test_a_denied_call_never_resolves_a_secret` — the
     docstring's security claim is an **ordering** fact ("a refused call
     never resolves — never even touches — a secret value"); the test hands
     a `_TouchFailsBroker` whose `get_secret` failing the test IS the
     assertion, under LOCKDOWN with a contract that names a secret. If
     anyone ever reorders `_invocation_env` ahead of the gate, this goes
     red. The denial row sequence is asserted alongside.
2. **`tests/skills/test_mcp.py` header comment (truth fix)** — the
   `calls.log` paragraph called itself "the denial tests' witness", but the
   denial tests actually use the `_NoSpawnSandbox` double (stronger: a call
   refused locally leaves no process at all). Reworded to describe the log
   as the post-mortem record of what actually reached a child.
3. **`docs/security/security-model-and-trust-boundaries.md` — 4 line-cite
   corrections** in hunks this task's in-tree doc diff had renumbered:
   `sandbox/base.py:55` → `:59` (`class IsolationLevel`), `:306` → `:628`
   (`def assert_inside_workspace`), the first `:338` → `:660`
   (`def build_child_env`, for the bootstrap-keys-plus-`spec.env` sentence),
   and `sandbox/base.py:275` → `config.py:467` (the `max_capture_bytes`
   setting — the constant no longer lives in base.py at all). The old
   numbers were renumbered without being read: they passed the conventions
   lock because their lines exist and are non-blank, which is exactly the
   "wrong without being broken" class the lock's own docstring names as
   human work. Each new target was read (`sed -n` against the live file)
   before writing it.
4. **Nothing in `src/paa/skills/adapters/mcp.py`** — the implementation is
   correct as landed; I verified rather than touched it. `src/paa/skills/
   registry.py` (named in the spec row's Files column) needs no change: no
   code in it references the MCP adapter (grep: zero hits), and the
   integration seams are the constructor's `sandbox` / `permission_mode` /
   `mandate_permissions` / `ledger` parameters, bound by the caller.

## 4. Commands run and results (verbatim summaries)

| Command | Result |
|---|---|
| `python -m pytest tests/skills -q -p no:cacheprovider` | two progress lines to `[100%]`, exit 0 — an extra `-q` over `addopts`' own suppresses the summary line; the count line is the same suite without it |
| `python -m pytest tests/skills -p no:cacheprovider` | 121 passed in 4.00s (117 before my additions) |
| `python -m pytest tests/skills/test_mcp.py -p no:cacheprovider` | 26 passed in 2.92s (22 before my additions) |
| `python -m pytest tests/mcp_server tests/coder -p no:cacheprovider` | 532 passed, 1 skipped in 23.02s |
| the same run with `-rs` | SKIPPED [1] tests/coder/test_shell_tools.py:381: "shell present; start-failure path not applicable" |
| `python -m pytest tests/conventions -p no:cacheprovider` | 34 passed in 108.08s (0:01:48) |
| `python -m pytest tests/conventions/test_docs_references.py -p no:cacheprovider` | 5 passed in 1.70s |
| `python -m ruff check src/paa/skills tests/skills` | All checks passed! |
| `python -m mypy src/paa` | Success: no issues found in 173 source files |

The one skip is a named-reason platform guard (the shell-present host cannot
exercise a start-failure path), not a quiet opt-out.

## 5. Out-of-scope findings (not fixed here, per brief)

1. **A session-open containment failure escapes `invoke` without a FAILED
   row.** If `sandbox.open()` raises `SandboxError` after the PROPOSED row
   (backend refused the session, process failed to start), the exception
   propagates as itself — the module's stated philosophy ("a containment
   failure is not a workload failure") — leaving PROPOSED for the recovery
   sweep to classify as interrupted/unknown (never auto-repeated; the
   ledger's crash-between-event-and-work rule). This differs from the MCP
   server's broader `except Exception` belt-and-braces for the same shape,
   and from the adapter's own TimeoutError/ConnectionError/
   SkillContractError/CancelledError catches. I judged it deliberate and
   consistent (the docstring never claims a FAILED row for a refused
   *open*), and did not restructure working code for it — but W7/C7.3
   (the recorder, the adapter's first production consumer) should make the
   choice explicit there: journal FAILED on SandboxError too, or let the
   sweep classify. Recorded, with a leaning, not decided.
2. **`docs/plans and audits/MCP_AND_PLUGINS.md:91`** still says "MCP support
   already exists and is completely unwired" — stale now (the client is
   sandboxed and the server is W4/C4.5), but that directory's truth pass is
   the W4 H-2 / central docs task, not C5.3.
3. **`docs/architecture/repository-map.md:92`** describes `mcp.py` as
   "`MCPClient`, `McpServerAdapter` — stdio JSON-RPC 2.0" with no mention of
   the sandbox session — not a contradiction (still true), a candidate for
   the central docs pass.

## 6. Deviations

- **`src/paa/skills/registry.py` not touched** (named in the spec row's
  Files column): the row anticipated registry wiring; none was needed — no
  registry code references the adapter, the USA does not dispatch MCP yet,
  and the row's semantics land entirely in the adapter's constructor seams.
- **`MCPClient.start()` changed shape** (now requires the backend, plus
  keyword-only containment knobs) instead of gaining an alternate path: the
  unsandboxed path was deleted, not deprecated — the row's "delete the
  false-containment docstring" intent and the repo's no-quiet-downgrade
  rule. No production consumer existed (grep: only tests and
  `adapters/__init__`'s re-export), so the blast radius is the test file,
  which was updated in the same write-set.
- **The orchestrator's pre-review summary said "845 lines, 117 tests" for
  `test_mcp.py`**: as landed the file had 22 tests in 845 lines — 117 was
  the whole `tests/skills` directory. Both numbers are restated correctly
  above (now 26 / 952, and 121).
- **Doc-scope interpretation**: the brief scoped doc fixes to "truth fixes
  about the MCP client"; I additionally corrected four sandbox/validation
  line-cites that this task's own in-tree doc diff had renumbered *wrongly*
  (they are in my write-set, and leaving known-wrong renumbers in a diff
  this task ships would be exactly the "re-numbered without being read"
  failure the docs lock's docstring warns about). Each fix is a cite-only
  change; no prose was altered. The four cites in hunks that remain only
  approximately anchored (`ast_scanner.py:125`–`:247`, `engine.py:238`) are
  the W5-IMPL-01 write-set's, left for their report's declared central pass.
- **`docs/interfaces/mcp-server.md`'s in-tree diff verified, not extended**:
  its additions (the Claude Code hook section, the scripted-smoke
  description) are the server-side/W5 pre-flight content; I verified the
  smoke claims against the committed `scripts/mcp_stdio_smoke.py` (the
  three-tool catalogue assertion at line 159, `SMOKE OK` at 220, the
  non-JSON-line guard at 99, exit 1 on failure at 276) — all true — and made
  no edits there.

## 7. Checklist

- [x] Spawn via `SandboxSession` (spec construction at `mcp.py:263-271`,
      `sandbox.open(spec)` at `:273`, wire over session `read`/`write`,
      close = whole-tree kill with the backend's honest classification).
- [x] Tool allowlist `manifest ∩ mandate ∩ policy`, tighten-only; discover
      hides ungranted tools, invoke re-checks **before** the spawn.
- [x] False-containment docstring deleted from the tree; behaviour
      inverted to refusal-without-sandbox on both discover and invoke.
- [x] Per-call policy denial journalled as `TOOL_CALL_DENIED` through the
      coder `LedgerToolJournal` — the server's own vocabulary and order
      (`PROPOSED` → gate → `DENIED`); the sweep treats it as an outcome.
- [x] Child runs under sandbox facts (self-report witness: cwd jail, root
      pid, env allowlist, no ambient leak, write lands in jail).
- [x] No ambient env/FS leaks (env built from empty; `SandboxSpec._reject_
      ambient_env_smell` guards the spec side; the canary test is the proof).
- [x] Secrets via the existing proxy only (broker value wins over the
      ambient same-name; allowlist-without-broker refuses; a denied call
      never touches the broker — new ordering pin).
- [x] Robustness paths real and distinct (crash/malformed/unknown-id/
      timeout/call-after-close/CL autodetect; chunk reassembly now also
      deterministic).
- [x] Kill/cancel: close kills the tree, classified, < 5 s, no orphan;
      cancel mid-call journals FAILED, kills the tree, no re-queue.
- [x] Docs checked for truth (4 wrong cites fixed; ADR-0051's two cites
      verified against the live file; skills.md rows verified; the
      mcp-server.md smoke claims verified against the committed script).
- [x] Scoped suites green with verbatim counts; ruff, mypy, conventions all
      green on the write-set.
- [x] No `git add`/`git commit` performed; write-set respected (no touches
      to sandbox/, tests/sandbox/, tests/observability/, README.md,
      docs/README.md).
