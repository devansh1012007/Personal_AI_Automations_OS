<!-- ORCH-DIGEST:START -->
agent: W7-ORCH | task: C7.3 (H3.5, recorder) | status: DONE
code_commits: (this commit) | report_commit: (this commit)
files_changed: 106 (src/paa/recorder/{__init__,record,tools,proxy,core}.py, src/paa/cli/record.py, src/paa/cli/main.py, src/paa/attest/bundle.py, src/paa/observe/observer.py, tests/recorder/{test_record,test_proxy,test_core,test_cli}.py, docs/interfaces/{record-format,attested-bundle-spec}.md, README.md, docs/README.md, AGENTS.md, this report, docs citations re-anchored across 95 files after the cli/main.py (+6) and README.md (+3) shifts)
contracts_changed: new standalone artifact format paa-record/1 (own hash chain over its own envelope; no ledger event types, no pipeline protocol keys). Attest builder gained two additive, validated parameters (extra_evidence, observed_command_rows) — format unchanged, all existing bundles verify unchanged
tests: added 50 (tests/recorder 50); full suite 4,769 collected = 4,525 passed / 243 env-gated skipped / 1 xfailed, 0 failures, exit 0 (C7.2 close: 4,475/244/1x — +50 exactly the recorder suite; the skip delta is one previously textual-gated TUI test that now runs because the fresh sandbox has textual installed, so the run is strictly stronger, not weaker)
ownership_violations: none
deferred_dependencies: none
out_of_scope_findings: 3
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: the TTY stdin relay is pinned by review only (tests cannot fake a TTY here; the non-TTY path — the one tests, CI and scripted harnesses take — is pinned at every door); and the sandbox session's session_root_died warning reports exit_code=128 while the collected result carries the true 0 — pre-existing C5.2 session logging, recorded as an out-of-scope finding
confidence: high (gates green: full pytest 4,525/243/1x/0 exit 0, mypy 197 files clean (+6: the recorder package and its CLI), ruff clean, conventions incl. the docs lock green after re-anchoring)
last_comms_line_read: n/a
<!-- ORCH-DIGEST:END -->
# Implementation report — W7-ORCH — C7.3 (H3.5) `paa record`, the harness-neutral flight recorder

## 1. Task and acceptance criteria (restated)

