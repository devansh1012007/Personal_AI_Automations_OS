# ADR-0045: Coder work tools — frozen Workspace API

## Status

Accepted

*Recorded 2026-11-09 by W1-IMPL-01 (wave 1a) for plan item N1.0. The
workspace module, the four tool modules and the registration are the
N1.0a and N1.1–N1.5 implementers' to write. This ADR freezes the
contract they must not have to re-derive: where the tools live, which
primitives they reuse, where the jail sits, what counts as protected,
which shell, and the exact public API of the one module every tool
imports. The N1.0a implementer implements the API below verbatim; the
N1.1–N1.4 implementers use it and do not re-implement it.*

## Context

The coder agent today has no filesystem, shell or git tool. Its
catalogue is read, submit, run and memory: it drives the runtime, not
the working tree. `src/paa/coder/runtime_tools.py` builds the nine
ledger and memory tools, `src/paa/coder/agent.py` composes them into
`AgentLoop`, and `src/paa/coder/loop.py` defines the tool-agnostic
`Tool` protocol with the safety ordering of hook, permission, prompt,
execute, hook. `src/paa/coder/permissions.py` already names the
filesystem and shell shapes (`write*`, `edit*`, `bash*`, `apply_patch*`)
but no implementation is bound to them.

Wave 1 adds four tool modules in parallel: read, write, shell and git.
Two jailed paths already exist, with deliberately different contracts.
`src/paa/sandbox/base.py` provides `resolve_workspace` and
`assert_inside_workspace`, raising `SandboxError` after
resolve-then-compare with `is_relative_to`. `src/paa/validation/patch.py`
provides `safe_relative_path`, raising `ValidationError`, explicitly
blocking traversal, absolute paths including Windows drive and UNC,
symlink escape via `realpath`, and NUL and control characters. The
sandbox pair takes candidate-first arguments and knows nothing about
protected paths; the patch helper takes patch-relative strings and
strips git prefixes. Neither is the coder's jailed surface: a coder
tool needs a `Workspace`-shaped value, a coder-local exception, and a
ledger-visible protected-path refusal, none of which either helper
offers directly.

Without a frozen contract, four parallel implementers would each invent
path handling where a Windows bug is a security bug (drive, UNC,
mixed separators, case, short names after `Path.resolve()` alone).
The failure looks like divergent jails: one tool jailed by string
prefix, another by `resolve()`, each passing its own tests while the
workspace is still escapable. The contract therefore has to be frozen
before any tool is written.

## Decision

**D-2: work tools live in `paa.coder`, not in the marketplace.** The
new tools are first-class coder tools beside `runtime_tools.py`,
implementing the `Tool` protocol from `loop.py` and governed by
`ToolPermissionPolicy`. The marketplace alternative is rejected: it
needs registry and permissions work before any tool runs, and the
third-party case that would justify that cost does not exist yet. A
future third-party tool layer can sit beside, not instead of, this
package.

**D-3: edits via `PatchApplier`, shell via `Sandbox.run`.** File edits
reuse `UnifiedDiffValidator.dry_run` followed by
`PatchApplier.apply`, which is journalled, hash-recorded and
rollback-capable, with atomic replace of already-computed content.
A direct write path is rejected: a second write surface doubles the
audit and recovery problem Wave 2 has to solve. The reuse carries one
correction the ADR records rather than hides: the applier today never
compares `prior_sha256` during apply, so N1.2 must add the prior-hash
re-check at the source rather than inheriting the time-of-check
window per tool. Shell execution reuses `Sandbox.run` so timeout,
isolation reporting and environment allowlisting are inherited, not
re-implemented.

**D-10: shell is `pwsh` on Windows and `bash` on POSIX.** The argv is
a frozen tuple, never a string split: `("pwsh", "-NoProfile",
"-Command", ...)` on Windows and `("bash", "-c", ...)` on POSIX, per
owner decision. Python `-c` is rejected because it hides shell bugs;
WSL-only bash is rejected because it blocks Windows development. The
subprocess backend cannot enforce `allow_network=False`; the tool
exposes that limit honestly via `preflight()` or a
`network_severed=False` result rather than claiming containment it
does not have, and requires a namespace-class backend for calls that
demand `allow_network=False`.

**Path jail: single module, resolve-before-compare, deny on doubt.**
Every work tool imports the jail from the one module below. No tool
may resolve, relativise or contain-check paths itself. Both sides of
every comparison are fully canonicalised (`expanduser().resolve()`
plus the Windows pre-checks below) before comparing, and containment
is `is_relative_to` on resolved paths, never string prefix. Any
canonicalisation failure (`OSError` during resolve, permission
denied, NUL or control characters `[\x00-\x1f\x7f]` in the input,
drive-relative paths, UNC prefixes, short-name ambiguity that cannot
be resolved) denies with `WorkspaceViolation`; it never passes
through. On POSIX a backslash is a legal filename character, so the
module does not manually rewrite separators: OS `Path` semantics
decide what is a separator, and the resolved-path check catches what
the split does not. The check is best-effort against a concurrent
symlink swap between check and use; the applier's own `realpath`
plus atomic replace is the second line, and the module does not
pretend to close the race with locking.

