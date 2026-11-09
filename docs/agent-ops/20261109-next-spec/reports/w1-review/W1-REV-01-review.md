<!-- ORCH-DIGEST:START -->
agent: W1-REV-01 | scope: wave 1a (N1.0 ADR-0045 + N1.0a workspace.py) | verdict: APPROVE
findings: BLOCKER 0 | MAJOR 0 | MINOR 4 | NIT 4
claims_audit: 20 claims checked / 0 unsupported or false
band_aids_found: none
top_3_risks: (1) No committed regression test yet for a security-critical jail (T-N1-1 downstream, per plan) — throwaway-script verdicts are not durable. (2) Symlink-swap TOCTOU between check and use is best-effort by design (ADR), second line is PatchApplier realpath. (3) Tools must jail-then-protect in frozen order; nothing in this diff enforces it — enforcement lives in N1.1–N1.4.
optimizations: DO-NOW 0 | DO-LATER 1 | NOT-WORTH-IT 2
reversal_check: reasoned-only (deleting module → N1.1–N1.4 import failure; fail-closed. Two-way door.)
confidence: high (frozen API byte-checked, ~30 independent red-team probes all agreed, ruff/mypy/conventions re-ran)
<!-- ORCH-DIGEST:END -->
# Code review — W1-REV-01 (N1.0 + N1.0a, diff ac59580..161bae2)

READ-ONLY review. No source edited. Code commits reviewed: `c9253c8` (ADR-0045 + coder-agent.md), `7c33723` (workspace.py + ADR README row). Report commits `5c94a65` / `161bae2` carry the implementers' own reports, used only for the claims audit (§2).

## 0. Method

Blind pass first: read the ADR, `workspace.py`, the two reference jails (`sandbox/base.py:495-532`, `validation/patch.py:270-322`), `config.py:49-57,805,926-933`, and `core/errors.py:34-55`, then probed the shipped module in OS-temp dirs before opening either implementer report. Commands re-run by me: `ruff check src tests`, `mypy src`, `pytest tests/conventions`, plus ~30 throwaway probes (see §5). Full suite not run (standing order 16; out of scope for this wave).

## 1. Blind-pass findings

F1 (verdict context): The module is a faithful implementation of the frozen sketch — `__all__` exactly the 5 names, `Workspace` a plain `@dataclass(frozen=True)` with a single `root: Path` field, ws-first `assert_inside_workspace`, `WorkspaceViolation(PaaError)` in the module, reasons byte-exact, `get_settings().home` re-read per call. No deviation found.
F2 (watch, not a finding): `is_protected` duplicates the jail logic of `assert_inside_workspace` rather than calling it, and numbers its reason choices for two cases the ADR left open (canonicalisation failure inside `assert` reuses the escapes reason; `PAA_HOME` canonicalisation failure reuses the protected string). Both are documented and defensible; a downstream test pinning another literal would need a wave-1 amendment, not a code fix.
F3 (watch): Nothing in this diff stops a future caller from skipping `is_protected` after the jail — the frozen order is enforced by ADR text and docstrings, not by the type system. Accepted at this stage; N1.1–N1.4 + T-N1-1 are the enforcement point.

## 2. Claims audit (20 checked, 0 false)

ADR/frozen-API claims (verified against source text):
C1 `__all__` is exactly the 5 frozen names [VERIFIED src/paa/coder/workspace.py:55-61 vs docs/adr/0045-coder-work-tools.md:153-159].
C2 `Workspace` is frozen, single-field, no `__post_init__`, no `slots` [VERIFIED workspace.py:96-100; ADR:190-195].
C3 `WorkspaceViolation(message, *, path, workspace_path, reason, **details)`, stores attributes, delegates details to `PaaError` [VERIFIED workspace.py:113-131 vs core/errors.py:41-44; ADR:228-230]. Attribute+detail keys both land in `to_payload()` [RAN probe: `to_payload` returned `error_type` + the 3 keys].
C4 `resolve_workspace(raw: str) -> Path`, never returns `Workspace` [VERIFIED workspace.py:198; wrapping idiom intact].
C5 `assert_inside_workspace(ws, target: Path)` is ws-first (reverse of `sandbox.base`) [VERIFIED workspace.py:239; ADR:210-213].
C6 `is_protected(ws, target) -> str | None`, returns one of the 3 frozen strings or `None` [VERIFIED workspace.py:73-75,279; probes for all 3 + `None`].
C7 Fresh `PAA_HOME` per call via `get_settings().home`, not cached at import, not `paa_home` [VERIFIED config.py:805,926-933; workspace.py:355. The `paa_home` mis-name caught by IMPL-01 is fixed].
C8 Reasons for workspace resolution (does-not-exist / not-a-directory / cannot-be-canonicalised) [RAN probes for all three + blank + UNC + drive-relative + NUL].
C9 `is_protected` resolves before matching (symlink into a protected subtree is itself protected) [RAN: symlink to `.env` → `protected path: env`; symlink to `.git/HEAD` → `protected path: git`].
C10 Conventions still green after the docs edits [RAN pytest tests/conventions → 34 passed].

