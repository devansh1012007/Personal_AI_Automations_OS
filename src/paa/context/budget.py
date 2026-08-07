"""Token accounting — the mechanism behind the RFC §5 context ceiling.

:class:`TokenBudget` is the enforcement point, not an advisory counter. The
whole bounded-context design rests on one property: **a budget can never go
negative and can never over-spend its allocation**, no matter what sequence of
calls it receives. Every consumption path therefore checks before it debits,
and there is no code path that assigns to ``consumed`` without that check.

Sub-budgets are *linked* to their parent (see :meth:`TokenBudget.child`). A
delegated agent spending from its own envelope also debits the ancestor that
granted it, so the conservation property holds across a whole delegation tree
rather than only within a single node. Without that link, four depth-1 children
of a 1500-token root could collectively spend 3000 tokens while each stayed
politely inside its own 750, and the ceiling the DoD requires would be fiction.

Estimation is pluggable because token counts are model-specific. The cheap
character heuristic is the default and is always correct in *direction*; the
real tokenizer is used when it happens to be installed.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from paa.config import ContextSettings, get_settings
from paa.core.errors import BudgetExceededError
from paa.core.types import MODALITY_PROFILES, ComplexityModality, ModalityProfile

if TYPE_CHECKING:  # pragma: no cover — import-time typing only
    from collections.abc import Iterable

__all__ = [
    "CharEstimator",
    "TiktokenEstimator",
    "TokenBudget",
    "TokenEstimator",
    "default_estimator",
    "estimate_total",
    "tiktoken_available",
]

log = structlog.get_logger(__name__)

#: Shifting an int right by more than its bit length is well defined in Python
#: but pointlessly expensive to reason about; past this depth every quota is 0.
#: Mirrors the guard in :meth:`ModalityProfile.token_quota_at_depth`.
_MAX_SHIFT_DEPTH = 32


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------


@runtime_checkable
class TokenEstimator(Protocol):
    """Anything that can put a token count on a string.

    Implementations must be **deterministic** and **monotonic in length**: for
    any prefix relationship ``a`` ⊑ ``b``, ``estimate(a) <= estimate(b)``. The
    compactor's budget fitting binary-searches over prefix lengths and would be
    incorrect against a non-monotonic estimator.
    """

    def estimate(self, text: str) -> int:
        """Tokens ``text`` is expected to occupy. Never negative."""
        ...


class CharEstimator:
    """Characters-per-token heuristic. The default estimator.

    Uses ``ContextSettings.chars_per_token`` (4.0, the standard English
    approximation) and rounds **up** via :func:`math.ceil`. Rounding up is the
    safe direction: over-estimating tokens packs a slightly smaller context,
    while under-estimating breaches the ceiling the whole subsystem exists to
    defend. Cheap and dependency-free, which matters because this runs on every
    candidate fact of every task.
    """

    __slots__ = ("chars_per_token",)

    def __init__(self, chars_per_token: float = 4.0) -> None:
        if not math.isfinite(chars_per_token) or chars_per_token <= 0:
            raise ValueError(f"chars_per_token must be strictly positive, got {chars_per_token!r}")
        self.chars_per_token = float(chars_per_token)

    @classmethod
    def from_settings(cls, settings: ContextSettings | None = None) -> CharEstimator:
        resolved = settings if settings is not None else get_settings().context
        return cls(chars_per_token=resolved.chars_per_token)

    def estimate(self, text: str) -> int:
        return math.ceil(len(text) / self.chars_per_token)

    def __repr__(self) -> str:
        return f"CharEstimator(chars_per_token={self.chars_per_token!r})"


class TiktokenEstimator:
    """Exact BPE token counts, when ``tiktoken`` happens to be importable.

    **Not a declared dependency** and deliberately so — ``tiktoken`` pulls a
    Rust extension and a network-fetched vocabulary, neither of which this
    local-first runtime will require of its users (docs/adr/0001). It is used
    opportunistically when present and silently absent otherwise.

    Construction raises :class:`ImportError` when the package is missing;
    callers that want a graceful fallback should use :func:`default_estimator`.
    """

    __slots__ = ("_encoding", "encoding_name")

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        try:
            import tiktoken
        except ImportError as exc:  # pragma: no cover — depends on the environment
            raise ImportError(
                "TiktokenEstimator requires the optional 'tiktoken' package; "
                "use CharEstimator or default_estimator() instead"
            ) from exc
        self.encoding_name = encoding_name
        self._encoding = tiktoken.get_encoding(encoding_name)

    def estimate(self, text: str) -> int:
        return len(self._encoding.encode(text, disallowed_special=()))

    def __repr__(self) -> str:
        return f"TiktokenEstimator(encoding_name={self.encoding_name!r})"


def tiktoken_available() -> bool:
    """Whether the optional exact tokenizer can be constructed in this process."""
    try:
        import tiktoken  # noqa: F401
    except ImportError:
        return False
    return True


def default_estimator(settings: ContextSettings | None = None) -> TokenEstimator:
    """Best estimator this environment can provide.

    Prefers :class:`TiktokenEstimator` and falls back to :class:`CharEstimator`.
    Both are deterministic, so a packet compiled twice on the *same* machine is
    always identical; across machines with different optional packages the token
    counts may differ, which is why anything asserting exact counts should pin
    an estimator explicitly rather than call this.
    """
    try:
        return TiktokenEstimator()
    # Broad by design: a missing package, a corrupt vocabulary cache and an
    # offline vocabulary fetch all mean the same thing here — fall back rather
    # than fail a task over an optional optimisation.
    except Exception as exc:
        log.debug("tiktoken_unavailable_using_char_estimator", error=str(exc))
        return CharEstimator.from_settings(settings)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class TokenBudget:
    """A monotonically-draining allocation of context tokens.

    Invariants, all of which hold after *any* sequence of public calls:

    * ``0 <= consumed <= allocated``
    * ``remaining == allocated - consumed`` and is never negative
    * a rejected consumption changes nothing — no partial debits, on this
      budget or on any ancestor

    The third is why :meth:`try_consume` checks the parent *before* debiting
    itself: a child that debited first and then discovered the parent could not
    afford the spend would have to roll back, and a rollback path is a bug
    surface that simply not existing avoids.
    """

    __slots__ = ("_profile", "allocated", "consumed", "depth", "kind", "parent")

    def __init__(
        self,
        allocated: int,
        *,
        kind: str = "context_tokens",
        depth: int = 0,
        profile: ModalityProfile | None = None,
        parent: TokenBudget | None = None,
    ) -> None:
        """
        :param allocated: total tokens this budget may ever hand out.
        :param kind: label carried into :class:`BudgetExceededError` payloads so
            a ledger reader can tell a context overrun from a wall-clock one.
        :param depth: delegation depth this budget sits at. Set by
            :meth:`child`; callers building a root budget leave it at 0.
        :param profile: modality envelope this budget was carved from, when one
            applies. Lets :meth:`child` defer to
            :meth:`ModalityProfile.token_quota_at_depth` rather than restating
            the RFC §15.7 halving rule.
        :param parent: ancestor to also debit. Set by :meth:`child`; passing it
            directly is supported but rarely what you want.
        """
        if isinstance(allocated, bool) or not isinstance(allocated, int):
            raise ValueError(f"allocated must be an int, got {type(allocated).__name__}")
        if allocated < 0:
            raise ValueError(f"allocated must be non-negative, got {allocated}")
        if depth < 0:
            raise ValueError(f"depth must be non-negative, got {depth}")

        self.allocated = allocated
        self.consumed = 0
        self.kind = kind
        self.depth = depth
        self.parent = parent
        self._profile = profile

    # -- construction ------------------------------------------------------

    @classmethod
    def for_modality(
        cls,
        modality: ComplexityModality,
        *,
        depth: int = 0,
        kind: str = "context_tokens",
    ) -> TokenBudget:
        """Root budget sized by a complexity modality's ceiling (RFC §9.2).

        ``SIMPLE`` yields a 0-token budget by design — the RFC bypasses the LLM
        entirely at that modality, so a planner that tries to spend against it
        is refused rather than quietly given a default.
        """
        profile = MODALITY_PROFILES[modality]
        return cls(
            profile.token_quota_at_depth(depth),
            kind=kind,
            depth=depth,
            profile=profile,
        )

    @classmethod
    def from_settings(
        cls,
        settings: ContextSettings | None = None,
        *,
        worker: bool = False,
    ) -> TokenBudget:
        """Root budget at the configured planner (1500) or worker (1000) ceiling."""
        resolved = settings if settings is not None else get_settings().context
        ceiling = resolved.worker_token_ceiling if worker else resolved.token_ceiling
        return cls(ceiling, kind="worker_tokens" if worker else "context_tokens")

    # -- state -------------------------------------------------------------

    @property
    def remaining(self) -> int:
        """Tokens still available. Never negative."""
        return self.allocated - self.consumed

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def can_afford(self, tokens: int) -> bool:
        """Whether ``tokens`` would fit here *and* in every ancestor."""
        tokens = self._check_tokens(tokens)
        if tokens > self.remaining:
            return False
        return self.parent is None or self.parent.can_afford(tokens)

    # -- consumption -------------------------------------------------------

    def try_consume(self, tokens: int) -> bool:
        """Debit ``tokens`` if they fit; otherwise change nothing and return ``False``.

        The non-raising form, used by the gatherer's packing loop where a fact
        that does not fit is an ordinary outcome rather than an error.
        """
        tokens = self._check_tokens(tokens)
        if tokens > self.remaining:
            return False
        # Ask the ancestor first: see the class docstring on why there is no
        # rollback path.
        if self.parent is not None and not self.parent.try_consume(tokens):
            return False
        self.consumed += tokens
        return True

    def consume_or_raise(self, tokens: int) -> None:
        """Debit ``tokens`` or raise :class:`BudgetExceededError`.

        The raising form, for call sites where exceeding the budget means the
        task cannot proceed at all and should be recorded as a failure.
        """
        tokens = self._check_tokens(tokens)
        if not self.try_consume(tokens):
            raise BudgetExceededError(
                self.kind,
                limit=float(self.allocated),
                consumed=float(self.consumed),
                requested=tokens,
                remaining=self.remaining,
                depth=self.depth,
            )

    def consume_text(self, text: str, estimator: TokenEstimator) -> bool:
        """Estimate ``text`` and try to debit it. Convenience over :meth:`try_consume`."""
        return self.try_consume(estimator.estimate(text))

    # -- delegation --------------------------------------------------------

    def child(self, depth: int = 1) -> TokenBudget:
        r"""Sub-budget for an agent nested ``depth`` levels below this one.

        RFC §15.7: :math:`quota(d) = ceiling / 2^{d}`, implemented as a right
        shift. Halving per level is what keeps deep plans affordable — a depth-2
        recursion that inherited the full parent budget at every level would
        cost four times the root allocation.

        Delegates to :meth:`ModalityProfile.token_quota_at_depth` when this
        budget was built from a modality, so the halving rule lives in exactly
        one place.

        The result is additionally capped at this budget's ``remaining``. A
        child envelope larger than what the parent still holds would be a
        promise the parent cannot keep; the cap makes the advertised allocation
        honest at the moment of hand-off, and the parent link keeps it honest
        afterwards.
        """
        if isinstance(depth, bool) or not isinstance(depth, int):
            raise ValueError(f"depth must be an int, got {type(depth).__name__}")
        if depth < 0:
            raise ValueError(f"depth must be non-negative, got {depth}")

        if self._profile is not None:
            quota = self._profile.token_quota_at_depth(self.depth + depth)
        elif depth < _MAX_SHIFT_DEPTH:
            quota = self.allocated >> depth
        else:
            quota = 0

        return TokenBudget(
            min(quota, self.remaining),
            kind=self.kind,
            depth=self.depth + depth,
            profile=self._profile,
            parent=self,
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _check_tokens(tokens: int) -> int:
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            raise ValueError(f"tokens must be an int, got {type(tokens).__name__} {tokens!r}")
        if tokens < 0:
            raise ValueError(f"tokens must be non-negative, got {tokens}")
        return tokens

    def __repr__(self) -> str:
        return (
            f"TokenBudget(allocated={self.allocated}, consumed={self.consumed}, "
            f"remaining={self.remaining}, depth={self.depth}, kind={self.kind!r})"
        )


def estimate_total(texts: Iterable[str], estimator: TokenEstimator) -> int:
    """Summed estimate for a collection of strings.

    Sums the *per-string* estimates rather than estimating the concatenation.
    The two differ under a real BPE tokenizer (tokens can straddle a boundary),
    and the per-string sum is the conservative one — it is what the gatherer
    charges, so it must be what any pre-flight check measures.
    """
    return sum(estimator.estimate(text) for text in texts)
