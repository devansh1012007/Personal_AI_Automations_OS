<!-- ORCH-DIGEST:START -->
agent: W1-REV-02 | scope: wave 1b T-N1-1 fs_read + T-N1-3 shell | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 3 | MINOR 4 | NIT 5
claims_audit: 22 claims checked / 3 unsupported-or-false (walk-truncation-marked, grep-file-or-dir, canary-in-ledger)
band_aids_found: none
top_3_risks: default PROCESS shell reads .env and writes outside ws (honest but uncontained) | silent 10k walk truncation presents incomplete grep/glob as complete | real shell execution never ran on this host (pwsh absent, 7 slow tests skipped)
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WORTH-IT 1
reversal_check: reasoned-only (read-only mandate; probes run, no code edited)
confidence: high-medium (static/jail/validation high; real pwsh execution not run here)
<!-- ORCH-DIGEST:END -->
# Code review — W1-REV-02

## 0. Method

Blind pass first: read `src/paa/coder/fs_read_tools.py` (623 lines),
`src/paa/coder/shell_tools.py` (608 lines), both test files (756 + 475 lines),
and the frozen contracts they build on — `src/paa/coder/workspace.py`,
`sandbox/base.py:build_child_env` + `Sandbox.run`, `sandbox/subprocess_backend.py:283-502`,
`coder/permissions.py`. Ran the scoped gates, then read the two implementer reports
(`W1-IMPL-03-N1-1.md` @ a74f5f0, `W1-IMPL-05-N1-3.md` @ 3a99108) for the claims audit.
Red-teamed with three throwaway probe scripts in `%TEMP%\opencode` (never in the repo,
no source edited).

- [RAN `.venv/Scripts/python.exe -m pytest tests/coder/test_fs_read_tools.py -q` -> **57 passed, EXIT 0**]
- [RAN `.venv/Scripts/python.exe -m pytest tests/coder/test_shell_tools.py -q` -> **24 passed, 7 skipped** (all `slow`, shell-presence gates), EXIT 0]
- [RAN `ruff check src tests` -> **All checks passed, EXIT 0**]
- [RAN `mypy src` -> **3 errors, all pre-existing** `subprocess_backend.py:548` (`os.killpg`/`getpgid`/`SIGKILL` Windows-fold), none in audited files, EXIT 1]
- [RAN `pytest tests/conventions -q` -> **34 passed, EXIT 0** (the sibling blank-citation that was red when both implementers ran is now resolved by ORCH commit 353e2c4)]

## 1. Blind-pass findings

**What is right (verified, not assumed).** Jail-then-protect order holds on every
disk-touching path: `fs_read_tools.py:158,161`, glob pattern-base + per-entry
(`:415,427,431`), grep root + per-file (`:513,531,535`), shell cwd (`shell_tools.py:456,466`).
Escape beats protection (`../.env` reports the escape — test `test_escape_beats_protection`
at `test_fs_read_tools.py:436`). Read cap reads `MAX_READ_BYTES+1` and slices
(`fs_read_tools.py:252,258`), so the file is never fully loaded to discover truncation;
incremental `utf-8-sig` decode structurally separates cap-split characters from invalid
bytes (`:183-211`) — the mutation-verified design choice, genuinely good. Shell argv is a
frozen tuple, `shell=True` appears nowhere in either module, timeouts are *refused* above
600 rather than clamped (`shell_tools.py:186-191`), env is built from the allowlist only
(`:194-225` + `build_child_env` builds up from empty, `base.py:550-602`), and
`network_severed` is computed from the *actual* isolation level on both success and refusal
paths (`shell_tools.py:330,376,561-564`). Permissions: read names match no mutating glob,
`bash` matches `bash*` (`permissions.py:127-137`) — PLAN denies bash, prompts in ASK. [VERIFIED]

**What is wrong.** Three MAJORs below: grep on a single file silently returns zero
matches; the 10 000-entry walk cap truncates silently with `truncated: false`; and the
default PROCESS backend gives the shell full host FS reach (read `.env`, write outside the
workspace) — the last one the implementer documents honestly, the first two are undocumented.

Two implementer claims did not survive verification: "truncation always marked, never
silent" is false at the walk cap, and grep's schema promises "File or directory" while
only directories work. Details are in §2/§4.

