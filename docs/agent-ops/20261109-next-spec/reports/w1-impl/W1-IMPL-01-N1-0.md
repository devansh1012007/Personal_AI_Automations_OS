<!-- ORCH-DIGEST:START -->
agent: W1-IMPL-01 | task: N1.0 | status: DONE
code_commits: c9253c8 | report_commit: PENDING
files_changed: 3 (docs/adr/0045-coder-work-tools.md, docs/coder-agent.md, docs/agent-ops/20261109-next-spec/reports/w1-impl/W1-IMPL-01-N1-0.md)
contracts_changed: none (docs-only; defines frozen contract for N1.0a)
tests: added 0, changed 0; scoped run: pass (pytest tests/conventions -q)
ownership_violations: none
deferred_dependencies: docs/adr/README.md index row for 0045 (outside WRITE-SET) — ORCH to add in wave-boundary commit
out_of_scope_findings: 0
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: none (ADR frozen API leaves no open choice for N1.0a; Windows 8.3/case canonicalisation deferred to strict subclass in N1.0a with deny-on-doubt)
confidence: high (code-verified signatures reused from sandbox/patch with explicit deltas; docs lock run scoped)
last_comms_line_read: 2026-11-09T09:19:15Z
<!-- ORCH-DIGEST:END -->
# Implementation report — W1-IMPL-01 — N1.0 ADR-0045 Coder work tools

## 1. Task and acceptance criteria (restated)

Write ADR-0045 (Coder work tools) defining frozen Workspace API before N1.0a implements it, and amend deliberate-separation statement in docs/coder-agent.md. ADR must record D-2 (work tools in paa.coder not marketplace), D-3 (edits via PatchApplier, bash via Sandbox.run), path jail, protected paths (.git/, .env*, PAA_HOME), shell definition (pwsh Win / bash POSIX), and define FROZEN Workspace public API: `Workspace {root: Path}`, `resolve_workspace(raw: str) -> Path`, `assert_inside_workspace(ws, target) -> Path` (raises WorkspaceViolation), `is_protected(ws, target) -> str|None`. Also amend "Known limitations" deliberate-no-fs-tools lines to point at ADR-0045 + work tools. Acceptance: future N1.0a coder can implement without inventing API; coder-agent.md amended line cited; pytest tests/conventions still green. Python spec only; no code change. WRITE-SET: docs/adr/0045-*.md, docs/coder-agent.md only.

## 2. Verification that the problem exists (evidence gathered myself)

[VERIFIED docs/coder-agent.md:203] "Known limitations" section exists with the deliberate-separation statement at lines 207-210: tool catalogue is read/submit/run/memory shaped, no filesystem tool, deliberate — workspace/validation layer is separate surface with its own gates. This overstates separation for Wave 1: without frozen contract, 4 parallel tool implementers (N1.1-N1.4) diverge on jail (plan B-2). No ADR-0045 exists; docs/adr/ holds through 0044 only [RAN glob docs/adr/004* -> 0040-0044]. ADR index lists 0044 as last row [VERIFIED docs/adr/README.md:64] and its "files on disk" sentence lists 0036-0044 [VERIFIED docs/adr/README.md:66-67], so 0045 is free and is the next number per brief §2(6).

[VERIFIED src/paa/sandbox/base.py:495] `resolve_workspace(workspace_path: Path | str) -> Path` and [VERIFIED src/paa/sandbox/base.py:518] `assert_inside_workspace(candidate: Path | str, workspace: Path) -> Path` exist with SandboxError semantics (resolve-then-compare, is_relative_to). [VERIFIED src/paa/validation/patch.py:270] `safe_relative_path(raw: str, workspace: Path) -> Path` exists with 4 escape classes (traversal, absolute, symlink via realpath, NUL/control). [VERIFIED src/paa/coder/permissions.py:127] `_MUTATING_TOOLS` names write*/edit*/bash*/apply_patch* etc., and [VERIFIED src/paa/coder/loop.py:85] `Tool` Protocol (name/description/schema/run) exists. Reuse is therefore real; a second jail or write path would duplicate it.

## 3. Root cause

Architectural entry point: the coder loop (`src/paa/coder/loop.py`) is tool-agnostic; the permission policy (`src/paa/coder/permissions.py`) already names filesystem/shell tools without implementations. Wave 1 adds four tool modules (read/write/bash/git) in parallel. Absent a frozen jail contract, each implementer invents its own canonicalisation, protected-path set, and error type — the exact failure B-2 warns against, with a Windows security hole (case/8.3/UNC) as the sharp edge. The ADR is the contract; it must be frozen *before* N1.0a lands so N1.1-N1.4 compose against one surface.

## 4. Approach chosen and why (alternatives rejected)