IMPL-02 claims (code report):
C11 ruff 0, mypy exactly 3 pre-existing, conventions 34 [RAN all three — agree verbatim].
C12 "45-check functional script — ALL 45 passed" — the script is a throwaway (not committed, honestly disclosed in the report §7). My independent ~30-case probe sweep covers the same mechanisms (same F-Table in §5) and agreed 30/30. Claim corroborated; not independently re-extractable from the repo. Counted as checked, not false.
C13 Hypothesis invariant held — I did not re-run a Hypothesis sweep; my manual generated-part cases are the same axes (`.`, `..`, `.git`, `.env`, `.ENV`, empty, `sub`). Claim not re-rerun; risk accepted because the invariant ("jail returns inside-ws or raises `WorkspaceViolation` only") is structurally implied by `_canonicalise` (raises only `WorkspaceViolation`) + the two containment comparisons. Stated plainly as not-re-verified below, not as refuted.
C14 `RuntimeError` added for symlink loops [VERIFIED workspace.py:192].
C15 Constants private / exactly-5 names [VERIFIED workspace.py:64-75].

IMPL-01 claims (ADR report):
C16 ADR text introduces no `path:line` citation that the docs-reference lock would flag; the coder-agent link resolves [VERIFIED: ADR cites bare paths plus function/method names; RAN conventions 34].
C17 ADR-0045 is present on disk and `docs/adr/README.md` lists `0036–0045` [VERIFIED: file exists; RAN conventions 34].
C18 Out-of-scope: `docs/architecture/overview.md:206` still says the on-disk set is `0036–0044` [VERIFIED overview.md:205-206 — claim true; see MINOR-4].
C19 Work-tool catalogue sentence in `docs/coder-agent.md:207` is prospective — the tools do not exist in this diff [VERIFIED coder-agent.md:207 vs diff name list; see NIT-2].
C20 No secrets/PII in the diff [RAN grep of the diff for key/secret/token patterns — only the word "secret-shaped" in a docstring fragment; paths only in denial details].

## 3. Long-run test (independent answers)

