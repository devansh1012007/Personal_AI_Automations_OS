"""Embeddings, re-exported so callers have one place to ask for a model.

The implementation lives in :mod:`paa.storage.vector.embeddings`, next to the
vector stores that consume it. This module exists only so that "give me a
model" is a single import for every caller, whether they want completions or
vectors — it deliberately adds no behaviour and must never grow a competing
encoder.

The import is inside the function body rather than at module scope so that
``paa.models`` stays importable on a machine where the vector stack is absent
or half-installed: the model layer is a hard dependency of the orchestrator,
and recall is not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from paa.config import ModelSettings, Settings
    from paa.storage.vector.embeddings import Embedder

__all__ = ["get_embedder"]


def get_embedder(settings: ModelSettings | Settings) -> Embedder:
    """Return the configured encoder. See :func:`paa.storage.vector.embeddings.get_embedder`.

    The contract is the implementation's contract, unchanged and forwarded
    verbatim: the argument is a :class:`~paa.config.Settings` or its
    :class:`~paa.config.ModelSettings` half, and this function hands it
    straight on.

    **This is a narrowing of the previous signature**, which took ``Any`` and
    left the implementation to make sense of it. The implementation now
    resolves the argument with an ``isinstance`` test
    (:func:`paa.storage.vector.embeddings.resolve_model_settings`), and that
    test removed an *undocumented* tolerance: the old
    ``getattr(settings, "models", settings)`` fallback accepted a
    ``Settings``-shaped **proxy** — any object exposing a ``.models``
    attribute — and resolved it, where the ``isinstance`` test does not
    recognise a proxy and treats it as the model settings itself.

    The tolerance is gone rather than kept, for two reasons.

    * It was never a contract. Nothing in the repository passed a proxy, and
      nothing documented one; it was a by-product of duck typing, and a
      tolerance nobody can find is a tolerance nobody can rely on.
    * The two entry points have to agree. This module exists to be *one*
      import, not two contracts: a public wrapper that quietly accepted what
      its implementation refuses would be the inconsistency, not the cure.
      Keeping the tolerance here alone — it cannot be restored in
      :mod:`paa.storage.vector.embeddings` without a second, differently
      behaved resolution — would have made
      ``paa.models.get_embedder`` and
      ``paa.storage.vector.embeddings.get_embedder`` answer differently for
      the same argument.

    A caller that was relying on the proxy should pass the
    :class:`~paa.config.ModelSettings` it was holding, or the whole
    :class:`~paa.config.Settings` — both are accepted, and both reach the
    same encoder.
    """
    try:
        from paa.storage.vector.embeddings import get_embedder as _get_embedder
    except ImportError as exc:  # pragma: no cover - only on a broken install
        raise ImportError(
            "embeddings are provided by paa.storage.vector.embeddings, which "
            f"could not be imported ({exc}). Install numpy, or "
            'pip install "paa[embeddings]" for real sentence embeddings.'
        ) from exc
    return _get_embedder(settings)