Single ADR recording D-2/D-3/D-10 + jail + shell + frozen API, plus one-paragraph amendments to coder-agent.md. Alternatives rejected:
- Per-tool jails (each tool module resolves paths itself): rejected — four jails = four bugs; plan §J names single workspace.py as the one place to harden.
- Marketplace/skills location for tools (D-2 alternative): rejected per plan D-2 — needs registry/permissions work before any tool runs; paa.coder reuses ToolPermissionPolicy, hooks, AgentLoop.
- New write path (direct file writes) instead of PatchApplier: rejected per D-3 — PatchApplier is journalled/hash-recorded/rollback-capable; INV correction (prior-hash re-check in N1.2) is recorded, not solved here.
- Free-form shell vs pwsh/bash (D-10): pwsh/bash (owner 2026-11-09) chosen; python-c would hide shell bugs, WSL-only would block Windows dev.
- Sketch-only ADR (copy brief shorthand without types/order/exception details): rejected — would force N1.0a coder to invent ws-first vs candidate-first ordering, Path vs Path|str, WorkspaceViolation base, and protected-match depth. ADR freezes each explicitly with rationale and invariants.
- Code change (workspace.py stub) inside N1.0: rejected — brief §6 skip list forbids it; N1.0a (W1-IMPL-02) implements verbatim.

## 5. What changed (per file)

### docs/adr/0045-coder-work-tools.md (new)
Frozen contract module `src/paa/coder/workspace.py` exporting `Workspace`, `WorkspaceViolation`, `resolve_workspace`, `assert_inside_workspace`, `is_protected`. Full text and line citations in §11; summary:
- Module + ownership: single file, all work tools import from there; no per-tool jail; existing sandbox helpers are reference, not reused blindly (different exception contract, different arg order, no protected-set). WorkspaceViolation subclasses PaaError (ledger-payload rule), carries details keys path/workspace_path/reason (plus structured denial reason for PLAN-vs-jail distinction). Defined in workspace.py, not core/errors.py, so a coder-local jail does not widen the global taxonomy; tools catch WorkspaceViolation (not SandboxError/ValidationError).
- `Workspace` frozen dataclass: `root: Path`; INVARIANT root is canonical absolute directory (expanduser + resolve, exists, is_dir). No __post_init__ validation frozen (resolve_workspace is the validating constructor); assert/is_protected re-resolve ws.root defensively so test-constructed Workspace(root=...) with uncanonical root still jails correctly.
- `resolve_workspace(raw: str) -> Path`: str only (CLI input), expanduser, relative to CWD, resolve() canonicalise, must exist and be directory, deny on doubt raises WorkspaceViolation (missing, not-a-dir, IO/permission on resolve). Returns Path (not Workspace); caller wraps `Workspace(root=resolve_workspace(raw))`. Frozen to match plan N1.0a row and brief sketch simultaneously.
- `assert_inside_workspace(ws: Workspace, target: Path) -> Path`: ws first (deliberate order, opposite of sandbox.base candidate-first), Path only (tools convert str args to Path before calling; str input is a type error, not silently coerced). Resolves both sides, allows root itself and descendants, raises WorkspaceViolation on escape. Covers .., absolute outside, drive/UNC, symlink escape, mixed separators. INVARIANT: caller must call is_protected after jail.
- `is_protected(ws: Workspace, target: Path) -> str|None`: canonical-resolves target, then matches frozen set: (a) top-level .git subtree (first relative part case-insensitive == ".git"), (b) any-depth .env* basename glob (case-insensitive match on each relative filename segment), (c) anywhere inside PAA_HOME (re-resolved from get_settings() per call, never cached at import; if PAA_HOME == ws.root everything is protected). Returns frozen reason strings `protected path: git` / `protected path: env` / `protected path: paa-home`, or None. Case-insensitive matching required on Windows; N1.0a must implement casefold compare, not locale compare.
- Windows hardening guidance frozen as behavior, not as loose advice: strict=True resolve avoided? Actually frozen as: Path.resolve() alone does not canonicalise 8.3/UNC/case (plan property 5); implementation must deny on doubt (any OSError during realpath/resolve, drive-relative, UNC, 8.3 short-name ambiguity, mixed-separator confusion) and must use is_relative_to on resolved paths, never string startswith. Property test T-N1-1/T-N1-2 must cover .., abs, drive/UNC, mixed sep, symlink, explicit symlink escape.
- D-2/D-3/D-10 recorded: tools live in paa.coder, edits via UnifiedDiffValidator.dry_run → PatchApplier.apply (N1.2 adds prior-hash re-check; ADR records INV that apply today never compares prior_sha256), bash via Sandbox.run with pwsh -NoProfile -Command (Win) / bash -c (POSIX), timeout 120s cap 600s, output cap 64KB truncation marked, env allowlist, network_severed=False exposure note. Shell argv frozen as tuple, never free-form string split.
- File-encoding/size/tool-shape constraints for downstream tools documented so N1.1-N1.4 need not re-derive: 64KB reads truncated+marked, UTF-8 round-trip/CRLF/BOM/binary refusal, edit exactly-once old_str, git fixed argv + git_stage naming + PLAN-coverage invariant, Tool Protocol conformance.