- **Source**: Per-call `expanduser().resolve()` + `is_relative_to`; O(path length); stat-only I/O plus the ADR-mandated per-call `get_settings()` (whose `lru_cache` makes it cheap after first load). No new state, no cache to invalidate. Consistent with the report's §8.
- **Load (100×, concurrency, partial failure)**: The module is stateless; immutable module-level regexes; no locks, retries, sleeps, or unbounded structures. Concurrent calls race only on the caller's disk writes, not on jail state. Every failure path in `_canonicalise` fails closed to `WorkspaceViolation`. [VERIFIED workspace.py:82-93,174-195]
- **Hostile input**: Covered in §5. Traversal, absolute-outside, UNC (both spellings), drive-relative incl. bare `C:`, NUL/control (`\x00`, `\x01`, `\x7f` class), mixed `/\`, symlink escape, symlink-into-protected, case variants (`.GIT`, `.ENV`), PAA_HOME sibling prefix — every hostile probe denied. Non-hostile inside paths (`a/b.txt`, `sub/../f.txt`) allowed, per design.
- **Operability**: Denial strings are stable literals; `WorkspaceViolation` details merge verbatim into ledger payloads via `PaaError` [VERIFIED core/errors.py:52-54]; no file contents or env values in reasons. Failures are diagnosable from `to_payload()` alone.
- **Reversal**: Deleting `workspace.py` breaks N1.1–N1.4 imports (fail-closed, never unjailed), per ADR rollback. **Reasoned-only**: I did not delete the file to prove it. The reasoned reversibility holds because the module is additive (no schema, ledger, config, or CLI change; `coder/__init__.py` untouched).

## 4. Findings

### BLOCKER: none

### MAJOR: none

### MINOR-1 — passing `str` to `assert_inside_workspace` / `is_protected` crashes with `AttributeError`, not `WorkspaceViolation` (workspace.py:239,279; `_canonicalise` at 174-191)

The frozen contract says `Path` only and a string input is a type error. At runtime the module calls `target.expanduser()` and a mis-converted tool argument raises `AttributeError` — which a tool catching only `WorkspaceViolation` will not turn into a denial. `mypy strict` will catch this statically at every typed call site, so this is not an exploitable hole; it is an ugly crash surface. [RAN probe: `assert_inside_workspace(ws, "some/rel/path")` → `AttributeError: 'str' object has no attribute 'expanduser'`.]

### MINOR-2 — `ws.root` is re-canonicalised but not pre-checked (workspace.py:259,316)

ADR says both sides are canonicalised "plus the Windows pre-checks". This module pre-checks only the target; a directly-constructed `Workspace` wrapping a UNC or drive-relative root resolves without hitting `_precheck_input`. Reachable only by bypassing `resolve_workspace` (test construction), and containment is still computed against the resolved root rather than an unchecked string, so I do not regard it as an escape. Flagged for N1.1+ hygiene: pass the root through the same pre-check or document why it is exempt.

### MINOR-3 — `is_protected` wraps the `PAA_HOME` resolve but not the settings load (workspace.py:354-359)

If `get_settings()` raises (e.g. malformed env — a `PAA_HOME` containing a NUL fails inside `config.py:56` during `Settings()` construction), the exception escapes as a non-`WorkspaceViolation` before `_canonicalise`'s guard. ADR's rule is that an un-canonicalisable `PAA_HOME` denies; the wrap only covers canonicalisation of the already-loaded value. Edge case, but the fail-closed guarantee has a hole at the seam.

### MINOR-4 — `docs/architecture/overview.md:205-206` is now stale (`0036–0044`)

After ADR-0045 landed, the on-disk set includes 0045; the overview sentence still says `0036–0044`. The `path:line` lock cannot see it (the cited line 66 of the README is non-blank). Correct as a wave-boundary ORCH sweep; already filed by IMPL-02. [VERIFIED overview.md:205-206]

### NIT-1 — redundant equality guard (workspace.py:272,341)

`resolved != resolved_ws and not resolved.is_relative_to(resolved_ws)` is redundant — `is_relative_to` is true for equality. Harmless; it mirrors `sandbox/base.py:526` so the two jails read identically. Not a behavioural bug.

### NIT-2 — prospective wording in `docs/coder-agent.md:207`

"The tool catalogue now includes jailed work tools (read, edit, shell, git)" is true only after N1.1–N1.5 land. In this diff the catalogue still has no work tools. A reader of this commit finds a doc that asserts code they cannot see. Correct reading: this is the ADR's forward-looking amendment; verify the sentence becomes true by the end of Wave 1.

### NIT-3 — ADR README table formatting (docs/adr/README.md)

The new 0045 row sits directly after the 0044 row in a previously blank line, so the table reads consistently; however the edit also removes the blank line before that point (visible in the diff as a removed empty line). Line-neutral by design; purely cosmetic.

### NIT-4 — security-critical jail ships without a committed regression test (process note, not source)

No `tests/coder/` test for this module exists in the diff. My probes and IMPL-02's throwaway 45-check agree, but nothing in the repository pins `canonicalise-then-is_relative_to`, the UNC/drive-relative pre-checks, casefold matching, or the PAA_HOME-sibling rule against a future edit. The plan assigns this to T-N1-1 (N1.1) — I accept the sequencing, but record the window: a jail-scope change between now and T-N1-1 would be unpinned.

## 5. Attempts to break it

All probes ran in OS-temp dirs against the shipped module (not a copy). `[RAN]` below is my run; clean = jail passes and `is_protected` returns the stated string.

| Attempt | Expect | Result |
|---|---|---|
| `a/b.txt` inside | allow, `None` | allow, `None` ✓ |
| `ws/../out/x` | deny | deny (escapes) ✓ |
| `ws/a/../f.txt` (stays in) | allow | allow, `None` ✓ |
| abs path outside ws | deny | deny ✓ |
| abs path inside ws | allow | allow ✓ |
| UNC `\\server\share\x` + `//server/share/x` (both spellings) | deny | deny ✓ |
| drive-relative `C:foo` and bare `C:` | deny | deny ✓ |
| NUL (`\x00`), ctrl (`\x01`) in target | deny | deny ✓ |
| mixed `..\out\x.txt`, `../out/x.txt` | deny | deny ✓ |
| symlink `link → outside`, probe `link/x.txt` | deny | deny ✓ |
| `.git/HEAD` (top-level), `.git` file itself | protect: git | `protected path: git` ✓ (both) |
| `.GIT/HEAD` (upper-case) | protect: git | `protected path: git` ✓ |
| `sub/.git/config` (nested) | not protected | `None` ✓ (per ADR top-level rule; residual risk noted) |
| `.env`, `a/b/.env.local`, `.ENV` | protect: env | `protected path: env` ✓ (all) |
| symlink → `.env` (symlink-into-protected) | protect: env | `protected path: env` ✓ |
| `PAA_HOME` inside ws subtree | protect: paa-home | `protected path: paa-home` ✓ |
| `PAA_HOME == ws.root` | everything protected | protected ✓ |
| `PAA_HOME` sibling `ph_evil` (prefix-same name) | not protected | `None` ✓ (component-aware `is_relative_to`) |
| `resolve_workspace`: blank/whitespace, missing, file-not-dir, UNC, drive-relative, NUL | deny with the three frozen reasons | all denied with correct reasons ✓ |
| `str` (not `Path`) to assert | — | `AttributeError` (see MINOR-1) |