SPEC 2 §6 C7.3 / ROADMAP H3.5's recorder half: `paa record <harness-cmd>` —
the sandbox shim (SandboxSession) + the MCP policy proxy (the harness's tool
calls route through PAA, policy-checked, journaled — the "one deterministic
policy across harnesses" second half) + a hash-chained run record + the
agent-attested bundle. Done when (the spec's own row): *a Claude Code /
Codex / Gemini CLI session runs under `paa record` and produces a verifying
bundle; unsandboxable effects honestly labelled* — plus the campaign gates
(full pytest, mypy, ruff). No real harness CLI exists in this sandbox, so
the done-when runs through the scripted fake harness exactly the way C7.1's
did — but here the fake harness is a **real subprocess** of the real CLI
speaking real MCP over real stdio, so the pin covers the whole chain, not a
Python-level seam.

## 2-4. Problem, root cause, approach

After C7.1/C7.2 the evidence ladder had a watcher (T1) and an artifact a
reviewer can verify offline, but nothing that *mediates*: a harness's tool
calls went wherever the harness sent them, under whatever policy that one
harness happened to enforce. C7.3 closes that gap with one record format
(`paa-record/1`) and three doors over it:

- **the record** (`src/paa/recorder/record.py`): append-only NDJSON, each
  event's digest covering `{schema, seq, at, kind, data, prev}`, `prev`
  chaining to the previous digest (`"genesis"` seeds the first — the
  attest chain's own rule). Two-phase summary (`record.json`): `started`
  before the harness spawns (the crash marker), `final` after the last
  event. Crash-safe by construction: one newline-terminated line per
  event, flushed and `fsync`-ed; a final line without its newline is a
  **torn tail** and `paa record verify` reports it, never drops it.
- **the proxy** (`src/paa/recorder/proxy.py`): an MCP stdio server over
  the shared framing kit (`paa.mcp.protocol` verbatim — one framing
  codebase now serves the C4.5 server, the C5.3 client and this proxy).
  Per call, in order: journal `tool_call` → validate shape → decide with
  the **run's recorded policy** (rebuilt from `record.json`, the single
  source — two proxies cannot disagree) → execute (`bash` through the
  sandbox one-shot; file tools jailed in-process) → journal
  `tool_result`/`policy_denied` → answer. The unattended **ask** resolves
  to a denial recorded as `resolution="denied_unattended"` with the raw
  decision kept beside it — never hidden, never upgraded to a quiet allow.
  No auth token, stated in the docstring as a reasoning: an ephemeral
  per-run stdio child grants nothing local process authority does not
  already grant; the honest boundary is the chain and the signature.
- **the core** (`src/paa/recorder/core.py`): the harness under a real
  `SandboxSession` (the C5.2 duplex contract, held for the whole life —
  this is the spec's "sandbox shim"), stdout relayed live to the operator,
  snapshots before/after, the observe bundle's classification vocabulary
  reused verbatim (`classify_outcome`), the T1 observe bundle written
  beside the record, and the attested bundle built at close.

The load-bearing honesty decision: **mediated and observed are never
collapsed.** Tool events exist only for calls that rode the proxy; the
unmediated harness effects are visible only as manifest/git deltas in the
observe bundle, and the record never infers a tool call from a file change.
The summary's `honesty` block states it once, where the code enforces it.

A design correction made during implementation, worth recording: the
two-writer handoff (core writes before and after the harness's life, the
proxy writes in between) first refused appends whose on-disk tail had moved
— which made the *designed* handoff impossible (the core's in-memory tail
is always stale after the child runs). The semantics now: each append takes
the `flock`, re-reads the tail, **verifies that tail's own digest**, and
extends it — a second writer's events extend the same chain, a tampered or
corrupt tail is refused, and the two-writer test pins the handoff with the
chain arriving intact.

## 5. What changed

- `src/paa/recorder/record.py` (new): the format leaf and both doors —
  `RunRecord.create/open_existing/append/finalise`, the closed event
  vocabulary, `_scan_events` (verify-at-load), `verify_record` (six checks,
  each reported independently; `ok` = every door *and* finalised), the
  summary with the policy in force, the `honesty` block and the
  `containment` block.
- `src/paa/recorder/tools.py` (new): the three v1 tools (`bash`,
  `read_file`, `write_file`) with the split stated in code: sandbox one-shot
  vs jailed in-process vs jail-refused, each a different `executed` value.
  The name `bash` is deliberate — the policy engine's mutating globs speak
  `bash*`, so a tool named `run_command` would quietly escape PLAN mode.
  Relative tool paths resolve **workspace-relative**, not CWD-relative (a
  gap found by the oversize-read test before it could become a bug).
- `src/paa/recorder/proxy.py` (new): the MCP policy proxy (above).
- `src/paa/recorder/core.py` (new): the sequencing + `_StdinRelay` (a daemon
  thread pumping the TTY into the session — the interactive door; the
  non-TTY path is the one tests pin) + `child_env_for_run` (the
  `PAA_RECORD_*` markers, including the ready-to-paste
  `PAA_RECORD_PROXY_CMD` stanza) + `_build_attested` (the close-time bundle).
- `src/paa/cli/record.py` (new) + `main.py` wiring: `paa record run|proxy|
  verify`. `--json` implies no relay (a machine door must not be corrupted
  by the harness's stdout; the artifacts carry the transcript). Exit 0 only
  for a clean exit — a crashed harness is a successful recording of a
  failed subject, the observe CLI's own distinction.
- `src/paa/attest/bundle.py` (+2 parameters, additive): `extra_evidence`
  (artifact blobs whose rows must name their own `kind`; the digest is
  stamped from the stored bytes) and `observed_command_rows` (each row's
  `evidence_sha256` **must name a blob in this bundle** — an observed row
  backed by no bytes is refused, loudly). This also closes C7.2's recorded
  out-of-scope finding: the command-row extractor exists now, framed by the
  recorder so journal rows and bundle rows agree on what a "command" is.
- `src/paa/observe/observer.py`: the git helpers extracted to module level
  (`git_query`/`git_before`/`git_after`, `_GitBefore` → public `GitBefore`)
  so the recorder asks git the same questions through the same containment
  instead of drifting into a second vocabulary. ObserverCore delegates; no
  behaviour change; all 57 observe tests pass untouched.
- Tests (`tests/recorder/`, 50): the chain (genesis, linkage, digest
  covering the timestamp — a flight record is a journal, so its honest
  determinism property is "backdating breaks the chain", not "same inputs
  same root"), tamper (substituted payload, truncated tail, reordered
  sequence, forged summary, foreign schema), crash discipline (the
  `started` marker, torn tail, append-onto-torn refused), the two-writer
  handoff, the proxy's protocol surface (handshake, toolset, the
  journalled-then-decided-then-executed order, ask→denied_unattended,
  deny-wins, PLAN-mode refusals, jail refusals as rows, failed commands as
  `isError` results, the framing loop over real streams), and the done-when
  as a real chain: fake harness (real child) → proxy (real
  `paa record proxy` subprocess over real stdio) → flight record (real
  chain on disk) → attested bundle → `verify_bundle` green — plus the
  chaos matrix (self-SIGKILL → `signalled`, wall-clock overrun →
  `timed_out`, both finalised and verifying; a non-cooperating harness
  gets the T1 floor with zero proxy events) and the honesty labels (the
  unmediated file in the manifest diff and in *no* tool event).
- Docs: `docs/interfaces/record-format.md` (new, indexed in `docs/README.md`);
  the attest spec gains "The recorder's rows (C7.3, additive)"; the README
  command surface gains `paa record run` + `paa record verify`.
- Conventions: 95 docs files' `cli/main.py`/`README.md` line citations
  re-anchored (difflib-mapped pure insertions against HEAD, verified by the
  docs lock going green).

## 7. Tests and gates

- `tests/recorder`: 50/50 green.
- Full suite: **4,769 collected = 4,525 passed / 243 env-gated skipped /
  1 xfailed, 0 failures, exit 0** (C7.2 close: 4,475/244/1x → +50, exactly
  the recorder suite; the skip delta is one textual-gated TUI test that now
  runs — the fresh sandbox has textual installed, so the gate is strictly
  stronger).
- `mypy src/paa`: 0 issues in 197 files (+6: the recorder package and its CLI).
- `ruff check src tests`: clean.
- Conventions incl. the docs-reference lock: green (after the re-anchoring
  above; one latin-1 archive file excluded from rewriting —
  `docs/agent-ops/20261106-memory-remembering-agent/COMMS.md` — which cites
  no affected file).

## 13. Out-of-scope findings

- The subprocess session's `session_root_died` warning (C5.2's
  `_watch_exit`) reports `exit_code=128` while the collected result carries
  the true exit code (0) — the warning's own account disagrees with the
  result it sits beside. Cosmetic for the recorder (classification reads
  the result), but the sandbox's own log should agree with itself; for a
  C5.2 follow-up.
- The recorder's PR story stops at the artifact: `paa attest pr`'s paste-
  ready body is what the operator carries; the recorder does not yet emit a
  PR body of its own (the C7.2 helper is reusable verbatim on the built
  bundle). Wiring it into the run's close output is a small follow-up.
- The proxy's `bash` tool runs commands under the subprocess backend at
  PROCESS isolation on this host — real containment waits for the docker
  backend, which is the backend story C5.2 already states, now visible
  from one more surface.
