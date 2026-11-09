<!-- ORCH-DIGEST:START -->
agent: W1-IMPL-02 | task: N1.0a | status: DONE
code_commits: 7c33723 | report_commit: PENDING
files_changed: 2 (src/paa/coder/workspace.py, docs/adr/README.md)
contracts_changed: none (implements ADR-0045 frozen contract verbatim; __all__ = 5 names exactly)
tests: added 0, changed 0; scoped run: pass (ruff 0, mypy 3 pre-existing, conventions 34 passed + 45-check functional script + hypothesis invariant)
ownership_violations: none
deferred_dependencies: none (ADR index row captured in-write-set; FINDING: overview.md ADR list string now needs 0045 sweep by ORCH)
out_of_scope_findings: 1 (overview.md:205-206 lists on-disk ADRs as 0036-0044; now stale incl. 0045, human-read level)
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: exact WorkspaceViolation reason for PAA_HOME/target canonicalisation failure in is_protected (ADR freezes only escapes reason + 3 protected strings; I chose protected-paa-home / escapes respectively and documented)
confidence: high (frozen API verbatim; deny-on-doubt pre-checks + resolved-path compare verified against 45 hostile cases + hypothesis invariant)
last_comms_line_read: 2026-11-09T09:35:35Z
<!-- ORCH-DIGEST:END -->

# Implementation report — W1-IMPL-02 — N1.0a Shared coder workspace jail

## 1. Task and acceptance criteria (restated)

Implement `src/paa/coder/workspace.py` per frozen ADR-0045 API (N1.0 merged c9253c8), verbatim:
`Workspace {root: Path}` frozen dataclass, `resolve_workspace(raw: str) -> Path`
(expanduser, resolve vs cwd, canonicalise, must exist and be a directory, deny on doubt),
`assert_inside_workspace(ws, target) -> Path` (ws-first, Path-only, both sides canonicalised,
root+descendants allowed, raises `WorkspaceViolation` subclassing `PaaError`), and
`is_protected(ws, target) -> str|None` (canonical-resolves first, casefold matching, returns
`protected path: git` / `protected path: env` / `protected path: paa-home` or None; PAA_HOME
from `get_settings().home` re-read per call). Also capture the ADR-0045 index row deferred by
W1-IMPL-01. Acceptance: Hypothesis-compatible pure functions (stat-only I/O), mypy strict +
ruff 0 for src/, `pytest tests/conventions -q` green, existing callers unaffected (new file only).
Skip list honoured: no tools, no ADR text edit, frozen API divergence would have been ESCALATE.

## 2. Verification that the problem exists (evidence gathered myself)

- [VERIFIED src/paa/sandbox/base.py:495] `resolve_workspace(workspace_path: Path | str)` and
  [VERIFIED src/paa/sandbox/base.py:518] `assert_inside_workspace(candidate, workspace)` exist:
  candidate-first, `SandboxError`, no protected set. Reusing them as the coder jail would
  contradict the frozen order/exception contract at every call site (ADR "Alternatives rejected").
- [VERIFIED src/paa/validation/patch.py:270] `safe_relative_path(raw, workspace)` blocks the 4
  escape classes but is patch-relative (strips `a/`/`b/`, rejects absolute), raises
  `ValidationError` — not workspace-shaped.
- [VERIFIED src/paa/config.py:805] `Settings.home` (field name `home`, not `paa_home`);
  [VERIFIED src/paa/config.py:926] `get_settings()` is lru_cached, so `is_protected` re-reads
  it per call and never caches at import.
- [VERIFIED src/paa/coder/__init__.py:28] no workspace export exists; `workspace.py` absent
  from `src/paa/coder/` listing. N1.1-N1.4 have no jail to import yet — genuine gap.
- [VERIFIED docs/adr/0045-coder-work-tools.md:143-186] frozen sketch gives exact `__all__`,
  signatures, arg order (ws-first), exception base and constructor. Implemented verbatim.
- [VERIFIED docs/adr/README.md:64] index lacked row 0045; its "files on disk" sentence listed
  0036–0044. The row is in my write-set (brief §4), so I added it (see §5).