**Protected paths: frozen set, frozen reason strings.** After the
jail passes, every tool consults `is_protected`, which
canonical-resolves the target before matching so that a symlink into
a protected subtree is itself protected. Matching is case-insensitive
via `casefold()` compare, which is what makes `.GIT` and `.ENV`
protected on Windows as well as POSIX. The set is:

- The top-level `.git` subtree: the canonical target's first relative
  part casefolds to `.git`. This covers the `.git` directory and the
  `.git` file used by linked worktrees. A nested `.git` in a fixture
  is not top-level and is not named by this clause; its traversal, if
  it escapes the workspace, is still caught by the jail symlink rule.
- Any-depth `.env*`: any relative filename segment casefolds-matching
  the glob `.env*` via `fnmatch.fnmatchcase`, covering `.env`,
  `.env.local` and `.ENV` at any depth. A secret-shaped name is denied
  wherever it sits; the workspace root `.env` is only the most common
  case, not the only one.
- `PAA_HOME`: the canonical target is at or under the canonical
  `PAA_HOME` from `get_settings().home` (the `Settings.home` field,
  rooted at `PAA_HOME` via `_default_home()`), re-read per call and
  never cached at import, because the settings cache is process-wide
  and tests move `PAA_HOME` and call `reset_settings_cache()`. If
  `PAA_HOME` equals the workspace root, every path in the workspace
  is protected. If `PAA_HOME` cannot be canonicalised, the call
  denies (`WorkspaceViolation`) rather than treating it as
  unprotected.

The return is one of the frozen strings `protected path: git`,
`protected path: env` or `protected path: paa-home`, or `None`. A
tool that finds a protected target aborts before touching the disk
and surfaces that exact string to the model and, in Wave 2, to the
ledger denial record. The refusal is not overrideable by an allow
rule, a permission mode, or `bypass`: jail and protection sit inside
`Tool.run()`, below every permission decision, the way an explicit
deny sits below `bypass` in the permission policy.

**Frozen Workspace public API.** N1.0a implements this module
verbatim. N1.1–N1.4 import only these names:

```python
# src/paa/coder/workspace.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.core.errors import PaaError

__all__ = [
    "Workspace",
    "WorkspaceViolation",
    "assert_inside_workspace",
    "is_protected",
    "resolve_workspace",
]


@dataclass(frozen=True)
class Workspace:
    """A jailed working tree. `root` is canonical (see below)."""

    root: Path


class WorkspaceViolation(PaaError):
    """A workspace path was refused. ..."""

    def __init__(
        self,
        message: str,
        *,
        path: str,
        workspace_path: str,
        reason: str,
        **details: Any,
    ) -> None: ...


def resolve_workspace(raw: str) -> Path: ...
def assert_inside_workspace(ws: Workspace, target: Path) -> Path: ...
def is_protected(ws: Workspace, target: Path) -> str | None: ...
```

The normative contract, in order:

* `Workspace` is a `@dataclass(frozen=True)` with the single field
  `root: Path`. No `slots` is frozen, and no `__post_init__`
  validation is frozen: `resolve_workspace` is the validating
  constructor, and `assert_inside_workspace` and `is_protected`
  re-resolve `ws.root` defensively so that a `Workspace` constructed
  directly in a test with an uncanonical root still jails correctly.
  Invariant: `Workspace` values produced by `resolve_workspace` carry
  a canonical absolute directory as `root`.
* `resolve_workspace(raw: str) -> Path` takes the raw string the
  caller was handed (CLI input), expands the user, resolves it
  relative to the current working directory, canonicalises it, and
  returns the canonical `Path`. It does not return a `Workspace`;
  the wrapping idiom is `Workspace(root=resolve_workspace(raw))`,
  frozen so the two halves cannot drift. The path must exist and be
  a directory; a missing path, a non-directory, or any
  canonicalisation failure raises `WorkspaceViolation` with reason
  `workspace does not exist`, `workspace is not a directory` or
  `workspace cannot be canonicalised` respectively. Creating a
  missing directory here is forbidden: it would hide a caller whose
  model of the filesystem is already wrong.