## 6. Dimension checklist

- **Correctness / logic**: Jail is `expanduser().resolve()` on both sides + `is_relative_to`, never string prefix; sibling-prefix safety confirmed. `resolve_workspace` enforces existence + directory-ness and never mkdirs. Blank/whitespace inputs denied in all three entry points.
- **Security (OWASP-relevant)**: Deny-on-doubt coverage — NUL/control (`[\x00-\x1f\x7f]`, matching `validation/patch.py:78`), drive-relative, UNC, canonicalisation failure (`OSError`/`RuntimeError`/`ValueError` → violation). Resolve-before-compare with symlink following (`Path.resolve`, `strict=False` — non-existing write tails still resolve through existing ancestors). Protected matching on resolved paths with `casefold()`.
- **Evidence/tagging**: Report claims-tagged per standing order 5; §2 separates [VERIFIED]/[RAN]/[ASSUMED]/[UNKNOWN].
- **Resource/perf**: No unbounded loops, queues, or payloads; regexes precompiled; per-call cost O(path length + one stat-heavy resolve).
- **Compatibility**: New file only; no existing callers; no schema/ledger/config/API change.

## 7. Optimization and simplification opportunities

DO-NOW (0): none worthwhile.
DO-LATER (1): `is_protected` re-implements the jail (canonicalise ws → blank check → pre-check → canonicalise target → containment) instead of calling `assert_inside_workspace` and canonicalising once. Consolidating would remove a drift site, but it is also the most obvious place to collide with the frozen API; leave it until N1.1's tool-level tests pin both.
NOT-WORTH-IT (2): removing the redundant equality guard (NIT-1 — mirrors sandbox/base.py; changing it creates a visual diff between the two jails for zero behaviour gain); exporting the reason-string constants (rejected by design — `__all__` is frozen at 5, strings are part of the return contract).

## 8. Cross-agent interactions

- W1-IMPL-01 (`c9253c8`): ADR + amendment. Registry/index row deferred; captured by W1-IMPL-02 in `7c33723`. The leftover (MINOR-4, overview) is ORCH's wave-boundary sweep.
- W1-IMPL-02 (`7c33723`): workspace.py + README row. No contract divergence from the ADR; the two non-frozen judgement calls (canonicalisation-failure reasons) are consistent with the frozen strings and the "always deny" posture — §12 of that report says exactly this, and I agree.
- No other wave-1 code touches `Workspace` yet; downstream N1.1–N1.4 must `assert_inside_workspace` then `is_protected`, in that order — the INVARIANT is named in both docstrings.

## 9. What I could not verify

- **Hypothesis sweep**: IMPL-02's property invariant was not re-run by me (no committed test artifact). Mitigation: structural implication + my manual generated-part probes across `.`/`..`/`.git`/`.env`/`.ENV`/`empty`/`sub` agreed.
- **Windows 8.3 short names / non-Latin / case-insensitive host semantics**: I ran on Windows (dev box), but the 8.3 ambiguity path (`OSError` during `resolve()`) was not triggered; the module denies on any such failure rather than guessing, which is the ADR-chosen behaviour. Over-denial risk on exotic Windows resolvers remains.
- **Full test suite**: not run per standing order 16; ruff/mypy/conventions are the scoped gate for this wave.
- **PAA_HOME load failure via settings construction** (MINOR-3): reasoned from `config.py:49-57,926-933`; not triggered with a live malformed env because that would poison the shared settings cache for the session.
- **TOCTOU symlink swap between check and use**: by design best-effort (ADR-0045 + docstring); requires a concurrent attacker with filesystem write access inside the workspace. Not closable at this layer; acknowledged in the ADR.

## What I did NOT verify (stated plainly)

That the module will be used correctly by the four N1.1–N1.4 tools — those do not exist yet. The pre-condition (frozen API implemented verbatim with deny-on-doubt jail semantics and named invariant) is verified; the usage is downstream.
