"""The cold-lake blob-store contract.

Two implementations satisfy it: the local content-addressed store
(:class:`paa.storage.coldlake.cas.ContentAddressedStore`, ADR-0004) and the
S3 client (:class:`paa.storage.coldlake.minio_backend.S3BlobStore`, ADR-0019,
ADR-0026). The repositories depend on this contract rather than on either
class, which is what lets a deployment choose the substrate by configuration
instead of by code.

The contract is deliberately narrow — the operations the cold lake actually
performs. It is *not* the union of both classes' methods: ``iter_blobs`` and
``total_bytes`` are filesystem bookkeeping with no S3 counterpart that means
the same thing, and a protocol that included them would force the S3
implementation to fake them.

Narrow is not the same as approximate. Every signature below is the one the
implementations actually have, because ``runtime_checkable`` proves only that
the *methods* exist — a protocol that named a keyword no implementation
accepts would pass ``isinstance`` and then fail in the caller, at runtime,
with a ``TypeError`` that says nothing about the real mistake. The
conformance is proved by a test (``tests/storage/test_blobstore_contract.py``)
rather than asserted in prose.

The error clause
----------------
**A contract that is silent about failure is a contract that cannot be
honoured.** This one used to be silent: it declared six methods and said
nothing about what any of them raises, and the result was that the *same*
condition — a missing bucket — arrived as two different exception types
depending on which method the caller used, so no caller could write a correct
``except`` clause at all.

The vocabulary is in :mod:`paa.storage.coldlake.errors` (ADR-0032), it is the
same on both substrates, and every member of it is a
:class:`paa.core.errors.StorageError` — so a caller that catches
``StorageError`` keeps catching everything it caught before, and a caller that
wants to know *which* failure it was can now say so. The members are:

``NotFound``
    The addressed thing is not there: a missing blob, or — on the S3
    substrate — a missing bucket.
``AccessDenied``
    The substrate refused: wrong credentials, or not permitted.
``Unreachable``
    The substrate could not be reached, or could not complete the request.
    Retryable.
``Corrupt``
    The bytes are not what the address says.
``MalformedRequest``
    The request was refused as malformed: a digest that is not a digest, a
    size hint that disagrees with the bytes.
``NotSupported``
    The substrate cannot serve the request at all.
``BlobFailure``
    The substrate failed, and the reason is not one the vocabulary names.

A driver's exception never reaches a caller: the translation happens at the
substrate boundary, once, and it is idempotent — an error that is already one
of ours comes back unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import IO, Protocol, runtime_checkable

from paa.storage.coldlake.cas import BlobRef

__all__ = ["BlobStore"]


@runtime_checkable
class BlobStore(Protocol):
    """Immutable, content-addressed blob storage.

    Every method is synchronous in both implementations: a store that is
    local is a bounded file read or write, and a store that is S3 is a
    bounded HTTP request. Wrapping them in coroutines would buy nothing but
    the illusion that they yield. A caller on the event loop that expects
    large payloads should use :func:`asyncio.to_thread`.
    """

    def put(self, data: bytes, *, compress: bool = True) -> BlobRef:
        """Store ``data`` and return a reference to it.

        The digest is over the *raw* bytes, never the compressed frame, so
        the reference is the same whichever way the blob was written.

        Raises :class:`~paa.storage.coldlake.errors.NotFound` if the
        substrate's namespace is absent (the S3 bucket), which is the one
        failure a write cannot recover from and cannot be mistaken for
        anything else; :class:`~paa.storage.coldlake.errors.AccessDenied` if
        the substrate refuses; :class:`~paa.storage.coldlake.errors.Unreachable`
        if it cannot be reached; and
        :class:`~paa.storage.coldlake.errors.MalformedRequest` if the payload
        is one this substrate cannot accept.
        """
        ...

    def put_stream(
        self,
        source: IO[bytes],
        *,
        compress: bool = True,
        size: int | None = None,
    ) -> BlobRef:
        """Store the contents of ``source`` and return a reference to it.

        ``size`` is an optional hint at the raw payload length, and a hint
        rather than a promise: a store told a length the bytes contradict
        refuses the write rather than recording a size that is a lie.

        The names here are the implementations' names, deliberately — the
        module docstring says why a protocol's signatures are load-bearing
        and where the conformance is proved.

        Raises the same failures as :meth:`put`, and in particular
        :class:`~paa.storage.coldlake.errors.MalformedRequest` when the
        declared size and the bytes read disagree — the same error whichever
        way the bytes were written.
        """
        ...

    def get(self, digest: str) -> bytes:
        """The stored bytes for ``digest``, verified against it.

        Raises :class:`~paa.storage.coldlake.errors.NotFound` if the blob is
        not there — with the **digest** in the error's details, because the
        digest is the address the caller asked about;
        :class:`~paa.storage.coldlake.errors.Corrupt` if the bytes fail
        verification or are not a readable frame;
        :class:`~paa.storage.coldlake.errors.MalformedRequest` if the digest
        is not a digest; and the substrate's own refusals as above.
        """
        ...

    def get_stream(self, digest: str) -> Iterator[bytes]:
        """The stored bytes for ``digest``, in chunks, verified against it.

        The :class:`~paa.storage.coldlake.errors.Corrupt` failure can only be
        raised *after* the final chunk, because a hash over partial data
        means nothing, so a consumer that writes chunks straight to disk must
        treat its output as unverified until the iterator is exhausted.
        Everything else raises as :meth:`get` does — a
        :class:`~paa.storage.coldlake.errors.NotFound` for a blob that is not
        there, and the substrate's own refusals.
        """
        ...

    def exists(self, digest: str) -> bool:
        """Whether a blob with this digest is stored.

        A blob that is **not** stored is not an error: the answer is
        ``False``. Everything that stops the store from answering *is* — a
        digest that is not a digest
        (:class:`~paa.storage.coldlake.errors.MalformedRequest`), a
        substrate that refuses
        (:class:`~paa.storage.coldlake.errors.AccessDenied`) or cannot be
        reached (:class:`~paa.storage.coldlake.errors.Unreachable`), and a
        substrate whose namespace is absent
        (:class:`~paa.storage.coldlake.errors.NotFound`), which is raised
        rather than answered ``False`` because "the store cannot answer" and
        "the blob is not there" are different facts.
        """
        ...

    def stat(self, digest: str) -> BlobRef | None:
        """The reference for ``digest``, or ``None`` if it is not stored.

        Raises exactly what :meth:`exists` raises, for the same reasons: a
        :class:`~paa.storage.coldlake.errors.NotFound` for an absent
        substrate namespace, and the substrate's own refusals.
        """
        ...

    def delete(self, digest: str) -> bool:
        """Remove the blob. Returns whether there was one to remove.

        A blob that is not there is not an error: the answer is ``False``.
        Everything that stops the store from answering is, exactly as
        :meth:`exists` says — a
        :class:`~paa.storage.coldlake.errors.NotFound` for an absent
        substrate namespace, and the substrate's own refusals.
        """
        ...

    def __repr__(self) -> str: ...
