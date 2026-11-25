"""The thread control plane's refusal type.

One class, not one per verb: every refusal in this package — unknown
harness, unknown thread, closed thread, double-closed segment, a worktree
that could not be created — is an operator-facing boundary condition with
a message and machine-readable details, which is exactly what
:class:`~paa.core.errors.PaaError` already carries. The CLI prints
``message`` and exits non-zero; the details ride along for whoever wants
them.
"""

from __future__ import annotations

from paa.core.errors import PaaError

__all__ = ["ThreadError"]


class ThreadError(PaaError):
    """The thread control plane refused an operation."""
