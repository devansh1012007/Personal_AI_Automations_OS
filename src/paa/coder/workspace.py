"""The shared path jail for the coder work tools (ADR-0045).

Every filesystem, shell and git tool imports its jail from this module and from
nowhere else (SPEC DEVIATION (docs/adr/0045)). The contract below is frozen by
ADR-0045; N1.1-N1.4 use it and must not re-implement it, because four jails are
four chances to diverge — and on Windows a divergence is a security bug (drive,
UNC, mixed separators, case, short names after :meth:`Path.resolve` alone).

Two jailed paths already exist, with deliberately different contracts, and this
module is the reference written against them rather than a call into them:
``sandbox.base.resolve_workspace`` / ``assert_inside_workspace`` take
candidate-first arguments and raise ``SandboxError``; ``validation.patch.
safe_relative_path`` takes a patch-relative string, strips git prefixes and
raises ``ValidationError``. A coder tool needs a ``Workspace``-shaped value, a
coder-local exception subclassing ``PaaError`` (the ledger-payload rule), and a
ledger-visible protected-path refusal. The module therefore reuses their
*patterns* — the control-character character class, the Windows absolute-path
recognition, resolve-then-compare with :meth:`Path.is_relative_to`, never string
prefix containment — and extends them for workspace-shaped (not patch-relative)
input.

The jail rule, in order: both sides of every comparison are fully canonicalised
(``expanduser().resolve()`` after the Windows pre-checks) before comparing, and
containment is :meth:`Path.is_relative_to` on resolved paths. Any
canonicalisation failure — ``OSError`` during resolve, permission denied, NUL or
control characters ``[\\x00-\\x1f\\x7f]`` in the input, drive-relative paths, UNC
prefixes, short-name ambiguity that cannot be resolved — denies with
:class:`WorkspaceViolation`; it never passes through. On POSIX a backslash is a
legal filename character, so the module does not manually rewrite separators:
OS :class:`~pathlib.Path` semantics decide what is a separator, and the
resolved-path check catches what the split does not. The check is best-effort
against a concurrent symlink swap between check and use; the applier's own
``realpath`` plus atomic replace is the second line (ADR-0045).

Protected paths are a frozen set with frozen reason strings, consulted *after*
the jail passes (the order is frozen; calling :func:`is_protected` alone is not
a jail): the top-level ``.git`` subtree, any-depth ``.env*``, and anywhere at or
under ``PAA_HOME`` (``get_settings().home``, re-read per call, never cached at
import). The refusal is not overrideable by an allow rule, a permission mode, or
``bypass``: jail and protection sit inside ``Tool.run()``, below every
permission decision.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.config import get_settings
from paa.core.errors import PaaError

__all__ = [
    "Workspace",
    "WorkspaceViolation",
    "assert_inside_workspace",
    "is_protected",
    "resolve_workspace",
]


#: The one escape reason frozen by ADR-0045; also the message, so a denial reads
#: the same in a ledger payload and in a tool result surfaced to the model.
#: Private: the frozen public surface is exactly ``__all__`` (ADR-0045), and
#: the protected strings are part of ``is_protected``'s return contract, so
#: tools surface them from the return value rather than importing them.
_REASON_ESCAPES = "path escapes the workspace jail"

#: The three frozen protected-path reason strings (ADR-0045). A tool that finds
#: a protected target surfaces the returned string verbatim.
_PROTECTED_GIT = "protected path: git"
_PROTECTED_ENV = "protected path: env"
_PROTECTED_PAA_HOME = "protected path: paa-home"

#: NUL and control characters. Same character class as
#: ``validation.patch._CONTROL_CHARS`` — copied, not imported, because importing
#: a private constant across packages would couple this jail to the patch
#: module's internals; ADR-0045 names the patch helper a reference, not a
#: provider.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: A drive-relative path: ``C:foo`` or a bare ``C:`` (the current directory on
#: drive C). It has no canonical meaning independent of a per-drive working
#: directory, so it denies on every host rather than being interpreted by the
#: one OS where :class:`~pathlib.Path` happens to understand it.
_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:(?![\\/])")

#: A UNC prefix, backslash or forward-slash spelling. Denied rather than resolved
#: (ADR-0045): a network share is not a supported coder workspace, and a share
#: path resolves differently depending on the resolver.
_UNC_PREFIX = re.compile(r"^(?:\\\\|//)")


@dataclass(frozen=True)
class Workspace:
    """A jailed working tree. ``root`` is canonical (see below)."""

    root: Path


class WorkspaceViolation(PaaError):
    """A workspace path was refused.

    ``message`` is the human- and model-readable refusal; ``path`` is the
    offending path as handed in, ``workspace_path`` the workspace it was checked
    against, and ``reason`` the stable denial reason. The latter three are the
    ledger detail keys (``PaaError`` merges details into event payloads
    verbatim), so a denial can be journalled without reconstruction.
    """

    def __init__(
        self,
        message: str,
        *,
        path: str,
        workspace_path: str,
        reason: str,
        **details: Any,
    ) -> None:
        super().__init__(
            message,
            path=path,
            workspace_path=workspace_path,
            reason=reason,
            **details,
        )
        self.path = path
        self.workspace_path = workspace_path
        self.reason = reason


def _violation(
    message: str,
    *,
    path: str,
    workspace_path: str,
    reason: str,
    **details: Any,
) -> WorkspaceViolation:
    return WorkspaceViolation(
        message, path=path, workspace_path=workspace_path, reason=reason, **details
    )


def _precheck_input(
    raw: str, *, path: str, workspace_path: str, reason: str
) -> None:
    """Deny inputs that no canonicalisation can be trusted to interpret.

    Runs on the raw string, before ``expanduser/resolve``: after resolution a
    NUL is gone (or the resolve raised), a drive-relative form has acquired a
    current directory from somewhere, and a UNC prefix has been split into
    components. Each is denied rather than reinterpreted (ADR-0045). The raw
    string is not stripped — leading/trailing whitespace is a legal POSIX
    filename character, and silently trimming it would change which file a
    tool means; an all-whitespace input is refused by the caller.
    """
    if _CONTROL_CHARS.search(raw):
        raise _violation(
            reason, path=path, workspace_path=workspace_path, reason=reason
        )
    if _UNC_PREFIX.match(raw):
        raise _violation(
            reason, path=path, workspace_path=workspace_path, reason=reason
        )
    if _DRIVE_RELATIVE.match(raw):
        raise _violation(
            reason, path=path, workspace_path=workspace_path, reason=reason
        )


def _canonicalise(
    target: Path, *, path: str, workspace_path: str, reason: str
) -> Path:
    """Fully canonicalise ``target`` (user expansion + resolution) or deny.

    :meth:`Path.resolve` collapses ``..`` *and* follows symlinks — including
    through non-existing tails, where the existing ancestor chain is still
    followed — so a symlink pointing outside the workspace becomes its true
    target before anything is compared against it. ``strict=False`` is the
    default since Python 3.6; non-existing tails are legal here because write
    tools jail paths that do not exist yet. An ``OSError`` (permission denied,
    symlink loop, short-name ambiguity the resolver cannot settle on Windows)
    or ``ValueError`` (e.g. an embedded NUL the pre-check somehow missed)
    denies with the caller's ``reason``, never passes through as an unresolved
    path.
    """
    try:
        return target.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _violation(
            reason, path=path, workspace_path=workspace_path, reason=reason
        ) from exc


def resolve_workspace(raw: str) -> Path:
    """Resolve a raw CLI workspace string to a canonical directory, or refuse.

    ``raw`` is the string the caller was handed (CLI input). It is expanded,
    resolved relative to the current working directory, and canonicalised; the
    result must exist and be a directory. The wrapping idiom is
    ``Workspace(root=resolve_workspace(raw))`` — this function returns the
    canonical :class:`~pathlib.Path`, not a :class:`Workspace`, so the two
    halves cannot drift. Relative ``..`` segments, absolute paths and ``~`` are
    all legitimate here: they are how a caller names a workspace, and
    resolution settles them. Creating a missing directory here is forbidden —
    booting against a missing workspace means the caller's model of the
    filesystem is already wrong, and inventing the directory would hide that.

    Raises :class:`WorkspaceViolation` with reason ``workspace does not
    exist``, ``workspace is not a directory`` or ``workspace cannot be
    canonicalised`` (blank input, NUL/control characters, drive-relative
    paths, UNC prefixes, or any resolution failure).
    """
    reason = "workspace cannot be canonicalised"
    if not raw or not raw.strip():
        raise _violation(reason, path=raw, workspace_path=raw, reason=reason)
    _precheck_input(raw, path=raw, workspace_path=raw, reason=reason)
    resolved = _canonicalise(Path(raw), path=raw, workspace_path=raw, reason=reason)
    if not resolved.exists():
        raise _violation(
            "workspace does not exist",
            path=raw,
            workspace_path=raw,
            reason="workspace does not exist",
        )
    if not resolved.is_dir():
        raise _violation(
            "workspace is not a directory",
            path=raw,
            workspace_path=raw,
            reason="workspace is not a directory",
        )
    return resolved


def assert_inside_workspace(ws: Workspace, target: Path) -> Path:
    """Assert ``target`` resolves to the workspace root or inside it, or raise.

    The workspace jail, workspace first — the reverse of the sandbox helper's
    candidate-first order, deliberately frozen so the two cannot be confused
    (ADR-0045). ``target`` is :class:`~pathlib.Path` only: tools convert string
    arguments to ``Path`` before calling, and no string is silently coerced.
    Both sides are canonicalised, so an escape through ``..``, an absolute path
    outside the workspace, a Windows drive/UNC form, or a symlink (including a
    symlink into a protected subtree) is caught by the resolved-path compare.
    The workspace root itself and its descendants are allowed; anything else —
    including a target or workspace that cannot be canonicalised at all — raises
    :class:`WorkspaceViolation` with reason ``path escapes the workspace
    jail``. The returned canonical path is the one the caller must use for I/O.

    INVARIANT: passing the jail is not passing protection. Callers must call
    :func:`is_protected` on the returned path before touching the disk.
    """
    reason = _REASON_ESCAPES
    target_str = str(target)
    resolved_ws = _canonicalise(
        ws.root, path=target_str, workspace_path=str(ws.root), reason=reason
    )
    if not target_str or not target_str.strip():
        raise _violation(
            reason, path=target_str, workspace_path=str(resolved_ws), reason=reason
        )
    _precheck_input(
        target_str, path=target_str, workspace_path=str(resolved_ws), reason=reason
    )
    resolved = _canonicalise(
        target, path=target_str, workspace_path=str(resolved_ws), reason=reason
    )
    if resolved != resolved_ws and not resolved.is_relative_to(resolved_ws):
        raise _violation(
            reason, path=str(resolved), workspace_path=str(resolved_ws), reason=reason
        )
    return resolved


def is_protected(ws: Workspace, target: Path) -> str | None:
    """Return the frozen protected reason for ``target``, or ``None``.

    The target is canonical-resolved *before* matching, so a symlink into a
    protected subtree is itself protected, and matching is case-insensitive via
    :meth:`str.casefold` — ``.GIT`` and ``.ENV`` are protected on Windows as
    well as POSIX. The set is frozen (ADR-0045):

    - the **top-level** ``.git`` subtree: the canonical target's first relative
      part casefolds to ``.git`` (covering the directory and the ``.git`` file
      used by linked worktrees; a nested ``.git`` in a fixture is not
      top-level and is not named by this clause — its traversal, if it escapes
      the workspace, is still caught by the jail);
    - any-depth ``.env*``: every relative filename segment casefold-matching
      the glob ``.env*`` via :func:`fnmatch.fnmatchcase` (``.env``,
      ``.env.local``, ``.ENV`` at any depth — a secret-shaped name is denied
      wherever it sits);
    - ``PAA_HOME``: the canonical target at or under the canonical ``PAA_HOME``
      from ``get_settings().home``, re-read per call and never cached at
      import (the settings cache is process-wide and tests move ``PAA_HOME``
      and call ``reset_settings_cache()``). If ``PAA_HOME`` equals the
      workspace root, every path in the workspace is protected.

    Returns one of ``protected path: git``, ``protected path: env``,
    ``protected path: paa-home``, or ``None``. When more than one clause
    matches, the most specific reason wins (git, then env, then paa-home — the
    catch-all last). The call never raises for an unprotected path; it raises
    :class:`WorkspaceViolation` only by denying, when the target or
    ``PAA_HOME`` cannot be canonicalised (with reason ``protected path:
    paa-home`` for the latter — the call must never treat an unreadable
    ``PAA_HOME`` as unprotected), or when handed a target outside the
    workspace, which the caller was required to jail first.

    INVARIANT (frozen order): callers must call this *after*
    :func:`assert_inside_workspace`; calling it alone is not a jail.
    """
    target_str = str(target)
    resolved_ws = _canonicalise(
        ws.root,
        path=target_str,
        workspace_path=str(ws.root),
        reason=_REASON_ESCAPES,
    )
    if not target_str or not target_str.strip():
        raise _violation(
            _REASON_ESCAPES,
            path=target_str,
            workspace_path=str(resolved_ws),
            reason=_REASON_ESCAPES,
        )
    _precheck_input(
        target_str,
        path=target_str,
        workspace_path=str(resolved_ws),
        reason=_REASON_ESCAPES,
    )
    resolved = _canonicalise(
        target,
        path=target_str,
        workspace_path=str(resolved_ws),
        reason=_REASON_ESCAPES,
    )
    if resolved != resolved_ws and not resolved.is_relative_to(resolved_ws):
        raise _violation(
            _REASON_ESCAPES,
            path=str(resolved),
            workspace_path=str(resolved_ws),
            reason=_REASON_ESCAPES,
        )
    parts = resolved.relative_to(resolved_ws).parts
    if parts and parts[0].casefold() == ".git":
        return _PROTECTED_GIT
    for part in parts:
        if fnmatch.fnmatchcase(part.casefold(), ".env*"):
            return _PROTECTED_ENV
    resolved_home = _canonicalise(
        get_settings().home,
        path=target_str,
        workspace_path=str(resolved_ws),
        reason=_PROTECTED_PAA_HOME,
    )
    if resolved == resolved_home or resolved.is_relative_to(resolved_home):
        return _PROTECTED_PAA_HOME
    return None
