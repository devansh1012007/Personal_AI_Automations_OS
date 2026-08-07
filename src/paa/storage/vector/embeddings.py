"""Text encoders for semantic recall.

Why there are two of these
--------------------------
RFC §5 makes semantic recall a hard dependency of context construction: the
gatherer's ``relevance_floor`` (0.75 cosine, see :class:`~paa.config.
ContextSettings`) is a meaningless number without an encoder behind it. But the
encoder the RFC implies — a sentence-transformers MiniLM — arrives with torch
bolted to it: ~2 GB on disk and several hundred MB resident. Per ADR-0007 that
is not always affordable on the target machine, and it is never affordable in
CI.

So there are two encoders behind one :class:`Embedder` protocol:

* :class:`SentenceTransformerEmbedder` — the real one. 384 dimensions, which is
  what the RFC's Qdrant collections are sized for.
* :class:`HashEmbedder` — a deterministic feature-hashing fallback whose only
  dependency is numpy. It keeps the runtime *functional*, not *good*. Read its
  docstring before trusting a recall decision made under it.

Which one is live is observable through :attr:`Embedder.model_name`, which is
meant to be written into the ledger alongside any recall result. A retrieval
that happened under the fallback should be identifiable — and re-runnable —
after the real model is installed.

Everything here is async because the real encoder is a blocking CPU-bound call
that must not sit on the event loop; it is handed to :func:`asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import numpy as np
import structlog

from paa.core.errors import StorageError

if TYPE_CHECKING:
    from paa.config import ModelSettings, Settings

__all__ = [
    "Embedder",
    "FloatArray",
    "HashEmbedder",
    "SentenceTransformerEmbedder",
    "cosine_similarity",
    "get_embedder",
]

log = structlog.get_logger(__name__)

#: A 2-D ``(n, dim)`` or 1-D ``(dim,)`` array of float32. Every encoder in this
#: module returns float32 — half the memory of float64 for a recall quality
#: difference that does not survive rounding to the 0.75 relevance floor.
FloatArray = np.ndarray[Any, np.dtype[np.float32]]

#: Substrate tag on errors raised from this module. The vector *stores* use
#: "qdrant"/"numpy"; encoding failures are a distinct thing to page on.
_SUBSTRATE = "embeddings"

#: Batches at or below this size are encoded inline. The hash encoder costs
#: microseconds per short document, and a thread hop costs more than the work.
_INLINE_BATCH_LIMIT = 64

#: Word characters only. Deliberately not unicode-aware punctuation handling —
#: this is a fallback, and pretending otherwise would be dishonest.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@runtime_checkable
class Embedder(Protocol):
    """Anything that can turn text into unit vectors of a fixed width."""

    @property
    def dimensions(self) -> int:
        """Width of the vectors produced. Must match the collection's size."""
        ...

    @property
    def model_name(self) -> str:
        """Identifier recorded in the ledger next to any recall this encoder fed."""
        ...

    async def embed(self, texts: list[str]) -> FloatArray:
        """Encode a batch. Returns ``(len(texts), dimensions)`` float32, L2-normalised."""
        ...

    async def embed_one(self, text: str) -> FloatArray:
        """Encode a single string. Returns ``(dimensions,)`` float32."""
        ...


class _EmbedderBase:
    """Shared plumbing so both encoders agree on the single-item convenience path."""

    @property
    def dimensions(self) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    async def embed(self, texts: list[str]) -> FloatArray:  # pragma: no cover - overridden
        raise NotImplementedError

    async def embed_one(self, text: str) -> FloatArray:
        matrix = await self.embed([text])
        return np.asarray(matrix[0], dtype=np.float32)


# ---------------------------------------------------------------------------
# Real embeddings
# ---------------------------------------------------------------------------


