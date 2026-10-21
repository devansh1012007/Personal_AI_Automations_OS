"""The Windows-only half of the sandbox, behind one honest door.

Two facts about Windows belong here rather than being spelled out at each call
site. Both of them are invisible to a type checker running on Linux, and both
of them are load-bearing.

**``ctypes`` exposes Windows-only attributes.** ``ctypes.WinDLL`` and
``ctypes.get_last_error`` exist only on Windows — typeshed guards both with
``sys.platform == "win32"`` — yet the code that uses them (the Job Object
memory cap, the native RSS sampler) is Windows-only too. A checker running
against a Linux host therefore reports an ``attr-defined`` error for a call
that is perfectly correct on the platform it will ever run on, and the two
available workarounds are both bad. A blanket ``# type: ignore`` also hides
any *other* error on the line; deleting the Windows path throws away the
strongest containment this codebase has on Windows.

Reaching the attributes through :func:`getattr` is the honest middle. The
lookup succeeds exactly when the call site can run, and what comes back is
*typed* — ``ctypes.CDLL`` — so the checker verifies every use of the function
table instead of trusting a suppression it has to take on faith.

**``sys.platform`` is folded at check time.** mypy resolves
``sys.platform == "win32"`` against the platform it is checking for, so on a
Linux host it *proves* that a Windows branch is dead — and with
``warn_unreachable`` it then reports the code after that branch as unreachable.
The branch is not dead. It is the branch that runs on the target host, and a
checker that cannot see it is a checker that will one day "fix" it by
deleting it.

A module-level :data:`IS_WINDOWS` constant is not folded, so the branch stays
visible to the checker. It is also the one place that answers the question, so
the sandbox's platform branches cannot disagree with each other.
"""

from __future__ import annotations

import ctypes
import sys

__all__ = ["IS_WINDOWS", "last_error", "win_dll"]

#: Whether this process is running on Windows.
#:
#: A constant rather than a bare ``sys.platform == "win32"`` at each call
#: site, for the reason in the module docstring: a direct comparison is
#: folded against the platform being checked for, and the branch it guards
#: then reads as dead code.
IS_WINDOWS: bool = sys.platform == "win32"


def win_dll(name: str) -> ctypes.CDLL:
    """Load a Windows DLL and return its function table.

    ``use_last_error=True`` is deliberate. The functions in these DLLs report
    failure through ``GetLastError`` rather than through ``errno``, and
    :func:`last_error` is how the caller reads the reason; a DLL loaded
    without it would report the wrong error for every failure.

    The return type is ``ctypes.CDLL`` — the cross-platform base class —
    because ``WinDLL`` is a subclass of it and exists only on Windows. The
    *function table* is untyped at the call boundary either way: ``ctypes``
    resolves a foreign function's signature when it is called, so every call
    through it returns ``Any``.

    Raises :class:`AttributeError` on a platform without ``ctypes.WinDLL``.
    Every call site is already behind a Windows guard, so this is the
    "someone moved the guard" failure rather than a runtime condition.

    The DLL's name is in the message because the callers load more than one:
    a report that says only "ctypes.WinDLL is unavailable" cannot say whether
    ``kernel32`` or ``psapi`` was the one that could not be loaded, and the
    two fail for different reasons.
    """
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise AttributeError(
            f"ctypes.WinDLL is unavailable on {sys.platform} (cannot load {name!r})"
        )
    dll: ctypes.CDLL = loader(name, use_last_error=True)
    return dll


def last_error() -> int:
    """The last Win32 error code, or ``0`` when the platform has no such thing.

    ``0`` is the Win32 convention for "no error was recorded". It is only
    ever logged — the caller has already established that the operation
    failed — so an unreadable code must not raise a second failure on top of
    the first.
    """
    getter = getattr(ctypes, "get_last_error", None)
    if getter is None:
        return 0
    return int(getter())