* `assert_inside_workspace(ws: Workspace, target: Path) -> Path`
  takes the workspace first and the target second (the reverse of
  the sandbox helper's candidate-first order; the reversal is
  deliberate and frozen so the two cannot be confused). `target` is
  `Path` only; tools convert string arguments to `Path` before
  calling, and a string input is a type error, not silently coerced.
  Both sides are canonicalised, the workspace root itself and its
  descendants are allowed, and anything else raises
  `WorkspaceViolation` with reason `path escapes the workspace jail`.
  The exception's detail keys are `path`, `workspace_path` and
  `reason`, and the same three are exposed as attributes, so the
  denial can be written into a ledger payload without reconstruction.
* `WorkspaceViolation` subclasses `PaaError` and is defined in the
  workspace module itself, not in `core/errors.py`. The base is the
  ledger-payload rule (details merge verbatim into event payloads);
  the module is coder-local so a coder jail does not widen the global
  error taxonomy. Tools catch `WorkspaceViolation`, not
  `SandboxError` or `ValidationError`, and the three subclasses are
  never used interchangeably for workspace refusals. The constructor
  takes `message` and keyword-only `path`, `workspace_path` and
  `reason`, stores them as attributes, and delegates to `PaaError`.
* `is_protected(ws: Workspace, target: Path) -> str | None`
  canonical-resolves the target and returns `None` if it is not
  protected, else exactly one of the three frozen strings above. It
  never raises for an unprotected path; it raises only when the
  target or `PAA_HOME` cannot be canonicalised, and then by denying.
  Callers must call it after `assert_inside_workspace`; calling it
  alone is not a jail, and the order is frozen.

**Agent-facing behaviour frozen for downstream tools.** Size and
encoding shapes the tools inherit, so N1.1–N1.4 do not re-derive them:
64 KB read and output caps, truncated and marked; UTF-8 round-trip
with CRLF preserved on read passthrough, BOM handled, binary refused
with a clear message; `edit_file` matches `old_str` exactly once and
fails without writing on zero or multiple matches, with the success
SHA matching `compute_patch_sha256`; git uses fixed argv tuples only,
with no push, force, config or hooks, and the mutating name is
`git_stage` (never `git_add`, which matches no PLAN glob); every
mutating tool name must match `DEFAULT_MUTATING_TOOLS`, enforced by
a test that fails when one does not; every tool is tested in PLAN,
ASK and LOCKDOWN postures.

### Alternatives rejected

* **Per-tool jails.** Rejected: four jails are four chances to diverge
  on Windows canonicalisation. One module is the one place to harden.
* **Marketplace skills as the tool location.** Rejected per D-2 above.
* **A new direct-write path.** Rejected per D-3 above.
* **Reusing the sandbox helpers directly as the coder jail.**
  Rejected: candidate-first order, `SandboxError` taxonomy and no
  protected set would have to be contradicted at every call site.
  The helpers are the reference the new module is written against,
  not the module itself.
* **A `resolve_workspace` that returns `Workspace`.** Rejected: the
  brief sketch and the plan row both freeze a `Path` return, and the
  wrapping idiom makes the split explicit rather than hidden inside a
  constructor. A `Workspace` return would have to be invented by
  N1.0a to suit one reader.
* **Top-level-only `.env*`.** Rejected: it would leave a nested
  `.env` readable while the root one is denied, which is the opposite
  of secret hygiene. Any-depth is the deny-on-doubt reading.

## Consequences

**What it costs.** One new module, one new exception type, one new
`Workspace` value, and a test suite that has to cover generated paths
(`..`, absolute, drive, UNC, mixed separators, symlink and explicit
symlink escape) plus protected-path denials with the frozen reason
strings. The Windows canonicalisation edge is the sharp part: where
`Path.resolve()` alone does not settle a short name, a drive-relative
form or a case variant, the module denies rather than guesses, which
can refuse a path a POSIX host would have allowed.

**What it does not change.** The sandbox helpers, the patch applier,
the permission policy, the loop ordering and the ledger are untouched
by this ADR. Existing callers of `sandbox.base.resolve_workspace` and
`validation.patch.safe_relative_path` are unaffected. Removing the
workspace module makes every work tool inert, a failure, not a crash
into an unjailed path.

**What remains open, owned elsewhere.** The prior-hash re-check in
`PatchApplier.apply` belongs to N1.2, not here; the subprocess
network limit and `preflight()` detail belong to N1.3; tool schemas,
registration and `--workspace` belong to N1.5; ledger journalling of
the denial reasons belongs to Wave 2, which must journal the post-hook
arguments so the ledger attests redacted values.

**How to roll it back.** Delete the module and revert the work tools.
No schema, ledger or config change has happened at this point, so the
revert is two-way and plain. After Wave 2 writes coder events, a
revert leaves forward-written rows unreadable by old binaries, which
is the known one-way door recorded for the event types and is not
introduced by this ADR.