### docs/coder-agent.md (amended)
Lines 203-211 "Known limitations": bullet 2 replaced. Old text claimed no filesystem tool and deliberate separation. New text: work tools exist (read/edit/shell/git) in paa.coder per ADR-0045, jailed by `coder/workspace.py`, edits via PatchApplier, bash via Sandbox.run; the deliberate separation now is that work tools always pass the jail + permission policy (never a bypass), and ADR-0045 owns the contract. --resume and token-accounting bullets untouched. Link `[ADR-0045](adr/0045-coder-work-tools.md)` relative to docs/ resolves to docs/adr/0045-coder-work-tools.md.

## 6. Blast radius and contracts

Docs-only: no public Python contract changed. Frozen contract is forward-only: N1.0a (W1-IMPL-02) implements workspace.py verbatim; N1.1-N1.4 import it. Existing sandbox.base.resolve_workspace / assert_inside_workspace and validation/patch.safe_relative_path are unchanged (no caller changes). No schema/ledger/API change. ADR index row for 0045 in docs/adr/README.md is intentionally NOT added (outside WRITE-SET; see §12 deferred dependency) — ORCH adds it at wave-boundary commit so tests/conventions markdown-link allowlist (15 index-only ADR rows) stays consistent.

## 7. Tests (how each confirmed to fail without fix)

No new tests authored (docs-only task; brief forbids implementation). Scoped verification: `pytest tests/conventions -q` exercises test_docs_references (every path:line in adr docs lands on non-blank line in exactly one file) and test_markdown_links (every markdown link resolves + anchor slugs). [RAN pytest tests/conventions -q -> 34 passed] (see §11 for verbatim). Mutation check: removing ADR file or breaking coder-agent.md link target would fail test_markdown_links (same mechanism that caught the untracked-spec anchor in Wave 0); a stale path:line in the ADR would fail test_docs_references. The ADR itself avoids most path:line citations (uses function names) to reduce rot surface; the one link added (coder-agent.md -> adr/0045) is covered by that lock. N1.0a property tests T-N1-1/T-N1-2/T-N1-3 are downstream (W1-IMPL-02), not claimed here.

## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)

- Source: jail canonicalisation is O(path length) resolve + is_relative_to, no new index; two ledger writes per Wave-2 tool call are negligible vs model latency (plan §J). Docs change has no runtime cost.
- Load (100x, concurrency, partial failure): N/A for docs; ADR records that workspace.py itself must be stateless (no cache, no lock) so concurrent tool calls in one session race only on file writes via PatchApplier journal, not on jail state.
- Hostile input: traversal, absolute, UNC/drive, symlink escape, NUL/control, case/8.3 confusion are the hostile classes; ADR mandates deny-on-doubt and resolve-before-compare, with property tests named so a revert that reintroduces string-prefix check fails T-N1-1.
- Operability: denial reasons are stable strings (`protected path: git|env|paa-home`, `path escapes the workspace jail`) so operators can grep ledgers/tool results; no secrets in reasons (paths only, no file contents, no env values).
- Reversal: reverting this commit deletes ADR-0045 and restores the old "no filesystem tool" bullet; N1.0a would then have no contract and would be blocked (its brief depends on N1.0 merging first). No forward-written ledger rows exist yet, so rollback is plain git revert, two-way door (plan §K).

## 9. Edge cases and abuse scenarios

Covered in ADR: .. traversal after normalisation; POSIX /etc, Windows C:, UNC, drive-relative, 8.3 short names, mixed / and \ separators, case-insensitive .GIT/.ENV on Windows, symlink pointing outside (resolved-path check catches it), symlink into .git/.env (is_protected resolves before match), workspace root itself as target (allowed by jail, still checked against protected), PAA_HOME == ws.root (everything protected), missing/non-directory workspace (deny, never mkdir), NUL/control chars (deny per safe_relative_path precedent). Abuse: model passes `../../.env` or absolute `C:\Windows\...` — jail must deny before any read/write; tool that skips is_protected after jail is a contract violation named in ADR. Each tool must return the denial reason string verbatim to the model (so the model learns it cannot do that) rather than a generic error.

## 10. Performance and security