class SentenceTransformerEmbedder(_EmbedderBase):
    """sentence-transformers encoder, loaded on first use.

    The model is *not* loaded in ``__init__``. Constructing this class must stay
    free: :func:`get_embedder` runs during startup wiring, and a 90 MB download
    plus a torch import is not something a constructor should do. The first
    :meth:`embed` call pays that cost, inside a worker thread.

    Nothing in this module imports torch at module scope, which is what lets
    ``paa.storage.vector`` be imported on a machine that has never seen it.

    Encoding is serialised behind a lock. RFC §6.2 caps concurrent generative
    streams at 2 on constrained hardware, and a second resident copy of the
    model is exactly the kind of allocation that pushes this box into swap.
    Serialising also sidesteps any question about the thread-safety of the
    underlying torch module.
    """

    def __init__(
        self,
        model_name: str,
        dimensions: int,
        *,
        device: str | None = None,
    ) -> None:
        self._model_name = model_name
        self._dimensions = dimensions
        self._device = device
        self._model: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def is_loaded(self) -> bool:
        """Whether the model weights are resident. Useful for startup probes."""
        return self._model is not None

    async def embed(self, texts: list[str]) -> FloatArray:
        if not texts:
            return np.zeros((0, self._dimensions), dtype=np.float32)
        async with self._lock:
            return await asyncio.to_thread(self._encode, texts)

    async def warm(self) -> None:
        """Load the weights now rather than on the first latency-sensitive call."""
        async with self._lock:
            await asyncio.to_thread(self._load)

    # -- blocking section; only ever runs in a worker thread ----------------

    def _load(self) -> Any:
        if self._model is not None:
            return self._model

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise StorageError(
                "sentence-transformers is not installed; "
                'install the extra with `pip install "paa[embeddings]"` '
                "or enable models.allow_hash_embedder_fallback",
                substrate=_SUBSTRATE,
                model=self._model_name,
            ) from exc

        log.info("embeddings.loading_model", model=self._model_name, device=self._device)
        try:
            model = SentenceTransformer(self._model_name, device=self._device)
        except Exception as exc:
            raise StorageError(
                f"failed to load embedding model: {exc}",
                substrate=_SUBSTRATE,
                model=self._model_name,
            ) from exc

        actual = model.get_sentence_embedding_dimension()
        if actual is not None and int(actual) != self._dimensions:
            # Loud failure rather than silent truncation: a width mismatch means
            # every vector already in the collection was written by a different
            # model, and mixing them produces confident nonsense.
            raise StorageError(
                "embedding model width does not match the configured dimensions",
                substrate=_SUBSTRATE,
                model=self._model_name,
                model_dimensions=int(actual),
                configured_dimensions=self._dimensions,
            )

        self._model = model
        log.info("embeddings.model_loaded", model=self._model_name, dimensions=self._dimensions)
        return model

    def _encode(self, texts: list[str]) -> FloatArray:
        model = self._load()
        try:
            raw = model.encode(
                texts,
                batch_size=min(32, len(texts)),
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            raise StorageError(
                f"embedding failed: {exc}",
                substrate=_SUBSTRATE,
                model=self._model_name,
                batch_size=len(texts),
            ) from exc
        return np.ascontiguousarray(raw, dtype=np.float32).reshape(len(texts), self._dimensions)


# ---------------------------------------------------------------------------
# Fallback embeddings
# ---------------------------------------------------------------------------


class HashEmbedder(_EmbedderBase):
    """Deterministic feature-hashing encoder. No dependencies beyond numpy.

    How it works
    ------------
    The hashing trick (Weinberger et al., 2009): tokenise, hash each token into
    one of ``dimensions`` buckets, and accumulate a *signed* sublinear term-
    frequency into that bucket. The sign is drawn from an independent bit of the
    same digest, so colliding tokens cancel in expectation rather than
    systematically inflating a bucket. The result is L2-normalised, so a dot
    product is a cosine.

    What it is good for
    -------------------
    Exact and near-exact lexical overlap. Identical text scores 1.0 against
    itself; text sharing most of its vocabulary scores high; unrelated text
    scores near zero. That is enough to keep the ingestion pipeline,
    de-duplication, the vector stores and their tests honest without a 2 GB
    dependency.

    What it is NOT good for — read this before shipping on it
    ---------------------------------------------------------
    This is a **materially worse** encoder than a real sentence model, and the
    gap is not a matter of degree in one place, it is a different capability:

    * No semantics whatsoever. "car" and "automobile" are as unrelated as "car"
      and "xylophone". Any recall that depends on paraphrase — which is most of
      what RFC §5 asks semantic recall to do — simply fails.
    * No word order. "the dog bit the man" and "the man bit the dog" are the
      same vector.
    * No subword robustness. "running" and "run" do not match; a typo is a miss.
    * The 0.75 ``relevance_floor`` is calibrated for a sentence model. Against
      these vectors it behaves like a strict lexical-overlap threshold, so
      recall is much lower than the configuration implies. It is not a bug in
      the floor; the floor is measuring a different quantity.

    Treat it as a *degraded operating mode*, not an alternative. That is why
    :func:`get_embedder` warns loudly when it selects this, and why
    :attr:`model_name` is a distinct string that should be persisted next to
    any result derived from it.
    """

    #: Prefix marks the mode in ledger payloads. Grep-able after the fact.
    MODEL_PREFIX = "hash-embedder-v1"

    def __init__(self, dimensions: int = 384) -> None:
        if dimensions < 8:
            raise ValueError(f"dimensions must be at least 8, got {dimensions}")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return f"{self.MODEL_PREFIX}-{self._dimensions}d"

    async def embed(self, texts: list[str]) -> FloatArray:
        if not texts:
            return np.zeros((0, self._dimensions), dtype=np.float32)
        if len(texts) > _INLINE_BATCH_LIMIT:
            return await asyncio.to_thread(self._encode, texts)
        return self._encode(texts)

    def encode_sync(self, texts: list[str]) -> FloatArray:
        """Synchronous escape hatch for non-async callers (CLI, migrations)."""
        return self._encode(texts)

    # -- internals ---------------------------------------------------------

    def _encode(self, texts: list[str]) -> FloatArray:
        out = np.zeros((len(texts), self._dimensions), dtype=np.float32)
        for row, text in enumerate(texts):
            out[row] = self._encode_one(text)
        return out

    def _encode_one(self, text: str) -> FloatArray:
        vector = np.zeros(self._dimensions, dtype=np.float32)

        # Sublinear tf (1 + log n) rather than raw counts: a word repeated ten
        # times is more relevant than a word used once, but not ten times more.
        for token, count in Counter(_TOKEN_RE.findall(text.lower())).items():
            bucket, sign = self._bucket_and_sign(token)
            vector[bucket] += sign * (1.0 + math.log(count))

        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            # Empty/punctuation-only input, or the astronomically unlikely case
            # of signed collisions cancelling exactly. Either way the caller was
            # promised a unit vector, so emit a deterministic basis vector keyed
            # on the whole string instead of a zero that would poison a cosine.
            return self._degenerate(text)
        return (vector / norm).astype(np.float32, copy=False)

    def _bucket_and_sign(self, token: str) -> tuple[int, float]:
        # blake2b, not the built-in hash(): PYTHONHASHSEED randomises str
        # hashing per process, and a vector store whose contents depend on the
        # process that wrote them is not a store.
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        # Top bit for the sign, low bits for the bucket — independent enough.
        return value % self._dimensions, (1.0 if value >> 63 else -1.0)

    def _degenerate(self, text: str) -> FloatArray:
        vector = np.zeros(self._dimensions, dtype=np.float32)
        digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
        vector[int.from_bytes(digest, "big") % self._dimensions] = 1.0
        return vector


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _sentence_transformers_available() -> bool:
    """Presence check that does not import torch.

    :func:`importlib.util.find_spec` locates the distribution without executing
    its ``__init__``, so this stays cheap even when the extra *is* installed.
    """
    try:
        return importlib.util.find_spec("sentence_transformers") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def get_embedder(settings: ModelSettings | Settings) -> Embedder:
    """Pick an encoder: the real model if it is installed, else the fallback.

    Accepts either the full :class:`~paa.config.Settings` or just its
    :class:`~paa.config.ModelSettings` sub-model, because half the call sites
    have one and half have the other.

    Raises :class:`~paa.core.errors.StorageError` when sentence-transformers is
    absent and ``allow_hash_embedder_fallback`` is off — that combination is an
    explicit statement that degraded recall is unacceptable, so booting anyway
    would be the wrong kind of helpful.
    """
    models = getattr(settings, "models", settings)

    if _sentence_transformers_available():
        log.debug(
            "embeddings.using_sentence_transformer",
            model=models.embedding_model,
            dimensions=models.embedding_dimensions,
        )
        return SentenceTransformerEmbedder(
            models.embedding_model, models.embedding_dimensions
        )

    if not models.allow_hash_embedder_fallback:
        raise StorageError(
            "sentence-transformers is not installed and the hash-embedder "
            'fallback is disabled; install `pip install "paa[embeddings]"` or '
            "set models.allow_hash_embedder_fallback=true",
            substrate=_SUBSTRATE,
            model=models.embedding_model,
        )

    embedder = HashEmbedder(models.embedding_dimensions)
    log.warning(
        "embeddings.hash_fallback_active",
        requested_model=models.embedding_model,
        active_model=embedder.model_name,
        dimensions=embedder.dimensions,
        impact=(
            "SEMANTIC RECALL IS DEGRADED. The hash embedder matches lexical "
            "overlap only — no synonyms, no paraphrase, no word order. The "
            "context.relevance_floor of 0.75 no longer means what it was "
            "calibrated to mean. Install paa[embeddings] before relying on "
            "recall quality."
        ),
    )
    return embedder


def cosine_similarity(a: FloatArray, b: FloatArray) -> float:
    """Cosine between two 1-D vectors, safe on zero vectors (returns 0.0).

    Encoders in this module already return unit vectors, so this is a dot
    product in the common case; the normalisation is here for callers holding
    vectors of unknown provenance.
    """
    left = np.asarray(a, dtype=np.float32).ravel()
    right = np.asarray(b, dtype=np.float32).ravel()
    if left.shape != right.shape:
        raise ValueError(f"shape mismatch: {left.shape} vs {right.shape}")
    denominator = float(np.linalg.norm(left)) * float(np.linalg.norm(right))
    if denominator == 0.0:
        return 0.0
    return float(np.clip(float(left @ right) / denominator, -1.0, 1.0))