## 2. Claims audit

**W1-IMPL-03 (T-N1-1).** 57 tests pass [VERIFIED — ran it, 57 passed]. Ruff 0 / mypy 0 on
file [VERIFIED]. Jail order, 64 KB + marked truncation, CRLF/BOM/binary refusal,
protected-with-frozen-reason, symlink escape [VERIFIED — code + tests + my probes P5/P6].
Hypothesis property exists and runs [VERIFIED `test_fs_read_tools.py:330-362`] but its
strategy (`:294-316`) generates *strings only* — no symlink segment, so "symlink never
resolves outside" is covered by the 3 explicit symlink tests, not by the property.
**Partial.** "Truncation always marked" **FALSE** at the walk cap (§4 MAJOR-2).
Grep "file or directory" **FALSE** (§4 MAJOR-1). LOCKDOWN posture: the documented
deviation (coder `PermissionMode` has no LOCKDOWN; core LOCKDOWN grants only
`SANDBOX_RUN`) is accurate [VERIFIED `permissions.py:40-56`] and the substitute pins are
reasonable — accepted.

**W1-IMPL-05 (T-N1-3).** 24 passed + 7 skipped [VERIFIED — ran it, matches exactly].
Ruff own-files 0, mypy 0 new [VERIFIED]. Frozen argv, 120/600 refuse-not-clamp, env
canary test, isolation reported, honest `network_severed=False`, jail-then-protect cwd,
mutating permissions [VERIFIED — code + unit tests]. "Canary absent from output/**ledger**"
— ledger half **unsupported** (no ledger writer exists in N1.3; test checks output/JSON
only). "Combined 64 KB cap" is char-based, not byte-based (§4 MINOR-1). The powershell-5.1
probe [UNKNOWN — throwaway, never committed, not reproducible from repo artifacts; my own
probes failed to start the shell because `pwsh` is absent on this host, `shutil.which("pwsh") -> None`].

## 3. Long-run test (independent answers)

- **Source.** Both modules are stdlib-only, no new dependency, no ambient state; the only
  cross-module coupling is frozen `workspace.py` + `Sandbox`. N1.5 registration will import
  `build_fs_read_tools`/`FS_READ_TOOLS`/`build_shell_tools` — the factories are shaped for
  that. [VERIFIED against `build_runtime_tools` pattern]
- **Load.** Per-read work is `cap+1` bytes; per-enumeration work is bounded by the 10k
  walk cap (but see MAJOR-2 — bounded yet silent); shell output is bounded by backend
  per-stream draining + tool combined cap; timeout ≤600 bounds hangs. A grep over 10k
  files does ~10k × (2 `resolve()` + capped read) — heavy for one agent turn but finite,
  and the tools are stateless so concurrent calls don't share mutable state. The shell's
  `_read_capped` drains unbounded output without storing (`subprocess_backend.py:447-488`)
  — CPU/bandwidth cost of `yes`-style output is unbounded until the timeout; inherited
  sandbox behaviour, flagged for awareness not action.
- **Hostile input.** Oversized/binary/non-UTF-8/symlink/drive/UNC/control-char/malformed-args
  all probed or test-pinned (see §5). Model-supplied regex (grep) and translated glob
  regex are backtracking `re` with no deadline — pathological patterns can stall the turn
  (implementer discloses this as ReDoS residual; per-file work is bounded but per-line
  worst case is exponential). My probe Q6 could not reach it because the single-file bug
  returns early. MINOR, correctly not patched with a timeout hack.
- **Operability.** Every refusal names its reason verbatim; shell results carry
  `isolation_level`/`backend`/`network_severed`/`duration_ms`/`memory_enforcement` on
  success *and* refusal for Wave-2 journalling; `preflight()` answers before exec. One
  wrinkle: `located_error` duplicates the human message into `reason` for non-jail
  failures (`shell_tools.py:382`), so a ledger consumer cannot distinguish frozen
  reasons from prose by that field. NIT.