No code change, so no new CVE surface. Security content is the frozen jail: resolve-before-compare, is_relative_to (never startswith), deny on doubt (any OSError during canonicalisation denies, never passes through), protected set covers .git (config/hooks), .env* (secrets, any depth), PAA_HOME (ledger/sessions). Sandbox env allowlist + redact path (N2.5) is inherited via Sandbox.run, not reimplemented. Observation: reason strings are stable and ledger-visible (via Wave-2 ToolJournal DENIED/FAILED), so a jail hit is auditable, not silent.

## 11. Commands run and results (verbatim summaries)

[RAN git branch --show-current -> ops/20261109-next-spec]
[RAN git status --porcelain -> untracked docs/2026-11-09-next-spec.md, docs/New shortcut.lnk, briefs/W1-IMPL-01.md, docs/plans/2026-11-08-*.md, uv.lock (all pre-existing Wave-0 leftovers, none mine)]
[RAN bare `pytest tests/conventions -q` -> ModuleNotFoundError: No module named 'paa' (system python has no package; not a docs failure. Retried with the repo venv)]
[RAN .venv\Scripts\python.exe -m pytest tests/conventions -q -> 34 passed, EXIT:0] (run twice: before self-review edits and after the two sketch fixes — both green; includes test_docs_references + test_markdown_links + test_import_conventions)
No ruff/mypy run (no Python files touched; brief §7 scoped docs checks only). No full suite (standing order 16).

## 12. Deviations from plan/brief

None material. Two clarifications to prevent N1.0a drift (recorded as ADR decisions, not brief deviations):
(1) `resolve_workspace` returns `Path` (not `Workspace`) — matches both brief sketch and plan N1.0a row; ADR spells out the wrapping idiom.
(2) `WorkspaceViolation` base and module frozen as `PaaError` subclass in `src/paa/coder/workspace.py` (brief names the exception but not its base/module) — required so N1.0a does not invent a third taxonomy.

## 13. Out-of-scope findings

None. No edits outside WRITE-SET.

## 14. Known limitations, risks, rollback notes

- ADR index row for 0045 not added (WRITE-SET excludes docs/adr/README.md). ORCH to add `| [0045](0045-coder-work-tools.md) | Coder work tools (frozen Workspace API) | Direction |` in wave-boundary commit. Without it, ADR-0045 is discoverable via coder-agent.md link but missing from the canonical index — a finding, not a blocker for N1.0a which cites the file directly.
- Windows 8.3/UNC/case canonicalisation is specified as deny-on-doubt behavior; the exact strict-realpath implementation is N1.0a's problem (W1-IMPL-02) with property tests T-N1-1/T-N1-2 as gate. If N1.0a cannot close 8.3 fully on Windows CI, it must deny short-name paths (documented fallback) rather than allow.
- Prior-hash TOCTOU in PatchApplier.apply (recorded in ADR, owned by N1.2) remains open until N1.2 adds the re-check; Wave-1 in-memory journal does not close it.
- Rollback: git revert of ADR + coder-agent.md paragraph (two-way, no schema/ledger change). N1.0a must not land before this commit (dependency order enforced by orchestrator).

## 15. Where I am least confident

Windows 8.3 short-name canonicalisation edge: Path.resolve() behavior for 8.3 on the CI host is unverified here (no N1.0a code to run); ADR therefore freezes deny-on-doubt rather than a specific 8.3 handling, which is the safe but possibly over-strict choice N1.0a may need to justify in its report.

## 16. Self-review checklist

- Diff re-read from scratch: ADR + coder-agent.md only, no stray edits, no src changes.
- Self-review MAJOR (fixed pre-commit): ADR draft cited `get_settings().paa_home` — the real field is `Settings.home` (config.py:805). Fixed to `get_settings().home` in ADR-0045; conventions re-run green. Without the fix N1.0a would AttributeError on first is_protected call.
- Self-review MAJOR (fixed pre-commit): ADR code sketch referenced `PaaError` without importing it and omitted the `**details` kwargs the decision text promises. Fixed sketch to import `Any` + `PaaError` and carry `**details: Any`; sketch is now copy-pasteable.
- ADR contains zero `path:line` citations and zero markdown links (verified by grep), so the docs-reference and markdown-link locks cannot be broken by this file; the one link added (coder-agent.md -> adr/0045-coder-work-tools.md) is covered and passes.
- Facts tagged [VERIFIED]/[RAN]; no claimed test run without command+outcome.
- Frozen API implementable verbatim by N1.0a (signatures, types, arg order, exception base, reason strings all explicit; no "e.g." left in contract).
- No band-aid: no code, no type suppression, no test weakening (docs only).
- Ownership respected: only WRITE-SET files edited; index row deferred explicitly.

## 17. Commits

Code commit: `c9253c8` — docs(N1.0): ADR-0045 Coder work tools with frozen Workspace API (files: docs/adr/0045-coder-work-tools.md, docs/coder-agent.md).
Report commit: PENDING (this file, separate docs-only commit listing `c9253c8`).