## 3. Root cause

Architectural entry point: Wave 1 adds four tool modules (read/write/bash/git) against one
shared jail. Without this module each tool would invent canonicalisation (plan B-2: four jails
= four divergent Windows bugs). The module is the single place to harden: resolve-before-compare
with `is_relative_to`, Windows pre-checks before resolution, and protected matching on resolved
paths. Both existing helpers encode the right *patterns* but the wrong *contract* for coder
tools (order, exception, patch-relative input), so the fix is a new module written against them
as reference — not a wrapper, and not a fifth caller with trust issues.

## 4. Approach chosen and why (alternatives rejected)

Single new module `src/paa/coder/workspace.py`, stdlib-only plus `paa.config`/`paa.core.errors`.
- **Direct reuse of sandbox helpers by importing and catching `SandboxError`: rejected** — ADR
  freezes `WorkspaceViolation` (PaaError subclass, coder-local module) and ws-first order;
  wrapping the sandbox pair would force tools to catch two taxonomies and would re-introduce
  the candidate-first confusion the freeze explicitly avoids.
- **Reuse of `safe_relative_path`: rejected** — it is patch-relative (git-prefix stripping,
  absolute rejection) whereas assert takes absolute-inside targets; copying its *patterns*
  (control-char class, Windows absolute recognition, realpath containment) without its I/O
  contract is the faithful reuse. The control-char regex is inline (not imported) because it is
  private (`_CONTROL_CHARS`) in patch.py; importing a private constant would couple the jail to
  the patch module's internals.