- **Reversal.** Read-only mandate: I edited nothing, so this is reasoned from probes and
  the test suite. Deleting either module fails its test file at import (pinned). Removing
  `is_protected` from `_jail_then_protect` flips 10+ protected tests to leaked content
  (§7 of IMPL-03, verified by reading the assertions). Conversely — and this is the sharp
  edge — **fixing MAJOR-1/MAJOR-2 would not fail any existing test**, because no test
  exercises a single-file grep path or the walk cap. Both fixes need new tests; a fix with
  no failing-before test would be unverifiable, so the new tests should land first.

## 4. Findings

### MAJOR-1 — grep silently returns empty for a single-file path
`src/paa/coder/fs_read_tools.py:523` walks `root` with `os.walk`; on a file path
`os.walk` yields nothing, so a file target returns `count: 0, files_scanned: 0,
truncated: false` — indistinguishable from "no matches". The schema description promises
"File or directory to search" (`:472-476`). [RAN probe P1/Q3 -> both return 0/0].
Fix: in `run`, if the jailed root `is_file()`, search that one file (or correct the schema
description). Add a test with `path: "only.txt"`.

### MAJOR-2 — 10 000-entry walk cap truncates silently, `truncated: false`
`src/paa/coder/fs_read_tools.py:613-621` returns early from `_walk` at the cap with no
signal; `glob`/`grep` payloads set `truncated` only from their match/result limits
(`:434-436,565-567`). A tree over 10k entries (ordinary in a repo with fixtures or
`node_modules`) yields incomplete enumeration presented as complete — the direct violation
of this repo's "truncation marked, never silent" convention, and it contradicts IMPL-03 §8
("truncation is flagged"). [RAN probe P3/P3b with the cap lowered to 2 in-process ->
glob/grep return empty results with `truncated: False`]. Fix: thread a "cap hit" signal
out of `_walk` into both payloads (`truncated: true` + note), and add a test with a
stubbed-small cap or a generated tree.

### MAJOR-3 — default PROCESS shell reads `.env` and writes outside the workspace (documented, uncontained)
`shell_tools.py:139-141` defaults to `SubprocessSandbox` (isolation `PROCESS`). The cwd
jail is not a chroot; the module docstring says so (`:33-36`), `preflight` reports
`filesystem_confined: False`, and every result carries `isolation_level: PROCESS`. I
confirmed the consequence concretely through the same backend the tool uses (python
direct, since `pwsh` is absent here): [RAN probe2 -> `.env` secret in stdout: True,
outside file read: True, outside write created: True]. This is plan-faithful (owner
decision D-10 + honest-reporting design) and **not a code defect in this task**, but it is
the top operational risk: the read tools' protected-path guarantee is void the moment a
shell runs on the default backend. Wave-2 must journal PROCESS isolation truthfully and
must not claim `.env` protection for shell runs; an operator wanting the guarantee needs a
NAMESPACE-or-better backend (or a FS analogue of `require_network_severance`). Not a
blocker for this commit; a hard requirement on the Wave-2 ledger claim.

### MINOR-1 — shell "combined 64 KB" cap is character-based, not byte-based
`shell_tools.py:160-165` measures `len()` on decoded strings. [RAN probe Q2 ->
`_truncate_combined("☃"*40000, "☃"*40000)` returns 65 536 chars = **196 608 bytes**, 3× the
byte cap; with the backend's per-stream byte cap the realistic worst case is 128 KB of
payload bytes from two unicode streams]. The plan says "combined output capped 64KB".
Fix: measure/cut on UTF-8 byte length, or document the char basis in the tool description
(the in-code comment acknowledges it; the model-facing description doesn't).

### MINOR-2 — grep binary/non-UTF-8 skips are uncounted
`fs_read_tools.py:546-551`: a NUL-bearing or non-UTF-8 file `continue`s without
incrementing `files_skipped` (and without `files_scanned`), so `count + scanned + skipped`
doesn't reconcile and a directory of binaries looks "clean". [RAN probe P12 -> skipped
counted the protected + symlink files but not the binary one]. Fix: count them as skipped.

### MINOR-3 — Hypothesis property cannot generate symlinks
The brief's "generated … symlink never resolves outside" is met only by the explicit
symlink tests; the property strategy (`test_fs_read_tools.py:294-316`) draws strings, and a
symlink is a filesystem object, not a string. Either the brief wording or the property
should say so. The explicit tests do cover escape-file, escape-dir (glob), and
into-protected symlinks, so coverage exists — the claim shape is what's wrong.

