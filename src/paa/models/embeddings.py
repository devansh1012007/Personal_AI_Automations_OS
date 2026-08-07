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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from paa.storage.vector.embeddings import Embedder

__all__ = ["get_embedder"]


def get_embedder(settings: Any) -> Embedder:
    """Return the configured encoder. See :func:`paa.storage.vector.embeddings.get_embedder`."""
    try:
        from paa.storage.vector.embeddings import get_embedder as _get_embedder
    except ImportError as exc:  # pragma: no cover - only on a broken install
        raise ImportError(
            "embeddings are provided by paa.storage.vector.embeddings, which "
            f"could not be imported ({exc}). Install numpy, or "
            'pip install "paa[embeddings]" for real sentence embeddings.'
        ) from exc
    return _get_embedder(settings)