- **Manual separator rewriting (`\`→`/`): rejected** — ADR: on POSIX backslash is a legal
  filename character; OS Path semantics decide, resolved compare catches escapes. Only
  OS-neutral string pre-checks (control chars, drive-relative, UNC) run before resolve.
- **Exporting reason-string constants as public names: rejected** — ADR's sketch freezes
  `__all__` at exactly 5 names and N1.1-N1.4 "import only these names". Constants are
  module-private; tools surface the literal strings from the return value / exception.
- **UNC and drive-relative workspace roots allowed: rejected** — ADR lists both among
  deny-on-doubt canonicalisation failures; a network-share workspace is not supported by the
  frozen contract. Documented; operator need would be an ESCALATE-level contract change.
- **Precedence git > env > paa-home: chosen** — ADR lists clauses in that order; most specific
  reason wins, paa-home catch-all last. Deterministic and ledger-informative.

## 5. What changed (per file)

### src/paa/coder/workspace.py (new, 362 lines)
- `Workspace`: `dataclass(frozen=True)`, single `root: Path`; no `__post_init__` (ADR: no
  slots/validation frozen; assert/is_protected re-resolve `ws.root` defensively).
- `WorkspaceViolation(PaaError)`: `(message, *, path, workspace_path, reason, **details)` —
  stores the three as attributes, delegates to `PaaError` so they land in `details` (ledger
  payload verbatim). Defined in this module, not `core/errors.py` (ADR).
- `resolve_workspace(raw: str) -> Path`: blank → `workspace cannot be canonicalised`; pre-check
  (control chars, UNC, drive-relative) before resolve; `expanduser().resolve()`; missing →
  `workspace does not exist`; non-dir → `workspace is not a directory`. Never mkdirs.
- `assert_inside_workspace(ws, target) -> Path`: ws root re-resolved defensively; blank/pre-check;
  target canonicalised; `resolved == resolved_ws or resolved.is_relative_to(resolved_ws)` else
  `path escapes the workspace jail`. Canonicalisation failures (`OSError`/`RuntimeError`/
  `ValueError` — RuntimeError added in self-review for symlink loops) deny with the same reason.
- `is_protected(ws, target) -> str | None`: canonical first (symlink into protected subtree is
  itself protected); outside-ws target raises escapes (caller must jail first); top-level
  `.git` via first-part `casefold() == ".git"`; any segment `fnmatchcase(part.casefold(), ".env*")`;
  PAA_HOME via `get_settings().home` per call, canonicalised, at-or-under check; PAA_HOME
  canonicalisation failure raises with reason `protected path: paa-home` (never treated as
  unprotected). Precedence git > env > paa-home documented in the docstring.
- Helpers are private: `_violation`, `_precheck_input`, `_canonicalise`. No global mutable
  state; no locks; no caching at import. House-style SPEC DEVIATION (docs/adr/0045) citation.

### docs/adr/README.md (one-row index fix, in write-set)
- Added row `| [0045](0045-coder-work-tools.md) | Coder work tools (frozen Workspace API) | Direction |`
  exactly as W1-IMPL-01 suggested, and moved `0036–0044` → `0036–0045` in the "files on disk"
  sentence. The new row occupies the previously blank line 65, making the edit line-neutral
  (the sentence stays on line 66), so `docs/architecture/overview.md:206`'s citation
  `docs/adr/README.md:66` remains true — no out-of-write-set file needed changing. Verified the
  alternative (row insertion +1 line) failed the docs-reference lock and was corrected.

## 6. Blast radius and contracts

New file only; no existing caller touches it (N1.1-N1.5 import it downstream). No ledger,
schema, config, API or CLI change. Frozen contract honoured: exported names, signatures, arg
order, exception base/module, reason strings, and the jail→protect calling order all match
ADR-0045 verbatim. `__all__` is exactly the 5 frozen names. `coder/__init__.py` deliberately
untouched (registration belongs to N1.5; ADR says tools import from `paa.coder.workspace`).

## 7. Tests (how each confirmed to fail without fix)

No test files authored: this task owns code only; the T-N1-1/T-N1-2/T-N1-3 suites land with
N1.1 (brief: "property-test-ready"). Verification was scoped and executed, not claimed:
- 45-check functional script (throwaway, in OS temp dir, NOT committed) exercising: canonical
  resolve (incl. `..` collapse, cwd-relative), missing/non-dir/blank/NUL/UNC/drive-relative
  denials with frozen reasons; inside root/descendants/`..`-stays-in; escapes via `..`,
  absolute-outside, empty, NUL, UNC, drive-relative, `C:\Windows`; non-canonical `Workspace`
  root constructed directly still jails; symlink escape denied / symlink inside allowed;
  protected git (incl. `.GIT` casefold), nested `.git` NOT protected, env root/nested/`.ENV`/
  dir-segment, unprotected None, non-existent protected path, PAA_HOME inside ws / == ws root,
  git-beats-paa-home precedence, is_protected outside denies, exception details keys
  (`path`/`workspace_path`/`reason` in `to_payload`, `isinstance PaaError`), 13/13 hostile
  strings denied (including `\x7f`, 300-char name, lone `\`). **All 45 passed** [RAN].
- Hypothesis invariant (throwaway): for generated relative parts (`.`, `..`, `a`, `.git`,
  `.env`, `x.txt`, empty, `.ENV`, `sub`), jail either returns a path inside ws and
  `is_protected` does not raise on it, or raises `WorkspaceViolation` only. **Held** [RAN].
- Mutation/anti-band-aid honest note: the script is not a committed regression test; a future
  revert of, e.g., the UNC pre-check would fail the "resolve UNC"/"escape UNC" checks above
  and T-N1-1 downstream. Committed coverage is N1.1's responsibility per the plan.

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)

- Source: `expanduser().resolve()` + `is_relative_to` per call; O(path length), stat-only I/O
  beyond `exists()/is_dir()` and the one-time settings read (ADR-mandated). No indexes, no new
  state to keep consistent.
- Load (100x, concurrency, partial failure): module is stateless (regexes are immutable);
  concurrent tool calls race only on file writes via PatchApplier, not jail state. No locks,
  no timeouts, no retries. Partial failure during resolve denies (fails closed).
- Hostile input: traversal, absolute, drive/UNC, drive-relative, mixed separators, NUL/control,
  case variants of `.git`/`.env`, symlink escape, symlink-into-protected, overlong names,
  blank/empty. All covered in §7. One known limit: `Path("")` equals `Path(".")` (cwd), so an
  empty tool argument cannot be distinguished at the Path level — tools must validate empty
  strings before converting (Caller contract; documented below).
- Operability: denial reasons are stable literals; `WorkspaceViolation.to_payload()` writes
  straight into a ledger event (`path`/`workspace_path`/`reason` + `error_type`/`message`).
  No secrets in reasons (paths only). `str(exc)` = message with sorted details.
- Reversal: deleting the module makes N1.1-N1.4 tools fail at import (fail-closed, not
  unjailed). Two-way door, no schema/ledger/config change.

## 9. Edge cases and abuse scenarios

Covered: `..` after normalisation; POSIX `/etc`, Windows `C:\`, UNC (`\\host\share` and
`//host/share` spellings), drive-relative `C:foo`/`C:`; 8.3 short names degrade to
OSError-on-resolve → deny; case-insensitive protected matching (`.GIT`, `.ENV`); symlinks
out (deny at jail), symlinks in (allowed), symlink into `.git`/`.env`/PAA_HOME (protected
via canonical match); PAA_HOME inside ws and PAA_HOME == ws root; workspace root as target
(allowed by jail, still protected-checked); nested `.git` fixture (not top-level → not
protected, traversal still jailed); worktree `.git` file (top-level part == `.git` → protected);
non-existent write targets (strict=False resolve checks existing ancestors); `~` expansion;
workspace through symlink alias (canonicalised at construction); TOCTOU symlink swap between
check and use is documented best-effort (PatchApplier realpath + atomic write is the second
line, per ADR). Abuse: model passes `../../.env` or absolute `C:\...` → jail denies before
any read/write; tool that skips `is_protected` after jail violates the frozen order (named in
both docstrings).

## 10. Performance and security

New attack surface is exactly the jail: string pre-checks + one resolve per path per call;
negligible vs model latency. Security posture: deny-on-doubt everywhere (canonicalisation
failure, ambiguous Windows forms, unreadable PAA_HOME all deny); protected matching on resolved
paths (symlinks cannot launder a `.env`); casefold matching (Windows `.GIT` caught); never
string-prefix containment (a ws of `/tmp/foo` cannot be escaped by `/tmp/foobar`). Reasons
carry no file contents, no env values. PAA_HOME read via `get_settings()` (no secret values
involved; `Settings.home` is a path, and the config file's secret-name discipline untouched).
No sandbox/shell/git code added, so no new CVE surface beyond path validation.

## 11. Commands run and results (verbatim summaries)

- [RAN .venv\Scripts\python.exe -m ruff check src/paa/coder/workspace.py -> All checks passed!]
  (initial run flagged 1 RUF002 en dash in docstring; fixed).
- [RAN .venv\Scripts\python.exe -m ruff check src tests -> All checks passed!]
- [RAN .venv\Scripts\python.exe -m mypy src/paa/coder/workspace.py -> Success: no issues found in 1 source file]
- [RAN .venv\Scripts\python.exe -m mypy src -> Found 3 errors in 1 file (subprocess_backend.py:548), checked 143 source files] — exactly the 3 pre-existing Windows errors from the baseline (00-mission.md); no new errors.
- [RAN .venv\Scripts\python.exe -m pytest tests/conventions -q -> 34 passed] (before README edit, and again after; an intermediate failure `test_every_referenced_line_is_not_blank` at overview.md:206 was caused by my first row-insertion shifting README:66 — fixed line-neutrally, re-run green).
- [RAN verify_ws.py (throwaway, 45 checks) -> ALL 45 checks passed]
- [RAN prop_ws.py (throwaway hypothesis invariant) -> property invariant held]
- Full suite NOT run (standing order; brief restricts to ruff/mypy/conventions). No docker.
- No background processes started; nothing to kill.

## 12. Deviations from plan/brief

None from ADR-0045 (frozen API verbatim). Judgement calls ADR left open, all documented in
code + here (not divergences):
1. Reason for target/ws canonicalisation failure inside assert/is_protected: ADR freezes only
   `path escapes the workspace jail`; using it for all jail-side resolution failures keeps one
   denial reason for "cannot establish containment".
2. `is_protected` outside-ws target raises the escapes reason (fails closed); ADR mandates
   jail-first order but does not define standalone behaviour — raising preserves deny-on-doubt
   instead of returning None (which would look "unprotected").
3. PAA_HOME canonicalisation failure inside `is_protected` raises with reason
   `protected path: paa-home` so a tool surfacing the reason verbatim stays in-vocabulary.
4. Protected-precedence git > env > paa-home (ADR lists in that order; docstring documents).
5. `RuntimeError` added to canonicalisation catch tuple (symlink-loop robustness).
6. Constants private (not exported), preserving exactly-5 `__all__`.

## 13. Out-of-scope findings

1. `docs/architecture/overview.md:205-206` lists the on-disk ADR set as `... 0036–0044`. After
   ADR-0045 (file exists on disk since c9253c8) that list is semantically stale, — the
   `path:line` lock cannot see it (line 66 remains non-blank). My write-set covers only the
   README, so I did NOT edit overview.md; ORCH may sweep it at the wave boundary for accuracy.
   The README edit I did make is line-neutral so the citation itself still holds.

## 14. Known limitations, risks, rollback notes

- Empty-string tool arguments: `Path("")` is `Path(".")` (cwd); tools must reject empty args
  before converting to `Path`. `resolve_workspace` itself rejects blank `str` input.
- Windows 8.3 short-name canonicalisation relies on `Path.resolve()` + deny-on-doubt (OSError
  → deny); residual risk is over-denial on exotic Windows resolvers, not escape. CI matrix
  (T-N1-1) is the real gate.
- `is_protected` PAA_HOME check calls `get_settings()` per invocation (ADR-mandated); first
  call constructs Settings (env/dotenv read). Long paths exceeding Windows MAX_PATH will
  OSError → deny (fail-closed).
- Symlink-swap TOCTOU is best-effort by design (ADR); PatchApplier's realpath + atomic write
  is the second line (N1.2).
- Rollback: delete `src/paa/coder/workspace.py` + revert the 2-line README diff (two-way,
  no schema/ledger change). N1.1-N1.4 must not land before this commit (dependency order).

## 15. Where I am least confident

The two non-frozen reason-string choices (canonicalisation-failure reasons in §12.1/12.3):
defensible and documented, but if N1.1-N1.4 tests expect different literals the contract would
need a wave-1 amendment — flag for the reviewer. All frozen strings themselves are verbatim.

## 16. Self-review checklist

- Diff re-read from scratch (git diff + full file read): only write-set files; no other agent's
  work touched; no stray temp files in repo (scripts live in OS temp dir).
- Fahrenheit-check on every frozen name: `__all__` 5/5, signatures `(raw: str) -> Path`,
  `(ws, target: Path) -> Path`, `(ws, target: Path) -> str | None`; reason strings byte-exact
  (`path escapes the workspace jail`, `protected path: git|env|paa-home`); PAAError base;
  `get_settings().home`; ws-first order. All verified against ADR text.
- Self-review MAJOR (fixed pre-commit): first draft exported 4 extra public constants —
  renamed private to keep the frozen 5-name surface exact.
- Self-review MAJOR (fixed pre-commit): docstring said "All four" ledger keys; corrected to
  three detail keys. Also fixed 1 RUF002 en dash.
- Self-review MINOR (fixed): `except (OSError, ValueError)` → added `RuntimeError` for
  symlink-loop resolve failures; bulk-rename check verified no `__PROTECTED` double-underscore
  remained (grep + mypy + runtime import all pass).
- Mutation mentalist: reverting (a) the UNC/drive-relative pre-check, (b) canonical-before-
  compare, or (c) casefold matching would each fail the §7 checks (hostile sweep 13/13,
  symlink escape, `.GIT` casefold) — the checks exercise the exact mechanisms.
- Facts tagged [VERIFIED]/[RAN]; no full-suite claim; scopes honoured (no pytest full run,
  no docker, explicit-path commits only).

## 17. Commits

Code commit: `7c33723` (files: src/paa/coder/workspace.py, docs/adr/README.md) — explicit paths.
Report commit: PENDING (this file; lists the code SHA).
Predecessor anchor: N1.0 docs commit `c9253c8` [VERIFIED reports/w1-impl/W1-IMPL-01-N1-0.md:124].