### MINOR-4 — shell env allowlist overrides sandbox bootstrap keys
`_parse_env` accepts any well-formed identifier, and `build_child_env` does
`env.update(spec.env)` last (`sandbox/base.py:602`), so the model can replace `PATH`,
`HOME`, `SYSTEMROOT`, `PYTHONIOENCODING`, etc. On POSIX that changes how the *child shell
binary itself is located* (exec lookup uses the child env's PATH). Impact is limited —
the shell script already executes arbitrary code, so this is not privilege escalation —
but a planted workspace executable named `bash`/`pwsh` ahead of a replaced PATH is a
real, if narrow, supply-chain wrinkle. Consider stripping bootstrap keys from the model
allowlist or documenting the override as intentional (the sandbox layer already calls
"caller wins" deliberate).

### NIT-1 — `read_file` NUL beyond the cap returns text
Only the first 64 KB is NUL-scanned (`fs_read_tools.py:260`); a NUL at byte 65 537+ in a
truncated file returns truncated text. [RAN probe Q1]. By construction this is defensible
(the binary tail is past the cap); one line in the docstring would settle it.

### NIT-2 — `require_network_severance=True` + `allow_network=True` runs
`shell_tools.py:487` only refuses when `not allow_network`. A contradictory request
executes with `network_severed: false` reported. Honest, but a one-line refusal would
match "require" semantics.

### NIT-3 — `reason` duplicates `error` for non-jail refusals
See §3 Operability (`shell_tools.py:382`). Frozen jail/protect reasons should be the only
contents of `reason`; other failures should be `null` or omitted, or the duplication
should be documented as such.

### NIT-4 — `preflight()` echoes unvalidated `timeout_seconds`
`shell_tools.py:314-349` reports whatever float it's given, including >600 or negative.
Operator-facing only, but it should mirror `_parse_timeout` so the preflight can't promise
a budget the tool would refuse.

### NIT-5 — ReDoS / glob-class corner cases are unpinned
Grep `re` and glob-translated regex have no per-pattern deadline; glob character classes
can match `/` (crossing segments) and exotic classes (`[]]`, `a**b`) follow unpinned
semantics (implementer discloses both). Not patched correctly, not patchable correctly at
this layer — record, don't fix (a sleep/timeout band-aid would be worse).

## 5. Attempts to break it

| # | Attempt | Result |
|---|---|---|
| P1 | grep with single-file `path: "a.txt"` | **0 matches, 0 scanned** — MAJOR-1 |
| P2 | grep with `path: ".env"` | refused `protected path: env` before any read ✓ |
| P3 | glob/grep with walk cap forced to 2 | empty results, `truncated: false` — MAJOR-2 |
| P4 | glob `".env"` and `"**/.env"` | literal denied; recursive returns nothing + `skipped_protected: 1` ✓ |
| P5 | read_file absolute path outside ws | `path escapes the workspace jail` ✓ |
| P6 | read_file symlink -> outside secret | escape refused, secret absent from payload ✓ |
| P7–P11 | shell via BashTool (`cat .env`, write outside, canary env, unicode cap, 1s timeout) | all `ok: false / failed to start` — `pwsh` absent on this host (`which pwsh -> None`); BashTool execution path unprovable here |
| P12 | grep over tree incl. binary file | binary file matched nothing and was counted nowhere — MINOR-2 |
| probe2 | same PROCESS backend via python (no shell needed) | read `.env` ✓, read outside ✓, wrote outside ✓ — MAJOR-3 is real |
| Q1 | NUL at byte 65 537 | truncated text returned — NIT-1 |
| Q2 | `_truncate_combined` on 80k snowmen | 196 608 bytes out — MINOR-1 |
| Q4 | `~` path | treated as ws-relative literal -> file-not-found (safe direction) ✓ |
| Q6 | `(a+)+$` on 500-char line, single-file path | 0.0 s / count 0 — masked by MAJOR-1 |

Succeeded breaks: MAJOR-1, MAJOR-2, MAJOR-3, MINOR-1, MINOR-2. No jail or protection
bypass was found in the read tools: 30+ probe/test escape shapes (traversal, absolute,
drive-relative, UNC, mixed separators, symlink file/dir/into-protected, protected-before-
existence) all refuse with the frozen reasons and never surface sentinel content.

## 6. Dimension checklist

- **Correctness.** Read encoding/cap logic sound; shell validation sound. Two functional
  defects (MAJOR-1/2). The char-vs-byte cap (MINOR-1) is a measure defect.
- **Security.** Read side: jail+protect correct, no bypass found (P2/P4/P5/P6 + property).
  Shell side: env canary contract holds at the validation layer; FS/network confinement is
  honestly absent at PROCESS (MAJOR-3, documented). No `shell=True`, no env inheritance,
  no string-prefix containment anywhere.
- **Error handling.** Errors-as-values throughout; each `except` is narrow and names its
  refusal. `SandboxError`/`ValueError` from the sandbox become JSON, nothing swallowed
  silently. `OSError` on read is reported with `strerror` only (no content, no secret).
- **Performance.** All bounds present (cap+1 reads, walk cap, match/line limits, 100k
  command / 64-entry env / 1s..600s timeouts, per-stream draining). Walk-cap silence and
  regex worst cases are the exceptions.
- **Tests.** 57 + 24 passing, well-shaped (dry-run fake for validation, real sandbox for
  execution, Hypothesis property, loop-level PLAN test, reversal notes). Gaps exactly at
  MAJOR-1 (single-file grep), MAJOR-2 (walk cap), and ledger-side canary. Shell execution
  tests are skip-gated on shell presence — honest skips, not weakened assertions.
- **Operability.** Verdicts and isolation are journal-ready; NIT-3/NIT-4 wrinkle the
  field semantics slightly.
- **Scope discipline.** Both commits touch only their write-sets; no workspace/sandbox/agent
  edits; no `type: ignore`/`noqa`/casts introduced (ruff 0, mypy clean on both files).
  Conventions 34/34 now green.

## 7. Optimization and simplification opportunities

- **DO-NOW (0):** MAJOR-1/2 are correctness fixes, not optimizations; no performance work
  is justified before them.
- **DO-LATER (2):** (a) a bounded-regex strategy for grep (RE2-style or per-pattern
  step budget) to close the disclosed ReDoS residual structurally; (b) a stricter globber
  (segment-safe classes) if exotic patterns ever matter to the model.
- **NOT-WORTH-IT (1):** deduplicating `_emit`/`_clamp_limit` by importing from
  `runtime_tools` — the private-helper coupling it would create costs more than the ~15
  duplicated lines; the implementers' call is right.

## 8. Cross-agent interactions

- The sibling collision both implementers flagged (blank citation
  `validation.md:46 -> patch.py:786`) is **resolved**: conventions now 34/34 and ORCH's
  re-point commit 353e2c4 is at HEAD. No action needed.
- **N1.5 registration is still pending and load-bearing:** until `FS_READ_TOOLS` is wired
  into the ASK standing-allow, reads in ASK mode *prompt* (test pins "never DENY", i.e.
  ASK). Shell needs no registration for its mutating posture (`bash*` already in
  `DEFAULT_MUTATING_TOOLS`). If N1.5 forgets the read set, the PLAN/ASK story in the brief
  is half-delivered — worth a line in the Wave-2 ledger.
- `build_shell_tools` returns the catalogue N1.5 merges; `BashTool` also accepts an
  injected sandbox, which is how a NAMESPACE backend could later make
  `allow_network=False` meaningful. The seam is already right.

## 9. What I could not verify

- **Real `pwsh`/`bash` execution on this host.** `pwsh` is absent; the 7 `slow` execution
  tests (echo, exit codes, cwd, timeout, process-tree kill, cap, canary) skipped. The
  timeout/tree-kill/cap/canary *execution* claims therefore rest on the implementer's
  uncommitted powershell-5.1 probe and (presumably) Linux CI — [UNKNOWN] from repo
  artifacts. I verified the backend those tests exercise by running it directly with
  python (probe2), but the frozen `pwsh -NoProfile -Command` argv has never executed here.
- **The full suite** (standing order 16 — scoped checks only, by design).
- **Postgres/S3/extra-gated paths** — untouched by these tasks; not verified.
- Whether the CI `bash` runs of `test_shell_tools.py` pass on Linux — CI output is not in
  this repo snapshot.
